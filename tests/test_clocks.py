from __future__ import annotations

import threading
import time

import pytest

from egocentric_capture.clocks import (
    HostClockMapper,
    estimate_serial_frame_monotonic_ns,
)


def test_host_clock_mapping_is_locally_linear() -> None:
    mapper = HostClockMapper()
    anchor = mapper.anchors[-1]
    assert mapper.to_unix_ns(anchor.monotonic_ns + 123_456) == (
        anchor.unix_ns + 123_456
    )


def test_host_clock_mapping_is_thread_safe() -> None:
    mapper = HostClockMapper(capacity=8)
    errors: list[Exception] = []

    def sample_clock() -> None:
        try:
            for _ in range(500):
                mapper.sample(attempts=1)
        except Exception as exc:
            errors.append(exc)

    def read_clock() -> None:
        try:
            for _ in range(2_000):
                mapper.stamp(time.monotonic_ns())
                mapper.offset_jitter_ns()
                assert mapper.anchors
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=sample_clock),
        threading.Thread(target=read_clock),
        threading.Thread(target=read_clock),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []


def test_serial_timestamp_uses_byte_position_and_8n1_wire_time() -> None:
    estimated, uncertainty = estimate_serial_frame_monotonic_ns(
        chunk_start_offset=100,
        frame_end_offset=120,
        chunk_length=40,
        read_start_monotonic_ns=1_000_000_000,
        read_end_monotonic_ns=1_002_000_000,
        baudrate=1_000_000,
    )
    assert estimated == 1_001_800_000
    assert uncertainty == 2_400_000


def test_serial_timestamp_rejects_invalid_baudrate() -> None:
    with pytest.raises(ValueError):
        estimate_serial_frame_monotonic_ns(
            chunk_start_offset=0,
            frame_end_offset=0,
            chunk_length=1,
            read_start_monotonic_ns=0,
            read_end_monotonic_ns=1,
            baudrate=0,
        )
