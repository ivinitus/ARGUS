from __future__ import annotations

import json
from pathlib import Path

import pytest

from argus.operations import audit_lock


def test_acquire_release_creates_and_removes_lock(tmp_path: Path):
    root = tmp_path / "out"

    with audit_lock.acquire_key(root, "QA-E1", owner="test") as lock:
        assert lock.path == root / "_audit_locks" / "QA-E1.lock"
        assert lock.path.is_dir()
        metadata = json.loads((lock.path / "owner.json").read_text())
        assert metadata["key"] == "QA-E1"
        assert metadata["owner"] == "test"
        assert isinstance(metadata["pid"], int)

    assert not lock.path.exists()


def test_second_acquire_fails_while_lock_is_active(tmp_path: Path):
    root = tmp_path / "out"

    with audit_lock.acquire_key(root, "QA-E1", owner="first"):
        with pytest.raises(audit_lock.AuditLockBusy) as exc:
            audit_lock.acquire_key(root, "QA-E1", owner="second")

    assert "already auditing QA-E1" in str(exc.value)
    assert "first" in str(exc.value)


def test_stale_lock_with_dead_pid_is_reclaimed(tmp_path: Path):
    root = tmp_path / "out"
    lock_path = audit_lock.key_lock_path(root, "QA-E1")
    lock_path.mkdir(parents=True)
    (lock_path / "owner.json").write_text(json.dumps({
        "key": "QA-E1",
        "owner": "dead-process",
        "pid": 999999999,
        "started_epoch": 1,
        "token": "old-token",
    }))

    with audit_lock.acquire_key(root, "QA-E1", owner="new") as lock:
        metadata = json.loads((lock.path / "owner.json").read_text())
        assert metadata["owner"] == "new"
        assert metadata["token"] != "old-token"


def test_release_does_not_remove_lock_reacquired_by_other_owner(
        tmp_path: Path):
    root = tmp_path / "out"
    first = audit_lock.acquire_key(root, "QA-E1", owner="first")
    first_meta = audit_lock.read_lock_metadata(first.path)
    first.path.joinpath("owner.json").write_text(json.dumps({
        **first_meta,
        "token": "someone-else",
    }))

    first.release()

    assert first.path.exists()
