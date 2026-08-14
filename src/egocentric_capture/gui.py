from __future__ import annotations

import shutil
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QLockFile, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtQuick import QQuickWindow, QSGRendererInterface
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .config import output_root
from .models import (
    CameraFrame,
    CaptureRequest,
    CaptureState,
    FinalizeProgress,
    HealthSnapshot,
    PreviewHealth,
    WearableEmgSample,
)
from .preview import GstreamerPreviewWidget
from .session import CaptureEngine, EngineCallbacks


class _Bridge(QObject):
    state = Signal(object, str)
    health = Signal(object)
    session = Signal(object, object)
    finalize_progress = Signal(object)
    shutdown = Signal(object)


class CaptureWindow(QMainWindow):
    """面向采集员的单窗口采集工作台。"""

    CAMERA_NAMES = {
        "cam_a": "相机 A",
        "cam_b": "相机 B",
        "cam_c": "相机 C",
        "cam_d": "相机 D",
    }

    def __init__(self, config: dict[str, Any], *, simulate: bool = False) -> None:
        super().__init__()
        self.config = config
        self.simulate = simulate
        self.bridge = _Bridge()
        self._emg_lock = threading.Lock()
        self._emg_preview_seconds = float(
            config.get("wearable", {}).get("preview_seconds", 5)
        )
        self._emg_plot_interval_ms = max(
            40,
            int(config.get("wearable", {}).get("plot_interval_ms", 100)),
        )
        self._emg_plot_downsample = max(
            1,
            int(config.get("wearable", {}).get("plot_downsample", 4)),
        )
        self._emg_scale_interval_ns = int(
            max(
                0.1,
                float(
                    config.get("wearable", {}).get(
                        "plot_scale_interval_s",
                        0.5,
                    )
                ),
            )
            * 1_000_000_000
        )
        self._emg_plot_spread = 1000.0
        self._last_emg_scale_update_ns = 0
        expected_hz = int(config.get("wearable", {}).get("emg_expected_hz", 250))
        self._emg_values: deque[tuple[int, tuple[int, ...]]] = deque(
            maxlen=max(250, int(self._emg_preview_seconds * expected_hz))
        )
        self._recording_started_wall: float | None = None
        self._last_result_failed = False
        self._last_health: HealthSnapshot | None = None
        self._closing = False
        self._allow_close = False
        self._main_page: QWidget | None = None
        self._finalize_progress_bar: QProgressBar | None = None
        self._finalize_message: QLabel | None = None
        self._last_event_loop_tick = time.monotonic()
        self._event_loop_lag_max_ms = 0.0
        self._last_health_ui_ns = time.monotonic_ns()
        self._emg_received_since_health = 0
        self._plot_duration_last_ms = 0.0
        self._plot_duration_max_ms = 0.0
        self._diagnostics_enabled = bool(
            config.get("preview", {}).get("diagnostics_enabled", False)
        )
        self._emg_diagnostics_enabled = bool(
            config.get("wearable", {}).get("diagnostics_enabled", False)
        )
        self._last_emg_diagnostic_ns = time.monotonic_ns()
        self._build_ui()
        self._connect_signals()
        self.engine = CaptureEngine(
            config,
            simulate=simulate,
            callbacks=EngineCallbacks(
                on_state=lambda state, message: self.bridge.state.emit(state, message),
                on_health=lambda snapshot: self.bridge.health.emit(snapshot),
                on_preview=self._submit_preview_direct,
                on_emg=self._append_emg,
                on_session=lambda path, manifest: self.bridge.session.emit(path, manifest),
                on_finalize_progress=lambda progress: (
                    self.bridge.finalize_progress.emit(progress)
                ),
            ),
        )
        self.preview_widget.health_changed.connect(self._on_preview_health)
        QTimer.singleShot(0, self._publish_initial_preview_health)
        self._ui_timer = QTimer(self)
        self._ui_timer.timeout.connect(self._refresh_periodic)
        self._ui_timer.start(1000)
        self._plot_timer = QTimer(self)
        self._plot_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._plot_timer.timeout.connect(self._refresh_emg_plot)
        self._plot_timer.start(self._emg_plot_interval_ms)
        self._event_loop_timer = QTimer(self)
        self._event_loop_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._event_loop_timer.timeout.connect(self._measure_event_loop_lag)
        self._event_loop_timer.start(100)
        QTimer.singleShot(0, self.preview_widget.start)
        QTimer.singleShot(0, self.engine.start)

    def _build_ui(self) -> None:
        self.setWindowTitle(
            "EgoCentric 多模态采集" + (" · 模拟模式" if self.simulate else "")
        )
        self.resize(1480, 920)
        self.setMinimumSize(1120, 720)
        root = QWidget()
        self._main_page = root
        self.setCentralWidget(root)
        page = QVBoxLayout(root)
        page.setContentsMargins(18, 16, 18, 16)
        page.setSpacing(12)

        page.addWidget(self._build_header())
        body = QHBoxLayout()
        body.setSpacing(14)
        body.addWidget(self._build_preview_area(), 1)
        body.addWidget(self._build_status_sidebar())
        page.addLayout(body, 1)
        page.addWidget(self._build_emg_area())
        page.addWidget(self._build_footer())
        self._apply_style()

    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("header")
        layout = QGridLayout(header)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(6)

        self.participant_edit = QLineEdit()
        self.participant_edit.setPlaceholderText("例如 P001")
        self.participant_edit.setMinimumHeight(38)
        self.task_edit = QLineEdit()
        self.task_edit.setPlaceholderText("例如 grasp_cup")
        self.task_edit.setMinimumHeight(38)
        self.hand_combo = QComboBox()
        self.hand_combo.setMinimumHeight(38)
        self.hand_combo.addItem("右手", "right")
        self.hand_combo.addItem("左手", "left")
        self.hand_combo.addItem("双手", "both")
        self.operator_edit = QLineEdit()
        self.operator_edit.setPlaceholderText("采集员姓名")
        self.operator_edit.setMinimumHeight(38)
        self.notes_edit = QPlainTextEdit()
        self.notes_edit.setPlaceholderText("可选备注")
        self.notes_edit.setFixedHeight(54)

        fields = (
            ("受试者", self.participant_edit),
            ("任务", self.task_edit),
            ("采集手", self.hand_combo),
            ("操作人", self.operator_edit),
        )
        for column, (label, widget) in enumerate(fields):
            title = QLabel(label)
            title.setObjectName("fieldLabel")
            layout.addWidget(title, 0, column)
            layout.addWidget(widget, 1, column)
        notes_label = QLabel("备注")
        notes_label.setObjectName("fieldLabel")
        layout.addWidget(notes_label, 0, 4)
        layout.addWidget(self.notes_edit, 1, 4)

        self.advanced_button = QPushButton("高级设置")
        self.advanced_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView)
        )
        self.advanced_button.clicked.connect(self._show_advanced)
        layout.addWidget(self.advanced_button, 1, 5)
        layout.setColumnStretch(0, 2)
        layout.setColumnStretch(1, 2)
        layout.setColumnStretch(4, 3)
        return header

    def _build_preview_area(self) -> QWidget:
        area = QFrame()
        area.setObjectName("previewArea")
        layout = QVBoxLayout(area)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        toolbar = QHBoxLayout()
        title = QLabel("视频预览")
        title.setObjectName("sectionTitle")
        toolbar.addWidget(title)
        toolbar.addStretch()
        self.camera_combo = QComboBox()
        for key, name in self.CAMERA_NAMES.items():
            self.camera_combo.addItem(name, key)
        self.camera_combo.currentIndexChanged.connect(self._apply_preview_mode)
        toolbar.addWidget(self.camera_combo)
        self.single_button = QToolButton()
        self.single_button.setText("单路")
        self.single_button.setCheckable(True)
        self.single_button.clicked.connect(lambda: self._set_preview_mode(True))
        self.grid_button = QToolButton()
        self.grid_button.setText("四宫格")
        self.grid_button.setCheckable(True)
        self.grid_button.setChecked(True)
        self.grid_button.clicked.connect(lambda: self._set_preview_mode(False))
        toolbar.addWidget(self.single_button)
        toolbar.addWidget(self.grid_button)
        layout.addLayout(toolbar)

        self.preview_widget = GstreamerPreviewWidget(
            dict(self.config.get("preview") or {}),
        )
        self.preview_widget.setMinimumSize(560, 360)
        self.preview_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        layout.addWidget(self.preview_widget, 1)
        return area

    def _build_status_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(340)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        title = QLabel("设备状态")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        self.status_rows: dict[str, tuple[QLabel, QLabel]] = {}
        rows = [
            ("camera/cam_a", "相机 A"),
            ("camera/cam_b", "相机 B"),
            ("camera/cam_c", "相机 C"),
            ("camera/cam_d", "相机 D"),
            ("imu/oak", "OAK IMU"),
            ("wearable/emg", "8 通道 EMG"),
            ("wearable/imu", "手环 IMU"),
        ]
        for key, name in rows:
            row = QWidget()
            row.setFixedHeight(38)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)
            dot = QLabel()
            dot.setFixedSize(10, 10)
            dot.setProperty("health", "red")
            label = QLabel(name)
            detail = QLabel("--")
            detail.setObjectName("muted")
            detail.setAlignment(Qt.AlignmentFlag.AlignRight)
            row_layout.addWidget(dot)
            row_layout.addWidget(label)
            row_layout.addStretch()
            row_layout.addWidget(detail)
            layout.addWidget(row)
            self.status_rows[key] = (dot, detail)
        layout.addSpacing(8)
        gap_title = QLabel("序号异常")
        gap_title.setObjectName("fieldLabel")
        self.gap_label = QLabel("0")
        self.gap_label.setObjectName("metric")
        layout.addWidget(gap_title)
        layout.addWidget(self.gap_label)
        queue_title = QLabel("写盘队列")
        queue_title.setObjectName("fieldLabel")
        self.queue_label = QLabel("0")
        self.queue_label.setObjectName("metric")
        layout.addWidget(queue_title)
        layout.addWidget(self.queue_label)
        self.count_label = QLabel("接收 -- / 持久化 --")
        self.count_label.setObjectName("muted")
        self.count_label.setWordWrap(True)
        layout.addWidget(self.count_label)
        self.preview_health_label = QLabel("预览 --")
        self.preview_health_label.setObjectName("muted")
        self.preview_health_label.setWordWrap(True)
        layout.addWidget(self.preview_health_label)
        self.loop_lag_label = QLabel("事件循环 --")
        self.loop_lag_label.setObjectName("muted")
        layout.addWidget(self.loop_lag_label)
        self.reconnect_label = QLabel("重连 OAK 0 / 手环 0")
        self.reconnect_label.setObjectName("muted")
        layout.addWidget(self.reconnect_label)
        layout.addStretch()
        return sidebar

    def _build_emg_area(self) -> QWidget:
        area = QFrame()
        area.setObjectName("emgArea")
        layout = QVBoxLayout(area)
        layout.setContentsMargins(10, 8, 10, 8)
        self.emg_toggle = QToolButton()
        self.emg_toggle.setText("原始 EMG 波形")
        self.emg_toggle.setCheckable(True)
        self.emg_toggle.setChecked(True)
        self.emg_toggle.setArrowType(Qt.ArrowType.DownArrow)
        self.emg_toggle.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.emg_toggle.clicked.connect(self._toggle_emg)
        layout.addWidget(self.emg_toggle)
        pg.setConfigOption("background", "#171b1f")
        pg.setConfigOption("foreground", "#9aa4ad")
        self.emg_plot = pg.PlotWidget()
        self.emg_plot.setFixedHeight(190)
        self.emg_plot.showGrid(x=True, y=True, alpha=0.16)
        self.emg_plot.setMouseEnabled(x=False, y=False)
        self.emg_plot.hideButtons()
        colors = (
            "#55c2a3",
            "#68a8e8",
            "#e6b85c",
            "#d87878",
            "#9f86d9",
            "#64c3d7",
            "#c4cb65",
            "#de8eb8",
        )
        self.emg_curves = [
            self.emg_plot.plot(pen=pg.mkPen(color, width=1.2))
            for color in colors
        ]
        for curve in self.emg_curves:
            curve.setClipToView(True)
            curve.setDownsampling(
                ds=self._emg_plot_downsample,
                auto=False,
                method="peak",
            )
            curve.setSkipFiniteCheck(True)
        self.emg_plot.setXRange(
            -self._emg_preview_seconds,
            0,
            padding=0,
        )
        self.emg_plot.setYRange(
            -self._emg_plot_spread,
            self._emg_plot_spread * 8,
            padding=0.02,
        )
        layout.addWidget(self.emg_plot)
        return area

    def _build_footer(self) -> QWidget:
        footer = QFrame()
        footer.setObjectName("footer")
        layout = QGridLayout(footer)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setHorizontalSpacing(18)
        self.record_button = QPushButton("设备检查中")
        self.record_button.setObjectName("recordButton")
        self.record_button.setMinimumSize(210, 52)
        self.record_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
        )
        self.record_button.setEnabled(False)
        self.record_button.clicked.connect(self._record_clicked)
        layout.addWidget(self.record_button, 0, 0, 2, 1)

        self.state_label = QLabel("正在启动设备")
        self.state_label.setObjectName("stateLabel")
        self.duration_label = QLabel("00:00:00")
        self.duration_label.setObjectName("duration")
        layout.addWidget(self.state_label, 0, 1)
        layout.addWidget(self.duration_label, 1, 1)

        self.trial_label = QLabel("r--")
        self.space_label = QLabel("-- GiB")
        self.estimate_label = QLabel("--")
        for column, (title, value) in enumerate(
            (
                ("当前轮次", self.trial_label),
                ("剩余空间", self.space_label),
                ("估算可录", self.estimate_label),
            ),
            start=2,
        ):
            label = QLabel(title)
            label.setObjectName("fieldLabel")
            value.setObjectName("metric")
            layout.addWidget(label, 0, column)
            layout.addWidget(value, 1, column)

        self.output_label = QLabel(str(output_root(self.config)))
        self.output_label.setObjectName("path")
        self.output_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.output_label, 0, 5)
        self.quality_label = QLabel("等待采集")
        self.quality_label.setObjectName("quality")
        layout.addWidget(self.quality_label, 1, 5)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(5, 3)
        return footer

    def _connect_signals(self) -> None:
        self.bridge.state.connect(self._on_state)
        self.bridge.health.connect(self._on_health)
        self.bridge.session.connect(self._on_session)
        self.bridge.finalize_progress.connect(self._on_finalize_progress)
        self.bridge.shutdown.connect(self._on_shutdown_complete)
        self.participant_edit.textChanged.connect(self._refresh_trial)
        self.task_edit.textChanged.connect(self._refresh_trial)

    def _record_clicked(self) -> None:
        if self.engine.state in {CaptureState.ARMING, CaptureState.RECORDING}:
            self.record_button.setEnabled(False)
            self.engine.request_stop()
            return
        if self.engine.state != CaptureState.READY:
            return
        participant = self.participant_edit.text().strip()
        task = self.task_edit.text().strip()
        operator = self.operator_edit.text().strip()
        missing = [
            label
            for label, value in (
                ("受试者", participant),
                ("任务", task),
                ("操作人", operator),
            )
            if not value
        ]
        if missing:
            QMessageBox.warning(self, "信息不完整", "请填写：" + "、".join(missing))
            return
        request = CaptureRequest(
            participant_id=participant,
            task_id=task,
            hand=str(self.hand_combo.currentData()),
            operator=operator,
            notes=self.notes_edit.toPlainText().strip(),
        )
        try:
            self.engine.request_record(request)
        except Exception as exc:
            QMessageBox.critical(self, "无法开始录制", str(exc))

    def _on_state(self, state: CaptureState, message: str) -> None:
        self.state_label.setText(message)
        self.state_label.setProperty("state", state.value.lower())
        self._repolish(self.state_label)
        if state == CaptureState.READY:
            self.record_button.setEnabled(True)
            self.record_button.setText(
                "重采本轮" if self._last_result_failed else "开始录制"
            )
            self.record_button.setIcon(
                self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
            )
        elif state in {CaptureState.ARMING, CaptureState.RECORDING}:
            if state == CaptureState.RECORDING and self._recording_started_wall is None:
                self._recording_started_wall = time.monotonic()
            self.record_button.setEnabled(True)
            self.record_button.setText("停止录制")
            self.record_button.setIcon(
                self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop)
            )
            self.quality_label.setText("录制中，等待最终质检")
            self.quality_label.setProperty("result", "pending")
            self._repolish(self.quality_label)
        elif state == CaptureState.FINALIZING:
            self.record_button.setEnabled(False)
            self.record_button.setText("正在收尾")
        elif state in {CaptureState.COMPLETED, CaptureState.FAILED}:
            self.record_button.setEnabled(False)
            self._recording_started_wall = None
            self._last_result_failed = state == CaptureState.FAILED
            if not self._closing:
                QTimer.singleShot(1200, self.engine.prepare_next)
        else:
            self.record_button.setEnabled(False)
            self.record_button.setText("设备检查中")

    def _on_health(self, snapshot: HealthSnapshot) -> None:
        health_ui_ns = time.monotonic_ns()
        health_interval_s = (
            health_ui_ns - self._last_health_ui_ns
        ) / 1_000_000_000
        self._last_health_ui_ns = health_ui_ns
        with self._emg_lock:
            emg_received = self._emg_received_since_health
            self._emg_received_since_health = 0
        if self._diagnostics_enabled or self._emg_diagnostics_enabled:
            camera_rates = ",".join(
                f"{name}={snapshot.rates_hz.get(f'camera/{name}', 0.0):.1f}"
                for name in ("cam_a", "cam_b", "cam_c", "cam_d")
            )
            print(
                "[ui-diag] "
                f"健康回调间隔={health_interval_s:.3f}s,"
                f"事件循环最大延迟={self._event_loop_lag_max_ms:.1f}ms,"
                f"EMG到达={emg_received / max(health_interval_s, 0.001):.1f}Hz,"
                f"绘图耗时={self._plot_duration_last_ms:.1f}"
                f"/{self._plot_duration_max_ms:.1f}ms,"
                f"相机={camera_rates}",
                flush=True,
            )
        self._event_loop_lag_max_ms = 0.0
        self._plot_duration_max_ms = 0.0
        self._last_health = snapshot
        for key, (dot, detail) in self.status_rows.items():
            hz = float(snapshot.rates_hz.get(key, 0.0))
            age_s = float(snapshot.last_seen_age_s.get(key, float("inf")))
            minimum = self._minimum_rate(key)
            if age_s > 1.0:
                level = "red"
            elif hz < minimum:
                level = "yellow"
            else:
                level = "green"
            self._set_dynamic_property(dot, "health", level)
            age_text = (
                "无数据"
                if not np.isfinite(age_s)
                else f"{age_s * 1000:.0f} ms"
            )
            detail.setText(f"{hz:.1f} Hz · {age_text}")
        gaps = sum(int(value) for value in snapshot.sequence_gaps.values())
        self.gap_label.setText(str(gaps))
        self._set_dynamic_property(self.gap_label, "alert", gaps > 0)
        self.queue_label.setText(
            f"{snapshot.queue_depth} / 峰值 {snapshot.writer_high_watermark}"
        )
        received = sum(snapshot.received_counts.values())
        persisted = sum(snapshot.persisted_counts.values())
        self.count_label.setText(
            f"接收 {received} / 持久化 {persisted} / 丢弃 {snapshot.queue_drops}"
        )
        submitted = sum(snapshot.preview_submitted_counts.values())
        rendered = sum(snapshot.preview_rendered_counts.values())
        max_latency = max(snapshot.preview_latency_ms.values(), default=0.0)
        self.preview_health_label.setText(
            f"预览 {submitted}/{rendered} · 延迟 {max_latency:.1f} ms · "
            f"四路差 {snapshot.preview_cross_camera_skew_ms:.1f} ms"
        )
        self.loop_lag_label.setText(
            f"事件循环延迟 {snapshot.event_loop_lag_ms:.1f} ms"
        )
        self.reconnect_label.setText(
            "重连 OAK "
            f"{snapshot.source_reconnects.get('OakSource', 0)} / 手环 "
            f"{snapshot.source_reconnects.get('WearableSource', 0)}"
        )

    def _submit_preview_direct(self, frame: CameraFrame) -> None:
        backend = self.preview_widget.backend
        if backend is not None:
            backend.submit(frame)

    def _on_preview_health(self, health: PreviewHealth) -> None:
        self.preview_widget.set_health(health)
        self.engine.update_preview_health(health)

    def _publish_initial_preview_health(self) -> None:
        for health in self.preview_widget.health().values():
            self._on_preview_health(health)

    def _on_session(self, path: Path | None, manifest: dict[str, Any] | None) -> None:
        if path is not None:
            self.output_label.setText(str(path))
        if not manifest:
            return
        status = manifest.get("status")
        quality = manifest.get("quality") or {}
        if status == "completed":
            sync = quality.get("camera_sync") or {}
            self.quality_label.setText(
                "质检通过 · "
                f"同步 p95 {float(sync.get('p95_ms', 0)):.3f} ms · 零丢弃"
            )
            self.quality_label.setProperty("result", "pass")
        elif status == "failed":
            reason = str(manifest.get("failure_reason") or "未通过质检")
            self.quality_label.setText("质检失败 · " + reason)
            self.quality_label.setProperty("result", "fail")
        self._repolish(self.quality_label)
        self._refresh_trial()

    def _append_emg(self, sample: WearableEmgSample) -> None:
        with self._emg_lock:
            self._emg_values.append(
                (sample.stamp.monotonic_ns, sample.channels_uv)
            )
            self._emg_received_since_health += 1

    def _refresh_emg_plot(self) -> None:
        started_ns = time.monotonic_ns()
        if not self.emg_plot.isVisible():
            return
        with self._emg_lock:
            samples = tuple(self._emg_values)
        if not samples:
            return
        timestamps_ns = np.fromiter(
            (sample[0] for sample in samples),
            dtype=np.int64,
            count=len(samples),
        )
        values = np.asarray(
            [sample[1] for sample in samples],
            dtype=np.float64,
        )
        if values.size == 0:
            return
        now_ns = time.monotonic_ns()
        time_axis_s = (timestamps_ns - now_ns) / 1_000_000_000
        if (
            self._emg_diagnostics_enabled
            and now_ns - self._last_emg_diagnostic_ns >= 1_000_000_000
        ):
            newest_age_ms = max(0.0, (now_ns - timestamps_ns[-1]) / 1_000_000)
            oldest_age_ms = max(0.0, (now_ns - timestamps_ns[0]) / 1_000_000)
            print(
                "[emg-ui-diag] "
                f"最新样本年龄={newest_age_ms:.2f}ms,"
                f"窗口={oldest_age_ms:.1f}ms,"
                f"样本数={len(samples)},"
                f"绘图周期={self._emg_plot_interval_ms}ms",
                flush=True,
            )
            self._last_emg_diagnostic_ns = now_ns
        candidate_spread = max(
            1000.0,
            float(np.nanpercentile(np.abs(values), 98)) * 2.5,
        )
        scale_changed = False
        if candidate_spread > self._emg_plot_spread * 1.1:
            self._emg_plot_spread = candidate_spread
            self._last_emg_scale_update_ns = now_ns
            scale_changed = True
        elif (
            now_ns - self._last_emg_scale_update_ns
            >= self._emg_scale_interval_ns
        ):
            self._emg_plot_spread = max(
                candidate_spread,
                self._emg_plot_spread * 0.8,
            )
            self._last_emg_scale_update_ns = now_ns
            scale_changed = True
        spread = self._emg_plot_spread
        offsets = np.arange(8, dtype=np.float64) * spread
        for index, curve in enumerate(self.emg_curves):
            curve.setData(
                time_axis_s,
                values[:, index] + offsets[index],
            )
        if scale_changed:
            self.emg_plot.setYRange(-spread, spread * 8, padding=0.02)
        duration_ms = (time.monotonic_ns() - started_ns) / 1_000_000
        self._plot_duration_last_ms = duration_ms
        self._plot_duration_max_ms = max(
            self._plot_duration_max_ms,
            duration_ms,
        )

    def _refresh_periodic(self) -> None:
        root = output_root(self.config)
        candidate = root
        while not candidate.exists() and candidate != candidate.parent:
            candidate = candidate.parent
        free = shutil.disk_usage(candidate).free
        self.space_label.setText(f"{free / 1024**3:.1f} GiB")
        bytes_per_second = 4 * 12_000_000 / 8 * 1.12
        seconds = free / bytes_per_second
        self.estimate_label.setText(_duration_text(seconds))
        if self._recording_started_wall is not None:
            elapsed = time.monotonic() - self._recording_started_wall
            self.duration_label.setText(_clock_text(elapsed))
        else:
            self.duration_label.setText("00:00:00")

    def _refresh_trial(self) -> None:
        participant = self.participant_edit.text().strip()
        task = self.task_edit.text().strip()
        if not participant or not task:
            self.trial_label.setText("r--")
            return
        if not hasattr(self, "engine"):
            return
        trial = self.engine.next_trial(participant, task)
        self.trial_label.setText(f"r{trial:02d}")

    def _set_preview_mode(self, single: bool) -> None:
        self.single_button.setChecked(single)
        self.grid_button.setChecked(not single)
        self.camera_combo.setEnabled(single)
        self._apply_preview_mode()

    def _apply_preview_mode(self) -> None:
        single = self.single_button.isChecked()
        selected = str(self.camera_combo.currentData())
        self.preview_widget.set_mode(single, selected)

    def _measure_event_loop_lag(self) -> None:
        now = time.monotonic()
        lag_ms = max(0.0, (now - self._last_event_loop_tick - 0.1) * 1000)
        self._last_event_loop_tick = now
        self._event_loop_lag_max_ms = max(
            self._event_loop_lag_max_ms,
            lag_ms,
        )
        self.engine.update_event_loop_lag(lag_ms)

    def _toggle_emg(self, checked: bool) -> None:
        self.emg_plot.setVisible(checked)
        self.emg_toggle.setArrowType(
            Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow
        )

    def _show_advanced(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("高级设备设置")
        form = QFormLayout(dialog)
        port = QLineEdit(str(self.config.get("wearable", {}).get("port", "auto")))
        baudrate = QSpinBox()
        baudrate.setRange(9600, 4_000_000)
        baudrate.setSingleStep(115200)
        baudrate.setValue(
            int(self.config.get("wearable", {}).get("baudrate", 921600))
        )
        queue_size = QSpinBox()
        queue_size.setRange(1000, 200000)
        queue_size.setValue(
            int(self.config.get("session", {}).get("writer_queue_size", 20000))
        )
        form.addRow("手环串口", port)
        form.addRow("波特率", baudrate)
        form.addRow("写盘队列", queue_size)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            if self.engine.state != CaptureState.DISCONNECTED:
                QMessageBox.information(
                    self,
                    "下次启动生效",
                    "设备参数已更新，重新启动应用后生效。",
                )
            self.config.setdefault("wearable", {})["port"] = port.text().strip() or "auto"
            self.config["wearable"]["baudrate"] = baudrate.value()
            self.config.setdefault("session", {})[
                "writer_queue_size"
            ] = queue_size.value()

    def _minimum_rate(self, key: str) -> float:
        if key.startswith("camera/"):
            return float(self.config.get("oak", {}).get("minimum_fps", 24))
        if key == "imu/oak":
            return float(
                self.config.get("oak", {}).get("imu", {}).get("minimum_hz", 80)
            )
        if key == "wearable/emg":
            return float(
                self.config.get("wearable", {}).get("emg_minimum_hz", 225)
            )
        return 1.0

    @staticmethod
    def _repolish(widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _set_dynamic_property(
        self,
        widget: QWidget,
        name: str,
        value: Any,
    ) -> None:
        if widget.property(name) == value:
            return
        widget.setProperty(name, value)
        self._repolish(widget)

    def _show_finalize_page(self) -> None:
        previous = self.takeCentralWidget()
        if previous is not None:
            previous.hide()
            self._main_page = previous
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(120, 80, 120, 80)
        layout.addStretch()
        title = QLabel("正在安全收尾")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size:24px;font-weight:700;")
        layout.addWidget(title)
        message = QLabel("正在停止新数据并等待已接收帧持久化")
        message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        message.setWordWrap(True)
        message.setStyleSheet("color:#aeb8c0;")
        layout.addWidget(message)
        progress = QProgressBar()
        progress.setRange(0, 0)
        progress.setTextVisible(True)
        layout.addWidget(progress)
        layout.addStretch()
        self._finalize_message = message
        self._finalize_progress_bar = progress
        self.setCentralWidget(page)

    def _on_finalize_progress(self, progress: FinalizeProgress) -> None:
        if self._finalize_message is not None:
            self._finalize_message.setText(progress.message)
        if self._finalize_progress_bar is not None:
            total = max(1, progress.total)
            self._finalize_progress_bar.setRange(0, total)
            self._finalize_progress_bar.setValue(
                min(total, progress.completed)
            )
            self._finalize_progress_bar.setFormat(
                f"{progress.stage}  %v/%m"
            )

    def _on_shutdown_complete(self, error: str | None) -> None:
        self._allow_close = True
        if self._finalize_message is not None:
            self._finalize_message.setText(
                f"关闭失败：{error}" if error else "收尾完成，正在退出"
            )
        QTimer.singleShot(
            1500 if error else 100,
            QApplication.instance().quit,
        )

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow {
                background: #111417;
            }
            QWidget {
                color: #eef1f3;
                font-size: 14px;
            }
            QFrame#header, QFrame#footer {
                background: #1b2025;
                border: 1px solid #303840;
                border-radius: 6px;
            }
            QFrame#sidebar {
                background: #181d21;
                border-left: 1px solid #303840;
            }
            QFrame#previewArea, QFrame#emgArea {
                background: transparent;
            }
            QLabel#sectionTitle {
                font-size: 17px;
                font-weight: 700;
            }
            QLabel#fieldLabel, QLabel#muted {
                color: #97a2ab;
                font-size: 12px;
            }
            QLabel#videoTile {
                background: #080a0c;
                border: 1px solid #303840;
                color: #77828b;
                font-size: 16px;
            }
            QLineEdit, QComboBox, QPlainTextEdit, QSpinBox {
                background: #101417;
                border: 1px solid #3a444d;
                border-radius: 4px;
                padding: 7px 9px;
                selection-background-color: #2f8f79;
            }
            QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {
                border-color: #55c2a3;
            }
            QPushButton, QToolButton {
                background: #252c32;
                border: 1px solid #3a444d;
                border-radius: 4px;
                padding: 7px 12px;
            }
            QPushButton:hover, QToolButton:hover {
                background: #303941;
            }
            QToolButton:checked {
                background: #245f54;
                border-color: #55c2a3;
            }
            QPushButton#recordButton {
                background: #237b67;
                border-color: #55c2a3;
                font-size: 17px;
                font-weight: 700;
            }
            QPushButton#recordButton:hover {
                background: #2b9079;
            }
            QPushButton#recordButton:disabled {
                background: #272d32;
                border-color: #3a444d;
                color: #7f8991;
            }
            QLabel#duration {
                font-size: 25px;
                font-weight: 700;
            }
            QLabel#metric {
                font-size: 18px;
                font-weight: 650;
            }
            QLabel#path {
                color: #aeb8c0;
            }
            QLabel#quality[result="pass"] {
                color: #63d2aa;
            }
            QLabel#quality[result="fail"], QLabel#metric[alert="true"] {
                color: #ef8585;
            }
            QLabel#quality[result="pending"] {
                color: #e6b85c;
            }
            QLabel[health="green"] {
                background: #4bd09e;
                border-radius: 5px;
            }
            QLabel[health="yellow"] {
                background: #e4b44f;
                border-radius: 5px;
            }
            QLabel[health="red"] {
                background: #df6666;
                border-radius: 5px;
            }
            """
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._allow_close:
            event.accept()
            return
        if self._closing:
            event.ignore()
            return
        if self.engine.recording:
            choice = QMessageBox.question(
                self,
                "正在录制",
                "关闭应用会将当前轮次标记为失败，确定关闭吗？",
            )
            if choice != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self._closing = True
        self._show_finalize_page()
        self._ui_timer.stop()
        self._plot_timer.stop()
        self._event_loop_timer.stop()

        def engine_stopped(error: str | None) -> None:
            preview_error: str | None = None
            try:
                self.preview_widget.stop()
            except Exception as exc:
                preview_error = f"{type(exc).__name__}: {exc}"
            combined = error or preview_error
            self.bridge.shutdown.emit(combined)

        self.engine.shutdown_async(engine_stopped)
        event.ignore()


def _clock_text(seconds: float) -> str:
    value = max(0, int(seconds))
    hours, remainder = divmod(value, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _duration_text(seconds: float) -> str:
    if seconds < 3600:
        return f"{seconds / 60:.0f} 分钟"
    return f"{seconds / 3600:.1f} 小时"


def run_gui(config: dict[str, Any], *, simulate: bool = False) -> int:
    application = QApplication.instance() or QApplication(sys.argv)
    application.setApplicationName("EgoCentric Capture")
    QQuickWindow.setGraphicsApi(
        QSGRendererInterface.GraphicsApi.OpenGL
    )
    server_name = f"egocentric-capture-{__import__('os').getuid()}"
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
