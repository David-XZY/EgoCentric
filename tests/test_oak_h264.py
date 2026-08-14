from __future__ import annotations

import pytest

from egocentric_capture.sources.oak import _ensure_h264_parameter_sets


class _CacheProbe(dict[int, bytes]):
    def __init__(self) -> None:
        super().__init__({7: b"sps", 8: b"pps"})
        self.write_count = 0

    def __setitem__(self, key: int, value: bytes) -> None:
        self.write_count += 1
        super().__setitem__(key, value)


def test_keyframe_reuses_cached_sps_and_pps() -> None:
    sps = b"\x00\x00\x00\x01\x67\x01"
    pps = b"\x00\x00\x00\x01\x68\x02"
    p_frame = b"\x00\x00\x00\x01\x41\x03"
    idr = b"\x00\x00\x00\x01\x65\x04"
    cache: dict[int, bytes] = {}
    assert _ensure_h264_parameter_sets(sps + pps + p_frame, False, cache)
    output = _ensure_h264_parameter_sets(idr, True, cache)
    assert output == sps + pps + idr


def test_first_keyframe_without_parameter_sets_is_rejected() -> None:
    with pytest.raises(RuntimeError, match="SPS/PPS"):
        _ensure_h264_parameter_sets(
            b"\x00\x00\x00\x01\x65\x04",
            True,
            {},
        )


def test_non_keyframe_skips_annex_b_scan_after_cache_is_ready() -> None:
    cache = _CacheProbe()
    payload = b"\x00\x00\x00\x01\x41" + b"x" * 50_000

    assert _ensure_h264_parameter_sets(payload, False, cache) is payload
    assert cache.write_count == 0
