from __future__ import annotations

import threading
import time
from pathlib import Path

import serial
from serial.tools import list_ports

from ..clocks import HostClockMapper, estimate_serial_frame_monotonic_ns
from ..models import (
    TimestampQuality,
    WearableEmgSample,
    WearableImuSample,
    WearableRawChunk,
)
from ..protocol import EmgFrame, FrameParser, ImuFrame
from .base import SourceCallbacks


def discover_serial_port(
    preferred: str | None = None,
    *,
    vid: int = 0x10C4,
    pid: int = 0xEA60,
    serial_number: str | None = None,
) -> str:
    if preferred and preferred != "auto":
        path = Path(preferred)
        if not path.exists():
            raise FileNotFoundError(f"串口不存在: {preferred}")
        return str(path)
    detected = list(list_ports.comports())
    candidates = [
        item
        for item in detected
        if item.vid == vid
        and item.pid == pid
        and (
            serial_number is None
            or str(item.serial_number or "") == serial_number
        )
    ]
    if not candidates:
        identity = f"VID=0x{vid:04x}, PID=0x{pid:04x}"
        if serial_number:
            identity += f", serial={serial_number}"
        raise FileNotFoundError(f"未发现匹配肌电手环串口: {identity}")
    if len(candidates) > 1 and not serial_number:
        serials = ", ".join(
            str(item.serial_number or item.device) for item in candidates
        )
        raise RuntimeError(
            "发现多台 CP210x 手环设备，请配置 wearable.serial_number: "
            + serials
        )
    selected = sorted(candidates, key=lambda item: item.device)[0]
    return _serial_by_id_path(selected.device) or selected.device


def _serial_by_id_path(device: str) -> str | None:
    directory = Path("/dev/serial/by-id")
    if not directory.is_dir():
        return None
    target = Path(device).resolve()
    for candidate in sorted(directory.iterdir()):
        try:
            if candidate.resolve() == target:
                return str(candidate)
        except OSError:
            continue
    return None


class WearableSource:
    """持续保存串口原始块，并解析 EMG 与手环 IMU。"""

    _RECONNECT_DELAYS = (0.5, 1.0, 2.0, 5.0)

    def __init__(
        self,
        config: dict,
        clock: HostClockMapper,
        callbacks: SourceCallbacks,
    ) -> None:
        self.config = config
        self.clock = clock
        self.callbacks = callbacks
        self.port: str | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._drain_event = threading.Event()
        self._connection: serial.Serial | None = None
        self.reconnect_count = 0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._drain_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="wearable-source",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        connection = self._connection
        if connection is not None:
            try:
                connection.cancel_read()
            except (AttributeError, OSError):
                pass
            try:
                connection.close()
            except OSError:
                pass
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=3)
        if thread.is_alive():
            self.callbacks.on_error(
                "wearable_stop_timeout",
                "手环接收线程未在 3 秒内退出，保留线程和串口句柄以便诊断",
            )
            return
        self._thread = None

    def drain_and_stop(self) -> None:
        self._drain_event.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=1)
        if thread.is_alive():
            self.stop()
            return
        self._thread = None

    def _run(self) -> None:
        attempt = 0
        while not self._stop_event.is_set() and not self._drain_event.is_set():
            connected_at = time.monotonic()
            try:
                self._run_once()
                if not self._stop_event.is_set():
                    raise RuntimeError("手环接收循环意外退出")
            except Exception as exc:
                if self._stop_event.is_set() or self._drain_event.is_set():
                    break
                self.callbacks.on_error(
                    "wearable_source",
                    f"{type(exc).__name__}: {exc}",
                )
                self.reconnect_count += 1
                if self.callbacks.on_metadata is not None:
                    self.callbacks.on_metadata(
                        "wearable_supervisor",
                        {"reconnect_count": self.reconnect_count},
                    )
                if time.monotonic() - connected_at >= 5:
                    attempt = 0
                delay = self._RECONNECT_DELAYS[
                    min(attempt, len(self._RECONNECT_DELAYS) - 1)
                ]
                attempt += 1
                self._stop_event.wait(delay)

    def _run_once(self) -> None:
        parser = FrameParser()
        total_received = 0
        previous_discarded = 0
        previous_invalid = 0
        diagnostics_enabled = bool(
            self.config.get("diagnostics_enabled", False)
        )
        diag_started_ns = time.monotonic_ns()
        diag_bytes = 0
        diag_reads = 0
        diag_emg = 0
        diag_imu = 0
        diag_last_read_ns: int | None = None
        diag_max_gap_ns = 0
        diag_callback_total_ns = 0
        diag_callback_max_ns = 0
        diag_callback_count = 0
        diag_frame_age_total_ns = 0
        diag_frame_age_max_ns = 0
        diag_frame_age_count = 0
        diag_uncertainty_total_ns = 0
        diag_uncertainty_max_ns = 0
        diag_chunk_bytes = 0
        diag_chunk_max_bytes = 0
        diag_sequence_missing = 0
        diag_sequence_reordered = 0
        diag_last_sequence: int | None = None
        diag_discarded_bytes = 0
        diag_invalid_frames = 0
        baudrate = int(self.config.get("baudrate", 921600))
        vid = _configured_usb_id(self.config.get("vid"), 0x10C4)
        pid = _configured_usb_id(self.config.get("pid"), 0xEA60)
        serial_number = self.config.get("serial_number")
        try:
            self.port = discover_serial_port(
                str(self.config.get("port", "auto")),
                vid=vid,
                pid=pid,
                serial_number=(
                    str(serial_number) if serial_number not in (None, "") else None
                ),
            )
            if self.callbacks.on_metadata is not None:
                self.callbacks.on_metadata(
                    "wearable",
                    {
                        "port": self.port,
                        "baudrate": baudrate,
                        "vid": vid,
                        "pid": pid,
                        "serial_number": serial_number,
                        "reconnect_count": self.reconnect_count,
                    },
                )
            with serial.Serial(
                port=self.port,
                baudrate=baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=float(self.config.get("read_timeout_s", 0.05)),
            ) as connection:
                self._connection = connection
                while not self._stop_event.is_set():
                    read_start = time.monotonic_ns()
                    waiting = connection.in_waiting
                    if self._drain_event.is_set() and waiting <= 0:
                        break
                    chunk = connection.read(max(waiting, 1))
                    read_end = time.monotonic_ns()
                    if not chunk:
                        continue
                    if diag_last_read_ns is not None:
                        diag_max_gap_ns = max(
                            diag_max_gap_ns,
                            read_end - diag_last_read_ns,
                        )
                    diag_last_read_ns = read_end
                    diag_bytes += len(chunk)
                    diag_reads += 1
                    diag_chunk_bytes += len(chunk)
                    diag_chunk_max_bytes = max(diag_chunk_max_bytes, len(chunk))
                    chunk_start = total_received
                    total_received += len(chunk)
                    callback_started_ns = time.monotonic_ns()
                    self.callbacks.on_sample(
                        WearableRawChunk(
                            data=bytes(chunk),
                            read_start_monotonic_ns=read_start,
                            read_end_monotonic_ns=read_end,
                            unix_ns=self.clock.to_unix_ns(read_end),
                        )
                    )
                    callback_ns = time.monotonic_ns() - callback_started_ns
                    diag_callback_total_ns += callback_ns
                    diag_callback_max_ns = max(
                        diag_callback_max_ns,
                        callback_ns,
                    )
                    diag_callback_count += 1
                    for parsed in parser.feed(chunk):
                        missing, reordered = _sequence_discontinuity(
                            diag_last_sequence,
                            parsed.frame.sequence,
                        )
                        diag_sequence_missing += missing
                        diag_sequence_reordered += reordered
                        diag_last_sequence = parsed.frame.sequence
                        estimated, uncertainty = estimate_serial_frame_monotonic_ns(
                            chunk_start_offset=chunk_start,
                            frame_end_offset=parsed.end_offset,
                            chunk_length=len(chunk),
                            read_start_monotonic_ns=read_start,
                            read_end_monotonic_ns=read_end,
                            baudrate=baudrate,
                        )
                        stamp = self.clock.stamp(
                            estimated,
                            arrival_monotonic_ns=read_end,
                            uncertainty_ns=uncertainty,
                            quality=TimestampQuality.SERIAL_ESTIMATED,
                        )
                        frame_age_ns = max(0, time.monotonic_ns() - estimated)
                        diag_frame_age_total_ns += frame_age_ns
                        diag_frame_age_max_ns = max(
                            diag_frame_age_max_ns,
                            frame_age_ns,
                        )
                        diag_frame_age_count += 1
                        diag_uncertainty_total_ns += uncertainty
                        diag_uncertainty_max_ns = max(
                            diag_uncertainty_max_ns,
                            uncertainty,
                        )
                        if isinstance(parsed.frame, EmgFrame):
                            diag_emg += 1
                            callback_started_ns = time.monotonic_ns()
                            self.callbacks.on_sample(
                                WearableEmgSample(
                                    sequence=parsed.frame.sequence,
                                    channels_uv=parsed.frame.channels,
                                    stamp=stamp,
                                )
                            )
                            callback_ns = (
                                time.monotonic_ns() - callback_started_ns
                            )
                        elif isinstance(parsed.frame, ImuFrame):
                            diag_imu += 1
                            callback_started_ns = time.monotonic_ns()
                            self.callbacks.on_sample(
                                WearableImuSample(
                                    sequence=parsed.frame.sequence,
                                    gyro_raw=parsed.frame.gyro_raw,
                                    accel_raw=parsed.frame.accel_raw,
                                    gyro_rad_s=parsed.frame.gyro,
                                    accel_m_s2=parsed.frame.accel,
                                    stamp=stamp,
                                )
                            )
                            callback_ns = (
                                time.monotonic_ns() - callback_started_ns
                            )
                        else:
                            continue
                        diag_callback_total_ns += callback_ns
                        diag_callback_max_ns = max(
                            diag_callback_max_ns,
                            callback_ns,
                        )
                        diag_callback_count += 1
                    if parser.discarded_bytes > previous_discarded:
                        delta = parser.discarded_bytes - previous_discarded
                        previous_discarded = parser.discarded_bytes
                        diag_discarded_bytes += delta
                        self.callbacks.on_error(
                            "wearable_parser_discard",
                            f"手环串口解析丢弃 {delta} 字节",
                        )
                    if parser.invalid_frames > previous_invalid:
                        delta = parser.invalid_frames - previous_invalid
                        previous_invalid = parser.invalid_frames
                        diag_invalid_frames += delta
                        self.callbacks.on_error(
                            "wearable_invalid_frame",
                            f"手环串口发现 {delta} 个无效帧",
                        )
                    if (
                        diagnostics_enabled
                        and read_end - diag_started_ns >= 1_000_000_000
                    ):
                        elapsed_s = (
                            read_end - diag_started_ns
                        ) / 1_000_000_000
                        callback_avg_ms = (
                            diag_callback_total_ns
                            / max(diag_callback_count, 1)
                            / 1_000_000
                        )
                        frame_age_avg_ms = (
                            diag_frame_age_total_ns
                            / max(diag_frame_age_count, 1)
                            / 1_000_000
                        )
                        uncertainty_avg_ms = (
                            diag_uncertainty_total_ns
                            / max(diag_frame_age_count, 1)
                            / 1_000_000
                        )
                        print(
                            "[wearable-diag] "
                            f"字节={diag_bytes / elapsed_s:.0f}B/s,"
                            f"read={diag_reads / elapsed_s:.1f}Hz,"
                            f"块={diag_chunk_bytes / max(diag_reads, 1):.1f}"
                            f"/{diag_chunk_max_bytes}B,"
                            f"最长无数据={diag_max_gap_ns / 1_000_000:.1f}ms,"
                            f"EMG={diag_emg / elapsed_s:.1f}Hz,"
                            f"IMU={diag_imu / elapsed_s:.1f}Hz,"
                            f"帧到回调={frame_age_avg_ms:.2f}"
                            f"/{diag_frame_age_max_ns / 1_000_000:.2f}ms,"
                            f"时间不确定度={uncertainty_avg_ms:.2f}"
                            f"/{diag_uncertainty_max_ns / 1_000_000:.2f}ms,"
                            f"序号缺失={diag_sequence_missing},"
                            f"乱序重复={diag_sequence_reordered},"
                            f"解析丢弃={diag_discarded_bytes},"
                            f"无效帧={diag_invalid_frames},"
                            f"回调={callback_avg_ms:.3f}"
                            f"/{diag_callback_max_ns / 1_000_000:.3f}ms",
                            flush=True,
                        )
                        diag_started_ns = read_end
                        diag_bytes = 0
                        diag_reads = 0
                        diag_emg = 0
                        diag_imu = 0
                        diag_max_gap_ns = 0
                        diag_callback_total_ns = 0
                        diag_callback_max_ns = 0
                        diag_callback_count = 0
                        diag_frame_age_total_ns = 0
                        diag_frame_age_max_ns = 0
                        diag_frame_age_count = 0
                        diag_uncertainty_total_ns = 0
                        diag_uncertainty_max_ns = 0
                        diag_chunk_bytes = 0
                        diag_chunk_max_bytes = 0
                        diag_sequence_missing = 0
                        diag_sequence_reordered = 0
                        diag_discarded_bytes = 0
                        diag_invalid_frames = 0
        finally:
            self._connection = None


def _sequence_discontinuity(
    previous: int | None,
    current: int,
) -> tuple[int, int]:
    if previous is None:
        return 0, 0
    expected = (previous + 1) % 256
    if current == expected:
        return 0, 0
    forward = (current - expected) % 256
    if 0 < forward <= 127:
        return forward, 0
    return 0, 1


def _configured_usb_id(value: object, default: int) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, str):
        return int(value, 0)
    return int(value)
