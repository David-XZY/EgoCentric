from __future__ import annotations

import math

import pytest

from egocentric_capture.protocol import (
    ACCEL_SCALE,
    FRAME_SIZE,
    GYRO_SCALE,
    EmgFrame,
    FrameParser,
    ImuFrame,
    ProtocolError,
    decode_frame,
)


def test_decode_emg_signed_24_bit_channels() -> None:
    values = (-8_388_608, -1024, -1, 0, 1, 1024, 500_000, 8_388_607)
    payload = b"".join(value.to_bytes(3, "big", signed=True) for value in values)
    frame = decode_frame(b"\xD2\xD2\xD2\xAA\xFE" + payload)
    assert isinstance(frame, EmgFrame)
    assert frame.sequence == 0xFE
    assert frame.channels == values


def test_decode_imu_keeps_raw_and_physical_values() -> None:
    values = (100, -200, 300, 400, -500, 600)
    payload = b"\x00\x00" + b"".join(
        value.to_bytes(2, "big", signed=True) for value in values
    )
    payload += bytes(24 - len(payload))
    frame = decode_frame(b"\xD2\xD2\xD2\xBB\x07" + payload)
    assert isinstance(frame, ImuFrame)
    assert frame.gyro_raw == values[:3]
    assert frame.accel_raw == values[3:]
    assert frame.gyro[1] == pytest.approx(values[1] * GYRO_SCALE)
    assert frame.accel[2] == pytest.approx(values[5] * ACCEL_SCALE)
    assert math.isfinite(frame.gyro[0])


def test_parser_recovers_split_frames_and_absolute_offsets() -> None:
    payload = b"".join(value.to_bytes(3, "big", signed=True) for value in range(8))
    frame = b"\xD2\xD2\xD2\xAA\x01" + payload
    parser = FrameParser()
    assert parser.feed(b"\x00\x01" + frame[:8]) == []
    parsed = parser.feed(frame[8:] + frame)
    assert [item.end_offset for item in parsed] == [2 + FRAME_SIZE, 2 + 2 * FRAME_SIZE]
    assert parser.discarded_bytes == 2


def test_decode_rejects_unknown_kind() -> None:
    with pytest.raises(ProtocolError):
        decode_frame(b"\xD2\xD2\xD2\xCC\x00" + bytes(24))

