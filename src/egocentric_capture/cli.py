from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .config import load_config
from .exporters import export_session
from .mcap_io import recover_mcap, validate_mcap
from .quality import build_quality_report
from .storage import read_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="egocentric-capture",
        description="统一多模态原始数据采集工具",
    )
    parser.add_argument("--config", type=Path, help="附加 YAML 配置文件")
    commands = parser.add_subparsers(dest="command")

    gui = commands.add_parser("gui", help="启动采集界面")
    gui.add_argument("--simulate", action="store_true", help="使用模拟设备")

    inspect_parser = commands.add_parser("inspect", help="查看 session 摘要")
    inspect_parser.add_argument("session", type=Path)
    inspect_parser.add_argument("--json", action="store_true")

    validate = commands.add_parser("validate", help="校验 session")
    validate.add_argument("session", type=Path)
    validate.add_argument("--json", action="store_true")

    recover = commands.add_parser("recover", help="恢复截断的 MCAP 分段")
    recover.add_argument("target", type=Path)
    recover.add_argument("-o", "--output", type=Path)

    export = commands.add_parser("export", help="导出后处理格式")
    export.add_argument("session", type=Path)
    export.add_argument(
        "--format",
        required=True,
        choices=("mp4", "parquet", "numpy"),
    )
    export.add_argument("-o", "--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    command = arguments.command or "gui"
    try:
        config = load_config(arguments.config)
        if command == "gui":
            from .gui import run_gui

            return run_gui(config, simulate=bool(getattr(arguments, "simulate", False)))
        if command == "inspect":
            return _inspect(arguments.session, arguments.json)
        if command == "validate":
            return _validate(arguments.session, config, arguments.json)
        if command == "recover":
            return _recover(arguments.target, arguments.output)
        if command == "export":
            output = arguments.output or arguments.session / "exports" / arguments.format
            paths = export_session(arguments.session, output, arguments.format)
            for path in paths:
                print(path)
            return 0
    except Exception as exc:
        print(f"错误: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    parser.print_help()
    return 1


def _inspect(session_dir: Path, json_output: bool) -> int:
    manifest = read_json(session_dir / "session.json")
    segments = [
        validate_mcap(path)
        for path in sorted((session_dir / "segments").glob("*.mcap"))
    ]
    summary: dict[str, Any] = {
        "session": str(session_dir),
        "status": manifest.get("status"),
        "request": manifest.get("request"),
        "trial": manifest.get("trial"),
        "failure_reason": manifest.get("failure_reason"),
        "segments": segments,
        "quality": manifest.get("quality"),
    }
    if json_output:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        request = summary["request"] or {}
        print(f"状态: {summary['status']}")
        print(
            "采集: "
            f"{request.get('participant_id', '-')} / "
            f"{request.get('task_id', '-')} / r{int(summary['trial'] or 0):02d}"
        )
        print(
            f"分段: {len(segments)}，消息: "
            f"{sum(int(item['message_count']) for item in segments)}"
        )
        quality = summary["quality"] or {}
        print(f"质检: {'通过' if quality.get('pass') else '未通过'}")
        if summary["failure_reason"]:
            print(f"原因: {summary['failure_reason']}")
    return 0


def _validate(session_dir: Path, config: dict[str, Any], json_output: bool) -> int:
    report = build_quality_report(session_dir, config, verify_hashes=True)
    if json_output:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("通过" if report["pass"] else "未通过")
        for error in report["errors"]:
            print(f"- {error}")
    return 0 if report["pass"] else 1


def _recover(target: Path, output: Path | None) -> int:
    if target.is_dir():
        candidates = sorted((target / "segments").glob("*.mcap"))
        if not candidates:
            raise FileNotFoundError("session 中没有 MCAP 分段")
        source = candidates[-1]
    else:
        source = target
    destination = output or source.with_name(f"{source.stem}.recovered.mcap")
    report = recover_mcap(source, destination)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["recovered_messages"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
