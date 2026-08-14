from __future__ import annotations

import math
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import Property, QObject, Signal, Slot

from .models import (
    CaptureState,
    FinalizeProgress,
    GestureFrame,
    HealthSnapshot,
    WearableEmgSample,
)


class CockpitBridge(QObject):
    changed = Signal()
    record_requested = Signal(object)
    stop_requested = Signal()
    advanced_requested = Signal(object)
    close_confirmed = Signal()
    close_cancelled = Signal()
    gesture_received = Signal(object)

    def __init__(
        self,
        config: dict[str, Any],
        *,
        simulate: bool,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.simulate = simulate
        self._participant = ""
        self._task = ""
        self._hand = "both"
        self._operator = ""
        self._notes = ""
        self._drawer_open = False
        self._demo_background = bool(
            simulate and os.environ.get("EGOCENTRIC_STATIC_DEMO") == "1"
        )
        self._capture_state = CaptureState.DISCONNECTED
        self._state_message = "正在启动设备"
        self._recording_started_at: float | None = None
        self._duration_text = "00:00:00.000"
        self._trial = "r--"
        self._output_path = str(
            Path(
                str(
                    config.get("workspace", {}).get(
                        "output_root",
                        "~/EgoCentricData",
                    )
                )
            ).expanduser()
        )
        self._quality_text = "等待采集"
        self._health: HealthSnapshot | None = None
        self._device_metrics: list[dict[str, Any]] = []
        self._gesture_frame = GestureFrame(
            monotonic_ns=time.monotonic_ns(),
            healthy=simulate,
            message="" if simulate else "等待识别器",
        )
        self._left_hand: dict[str, Any] = {}
        self._right_hand: dict[str, Any] = {}
        self._gesture_label = "WAITING"
        self._gesture_confidence = 0.0
        self._ai_status = "SIMULATED" if simulate else "INITIALIZING"
        self._emg_lock = threading.Lock()
        preview_seconds = float(
            config.get("wearable", {}).get("preview_seconds", 5)
        )
        expected_hz = int(
            config.get("wearable", {}).get("emg_expected_hz", 250)
        )
        self._emg_values: deque[tuple[int, tuple[int, ...]]] = deque(
            maxlen=max(250, int(preview_seconds * expected_hz))
        )
        self._left_emg: list[list[float]] = [[], [], [], []]
        self._right_emg: list[list[float]] = [[], [], [], []]
        self._finalizing = False
        self._finalize_progress = 0.0
        self._finalize_message = ""
        self._close_confirmation = False
        self._toast_message = ""
        self._toast_deadline = 0.0
        self._target_overlay = {
            "visible": simulate,
            "x": 0.435,
            "y": 0.315,
            "width": 0.13,
            "height": 0.19,
            "label": "TARGET / MUG",
            "confidence": 0.982,
        }
        self.gesture_received.connect(self._apply_gesture)

    participant = Property(
        str,
        lambda self: self._participant,
        lambda self, value: self._set_field("_participant", value),
        notify=changed,
    )
    task = Property(
        str,
        lambda self: self._task,
        lambda self, value: self._set_field("_task", value),
        notify=changed,
    )
    hand = Property(
        str,
        lambda self: self._hand,
        lambda self, value: self._set_field("_hand", value),
        notify=changed,
    )
    operator = Property(
        str,
        lambda self: self._operator,
        lambda self, value: self._set_field("_operator", value),
        notify=changed,
    )
    notes = Property(
        str,
        lambda self: self._notes,
        lambda self, value: self._set_field("_notes", value),
        notify=changed,
    )
    drawerOpen = Property(
        bool,
        lambda self: self._drawer_open,
        lambda self, value: self._set_drawer_open(bool(value)),
        notify=changed,
    )
    captureState = Property(
        str,
        lambda self: self._capture_state.value,
        notify=changed,
    )
    stateMessage = Property(
        str,
        lambda self: self._state_message,
        notify=changed,
    )
    recording = Property(
        bool,
        lambda self: self._capture_state
        in {CaptureState.ARMING, CaptureState.RECORDING},
        notify=changed,
    )
    recordEnabled = Property(
        bool,
        lambda self: self._record_enabled(),
        notify=changed,
    )
    metadataLocked = Property(
        bool,
        lambda self: self._capture_state
        in {
            CaptureState.ARMING,
            CaptureState.RECORDING,
            CaptureState.FINALIZING,
        },
        notify=changed,
    )
    durationText = Property(
        str,
        lambda self: self._duration_text,
        notify=changed,
    )
    trialText = Property(str, lambda self: self._trial, notify=changed)
    sessionTitle = Property(
        str,
        lambda self: self._session_title(),
        notify=changed,
    )
    outputPath = Property(
        str,
        lambda self: self._output_path,
        notify=changed,
    )
    qualityText = Property(
        str,
        lambda self: self._quality_text,
        notify=changed,
    )
    deviceMetrics = Property(
        "QVariantList",
        lambda self: self._device_metrics,
        notify=changed,
    )
    fpsText = Property(str, lambda self: self._fps_text(), notify=changed)
    syncText = Property(str, lambda self: self._sync_text(), notify=changed)
    writeRateText = Property(
        str,
        lambda self: self._write_rate_text(),
        notify=changed,
    )
    storageText = Property(
        str,
        lambda self: self._storage_text(),
        notify=changed,
    )
    queueText = Property(
        str,
        lambda self: self._queue_text(),
        notify=changed,
    )
    gestureLabel = Property(
        str,
        lambda self: self._gesture_label,
        notify=changed,
    )
    gestureConfidenceText = Property(
        str,
        lambda self: f"{self._gesture_confidence * 100:.1f}%",
        notify=changed,
    )
    aiStatus = Property(str, lambda self: self._ai_status, notify=changed)
    leftHand = Property(
        "QVariantMap",
        lambda self: self._left_hand,
        notify=changed,
    )
    rightHand = Property(
        "QVariantMap",
        lambda self: self._right_hand,
        notify=changed,
    )
    leftEmg = Property(
        "QVariantList",
        lambda self: self._left_emg,
        notify=changed,
    )
    rightEmg = Property(
        "QVariantList",
        lambda self: self._right_emg,
        notify=changed,
    )
    finalizing = Property(
        bool,
        lambda self: self._finalizing,
        notify=changed,
    )
    finalizeProgress = Property(
        float,
        lambda self: self._finalize_progress,
        notify=changed,
    )
    finalizeMessage = Property(
        str,
        lambda self: self._finalize_message,
        notify=changed,
    )
    closeConfirmation = Property(
        bool,
        lambda self: self._close_confirmation,
        notify=changed,
    )
    toastMessage = Property(
        str,
        lambda self: self._toast_message,
        notify=changed,
    )
    targetOverlay = Property(
        "QVariantMap",
        lambda self: self._target_overlay,
        notify=changed,
    )
    demoBackground = Property(
        bool,
        lambda self: self._demo_background,
        notify=changed,
    )
    wearablePort = Property(
        str,
        lambda self: str(
            self.config.get("wearable", {}).get("port", "auto")
        ),
        notify=changed,
    )
    wearableBaudrate = Property(
        int,
        lambda self: int(
            self.config.get("wearable", {}).get("baudrate", 921600)
        ),
        notify=changed,
    )
    writerQueueSize = Property(
        int,
        lambda self: int(
            self.config.get("session", {}).get(
                "writer_queue_size",
                20000,
            )
        ),
        notify=changed,
    )

    @Slot()
    def toggleRecording(self) -> None:
        if self.recording:
            self.stop_requested.emit()
            return
        if self._capture_state != CaptureState.READY:
            self.show_toast("设备尚未就绪")
            return
        missing = [
            label
            for label, value in (
                ("受试者", self._participant),
                ("任务", self._task),
                ("操作人", self._operator),
            )
            if not value.strip()
        ]
        if missing:
            self._set_drawer_open(True)
            self.show_toast("请填写：" + "、".join(missing))
            return
        self.record_requested.emit(
            {
                "participant_id": self._participant.strip(),
                "task_id": self._task.strip(),
                "hand": self._hand,
                "operator": self._operator.strip(),
                "notes": self._notes.strip(),
            }
        )

    @Slot(bool)
    def setDrawerOpen(self, open_: bool) -> None:
        self._set_drawer_open(open_)

    @Slot(str, int, int)
    def applyAdvancedSettings(
        self,
        port: str,
        baudrate: int,
        queue_size: int,
    ) -> None:
        self.advanced_requested.emit(
            {
                "port": port.strip() or "auto",
                "baudrate": max(9600, int(baudrate)),
                "writer_queue_size": max(1000, int(queue_size)),
            }
        )

    @Slot()
    def confirmClose(self) -> None:
        self._close_confirmation = False
        self.changed.emit()
        self.close_confirmed.emit()

    @Slot()
    def cancelClose(self) -> None:
        self._close_confirmation = False
        self.changed.emit()
        self.close_cancelled.emit()

    def set_capture_state(
        self,
        state: CaptureState,
        message: str,
    ) -> None:
        self._capture_state = state
        self._state_message = message
        if state == CaptureState.RECORDING:
            if self._recording_started_at is None:
                self._recording_started_at = time.monotonic()
            self._drawer_open = False
            self._quality_text = "录制中，等待最终质检"
        elif state in {
            CaptureState.READY,
            CaptureState.COMPLETED,
            CaptureState.FAILED,
        }:
            self._recording_started_at = None
            if state == CaptureState.READY:
                self._finalizing = False
                self._finalize_progress = 0.0
                self._finalize_message = ""
        if state == CaptureState.FINALIZING:
            self._finalizing = True
            self._finalize_message = message
        self.changed.emit()

    def update_health(self, snapshot: HealthSnapshot) -> None:
        self._health = snapshot
        rows = (
            ("camera/cam_a", "CAM A"),
            ("camera/cam_b", "CAM B"),
            ("camera/cam_c", "CAM C"),
            ("camera/cam_d", "CAM D"),
            ("imu/oak", "OAK IMU"),
            ("wearable/emg", "WRIST EMG"),
            ("wearable/imu", "WRIST IMU"),
        )
        metrics: list[dict[str, Any]] = []
        for key, label in rows:
            rate = float(snapshot.rates_hz.get(key, 0.0))
            age = float(snapshot.last_seen_age_s.get(key, math.inf))
            healthy = age <= 1.0 and rate > 0
            metrics.append(
                {
                    "key": key,
                    "label": label,
                    "rate": f"{rate:.1f} HZ",
                    "state": "ONLINE" if healthy else "OFFLINE",
                    "healthy": healthy,
                }
            )
        self._device_metrics = metrics
        self.changed.emit()

    def append_emg(self, sample: WearableEmgSample) -> None:
        with self._emg_lock:
            self._emg_values.append(
                (sample.stamp.monotonic_ns, sample.channels_uv)
            )

    def refresh_emg(self) -> None:
        with self._emg_lock:
            samples = tuple(self._emg_values)
        if not samples:
            return
        values = np.asarray([sample[1] for sample in samples], dtype=np.float64)
        if values.ndim != 2 or values.shape[1] < 8:
            return
        target_points = 180
        step = max(1, len(values) // target_points)
        values = values[::step][-target_points:]
        left = values[:, :4]
        right = values[:, 4:8]
        self._left_emg = _normalize_emg(left)
        self._right_emg = _normalize_emg(right)
        self.changed.emit()

    def update_gesture(self, frame: GestureFrame) -> None:
        self.gesture_received.emit(frame)

    @Slot(object)
    def _apply_gesture(self, frame: GestureFrame) -> None:
        self._gesture_frame = frame
        self._ai_status = "ONLINE" if frame.healthy else "OFFLINE"
        left: dict[str, Any] = {}
        right: dict[str, Any] = {}
        strongest = None
        for hand in frame.hands:
            payload = {
                "handedness": hand.handedness,
                "gesture": hand.gesture,
                "confidence": hand.confidence,
                "staleMs": hand.stale_ms,
                "landmarks": [
                    {"x": x, "y": y, "z": z}
                    for x, y, z in hand.landmarks
                ],
            }
            if hand.handedness == "LEFT":
                left = payload
            elif hand.handedness == "RIGHT":
                right = payload
            if strongest is None or hand.confidence > strongest.confidence:
                strongest = hand
        self._left_hand = left
        self._right_hand = right
        if strongest is not None:
            self._gesture_label = strongest.gesture
            self._gesture_confidence = strongest.confidence
        elif frame.healthy:
            self._gesture_label = "NO HAND"
            self._gesture_confidence = 0.0
        else:
            self._gesture_label = "AI OFFLINE"
            self._gesture_confidence = 0.0
            if frame.message:
                self.show_toast("手势识别不可用：" + frame.message)
                return
        self.changed.emit()

    def update_session(
        self,
        path: Path | None,
        manifest: dict[str, Any] | None,
    ) -> None:
        if path is not None:
            self._output_path = str(path)
        if manifest:
            status = manifest.get("status")
            quality = manifest.get("quality") or {}
            if status == "completed":
                sync = quality.get("camera_sync") or {}
                self._quality_text = (
                    "质检通过 · SYNC P95 "
                    f"{float(sync.get('p95_ms', 0)):.3f} MS"
                )
            elif status == "failed":
                reason = str(
                    manifest.get("failure_reason") or "未通过质检"
                )
                self._quality_text = "质检失败 · " + reason
        self.changed.emit()

    def set_trial(self, trial: int | None) -> None:
        value = "r--" if trial is None else f"r{trial:02d}"
        if self._trial == value:
            return
        self._trial = value
        self.changed.emit()

    def set_finalize_progress(self, progress: FinalizeProgress) -> None:
        self._finalizing = True
        self._finalize_message = progress.message
        self._finalize_progress = min(
            1.0,
            max(0.0, progress.completed / max(1, progress.total)),
        )
        self.changed.emit()

    def set_finalize_result(self, message: str) -> None:
        self._finalizing = True
        self._finalize_progress = 1.0
        self._finalize_message = message
        self.changed.emit()

    def show_close_confirmation(self) -> None:
        self._close_confirmation = True
        self.changed.emit()

    def show_toast(self, message: str, duration_s: float = 3.5) -> None:
        self._toast_message = message
        self._toast_deadline = time.monotonic() + duration_s
        self.changed.emit()

    def tick(self) -> None:
        if self._recording_started_at is not None:
            elapsed = time.monotonic() - self._recording_started_at
            self._duration_text = _clock_text(elapsed)
        else:
            self._duration_text = "00:00:00.000"
        if self._toast_message and time.monotonic() >= self._toast_deadline:
            self._toast_message = ""
        self.changed.emit()

    def _set_field(self, name: str, value: str) -> None:
        normalized = str(value)
        if getattr(self, name) == normalized:
            return
        setattr(self, name, normalized)
        self.changed.emit()

    def _set_drawer_open(self, open_: bool) -> None:
        if self._drawer_open == open_:
            return
        self._drawer_open = open_
        self.changed.emit()

    def _record_enabled(self) -> bool:
        if self.recording:
            return True
        return (
            self._capture_state == CaptureState.READY
            and bool(self._participant.strip())
            and bool(self._task.strip())
            and bool(self._operator.strip())
        )

    def _session_title(self) -> str:
        participant = self._participant.strip() or "P---"
        task = self._task.strip().upper() or "TASK"
        return f"{participant} · {task} / {self._trial.upper()}"

    def _fps_text(self) -> str:
        if self._health is None:
            return "--"
        return f"{self._health.rates_hz.get('camera/cam_a', 0.0):.2f}"

    def _sync_text(self) -> str:
        if self._health is None:
            return "-- MS"
        return f"{self._health.preview_cross_camera_skew_ms:+.2f} MS"

    def _write_rate_text(self) -> str:
        if self._health is None:
            return "-- MB/S"
        value = self._health.writer_bytes_per_second / 1_000_000
        return f"{value:.1f} MB/S"

    def _storage_text(self) -> str:
        if self._health is None:
            return "-- GB"
        return f"{self._health.disk_free_bytes / 1024**3:.1f} GB"

    def _queue_text(self) -> str:
        if self._health is None:
            return "0 / 0"
        return (
            f"{self._health.queue_depth} / "
            f"{self._health.writer_high_watermark}"
        )


def _normalize_emg(values: np.ndarray) -> list[list[float]]:
    scale = max(1000.0, float(np.nanpercentile(np.abs(values), 98)))
    clipped = np.clip(values / scale, -1.0, 1.0)
    return [
        [float(value) for value in clipped[:, index]]
        for index in range(clipped.shape[1])
    ]


def _clock_text(seconds: float) -> str:
    milliseconds = max(0, int(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds_value, millis = divmod(remainder, 1000)
    return (
        f"{hours:02d}:{minutes:02d}:{seconds_value:02d}.{millis:03d}"
    )
