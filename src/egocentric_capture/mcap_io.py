from __future__ import annotations

import os
import queue
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable

from foxglove_schemas_protobuf.CompressedVideo_pb2 import CompressedVideo
from google.protobuf.timestamp_pb2 import Timestamp
from mcap.reader import make_reader
from mcap.records import Attachment, Channel, Message, Metadata, Schema
from mcap.stream_reader import StreamReader
from mcap.writer import CompressionType
from mcap.writer import Writer as RawMcapWriter
from mcap_protobuf.writer import Writer as ProtobufWriter

from .models import (
    CameraFrame,
    ClockStamp,
    ClockSyncSample,
    HealthSnapshot,
    OakImuSample,
    SystemEvent,
    WearableEmgSample,
    WearableImuSample,
    WearableRawChunk,
)
from .schemas import egocentric_pb2 as pb


class WriterQueueFull(RuntimeError):
    """MCAP 写入队列已满，继续接收会破坏实时性或数据完整性。"""


@dataclass(frozen=True, slots=True)
class McapRecord:
    topic: str
    message: Any
    log_time: int
    publish_time: int
    sequence: int = 0


@dataclass(slots=True)
class SegmentInfo:
    path: str
    started_unix_ns: int
    ended_unix_ns: int = 0
    message_count: int = 0
    size_bytes: int = 0


@dataclass(slots=True)
class _CounterState:
    count: int = 0
    first_sequence: int | None = None
    last_sequence: int | None = None
    sequence_gaps: int = 0
    duplicate_or_reordered: int = 0

    def push(self, sequence: int | None) -> None:
        self.count += 1
        if sequence is None:
            return
        if self.first_sequence is None:
            self.first_sequence = sequence
        elif self.last_sequence is not None:
            if sequence == self.last_sequence:
                self.duplicate_or_reordered += 1
            elif sequence < self.last_sequence:
                self.duplicate_or_reordered += 1
            elif sequence > self.last_sequence + 1:
                self.sequence_gaps += sequence - self.last_sequence - 1
        self.last_sequence = sequence


_STOP = object()


class SegmentedMcapWriter:
    """由单一线程完成序列化、分段、刷新和关闭 MCAP 文件。"""

    def __init__(
        self,
        session_dir: Path,
        *,
        queue_size: int,
        chunk_size_bytes: int,
        segment_duration_s: float,
        segment_size_bytes: int,
        fsync_interval_s: float,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self.session_dir = Path(session_dir)
        self.segment_dir = self.session_dir / "segments"
        self.queue: queue.Queue[Any] = queue.Queue(maxsize=queue_size)
        self.chunk_size_bytes = int(chunk_size_bytes)
        self.segment_duration_s = float(segment_duration_s)
        self.segment_size_bytes = int(segment_size_bytes)
        self.fsync_interval_s = float(fsync_interval_s)
        self.on_error = on_error
        self.segments: list[SegmentInfo] = []
        self.error: str | None = None
        self.queue_drops = 0
        self.high_watermark = 0
        self._thread: threading.Thread | None = None
        self._stop_requested = threading.Event()
        self._accepting = False
        self._state_lock = threading.RLock()
        self._stop_lock = threading.Lock()
        self._metrics_lock = threading.Lock()
        self._accepted: dict[str, _CounterState] = {}
        self._persisted: dict[str, _CounterState] = {}
        self._record_counts: dict[str, int] = {}
        self._persisted_bytes = 0
        self._throughput_samples: deque[tuple[float, int]] = deque(maxlen=128)

    @property
    def running(self) -> bool:
        with self._state_lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def queue_depth(self) -> int:
        return self.queue.qsize()

    @property
    def accepted_counts(self) -> dict[str, int]:
        with self._metrics_lock:
            return {key: value.count for key, value in self._accepted.items()}

    @property
    def persisted_counts(self) -> dict[str, int]:
        with self._metrics_lock:
            return {key: value.count for key, value in self._persisted.items()}

    @property
    def record_counts(self) -> dict[str, int]:
        with self._metrics_lock:
            return dict(self._record_counts)

    @property
    def bytes_per_second(self) -> float:
        now = time.monotonic()
        with self._metrics_lock:
            while (
                len(self._throughput_samples) > 1
                and now - self._throughput_samples[0][0] > 5.0
            ):
                self._throughput_samples.popleft()
            if len(self._throughput_samples) < 2:
                return 0.0
            started_at, started_bytes = self._throughput_samples[0]
            ended_at, ended_bytes = self._throughput_samples[-1]
        elapsed = ended_at - started_at
        if elapsed <= 0:
            return 0.0
        return max(0.0, (ended_bytes - started_bytes) / elapsed)

    @property
    def sequence_stats(self) -> dict[str, dict[str, int | None]]:
        with self._metrics_lock:
            return {
                key: {
                    "count": value.count,
                    "first_sequence": value.first_sequence,
                    "last_sequence": value.last_sequence,
                    "sequence_gaps": value.sequence_gaps,
                    "duplicate_or_reordered": value.duplicate_or_reordered,
                }
                for key, value in self._persisted.items()
            }

    def start(self) -> None:
        with self._stop_lock:
            with self._state_lock:
                if self._thread is not None and self._thread.is_alive():
                    return
                self.segment_dir.mkdir(parents=True, exist_ok=True)
                self._stop_requested.clear()
                self._accepting = True
                self._thread = threading.Thread(
                    target=self._run,
                    name="mcap-writer",
                    daemon=True,
                )
                self._thread.start()

    def submit(self, record: McapRecord) -> None:
        """兼容旧调用；新采集路径应使用 submit_sample。"""
        self._submit_item(record, record.topic, record.sequence)

    def submit_sample(self, sample: Any) -> None:
        key = _sample_count_key(sample)
        self._submit_item(sample, key, _sample_sequence(sample))

    def _submit_item(
        self,
        item: Any,
        count_key: str,
        sequence: int | None,
    ) -> None:
        with self._state_lock:
            if self.error is not None:
                raise RuntimeError(self.error)
            if (
                self._thread is None
                or not self._thread.is_alive()
                or not self._accepting
            ):
                raise RuntimeError("MCAP 写入线程未运行")
            try:
                self.queue.put_nowait(item)
            except queue.Full as exc:
                with self._metrics_lock:
                    self.queue_drops += 1
                raise WriterQueueFull(
                    "MCAP 写入队列已满，已停止录制以避免采集线程阻塞和数据静默丢失"
                ) from exc
            depth = self.queue.qsize()
            with self._metrics_lock:
                self.high_watermark = max(self.high_watermark, depth)
                self._accepted.setdefault(count_key, _CounterState()).push(
                    sequence
                )

    def stop(self, timeout_s: float = 30) -> list[SegmentInfo]:
        with self._stop_lock:
            with self._state_lock:
                thread = self._thread
                if thread is None:
                    return list(self.segments)
                self._accepting = False
                self._stop_requested.set()
            deadline = time.monotonic() + timeout_s
            while thread.is_alive():
                try:
                    self.queue.put(_STOP, timeout=0.1)
                    break
                except queue.Full:
                    if self.error is not None or not thread.is_alive():
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError("无法通知 MCAP 写入线程停止")
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(timeout=remaining)
            if thread.is_alive():
                raise TimeoutError("MCAP 写入线程未在限定时间内结束")
            with self._state_lock:
                if self._thread is thread:
                    self._thread = None
                error = self.error
            if error is not None:
                raise RuntimeError(error)
            return list(self.segments)

    def _run(self) -> None:
        file: BinaryIO | None = None
        writer: ProtobufWriter | None = None
        segment: SegmentInfo | None = None
        opened_monotonic = 0.0
        last_fsync = 0.0
        try:
            while True:
                item = self.queue.get()
                try:
                    if item is _STOP:
                        break
                    records = (
                        [item]
                        if isinstance(item, McapRecord)
                        else record_for_sample(item)
                    )
                    if not records:
                        continue
                    first_record = records[0]
                    if writer is None or file is None or segment is None:
                        file, writer, segment = self._open_segment(
                            first_record.log_time
                        )
                        opened_monotonic = time.monotonic()
                        last_fsync = opened_monotonic
                    elif self._should_rotate(file, opened_monotonic):
                        self._close_segment(file, writer, segment)
                        file, writer, segment = self._open_segment(
                            first_record.log_time
                        )
                        opened_monotonic = time.monotonic()
                        last_fsync = opened_monotonic
                    bytes_before = file.tell()
                    for record in records:
                        writer.write_message(
                            record.topic,
                            record.message,
                            log_time=record.log_time,
                            publish_time=record.publish_time,
                            sequence=record.sequence,
                        )
                        segment.message_count += 1
                        segment.ended_unix_ns = max(
                            segment.ended_unix_ns,
                            record.log_time,
                        )
                    count_key = (
                        first_record.topic
                        if isinstance(item, McapRecord)
                        else _sample_count_key(item)
                    )
                    sequence = (
                        first_record.sequence
                        if isinstance(item, McapRecord)
                        else _sample_sequence(item)
                    )
                    bytes_after = file.tell()
                    with self._metrics_lock:
                        self._persisted.setdefault(
                            count_key, _CounterState()
                        ).push(sequence)
                        for record in records:
                            self._record_counts[record.topic] = (
                                self._record_counts.get(record.topic, 0) + 1
                            )
                        self._persisted_bytes += max(
                            0,
                            bytes_after - bytes_before,
                        )
                        self._throughput_samples.append(
                            (time.monotonic(), self._persisted_bytes)
                        )
                    if time.monotonic() - last_fsync >= self.fsync_interval_s:
                        file.flush()
                        os.fsync(file.fileno())
                        last_fsync = time.monotonic()
                finally:
                    self.queue.task_done()
        except Exception as exc:
            self.error = f"MCAP 写入失败: {type(exc).__name__}: {exc}"
            if self.on_error is not None:
                self.on_error(self.error)
        finally:
            if file is not None and writer is not None and segment is not None:
                try:
                    self._close_segment(file, writer, segment)
                except Exception as exc:
                    if self.error is None:
                        self.error = f"MCAP 关闭失败: {type(exc).__name__}: {exc}"
                        if self.on_error is not None:
                            self.on_error(self.error)

    def _open_segment(
        self, started_unix_ns: int
    ) -> tuple[BinaryIO, ProtobufWriter, SegmentInfo]:
        index = len(self.segments)
        path = self.segment_dir / f"{index:04d}.mcap"
        file = path.open("wb", buffering=1024 * 1024)
        writer = ProtobufWriter(
            file,
            chunk_size=self.chunk_size_bytes,
            compression=CompressionType.NONE,
            enable_crcs=True,
        )
        segment = SegmentInfo(
            path=str(path.relative_to(self.session_dir)),
            started_unix_ns=started_unix_ns,
            ended_unix_ns=started_unix_ns,
        )
        self.segments.append(segment)
        return file, writer, segment

    def _should_rotate(self, file: BinaryIO, opened_monotonic: float) -> bool:
        return (
            time.monotonic() - opened_monotonic >= self.segment_duration_s
            or file.tell() >= self.segment_size_bytes
        )

    @staticmethod
    def _close_segment(
        file: BinaryIO,
        writer: ProtobufWriter,
        segment: SegmentInfo,
    ) -> None:
        writer.finish()
        file.flush()
        os.fsync(file.fileno())
        segment.size_bytes = file.tell()
        file.close()


def record_for_sample(sample: Any) -> list[McapRecord]:
    if isinstance(sample, CameraFrame):
        timestamp = Timestamp(
            seconds=sample.stamp.unix_ns // 1_000_000_000,
            nanos=sample.stamp.unix_ns % 1_000_000_000,
        )
        video = CompressedVideo(
            timestamp=timestamp,
            frame_id=sample.camera,
            data=sample.payload,
            format="h264",
        )
        timing = pb.CameraTiming(
            stamp=_clock_reference(sample.stamp),
            camera=sample.camera,
            socket=sample.socket,
            sequence=sample.sequence,
            frame_type=sample.frame_type,
            width=sample.width,
            height=sample.height,
            codec=sample.codec,
            payload_bytes=len(sample.payload),
            keyframe=sample.is_keyframe,
        )
        return [
            McapRecord(
                topic=f"/camera/{sample.camera}/video",
                message=video,
                log_time=sample.stamp.unix_ns,
                publish_time=sample.stamp.unix_ns,
                sequence=sample.sequence,
            ),
            McapRecord(
                topic=f"/camera/{sample.camera}/timing",
                message=timing,
                log_time=sample.stamp.unix_ns,
                publish_time=sample.stamp.unix_ns,
                sequence=sample.sequence,
            ),
        ]
    if isinstance(sample, OakImuSample):
        message = pb.OakImuSample(stamp=_clock_reference(sample.stamp))
        _extend_if_present(message.accel_m_s2, sample.accel)
        _extend_if_present(message.gyro_rad_s, sample.gyro)
        _extend_if_present(message.magnetic_ut, sample.magnetic)
        _extend_if_present(message.quaternion_xyzw, sample.quaternion)
        message.sensor_device_timestamps_ns.update(
            {key: value for key, value in sample.sensor_timestamps_ns.items() if value}
        )
        message.sensor_host_timestamps_ns.update(
            {
                key: value
                for key, value in sample.sensor_host_timestamps_ns.items()
                if value
            }
        )
        if sample.orientation_accuracy is not None:
            message.orientation_accuracy = sample.orientation_accuracy
        return [_record("/imu/oak", message, sample.stamp.unix_ns)]
    if isinstance(sample, WearableRawChunk):
        message = pb.WearableRawChunk(
            data=sample.data,
            read_start_monotonic_ns=sample.read_start_monotonic_ns,
            read_end_monotonic_ns=sample.read_end_monotonic_ns,
            normalized_unix_ns=sample.unix_ns,
        )
        return [_record("/wearable/raw", message, sample.unix_ns)]
    if isinstance(sample, WearableEmgSample):
        message = pb.WearableEmgSample(
            stamp=_clock_reference(sample.stamp),
            sequence=sample.sequence,
            channels_uv=sample.channels_uv,
        )
        return [
            McapRecord(
                "/wearable/emg",
                message,
                sample.stamp.unix_ns,
                sample.stamp.unix_ns,
                sample.sequence,
            )
        ]
    if isinstance(sample, WearableImuSample):
        message = pb.WearableImuSample(
            stamp=_clock_reference(sample.stamp),
            sequence=sample.sequence,
            gyro_raw=sample.gyro_raw,
            accel_raw=sample.accel_raw,
            gyro_rad_s=sample.gyro_rad_s,
            accel_m_s2=sample.accel_m_s2,
        )
        return [
            McapRecord(
                "/wearable/imu",
                message,
                sample.stamp.unix_ns,
                sample.stamp.unix_ns,
                sample.sequence,
            )
        ]
    if isinstance(sample, HealthSnapshot):
        message = pb.HealthSample(
            stamp=_clock_reference(sample.stamp),
            rates_hz=sample.rates_hz,
            last_seen_age_s=sample.last_seen_age_s,
            sequence_gaps=sample.sequence_gaps,
            queue_depth=sample.queue_depth,
            queue_drops=sample.queue_drops,
            disk_free_bytes=sample.disk_free_bytes,
            ready=sample.ready,
        )
        return [_record("/system/health", message, sample.stamp.unix_ns)]
    if isinstance(sample, SystemEvent):
        message = pb.SystemEvent(
            stamp=_clock_reference(sample.stamp),
            level=sample.level,
            code=sample.code,
            message=sample.message,
            details={key: str(value) for key, value in sample.details.items()},
        )
        return [_record("/system/events", message, sample.stamp.unix_ns)]
    if isinstance(sample, ClockSyncSample):
        message = pb.ClockSync(
            host_monotonic_ns=sample.monotonic_ns,
            unix_ns=sample.unix_ns,
            uncertainty_ns=sample.uncertainty_ns,
            offset_jitter_ns=sample.offset_jitter_ns,
        )
        return [_record("/system/clock_sync", message, sample.unix_ns)]
    raise TypeError(f"不支持写入 MCAP 的样本类型: {type(sample).__name__}")


def clock_sync_record(
    monotonic_ns: int,
    unix_ns: int,
    uncertainty_ns: int,
    offset_jitter_ns: float,
) -> McapRecord:
    message = pb.ClockSync(
        host_monotonic_ns=monotonic_ns,
        unix_ns=unix_ns,
        uncertainty_ns=uncertainty_ns,
        offset_jitter_ns=offset_jitter_ns,
    )
    return _record("/system/clock_sync", message, unix_ns)


def validate_mcap(path: Path) -> dict[str, Any]:
    counts: dict[str, int] = {}
    first_time: int | None = None
    last_time: int | None = None
    with path.open("rb") as file:
        reader = make_reader(file)
        for _, channel, message in reader.iter_messages(log_time_order=False):
            counts[channel.topic] = counts.get(channel.topic, 0) + 1
            first_time = (
                message.log_time
                if first_time is None
                else min(first_time, message.log_time)
            )
            last_time = (
                message.log_time
                if last_time is None
                else max(last_time, message.log_time)
            )
    return {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "message_count": sum(counts.values()),
        "topics": counts,
        "first_log_time_ns": first_time,
        "last_log_time_ns": last_time,
    }


def recover_mcap(source: Path, destination: Path) -> dict[str, Any]:
    schemas: dict[int, int] = {}
    channels: dict[int, int] = {}
    recovered = 0
    discarded_error: str | None = None
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as input_file, destination.open("wb") as output_file:
        writer = RawMcapWriter(
            output_file,
            compression=CompressionType.NONE,
            enable_crcs=True,
        )
        writer.start(library="egocentric-capture recover")
        try:
            for record in StreamReader(
                input_file,
                emit_chunks=False,
                validate_crcs=False,
            ).records:
                if isinstance(record, Schema):
                    schemas[record.id] = writer.register_schema(
                        record.name,
                        record.encoding,
                        record.data,
                    )
                elif isinstance(record, Channel):
                    schema_id = schemas.get(record.schema_id, 0)
                    channels[record.id] = writer.register_channel(
                        record.topic,
                        record.message_encoding,
                        schema_id,
                        metadata=record.metadata,
                    )
                elif isinstance(record, Message) and record.channel_id in channels:
                    writer.add_message(
                        channels[record.channel_id],
                        record.log_time,
                        record.data,
                        record.publish_time,
                        record.sequence,
                    )
                    recovered += 1
                elif isinstance(record, Metadata):
                    writer.add_metadata(record.name, record.metadata)
                elif isinstance(record, Attachment):
                    writer.add_attachment(
                        record.create_time,
                        record.log_time,
                        record.name,
                        record.media_type,
                        record.data,
                    )
        except Exception as exc:
            discarded_error = f"{type(exc).__name__}: {exc}"
        writer.finish()
        output_file.flush()
        os.fsync(output_file.fileno())
    return {
        "source": str(source),
        "destination": str(destination),
        "recovered_messages": recovered,
        "lossy": discarded_error is not None,
        "error": discarded_error,
    }


def segment_dicts(segments: list[SegmentInfo]) -> list[dict[str, Any]]:
    return [asdict(segment) for segment in segments]


def _record(topic: str, message: Any, timestamp_ns: int) -> McapRecord:
    return McapRecord(topic, message, timestamp_ns, timestamp_ns)


def _clock_reference(stamp: ClockStamp) -> pb.ClockReference:
    message = pb.ClockReference(
        normalized_unix_ns=stamp.unix_ns,
        host_monotonic_ns=stamp.monotonic_ns,
        uncertainty_ns=stamp.uncertainty_ns,
        quality=stamp.quality.value,
    )
    if stamp.source_device_ns is not None:
        message.source_device_ns = stamp.source_device_ns
    if stamp.source_host_ns is not None:
        message.source_host_ns = stamp.source_host_ns
    if stamp.arrival_monotonic_ns is not None:
        message.arrival_monotonic_ns = stamp.arrival_monotonic_ns
    return message


def _extend_if_present(target: Any, values: tuple[float, ...] | None) -> None:
    if values is not None:
        target.extend(values)


def _sample_count_key(sample: Any) -> str:
    if isinstance(sample, CameraFrame):
        return f"camera/{sample.camera}"
    if isinstance(sample, OakImuSample):
        return "imu/oak"
    if isinstance(sample, WearableRawChunk):
        return "wearable/raw"
    if isinstance(sample, WearableEmgSample):
        return "wearable/emg"
    if isinstance(sample, WearableImuSample):
        return "wearable/imu"
    if isinstance(sample, HealthSnapshot):
        return "system/health"
    if isinstance(sample, SystemEvent):
        return "system/events"
    if isinstance(sample, ClockSyncSample):
        return "system/clock_sync"
    raise TypeError(f"无法统计写入样本类型: {type(sample).__name__}")


def _sample_sequence(sample: Any) -> int | None:
    if isinstance(
        sample,
        (CameraFrame, WearableEmgSample, WearableImuSample),
    ):
        return int(sample.sequence)
    return None
