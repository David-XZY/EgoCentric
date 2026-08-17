from __future__ import annotations

import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
os.environ.setdefault("QT_OPENGL", "software")

from PySide6.QtCore import QUrl
from PySide6.QtQuick import QQuickItem, QQuickView

from egocentric_capture.cockpit_bridge import CockpitBridge
from egocentric_capture.config import load_config
from egocentric_capture.gesture import simulated_gesture_frame
from egocentric_capture.models import CaptureState, FinalizeProgress
from egocentric_capture.preview import initialize_gstreamer_qml


def test_bridge_exposes_simulated_hands_and_record_metadata() -> None:
    bridge = CockpitBridge(load_config(), simulate=True)
    bridge.participant = "P014"
    bridge.task = "grasp_cup"
    bridge.operator = "operator-02"
    bridge.update_gesture(simulated_gesture_frame(time.monotonic_ns()))

    assert bridge.sessionTitle == "P014 · GRASP_CUP / R--"
    assert bridge.leftHand["gesture"] == "OPEN_PALM"
    assert bridge.rightHand["gesture"] == "PRE-GRASP"


def test_bridge_controls_record_lock_drawer_and_finalize_state() -> None:
    bridge = CockpitBridge(load_config(), simulate=False)
    requested = []
    bridge.record_requested.connect(requested.append)
    bridge.set_capture_state(CaptureState.READY, "设备就绪")
    bridge.participant = "P015"
    bridge.task = "pinch"
    bridge.operator = "operator-03"
    bridge.setDrawerOpen(True)

    bridge.toggleRecording()

    assert requested[0]["participant_id"] == "P015"
    assert bridge.drawerOpen is True
    bridge.set_capture_state(CaptureState.RECORDING, "录制中")
    assert bridge.metadataLocked is True
    assert bridge.drawerOpen is False
    bridge.set_finalize_progress(
        FinalizeProgress(
            stage="quality",
            completed=1,
            total=2,
            message="正在质检",
        )
    )
    assert bridge.finalizing is True
    assert bridge.finalizeProgress == 0.5
    assert bridge.targetOverlay["visible"] is False


def test_cockpit_qml_loads_and_contains_video_surface(qtbot) -> None:
    initialize_gstreamer_qml()
    bridge = CockpitBridge(load_config(), simulate=True)
    view = QQuickView()
    view.rootContext().setContextProperty("uiBridge", bridge)
    qml_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "egocentric_capture"
        / "assets"
        / "cockpit.qml"
    )
    view.setSource(QUrl.fromLocalFile(str(qml_path)))
    qtbot.addWidget(
        __import__("PySide6.QtWidgets", fromlist=["QWidget"]).QWidget.createWindowContainer(
            view
        )
    )

    assert view.status() == QQuickView.Status.Ready
    root = view.rootObject()
    assert isinstance(root, QQuickItem)
    assert root.findChild(QQuickItem, "mosaicVideo") is not None
    assert root.findChild(QQuickItem, "huazhiLogo") is not None
    assert root.property("primaryCamera") == "cam_a"
    view.setSource(QUrl())
