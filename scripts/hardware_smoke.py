from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from egocentric_capture.config import load_config
from egocentric_capture.models import CaptureRequest, CaptureState
from egocentric_capture.session import CaptureEngine, EngineCallbacks
from egocentric_capture.storage import read_json


def main() -> int:
    parser = argparse.ArgumentParser(description="真实硬件短采集验收")
    parser.add_argument("--duration", type=float, default=10)
    parser.add_argument("--output", type=Path, default=Path("/tmp/egocap-hardware"))
    arguments = parser.parse_args()

    config = load_config()
    config["workspace"]["output_root"] = str(arguments.output)
    config["workspace"]["minimum_start_free_gib"] = 0
    config["workspace"]["emergency_stop_free_gib"] = 0
    config["session"]["healthy_before_ready_s"] = 2

    latest_rates: dict[str, float] = {}

    def on_state(state: CaptureState, message: str) -> None:
        print(f"状态: {state.value} - {message}", flush=True)

    def on_health(snapshot: object) -> None:
        nonlocal latest_rates
        latest_rates = dict(getattr(snapshot, "rates_hz", {}))

    engine = CaptureEngine(
        config,
        simulate=False,
        callbacks=EngineCallbacks(on_state=on_state, on_health=on_health),
    )
    engine.start()
    try:
        deadline = time.monotonic() + 45
        while engine.state != CaptureState.READY and time.monotonic() < deadline:
            time.sleep(1)
            print(
                "实时速率: "
                + json.dumps(latest_rates, ensure_ascii=False, sort_keys=True),
                flush=True,
            )
        if engine.state != CaptureState.READY:
            raise RuntimeError(
                f"设备未在限定时间内就绪: {engine.state.value} "
                f"{engine.state_message}"
            )
        session = engine.request_record(
            CaptureRequest(
                participant_id="HARDWARE",
                task_id="smoke_test",
                hand="right",
                operator="codex",
                notes="真实设备自动短采集验收",
                duration_s=arguments.duration,
            )
        )
        deadline = time.monotonic() + arguments.duration + 45
        while (
            engine.state not in {CaptureState.COMPLETED, CaptureState.FAILED}
            and time.monotonic() < deadline
        ):
            time.sleep(1)
            print(
                "录制速率: "
                + json.dumps(latest_rates, ensure_ascii=False, sort_keys=True),
                flush=True,
            )
        manifest = read_json(session / "session.json")
        print(json.dumps(manifest, ensure_ascii=False, indent=2), flush=True)
        return 0 if manifest.get("status") == "completed" else 1
    finally:
        engine.stop()


if __name__ == "__main__":
    raise SystemExit(main())
