from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import CaptureRequest


class TrialIndex:
    """在内存中维护已完成轮次，并在开始录制前重新核对冲突。"""

    def __init__(self, output_root: Path) -> None:
        self.output_root = Path(output_root)
        self._lock = threading.Lock()
        self._trials: dict[tuple[str, str], int] = {}

    def refresh(self) -> None:
        values: dict[tuple[str, str], int] = {}
        if self.output_root.exists():
            for manifest_path in self.output_root.glob("*/session.json"):
                try:
                    payload = read_json(manifest_path)
                except (OSError, ValueError):
                    continue
                if payload.get("status") != "completed":
                    continue
                request = payload.get("request") or {}
                key = self._key(
                    str(request.get("participant_id", "")),
                    str(request.get("task_id", "")),
                )
                values[key] = max(
                    values.get(key, 0),
                    int(payload.get("trial", 0)),
                )
        with self._lock:
            self._trials = values

    def next(self, participant_id: str, task_id: str) -> int:
        key = self._key(participant_id, task_id)
        with self._lock:
            return self._trials.get(key, 0) + 1

    def reserve(self, participant_id: str, task_id: str) -> int:
        key = self._key(participant_id, task_id)
        with self._lock:
            maximum = self._scan_pair(key)
            trial = maximum + 1
            self._trials[key] = trial
            return trial

    def _scan_pair(self, key: tuple[str, str]) -> int:
        maximum = 0
        if not self.output_root.exists():
            return maximum
        for manifest_path in self.output_root.glob("*/session.json"):
            try:
                payload = read_json(manifest_path)
            except (OSError, ValueError):
                continue
            request = payload.get("request") or {}
            if payload.get("status") == "completed" and self._key(
                str(request.get("participant_id", "")),
                str(request.get("task_id", "")),
            ) == key:
                maximum = max(maximum, int(payload.get("trial", 0)))
        return maximum

    @staticmethod
    def _key(participant_id: str, task_id: str) -> tuple[str, str]:
        return (
            slugify(participant_id, "participant"),
            slugify(task_id, "task"),
        )


def slugify(value: str, fallback: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-")
    return normalized or fallback


def next_trial(output_root: Path, participant_id: str, task_id: str) -> int:
    participant = slugify(participant_id, "participant")
    task = slugify(task_id, "task")
    maximum = 0
    if not output_root.exists():
        return 1
    for manifest_path in output_root.glob("*/session.json"):
        try:
            payload = read_json(manifest_path)
        except (OSError, ValueError):
            continue
        request = payload.get("request") or {}
        if (
            payload.get("status") == "completed"
            and slugify(str(request.get("participant_id", "")), "participant")
            == participant
            and slugify(str(request.get("task_id", "")), "task") == task
        ):
            maximum = max(maximum, int(payload.get("trial", 0)))
    return maximum + 1


def create_session_dir(
    output_root: Path,
    request: CaptureRequest,
    trial: int,
    now: datetime | None = None,
) -> Path:
    timestamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    participant = slugify(request.participant_id, "participant")
    task = slugify(request.task_id, "task")
    base = output_root / f"{timestamp}_{participant}_{task}_r{trial:02d}"
    candidate = base
    suffix = 1
    while candidate.exists():
        candidate = output_root / f"{base.name}_{suffix:02d}"
        suffix += 1
    candidate.mkdir(parents=True, exist_ok=False)
    (candidate / "segments").mkdir()
    (candidate / "logs").mkdir()
    return candidate


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_bytes(path, _json_bytes(payload))


def commit_session_artifacts(
    session_dir: Path,
    manifest: dict[str, Any],
    *,
    precomputed: dict[str, str] | None = None,
) -> Path:
    """先发布校验和，最后原子发布终态 manifest。"""
    session_dir = Path(session_dir)
    manifest_bytes = _json_bytes(manifest)
    checksums = dict(precomputed or {})
    checksums["session.json"] = hashlib.sha256(manifest_bytes).hexdigest()
    checksum_path = session_dir / "checksums.sha256"
    try:
        write_checksums(session_dir, precomputed=checksums)
        _atomic_write_bytes(session_dir / "session.json", manifest_bytes)
    except Exception:
        checksum_path.unlink(missing_ok=True)
        _fsync_directory(session_dir)
        raise
    return checksum_path


def _json_bytes(payload: dict[str, Any]) -> bytes:
    value = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    return (value + "\n").encode("utf-8")


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} 顶层必须是 JSON 对象")
    return payload


def write_checksums(
    session_dir: Path,
    precomputed: dict[str, str] | None = None,
) -> Path:
    output = session_dir / "checksums.sha256"
    files = sorted(
        path
        for path in session_dir.rglob("*")
        if path.is_file()
        and path != output
        and not path.name.startswith(".")
    )
    lines = []
    for path in files:
        relative = str(path.relative_to(session_dir))
        checksum = (precomputed or {}).get(relative)
        if checksum is None:
            checksum = _sha256_file(path)
        lines.append(f"{checksum}  {relative}\n")
    _atomic_write_bytes(output, "".join(lines).encode("utf-8"))
    return output


def verify_checksums(
    session_dir: Path,
    *,
    precomputed: dict[str, str] | None = None,
) -> list[str]:
    checksum_path = session_dir / "checksums.sha256"
    if not checksum_path.exists():
        return ["缺少 checksums.sha256"]
    errors: list[str] = []
    with checksum_path.open(encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            value = line.rstrip("\n")
            if not value:
                continue
            try:
                expected, relative = value.split("  ", 1)
            except ValueError:
                errors.append(f"校验和第 {line_number} 行格式错误")
                continue
            path = session_dir / relative
            if not path.exists():
                errors.append(f"缺少文件: {relative}")
                continue
            digest = (precomputed or {}).get(relative)
            if digest is None:
                digest = _sha256_file(path)
            if digest != expected:
                errors.append(f"校验和不匹配: {relative}")
    return errors


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
