from __future__ import annotations

import hashlib
import io
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, BinaryIO

from mcap.records import Channel, Message
from mcap.stream_reader import StreamReader

from .schemas import egocentric_pb2 as pb
from .storage import read_json, verify_checksums


class _HashingRawReader(io.RawIOBase):
    """在 MCAP 顺序解析时同步计算 SHA-256，避免再次读取大文件。"""

    def __init__(self, source: BinaryIO) -> None:
        self.source = source
        self.digest = hashlib.sha256()
        self.bytes_read = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: bytearray) -> int:
        count = self.source.readinto(buffer)
        if count:
            self.digest.update(memoryview(buffer)[:count])
            self.bytes_read += count
        return count


def build_quality_report(
    session_dir: Path,
    config: dict[str, Any],
    *,
    verify_hashes: bool = False,
) -> dict[str, Any]:
    manifest = read_json(session_dir / "session.json")
    segment_paths = sorted((session_dir / "segments").glob("*.mcap"))
    topic_times: dict[str, list[int]] = defaultdict(list)
    topic_bytes: dict[str, int] = defaultdict(int)
    camera_device_times: dict[str, list[int]] = defaultdict(list)
    camera_sequences: dict[str, list[int]] = defaultdict(list)
    wearable_sequence: list[int] = []
    queue_drops = 0
    segment_reports: list[dict[str, Any]] = []
    segment_checksums: dict[str, str] = {}
    errors: list[str] = []

    for path in segment_paths:
        try:
            report, checksum, observed_drops = _scan_segment(
                path,
                topic_times,
                topic_bytes,
                camera_device_times,
                camera_sequences,
                wearable_sequence,
            )
            segment_reports.append(report)
            segment_checksums[str(path.relative_to(session_dir))] = checksum
            queue_drops = max(queue_drops, observed_drops)
        except Exception as exc:
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")

    required = [
        *(f"/camera/cam_{suffix}/video" for suffix in "abcd"),
        *(f"/camera/cam_{suffix}/timing" for suffix in "abcd"),
        "/imu/oak",
        "/wearable/emg",
        "/wearable/imu",
        "/wearable/raw",
        "/system/clock_sync",
        "/system/health",
        "/system/events",
    ]
    missing_topics = [topic for topic in required if not topic_times.get(topic)]
    if missing_topics:
        errors.append("缺少主题: " + ", ".join(missing_topics))

    rates = {
        topic: _rate(values)
        for topic, values in topic_times.items()
        if len(values) >= 2
    }
    sequence_stats = {
        camera: _sequence_stats(values, modulo=None)
        for camera, values in camera_sequences.items()
    }
    sequence_stats["wearable"] = _sequence_stats(
        wearable_sequence,
        modulo=256,
    )
    sequence_gaps = {
        key: int(value["gaps"]) for key, value in sequence_stats.items()
    }
    quality_config = config.get("quality", {})
    if quality_config.get("require_zero_sequence_gaps", True) and any(
        value for value in sequence_gaps.values()
    ):
        errors.append(f"发现序号缺失: {sequence_gaps}")
    if quality_config.get("require_zero_queue_drops", True) and queue_drops:
        errors.append(f"应用写入队列丢弃 {queue_drops} 条消息")

    oak_config = config.get("oak", {})
    for suffix in "abcd":
        topic = f"/camera/cam_{suffix}/timing"
        if rates.get(topic, 0.0) < float(oak_config.get("minimum_fps", 24)):
            errors.append(
                f"cam_{suffix} 帧率 {rates.get(topic, 0.0):.2f} Hz 低于阈值"
            )
    oak_imu_rate = rates.get("/imu/oak", 0.0)
    oak_imu_minimum = float(
        oak_config.get("imu", {}).get("minimum_hz", 80)
    )
    if oak_imu_rate < oak_imu_minimum:
        errors.append(f"OAK IMU {oak_imu_rate:.2f} Hz 低于阈值")
    wearable_config = config.get("wearable", {})
    emg_rate = rates.get("/wearable/emg", 0.0)
    if emg_rate < float(wearable_config.get("emg_minimum_hz", 225)):
        errors.append(f"EMG {emg_rate:.2f} Hz 低于阈值")
    baseline = float(manifest.get("wearable_imu_baseline_hz") or 0.0)
    wearable_imu_minimum = baseline * float(
        wearable_config.get("imu_minimum_baseline_ratio", 0.8)
    )
    wearable_imu_rate = rates.get("/wearable/imu", 0.0)
    if wearable_imu_rate < max(1.0, wearable_imu_minimum):
        errors.append(
            f"手环 IMU {wearable_imu_rate:.2f} Hz 低于预热基线阈值 "
            f"{wearable_imu_minimum:.2f} Hz"
        )

    sync = (
        (manifest.get("online_quality") or {}).get("camera_sync")
        or _camera_sync(
            camera_device_times,
            max_window_ms=float(
                quality_config.get("camera_sync_max_ms", 5)
            ),
            duplicate_frames=sum(
                int(sequence_stats[camera]["duplicate_or_reordered"])
                for camera in camera_sequences
            ),
        )
    )
    p95_limit = float(quality_config.get("camera_sync_p95_ms", 0.5))
    max_limit = float(quality_config.get("camera_sync_max_ms", 5))
    if sync["complete_groups"] == 0:
        errors.append("没有形成四相机同步组")
    elif (
        sync["missing_groups"]
        or sync["duplicate_frames"]
        or sync["unmatched_frames"]
        or sync["p95_ms"] > p95_limit
        or sync["max_ms"] > max_limit
    ):
        errors.append(
            "四相机同步不完整或超限: "
            f"missing={sync['missing_groups']}, "
            f"duplicate={sync['duplicate_frames']}, "
            f"unmatched={sync['unmatched_frames']}, "
            f"p95={sync['p95_ms']:.3f} ms, "
            f"max={sync['max_ms']:.3f} ms"
        )

    checksum_errors = (
        verify_checksums(
            session_dir,
            precomputed=segment_checksums,
        )
        if verify_hashes
        else []
    )
    errors.extend(checksum_errors)
    return {
        "pass": not errors,
        "errors": errors,
        "segments": segment_reports,
        "topic_counts": {
            topic: len(values) for topic, values in sorted(topic_times.items())
        },
        "topic_record_bytes": dict(sorted(topic_bytes.items())),
        "rates_hz": rates,
        "sequence_gaps": sequence_gaps,
        "sequence_stats": sequence_stats,
        "queue_drops": queue_drops,
        "camera_sync": sync,
        "checksum_errors": checksum_errors,
        "_segment_checksums": segment_checksums,
    }


def _scan_segment(
    path: Path,
    topic_times: dict[str, list[int]],
    topic_bytes: dict[str, int],
    camera_device_times: dict[str, list[int]],
    camera_sequences: dict[str, list[int]],
    wearable_sequence: list[int],
) -> tuple[dict[str, Any], str, int]:
    channels: dict[int, str] = {}
    counts: dict[str, int] = defaultdict(int)
    first_time: int | None = None
    last_time: int | None = None
    queue_drops = 0
    with path.open("rb", buffering=0) as source:
        hashing_reader = _HashingRawReader(source)
        reader = StreamReader(
            hashing_reader,
            emit_chunks=False,
            validate_crcs=True,
        )
        for record in reader.records:
            if isinstance(record, Channel):
                channels[record.id] = record.topic
                continue
            if not isinstance(record, Message):
                continue
            topic = channels.get(record.channel_id)
            if topic is None:
                raise ValueError(f"消息引用未知 Channel {record.channel_id}")
            log_time = int(record.log_time)
            counts[topic] += 1
            topic_times[topic].append(log_time)
            topic_bytes[topic] += len(record.data)
            first_time = log_time if first_time is None else min(first_time, log_time)
            last_time = log_time if last_time is None else max(last_time, log_time)
            if topic.startswith("/camera/") and topic.endswith("/timing"):
                timing = pb.CameraTiming.FromString(record.data)
                camera = str(timing.camera)
                camera_sequences[camera].append(int(timing.sequence))
                if timing.stamp.HasField("source_device_ns"):
                    camera_device_times[camera].append(
                        int(timing.stamp.source_device_ns)
                    )
            elif topic == "/wearable/emg":
                wearable_sequence.append(
                    int(pb.WearableEmgSample.FromString(record.data).sequence)
                )
            elif topic == "/wearable/imu":
                wearable_sequence.append(
                    int(pb.WearableImuSample.FromString(record.data).sequence)
                )
            elif topic == "/system/health":
                queue_drops = max(
                    queue_drops,
                    int(pb.HealthSample.FromString(record.data).queue_drops),
                )
        checksum = hashing_reader.digest.hexdigest()
    return (
        {
            "path": str(path),
            "size_bytes": path.stat().st_size,
            "bytes_read": hashing_reader.bytes_read,
            "message_count": sum(counts.values()),
            "topics": dict(sorted(counts.items())),
            "first_log_time_ns": first_time,
            "last_log_time_ns": last_time,
            "crc_validated": True,
        },
        checksum,
        queue_drops,
    )


def _rate(timestamps: list[int]) -> float:
    if len(timestamps) < 2:
        return 0.0
    ordered = sorted(timestamps)
    duration = (ordered[-1] - ordered[0]) / 1_000_000_000
    return (len(ordered) - 1) / duration if duration > 0 else 0.0


def _sequence_stats(
    values: list[int],
    modulo: int | None,
) -> dict[str, int | None]:
    if not values:
        return {
            "count": 0,
            "first_sequence": None,
            "last_sequence": None,
            "gaps": 0,
            "duplicate_or_reordered": 0,
        }
    gaps = 0
    duplicate_or_reordered = 0
    for previous, current in zip(values, values[1:], strict=False):
        expected = previous + 1
        if modulo is not None:
            expected %= modulo
            if current == previous:
                duplicate_or_reordered += 1
            elif current != expected:
                gaps += (current - expected) % modulo
        elif current <= previous:
            duplicate_or_reordered += 1
        elif current != expected:
            gaps += current - expected
    return {
        "count": len(values),
        "first_sequence": values[0],
        "last_sequence": values[-1],
        "gaps": gaps,
        "duplicate_or_reordered": duplicate_or_reordered,
    }


def _sequence_gaps(values: list[int], modulo: int | None) -> int:
    """保留旧测试和外部调用使用的序号缺失接口。"""
    return int(_sequence_stats(values, modulo)["gaps"])


def _camera_sync(
    timestamps: dict[str, list[int]],
    *,
    max_window_ms: float = 5.0,
    duplicate_frames: int = 0,
) -> dict[str, Any]:
    required = ["cam_a", "cam_b", "cam_c", "cam_d"]
    ordered = {camera: sorted(timestamps.get(camera, [])) for camera in required}
    indices = {camera: 0 for camera in required}
    unmatched = {camera: 0 for camera in required}
    spreads: list[float] = []
    window_ns = int(max_window_ms * 1_000_000)

    while all(indices[camera] < len(ordered[camera]) for camera in required):
        current = {
            camera: ordered[camera][indices[camera]] for camera in required
        }
        earliest_camera = min(current, key=current.get)
        spread_ns = max(current.values()) - min(current.values())
        if spread_ns <= window_ns:
            spreads.append(spread_ns / 1_000_000)
            for camera in required:
                indices[camera] += 1
        else:
            unmatched[earliest_camera] += 1
            indices[earliest_camera] += 1

    for camera in required:
        remaining = len(ordered[camera]) - indices[camera]
        unmatched[camera] += remaining

    complete_groups = len(spreads)
    expected_groups = max((len(values) for values in ordered.values()), default=0)
    missing_groups = max(0, expected_groups - complete_groups)
    ordered_spreads = sorted(spreads)
    return {
        "complete_groups": complete_groups,
        "matched_groups": complete_groups,
        "missing_groups": missing_groups,
        "duplicate_frames": duplicate_frames,
        "unmatched_frames": sum(unmatched.values()),
        "unmatched_by_camera": unmatched,
        "p50_ms": _percentile(ordered_spreads, 0.50),
        "p95_ms": _percentile(ordered_spreads, 0.95),
        "max_ms": max(ordered_spreads, default=0.0),
        "mean_ms": statistics.fmean(ordered_spreads) if ordered_spreads else 0.0,
    }


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    index = min(len(values) - 1, int(percentile * len(values)))
    return values[index]
