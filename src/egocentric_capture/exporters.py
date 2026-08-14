from __future__ import annotations

import tempfile
from collections import defaultdict
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable

import av
import numpy as np
from mcap_protobuf.reader import read_protobuf_messages

from .storage import read_json


def export_session(
    session_dir: Path,
    output_dir: Path,
    format_name: str,
) -> list[Path]:
    """把权威 MCAP 数据导出为常用后处理格式。"""
    session_dir = Path(session_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if format_name == "mp4":
        return export_mp4(session_dir, output_dir)
    if format_name == "numpy":
        return export_numpy(session_dir, output_dir)
    if format_name == "parquet":
        return export_parquet(session_dir, output_dir)
    raise ValueError(f"不支持的导出格式: {format_name}")


def export_mp4(session_dir: Path, output_dir: Path) -> list[Path]:
    manifest = read_json(session_dir / "session.json")
    minimum_fps = float(
        (manifest.get("config") or {}).get("oak", {}).get("minimum_fps", 24)
    )
    quality_rates = (manifest.get("quality") or {}).get("rates_hz") or {}
    outputs: list[Path] = []
    for camera in ("cam_a", "cam_b", "cam_c", "cam_d"):
        topic = f"/camera/{camera}/video"
        fps = float(quality_rates.get(f"/camera/{camera}/timing", minimum_fps))
        output = output_dir / f"{camera}.mp4"
        with tempfile.NamedTemporaryFile(suffix=".h264") as raw:
            frame_count = 0
            for item in _messages(session_dir, {topic}):
                raw.write(bytes(item.proto_msg.data))
                frame_count += 1
            raw.flush()
            if frame_count == 0:
                raise RuntimeError(f"主题 {topic} 没有视频帧")
            _remux_h264(Path(raw.name), output, fps)
        outputs.append(output)
    return outputs


def export_numpy(session_dir: Path, output_dir: Path) -> list[Path]:
    rows = _collect_sensor_rows(session_dir)
    output = output_dir / "sensors.npz"
    np.savez_compressed(
        output,
        emg_time_ns=np.asarray(rows["emg_time_ns"], dtype=np.int64),
        emg_sequence=np.asarray(rows["emg_sequence"], dtype=np.uint8),
        emg_uv=np.asarray(rows["emg_uv"], dtype=np.int32).reshape(-1, 8),
        wearable_imu_time_ns=np.asarray(
            rows["wearable_imu_time_ns"], dtype=np.int64
        ),
        wearable_imu_sequence=np.asarray(
            rows["wearable_imu_sequence"], dtype=np.uint8
        ),
        wearable_gyro_raw=np.asarray(
            rows["wearable_gyro_raw"], dtype=np.int32
        ).reshape(-1, 3),
        wearable_accel_raw=np.asarray(
            rows["wearable_accel_raw"], dtype=np.int32
        ).reshape(-1, 3),
        wearable_gyro_rad_s=np.asarray(
            rows["wearable_gyro_rad_s"], dtype=np.float64
        ).reshape(-1, 3),
        wearable_accel_m_s2=np.asarray(
            rows["wearable_accel_m_s2"], dtype=np.float64
        ).reshape(-1, 3),
        oak_imu_time_ns=np.asarray(rows["oak_imu_time_ns"], dtype=np.int64),
        oak_accel_m_s2=np.asarray(
            rows["oak_accel_m_s2"], dtype=np.float64
        ).reshape(-1, 3),
        oak_gyro_rad_s=np.asarray(
            rows["oak_gyro_rad_s"], dtype=np.float64
        ).reshape(-1, 3),
        oak_magnetic_ut=np.asarray(
            rows["oak_magnetic_ut"], dtype=np.float64
        ).reshape(-1, 3),
        oak_quaternion_xyzw=np.asarray(
            rows["oak_quaternion_xyzw"], dtype=np.float64
        ).reshape(-1, 4),
    )
    return [output]


def export_parquet(session_dir: Path, output_dir: Path) -> list[Path]:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError(
            "Parquet 导出需要安装可选依赖 pyarrow"
        ) from exc

    rows = _collect_sensor_rows(session_dir)
    tables = {
        "emg.parquet": {
            "time_ns": rows["emg_time_ns"],
            "sequence": rows["emg_sequence"],
            **{
                f"channel_{index + 1}_uv": [
                    value[index] for value in rows["emg_uv"]
                ]
                for index in range(8)
            },
        },
        "wearable_imu.parquet": {
            "time_ns": rows["wearable_imu_time_ns"],
            "sequence": rows["wearable_imu_sequence"],
            **_vector_columns("gyro_raw", rows["wearable_gyro_raw"], "xyz"),
            **_vector_columns("accel_raw", rows["wearable_accel_raw"], "xyz"),
            **_vector_columns(
                "gyro_rad_s", rows["wearable_gyro_rad_s"], "xyz"
            ),
            **_vector_columns(
                "accel_m_s2", rows["wearable_accel_m_s2"], "xyz"
            ),
        },
        "oak_imu.parquet": {
            "time_ns": rows["oak_imu_time_ns"],
            **_vector_columns("accel_m_s2", rows["oak_accel_m_s2"], "xyz"),
            **_vector_columns("gyro_rad_s", rows["oak_gyro_rad_s"], "xyz"),
            **_vector_columns("magnetic_ut", rows["oak_magnetic_ut"], "xyz"),
            **_vector_columns(
                "quaternion", rows["oak_quaternion_xyzw"], "xyzw"
            ),
        },
    }
    outputs: list[Path] = []
    for name, columns in tables.items():
        output = output_dir / name
        pq.write_table(pa.table(columns), output)
        outputs.append(output)
    return outputs


def _collect_sensor_rows(session_dir: Path) -> dict[str, list[Any]]:
    rows: dict[str, list[Any]] = defaultdict(list)
    topics = {"/wearable/emg", "/wearable/imu", "/imu/oak"}
    for item in _messages(session_dir, topics):
        message = item.proto_msg
        if item.topic == "/wearable/emg":
            rows["emg_time_ns"].append(item.log_time_ns)
            rows["emg_sequence"].append(message.sequence)
            rows["emg_uv"].append(tuple(message.channels_uv))
        elif item.topic == "/wearable/imu":
            rows["wearable_imu_time_ns"].append(item.log_time_ns)
            rows["wearable_imu_sequence"].append(message.sequence)
            rows["wearable_gyro_raw"].append(tuple(message.gyro_raw))
            rows["wearable_accel_raw"].append(tuple(message.accel_raw))
            rows["wearable_gyro_rad_s"].append(tuple(message.gyro_rad_s))
            rows["wearable_accel_m_s2"].append(tuple(message.accel_m_s2))
        elif item.topic == "/imu/oak":
            rows["oak_imu_time_ns"].append(item.log_time_ns)
            rows["oak_accel_m_s2"].append(_fixed_vector(message.accel_m_s2, 3))
            rows["oak_gyro_rad_s"].append(_fixed_vector(message.gyro_rad_s, 3))
            rows["oak_magnetic_ut"].append(_fixed_vector(message.magnetic_ut, 3))
            rows["oak_quaternion_xyzw"].append(
                _fixed_vector(message.quaternion_xyzw, 4)
            )
    return rows


def _messages(session_dir: Path, topics: set[str]) -> Iterable[Any]:
    for segment in sorted((session_dir / "segments").glob("*.mcap")):
        yield from read_protobuf_messages(
            segment,
            topics=topics,
            log_time_order=False,
        )


def _fixed_vector(values: Any, length: int) -> tuple[float, ...]:
    output = tuple(float(value) for value in values)
    if len(output) == length:
        return output
    return tuple(float("nan") for _ in range(length))


def _vector_columns(
    prefix: str,
    values: list[tuple[Any, ...]],
    axes: str,
) -> dict[str, list[Any]]:
    return {
        f"{prefix}_{axis}": [value[index] for value in values]
        for index, axis in enumerate(axes)
    }


def _remux_h264(source: Path, destination: Path, fps: float) -> None:
    destination.unlink(missing_ok=True)
    output_rate = Fraction(str(max(1.0, fps))).limit_denominator(1_000)
    frame_time_base = Fraction(output_rate.denominator, output_rate.numerator)
    with av.open(
        str(source),
        mode="r",
        format="h264",
        options={"framerate": f"{float(output_rate):.6f}"},
    ) as input_container:
        input_stream = input_container.streams.video[0]
        with av.open(str(destination), mode="w", format="mp4") as output_container:
            output_stream = output_container.add_stream(
                "libx264",
                rate=output_rate,
            )
            output_stream.width = input_stream.codec_context.width
            output_stream.height = input_stream.codec_context.height
            output_stream.pix_fmt = "yuv420p"
            output_stream.options = {"preset": "fast", "crf": "18"}
            for index, frame in enumerate(input_container.decode(input_stream)):
                frame.pts = index
                frame.time_base = frame_time_base
                for packet in output_stream.encode(frame):
                    output_container.mux(packet)
            for packet in output_stream.encode():
                output_container.mux(packet)
    if not destination.exists() or destination.stat().st_size == 0:
        raise RuntimeError(f"MP4 导出失败: {destination}")
