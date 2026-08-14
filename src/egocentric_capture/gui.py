from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QLockFile, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QColor, QPalette
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtQuick import (
    QQuickItem,
    QQuickView,
    QQuickWindow,
    QSGRendererInterface,
)
from PySide6.QtWidgets import QApplication, QMainWindow, QWidget

from .cockpit_bridge import CockpitBridge
from .gesture import GestureWorker, simulated_gesture_frame
from .models import (
    CaptureRequest,
    CaptureState,
    FinalizeProgress,
    GestureFrame,
    HealthSnapshot,
    PreviewHealth,
)
from .preview import (
    GstreamerPreviewController,
    PreviewBackendError,
    initialize_gstreamer_qml,
)
from .session import CaptureEngine, EngineCallbacks


class _UiSignals(QObject):
    state = Signal(object, str)
    health = Signal(object)
    session = Signal(object, object)
    finalize_progress = Signal(object)
    shutdown = Signal(object)


class CaptureWindow(QMainWindow):
    """承载 QML 数据驾驶舱和采集生命周期。"""

    def __init__(self, config: dict[str, Any], *, simulate: bool = False) -> None:
        super().__init__()
        self.config = config
        self.simulate = simulate
        self.signals = _UiSignals()
        self._closing = False
        self._allow_close = False
        self._last_result_failed = False
        self._last_event_loop_tick = time.monotonic()
        self._event_loop_lag_max_ms = 0.0
        self.setWindowTitle(
            "EgoCentric Data Cockpit" + (" · 模拟模式" if simulate else "")
        )
        self.resize(1600, 900)
        self.setMinimumSize(1120, 720)

        self.bridge = CockpitBridge(config, simulate=simulate, parent=self)
        self.bridge.record_requested.connect(self._request_record)
        self.bridge.stop_requested.connect(self._request_stop)
        self.bridge.advanced_requested.connect(self._apply_advanced)
        self.bridge.close_confirmed.connect(self._begin_shutdown)

        initialize_gstreamer_qml()
        self.quick_view = QQuickView()
        self.quick_view.setResizeMode(
            QQuickView.ResizeMode.SizeRootObjectToView
        )
        self.quick_view.rootContext().setContextProperty(
            "uiBridge",
            self.bridge,
        )
        qml_path = (
            Path(__file__).resolve().parent
            / "assets"
            / "cockpit.qml"
        )
        self.quick_view.setSource(QUrl.fromLocalFile(str(qml_path)))
        if self.quick_view.status() == QQuickView.Status.Error:
            details = "; ".join(
                error.toString() for error in self.quick_view.errors()
            )
            raise RuntimeError(f"驾驶舱 QML 加载失败: {details}")
        root_item = self.quick_view.rootObject()
        if not isinstance(root_item, QQuickItem):
            raise RuntimeError("驾驶舱 QML 根对象不是 QQuickItem")
        video_item = root_item.findChild(QQuickItem, "mosaicVideo")
        if video_item is None:
            raise RuntimeError("驾驶舱 QML 缺少 mosaicVideo")

        container = QWidget.createWindowContainer(self.quick_view, self)
        container.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCentralWidget(container)

        gesture_config = dict(config.get("gesture") or {})
        gesture_enabled = bool(gesture_config.get("enabled", True))
        self.gesture_worker: GestureWorker | None = None
        analysis_callback = None
        if gesture_enabled and not simulate:
            self.gesture_worker = GestureWorker(
                gesture_config,
                self.bridge.update_gesture,
            )
            self.gesture_worker.start()
            analysis_callback = self.gesture_worker.submit
        elif not gesture_enabled:
            self.bridge.update_gesture(
                GestureFrame(
                    monotonic_ns=time.monotonic_ns(),
                    healthy=False,
                    message="手势识别已在配置中关闭",
                )
            )

        preview_config = dict(config.get("preview") or {})
        preview_config.update(
            {
                "primary_camera": gesture_config.get(
                    "primary_camera",
                    "cam_a",
                ),
                "analysis_camera": gesture_config.get(
                    "primary_camera",
                    "cam_a",
                ),
                "analysis_width": gesture_config.get(
                    "input_width",
                    640,
                ),
                "analysis_height": gesture_config.get(
                    "input_height",
                    360,
                ),
                "analysis_fps": gesture_config.get(
                    "inference_fps",
                    12,
                ),
            }
        )
        self.preview_controller: GstreamerPreviewController | None = None
        try:
            self.preview_controller = GstreamerPreviewController(
                preview_config,
                self.quick_view,
                video_item,
                analysis_callback=analysis_callback,
                parent=self,
            )
            self.preview_controller.health_changed.connect(
                self._on_preview_health
            )
        except PreviewBackendError as exc:
            self.bridge.show_toast(f"预览不可用，原始录制仍可继续：{exc}")

        self._connect_signals()
        self.engine = CaptureEngine(
            config,
            simulate=simulate,
            callbacks=EngineCallbacks(
                on_state=lambda state, message: (
                    self.signals.state.emit(state, message)
                ),
                on_health=lambda snapshot: self.signals.health.emit(snapshot),
                on_preview=self._submit_preview_direct,
                on_emg=self.bridge.append_emg,
                on_session=lambda path, manifest: (
                    self.signals.session.emit(path, manifest)
                ),
                on_finalize_progress=lambda progress: (
                    self.signals.finalize_progress.emit(progress)
                ),
            ),
        )

        self._ui_timer = QTimer(self)
        self._ui_timer.timeout.connect(self._refresh_ui)
        self._ui_timer.start(100)
        self._emg_timer = QTimer(self)
        self._emg_timer.timeout.connect(self.bridge.refresh_emg)
        self._emg_timer.start(
            max(
                40,
                int(
                    config.get("wearable", {}).get(
                        "plot_interval_ms",
                        100,
                    )
                ),
            )
        )
        self._event_loop_timer = QTimer(self)
        self._event_loop_timer.timeout.connect(self._measure_event_loop_lag)
        self._event_loop_timer.start(100)

        if self.preview_controller is not None:
            QTimer.singleShot(0, self.preview_controller.start)
            QTimer.singleShot(0, self._publish_initial_preview_health)
        QTimer.singleShot(0, self.engine.start)

    def _connect_signals(self) -> None:
        self.signals.state.connect(self._on_state)
        self.signals.health.connect(self._on_health)
        self.signals.session.connect(self._on_session)
        self.signals.finalize_progress.connect(self._on_finalize_progress)
        self.signals.shutdown.connect(self._on_shutdown_complete)

    def _request_record(self, values: dict[str, Any]) -> None:
        request = CaptureRequest(
            participant_id=str(values["participant_id"]),
            task_id=str(values["task_id"]),
            hand=str(values["hand"]),
            operator=str(values["operator"]),
            notes=str(values.get("notes", "")),
        )
        try:
            self.engine.request_record(request)
        except Exception as exc:
            self.bridge.show_toast(f"无法开始录制：{exc}")

    def _request_stop(self) -> None:
        if self.engine.state in {
            CaptureState.ARMING,
            CaptureState.RECORDING,
        }:
            self.engine.request_stop()

    def _apply_advanced(self, values: dict[str, Any]) -> None:
        self.config.setdefault("wearable", {})["port"] = str(values["port"])
        self.config["wearable"]["baudrate"] = int(values["baudrate"])
        self.config.setdefault("session", {})["writer_queue_size"] = int(
            values["writer_queue_size"]
        )
        self.bridge.changed.emit()
        self.bridge.show_toast("高级参数已更新，重新启动应用后生效")

    def _on_state(self, state: CaptureState, message: str) -> None:
        self.bridge.set_capture_state(state, message)
        if state in {CaptureState.COMPLETED, CaptureState.FAILED}:
            self._last_result_failed = state == CaptureState.FAILED
            if not self._closing:
                QTimer.singleShot(1200, self.engine.prepare_next)

    def _on_health(self, snapshot: HealthSnapshot) -> None:
        self.bridge.update_health(snapshot)

    def _submit_preview_direct(self, frame: Any) -> None:
        if self.preview_controller is not None:
            self.preview_controller.submit(frame)

    def _on_preview_health(self, health: PreviewHealth) -> None:
        if self.preview_controller is not None:
            self.preview_controller.set_health(health)
        if hasattr(self, "engine"):
            self.engine.update_preview_health(health)

    def _publish_initial_preview_health(self) -> None:
        if self.preview_controller is None:
            return
        for health in self.preview_controller.health().values():
            self._on_preview_health(health)

    def _on_session(
        self,
        path: Path | None,
        manifest: dict[str, Any] | None,
    ) -> None:
        self.bridge.update_session(path, manifest)
        self._refresh_trial()

    def _on_finalize_progress(self, progress: FinalizeProgress) -> None:
        self.bridge.set_finalize_progress(progress)

    def _refresh_ui(self) -> None:
        self.bridge.tick()
        if self.simulate:
            self.bridge.update_gesture(
                simulated_gesture_frame(time.monotonic_ns())
            )
        if self.engine.state == CaptureState.READY:
            self._refresh_trial()

    def _refresh_trial(self) -> None:
        participant = self.bridge.participant.strip()
        task = self.bridge.task.strip()
        if not participant or not task or not hasattr(self, "engine"):
            self.bridge.set_trial(None)
            return
        self.bridge.set_trial(self.engine.next_trial(participant, task))

    def _measure_event_loop_lag(self) -> None:
        now = time.monotonic()
        lag_ms = max(0.0, (now - self._last_event_loop_tick - 0.1) * 1000)
        self._last_event_loop_tick = now
        self._event_loop_lag_max_ms = max(
            self._event_loop_lag_max_ms,
            lag_ms,
        )
        if hasattr(self, "engine"):
            self.engine.update_event_loop_lag(lag_ms)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._allow_close:
            event.accept()
            return
        if self._closing:
            event.ignore()
            return
        if hasattr(self, "engine") and self.engine.recording:
            self.bridge.show_close_confirmation()
            event.ignore()
            return
        self._begin_shutdown()
        event.ignore()

    def _begin_shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.bridge.set_finalize_progress(
            FinalizeProgress(
                stage="shutdown",
                completed=0,
                total=1,
                message="正在停止新数据并等待已接收帧持久化",
            )
        )
        self._ui_timer.stop()
        self._emg_timer.stop()
        self._event_loop_timer.stop()

        def engine_stopped(error: str | None) -> None:
            preview_error: str | None = None
            try:
                if self.preview_controller is not None:
                    self.preview_controller.stop()
            except Exception as exc:
                preview_error = f"{type(exc).__name__}: {exc}"
            try:
                if self.gesture_worker is not None:
                    self.gesture_worker.stop()
            except Exception as exc:
                if preview_error is None:
                    preview_error = f"{type(exc).__name__}: {exc}"
            self.signals.shutdown.emit(error or preview_error)

        self.engine.shutdown_async(engine_stopped)

    def _on_shutdown_complete(self, error: str | None) -> None:
        self._allow_close = True
        self.bridge.set_finalize_result(
            f"关闭失败：{error}" if error else "收尾完成，正在退出"
        )
        QTimer.singleShot(
            1500 if error else 100,
            QApplication.instance().quit,
        )


def run_gui(config: dict[str, Any], *, simulate: bool = False) -> int:
    os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Fusion")
    application = QApplication.instance() or QApplication(sys.argv)
    application.setApplicationName("EgoCentric Capture")
    application.setPalette(_dark_palette())
    QQuickWindow.setGraphicsApi(
        QSGRendererInterface.GraphicsApi.OpenGL
    )
    server_name = f"egocentric-capture-{os.getuid()}"
    if _activate_existing_instance(server_name):
        return 0
    lock = QLockFile(f"/tmp/{server_name}.lock")
    lock.setStaleLockTime(30_000)
    if not lock.tryLock(0):
        if _activate_existing_instance(server_name, timeout_ms=800):
            return 0
        return 1
    QLocalServer.removeServer(server_name)
    server = QLocalServer(application)
    if not server.listen(server_name):
        lock.unlock()
        return 1
    window = CaptureWindow(config, simulate=simulate)

    def activate_window() -> None:
        while server.hasPendingConnections():
            socket = server.nextPendingConnection()
            socket.readAll()
            socket.disconnectFromServer()
        if window.isMinimized():
            window.showNormal()
        window.show()
        window.raise_()
        window.activateWindow()

    server.newConnection.connect(activate_window)
    window._single_instance_lock = lock
    window._single_instance_server = server
    window.show()
    return application.exec()


def _activate_existing_instance(
    server_name: str,
    *,
    timeout_ms: int = 250,
) -> bool:
    socket = QLocalSocket()
    socket.connectToServer(server_name)
    if not socket.waitForConnected(timeout_ms):
        return False
    socket.write(b"activate")
    socket.flush()
    socket.waitForBytesWritten(timeout_ms)
    socket.disconnectFromServer()
    return True


def _dark_palette() -> QPalette:
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#071116"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#eefbfa"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#0a171c"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#102126"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#eefbfa"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#15272d"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#eefbfa"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#2b7f75"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#71868a"))
    return palette
