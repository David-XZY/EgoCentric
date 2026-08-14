from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

from .depthai_runtime import normalize_encoder_codec


def import_av() -> Any:
    try:
        import av
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "缺少 PyAV 依赖，无法解码 H.264/MJPEG。请更新环境: "
            "conda env update -n oak-depthai -f envs/oak-depthai.yml"
        ) from exc
    return av


def encoded_data_to_bytes(data: Any) -> bytes:
    tobytes = getattr(data, "tobytes", None)
    if callable(tobytes):
        return bytes(tobytes())
    return bytes(data)


def encoded_message_bytes(message: Any) -> bytes:
    get_data = getattr(message, "getData", None)
    if not callable(get_data):
        raise RuntimeError("编码帧对象不支持 getData")
    return encoded_data_to_bytes(get_data())


def pyav_codec_name(codec: str) -> str:
    profile = normalize_encoder_codec(codec)
    return {
        "H264_MAIN": "h264",
        "H265_MAIN": "hevc",
        "MJPEG": "mjpeg",
    }[profile]


class EncodedFrameDecoder:
    def __init__(self, codec: str, av_module: Any | None = None) -> None:
        self.codec = normalize_encoder_codec(codec)
        self._av = av_module or import_av()
        self._context = self._av.CodecContext.create(pyav_codec_name(self.codec), "r")
        self.packet_count = 0
        self.decoded_frame_count = 0
        self.decode_error_count = 0
        self.last_error: str | None = None
        self.last_frame: Any | None = None

    def decode(self, payload: bytes) -> list[Any]:
        if not payload:
            return []
        frames: list[Any] = []
        try:
            packets = self._context.parse(payload)
        except Exception as exc:
            self.decode_error_count += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []
        for packet in packets:
            self.packet_count += 1
            try:
                decoded = self._context.decode(packet)
            except Exception as exc:
                self.decode_error_count += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                continue
            for frame in decoded:
                image = frame.to_ndarray(format="bgr24")
                frames.append(image)
                self.last_frame = image
                self.decoded_frame_count += 1
        return frames

    def decode_message(self, message: Any) -> list[Any]:
        return self.decode(encoded_message_bytes(message))

    def decode_latest(self, payload: bytes) -> Any | None:
        frames = self.decode(payload)
        return frames[-1] if frames else None

    def decode_message_latest(self, message: Any) -> Any | None:
        frames = self.decode_message(message)
        return frames[-1] if frames else None

    def flush(self) -> list[Any]:
        frames: list[Any] = []
        for frame in self._context.decode(None):
            image = frame.to_ndarray(format="bgr24")
            frames.append(image)
            self.last_frame = image
            self.decoded_frame_count += 1
        return frames

    def state(self) -> dict[str, Any]:
        return {
            "codec": self.codec,
            "packet_count": self.packet_count,
            "decoded_frame_count": self.decoded_frame_count,
            "decode_error_count": self.decode_error_count,
            "last_error": self.last_error,
            "has_frame": self.last_frame is not None,
        }


def decode_message_into_latest(
    decoder: EncodedFrameDecoder,
    message: Any,
    latest_frames: dict[str, Any],
    stream_name: str,
) -> bool:
    frame = decoder.decode_message_latest(message)
    if frame is None:
        return False
    latest_frames[stream_name] = frame
    return True


class EncodedFileReader:
    def __init__(self, path: Path, codec: str) -> None:
        self.path = Path(path)
        self.codec = normalize_encoder_codec(codec)
        av = import_av()
        self._container = av.open(
            str(self.path), mode="r", format=pyav_codec_name(self.codec)
        )
        self._frames: Iterator[Any] = iter(self._container.decode(video=0))
        self.next_index = 0
        self.last_frame: Any | None = None

    def read_until(self, index: int) -> Any | None:
        while self.next_index <= index:
            try:
                frame = next(self._frames)
            except StopIteration:
                return self.last_frame
            self.last_frame = frame.to_ndarray(format="bgr24")
            self.next_index += 1
        return self.last_frame

    def close(self) -> None:
        self._container.close()
