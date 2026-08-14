from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CaptureState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CHECKING = "CHECKING"
    READY = "READY"
    ARMING = "ARMING"
    RECORDING = "RECORDING"
    FINALIZING = "FINALIZING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class TimestampQuality(str, Enum):
    EXACT_HOST = "exact_host"
    DEVICE_SYNCED = "device_synced"
    SERIAL_ESTIMATED = "serial_estimated"


@dataclass(frozen=True, slots=True)
class CaptureRequest:
    participant_id: str
    task_id: str
    hand: str
    operator: str
    notes: str = ""
    duration_s: float | None = None


@dataclass(frozen=True, slots=True)
class ClockStamp:
    monotonic_ns: int
    unix_ns: int
    source_device_ns: int | None = None
    source_host_ns: int | None = None
    arrival_monotonic_ns: int | None = None
    uncertainty_ns: int = 0
    quality: TimestampQuality = TimestampQuality.EXACT_HOST


@dataclass(frozen=True, slots=True)
class CameraFrame:
    camera: str
    socket: str
    sequence: int
    frame_type: str
    width: int
    height: int
    codec: str
    payload: bytes
    stamp: ClockStamp

    @property
    def is_keyframe(self) -> bool:
        return self.frame_type.upper() in {"I", "IDR", "KEY"}


@dataclass(frozen=True, slots=True)
class OakImuSample:
    stamp: ClockStamp
    accel: tuple[float, float, float] | None = None
    gyro: tuple[float, float, float] | None = None
    magnetic: tuple[float, float, float] | None = None
    quaternion: tuple[float, float, float, float] | None = None
    sensor_timestamps_ns: dict[str, int | None] = field(default_factory=dict)
    sensor_host_timestamps_ns: dict[str, int | None] = field(default_factory=dict)
    orientation_accuracy: float | None = None


@dataclass(frozen=True, slots=True)
class WearableRawChunk:
    data: bytes
    read_start_monotonic_ns: int
    read_end_monotonic_ns: int
    unix_ns: int


@dataclass(frozen=True, slots=True)
class WearableEmgSample:
    sequence: int
    channels_uv: tuple[int, int, int, int, int, int, int, int]
    stamp: ClockStamp


@dataclass(frozen=True, slots=True)
class WearableImuSample:
    sequence: int
    gyro_raw: tuple[int, int, int]
    accel_raw: tuple[int, int, int]
    gyro_rad_s: tuple[float, float, float]
    accel_m_s2: tuple[float, float, float]
    stamp: ClockStamp


@dataclass(frozen=True, slots=True)
class TrackedHand:
    handedness: str
    landmarks: tuple[tuple[float, float, float], ...]
    world_landmarks: tuple[tuple[float, float, float], ...]
    gesture: str
    confidence: float
    stale_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class GestureFrame:
    monotonic_ns: int
    hands: tuple[TrackedHand, ...] = ()
    inference_ms: float = 0.0
    healthy: bool = True
    message: str = ""


@dataclass(frozen=True, slots=True)
class SystemEvent:
    level: str
    code: str
    message: str
    stamp: ClockStamp
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ClockSyncSample:
    monotonic_ns: int
    unix_ns: int
    uncertainty_ns: int
    offset_jitter_ns: float


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    stamp: ClockStamp
    rates_hz: dict[str, float]
    last_seen_age_s: dict[str, float]
    sequence_gaps: dict[str, int]
    queue_depth: int
    queue_drops: int
    disk_free_bytes: int
    ready: bool
    received_counts: dict[str, int] = field(default_factory=dict)
    writer_accepted_counts: dict[str, int] = field(default_factory=dict)
    persisted_counts: dict[str, int] = field(default_factory=dict)
    preview_submitted_counts: dict[str, int] = field(default_factory=dict)
    preview_rendered_counts: dict[str, int] = field(default_factory=dict)
    preview_latency_ms: dict[str, float] = field(default_factory=dict)
    writer_high_watermark: int = 0
    source_reconnects: dict[str, int] = field(default_factory=dict)
    event_loop_lag_ms: float = 0.0
    preview_cross_camera_skew_ms: float = 0.0
    writer_bytes_per_second: float = 0.0


@dataclass(frozen=True, slots=True)
class FinalizeProgress:
    stage: str
    completed: int
    total: int
    message: str


@dataclass(frozen=True, slots=True)
class PreviewHealth:
    camera: str
    submitted_count: int
    rendered_count: int
    last_submitted_sequence: int | None
    last_rendered_sequence: int | None
    latency_ms: float
    healthy: bool
    message: str = ""
    rendered_source_monotonic_ns: int | None = None
