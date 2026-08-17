from __future__ import annotations

import queue
import threading

from egocentric_capture.models import CameraFrame, ClockStamp
from egocentric_capture.preview.gstreamer import (
    HARDWARE_DECODER_CANDIDATES,
    SOFTWARE_DECODER,
    _GstreamerBackend,
    _PreviewCounters,
    cockpit_layout,
    configure_appsrc_queue,
    configure_latest_frame_queue,
    configure_preview_sink,
    decoder_candidates,
    native_video_caps,
)


class _FakeElement:
    def __init__(self) -> None:
        self.properties: dict[str, object] = {}

    def set_property(self, name: str, value: object) -> None:
        self.properties[name] = value


def test_decoder_auto_prefers_hardware_and_falls_back_to_software() -> None:
    assert decoder_candidates("auto") == (
        *HARDWARE_DECODER_CANDIDATES,
        SOFTWARE_DECODER,
    )


def test_decoder_can_force_software_or_explicit_factory() -> None:
    assert decoder_candidates("software") == (SOFTWARE_DECODER,)
    assert decoder_candidates("AVDEC_H264") == (SOFTWARE_DECODER,)
    assert decoder_candidates("customh264dec") == ("customh264dec",)


def test_latest_frame_queue_discards_old_decoded_frames() -> None:
    element = _FakeElement()

    configure_latest_frame_queue(element, True)

    assert element.properties == {
        "leaky": 2,
        "max-size-buffers": 1,
        "max-size-bytes": 0,
        "max-size-time": 0,
    }


def test_appsrc_queue_is_bounded_and_keeps_latest_encoded_data() -> None:
    element = _FakeElement()

    configure_appsrc_queue(element, True, 8)

    assert element.properties == {
        "block": False,
        "max-buffers": 8,
        "max-bytes": 0,
        "max-time": 0,
        "leaky-type": 2,
    }


def test_latest_frame_sink_does_not_serialize_four_streams_with_throttle() -> None:
    element = _FakeElement()

    configure_preview_sink(element, True, 15)

    assert element.properties == {
        "sync": False,
        "max-lateness": 0,
        "qos": False,
        "enable-last-sample": False,
        "throttle-time": 0,
    }


def test_native_video_caps_keeps_analysis_branch_from_resizing_decoder() -> None:
    assert native_video_caps(1920, 1080, 30) == (
        "video/x-raw,width=1920,height=1080,framerate=30/1"
    )


def test_cockpit_layout_uses_main_camera_and_three_centered_thumbnails() -> None:
    layout = cockpit_layout(1280, 720, "cam_a")

    assert layout["cam_a"] == {
        "alpha": 1.0,
        "x": 0,
        "y": 0,
        "width": 1280,
        "height": 720,
        "z": 0,
    }
    thumbnails = [layout[name] for name in ("cam_b", "cam_c", "cam_d")]
    assert all(item["width"] == thumbnails[0]["width"] for item in thumbnails)
    assert all(item["height"] == thumbnails[0]["height"] for item in thumbnails)
    assert [item["z"] for item in thumbnails] == [1, 2, 3]
    assert thumbnails[0]["x"] < thumbnails[1]["x"] < thumbnails[2]["x"]


def test_preview_waits_for_each_camera_keyframe_before_queueing() -> None:
    backend = object.__new__(_GstreamerBackend)
    backend.branches = {"cam_a": object()}
    backend._stop_event = threading.Event()
    backend._draining_event = threading.Event()
    backend._awaiting_keyframe = {"cam_a": True}
    backend.failure_policy = "warn_continue"
    backend._queues = {"cam_a": queue.Queue(maxsize=8)}
    backend._counter_lock = threading.Lock()
    backend._counters = {"cam_a": _PreviewCounters()}
    backend.health_callback = lambda _health: None
    stamp = ClockStamp(monotonic_ns=1, unix_ns=1)
    predicted = CameraFrame(
        camera="cam_a",
        socket="CAM_A",
        sequence=1,
        frame_type="P",
        width=1920,
        height=1080,
        codec="H264_MAIN",
        payload=b"predicted",
        stamp=stamp,
    )
    keyframe = CameraFrame(
        camera="cam_a",
        socket="CAM_A",
        sequence=2,
        frame_type="I",
        width=1920,
        height=1080,
        codec="H264_MAIN",
        payload=b"keyframe",
        stamp=stamp,
    )

    backend.submit(predicted)
    assert backend._queues["cam_a"].empty()
    backend.submit(keyframe)
    assert backend._queues["cam_a"].get_nowait() is keyframe


def test_preview_uses_independent_source_origins_for_mixer_alignment() -> None:
    backend = object.__new__(_GstreamerBackend)
    backend.CAMERAS = ("cam_a", "cam_b")
    backend._source_pts_origins_ns = {"cam_a": None, "cam_b": None}
    backend._pipeline_pts_origin_ns = None
    backend.preview_jitter_ms = 80
    backend._counter_lock = threading.Lock()
    backend._counters = {
        "cam_a": _PreviewCounters(),
        "cam_b": _PreviewCounters(),
    }

    cam_b_first = backend._next_preview_pts(
        "cam_b",
        1_000_000_000,
        20_000_000,
    )
    cam_a_first = backend._next_preview_pts(
        "cam_a",
        1_280_000_000,
        300_000_000,
    )
    cam_b_next = backend._next_preview_pts(
        "cam_b",
        1_033_000_000,
        53_000_000,
    )
    cam_a_next = backend._next_preview_pts(
        "cam_a",
        1_313_000_000,
        333_000_000,
    )

    assert cam_a_first == cam_b_first
    assert cam_a_next - cam_a_first == 33_000_000
    assert cam_b_next - cam_b_first == 33_000_000
