from __future__ import annotations

from egocentric_capture.preview.gstreamer import (
    HARDWARE_DECODER_CANDIDATES,
    SOFTWARE_DECODER,
    cockpit_layout,
    configure_appsrc_queue,
    configure_latest_frame_queue,
    configure_preview_sink,
    decoder_candidates,
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
