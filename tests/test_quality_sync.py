from __future__ import annotations

from egocentric_capture.quality import _camera_sync


def test_camera_sync_consumes_each_frame_once() -> None:
    timestamps = {
        "cam_a": [0, 1_000_000],
        "cam_b": [0],
        "cam_c": [0],
        "cam_d": [0],
    }
    report = _camera_sync(timestamps, max_window_ms=5)
    assert report["complete_groups"] == 1
    assert report["missing_groups"] == 1
    assert report["unmatched_frames"] == 1
    assert report["unmatched_by_camera"]["cam_a"] == 1
