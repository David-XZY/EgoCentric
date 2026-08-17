from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import numpy as np

from egocentric_capture.config import load_config
from egocentric_capture.gesture import (
    GestureWorker,
    semantic_gesture,
    simulated_gesture_frame,
)


def _open_hand() -> tuple[tuple[float, float, float], ...]:
    return simulated_gesture_frame(1_000_000_000).hands[0].landmarks


def test_pinch_has_priority_over_canned_gesture() -> None:
    points = list(_open_hand())
    points[4] = points[8]

    gesture, confidence = semantic_gesture(
        tuple(points),
        "Open_Palm",
        0.99,
    )

    assert gesture == "PINCH"
    assert confidence >= 0.55


def test_confident_canned_gesture_is_preserved() -> None:
    gesture, confidence = semantic_gesture(
        _open_hand(),
        "Victory",
        0.92,
    )

    assert gesture == "VICTORY"
    assert confidence == 0.92


def test_simulated_frame_contains_stable_left_and_right_hands() -> None:
    frame = simulated_gesture_frame(2_000_000_000)

    assert [hand.handedness for hand in frame.hands] == ["LEFT", "RIGHT"]
    assert all(len(hand.landmarks) == 21 for hand in frame.hands)
    assert frame.hands[1].gesture == "PRE-GRASP"


def test_landmark_smoothing_and_short_stale_window() -> None:
    config = load_config()["gesture"]
    worker = GestureWorker(config, lambda _frame: None)
    first = tuple((0.0, 0.0, 0.0) for _ in range(21))
    second = tuple((1.0, 1.0, 1.0) for _ in range(21))

    assert worker._smooth("LEFT", first) == first
    smoothed = worker._smooth("LEFT", second)
    assert config["inference_fps"] == 25
    assert smoothed[0] == (1.0, 1.0, 1.0)

    now = time.monotonic_ns()
    previous = simulated_gesture_frame(now).hands[0]
    worker._last_hands["LEFT"] = previous
    worker._last_seen_ns["LEFT"] = now - 250_000_000
    frame = worker._to_frame(
        SimpleNamespace(
            hand_landmarks=[],
            hand_world_landmarks=[],
            handedness=[],
            gestures=[],
        ),
        now,
        1.0,
    )

    assert len(frame.hands) == 1
    assert 249 <= frame.hands[0].stale_ms <= 251


def test_hand_is_cleared_after_stale_timeout() -> None:
    config = load_config()["gesture"]
    worker = GestureWorker(config, lambda _frame: None)
    now = time.monotonic_ns()
    worker._last_hands["LEFT"] = simulated_gesture_frame(now).hands[0]
    worker._last_seen_ns["LEFT"] = now - 501_000_000

    frame = worker._to_frame(
        SimpleNamespace(
            hand_landmarks=[],
            hand_world_landmarks=[],
            handedness=[],
            gestures=[],
        ),
        now,
        1.0,
    )

    assert frame.hands == ()
    assert "LEFT" not in worker._last_hands


def test_inference_queue_keeps_only_latest_frame() -> None:
    worker = GestureWorker(load_config()["gesture"], lambda _frame: None)
    worker._thread = threading.current_thread()
    first = np.zeros((4, 4, 3), dtype=np.uint8)
    latest = np.ones((4, 4, 3), dtype=np.uint8)

    worker.submit(first, 1)
    worker.submit(latest, 2)

    image, timestamp = worker._queue.get_nowait()
    assert timestamp == 2
    assert np.array_equal(image, latest)


def test_missing_model_degrades_to_unhealthy_frame(tmp_path) -> None:
    result_ready = threading.Event()
    results = []
    config = {
        **load_config()["gesture"],
        "model_path": str(tmp_path / "missing.task"),
    }
    worker = GestureWorker(
        config,
        lambda frame: (results.append(frame), result_ready.set()),
    )
    worker.start()
    try:
        assert result_ready.wait(3)
        assert results[-1].healthy is False
        assert "missing.task" in results[-1].message
    finally:
        worker.stop()


def test_mediapipe_worker_processes_latest_rgb_frame() -> None:
    result_ready = threading.Event()
    results = []
    config = load_config()["gesture"]
    worker = GestureWorker(
        config,
        lambda frame: (results.append(frame), result_ready.set()),
    )
    worker.start()
    try:
        worker.submit(
            np.zeros(
                (
                    int(config["input_height"]),
                    int(config["input_width"]),
                    3,
                ),
                dtype=np.uint8,
            ),
            time.monotonic_ns(),
        )
        assert result_ready.wait(5)
        assert results[-1].healthy is True
    finally:
        worker.stop()
