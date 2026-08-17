from __future__ import annotations

import ctypes
import ctypes.util
import os
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any, Callable

import numpy as np
import shiboken6
from PySide6.QtCore import QObject, QRunnable, Signal
from PySide6.QtQuick import QQuickItem, QQuickView, QQuickWindow

from ..models import CameraFrame, PreviewHealth

HARDWARE_DECODER_CANDIDATES = (
    "vah264dec",
    "vaapih264dec",
    "v4l2slh264dec",
    "v4l2h264dec",
    "nvh264dec",
)
SOFTWARE_DECODER = "avdec_h264"
_QML_TYPE_REGISTRATION_SINK: Any = None


class PreviewBackendError(RuntimeError):
    """GStreamer Qt6 预览后端不可用。"""


def initialize_gstreamer_qml() -> None:
    global _QML_TYPE_REGISTRATION_SINK

    _ensure_system_typelib_path()
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
    except (ImportError, ValueError) as exc:
        raise PreviewBackendError(
            "缺少 PyGObject/GStreamer GI 绑定，无法注册 QML 视频类型"
        ) from exc
    Gst.init(None)
    if Gst.ElementFactory.find("qml6glsink") is None:
        raise PreviewBackendError(
            "缺少 qml6glsink，请安装 gstreamer1.0-qt6"
        )
    if _QML_TYPE_REGISTRATION_SINK is None:
        _QML_TYPE_REGISTRATION_SINK = Gst.ElementFactory.make(
            "qml6glsink",
            "qml-type-registration-sink",
        )
    if _QML_TYPE_REGISTRATION_SINK is None:
        raise PreviewBackendError("qml6glsink 无法注册 QML 视频类型")


def decoder_candidates(preference: str) -> tuple[str, ...]:
    normalized = preference.strip().lower()
    if normalized == "auto":
        return (*HARDWARE_DECODER_CANDIDATES, SOFTWARE_DECODER)
    if normalized in {"software", SOFTWARE_DECODER}:
        return (SOFTWARE_DECODER,)
    return (normalized,)


def configure_latest_frame_queue(
    element: Any,
    enabled: bool,
    max_buffers: int = 1,
) -> None:
    element.set_property("leaky", 2 if enabled else 0)
    element.set_property(
        "max-size-buffers",
        max(1, max_buffers) if enabled else 0,
    )
    element.set_property("max-size-bytes", 0)
    element.set_property("max-size-time", 0)


def configure_appsrc_queue(
    element: Any,
    latest_frame_only: bool,
    max_buffers: int,
) -> None:
    element.set_property("block", False)
    element.set_property("max-buffers", max(1, max_buffers))
    element.set_property("max-bytes", 0)
    element.set_property("max-time", 0)
    element.set_property("leaky-type", 2 if latest_frame_only else 0)


def configure_preview_sink(
    element: Any,
    latest_frame_only: bool,
    _display_fps: float,
) -> None:
    element.set_property("sync", not latest_frame_only)
    element.set_property("max-lateness", 0 if latest_frame_only else -1)
    element.set_property("qos", False)
    element.set_property("enable-last-sample", False)
    element.set_property("throttle-time", 0)


def native_video_caps(width: int, height: int, fps: int) -> str:
    return (
        "video/x-raw,"
        f"width={max(1, width)},"
        f"height={max(1, height)},"
        f"framerate={max(1, fps)}/1"
    )


def cockpit_layout(
    width: int,
    height: int,
    primary_camera: str = "cam_a",
) -> dict[str, dict[str, int | float]]:
    cameras = ("cam_a", "cam_b", "cam_c", "cam_d")
    thumbnail_width = max(120, int(width * 0.12))
    thumbnail_height = max(68, int(thumbnail_width * 9 / 16))
    gap = max(6, int(width * 0.006))
    auxiliaries = [camera for camera in cameras if camera != primary_camera]
    total_width = len(auxiliaries) * thumbnail_width + max(
        0,
        len(auxiliaries) - 1,
    ) * gap
    start_x = (width - total_width) // 2
    y = height - thumbnail_height - max(14, int(height * 0.025))
    layout: dict[str, dict[str, int | float]] = {
        primary_camera: {
            "alpha": 1.0,
            "x": 0,
            "y": 0,
            "width": width,
            "height": height,
            "z": 0,
        }
    }
    for index, camera in enumerate(auxiliaries):
        layout[camera] = {
            "alpha": 1.0,
            "x": start_x + index * (thumbnail_width + gap),
            "y": y,
            "width": thumbnail_width,
            "height": thumbnail_height,
            "z": index + 1,
        }
    return layout


@dataclass(slots=True)
class _Branch:
    camera: str
    bin: Any
    appsrc: Any
    decoded_queue: Any
    mixer_pad: Any = None


@dataclass(slots=True)
class _PreviewCounters:
    submitted: int = 0
    rendered: int = 0
    last_submitted_sequence: int | None = None
    last_rendered_sequence: int | None = None
    latency_ms: float = 0.0
    healthy: bool = True
    message: str = ""
    suspended: bool = False
    backlog_origin_submitted: int = 0
    backlog_origin_rendered: int = 0
    parsed: int = 0
    decoded: int = 0
    sink_arrived: int = 0
    stats_last_rendered: int = 0
    stats_last_dropped: int = 0
    stats_last_submitted: int = 0
    stats_last_parsed: int = 0
    stats_last_decoded: int = 0
    stats_last_sink_arrived: int = 0
    stats_last_poll_ns: int = 0
    pending_frames: deque[tuple[int, int]] = dataclass_field(
        default_factory=deque
    )
    rendered_source_monotonic_ns: int | None = None
    last_pipeline_pts_ns: int = -1


class _StartPipelineJob(QRunnable):
    def __init__(self, backend: _GstreamerBackend) -> None:
        super().__init__()
        self.backend = backend

    def run(self) -> None:
        try:
            self.backend.start()
        except Exception as exc:
            self.backend.report_start_failure(
                f"{type(exc).__name__}: {exc}"
            )


class _GstreamerBackend:
    CAMERAS = ("cam_a", "cam_b", "cam_c", "cam_d")

    def __init__(
        self,
        config: dict[str, Any],
        health_callback: Callable[[PreviewHealth], None],
        analysis_callback: Callable[[np.ndarray, int], None] | None = None,
    ) -> None:
        self.config = config
        self.health_callback = health_callback
        self.analysis_callback = analysis_callback
        self.analysis_camera = str(config.get("analysis_camera", "cam_a"))
        self.analysis_width = max(160, int(config.get("analysis_width", 640)))
        self.analysis_height = max(90, int(config.get("analysis_height", 360)))
        self.analysis_fps = max(1, int(config.get("analysis_fps", 12)))
        self.source_width = max(1, int(config.get("source_width", 1920)))
        self.source_height = max(1, int(config.get("source_height", 1080)))
        self.source_fps = max(1, int(config.get("source_fps", 30)))
        self.max_latency_ms = float(config.get("max_latency_ms", 250))
        self.software_threads = int(config.get("software_decoder_threads", 2))
        self.failure_policy = str(
            config.get("failure_policy", "warn_continue")
        )
        self.monitoring_enabled = bool(
            config.get("monitoring_enabled", True)
        )
        self.monitor_interval_s = max(
            0.1,
            float(config.get("monitor_interval_s", 1.0)),
        )
        self.latest_frame_only = bool(
            config.get("latest_frame_only", True)
        )
        self.diagnostics_enabled = bool(
            config.get("diagnostics_enabled", False)
        )
        self.preview_fps = max(1, int(config.get("fixed_fps", 30)))
        self.display_fps = max(
            0.0,
            float(config.get("display_fps", 15)),
        )
        self.decoder_preference = str(config.get("decoder", "auto"))
        self._Gst = self._load_gstreamer()
        self._Gst.init(None)
        self.decoder_name = self._probe_decoder()
        self.pipeline = self._Gst.Pipeline.new("egocentric-preview")
        if self.pipeline is None:
            raise PreviewBackendError("无法创建 GStreamer Pipeline")
        self.mosaic_width = max(320, int(config.get("mosaic_width", 1280)))
        self.mosaic_height = max(180, int(config.get("mosaic_height", 720)))
        self.mixer_latency_ms = max(
            0.0,
            float(config.get("mixer_latency_ms", 50)),
        )
        self.preview_jitter_ms = max(
            0.0,
            float(config.get("preview_jitter_ms", 80)),
        )
        self.decoded_queue_frames = max(
            1,
            int(config.get("decoded_queue_frames", 4)),
        )
        self.mixer: Any = None
        self.sink: Any = None
        self._sink_stats_last_rendered = 0
        self._sink_stats_last_dropped = 0
        self.branches: dict[str, _Branch] = {}
        self._counters = {
            camera: _PreviewCounters() for camera in self.CAMERAS
        }
        self._counter_lock = threading.Lock()
        self.input_queue_frames = max(
            1,
            int(config.get("input_queue_frames", 90)),
        )
        self.appsrc_queue_frames = max(
            1,
            int(config.get("appsrc_queue_frames", 8)),
        )
        self._queues = {
            camera: queue.Queue[CameraFrame](
                maxsize=self.input_queue_frames
            )
            for camera in self.CAMERAS
        }
        self._awaiting_keyframe = {
            camera: True for camera in self.CAMERAS
        }
        self._stop_event = threading.Event()
        self._draining_event = threading.Event()
        self._workers: dict[str, threading.Thread] = {}
        self._bus_thread: threading.Thread | None = None
        self._origin_ns = time.monotonic_ns()
        self._source_pts_origins_ns: dict[str, int | None] = {
            camera: None for camera in self.CAMERAS
        }
        self._pipeline_pts_origin_ns: int | None = None
        self._frame_metadata: dict[
            str,
            dict[int, tuple[int, int]],
        ] = {
            camera: {} for camera in self.CAMERAS
        }
        self._build_pipeline()

    @staticmethod
    def _load_gstreamer() -> Any:
        _ensure_system_typelib_path()
        try:
            import gi

            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
        except (ImportError, ValueError) as exc:
            raise PreviewBackendError(
                "缺少 PyGObject/GStreamer GI 绑定，录制可继续但预览不可用"
            ) from exc
        return Gst

    def _probe_decoder(self) -> str:
        Gst = self._Gst
        if Gst.ElementFactory.find("qml6glsink") is None:
            raise PreviewBackendError(
                "缺少 qml6glsink，请安装 gstreamer1.0-qt6"
            )
        for required in (
            "h264parse",
            "glupload",
            "glcolorconvert",
            "glvideomixer",
        ):
            if Gst.ElementFactory.find(required) is None:
                raise PreviewBackendError(f"缺少 GStreamer 元素: {required}")
        if self.analysis_callback is not None:
            for required in (
                "tee",
                "videoconvert",
                "videoscale",
                "videorate",
                "appsink",
            ):
                if Gst.ElementFactory.find(required) is None:
                    raise PreviewBackendError(
                        f"手势分析缺少 GStreamer 元素: {required}"
                    )
        candidates = decoder_candidates(self.decoder_preference)
        for index, name in enumerate(candidates):
            if Gst.ElementFactory.find(name) is None:
                continue
            probe = Gst.ElementFactory.make(name, f"decoder-probe-{index}")
            if probe is None:
                continue
            result = probe.set_state(Gst.State.READY)
            probe.set_state(Gst.State.NULL)
            if result != Gst.StateChangeReturn.FAILURE:
                return name
        raise PreviewBackendError(
            "没有可用的 H.264 解码器，已尝试: "
            + ", ".join(candidates)
        )

    def _build_pipeline(self) -> None:
        Gst = self._Gst
        mixer = Gst.ElementFactory.make("glvideomixer", "preview-mixer")
        output_queue = Gst.ElementFactory.make("queue", "preview-output-queue")
        output_caps = Gst.ElementFactory.make("capsfilter", "preview-output-caps")
        sink = Gst.ElementFactory.make("qml6glsink", "preview-sink")
        if any(
            element is None
            for element in (mixer, output_queue, output_caps, sink)
        ):
            raise PreviewBackendError("无法创建预览合成或显示元素")
        if mixer.find_property("background") is not None:
            mixer.set_property("background", 1)
        if mixer.find_property("start-time-selection") is not None:
            mixer.set_property("start-time-selection", 1)
        if mixer.find_property("latency") is not None:
            mixer.set_property(
                "latency",
                int(self.mixer_latency_ms * 1_000_000),
            )
        configure_latest_frame_queue(output_queue, self.latest_frame_only)
        output_caps.set_property(
            "caps",
            Gst.Caps.from_string(
                "video/x-raw(memory:GLMemory),format=RGBA,"
                f"width={self.mosaic_width},height={self.mosaic_height},"
                f"framerate={self.preview_fps}/1"
            ),
        )
        configure_preview_sink(
            sink,
            self.latest_frame_only,
            self.display_fps,
        )
        for element in (mixer, output_queue, output_caps, sink):
            self.pipeline.add(element)
        for previous, current in zip(
            (mixer, output_queue, output_caps),
            (output_queue, output_caps, sink),
            strict=True,
        ):
            if not previous.link(current):
                raise PreviewBackendError(
                    f"无法连接 {previous.get_name()} → {current.get_name()}"
                )
        sink_pad = sink.get_static_pad("sink")
        sink_pad.add_probe(
            Gst.PadProbeType.BUFFER,
            self._on_mosaic_buffer,
        )
        self.mixer = mixer
        self.sink = sink
        for camera in self.CAMERAS:
            branch = self._build_branch(camera)
            self.branches[camera] = branch
            self.pipeline.add(branch.bin)
            mixer_pad = mixer.request_pad_simple("sink_%u")
            branch_src = branch.bin.get_static_pad("src")
            if (
                mixer_pad is None
                or branch_src is None
                or branch_src.link(mixer_pad) != Gst.PadLinkReturn.OK
            ):
                raise PreviewBackendError(f"{camera} 无法连接到 GPU 合成器")
            branch.mixer_pad = mixer_pad
        self.set_cockpit_layout(
            str(self.config.get("primary_camera", "cam_a"))
        )

    def _build_branch(self, camera: str) -> _Branch:
        Gst = self._Gst
        branch_bin = Gst.Bin.new(f"{camera}-preview-bin")
        elements: dict[str, Any] = {
            "appsrc": Gst.ElementFactory.make("appsrc", f"{camera}-appsrc"),
            "parser": Gst.ElementFactory.make("h264parse", f"{camera}-parser"),
            "decoder": Gst.ElementFactory.make(
                self.decoder_name,
                f"{camera}-decoder",
            ),
            "queue": Gst.ElementFactory.make("queue", f"{camera}-queue"),
            "upload": Gst.ElementFactory.make("glupload", f"{camera}-upload"),
            "convert": Gst.ElementFactory.make(
                "glcolorconvert",
                f"{camera}-convert",
            ),
        }
        analysis_enabled = (
            self.analysis_callback is not None
            and camera == self.analysis_camera
        )
        if analysis_enabled:
            elements.update(
                {
                    "native_caps": Gst.ElementFactory.make(
                        "capsfilter",
                        f"{camera}-native-caps",
                    ),
                    "tee": Gst.ElementFactory.make("tee", f"{camera}-tee"),
                    "analysis_queue": Gst.ElementFactory.make(
                        "queue",
                        f"{camera}-analysis-queue",
                    ),
                    "analysis_rate": Gst.ElementFactory.make(
                        "videorate",
                        f"{camera}-analysis-rate",
                    ),
                    "analysis_scale": Gst.ElementFactory.make(
                        "videoscale",
                        f"{camera}-analysis-scale",
                    ),
                    "analysis_convert": Gst.ElementFactory.make(
                        "videoconvert",
                        f"{camera}-analysis-convert",
                    ),
                    "analysis_caps": Gst.ElementFactory.make(
                        "capsfilter",
                        f"{camera}-analysis-caps",
                    ),
                    "analysis_sink": Gst.ElementFactory.make(
                        "appsink",
                        f"{camera}-analysis-sink",
                    ),
                }
            )
        missing = [name for name, element in elements.items() if element is None]
        if missing:
            raise PreviewBackendError(
                f"{camera} 无法创建 GStreamer 元素: {', '.join(missing)}"
            )
        appsrc = elements["appsrc"]
        appsrc.set_property("is-live", True)
        appsrc.set_property("format", Gst.Format.TIME)
        appsrc.set_property("do-timestamp", False)
        configure_appsrc_queue(
            appsrc,
            self.latest_frame_only,
            self.appsrc_queue_frames,
        )
        appsrc.set_property(
            "caps",
            Gst.Caps.from_string(
                "video/x-h264,stream-format=byte-stream,alignment=au,"
                f"framerate={self.source_fps}/1"
            ),
        )
        parser = elements["parser"]
        parser.set_property("config-interval", -1)
        decoder = elements["decoder"]
        if self.decoder_name == "avdec_h264":
            decoder.set_property("max-threads", self.software_threads)
        for property_name in (
            "automatic-request-sync-points",
            "discard-corrupted-frames",
        ):
            if decoder.find_property(property_name) is not None:
                decoder.set_property(property_name, True)
        gst_queue = elements["queue"]
        configure_latest_frame_queue(
            gst_queue,
            self.latest_frame_only,
            self.decoded_queue_frames,
        )
        if analysis_enabled:
            elements["native_caps"].set_property(
                "caps",
                Gst.Caps.from_string(
                    native_video_caps(
                        self.source_width,
                        self.source_height,
                        self.source_fps,
                    )
                ),
            )
            analysis_queue = elements["analysis_queue"]
            configure_latest_frame_queue(analysis_queue, True)
            elements["analysis_rate"].set_property("drop-only", True)
            elements["analysis_caps"].set_property(
                "caps",
                Gst.Caps.from_string(
                    "video/x-raw,format=RGB,"
                    f"width={self.analysis_width},"
                    f"height={self.analysis_height},"
                    f"framerate={self.analysis_fps}/1"
                ),
            )
            analysis_sink = elements["analysis_sink"]
            analysis_sink.set_property("emit-signals", True)
            analysis_sink.set_property("sync", False)
            analysis_sink.set_property("max-buffers", 1)
            analysis_sink.set_property("drop", True)
            analysis_sink.connect(
                "new-sample",
                self._on_analysis_sample,
            )
        for element in elements.values():
            branch_bin.add(element)
        if analysis_enabled:
            if not appsrc.link(parser) or not parser.link(decoder):
                raise PreviewBackendError(f"{camera} 无法连接解码输入")
            native_caps = elements["native_caps"]
            tee = elements["tee"]
            if (
                not decoder.link(native_caps)
                or not native_caps.link(tee)
                or not tee.link(gst_queue)
            ):
                raise PreviewBackendError(f"{camera} 无法连接预览分支")
            preview_ordered = [
                gst_queue,
                elements["upload"],
                elements["convert"],
            ]
            analysis_ordered = [
                elements["analysis_queue"],
                elements["analysis_rate"],
                elements["analysis_scale"],
                elements["analysis_convert"],
                elements["analysis_caps"],
                elements["analysis_sink"],
            ]
            if not tee.link(analysis_ordered[0]):
                raise PreviewBackendError(f"{camera} 无法连接手势分析分支")
            for ordered in (preview_ordered, analysis_ordered):
                for previous, current in zip(
                    ordered,
                    ordered[1:],
                    strict=False,
                ):
                    if not previous.link(current):
                        raise PreviewBackendError(
                            f"{camera} 无法连接 "
                            f"{previous.get_name()} → {current.get_name()}"
                        )
        else:
            ordered = [
                appsrc,
                parser,
                decoder,
                gst_queue,
                elements["upload"],
                elements["convert"],
            ]
            for previous, current in zip(ordered, ordered[1:], strict=False):
                if not previous.link(current):
                    raise PreviewBackendError(
                        f"{camera} 无法连接 "
                        f"{previous.get_name()} → {current.get_name()}"
                    )
        parser.get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER,
            self._on_stage_buffer,
            (camera, "parsed"),
        )
        decoder.get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER,
            self._on_stage_buffer,
            (camera, "decoded"),
        )
        convert_src = elements["convert"].get_static_pad("src")
        convert_src.add_probe(
            Gst.PadProbeType.BUFFER,
            self._on_branch_buffer,
            camera,
        )
        ghost_pad = Gst.GhostPad.new("src", convert_src)
        if ghost_pad is None or not branch_bin.add_pad(ghost_pad):
            raise PreviewBackendError(f"{camera} 无法创建合成输出 Pad")
        return _Branch(camera, branch_bin, appsrc, gst_queue)

    def _on_stage_buffer(
        self,
        _pad: Any,
        info: Any,
        stage: tuple[str, str],
    ) -> Any:
        if info.get_buffer() is None or not self.monitoring_enabled:
            return self._Gst.PadProbeReturn.OK
        camera, counter_name = stage
        with self._counter_lock:
            counters = self._counters[camera]
            setattr(counters, counter_name, getattr(counters, counter_name) + 1)
        return self._Gst.PadProbeReturn.OK

    def bind_widget(self, item: QQuickItem) -> None:
        if self.sink is None:
            raise PreviewBackendError("预览 sink 尚未创建")
        _set_gpointer_property(self.sink, "widget", item)

    def set_cockpit_layout(self, primary_camera: str = "cam_a") -> None:
        if primary_camera not in self.CAMERAS:
            primary_camera = "cam_a"
        layout = cockpit_layout(
            self.mosaic_width,
            self.mosaic_height,
            primary_camera,
        )
        for name, branch in self.branches.items():
            pad = branch.mixer_pad
            if pad is None:
                continue
            values = layout[name]
            pad.set_property("alpha", values["alpha"])
            pad.set_property("xpos", values["x"])
            pad.set_property("ypos", values["y"])
            pad.set_property("width", values["width"])
            pad.set_property("height", values["height"])
            pad.set_property("zorder", values["z"])

    def set_mode(self, single: bool, camera: str) -> None:
        if camera not in self.CAMERAS:
            return
        half_width = self.mosaic_width // 2
        half_height = self.mosaic_height // 2
        grid_positions = {
            "cam_a": (0, 0),
            "cam_b": (half_width, 0),
            "cam_c": (0, half_height),
            "cam_d": (half_width, half_height),
        }
        for name, branch in self.branches.items():
            pad = branch.mixer_pad
            if pad is None:
                continue
            if single:
                selected = name == camera
                pad.set_property("alpha", 1.0 if selected else 0.0)
                pad.set_property("xpos", 0)
                pad.set_property("ypos", 0)
                pad.set_property("width", self.mosaic_width)
                pad.set_property("height", self.mosaic_height)
                pad.set_property("zorder", 1 if selected else 0)
            else:
                xpos, ypos = grid_positions[name]
                pad.set_property("alpha", 1.0)
                pad.set_property("xpos", xpos)
                pad.set_property("ypos", ypos)
                pad.set_property("width", half_width)
                pad.set_property("height", half_height)
                pad.set_property("zorder", self.CAMERAS.index(name))

    def start(self) -> None:
        if any(thread.is_alive() for thread in self._workers.values()):
            return
        Gst = self._Gst
        if self.sink is None:
            raise PreviewBackendError("预览 sink 尚未创建")
        result = self.sink.set_state(Gst.State.READY)
        if result == Gst.StateChangeReturn.FAILURE:
            raise PreviewBackendError("qml6glsink 无法进入 READY")
        result = self.pipeline.set_state(Gst.State.PLAYING)
        if result == Gst.StateChangeReturn.FAILURE:
            raise PreviewBackendError("GStreamer 预览 Pipeline 启动失败")
        self._origin_ns = time.monotonic_ns()
        self._source_pts_origins_ns = {
            camera: None for camera in self.CAMERAS
        }
        self._pipeline_pts_origin_ns = None
        self._stop_event.clear()
        self._draining_event.clear()
        self._workers = {
            camera: threading.Thread(
                target=self._push_loop,
                args=(camera,),
                name=f"gstreamer-preview-{camera}",
                daemon=True,
            )
            for camera in self.CAMERAS
        }
        self._bus_thread = threading.Thread(
            target=self._bus_loop,
            name="gstreamer-preview-bus",
            daemon=True,
        )
        for thread in self._workers.values():
            thread.start()
        self._bus_thread.start()

    def submit(self, frame: CameraFrame) -> None:
        if (
            frame.camera not in self.branches
            or self._stop_event.is_set()
            or self._draining_event.is_set()
        ):
            return
        if self._awaiting_keyframe[frame.camera]:
            if not frame.is_keyframe:
                return
            self._awaiting_keyframe[frame.camera] = False
            if self.failure_policy == "warn_continue":
                self._mark_recovered(frame.camera)
        input_queue = self._queues[frame.camera]
        try:
            input_queue.put_nowait(frame)
        except queue.Full:
            self._mark_failed(
                frame.camera,
                "预览输入积压超过 "
                f"{self.input_queue_frames} 帧，已等待关键帧恢复",
            )
            while True:
                try:
                    input_queue.get_nowait()
                    input_queue.task_done()
                except queue.Empty:
                    break
            if frame.is_keyframe:
                if self.failure_policy == "warn_continue":
                    self._mark_recovered(frame.camera)
                input_queue.put_nowait(frame)
            else:
                self._awaiting_keyframe[frame.camera] = True

    def health(self) -> dict[str, PreviewHealth]:
        with self._counter_lock:
            return {
                camera: self._health_value(camera, counters)
                for camera, counters in self._counters.items()
            }

    def report_start_failure(self, message: str) -> None:
        for camera in self.CAMERAS:
            self._mark_failed(camera, message)

    def stop(self) -> None:
        if any(thread.is_alive() for thread in self._workers.values()):
            self._draining_event.set()
            drain_deadline = time.monotonic() + 3
            while time.monotonic() < drain_deadline:
                if all(
                    input_queue.unfinished_tasks == 0
                    for input_queue in self._queues.values()
                ):
                    break
                time.sleep(0.01)
            for branch in self.branches.values():
                branch.appsrc.emit("end-of-stream")
            if self.monitoring_enabled and not self.latest_frame_only:
                render_deadline = time.monotonic() + 3
                while time.monotonic() < render_deadline:
                    self._poll_sink_stats()
                    with self._counter_lock:
                        complete = all(
                            counters.rendered >= counters.submitted
                            for counters in self._counters.values()
                        )
                    if complete:
                        break
                    time.sleep(0.01)
        self._stop_event.set()
        for thread in (*self._workers.values(), self._bus_thread):
            if thread is not None:
                thread.join(timeout=3)
        self.pipeline.set_state(self._Gst.State.NULL)
        self._workers = {
            camera: thread
            for camera, thread in self._workers.items()
            if thread.is_alive()
        }
        if self._bus_thread is not None and not self._bus_thread.is_alive():
            self._bus_thread = None

    def _push_loop(self, camera: str) -> None:
        input_queue = self._queues[camera]
        while not self._stop_event.is_set():
            try:
                frame = input_queue.get(timeout=0.05)
            except queue.Empty:
                if self.monitoring_enabled:
                    self._check_backlog()
                continue
            try:
                with self._counter_lock:
                    failed = not self._counters[frame.camera].healthy
                if failed and self.failure_policy != "warn_continue":
                    self._suspend_branch(frame.camera)
                    if not frame.is_keyframe:
                        continue
                    self._restart_branch(frame.camera)
                self._push_frame(frame)
                if self.monitoring_enabled:
                    self._check_backlog()
            finally:
                input_queue.task_done()

    def _push_frame(self, frame: CameraFrame) -> None:
        Gst = self._Gst
        buffer = Gst.Buffer.new_allocate(None, len(frame.payload), None)
        buffer.fill(0, frame.payload)
        duration = Gst.util_uint64_scale_int(
            1,
            Gst.SECOND,
            self.source_fps,
        )
        clock = self.pipeline.get_clock()
        base_time = self.pipeline.get_base_time()
        if clock is not None and base_time != Gst.CLOCK_TIME_NONE:
            running_time_ns = max(0, int(clock.get_time() - base_time))
        else:
            running_time_ns = max(0, time.monotonic_ns() - self._origin_ns)
        with self._counter_lock:
            counters = self._counters[frame.camera]
            source_time_ns = frame.stamp.monotonic_ns
            pts = self._next_preview_pts(
                frame.camera,
                source_time_ns,
                running_time_ns,
            )
            pts = max(0, pts, counters.last_pipeline_pts_ns + 1)
            counters.last_pipeline_pts_ns = pts
            if self.monitoring_enabled:
                self._frame_metadata[frame.camera][pts] = (
                    frame.sequence,
                    frame.stamp.monotonic_ns,
                )
        buffer.pts = pts
        buffer.dts = pts
        buffer.duration = duration
        buffer.offset = frame.sequence
        result = self.branches[frame.camera].appsrc.emit("push-buffer", buffer)
        if result != Gst.FlowReturn.OK:
            if self.monitoring_enabled:
                with self._counter_lock:
                    self._frame_metadata[frame.camera].pop(pts, None)
            self._mark_failed(
                frame.camera,
                f"appsrc push-buffer 返回 {result.value_nick}",
            )
            return
        with self._counter_lock:
            counters = self._counters[frame.camera]
            counters.submitted += 1
            counters.last_submitted_sequence = frame.sequence
            if len(self._frame_metadata[frame.camera]) > 512:
                oldest_pts = min(self._frame_metadata[frame.camera])
                self._frame_metadata[frame.camera].pop(oldest_pts, None)

    def _next_preview_pts(
        self,
        camera: str,
        source_time_ns: int,
        running_time_ns: int,
    ) -> int:
        if self._pipeline_pts_origin_ns is None:
            self._pipeline_pts_origin_ns = (
                running_time_ns + int(self.preview_jitter_ms * 1_000_000)
            )
        source_origin_ns = self._source_pts_origins_ns[camera]
        if source_origin_ns is None:
            source_origin_ns = source_time_ns
            self._source_pts_origins_ns[camera] = source_origin_ns
        return self._pipeline_pts_origin_ns + source_time_ns - source_origin_ns

    def _on_analysis_sample(self, sink: Any) -> Any:
        Gst = self._Gst
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buffer = sample.get_buffer()
        caps = sample.get_caps()
        if buffer is None or caps is None or self.analysis_callback is None:
            return Gst.FlowReturn.OK
        structure = caps.get_structure(0)
        width = int(structure.get_value("width"))
        height = int(structure.get_value("height"))
        success, mapping = buffer.map(Gst.MapFlags.READ)
        if not success:
            return Gst.FlowReturn.OK
        try:
            expected = width * height * 3
            array = np.frombuffer(mapping.data, dtype=np.uint8, count=expected)
            image = array.reshape((height, width, 3)).copy()
        finally:
            buffer.unmap(mapping)
        self.analysis_callback(image, time.monotonic_ns())
        return Gst.FlowReturn.OK

    def _on_branch_buffer(
        self,
        _pad: Any,
        info: Any,
        camera: str,
    ) -> Any:
        buffer = info.get_buffer()
        if buffer is None or not self.monitoring_enabled:
            return self._Gst.PadProbeReturn.OK
        mapping_failed = False
        with self._counter_lock:
            counters = self._counters[camera]
            counters.sink_arrived += 1
            metadata = self._frame_metadata[camera].pop(
                int(buffer.pts),
                None,
            )
            if metadata is not None:
                counters.pending_frames.append(metadata)
            else:
                mapping_failed = True
        if mapping_failed and self.monitoring_enabled:
            self._mark_failed(
                camera,
                "解码输出无法匹配输入帧序号",
            )
        return self._Gst.PadProbeReturn.OK

    def _on_mosaic_buffer(self, _pad: Any, info: Any) -> Any:
        if info.get_buffer() is None or not self.monitoring_enabled:
            return self._Gst.PadProbeReturn.OK
        now = time.monotonic_ns()
        with self._counter_lock:
            for counters in self._counters.values():
                if not counters.pending_frames:
                    continue
                sequence, capture_ns = counters.pending_frames[-1]
                counters.pending_frames.clear()
                counters.rendered += 1
                counters.last_rendered_sequence = sequence
                counters.rendered_source_monotonic_ns = capture_ns
                counters.latency_ms = max(
                    0.0,
                    (now - capture_ns) / 1_000_000,
                )
        return self._Gst.PadProbeReturn.OK

    def _poll_sink_stats(self) -> None:
        now = time.monotonic_ns()
        diagnostics: list[str] = []
        try:
            stats = self.sink.get_property("stats")
            sink_rendered = int(stats.get_value("rendered"))
            sink_dropped = int(stats.get_value("dropped"))
        except Exception:
            sink_rendered = self._sink_stats_last_rendered
            sink_dropped = self._sink_stats_last_dropped
        sink_rendered_delta = (
            sink_rendered - self._sink_stats_last_rendered
            if sink_rendered >= self._sink_stats_last_rendered
            else sink_rendered
        )
        sink_dropped_delta = (
            sink_dropped - self._sink_stats_last_dropped
            if sink_dropped >= self._sink_stats_last_dropped
            else sink_dropped
        )
        self._sink_stats_last_rendered = sink_rendered
        self._sink_stats_last_dropped = sink_dropped
        for camera in self.CAMERAS:
            should_fail = False
            with self._counter_lock:
                counters = self._counters[camera]
                elapsed_s = (
                    (now - counters.stats_last_poll_ns) / 1_000_000_000
                    if counters.stats_last_poll_ns
                    else self.monitor_interval_s
                )
                submitted_delta = counters.submitted - counters.stats_last_submitted
                parsed_delta = counters.parsed - counters.stats_last_parsed
                decoded_delta = counters.decoded - counters.stats_last_decoded
                arrived_delta = (
                    counters.sink_arrived - counters.stats_last_sink_arrived
                )
                rendered_delta = counters.rendered - counters.stats_last_rendered
                counters.stats_last_rendered = counters.rendered
                counters.stats_last_submitted = counters.submitted
                counters.stats_last_parsed = counters.parsed
                counters.stats_last_decoded = counters.decoded
                counters.stats_last_sink_arrived = counters.sink_arrived
                counters.stats_last_poll_ns = now
                should_fail = counters.latency_ms > self.max_latency_ms
                health = self._health_value(camera, counters)
                if self.diagnostics_enabled:
                    appsrc_level = int(
                        self.branches[camera].appsrc.get_property(
                            "current-level-buffers"
                        )
                    )
                    decoded_level = int(
                        self.branches[camera].decoded_queue.get_property(
                            "current-level-buffers"
                        )
                    )
                    diagnostics.append(
                        f"{camera}:提交={submitted_delta / max(elapsed_s, 0.001):.1f},"
                        f"解析={parsed_delta / max(elapsed_s, 0.001):.1f},"
                        f"解码={decoded_delta / max(elapsed_s, 0.001):.1f},"
                        f"进mixer={arrived_delta / max(elapsed_s, 0.001):.1f},"
                        f"渲染={rendered_delta / max(elapsed_s, 0.001):.1f},"
                        f"Python队列={self._queues[camera].qsize()},"
                        f"appsrc队列={appsrc_level},"
                        f"解码队列={decoded_level},"
                        f"待统计={len(counters.pending_frames)},"
                        f"延迟={counters.latency_ms:.1f}ms"
                    )
            self.health_callback(health)
            if should_fail and self.monitoring_enabled:
                self._mark_failed(
                    camera,
                    f"预览延迟 {health.latency_ms:.1f} ms 超过 "
                    f"{self.max_latency_ms:.0f} ms",
                )
        if diagnostics:
            print(
                "[preview-diag] "
                f"decoder={self.decoder_name},"
                f"mosaic渲染={sink_rendered_delta / self.monitor_interval_s:.1f},"
                f"mosaic丢弃={sink_dropped_delta / self.monitor_interval_s:.1f}fps | "
                + " | ".join(diagnostics),
                flush=True,
            )

    def _check_backlog(self) -> None:
        if self.latest_frame_only:
            return
        threshold = max(
            2,
            int(self.max_latency_ms / (1000 / self.preview_fps)),
        )
        failures: list[str] = []
        with self._counter_lock:
            for camera, counters in self._counters.items():
                submitted = (
                    counters.submitted - counters.backlog_origin_submitted
                )
                rendered = counters.rendered - counters.backlog_origin_rendered
                if counters.healthy and submitted - rendered > threshold:
                    failures.append(camera)
        for camera in failures:
            self._mark_failed(
                camera,
                "预览渲染积压超过延迟阈值，已停止该路并等待关键帧重建",
            )

    def _restart_branch(self, camera: str) -> None:
        branch = self.branches[camera]
        branch.bin.sync_state_with_parent()
        with self._counter_lock:
            counters = self._counters[camera]
            counters.healthy = True
            counters.message = ""
            counters.suspended = False
            counters.backlog_origin_submitted = counters.submitted
            counters.backlog_origin_rendered = counters.rendered
            counters.pending_frames.clear()
            self._frame_metadata[camera].clear()
            health = self._health_value(camera, counters)
        self.health_callback(health)

    def _suspend_branch(self, camera: str) -> None:
        with self._counter_lock:
            counters = self._counters[camera]
            if counters.suspended:
                return
            counters.suspended = True
        self.branches[camera].bin.set_state(self._Gst.State.READY)

    def _mark_failed(self, camera: str, message: str) -> None:
        with self._counter_lock:
            counters = self._counters[camera]
            if not counters.healthy:
                return
            counters.healthy = False
            counters.message = message
            health = self._health_value(camera, counters)
        self.health_callback(health)

    def _mark_recovered(self, camera: str) -> None:
        with self._counter_lock:
            counters = self._counters[camera]
            if counters.healthy:
                return
            counters.healthy = True
            counters.message = ""
            health = self._health_value(camera, counters)
        self.health_callback(health)

    def _bus_loop(self) -> None:
        Gst = self._Gst
        bus = self.pipeline.get_bus()
        mask = Gst.MessageType.ERROR | Gst.MessageType.EOS
        next_monitor = time.monotonic()
        while not self._stop_event.is_set():
            message = bus.timed_pop_filtered(20 * Gst.MSECOND, mask)
            now = time.monotonic()
            if self.monitoring_enabled and now >= next_monitor:
                self._poll_sink_stats()
                next_monitor = now + self.monitor_interval_s
            if message is None:
                continue
            if message.type == Gst.MessageType.ERROR:
                error, debug = message.parse_error()
                text = f"{error.message}; {debug or ''}".strip()
            elif self._draining_event.is_set():
                continue
            else:
                text = "预览 Pipeline 收到 EOS"
            for camera in self.CAMERAS:
                self._mark_failed(camera, text)

    @staticmethod
    def _health_value(
        camera: str,
        counters: _PreviewCounters,
    ) -> PreviewHealth:
        return PreviewHealth(
            camera=camera,
            submitted_count=counters.submitted,
            rendered_count=counters.rendered,
            last_submitted_sequence=counters.last_submitted_sequence,
            last_rendered_sequence=counters.last_rendered_sequence,
            latency_ms=counters.latency_ms,
            healthy=counters.healthy,
            message=counters.message,
            rendered_source_monotonic_ns=(
                counters.rendered_source_monotonic_ns
            ),
        )


class GstreamerPreviewController(QObject):
    health_changed = Signal(object)

    def __init__(
        self,
        config: dict[str, Any],
        quick_view: QQuickView,
        video_item: QQuickItem,
        *,
        analysis_callback: Callable[[np.ndarray, int], None] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.quick_view = quick_view
        self.backend = _GstreamerBackend(
            config,
            self.health_changed.emit,
            analysis_callback,
        )
        self.backend.bind_widget(video_item)
        self._start_job: _StartPipelineJob | None = None

    def start(self) -> None:
        self._start_job = _StartPipelineJob(self.backend)
        self.quick_view.scheduleRenderJob(
            self._start_job,
            QQuickWindow.RenderStage.BeforeSynchronizingStage,
        )

    def submit(self, frame: CameraFrame) -> None:
        self.backend.submit(frame)

    def set_cockpit_layout(self, primary_camera: str = "cam_a") -> None:
        self.backend.set_cockpit_layout(primary_camera)

    def set_health(self, _health: PreviewHealth) -> None:
        return

    def health(self) -> dict[str, PreviewHealth]:
        return self.backend.health()

    def stop(self) -> None:
        self.backend.stop()


def _set_gpointer_property(element: Any, name: str, item: QQuickItem) -> None:
    capsule = getattr(element, "__gpointer__", None)
    if capsule is None:
        raise PreviewBackendError("PyGObject 未暴露 GObject 指针")
    get_pointer = ctypes.pythonapi.PyCapsule_GetPointer
    get_pointer.restype = ctypes.c_void_p
    get_pointer.argtypes = [ctypes.py_object, ctypes.c_char_p]
    element_pointer = get_pointer(capsule, None)
    item_pointer = shiboken6.getCppPointer(item)[0]
    library_name = ctypes.util.find_library("gobject-2.0")
    if not library_name:
        raise PreviewBackendError("找不到 libgobject-2.0")
    library = ctypes.CDLL(library_name)
    setter = library.g_object_set
    setter.restype = None
    setter.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    setter(
        ctypes.c_void_p(element_pointer),
        name.encode(),
        ctypes.c_void_p(item_pointer),
        None,
    )


def _ensure_system_typelib_path() -> None:
    candidates = [
        Path("/usr/lib/x86_64-linux-gnu/girepository-1.0"),
        Path("/usr/lib/girepository-1.0"),
    ]
    existing = [str(path) for path in candidates if path.is_dir()]
    configured = [
        value
        for value in os.environ.get("GI_TYPELIB_PATH", "").split(":")
        if value
    ]
    combined = [*existing, *configured]
    if combined:
        os.environ["GI_TYPELIB_PATH"] = ":".join(dict.fromkeys(combined))
