from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

import pytest

import egocentric_capture.storage as storage_module
from egocentric_capture.models import CaptureRequest
from egocentric_capture.storage import (
    TrialIndex,
    atomic_write_json,
    commit_session_artifacts,
    create_session_dir,
    next_trial,
    read_json,
    verify_checksums,
    write_checksums,
)


def test_atomic_manifest_and_completed_trial_numbering(tmp_path) -> None:
    request = CaptureRequest("P 01", "grasp/cup", "right", "operator")
    failed = create_session_dir(
        tmp_path,
        request,
        1,
        datetime(2026, 8, 6, 12, 0, 0),
    )
    atomic_write_json(
        failed / "session.json",
        {"status": "failed", "request": request.__dict__ if hasattr(request, "__dict__") else {
            "participant_id": request.participant_id,
            "task_id": request.task_id,
        }, "trial": 1},
    )
    completed = create_session_dir(
        tmp_path,
        request,
        1,
        datetime(2026, 8, 6, 12, 0, 1),
    )
    atomic_write_json(
        completed / "session.json",
        {
            "status": "completed",
            "request": {
                "participant_id": request.participant_id,
                "task_id": request.task_id,
            },
            "trial": 1,
        },
    )
    assert read_json(completed / "session.json")["status"] == "completed"
    assert next_trial(tmp_path, "P 01", "grasp/cup") == 2


def test_checksums_detect_modified_file(tmp_path) -> None:
    (tmp_path / "segments").mkdir()
    payload = tmp_path / "segments" / "0000.mcap"
    payload.write_bytes(b"abc")
    write_checksums(tmp_path)
    assert verify_checksums(tmp_path) == []
    payload.write_bytes(b"changed")
    assert verify_checksums(tmp_path) == ["校验和不匹配: segments/0000.mcap"]


def test_terminal_manifest_is_published_after_checksums(tmp_path) -> None:
    (tmp_path / "segments").mkdir()
    (tmp_path / "logs").mkdir()
    (tmp_path / "segments" / "0000.mcap").write_bytes(b"segment")
    atomic_write_json(tmp_path / "session.json", {"status": "finalizing"})

    commit_session_artifacts(
        tmp_path,
        {"status": "completed", "quality": {"pass": True}},
    )

    assert read_json(tmp_path / "session.json")["status"] == "completed"
    assert verify_checksums(tmp_path) == []


def test_terminal_manifest_failure_never_leaves_completed_session(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "segments").mkdir()
    (tmp_path / "logs").mkdir()
    atomic_write_json(tmp_path / "session.json", {"status": "finalizing"})
    original = storage_module._atomic_write_bytes

    def fail_manifest(path: Path, payload: bytes) -> None:
        if path.name == "session.json":
            raise OSError("manifest publish failed")
        original(path, payload)

    monkeypatch.setattr(storage_module, "_atomic_write_bytes", fail_manifest)

    with pytest.raises(OSError, match="manifest publish failed"):
        commit_session_artifacts(tmp_path, {"status": "completed"})

    assert read_json(tmp_path / "session.json")["status"] == "finalizing"
    assert not (tmp_path / "checksums.sha256").exists()


def test_checksum_verification_streams_files(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"streamed checksum")
    write_checksums(tmp_path)

    def reject_read_bytes(_path: Path) -> bytes:
        raise AssertionError("校验不应整体读取文件")

    monkeypatch.setattr(Path, "read_bytes", reject_read_bytes)

    assert verify_checksums(tmp_path) == []


def test_checksum_verification_reuses_precomputed_digest(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "segments").mkdir()
    segment = tmp_path / "segments" / "0000.mcap"
    segment.write_bytes(b"precomputed")
    digest = hashlib.sha256(b"precomputed").hexdigest()
    write_checksums(
        tmp_path,
        precomputed={"segments/0000.mcap": digest},
    )

    def reject_hash(path: Path) -> str:
        if path == segment:
            raise AssertionError("MCAP 分段不应重复读取")
        return hashlib.sha256(path.read_bytes()).hexdigest()

    monkeypatch.setattr(storage_module, "_sha256_file", reject_hash)

    assert verify_checksums(
        tmp_path,
        precomputed={"segments/0000.mcap": digest},
    ) == []


def test_trial_index_uses_memory_and_rechecks_before_reserve(tmp_path) -> None:
    request = CaptureRequest("P01", "task", "right", "operator")
    session = create_session_dir(
        tmp_path,
        request,
        2,
        datetime(2026, 8, 6, 12, 0, 2),
    )
    atomic_write_json(
        session / "session.json",
        {
            "status": "completed",
            "request": {
                "participant_id": "P01",
                "task_id": "task",
            },
            "trial": 2,
        },
    )
    index = TrialIndex(tmp_path)
    index.refresh()
    assert index.next("P01", "task") == 3
    assert index.reserve("P01", "task") == 3
    assert index.next("P01", "task") == 4
