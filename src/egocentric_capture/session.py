from __future__ import annotations

import json
import os
import shutil
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .clocks import HostClockMapper
from .config import output_root
from .mcap_io import (
    SegmentedMcapWriter,
    segment_dicts,
)
from .models import (
    CameraFrame,
    CaptureRequest,
    CaptureState,
    ClockSyncSample,
    FinalizeProgress,
    HealthSnapshot,
    OakImuSample,
    PreviewHealth,
    SystemEvent,
    WearableEmgSample,
    WearableImuSample,
    WearableRawChunk,
)
from .quality import _camera_sync, build_quality_report
from .sources.base import SourceCallbacks
from .sources.oak import OakSource
from .sources.simulated import SimulatedSource
from .sources.wearable import WearableSource
from .storage import (
    TrialIndex,
    atomic_write_json,
    commit_session_artifacts,
    create_session_dir,
)

StateCallback = Callable[[CaptureState, str], None]
HealthCallback = Callable[[HealthSnapshot], None]
PreviewCallback = Callable[[CameraFrame], None]
EmgCallback = Callable[[WearableEmgSample], None]
SessionCallback = Callable[[Path | None, dict[str, Any] | None], None]
FinalizeProgressCallback = Callable[[FinalizeProgress], None]


@dataclass(slots=True)
class EngineCallbacks:
    on_state: StateCallback | None = None
    on_health: HealthCallback | None = None
    on_preview: PreviewCallback | None = None
    on_emg: EmgCallback | None = None
    on_session: SessionCallback | None = None
    on_finalize_progress: FinalizeProgressCallback | None = None


@dataclass(slots=True)
class _SequenceState:
    last: int | None = None
    gaps: int = 0

    def push(self, value: int, modulo: int | None = None) -> int:
        if self.last is None:
            self.last = value
            return 0
        expected = self.last + 1
        if modulo is not None:
            expected %= modulo
            missing = (value - expected) % modulo if value != expected else 0
        else:
            missing = max(1, value - expected) if value != expected else 0
        self.gaps += missing
        self.last = value
        return missing


class CaptureEngine:
    """统一协调设备健康、严格录制状态和最终质检。"""

    REQUIRED_KEYS = (
        "camera/cam_a",
        "camera/cam_b",
        "camera/cam_c",
        "camera/cam_d",
        "imu/oak",
        "wearable/emg",
        "wearable/imu",
    )

    def __init__(
        self,
        config: dict[str, Any],
        *,
        simulate: bool = False,
        callbacks: EngineCallbacks | None = None,
    ) -> None:
        self.config = config
        self.simulate = simulate
        self.callbacks = callbacks or EngineCallbacks()
        self.clock = HostClockMapper()
        self._output_root = output_root(config)
        self._trial_index = TrialIndex(self._output_root)
        self.state = CaptureState.DISCONNECTED
        self.state_message = "尚未连接设备"
        self._lock = threading.RLock()
        self._ingest_condition = threading.Condition(self._lock)
        self._monitor_thread: threading.Thread | None = None
        self._source_start_thread: threading.Thread | None = None
        self._monitor_stop = threading.Event()
        self._sources: list[Any] = []
        self._last_source_seen: dict[str, int] = {}
        self._last_arrival_seen: dict[str, int] = {}
        self._first_seen: dict[str, int] = {}
        self._first_arrival_seen: dict[str, int] = {}
        self._rate_windows: dict[str, deque[int]] = defaultdict(
            lambda: deque(maxlen=3000)
        )
        self._arrival_rate_windows: dict[str, deque[int]] = defaultdict(
            lambda: deque(maxlen=3000)
        )
        self._sequence: dict[str, _SequenceState] = defaultdict(_SequenceState)
        self._healthy_since_ns: int | None = None
        self._metadata: dict[str, dict[str, Any]] = {}
        self._writer: SegmentedMcapWriter | None = None
        self._session_dir: Path | None = None
        self._manifest: dict[str, Any] | None = None
        self._arm_first: dict[str, int] = {}
        self._arm_requested_ns: int | None = None
        self._recording_started_ns: int | None = None
        self._stop_requested_ns: int | None = None
        self._finalizing = False
        self._failure_reason: str | None = None
        self._wearable_imu_baseline_hz = 0.0
        self._last_system_sample_ns = 0
        self._recording_accepting = False
        self._ingest_inflight = 0
        self._received_counts: dict[str, int] = defaultdict(int)
        self._received_first_sequence: dict[str, int] = {}
        self._received_last_sequence: dict[str, int] = {}
        self._online_camera_times: dict[str, list[int]] = defaultdict(list)
        self._preview_health: dict[str, PreviewHealth] = {}
        self._event_loop_lag_ms = 0.0
        self._shutdown_thread: threading.Thread | None = None
        self._global_log_lock = threading.Lock()
        self._event_lock = threading.Lock()
        self._event_accepting = False

    @property
    def session_dir(self) -> Path | None:
        return self._session_dir

    @property
    def recording(self) -> bool:
        return self.state in {CaptureState.ARMING, CaptureState.RECORDING}

    @property
    def bypass_device_checks(self) -> bool:
        return bool(
            self.config.get("session", {}).get(
                "bypass_device_checks",
                False,
            )
        )

    def start(self) -> None:
        with self._lock:
            if self.state != CaptureState.DISCONNECTED:
                return
            self._output_root.mkdir(parents=True, exist_ok=True)
            (self._output_root / "logs").mkdir(parents=True, exist_ok=True)
            threading.Thread(
                target=self._trial_index.refresh,
                name="trial-index-scan",
                daemon=True,
            ).start()
            self._set_state(CaptureState.CHECKING, "正在检查全部采集设备")
            source_callbacks = SourceCallbacks(
                on_sample=self.ingest,
                on_error=self._source_error,
                on_metadata=self._source_metadata,
            )
            if self.simulate:
                self._sources = [
                    SimulatedSource(
                        dict(self.config.get("simulation") or {}),
                        self.clock,
                        source_callbacks,
                    )
                ]
            else:
                self._sources = [
                    OakSource(
                        dict(self.config.get("oak") or {}),
                        self.clock,
                        source_callbacks,
                    ),
                    WearableSource(
                        dict(self.config.get("wearable") or {}),
                        self.clock,
                        source_callbacks,
                    ),
                ]
            self._monitor_stop.clear()
            self._start_sources()
            if self.bypass_device_checks:
                self._set_state(
                    CaptureState.READY,
                    "就绪",
                )
            self._monitor_thread = threading.Thread(
                target=self._monitor,
                name="capture-monitor",
                daemon=True,
            )
            self._monitor_thread.start()

    def stop(self) -> None:
        if self.recording or self._finalizing:
            if self.recording:
                self._begin_finalize(False, "应用关闭时仍在录制")
            if not self.wait_for_finalization(120):
                raise TimeoutError("采集收尾未在 120 秒内完成")
        self._stop_runtime()

    def shutdown_async(
        self,
        on_complete: Callable[[str | None], None] | None = None,
    ) -> None:
        if self._shutdown_thread is not None and self._shutdown_thread.is_alive():
            return

        def shutdown_worker() -> None:
            error: str | None = None
            try:
                self.stop()
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            if on_complete is not None:
                on_complete(error)

        self._shutdown_thread = threading.Thread(
            target=shutdown_worker,
            name="capture-shutdown",
            daemon=True,
        )
        self._shutdown_thread.start()

    def _stop_runtime(self) -> None:
        if self.recording:
            self._begin_finalize(False, "应用关闭时仍在录制")
        self._monitor_stop.set()
        source_start_thread = self._source_start_thread
        if source_start_thread is not None:
            source_start_thread.join(timeout=1)
            if not source_start_thread.is_alive():
                self._source_start_thread = None
        monitor = self._monitor_thread
        if monitor is not None:
            monitor.join(timeout=3)
            if not monitor.is_alive():
                self._monitor_thread = None
        for source in self._sources:
            source.stop()
        with self._lock:
            if not self._finalizing:
                self._set_state(CaptureState.DISCONNECTED, "设备已断开")

    def request_record(self, request: CaptureRequest) -> Path:
        with self._lock:
            if self.state != CaptureState.READY:
                raise RuntimeError("全部设备尚未就绪")
            root = output_root(self.config)
            root.mkdir(parents=True, exist_ok=True)
            free_bytes = shutil.disk_usage(root).free
            minimum = int(
                float(
                    self.config.get("workspace", {}).get(
                        "minimum_start_free_gib", 20
                    )
                )
                * 1024**3
            )
            if free_bytes < minimum:
                raise RuntimeError(
                    f"剩余空间不足，至少需要 {minimum / 1024**3:.0f} GiB"
                )
            trial = self._trial_index.reserve(
                request.participant_id,
                request.task_id,
            )
            self._session_dir = create_session_dir(root, request, trial)
            self._arm_first.clear()
            self._arm_requested_ns = time.monotonic_ns()
            self._recording_started_ns = None
            self._stop_requested_ns = None
            self._failure_reason = None
            self._finalizing = False
            self._last_system_sample_ns = 0
            self._sequence.clear()
            self._received_counts.clear()
            self._received_first_sequence.clear()
            self._received_last_sequence.clear()
            self._online_camera_times.clear()
            self._recording_accepting = True
            self._manifest = {
                "schema_version": 2,
                "status": "arming",
                "request": asdict(request),
                "trial": trial,
                "session_dir": str(self._session_dir),
                "created_unix_ns": self.clock.to_unix_ns(self._arm_requested_ns),
                "recording_start_unix_ns": None,
                "valid_start_unix_ns": None,
                "stop_request_unix_ns": None,
                "completed_unix_ns": None,
                "failure_reason": None,
                "config": self.config,
                "devices": self._metadata,
                "clock_anchors": [
                    asdict(anchor) for anchor in self.clock.anchors
                ],
                "segments": [],
                "quality": None,
                "counts": None,
                "wearable_imu_baseline_hz": self._rate("wearable/imu"),
            }
            self._wearable_imu_baseline_hz = float(
                self._manifest["wearable_imu_baseline_hz"]
            )
            atomic_write_json(self._session_dir / "session.json", self._manifest)
            session_config = self.config.get("session", {})
            self._writer = SegmentedMcapWriter(
                self._session_dir,
                queue_size=int(session_config.get("writer_queue_size", 20000)),
                chunk_size_bytes=int(
                    float(session_config.get("mcap_chunk_size_mib", 8)) * 1024**2
                ),
                segment_duration_s=float(
                    session_config.get("segment_duration_s", 60)
                ),
                segment_size_bytes=int(
                    float(session_config.get("segment_size_gib", 1)) * 1024**3
                ),
                fsync_interval_s=float(session_config.get("fsync_interval_s", 2)),
                on_error=lambda message: self._begin_finalize(False, message),
            )
            self._writer.start()
            with self._event_lock:
                self._event_accepting = True
            self._write_event("info", "record_requested", "采集员请求开始录制")
            if self.bypass_device_checks:
                self._recording_started_ns = time.monotonic_ns()
                self._manifest["status"] = "recording"
                self._manifest["recording_start_unix_ns"] = (
                    self.clock.to_unix_ns(self._recording_started_ns)
                )
                self._manifest["valid_start_unix_ns"] = (
                    self._manifest["recording_start_unix_ns"]
                )
                atomic_write_json(
                    self._session_dir / "session.json",
                    self._manifest,
                )
                self._set_state(
                    CaptureState.RECORDING,
                    "正在录制",
                )
                self._write_event(
                    "warning",
                    "device_checks_bypassed",
                    "本轮录制已跳过设备就绪与中断检查",
                )
            else:
                self._set_state(
                    CaptureState.ARMING,
                    "等待四路关键帧和全部传感器",
                )
            for health in self._preview_health.values():
                if not health.healthy:
                    self._write_event(
                        "warning",
                        "preview_unavailable",
                        health.message or f"{health.camera} 预览不可用",
                        {"camera": health.camera},
                    )
            if self.callbacks.on_session is not None:
                self.callbacks.on_session(self._session_dir, self._manifest)
            return self._session_dir

    def next_trial(self, participant_id: str, task_id: str) -> int:
        with self._lock:
            request = (self._manifest or {}).get("request") or {}
            if (
                self._manifest is not None
                and str(request.get("participant_id", "")) == participant_id
                and str(request.get("task_id", "")) == task_id
                and self._manifest.get("status")
                in {"arming", "recording", "finalizing"}
            ):
                return int(self._manifest.get("trial", 1))
        return self._trial_index.next(participant_id, task_id)

    def request_stop(self) -> None:
        with self._lock:
            if self.state not in {CaptureState.ARMING, CaptureState.RECORDING}:
                return
        self._begin_finalize(True, None)

    def prepare_next(self) -> None:
        """确认上一轮结果，并重新进入设备检查。"""
        with self._lock:
            if self.state not in {CaptureState.COMPLETED, CaptureState.FAILED}:
                return
            self._session_dir = None
            self._manifest = None
            self._failure_reason = None
            self._arm_first.clear()
            self._arm_requested_ns = None
            self._recording_started_ns = None
            self._stop_requested_ns = None
            self._healthy_since_ns = None
            self._start_sources()
            threading.Thread(
                target=self._trial_index.refresh,
                name="trial-index-refresh",
                daemon=True,
            ).start()
            if self.bypass_device_checks:
                self._set_state(
                    CaptureState.READY,
                    "就绪",
                )
            else:
                self._set_state(CaptureState.CHECKING, "正在确认设备状态")

    def wait_for_finalization(self, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with self._lock:
                if not self._finalizing:
                    return True
            time.sleep(0.05)
        return False

    def ingest(self, sample: Any) -> None:
        key = _sample_key(sample)
        timestamp_ns = _sample_monotonic_ns(sample)
        arrival_ns = _sample_arrival_monotonic_ns(sample)
        writer: SegmentedMcapWriter | None = None
        should_record = False
        with self._lock:
            if key is not None:
                self._last_source_seen[key] = timestamp_ns
                self._last_arrival_seen[key] = arrival_ns
                self._first_seen.setdefault(key, timestamp_ns)
                self._first_arrival_seen.setdefault(key, arrival_ns)
                self._rate_windows[key].append(timestamp_ns)
                self._arrival_rate_windows[key].append(arrival_ns)
            if isinstance(sample, WearableEmgSample):
                self._check_sequence("wearable", sample.sequence, 256)
            elif isinstance(sample, WearableImuSample):
                self._check_sequence("wearable", sample.sequence, 256)
            elif isinstance(sample, CameraFrame):
                self._check_sequence(sample.camera, sample.sequence, None)

            writer = self._writer
            should_record = self._recording_accepting and writer is not None
            if should_record:
                self._ingest_inflight += 1
                if key is not None:
                    self._received_counts[key] += 1
                    sequence = _sample_sequence(sample)
                    if sequence is not None:
                        self._received_first_sequence.setdefault(key, sequence)
                        self._received_last_sequence[key] = sequence
                if (
                    isinstance(sample, CameraFrame)
                    and sample.stamp.source_device_ns is not None
                ):
                    self._online_camera_times[sample.camera].append(
                        sample.stamp.source_device_ns
                    )

        accepted = False
        try:
            if should_record and writer is not None:
                writer.submit_sample(sample)
                accepted = True
        except RuntimeError as exc:
            self._begin_finalize(False, str(exc))
        finally:
            if should_record:
                with self._lock:
                    if accepted and self.state == CaptureState.ARMING:
                        self._update_arming(sample)
                    self._ingest_inflight -= 1
                    self._ingest_condition.notify_all()

        if isinstance(sample, CameraFrame):
            if not should_record or accepted:
                callback = self.callbacks.on_preview
                if callback is not None:
                    callback(sample)
        elif isinstance(sample, WearableEmgSample):
            callback = self.callbacks.on_emg
            if callback is not None:
                callback(sample)

    def update_preview_health(self, health: PreviewHealth) -> None:
        with self._lock:
            previous = self._preview_health.get(health.camera)
            self._preview_health[health.camera] = health
        if not health.healthy and (previous is None or previous.healthy):
            event_thread = threading.Thread(
                target=self._write_event,
                args=(
                    "warning",
                    "preview_failure",
                    health.message or f"{health.camera} 预览异常",
                    {
                        "camera": health.camera,
                        "submitted_count": health.submitted_count,
                        "rendered_count": health.rendered_count,
                        "latency_ms": health.latency_ms,
                    },
                ),
                name=f"preview-event-{health.camera}",
                daemon=True,
            )
            event_thread.start()

    def update_event_loop_lag(self, lag_ms: float) -> None:
        with self._lock:
            self._event_loop_lag_ms = max(0.0, float(lag_ms))

    def health_snapshot(self) -> HealthSnapshot:
        now = time.monotonic_ns()
        root = output_root(self.config)
        writer = self._writer
        preview = dict(self._preview_health)
        received_counts = dict(self._received_counts)
        accepted_counts = writer.accepted_counts if writer else {}
        persisted_counts = writer.persisted_counts if writer else {}
        rendered_source_times = [
            value.rendered_source_monotonic_ns
            for value in preview.values()
            if value.rendered_source_monotonic_ns is not None
        ]
        return HealthSnapshot(
            stamp=self.clock.stamp(now),
            rates_hz={key: self._rate(key) for key in self.REQUIRED_KEYS},
            last_seen_age_s={
                key: (
                    max(0, now - self._last_arrival_seen[key])
                    / 1_000_000_000
                    if key in self._last_arrival_seen
                    else float("inf")
                )
                for key in self.REQUIRED_KEYS
            },
            sequence_gaps={
                key: tracker.gaps for key, tracker in self._sequence.items()
            },
            queue_depth=writer.queue_depth if writer else 0,
            queue_drops=writer.queue_drops if writer else 0,
            disk_free_bytes=_disk_free_bytes(root),
            ready=self.state == CaptureState.READY,
            received_counts=received_counts,
            writer_accepted_counts={
                key: int(accepted_counts.get(key, 0))
                for key in received_counts
            },
            persisted_counts={
                key: int(persisted_counts.get(key, 0))
                for key in received_counts
            },
            preview_submitted_counts={
                key: value.submitted_count for key, value in preview.items()
            },
            preview_rendered_counts={
                key: value.rendered_count for key, value in preview.items()
            },
            preview_latency_ms={
                key: value.latency_ms for key, value in preview.items()
            },
            writer_high_watermark=writer.high_watermark if writer else 0,
            source_reconnects={
                type(source).__name__: int(
                    getattr(source, "reconnect_count", 0)
                )
                for source in self._sources
            },
            event_loop_lag_ms=self._event_loop_lag_ms,
            writer_bytes_per_second=(
                writer.bytes_per_second if writer else 0.0
            ),
            preview_cross_camera_skew_ms=(
                (max(rendered_source_times) - min(rendered_source_times))
                / 1_000_000
                if len(rendered_source_times) == 4
                else 0.0
            ),
        )

    def _monitor(self) -> None:
        interval_s = 1.0 if self.bypass_device_checks else 0.2
        while not self._monitor_stop.wait(interval_s):
            with self._lock:
                self.clock.sample(3)
                snapshot = self.health_snapshot()
                now = time.monotonic_ns()
                if (
                    not self.bypass_device_checks
                    and self.state
                    in {CaptureState.CHECKING, CaptureState.READY}
                ):
                    healthy = self._health_is_ready(snapshot)
                    if healthy:
                        if self._healthy_since_ns is None:
                            self._healthy_since_ns = now
                        required = float(
                            self.config.get("session", {}).get(
                                "healthy_before_ready_s", 2
                            )
                        )
                        if (
                            now - self._healthy_since_ns
                            >= int(required * 1_000_000_000)
                            and self.state != CaptureState.READY
                        ):
                            self._set_state(
                                CaptureState.READY,
                                "全部设备稳定，可开始录制",
                            )
                    else:
                        self._healthy_since_ns = None
                        if self.state == CaptureState.READY:
                            self._set_state(
                                CaptureState.CHECKING,
                                "设备数据不完整，正在重新检查",
                            )
                if (
                    not self.bypass_device_checks
                    and self.state == CaptureState.ARMING
                ):
                    timeout = float(
                        self.config.get("session", {}).get("arm_timeout_s", 3)
                    )
                    if (
                        self._arm_requested_ns is not None
                        and now - self._arm_requested_ns > timeout * 1_000_000_000
                    ):
                        missing = sorted(
                            set(self.REQUIRED_KEYS) - set(self._arm_first)
                        )
                        self._begin_finalize(
                            False,
                            "录制准备超时，缺少: " + ", ".join(missing),
                        )
                if self.state == CaptureState.RECORDING:
                    if not self.bypass_device_checks:
                        stale = [
                            key
                            for key, age in snapshot.last_seen_age_s.items()
                            if age
                            > float(
                                self.config.get("session", {}).get(
                                    "stale_timeout_s", 1
                                )
                            )
                        ]
                        if stale:
                            self._begin_finalize(
                                False,
                                "录制中数据中断: " + ", ".join(stale),
                            )
                    emergency = int(
                        float(
                            self.config.get("workspace", {}).get(
                                "emergency_stop_free_gib", 2
                            )
                        )
                        * 1024**3
                    )
                    if snapshot.disk_free_bytes < emergency:
                        self._begin_finalize(False, "磁盘剩余空间低于安全阈值")
                    request = (self._manifest or {}).get("request") or {}
                    duration = request.get("duration_s")
                    if (
                        duration
                        and self._recording_started_ns is not None
                        and now - self._recording_started_ns
                        >= float(duration) * 1_000_000_000
                    ):
                        self._begin_finalize(True, None)
                if (
                    self.state in {CaptureState.ARMING, CaptureState.RECORDING}
                    and now - self._last_system_sample_ns >= 1_000_000_000
                ):
                    self._last_system_sample_ns = now
                    self._write_sample(snapshot)
                    anchor = self.clock.anchors[-1]
                    self._write_sample(
                        ClockSyncSample(
                            anchor.monotonic_ns,
                            anchor.unix_ns,
                            anchor.uncertainty_ns,
                            self.clock.offset_jitter_ns(),
                        )
                    )
            if self.callbacks.on_health is not None:
                self.callbacks.on_health(snapshot)

    def _health_is_ready(self, snapshot: HealthSnapshot) -> bool:
        minimum_free = int(
            float(
                self.config.get("workspace", {}).get(
                    "minimum_start_free_gib", 20
                )
            )
            * 1024**3
        )
        if snapshot.disk_free_bytes < minimum_free:
            return False
        if any(age > 1.0 for age in snapshot.last_seen_age_s.values()):
            return False
        oak = self.config.get("oak", {})
        if any(
            snapshot.rates_hz[f"camera/cam_{suffix}"]
            < float(oak.get("minimum_fps", 24))
            for suffix in "abcd"
        ):
            return False
        if snapshot.rates_hz["imu/oak"] < float(
            oak.get("imu", {}).get("minimum_hz", 80)
        ):
            return False
        if snapshot.rates_hz["wearable/emg"] < float(
            self.config.get("wearable", {}).get("emg_minimum_hz", 225)
        ):
            return False
        first_wearable_imu = self._first_seen.get("wearable/imu")
        warmup_s = float(
            self.config.get("wearable", {}).get("imu_warmup_s", 5)
        )
        if (
            first_wearable_imu is None
            or snapshot.stamp.monotonic_ns - first_wearable_imu
            < warmup_s * 1_000_000_000
        ):
            return False
        return snapshot.rates_hz["wearable/imu"] >= 1.0

    def _update_arming(self, sample: Any) -> None:
        key = _sample_key(sample)
        if key is None:
            return
        if isinstance(sample, CameraFrame) and not sample.is_keyframe:
            return
        self._arm_first.setdefault(key, _sample_unix_ns(sample))
        if set(self.REQUIRED_KEYS).issubset(self._arm_first):
            valid_start = max(self._arm_first.values())
            self._recording_started_ns = time.monotonic_ns()
            if self._manifest is not None and self._session_dir is not None:
                self._manifest["status"] = "recording"
                self._manifest["recording_start_unix_ns"] = self.clock.to_unix_ns(
                    self._recording_started_ns
                )
                self._manifest["valid_start_unix_ns"] = valid_start
                atomic_write_json(
                    self._session_dir / "session.json",
                    self._manifest,
                )
            self._set_state(CaptureState.RECORDING, "正在录制全部原始数据")
            self._write_event(
                "info",
                "recording_started",
                "全部模态已建立有效起点",
                {"valid_start_unix_ns": valid_start},
            )

    def _check_sequence(
        self,
        key: str,
        sequence: int,
        modulo: int | None,
    ) -> None:
        if self.bypass_device_checks:
            return
        if self.state not in {CaptureState.ARMING, CaptureState.RECORDING}:
            return
        missing = self._sequence[key].push(sequence, modulo)
        if missing and self.config.get("quality", {}).get(
            "require_zero_sequence_gaps",
            True,
        ):
            self._begin_finalize(
                False,
                f"{key} 序号断裂，缺少 {missing} 个样本",
            )

    def _write_sample(self, sample: Any) -> None:
        writer = self._writer
        if writer is None:
            return
        try:
            writer.submit_sample(sample)
        except RuntimeError as exc:
            self._begin_finalize(False, str(exc))

    def _write_event(
        self,
        level: str,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        error: str | None = None
        with self._event_lock:
            writer = self._writer
            if (
                not self._event_accepting
                or writer is None
                or self._session_dir is None
            ):
                return
            event = SystemEvent(
                level=level,
                code=code,
                message=message,
                stamp=self.clock.stamp(),
                details=details or {},
            )
            try:
                writer.submit_sample(event)
            except RuntimeError as exc:
                error = str(exc)
            self._append_app_log(event)
        if error is not None:
            self._begin_finalize(False, error)

    def _append_app_log(self, event: SystemEvent) -> None:
        if self._session_dir is None:
            return
        path = self._session_dir / "logs" / "app.log"
        payload = {
            "unix_ns": event.stamp.unix_ns,
            "level": event.level,
            "code": event.code,
            "message": event.message,
            "details": event.details,
        }
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())

    def _freeze_event_writes(self) -> None:
        with self._event_lock:
            self._event_accepting = False

    def _begin_finalize(self, requested_success: bool, reason: str | None) -> None:
        with self._lock:
            if self._finalizing or self._writer is None:
                return
            self._finalizing = True
            self._failure_reason = reason
            self._stop_requested_ns = time.monotonic_ns()
            self._set_state(CaptureState.FINALIZING, "正在关闭和校验采集文件")
            self._write_event(
                "error" if reason else "info",
                "recording_failed" if reason else "recording_stop",
                reason or "采集员请求停止录制",
            )
            thread = threading.Thread(
                target=self._finalize,
                args=(requested_success,),
                name="capture-finalizer",
                daemon=True,
            )
            thread.start()

    def _finalize(self, requested_success: bool) -> None:
        final_state = CaptureState.FAILED
        message = self._failure_reason or "采集失败"
        quality: dict[str, Any] | None = None
        segment_checksums: dict[str, str] = {}
        terminal_committed = False
        try:
            writer = self._writer
            session_dir = self._session_dir
            manifest = self._manifest
            if writer is None or session_dir is None or manifest is None:
                raise RuntimeError("录制上下文不完整")
            source_total = max(1, len(self._sources))
            self._emit_finalize_progress(
                "drain_sources",
                0,
                source_total,
                "正在停止设备输入并排空主机接收线程",
            )
            source_errors: list[str] = []
            for index, source in enumerate(self._sources, start=1):
                drain_and_stop = getattr(source, "drain_and_stop", None)
                if callable(drain_and_stop):
                    drain_and_stop()
                else:
                    source.stop()
                if source.running:
                    source_errors.append(
                        f"{type(source).__name__} 接收线程未退出"
                    )
                self._emit_finalize_progress(
                    "drain_sources",
                    index,
                    source_total,
                    f"已停止 {type(source).__name__}",
                )
            with self._ingest_condition:
                self._recording_accepting = False
                deadline = time.monotonic() + 30
                while self._ingest_inflight:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            f"仍有 {self._ingest_inflight} 个样本提交未结束"
                        )
                    self._ingest_condition.wait(timeout=min(0.2, remaining))
            if source_errors:
                raise RuntimeError("; ".join(source_errors))

            self._emit_finalize_progress(
                "writer_barrier",
                0,
                1,
                "正在等待全部已接收样本持久化",
            )
            self._freeze_event_writes()
            segments = writer.stop()
            self._emit_finalize_progress(
                "writer_barrier",
                1,
                1,
                "Writer 屏障完成",
            )
            received_counts = dict(self._received_counts)
            all_accepted_counts = writer.accepted_counts
            all_persisted_counts = writer.persisted_counts
            accepted_counts = {
                key: int(all_accepted_counts.get(key, 0))
                for key in received_counts
            }
            persisted_counts = {
                key: int(all_persisted_counts.get(key, 0))
                for key in received_counts
            }
            count_errors = _count_integrity_errors(
                received_counts,
                accepted_counts,
                persisted_counts,
                writer.sequence_stats,
            )
            if count_errors and self._failure_reason is None:
                self._failure_reason = "; ".join(count_errors)
            manifest["segments"] = segment_dicts(segments)
            manifest["stop_request_unix_ns"] = (
                self.clock.to_unix_ns(self._stop_requested_ns)
                if self._stop_requested_ns is not None
                else None
            )
            manifest["clock_anchors"] = [
                asdict(anchor) for anchor in self.clock.anchors
            ]
            manifest["wearable_imu_baseline_hz"] = self._wearable_imu_baseline_hz
            quality_config = self.config.get("quality", {})
            manifest["online_quality"] = {
                "topic_counts": writer.record_counts,
                "rates_hz": {
                    key: self._rate(key) for key in self.REQUIRED_KEYS
                },
                "sequence_gaps": {
                    key: tracker.gaps
                    for key, tracker in self._sequence.items()
                },
                "camera_sync": _camera_sync(
                    dict(self._online_camera_times),
                    max_window_ms=float(
                        quality_config.get("camera_sync_max_ms", 5)
                    ),
                ),
                "writer_high_watermark": writer.high_watermark,
                "writer_queue_drops": writer.queue_drops,
            }
            manifest["counts"] = {
                "received": received_counts,
                "writer_accepted": accepted_counts,
                "persisted": persisted_counts,
                "auxiliary_writer_accepted": {
                    key: value
                    for key, value in all_accepted_counts.items()
                    if key not in received_counts
                },
                "auxiliary_persisted": {
                    key: value
                    for key, value in all_persisted_counts.items()
                    if key not in received_counts
                },
                "received_first_sequence": dict(
                    self._received_first_sequence
                ),
                "received_last_sequence": dict(self._received_last_sequence),
                "persisted_sequence": writer.sequence_stats,
                "writer_high_watermark": writer.high_watermark,
                "writer_queue_drops": writer.queue_drops,
            }
            manifest["status"] = "finalizing"
            self._emit_finalize_progress(
                "manifest",
                0,
                1,
                "正在写入收尾清单",
            )
            atomic_write_json(session_dir / "session.json", manifest)
            self._emit_finalize_progress(
                "manifest",
                1,
                1,
                "收尾清单已落盘",
            )
            self._emit_finalize_progress(
                "validate",
                0,
                max(1, len(segments)),
                "正在单遍校验 MCAP CRC、计数和 SHA-256",
            )
            quality = build_quality_report(session_dir, self.config)
            segment_checksums = dict(quality.pop("_segment_checksums", {}))
            writer_queue_drops = max(
                int(quality.get("queue_drops", 0)),
                writer.queue_drops,
            )
            quality["queue_drops"] = writer_queue_drops
            if (
                writer_queue_drops
                and quality_config.get("require_zero_queue_drops", True)
                and not any(
                    "写入队列丢弃" in error
                    for error in quality.get("errors", [])
                )
            ):
                quality["errors"] = [
                    *(quality.get("errors") or []),
                    f"应用写入队列丢弃 {writer_queue_drops} 条消息",
                ]
                quality["pass"] = False
            if count_errors:
                quality["errors"] = [
                    *(quality.get("errors") or []),
                    *count_errors,
                ]
                quality["pass"] = False
            self._emit_finalize_progress(
                "validate",
                max(1, len(segments)),
                max(1, len(segments)),
                "MCAP 单遍质检完成",
            )
            passed = requested_success and self._failure_reason is None and quality["pass"]
            if passed:
                final_state = CaptureState.COMPLETED
                message = "录制完成，全部数据通过完整性检查"
                manifest["status"] = "completed"
                manifest["failure_reason"] = None
            else:
                final_state = CaptureState.FAILED
                quality_reason = "; ".join(quality.get("errors") or [])
                message = self._failure_reason or quality_reason or "采集未通过质检"
                manifest["status"] = "failed"
                manifest["failure_reason"] = message
            manifest["quality"] = quality
            manifest["completed_unix_ns"] = time.time_ns()
            self._emit_finalize_progress(
                "checksums",
                0,
                1,
                "正在写入校验和清单",
            )
            commit_session_artifacts(
                session_dir,
                manifest,
                precomputed=segment_checksums,
            )
            terminal_committed = True
            self._emit_finalize_progress(
                "checksums",
                1,
                1,
                "校验和清单已写入",
            )
        except Exception as exc:
            if terminal_committed:
                self._append_global_log(
                    "error",
                    "post_commit_finalize_callback",
                    f"{type(exc).__name__}: {exc}",
                )
            else:
                message = f"采集收尾失败: {type(exc).__name__}: {exc}"
                if self._manifest is not None and self._session_dir is not None:
                    self._manifest["status"] = "failed"
                    self._manifest["failure_reason"] = message
                    self._manifest["quality"] = quality
                    self._manifest["completed_unix_ns"] = time.time_ns()
                    try:
                        (self._session_dir / "checksums.sha256").unlink(
                            missing_ok=True
                        )
                        atomic_write_json(
                            self._session_dir / "session.json",
                            self._manifest,
                        )
                    except OSError:
                        pass
                final_state = CaptureState.FAILED
        finally:
            self._freeze_event_writes()
            with self._lock:
                self._writer = None
                self._recording_accepting = False
                self._finalizing = False
                self._set_state(final_state, message)
                if self.callbacks.on_session is not None:
                    self.callbacks.on_session(self._session_dir, self._manifest)
                self._healthy_since_ns = None
            self._emit_finalize_progress(
                "complete",
                1,
                1,
                message,
            )

    def _emit_finalize_progress(
        self,
        stage: str,
        completed: int,
        total: int,
        message: str,
    ) -> None:
        callback = self.callbacks.on_finalize_progress
        if callback is not None:
            callback(
                FinalizeProgress(
                    stage=stage,
                    completed=completed,
                    total=total,
                    message=message,
                )
            )

    def _source_error(self, code: str, message: str) -> None:
        self._append_global_log("error", code, message)
        with self._lock:
            if self.bypass_device_checks:
                if self.state in {
                    CaptureState.ARMING,
                    CaptureState.RECORDING,
                }:
                    self._write_event(
                        "warning",
                        code,
                        message,
                    )
                return
            if self.state in {CaptureState.ARMING, CaptureState.RECORDING}:
                self._begin_finalize(False, f"{code}: {message}")
            elif code in {"wearable_parser_discard", "wearable_invalid_frame"}:
                self._set_state(CaptureState.CHECKING, message)
            else:
                self._set_state(CaptureState.CHECKING, f"设备错误: {message}")

    def _source_metadata(self, name: str, values: dict[str, Any]) -> None:
        with self._lock:
            self._metadata[name] = values

    def _append_global_log(
        self,
        level: str,
        code: str,
        message: str,
    ) -> None:
        path = self._output_root / "logs" / "capture.log"
        payload = {
            "unix_ns": time.time_ns(),
            "level": level,
            "code": code,
            "message": message,
        }
        try:
            with self._global_log_lock:
                if path.exists() and path.stat().st_size >= 5 * 1024**2:
                    rotated = path.with_suffix(".log.1")
                    os.replace(path, rotated)
                with path.open("a", encoding="utf-8") as file:
                    file.write(
                        json.dumps(
                            payload,
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                    )
                    file.write("\n")
        except OSError:
            pass

    def _rate(self, key: str) -> float:
        if key.startswith("wearable/"):
            return self._arrival_rate(key)
        values = self._rate_windows.get(key)
        if values is None or len(values) < 2:
            return 0.0
        now = time.monotonic_ns()
        cutoff = now - 5_000_000_000
        while values and values[0] < cutoff:
            values.popleft()
        if len(values) < 2:
            return 0.0
        duration = (values[-1] - values[0]) / 1_000_000_000
        return (len(values) - 1) / duration if duration > 0 else 0.0

    def _arrival_rate(self, key: str) -> float:
        values = self._arrival_rate_windows.get(key)
        if values is None or len(values) < 2:
            return 0.0
        now = time.monotonic_ns()
        cutoff = now - 5_000_000_000
        while values and values[0] < cutoff:
            values.popleft()
        if len(values) < 2:
            return 0.0
        first_arrival = self._first_arrival_seen.get(key, values[0])
        window_start = max(cutoff, first_arrival)
        duration = (now - window_start) / 1_000_000_000
        return len(values) / duration if duration > 0 else 0.0

    def _start_sources(self) -> None:
        sources_to_start = [
            source for source in self._sources if not source.running
        ]
        if not sources_to_start:
            return
        oak = next(
            (
                source
                for source in sources_to_start
                if isinstance(source, OakSource)
            ),
            None,
        )
        wearable = next(
            (
                source
                for source in sources_to_start
                if isinstance(source, WearableSource)
            ),
            None,
        )
        for source in sources_to_start:
            if source is not wearable:
                source.start()
        if wearable is None:
            return
        if oak is None:
            wearable.start()
            return

        def start_wearable_when_oak_is_ready() -> None:
            try:
                while not self._monitor_stop.is_set():
                    if oak.wait_until_ready(0.2):
                        if not self._monitor_stop.is_set():
                            wearable.start()
                        return
            finally:
                if self._source_start_thread is threading.current_thread():
                    self._source_start_thread = None

        self._source_start_thread = threading.Thread(
            target=start_wearable_when_oak_is_ready,
            name="source-start-coordinator",
            daemon=True,
        )
        self._source_start_thread.start()

    def _set_state(self, state: CaptureState, message: str) -> None:
        self.state = state
        self.state_message = message
        callback = self.callbacks.on_state
        if callback is not None:
            callback(state, message)


def _sample_key(sample: Any) -> str | None:
    if isinstance(sample, CameraFrame):
        return f"camera/{sample.camera}"
    if isinstance(sample, OakImuSample):
        return "imu/oak"
    if isinstance(sample, WearableEmgSample):
        return "wearable/emg"
    if isinstance(sample, WearableImuSample):
        return "wearable/imu"
    if isinstance(sample, WearableRawChunk):
        return "wearable/raw"
    return None


def _sample_monotonic_ns(sample: Any) -> int:
    if isinstance(sample, WearableRawChunk):
        return sample.read_end_monotonic_ns
    return int(sample.stamp.monotonic_ns)


def _sample_arrival_monotonic_ns(sample: Any) -> int:
    if isinstance(sample, WearableRawChunk):
        return sample.read_end_monotonic_ns
    arrival_ns = sample.stamp.arrival_monotonic_ns
    return time.monotonic_ns() if arrival_ns is None else int(arrival_ns)


def _sample_unix_ns(sample: Any) -> int:
    if isinstance(sample, WearableRawChunk):
        return sample.unix_ns
    return int(sample.stamp.unix_ns)


def _sample_sequence(sample: Any) -> int | None:
    if isinstance(
        sample,
        (CameraFrame, WearableEmgSample, WearableImuSample),
    ):
        return int(sample.sequence)
    return None


def _disk_free_bytes(path: Path) -> int:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return shutil.disk_usage(candidate).free


def _count_integrity_errors(
    received: dict[str, int],
    accepted: dict[str, int],
    persisted: dict[str, int],
    sequence_stats: dict[str, dict[str, int | None]],
) -> list[str]:
    errors: list[str] = []
    for key, received_count in sorted(received.items()):
        accepted_count = int(accepted.get(key, 0))
        persisted_count = int(persisted.get(key, 0))
        if received_count != accepted_count or received_count != persisted_count:
            errors.append(
                f"{key} 计数不一致: received={received_count}, "
                f"accepted={accepted_count}, persisted={persisted_count}"
            )
    for camera in ("cam_a", "cam_b", "cam_c", "cam_d"):
        key = f"camera/{camera}"
        stats = sequence_stats.get(key) or {}
        gaps = int(stats.get("sequence_gaps") or 0)
        duplicate = int(stats.get("duplicate_or_reordered") or 0)
        if gaps or duplicate:
            errors.append(
                f"{key} 持久化序号异常: gaps={gaps}, "
                f"duplicate_or_reordered={duplicate}"
            )
    return errors
