from __future__ import annotations

import math
import random
import threading
import time
from fractions import Fraction
from typing import Any

import av
import numpy as np

from ..clocks import HostClockMapper
from ..models import (
    CameraFrame,
    OakImuSample,
    TimestampQuality,
    WearableEmgSample,
    WearableImuSample,
    WearableRawChunk,
)
from .base import SourceCallbacks


class _VideoEncoder:
    def __init__(self, width: int, height: int, fps: int) -> None:
        self.width = width
        self.height = height
        self.fps = fps
        self.context = av.CodecContext.create("libx264", "w")
        self.context.width = width
        self.context.height = height
        self.context.pix_fmt = "yuv420p"
        self.context.time_base = Fraction(1, fps)
        self.context.framerate = Fraction(fps, 1)
        self.context.options = {
            "preset": "ultrafast",
            "tune": "zerolatency",
            "g": str(fps),
            "bf": "0",
            "x264-params": "annexb=1:repeat-headers=1",
        }
        self.context.open()

    def encode(self, image: np.ndarray, index: int) -> tuple[bytes, bool]:
        frame = av.VideoFrame.from_ndarray(image, format="bgr24")
        frame.pts = index
        packets = self.context.encode(frame)
        payload = b"".join(bytes(packet) for packet in packets)
        keyframe = any(bool(packet.is_keyframe) for packet in packets)
        return payload, keyframe


class SimulatedSource:
    """在无硬件环境下生成可重复的完整多模态数据。"""

    def __init__(
        self,
        config: dict[str, Any],
        clock: HostClockMapper,
        callbacks: SourceCallbacks,
    ) -> None:
        self.config = config
        self.clock = clock
        self.callbacks = callbacks
        self._camera_thread: threading.Thread | None = None
        self._sensor_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    @property
    def running(self) -> bool:
        return bool(
            self._camera_thread
            and self._camera_thread.is_alive()
            and self._sensor_thread
            and self._sensor_thread.is_alive()
        )

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        if self.callbacks.on_metadata is not None:
            self.callbacks.on_metadata(
                "oak",
                {"simulated": True, "usb_speed": "SUPER", "mxid": "SIMULATED"},
            )
            self.callbacks.on_metadata(
                "wearable",
                {"simulated": True, "port": "SIMULATED", "baudrate": 921600},
            )
        self._camera_thread = threading.Thread(
            target=self._run_cameras,
            name="simulated-cameras",
            daemon=True,
        )
        self._sensor_thread = threading.Thread(
            target=self._run_sensors,
            name="simulated-sensors",
            daemon=True,
        )
        self._camera_thread.start()
        self._sensor_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        for thread in (self._camera_thread, self._sensor_thread):
            if thread is not None:
                thread.join(timeout=5)
        self._camera_thread = None
        self._sensor_thread = None

    def _run_cameras(self) -> None:
        camera_fps = int(self.config.get("camera_fps", 25))
        cameras = ("cam_a", "cam_b", "cam_c", "cam_d")
        width, height = 640, 360
        encoders = {
            camera: _VideoEncoder(width, height, camera_fps) for camera in cameras
        }
        started = time.monotonic()
        next_camera = started
        camera_sequence = 0
        while not self._stop_event.is_set():
            now = time.monotonic()
            if now >= next_camera:
                capture_ns = time.monotonic_ns()
                device_ns = int((now - started) * 1_000_000_000)
                for camera_index, camera in enumerate(cameras):
                    image = _simulation_image(
                        width,
                        height,
                        camera,
                        camera_sequence,
                        camera_index,
                    )
                    payload, keyframe = encoders[camera].encode(
                        image,
                        camera_sequence,
                    )
                    if payload:
                        stamp = self.clock.stamp(
                            capture_ns + camera_index * 20_000,
                            source_device_ns=device_ns + camera_index * 20_000,
                            source_host_ns=capture_ns + camera_index * 20_000,
                            arrival_monotonic_ns=time.monotonic_ns(),
                            quality=TimestampQuality.DEVICE_SYNCED,
                        )
                        self.callbacks.on_sample(
                            CameraFrame(
                                camera=camera,
                                socket=f"CAM_{chr(65 + camera_index)}",
                                sequence=camera_sequence,
                                frame_type="I" if keyframe else "P",
                                width=width,
                                height=height,
                                codec="H264_MAIN",
                                payload=payload,
                                stamp=stamp,
                            )
                        )
                camera_sequence += 1
                next_camera += 1 / camera_fps
            time.sleep(0.0005)

    def _run_sensors(self) -> None:
        randomizer = random.Random(int(self.config.get("seed", 20260806)))
        oak_imu_hz = int(self.config.get("oak_imu_hz", 90))
        emg_hz = int(self.config.get("emg_hz", 250))
        wearable_imu_hz = int(self.config.get("wearable_imu_hz", 50))
        started = time.monotonic()
        next_oak_imu = started
        next_emg = started
        next_wearable_imu = started
        wearable_sequence = 0
        while not self._stop_event.is_set():
            now = time.monotonic()
            if now >= next_oak_imu:
                timestamp = time.monotonic_ns()
                phase = now - started
                self.callbacks.on_sample(
                    OakImuSample(
                        stamp=self.clock.stamp(
                            timestamp,
                            source_device_ns=int(phase * 1_000_000_000),
                            source_host_ns=timestamp,
                            arrival_monotonic_ns=timestamp,
                            quality=TimestampQuality.DEVICE_SYNCED,
                        ),
                        accel=(0.1 * math.sin(phase), 0.0, 9.80665),
                        gyro=(0.0, 0.0, 0.05 * math.cos(phase)),
                        magnetic=(32.0, 4.0, 18.0),
                        quaternion=(0.0, 0.0, 0.0, 1.0),
                    )
                )
                next_oak_imu += 1 / oak_imu_hz
            if now >= next_emg:
                timestamp = time.monotonic_ns()
                values = tuple(
                    int(
                        800 * math.sin((now - started) * (2 + channel * 0.2))
                        + randomizer.randint(-30, 30)
                    )
                    for channel in range(8)
                )
                stamp = self.clock.stamp(
                    timestamp,
                    arrival_monotonic_ns=timestamp,
                    uncertainty_ns=350_000,
                    quality=TimestampQuality.SERIAL_ESTIMATED,
                )
                raw = _emg_bytes(wearable_sequence, values)
                self.callbacks.on_sample(
                    WearableRawChunk(raw, timestamp, timestamp, stamp.unix_ns)
                )
                self.callbacks.on_sample(
                    WearableEmgSample(wearable_sequence, values, stamp)
                )
                wearable_sequence = (wearable_sequence + 1) & 0xFF
                next_emg += 1 / emg_hz
            if now >= next_wearable_imu:
                timestamp = time.monotonic_ns()
                stamp = self.clock.stamp(
                    timestamp,
                    arrival_monotonic_ns=timestamp,
                    uncertainty_ns=350_000,
                    quality=TimestampQuality.SERIAL_ESTIMATED,
                )
                gyro_raw = (10, -5, 2)
                accel_raw = (0, 0, 16400)
                self.callbacks.on_sample(
                    WearableImuSample(
                        wearable_sequence,
                        gyro_raw,
                        accel_raw,
                        tuple(value * 0.00012 * math.pi for value in gyro_raw),
                        tuple(value * 0.0005978 for value in accel_raw),
                        stamp,
                    )
                )
                wearable_sequence = (wearable_sequence + 1) & 0xFF
                next_wearable_imu += 1 / wearable_imu_hz
            delay = min(next_oak_imu, next_emg, next_wearable_imu) - time.monotonic()
            time.sleep(max(0.0001, min(0.001, delay)))


def _simulation_image(
    width: int,
    height: int,
    camera: str,
    sequence: int,
    camera_index: int,
) -> np.ndarray:
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:, :, camera_index % 3] = 55 + camera_index * 35
    position = (sequence * 7 + camera_index * 45) % max(1, width - 80)
    image[120:240, position : position + 80] = (230, 230, 230)
    import cv2

    cv2.putText(
        image,
        f"{camera}  #{sequence}",
        (24, 48),
        cv2.FONT_HERSHEY_SIMPLEX,
        1,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    return image


def _emg_bytes(sequence: int, channels: tuple[int, ...]) -> bytes:
    payload = b"".join(
        int(value).to_bytes(3, byteorder="big", signed=True) for value in channels
    )
    return b"\xD2\xD2\xD2\xAA" + bytes([sequence]) + payload
