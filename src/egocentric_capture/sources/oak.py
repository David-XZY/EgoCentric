from __future__ import annotations

import queue
import re
import threading
import time
from collections import defaultdict
from typing import Any

from ..clocks import HostClockMapper
from ..models import CameraFrame, OakImuSample, TimestampQuality
from ..oak_runtime.depthai_runtime import (
    build_encoded_camera_streams,
    build_imu_stream,
    close_message_queue,
    collect_device_snapshot,
    device_timestamp_ns,
    extract_imu_wide_samples,
    get_messages_any,
    host_timestamp_ns,
    import_depthai,
    open_device,
    select_device_info,
    sequence_num,
    start_pipeline,
    start_pipeline_context,
    stop_pipeline,
)
from ..oak_runtime.encoded_video import encoded_message_bytes
from .base import SourceCallbacks

_ANNEX_B_START_CODE = re.compile(b"\x00\x00\x00\x01|\x00\x00\x01")


class OakSource:
    """阻塞接收 OAK 编码帧和 IMU，并在异常后按退避策略重连。"""

    _RECONNECT_DELAYS = (0.5, 1.0, 2.0, 5.0)

    def __init__(
        self,
        config: dict[str, Any],
        clock: HostClockMapper,
        callbacks: SourceCallbacks,
    ) -> None:
        self.config = config
        self.clock = clock
        self.callbacks = callbacks
        self._thread: threading.Thread | None = None
        self._imu_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._drain_event = threading.Event()
        self._ready_event = threading.Event()
        self._resource_lock = threading.Lock()
        self._queues: dict[str, Any] = {}
        self._pipeline: Any = None
        self.reconnect_count = 0
        self._diagnostics_enabled = bool(config.get("diagnostics_enabled", False))
        self._diag_started_ns = time.monotonic_ns()
        self._diag_counts: dict[str, int] = defaultdict(int)
        self._diag_last_arrival_ns: dict[str, int] = {}
        self._diag_gap_min_ns: dict[str, int] = {}
        self._diag_gap_max_ns: dict[str, int] = {}
        self._diag_age_total_ns: dict[str, int] = defaultdict(int)
        self._diag_age_max_ns: dict[str, int] = defaultdict(int)
        self._diag_callback_total_ns: dict[str, int] = defaultdict(int)
        self._diag_callback_max_ns: dict[str, int] = defaultdict(int)

    @property
    def running(self) -> bool:
        return any(
            thread is not None and thread.is_alive()
            for thread in (self._thread, self._imu_thread)
        )

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._drain_event.clear()
        self._ready_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="oak-source",
            daemon=True,
        )
        self._thread.start()

    def wait_until_ready(self, timeout_s: float) -> bool:
        return self._ready_event.wait(timeout_s)

    def stop(self) -> None:
        self._stop_event.set()
        self._ready_event.clear()
        with self._resource_lock:
            queues = list(self._queues.values())
            pipeline = self._pipeline
        for message_queue in queues:
            close_message_queue(message_queue)
        if pipeline is not None:
            stop_pipeline(pipeline)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=8)
        imu_thread = self._imu_thread
        if imu_thread is not None:
            imu_thread.join(timeout=2)
        if self.running:
            self.callbacks.on_error(
                "oak_stop_timeout",
                "OAK 接收线程未按时退出，保留线程和设备句柄以便诊断",
            )
            return
        self._thread = None
        self._imu_thread = None

    def drain_and_stop(self) -> None:
        self._drain_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=8)
        imu_thread = self._imu_thread
        if imu_thread is not None:
            imu_thread.join(timeout=2)
        if self.running:
            self.callbacks.on_error(
                "oak_drain_timeout",
                "OAK 排空线程未按时退出，转为强制关闭队列",
            )
            self.stop()
            return
        self._thread = None
        self._imu_thread = None

    def _run(self) -> None:
        attempt = 0
        while not self._stop_event.is_set() and not self._drain_event.is_set():
            connected_at = time.monotonic()
            try:
                self._run_once()
                if not self._stop_event.is_set():
                    raise RuntimeError("OAK 接收循环意外退出")
            except Exception as exc:
                if self._stop_event.is_set() or self._drain_event.is_set():
                    break
                self.callbacks.on_error(
                    "oak_source",
                    f"{type(exc).__name__}: {exc}",
                )
                self.reconnect_count += 1
                if self.callbacks.on_metadata is not None:
                    self.callbacks.on_metadata(
                        "oak_supervisor",
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
        pipeline = None
        queues: dict[str, Any] = {}
        imu_thread: threading.Thread | None = None
        imu_consumer_stop = threading.Event()
        imu_producer_stopped = threading.Event()
        imu_failures: queue.Queue[Exception] = queue.Queue(maxsize=1)
        try:
            dai = import_depthai()
            device_info = select_device_info(dai, self.config.get("mxid"))
            device = open_device(dai, device_info)
            snapshot = collect_device_snapshot(
                dai,
                device_info,
                device=device,
            )
            if self.callbacks.on_metadata is not None:
                snapshot["reconnect_count"] = self.reconnect_count
                self.callbacks.on_metadata("oak", snapshot)
            pipeline = start_pipeline_context(
                dai,
                device_info,
                device=device,
            )
            cameras = list(self.config.get("cameras") or [])
            width = int(self.config.get("width", 1920))
            height = int(self.config.get("height", 1080))
            sensor_fps = int(self.config.get("sensor_fps", 30))
            codec = str(self.config.get("codec", "H264_MAIN"))
            hardware = dict(self.config.get("hardware_sync") or {})
            hardware_runtime = {
                "enabled": bool(hardware.get("enabled", True)),
                "pulse_width_sec": float(hardware.get("pulse_width_s", 0.001)),
                "script_overhead_sec": float(
                    hardware.get("script_overhead_s", 0.009)
                ),
            }
            streams = build_encoded_camera_streams(
                pipeline,
                dai,
                cameras,
                width,
                height,
                sensor_fps,
                int(self.config.get("video_host_queue_frames", 90)),
                codec,
                bitrate_kbps=int(self.config.get("bitrate_kbps", 12000)),
                num_frames_pool=4,
                keyframe_frequency=int(
                    self.config.get("keyframe_frequency", sensor_fps)
                ),
                num_b_frames=int(self.config.get("num_b_frames", 0)),
                hardware_sync_config=hardware_runtime,
                camera_control_config={"mode": "auto"},
            )
            imu_stream = build_imu_stream(
                pipeline,
                dai,
                dict(self.config.get("imu") or {}),
                int(self.config.get("imu_host_queue_samples", 400)),
            )
            if imu_stream is None:
                raise RuntimeError("OAK 未提供可用 IMU 输出")
            stream_by_name = {stream.name: stream for stream in streams}
            video_queues = {
                stream.name: stream.queue for stream in streams
            }
            queues = {
                **video_queues,
                "imu/oak": imu_stream.queue,
            }
            parameter_sets: dict[str, dict[int, bytes]] = {
                stream.name: {} for stream in streams
            }
            with self._resource_lock:
                self._pipeline = pipeline
                self._queues = dict(queues)
            start_pipeline(pipeline)
            imu_thread = threading.Thread(
                target=self._consume_imu_queue,
                args=(
                    imu_stream.queue,
                    imu_consumer_stop,
                    imu_producer_stopped,
                    imu_failures,
                ),
                name="oak-imu-source",
                daemon=True,
            )
            with self._resource_lock:
                self._imu_thread = imu_thread
            imu_thread.start()
            self._ready_event.set()
            while not self._stop_event.is_set():
                self._raise_imu_failure(imu_failures)
                if self._drain_event.is_set():
                    stop = getattr(pipeline, "stop", None)
                    if callable(stop):
                        stop()
                    imu_producer_stopped.set()
                    self._drain_available(
                        stream_by_name,
                        video_queues,
                        width,
                        height,
                        parameter_sets,
                    )
                    imu_thread.join(timeout=3)
                    if imu_thread.is_alive():
                        raise TimeoutError("OAK IMU 队列未在 3 秒内排空")
                    self._raise_imu_failure(imu_failures)
                    return
                messages = get_messages_any(
                    dai,
                    video_queues,
                    timeout_s=0.2,
                )
                self._raise_imu_failure(imu_failures)
                if not messages:
                    continue
                for key, message in messages.items():
                    self._ingest_video_message(
                        stream_by_name[key],
                        message,
                        width,
                        height,
                        parameter_sets[key],
                    )
        finally:
            self._ready_event.clear()
            imu_consumer_stop.set()
            imu_producer_stopped.set()
            for message_queue in queues.values():
                close_message_queue(message_queue)
            if pipeline is not None:
                stop_pipeline(pipeline)
            if imu_thread is not None:
                imu_thread.join(timeout=3)
            with self._resource_lock:
                if self._pipeline is pipeline:
                    self._pipeline = None
                    self._queues = {}
                if (
                    self._imu_thread is imu_thread
                    and (
                        imu_thread is None
                        or not imu_thread.is_alive()
                    )
                ):
                    self._imu_thread = None

    def _consume_imu_queue(
        self,
        message_queue: Any,
        consumer_stop: threading.Event,
        producer_stopped: threading.Event,
        failures: queue.Queue[Exception],
    ) -> None:
        try:
            while not self._stop_event.is_set() and not consumer_stop.is_set():
                messages = _queue_messages_nowait(message_queue)
                if messages:
                    for message in messages:
                        self._ingest_imu_message(message)
                    continue
                if self._drain_event.is_set() and producer_stopped.is_set():
                    return
                consumer_stop.wait(0.001)
        except Exception as exc:
            stopping = (
                self._stop_event.is_set()
                or consumer_stop.is_set()
                or (
                    self._drain_event.is_set()
                    and producer_stopped.is_set()
                )
            )
            if not stopping:
                try:
                    failures.put_nowait(exc)
                except queue.Full:
                    pass

    @staticmethod
    def _raise_imu_failure(failures: queue.Queue[Exception]) -> None:
        try:
            failure = failures.get_nowait()
        except queue.Empty:
            return
        raise RuntimeError(
            f"OAK IMU 接收线程异常: {type(failure).__name__}: {failure}"
        ) from failure

    def _drain_available(
        self,
        stream_by_name: dict[str, Any],
        queues: dict[str, Any],
        width: int,
        height: int,
        parameter_sets: dict[str, dict[int, bytes]],
    ) -> None:
        pending: dict[str, list[Any]] = {}
        for key, message_queue in queues.items():
            try_get_all = getattr(message_queue, "tryGetAll", None)
            if callable(try_get_all):
                try:
                    pending[key] = list(try_get_all() or [])
                except Exception:
                    pending[key] = []
        while any(pending.values()):
            for key in queues:
                messages = pending.get(key) or []
                if not messages:
                    continue
                message = messages.pop(0)
                if key == "imu/oak":
                    self._ingest_imu_message(message)
                else:
                    self._ingest_video_message(
                        stream_by_name[key],
                        message,
                        width,
                        height,
                        parameter_sets[key],
                    )

    def _ingest_video_message(
        self,
        stream: Any,
        message: Any,
        width: int,
        height: int,
        parameter_sets: dict[int, bytes],
    ) -> None:
        arrival = time.monotonic_ns()
        device_ns = device_timestamp_ns(message)
        source_host_ns = host_timestamp_ns(message)
        host_monotonic = _host_monotonic_ns(source_host_ns, arrival)
        payload = encoded_message_bytes(message)
        frame_type = _encoded_frame_type(message)
        payload = _ensure_h264_parameter_sets(
            payload,
            _is_keyframe(frame_type),
            parameter_sets,
        )
        sequence = sequence_num(message)
        if sequence is None:
            raise RuntimeError(f"{stream.name} 编码帧缺少序号")
        callback_started_ns = time.monotonic_ns()
        self.callbacks.on_sample(
            CameraFrame(
                camera=stream.name,
                socket=stream.socket,
                sequence=sequence,
                frame_type=frame_type,
                width=width,
                height=height,
                codec=stream.codec,
                payload=payload,
                stamp=self.clock.stamp(
                    host_monotonic,
                    source_device_ns=device_ns,
                    source_host_ns=source_host_ns,
                    arrival_monotonic_ns=arrival,
                    quality=TimestampQuality.DEVICE_SYNCED,
                ),
            )
        )
        callback_ns = time.monotonic_ns() - callback_started_ns
        self._record_video_diagnostics(
            stream.name,
            arrival,
            max(0, arrival - host_monotonic),
            callback_ns,
        )

    def _ingest_imu_message(self, message: Any) -> None:
        arrival = time.monotonic_ns()
        for row in extract_imu_wide_samples(message):
            source_host_ns = _integer(row.get("host_timestamp_ns"))
            host_monotonic = _host_monotonic_ns(source_host_ns, arrival)
            self.callbacks.on_sample(
                OakImuSample(
                    stamp=self.clock.stamp(
                        host_monotonic,
                        source_device_ns=_integer(
                            row.get("device_timestamp_ns")
                        ),
                        source_host_ns=source_host_ns,
                        arrival_monotonic_ns=arrival,
                        quality=TimestampQuality.DEVICE_SYNCED,
                    ),
                    accel=_triple(
                        row,
                        (
                            "accel_x_m_s2",
                            "accel_y_m_s2",
                            "accel_z_m_s2",
                        ),
                    ),
                    gyro=_triple(
                        row,
                        (
                            "gyro_x_rad_s",
                            "gyro_y_rad_s",
                            "gyro_z_rad_s",
                        ),
                    ),
                    magnetic=_triple(
                        row,
                        ("mag_x_uT", "mag_y_uT", "mag_z_uT"),
                    ),
                    quaternion=_quadruple(
                        row,
                        ("quat_x", "quat_y", "quat_z", "quat_w"),
                    ),
                    sensor_timestamps_ns={
                        key: _integer(row.get(f"{key}_device_timestamp_ns"))
                        for key in ("accel", "gyro", "mag", "rotation")
                    },
                    sensor_host_timestamps_ns={
                        key: _integer(row.get(f"{key}_host_timestamp_ns"))
                        for key in ("accel", "gyro", "mag", "rotation")
                    },
                    orientation_accuracy=_float_or_none(
                        row.get("orientation_accuracy")
                    ),
                )
            )

    def _record_video_diagnostics(
        self,
        camera: str,
        arrival_ns: int,
        source_age_ns: int,
        callback_ns: int,
    ) -> None:
        if not self._diagnostics_enabled:
            return
        previous = self._diag_last_arrival_ns.get(camera)
        if previous is not None:
            gap_ns = max(0, arrival_ns - previous)
            self._diag_gap_min_ns[camera] = min(
                self._diag_gap_min_ns.get(camera, gap_ns),
                gap_ns,
            )
            self._diag_gap_max_ns[camera] = max(
                self._diag_gap_max_ns.get(camera, 0),
                gap_ns,
            )
        self._diag_last_arrival_ns[camera] = arrival_ns
        self._diag_counts[camera] += 1
        self._diag_age_total_ns[camera] += source_age_ns
        self._diag_age_max_ns[camera] = max(
            self._diag_age_max_ns[camera],
            source_age_ns,
        )
        self._diag_callback_total_ns[camera] += callback_ns
        self._diag_callback_max_ns[camera] = max(
            self._diag_callback_max_ns[camera],
            callback_ns,
        )
        elapsed_ns = arrival_ns - self._diag_started_ns
        if elapsed_ns < 1_000_000_000:
            return
        elapsed_s = elapsed_ns / 1_000_000_000
        parts: list[str] = []
        for name in sorted(self._diag_counts):
            count = self._diag_counts[name]
            divisor = max(1, count)
            parts.append(
                f"{name}:到达={count / elapsed_s:.1f}fps,"
                f"间隔={self._diag_gap_min_ns.get(name, 0) / 1_000_000:.1f}"
                f"..{self._diag_gap_max_ns.get(name, 0) / 1_000_000:.1f}ms,"
                f"队列年龄={self._diag_age_total_ns[name] / divisor / 1_000_000:.1f}"
                f"/{self._diag_age_max_ns[name] / 1_000_000:.1f}ms,"
                f"回调={self._diag_callback_total_ns[name] / divisor / 1_000_000:.2f}"
                f"/{self._diag_callback_max_ns[name] / 1_000_000:.2f}ms"
            )
        print("[oak-diag] " + " | ".join(parts), flush=True)
        self._diag_started_ns = arrival_ns
        self._diag_counts.clear()
        self._diag_gap_min_ns.clear()
        self._diag_gap_max_ns.clear()
        self._diag_age_total_ns.clear()
        self._diag_age_max_ns.clear()
        self._diag_callback_total_ns.clear()
        self._diag_callback_max_ns.clear()


def _encoded_frame_type(message: Any) -> str:
    getter = getattr(message, "getFrameType", None)
    if not callable(getter):
        return "Unknown"
    try:
        value = getter()
    except Exception:
        return "Unknown"
    name = getattr(value, "name", None)
    return str(name or str(value).rsplit(".", 1)[-1])


def _queue_messages_nowait(message_queue: Any) -> list[Any]:
    try_get_all = getattr(message_queue, "tryGetAll", None)
    if callable(try_get_all):
        return list(try_get_all() or [])
    try_get = getattr(message_queue, "tryGet", None)
    if callable(try_get):
        message = try_get()
        return [] if message is None else [message]
    raise RuntimeError("OAK IMU 队列不支持 tryGetAll/tryGet")


def _is_keyframe(frame_type: str) -> bool:
    return frame_type.upper() in {"I", "IDR", "KEY"}


def _ensure_h264_parameter_sets(
    payload: bytes,
    keyframe: bool,
    cached: dict[int, bytes],
) -> bytes:
    if not payload:
        return payload
    if not keyframe and 7 in cached and 8 in cached:
        return payload
    present: set[int] = set()
    for nal_type, unit in _annex_b_units(payload):
        present.add(nal_type)
        if nal_type in {7, 8}:
            cached[nal_type] = unit
    if not keyframe:
        return payload
    missing = [nal_type for nal_type in (7, 8) if nal_type not in present]
    if not missing:
        return payload
    if any(nal_type not in cached for nal_type in missing):
        raise RuntimeError("H.264 关键帧缺少 SPS/PPS，无法保证独立解码")
    return b"".join(cached[nal_type] for nal_type in missing) + payload


def _annex_b_units(payload: bytes) -> list[tuple[int, bytes]]:
    starts = [
        (match.start(), match.end() - match.start())
        for match in _ANNEX_B_START_CODE.finditer(payload)
    ]
    units: list[tuple[int, bytes]] = []
    for unit_index, (start, prefix_size) in enumerate(starts):
        end = starts[unit_index + 1][0] if unit_index + 1 < len(starts) else len(payload)
        header = start + prefix_size
        if header < end:
            units.append((payload[header] & 0x1F, payload[start:end]))
    return units


def _host_monotonic_ns(source_host_ns: int | None, arrival_ns: int) -> int:
    if source_host_ns is None:
        return arrival_ns
    if abs(arrival_ns - source_host_ns) <= 60_000_000_000:
        return source_host_ns
    return arrival_ns


def _integer(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _triple(
    row: dict[str, Any],
    names: tuple[str, str, str],
) -> tuple[float, float, float] | None:
    values = tuple(_float_or_none(row.get(name)) for name in names)
    if any(value is None for value in values):
        return None
    return values  # type: ignore[return-value]


def _quadruple(
    row: dict[str, Any],
    names: tuple[str, str, str, str],
) -> tuple[float, float, float, float] | None:
    values = tuple(_float_or_none(row.get(name)) for name in names)
    if any(value is None for value in values):
        return None
    return values  # type: ignore[return-value]
