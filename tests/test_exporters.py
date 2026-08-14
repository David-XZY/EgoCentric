from __future__ import annotations

import av
import numpy as np

from egocentric_capture.exporters import _remux_h264
from egocentric_capture.sources.simulated import _simulation_image, _VideoEncoder


def test_h264_export_produces_decodable_mp4(tmp_path) -> None:
    encoder = _VideoEncoder(320, 180, 25)
    raw = tmp_path / "input.h264"
    with raw.open("wb") as file:
        for index in range(40):
            payload, _ = encoder.encode(
                _simulation_image(320, 180, "cam_a", index, 0),
                index,
            )
            file.write(payload)
    output = tmp_path / "output.mp4"
    _remux_h264(raw, output, 25)
    with av.open(output) as container:
        duration_s = float(container.duration or 0) / 1_000_000
        frames = list(container.decode(video=0))
    assert len(frames) >= 30
    assert 1.4 <= duration_s <= 1.8
    assert np.asarray(frames[0].to_ndarray(format="bgr24")).shape == (180, 320, 3)
