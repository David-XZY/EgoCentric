from __future__ import annotations

import threading
import time

import egocentric_capture.session as session_module
from egocentric_capture.config import load_config
from egocentric_capture.models import (
    CameraFrame,
    CaptureRequest,
    CaptureState,
    HealthSnapshot,
    PreviewHealth,
)
from egocentric_capture.quality import _sequence_gaps
from egocentric_capture.session import (
    CaptureEngine,
    EngineCallbacks,
    _SequenceState,
)
from egocentric_capture.storage import read_json, verify_checksums


def test_sequence_wrap_and_gap_detection() -> None:
    tracker = _SequenceState()
    assert tracker.push(254, 256) == 0
    assert tracker.push(255, 256) == 0
    assert tracker.push(0, 256) == 0
    assert tracker.push(2, 256) == 1
    assert tracker.gaps == 1


def test_wearable_sequence_uses_record_order_not_estimated_time() -> None:
    # 串口时间回推可能让相邻帧时间戳交换，但协议序号仍按写入顺序连续。
    write_order = [62, 63, 64, 65, 66, 67, 68, 69, 70, 71]
    estimated_time_order = [62, 63, 64, 65, 66, 67, 68, 70, 69, 71]
    assert _sequence_gaps(write_order, modulo=256) == 0
    assert _sequence_gaps(estimated_time_order, modulo=256) > 0


def test_simulated_capture_completes_with_canonical_artifacts(tmp_path) -> None:
    config = load_config()
    config["workspace"]["output_root"] = str(tmp_path)
    config["workspace"]["minimum_start_free_gib"] = 0
    config["workspace"]["emergency_stop_free_gib"] = 0
    config["session"]["healthy_before_ready_s"] = 0.4
    engine = CaptureEngine(config, simulate=True)
    engine.start()
    try:
        _wait_state(engine, {CaptureState.READY}, 8)
        session = engine.request_record(
            CaptureRequest(
                "P001",
                "grasp",
                "right",
                "tester",
                duration_s=2.2,
            )
        )
        _wait_state(engine, {CaptureState.COMPLETED, CaptureState.FAILED}, 15)
        manifest = read_json(session / "session.json")
        assert engine.state == CaptureState.COMPLETED, manifest.get("failure_reason")
        assert manifest["status"] == "completed"
        assert manifest["schema_version"] == 2
        assert manifest["quality"]["pass"] is True
        assert manifest["quality"]["queue_drops"] == 0
        assert manifest["quality"]["sequence_gaps"]["wearable"] == 0
        assert all(
            segment["bytes_read"] <= segment["size_bytes"] * 1.1
            for segment in manifest["quality"]["segments"]
        )
        assert (session / "logs" / "app.log").is_file()
        assert (session / "checksums.sha256").is_file()
        assert verify_checksums(session) == []
        counts = manifest["counts"]
        assert counts["received"] == counts["writer_accepted"]
        assert counts["received"] == counts["persisted"]
        engine.prepare_next()
        _wait_state(engine, {CaptureState.READY}, 5)
    finally:
        engine.stop()


def test_preview_receives_same_encoded_frame_after_writer_accepts(
    tmp_path,
) -> None:
    config = load_config()
    config["workspace"]["output_root"] = str(tmp_path)
    config["workspace"]["minimum_start_free_gib"] = 0
    received: list[CameraFrame] = []
    engine = CaptureEngine(
        config,
        simulate=True,
        callbacks=EngineCallbacks(on_preview=received.append),
    )
    engine.state = CaptureState.READY
    engine.request_record(
        CaptureRequest("P002", "identity", "right", "tester")
    )
    frame = CameraFrame(
        camera="cam_a",
        socket="CAM_A",
        sequence=7,
        frame_type="I",
        width=1920,
        height=1080,
        codec="H264_MAIN",
        payload=(
            b"\x00\x00\x00\x01\x67\x64\x00\x28"
            b"\x00\x00\x00\x01\x68\xee\x3c\x80"
            b"\x00\x00\x00\x01\x65\x88\x84"
        ),
        stamp=engine.clock.stamp(),
    )
    engine.ingest(frame)
    assert received == [frame]
    assert received[0] is frame
    assert engine._writer is not None
    assert engine._writer.accepted_counts["camera/cam_a"] == 1
    engine.request_stop()
    _wait_state(engine, {CaptureState.FAILED}, 5)
    engine.stop()


def test_event_freeze_waits_for_inflight_log_write(tmp_path) -> None:
    config = load_config()
    config["workspace"]["output_root"] = str(tmp_path)
    engine = CaptureEngine(config, simulate=True)
    session_dir = tmp_path / "session"
    (session_dir / "logs").mkdir(parents=True)
    submit_entered = threading.Event()
    release_submit = threading.Event()
    freeze_finished = threading.Event()

    class Writer:
        def submit_sample(self, _sample: object) -> None:
            submit_entered.set()
            release_submit.wait(2)

    engine._writer = Writer()
    engine._session_dir = session_dir
    engine._event_accepting = True
    event_thread = threading.Thread(
        target=engine._write_event,
        args=("warning", "preview_failure", "预览异常"),
    )

    def freeze() -> None:
        engine._freeze_event_writes()
        freeze_finished.set()

    freeze_thread = threading.Thread(target=freeze)
    event_thread.start()
    assert submit_entered.wait(1)
    freeze_thread.start()
    assert not freeze_finished.wait(0.05)
    release_submit.set()
    event_thread.join(timeout=1)
    freeze_thread.join(timeout=1)

    assert not event_thread.is_alive()
    assert not freeze_thread.is_alive()
    assert freeze_finished.is_set()
    log_path = session_dir / "logs" / "app.log"
    original = log_path.read_text(encoding="utf-8")
    engine._write_event("warning", "ignored", "不应写入")
    assert log_path.read_text(encoding="utf-8") == original


def test_health_uses_arrival_time_for_liveness(tmp_path) -> None:
    config = load_config()
    config["workspace"]["output_root"] = str(tmp_path)
    config["workspace"]["minimum_start_free_gib"] = 0
    engine = CaptureEngine(config, simulate=True)
    capture_ns = time.monotonic_ns() - 2_000_000_000
    arrival_ns = time.monotonic_ns()
    frame = CameraFrame(
        camera="cam_a",
        socket="CAM_A",
        sequence=1,
        frame_type="I",
        width=1920,
        height=1080,
        codec="H264_MAIN",
        payload=b"frame",
        stamp=engine.clock.stamp(
            capture_ns,
            arrival_monotonic_ns=arrival_ns,
        ),
    )

    engine.ingest(frame)
    snapshot = engine.health_snapshot()

    assert snapshot.last_seen_age_s["camera/cam_a"] < 0.2
    assert engine._last_source_seen["camera/cam_a"] == capture_ns
    assert engine._last_arrival_seen["camera/cam_a"] == arrival_ns


def test_unhealthy_preview_does_not_block_device_readiness(tmp_path) -> None:
    config = load_config()
    config["workspace"]["output_root"] = str(tmp_path)
    config["workspace"]["minimum_start_free_gib"] = 0
    config["wearable"]["imu_warmup_s"] = 0
    engine = CaptureEngine(config, simulate=True)
    now = time.monotonic_ns()
    engine._first_seen["wearable/imu"] = now
    engine._preview_health["cam_a"] = PreviewHealth(
        camera="cam_a",
        submitted_count=100,
        rendered_count=90,
        last_submitted_sequence=100,
        last_rendered_sequence=90,
        latency_ms=1000,
        healthy=False,
        message="预览延迟过高",
    )
    rates = {
        "camera/cam_a": 25.0,
        "camera/cam_b": 25.0,
        "camera/cam_c": 25.0,
        "camera/cam_d": 25.0,
        "imu/oak": 100.0,
        "wearable/emg": 250.0,
        "wearable/imu": 50.0,
    }
    snapshot = HealthSnapshot(
        stamp=engine.clock.stamp(now),
        rates_hz=rates,
        last_seen_age_s={key: 0.01 for key in rates},
        sequence_gaps={},
        queue_depth=0,
        queue_drops=0,
        disk_free_bytes=2**60,
        ready=False,
        preview_latency_ms={"cam_a": 1000},
    )

    assert engine._health_is_ready(snapshot) is True


def test_wearable_starts_after_oak_pipeline_is_ready(
    tmp_path,
    monkeypatch,
) -> None:
    instances: dict[str, object] = {}

    class FakeOakSource:
        def __init__(self, *_args, **_kwargs) -> None:
            self.running = False
            self.ready = threading.Event()
            instances["oak"] = self

        def start(self) -> None:
            self.running = True

        def wait_until_ready(self, timeout_s: float) -> bool:
            return self.ready.wait(timeout_s)

        def stop(self) -> None:
            self.running = False
            self.ready.set()

    class FakeWearableSource:
        def __init__(self, *_args, **_kwargs) -> None:
            self.running = False
            self.start_count = 0
            instances["wearable"] = self

        def start(self) -> None:
            self.running = True
            self.start_count += 1

        def stop(self) -> None:
            self.running = False

    monkeypatch.setattr(session_module, "OakSource", FakeOakSource)
    monkeypatch.setattr(session_module, "WearableSource", FakeWearableSource)
    config = load_config()
    config["workspace"]["output_root"] = str(tmp_path)
    engine = CaptureEngine(config)
    engine.start()
    try:
        oak = instances["oak"]
        wearable = instances["wearable"]
        assert oak.running is True
        assert wearable.running is False
        oak.ready.set()
        deadline = time.monotonic() + 1
        while not wearable.running and time.monotonic() < deadline:
            time.sleep(0.01)
        assert wearable.running is True
        assert wearable.start_count == 1
    finally:
        engine.stop()


def test_wearable_rate_uses_fixed_arrival_window(
    tmp_path,
    monkeypatch,
) -> None:
    config = load_config()
    config["workspace"]["output_root"] = str(tmp_path)
    engine = CaptureEngine(config, simulate=True)
    now = 10_000_000_000
    key = "wearable/emg"
    engine._first_arrival_seen[key] = now - 5_000_000_000
    engine._arrival_rate_windows[key].extend(
        now - 5_000_000_000 + index * 4_000_000
        for index in range(1250)
    )
    engine._rate_windows[key].extend(
        now - 1_000_000_000 + index * 800_000
        for index in range(1250)
    )
    monkeypatch.setattr(session_module.time, "monotonic_ns", lambda: now)

    assert engine._rate(key) == 250.0


def _wait_state(
    engine: CaptureEngine,
    expected: set[CaptureState],
    timeout_s: float,
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if engine.state in expected:
            return
        time.sleep(0.05)
    raise AssertionError(f"状态等待超时: {engine.state} {engine.state_message}")
