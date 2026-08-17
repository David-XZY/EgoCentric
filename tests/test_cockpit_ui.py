from __future__ import annotations

import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
os.environ.setdefault("QT_OPENGL", "software")

from PySide6.QtCore import QUrl
from PySide6.QtGui import QImage
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
    assert root.findChild(QQuickItem, "videoHandOverlay") is not None
    assert root.findChild(QQuickItem, "huazhiLogo") is not None
    assert root.findChild(QQuickItem, "productName") is not None
    assert root.findChild(QQuickItem, "cockpitLabel") is not None
    assert root.findChild(QQuickItem, "handPanel_LEFT") is None
    assert root.findChild(QQuickItem, "handPanel_RIGHT") is None
    left_emg_panel = root.findChild(QQuickItem, "emgPanel_LEFT")
    right_emg_panel = root.findChild(QQuickItem, "emgPanel_RIGHT")
    assert left_emg_panel is not None
    assert right_emg_panel is not None
    assert left_emg_panel.width() == right_emg_panel.width()
    assert left_emg_panel.height() == right_emg_panel.height()
    assert left_emg_panel.height() < left_emg_panel.width() * 0.4
    right_metric_dots = root.findChildren(QQuickItem, "rightMetricDot")
    assert len(right_metric_dots) == 4
    assert len(
        {round(float(dot.property("x")), 3) for dot in right_metric_dots}
    ) == 1
    assert root.property("primaryCamera") == "cam_a"
    requested = []
    root.primaryCameraRequested.connect(requested.append)
    root.selectPrimaryCamera("cam_b")
    assert requested == ["cam_b"]
    assert root.property("primaryCamera") == "cam_a"

    qml_source = qml_path.read_text(encoding="utf-8")
    logo_path = qml_path.parent / "huazhi_logo.webp"
    hud_logo_path = qml_path.parent / "huazhi_logo_hud.png"
    assert logo_path.exists()
    assert hud_logo_path.exists()
    logo_image = QImage(str(logo_path))
    hud_logo_image = QImage(str(hud_logo_path))
    assert not logo_image.isNull()
    assert not hud_logo_image.isNull()
    assert logo_image.hasAlphaChannel()
    assert hud_logo_image.hasAlphaChannel()
    assert logo_image.pixelColor(0, 0).alpha() == 0
    assert hud_logo_image.pixelColor(0, 0).alpha() == 0
    assert 'source: "huazhi_logo_hud.png"' in qml_source
    assert "MultiEffect" not in qml_source
    assert 'text: "EgoCentric"' in qml_source
    assert "数据驾驶舱  /  DATA COCKPIT" in qml_source
    assert "trajectoryCanvas" not in qml_source
    assert "setLineDash" not in qml_source
    assert "renderedPose" not in qml_source
    assert "component HandPanel" not in qml_source
    assert 'context.fillStyle = "#e6fffc"' in qml_source
    assert "handWristColor" not in qml_source
    assert "root.cameraViewport(root.gestureCamera)" in qml_source
    assert "context.strokeRect(boxX, boxY, boxWidth, boxHeight)" in qml_source
    assert "values.length - 1 - index" in qml_source
    assert "context.createLinearGradient" in qml_source
    view.setSource(QUrl())
