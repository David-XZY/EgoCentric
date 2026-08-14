from __future__ import annotations

from dataclasses import dataclass
from math import pi
from typing import TypeAlias

HEADER = b"\xD2\xD2\xD2"
FRAME_SIZE = 29
PAYLOAD_SIZE = 24
EMG_KIND = 0xAA
IMU_KIND = 0xBB
GYRO_SCALE = 0.00012 * pi
ACCEL_SCALE = 0.0005978


class ProtocolError(ValueError):
    """手环数据不符合固定帧协议。"""


@dataclass(frozen=True, slots=True)
class EmgFrame:
    sequence: int
    channels: tuple[int, int, int, int, int, int, int, int]


@dataclass(frozen=True, slots=True)
class ImuFrame:
    sequence: int
    gyro_raw: tuple[int, int, int]
    accel_raw: tuple[int, int, int]
    gyro: tuple[float, float, float]
    accel: tuple[float, float, float]


DecodedFrame: TypeAlias = EmgFrame | ImuFrame


def decode_frame(data: bytes | bytearray | memoryview) -> DecodedFrame:
    frame = bytes(data)
    if len(frame) != FRAME_SIZE:
        raise ProtocolError(f"帧长度应为 {FRAME_SIZE} 字节，实际为 {len(frame)} 字节")
    if frame[:3] != HEADER:
        raise ProtocolError("包头不是 D2 D2 D2")
    sequence = frame[4]
    payload = frame[5:]
    if frame[3] == EMG_KIND:
        channels = tuple(
            int.from_bytes(payload[offset : offset + 3], "big", signed=True)
            for offset in range(0, PAYLOAD_SIZE, 3)
        )
        return EmgFrame(sequence, channels)  # type: ignore[arg-type]
    if frame[3] == IMU_KIND:
        values = tuple(
            int.from_bytes(payload[offset : offset + 2], "big", signed=True)
            for offset in range(2, 14, 2)
        )
        gyro_raw = values[:3]
        accel_raw = values[3:]
        return ImuFrame(
            sequence,
            gyro_raw,  # type: ignore[arg-type]
            accel_raw,  # type: ignore[arg-type]
            tuple(value * GYRO_SCALE for value in gyro_raw),  # type: ignore[arg-type]
            tuple(value * ACCEL_SCALE for value in accel_raw),  # type: ignore[arg-type]
        )
    raise ProtocolError(f"未知的数据包类型 0x{frame[3]:02X}")


@dataclass(frozen=True, slots=True)
class ParsedFrame:
    frame: DecodedFrame
    end_offset: int


class FrameParser:
    """从任意串口分块中恢复完整帧，并保留帧结束位置。"""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._stream_offset = 0
        self.discarded_bytes = 0
        self.invalid_frames = 0

    def reset(self) -> None:
        self._buffer.clear()
        self._stream_offset = 0
        self.discarded_bytes = 0
        self.invalid_frames = 0

    def feed(self, data: bytes | bytearray | memoryview) -> list[ParsedFrame]:
        if data:
            self._buffer.extend(data)
        frames: list[ParsedFrame] = []
        while True:
            header_index = self._buffer.find(HEADER)
            if header_index < 0:
                keep = min(len(self._buffer), len(HEADER) - 1)
                self._discard(len(self._buffer) - keep)
                break
            if header_index:
                self._discard(header_index)
            if len(self._buffer) < FRAME_SIZE:
                break
            if self._buffer[3] not in (EMG_KIND, IMU_KIND):
                self._discard(1)
                self.invalid_frames += 1
                continue
            candidate = bytes(self._buffer[:FRAME_SIZE])
            try:
                decoded = decode_frame(candidate)
            except ProtocolError:
                self._discard(1)
                self.invalid_frames += 1
                continue
            del self._buffer[:FRAME_SIZE]
            self._stream_offset += FRAME_SIZE
            frames.append(ParsedFrame(decoded, self._stream_offset))
        return frames

    def _discard(self, count: int) -> None:
        if count <= 0:
            return
        del self._buffer[:count]
        self._stream_offset += count
        self.discarded_bytes += count
