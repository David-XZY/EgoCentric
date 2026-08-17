from __future__ import annotations

import math
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .models import GestureFrame, TrackedHand

GestureCallback = Callable[[GestureFrame], None]


class GestureWorker:
    """在独立线程中处理最新一帧手势识别任务。"""

    def __init__(
        self,
        config: dict[str, Any],
        callback: GestureCallback,
    ) -> None:
        self.config = config
        self.callback = callback
        self._queue: queue.Queue[tuple[np.ndarray, int] | None] = queue.Queue(
            maxsize=1
        )
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._previous_landmarks: dict[
            str,
            tuple[tuple[float, float, float], ...],
        ] = {}
        self._last_hands: dict[str, TrackedHand] = {}
        self._last_seen_ns: dict[str, int] = {}
        self._last_timestamp_ms = -1

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="gesture-recognizer",
            daemon=True,
        )
        self._thread.start()

    def submit(self, image: np.ndarray, monotonic_ns: int) -> None:
        if self._stop_event.is_set() or not self.running:
            return
        frame = (np.ascontiguousarray(image), int(monotonic_ns))
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(frame)
            except queue.Full:
                pass

    def stop(self, timeout_s: float = 3.0) -> None:
        self._stop_event.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout_s)
            if not thread.is_alive():
                self._thread = None

    def _run(self) -> None:
        recognizer = None
        try:
            import mediapipe as mp
            from mediapipe.tasks.python import BaseOptions, vision

            model_path = Path(
                str(
                    self.config.get(
                        "model_path",
                        _default_model_path(),
                    )
                )
            ).expanduser()
            options = vision.GestureRecognizerOptions(
                base_options=BaseOptions(model_asset_path=str(model_path)),
                running_mode=vision.RunningMode.VIDEO,
                num_hands=int(self.config.get("max_hands", 2)),
                min_hand_detection_confidence=float(
                    self.config.get("min_detection_confidence", 0.6)
                ),
                min_hand_presence_confidence=float(
                    self.config.get("min_presence_confidence", 0.6)
                ),
                min_tracking_confidence=float(
                    self.config.get("min_tracking_confidence", 0.5)
                ),
            )
            recognizer = vision.GestureRecognizer.create_from_options(options)
            while not self._stop_event.is_set():
                try:
                    item = self._queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    if item is None:
                        break
                    image, monotonic_ns = item
                    timestamp_ms = max(
                        self._last_timestamp_ms + 1,
                        monotonic_ns // 1_000_000,
                    )
                    self._last_timestamp_ms = timestamp_ms
                    started_ns = time.monotonic_ns()
                    result = recognizer.recognize_for_video(
                        mp.Image(
                            image_format=mp.ImageFormat.SRGB,
                            data=image,
                        ),
                        timestamp_ms,
                    )
                    inference_ms = (
                        time.monotonic_ns() - started_ns
                    ) / 1_000_000
                    self.callback(
                        self._to_frame(
                            result,
                            monotonic_ns,
                            inference_ms,
                        )
                    )
                finally:
                    self._queue.task_done()
        except Exception as exc:
            self.callback(
                GestureFrame(
                    monotonic_ns=time.monotonic_ns(),
                    healthy=False,
                    message=f"{type(exc).__name__}: {exc}",
                )
            )
        finally:
            if recognizer is not None:
                recognizer.close()

    def _to_frame(
        self,
        result: Any,
        monotonic_ns: int,
        inference_ms: float,
    ) -> GestureFrame:
        detected: dict[str, TrackedHand] = {}
        count = min(
            len(result.hand_landmarks),
            len(result.hand_world_landmarks),
            len(result.handedness),
        )
        for index in range(count):
            handedness_category = result.handedness[index][0]
            handedness = str(
                getattr(handedness_category, "category_name", "")
                or getattr(handedness_category, "display_name", "")
                or f"Hand{index}"
            ).upper()
            landmarks = tuple(
                (float(point.x), float(point.y), float(point.z))
                for point in result.hand_landmarks[index]
            )
            world_landmarks = tuple(
                (float(point.x), float(point.y), float(point.z))
                for point in result.hand_world_landmarks[index]
            )
            landmarks = self._smooth(handedness, landmarks)
            canned_name = "NONE"
            canned_score = 0.0
            if index < len(result.gestures) and result.gestures[index]:
                category = result.gestures[index][0]
                canned_name = str(
                    getattr(category, "category_name", "NONE") or "NONE"
                )
                canned_score = float(getattr(category, "score", 0.0))
            gesture, confidence = semantic_gesture(
                landmarks,
                canned_name,
                canned_score,
                score_threshold=float(
                    self.config.get("gesture_score_threshold", 0.55)
                ),
                pinch_ratio_threshold=float(
                    self.config.get("pinch_ratio_threshold", 0.22)
                ),
            )
            hand = TrackedHand(
                handedness=handedness,
                landmarks=landmarks,
                world_landmarks=world_landmarks,
                gesture=gesture,
                confidence=confidence,
            )
            detected[handedness] = hand
            self._last_hands[handedness] = hand
            self._last_seen_ns[handedness] = monotonic_ns

        stale_after_ms = float(self.config.get("stale_after_ms", 500))
        for handedness, previous in tuple(self._last_hands.items()):
            if handedness in detected:
                continue
            stale_ms = max(
                0.0,
                (monotonic_ns - self._last_seen_ns[handedness]) / 1_000_000,
            )
            if stale_ms > stale_after_ms:
                self._last_hands.pop(handedness, None)
                self._last_seen_ns.pop(handedness, None)
                self._previous_landmarks.pop(handedness, None)
                continue
            detected[handedness] = TrackedHand(
                handedness=previous.handedness,
                landmarks=previous.landmarks,
                world_landmarks=previous.world_landmarks,
                gesture=previous.gesture,
                confidence=previous.confidence,
                stale_ms=stale_ms,
            )

        hands = tuple(
            detected[key]
            for key in ("LEFT", "RIGHT")
            if key in detected
        )
        return GestureFrame(
            monotonic_ns=monotonic_ns,
            hands=hands,
            inference_ms=inference_ms,
        )

    def _smooth(
        self,
        handedness: str,
        landmarks: tuple[tuple[float, float, float], ...],
    ) -> tuple[tuple[float, float, float], ...]:
        previous = self._previous_landmarks.get(handedness)
        alpha = min(
            1.0,
            max(0.0, float(self.config.get("smoothing_alpha", 0.72))),
        )
        if previous is None or len(previous) != len(landmarks):
            smoothed = landmarks
        else:
            smoothed = tuple(
                tuple(
                    old_value + (new_value - old_value) * alpha
                    for old_value, new_value in zip(old, new, strict=True)
                )
                for old, new in zip(previous, landmarks, strict=True)
            )
        self._previous_landmarks[handedness] = smoothed
        return smoothed


def semantic_gesture(
    landmarks: tuple[tuple[float, float, float], ...],
    canned_name: str,
    canned_score: float,
    *,
    score_threshold: float = 0.55,
    pinch_ratio_threshold: float = 0.22,
) -> tuple[str, float]:
    if len(landmarks) >= 21:
        palm_width = _distance(landmarks[5], landmarks[17])
        pinch_ratio = _distance(landmarks[4], landmarks[8]) / max(
            palm_width,
            1e-6,
        )
        if pinch_ratio <= pinch_ratio_threshold:
            confidence = 1.0 - min(1.0, pinch_ratio / pinch_ratio_threshold)
            return "PINCH", max(0.55, confidence)

    normalized = canned_name.strip().upper().replace(" ", "_")
    if (
        normalized not in {"", "NONE", "UNKNOWN"}
        and canned_score >= score_threshold
    ):
        return normalized, canned_score

    if len(landmarks) >= 21:
        partial_count = sum(
            25.0 <= _finger_bend_degrees(landmarks, indices) <= 110.0
            for indices in (
                (5, 6, 8),
                (9, 10, 12),
                (13, 14, 16),
                (17, 18, 20),
            )
        )
        if partial_count >= 2:
            return "PRE-GRASP", min(0.95, 0.55 + partial_count * 0.1)
    return "NONE", max(0.0, canned_score)


def simulated_gesture_frame(monotonic_ns: int) -> GestureFrame:
    phase = monotonic_ns / 1_000_000_000
    return GestureFrame(
        monotonic_ns=monotonic_ns,
        hands=(
            _simulated_hand("LEFT", phase, -1.0),
            _simulated_hand("RIGHT", phase + 0.7, 1.0),
        ),
        inference_ms=6.4,
    )


def _simulated_hand(
    handedness: str,
    phase: float,
    direction: float,
) -> TrackedHand:
    base = (
        (0.50, 0.78, 0.00),
        (0.37, 0.68, 0.02),
        (0.28, 0.56, 0.03),
        (0.22, 0.44, 0.02),
        (0.20, 0.34, 0.00),
        (0.42, 0.56, 0.02),
        (0.40, 0.40, 0.03),
        (0.40, 0.25, 0.02),
        (0.41, 0.12, 0.00),
        (0.51, 0.54, 0.03),
        (0.51, 0.36, 0.04),
        (0.51, 0.20, 0.02),
        (0.51, 0.06, 0.00),
        (0.60, 0.57, 0.02),
        (0.62, 0.40, 0.03),
        (0.63, 0.25, 0.01),
        (0.64, 0.13, -0.01),
        (0.68, 0.63, 0.00),
        (0.73, 0.50, 0.01),
        (0.76, 0.39, -0.01),
        (0.78, 0.30, -0.03),
    )
    wobble = math.sin(phase * 1.7) * 0.012
    points = tuple(
        (
            0.5 + direction * (x - 0.5),
            y + wobble * math.sin(index * 0.7),
            z,
        )
        for index, (x, y, z) in enumerate(base)
    )
    return TrackedHand(
        handedness=handedness,
        landmarks=points,
        world_landmarks=points,
        gesture="OPEN_PALM" if handedness == "LEFT" else "PRE-GRASP",
        confidence=0.968 if handedness == "LEFT" else 0.984,
    )


def _finger_bend_degrees(
    landmarks: tuple[tuple[float, float, float], ...],
    indices: tuple[int, int, int],
) -> float:
    first, middle, last = (landmarks[index] for index in indices)
    vector_a = tuple(a - b for a, b in zip(first, middle, strict=True))
    vector_b = tuple(a - b for a, b in zip(last, middle, strict=True))
    length_a = math.sqrt(sum(value * value for value in vector_a))
    length_b = math.sqrt(sum(value * value for value in vector_b))
    if length_a <= 1e-9 or length_b <= 1e-9:
        return 0.0
    cosine = sum(
        a * b for a, b in zip(vector_a, vector_b, strict=True)
    ) / (length_a * length_b)
    angle = math.degrees(math.acos(max(-1.0, min(1.0, cosine))))
    return 180.0 - angle


def _distance(
    first: tuple[float, float, float],
    second: tuple[float, float, float],
) -> float:
    return math.sqrt(
        sum(
            (a - b) ** 2
            for a, b in zip(first, second, strict=True)
        )
    )


def _default_model_path() -> Path:
    return (
        Path(__file__).resolve().parent
        / "assets"
        / "models"
        / "gesture_recognizer.task"
    )
