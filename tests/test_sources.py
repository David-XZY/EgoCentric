from __future__ import annotations

import queue
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from egocentric_capture.clocks import HostClockMapper
from egocentric_capture.oak_runtime import depthai_runtime
from egocentric_capture.oak_runtime.depthai_runtime import get_queue_output
from egocentric_capture.sources import wearable
from egocentric_capture.sources.base import SourceCallbacks
from egocentric_capture.sources.oak import OakSource
from egocentric_capture.sources.wearable import (
    _sequence_discontinuity,
    discover_serial_port,
)


def test_depthai_host_queue_is_blocking() -> None:
    calls: list[dict[str, object]] = []

    class Output:
        def createOutputQueue(self, **kwargs: object) -> object:
            calls.append(kwargs)
            return object()

    get_queue_output(Output(), 90)
    assert calls == [{"maxSize": 90, "blocking": True}]


def test_missing_depthai_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "depthai", None)
    with pytest.raises(RuntimeError, match="未安装 depthai"):
        depthai_runtime.import_depthai()


def test_missing_configured_mxid_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = SimpleNamespace()
    monkeypatch.setattr(
        depthai_runtime,
        "enumerate_device_infos",
        lambda _dai: [device],
    )
    monkeypatch.setattr(
        depthai_runtime,
        "device_info_summary",
        lambda _device: {"mxid": "actual"},
    )

    with pytest.raises(RuntimeError, match="未找到 MXID=expected"):
        depthai_runtime.select_device_info(SimpleNamespace(), "expected")


def test_oak_supervisor_reports_initialization_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    errors: list[tuple[str, str]] = []
    source = OakSource(
        {},
        HostClockMapper(),
        SourceCallbacks(
            on_sample=lambda _sample: None,
            on_error=lambda code, message: (
                errors.append((code, message)),
                source._stop_event.set(),
            ),
        ),
    )
    monkeypatch.setattr(
        depthai_runtime,
        "import_depthai",
        lambda: (_ for _ in ()).throw(RuntimeError("初始化失败")),
    )
    monkeypatch.setattr(
        "egocentric_capture.sources.oak.import_depthai",
        depthai_runtime.import_depthai,
    )

    source.start()
    assert source._thread is not None
    source._thread.join(timeout=1)

    assert errors == [("oak_source", "RuntimeError: 初始化失败")]
    assert not source.running
def test_serial_discovery_filters_cp210x(monkeypatch: pytest.MonkeyPatch) -> None:
    ports = [
        SimpleNamespace(
            device="/dev/ttyUSB0",
            vid=0x1234,
            pid=0x5678,
            serial_number="other",
        ),
        SimpleNamespace(
            device="/dev/ttyUSB1",
            vid=0x10C4,
            pid=0xEA60,
            serial_number="bracelet",
        ),
    ]
    monkeypatch.setattr(wearable.list_ports, "comports", lambda: ports)
    monkeypatch.setattr(wearable, "_serial_by_id_path", lambda _device: None)
    assert discover_serial_port() == "/dev/ttyUSB1"


def test_multiple_serial_devices_require_serial_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ports = [
        SimpleNamespace(
            device=f"/dev/ttyUSB{index}",
            vid=0x10C4,
            pid=0xEA60,
            serial_number=f"bracelet-{index}",
        )
        for index in range(2)
    ]
    monkeypatch.setattr(wearable.list_ports, "comports", lambda: ports)
    with pytest.raises(RuntimeError, match="serial_number"):
        discover_serial_port()


def test_wearable_sequence_discontinuity_handles_wrap_and_loss() -> None:
    assert _sequence_discontinuity(None, 10) == (0, 0)
    assert _sequence_discontinuity(254, 255) == (0, 0)
    assert _sequence_discontinuity(255, 0) == (0, 0)
    assert _sequence_discontinuity(10, 13) == (2, 0)
    assert _sequence_discontinuity(10, 10) == (0, 1)
    assert _sequence_discontinuity(10, 9) == (0, 1)


def test_oak_imu_uses_independent_consumer_and_drains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MessageQueue:
        def __init__(self) -> None:
            self._lock = threading.Lock()
            self._messages: list[object] = []

        def push(self, message: object) -> None:
            with self._lock:
                self._messages.append(message)

        def tryGetAll(self) -> list[object]:
            with self._lock:
                messages = list(self._messages)
                self._messages.clear()
                return messages

    source = OakSource(
        {},
        HostClockMapper(),
        SourceCallbacks(
            on_sample=lambda _sample: None,
            on_error=lambda _code, _message: None,
        ),
    )
    message_queue = MessageQueue()
    processed: list[object] = []
    monkeypatch.setattr(source, "_ingest_imu_message", processed.append)
    consumer_stop = threading.Event()
    producer_stopped = threading.Event()
    failures: queue.Queue[Exception] = queue.Queue(maxsize=1)
    thread = threading.Thread(
        target=source._consume_imu_queue,
        args=(
            message_queue,
            consumer_stop,
            producer_stopped,
            failures,
        ),
    )
    thread.start()
    first = object()
    second = object()
    message_queue.push(first)
    message_queue.push(second)
    deadline = time.monotonic() + 1
    while len(processed) < 2 and time.monotonic() < deadline:
        time.sleep(0.001)
    source._drain_event.set()
    producer_stopped.set()
    thread.join(timeout=1)

    assert processed == [first, second]
    assert not thread.is_alive()
    assert failures.empty()


def test_oak_imu_consumer_reports_processing_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MessageQueue:
        def __init__(self) -> None:
            self._returned = False

        def tryGetAll(self) -> list[object]:
            if self._returned:
                return []
            self._returned = True
            return [object()]

    source = OakSource(
        {},
        HostClockMapper(),
        SourceCallbacks(
            on_sample=lambda _sample: None,
            on_error=lambda _code, _message: None,
        ),
    )

    def fail(_message: object) -> None:
        raise ValueError("imu failure")

    monkeypatch.setattr(source, "_ingest_imu_message", fail)
    failures: queue.Queue[Exception] = queue.Queue(maxsize=1)
    source._consume_imu_queue(
        MessageQueue(),
        threading.Event(),
        threading.Event(),
        failures,
    )

    with pytest.raises(RuntimeError, match="OAK IMU 接收线程异常"):
        source._raise_imu_failure(failures)
