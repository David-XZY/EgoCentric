from __future__ import annotations

import queue
import threading
import time

import pytest

from egocentric_capture.clocks import HostClockMapper
from egocentric_capture.mcap_io import (
    SegmentedMcapWriter,
    WriterQueueFull,
    record_for_sample,
    recover_mcap,
    validate_mcap,
)
from egocentric_capture.models import SystemEvent


def test_segment_rotation_and_validation(tmp_path) -> None:
    writer = SegmentedMcapWriter(
        tmp_path,
        queue_size=20,
        chunk_size_bytes=1024,
        segment_duration_s=0,
        segment_size_bytes=1024**3,
        fsync_interval_s=10,
    )
    mapper = HostClockMapper()
    writer.start()
    for index in range(3):
        event = SystemEvent("info", f"event_{index}", "测试", mapper.stamp())
        writer.submit(record_for_sample(event)[0])
        time.sleep(0.01)
    segments = writer.stop()
    assert len(segments) == 3
    assert sum(validate_mcap(tmp_path / item.path)["message_count"] for item in segments) == 3


def test_recover_truncated_segment(tmp_path) -> None:
    writer = SegmentedMcapWriter(
        tmp_path,
        queue_size=20,
        chunk_size_bytes=1024,
        segment_duration_s=60,
        segment_size_bytes=1024**3,
        fsync_interval_s=10,
    )
    mapper = HostClockMapper()
    writer.start()
    for index in range(20):
        writer.submit(
            record_for_sample(
                SystemEvent("info", str(index), "恢复测试", mapper.stamp())
            )[0]
        )
    source = tmp_path / writer.stop()[0].path
    truncated = tmp_path / "truncated.mcap"
    truncated.write_bytes(source.read_bytes()[:-128])
    recovered = tmp_path / "recovered.mcap"
    report = recover_mcap(truncated, recovered)
    assert report["recovered_messages"] == 20
    assert report["lossy"] is True
    assert validate_mcap(recovered)["message_count"] == 20


def test_writer_queue_full_fails_fast_without_blocking_source(tmp_path) -> None:
    writer = SegmentedMcapWriter(
        tmp_path,
        queue_size=1,
        chunk_size_bytes=1024,
        segment_duration_s=60,
        segment_size_bytes=1024**3,
        fsync_interval_s=10,
    )
    release = threading.Event()
    original_run = writer._run

    def gated_run() -> None:
        release.wait()
        original_run()

    writer._run = gated_run
    writer.start()
    mapper = HostClockMapper()
    first = SystemEvent("info", "first", "队列测试", mapper.stamp())
    second = SystemEvent("info", "second", "队列测试", mapper.stamp())
    writer.submit_sample(first)
    started = time.monotonic()
    try:
        writer.submit_sample(second)
    except WriterQueueFull:
        pass
    else:
        raise AssertionError("写入队列满时应立即失败")
    assert time.monotonic() - started < 0.05
    assert writer.queue_drops == 1
    release.set()
    writer.stop()
    assert writer.accepted_counts["system/events"] == 1
    assert writer.persisted_counts["system/events"] == 1


def test_writer_stop_waits_for_inflight_submit_before_sentinel(tmp_path) -> None:
    class PausingQueue(queue.Queue):
        def __init__(self) -> None:
            super().__init__(maxsize=10)
            self.submit_entered = threading.Event()
            self.release_submit = threading.Event()
            self.pause_next_submit = True

        def put_nowait(self, item: object) -> None:
            if self.pause_next_submit:
                self.pause_next_submit = False
                self.submit_entered.set()
                self.release_submit.wait(2)
            super().put_nowait(item)

    writer = SegmentedMcapWriter(
        tmp_path,
        queue_size=10,
        chunk_size_bytes=1024,
        segment_duration_s=60,
        segment_size_bytes=1024**3,
        fsync_interval_s=10,
    )
    paused_queue = PausingQueue()
    writer.queue = paused_queue
    writer.start()
    event = SystemEvent(
        "info",
        "race",
        "停止竞态测试",
        HostClockMapper().stamp(),
    )
    submit_errors: list[Exception] = []
    stop_errors: list[Exception] = []
    stopped = threading.Event()

    def submit() -> None:
        try:
            writer.submit_sample(event)
        except Exception as exc:
            submit_errors.append(exc)

    def stop() -> None:
        try:
            writer.stop()
        except Exception as exc:
            stop_errors.append(exc)
        finally:
            stopped.set()

    submit_thread = threading.Thread(target=submit)
    stop_thread = threading.Thread(target=stop)
    submit_thread.start()
    assert paused_queue.submit_entered.wait(1)
    stop_thread.start()
    assert not stopped.wait(0.05)
    paused_queue.release_submit.set()
    submit_thread.join(timeout=1)
    stop_thread.join(timeout=2)

    assert not submit_thread.is_alive()
    assert not stop_thread.is_alive()
    assert submit_errors == []
    assert stop_errors == []
    assert writer.accepted_counts["system/events"] == 1
    assert writer.persisted_counts["system/events"] == 1
    assert writer.queue_depth == 0
    assert writer.stop() == writer.segments
    with pytest.raises(RuntimeError, match="未运行"):
        writer.submit_sample(event)


def test_writer_reports_real_file_throughput(tmp_path) -> None:
    writer = SegmentedMcapWriter(
        tmp_path,
        queue_size=100,
        chunk_size_bytes=64,
        segment_duration_s=60,
        segment_size_bytes=1024**3,
        fsync_interval_s=0.01,
    )
    mapper = HostClockMapper()
    writer.start()
    for index in range(40):
        writer.submit_sample(
            SystemEvent(
                "info",
                f"throughput-{index}",
                "吞吐统计测试" * 20,
                mapper.stamp(),
            )
        )
        time.sleep(0.003)
    deadline = time.monotonic() + 2
    while writer.queue_depth and time.monotonic() < deadline:
        time.sleep(0.01)

    assert writer.bytes_per_second > 0
    writer.stop()
