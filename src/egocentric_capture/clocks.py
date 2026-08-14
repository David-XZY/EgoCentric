from __future__ import annotations

import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass

from .models import ClockStamp, TimestampQuality


@dataclass(frozen=True, slots=True)
class ClockAnchor:
    monotonic_ns: int
    unix_ns: int
    uncertainty_ns: int


class HostClockMapper:
    """保存单调时钟到 Unix 时钟的稳定映射。"""

    def __init__(self, capacity: int = 64) -> None:
        self._anchors: deque[ClockAnchor] = deque(maxlen=capacity)
        self._lock = threading.RLock()
        self.sample()

    @property
    def anchors(self) -> tuple[ClockAnchor, ...]:
        with self._lock:
            return tuple(self._anchors)

    def sample(self, attempts: int = 7) -> ClockAnchor:
        candidates: list[ClockAnchor] = []
        for _ in range(max(1, attempts)):
            before = time.monotonic_ns()
            unix_ns = time.time_ns()
            after = time.monotonic_ns()
            candidates.append(
                ClockAnchor(
                    monotonic_ns=(before + after) // 2,
                    unix_ns=unix_ns,
                    uncertainty_ns=max(1, (after - before) // 2),
                )
            )
        anchor = min(candidates, key=lambda item: item.uncertainty_ns)
        with self._lock:
            self._anchors.append(anchor)
        return anchor

    def to_unix_ns(self, monotonic_ns: int) -> int:
        with self._lock:
            anchors = tuple(self._anchors)
        anchor = min(
            anchors,
            key=lambda item: abs(item.monotonic_ns - monotonic_ns),
        )
        return anchor.unix_ns + monotonic_ns - anchor.monotonic_ns

    def stamp(
        self,
        monotonic_ns: int | None = None,
        *,
        source_device_ns: int | None = None,
        source_host_ns: int | None = None,
        arrival_monotonic_ns: int | None = None,
        uncertainty_ns: int = 0,
        quality: TimestampQuality = TimestampQuality.EXACT_HOST,
    ) -> ClockStamp:
        value = time.monotonic_ns() if monotonic_ns is None else int(monotonic_ns)
        return ClockStamp(
            monotonic_ns=value,
            unix_ns=self.to_unix_ns(value),
            source_device_ns=source_device_ns,
            source_host_ns=source_host_ns,
            arrival_monotonic_ns=arrival_monotonic_ns,
            uncertainty_ns=max(0, int(uncertainty_ns)),
            quality=quality,
        )

    def offset_jitter_ns(self) -> float:
        with self._lock:
            anchors = tuple(self._anchors)
        offsets = [item.unix_ns - item.monotonic_ns for item in anchors]
        return statistics.pstdev(offsets) if len(offsets) > 1 else 0.0


def estimate_serial_frame_monotonic_ns(
    *,
    chunk_start_offset: int,
    frame_end_offset: int,
    chunk_length: int,
    read_start_monotonic_ns: int,
    read_end_monotonic_ns: int,
    baudrate: int,
) -> tuple[int, int]:
    """按串口线速和帧在字节流中的位置估计帧结束时间。"""
    if baudrate <= 0:
        raise ValueError("波特率必须大于零")
    if frame_end_offset < chunk_start_offset:
        raise ValueError("帧结束位置早于当前串口块")
    bytes_after = max(
        0,
        chunk_start_offset + chunk_length - frame_end_offset,
    )
    nanoseconds_per_byte = 10_000_000_000 / baudrate
    estimated = int(read_end_monotonic_ns - bytes_after * nanoseconds_per_byte)
    read_uncertainty = max(0, read_end_monotonic_ns - read_start_monotonic_ns)
    transport_uncertainty = int(max(1, chunk_length) * nanoseconds_per_byte)
    return estimated, read_uncertainty + transport_uncertainty
