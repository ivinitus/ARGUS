"""Per-execution audit locks."""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_STALE_AFTER_SECONDS = 6 * 3600
_SAFE_KEY_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_OWNER_FILE = "owner.json"
_ownership_lost = False


class LeaseOwnershipLost(RuntimeError):
    """The worker has lost ownership of its audit lock."""


class AuditLockBusy(Exception):
    """Raised when an audit lock is held by a live/non-stale owner."""

    def __init__(self, key: str, path: Path, metadata: dict[str, Any]):
        self.key = key
        self.path = path
        self.metadata = metadata
        super().__init__(format_busy_message(key, metadata))


@dataclass
class AuditLock:
    path: Path
    key: str
    token: str
    metadata: dict[str, Any]
    _released: bool = False

    def __enter__(self) -> "AuditLock":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        current = read_lock_metadata(self.path)
        if current.get("token") != self.token:
            return
        try:
            shutil.rmtree(self.path)
        except FileNotFoundError:
            pass


def reset() -> None:
    global _ownership_lost
    _ownership_lost = False


def mark_ownership_lost(_key: str) -> None:
    global _ownership_lost
    _ownership_lost = True


def ensure_ownership() -> None:
    if _ownership_lost:
        raise LeaseOwnershipLost(
            "audit lock ownership was lost; refusing further work"
        )


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_key(key: str) -> str:
    safe = _SAFE_KEY_RE.sub("_", key).strip("._")
    return safe or "unknown"


def key_lock_path(output_root: Path, key: str) -> Path:
    return output_root / "_audit_locks" / f"{_safe_key(key)}.lock"


def read_lock_metadata(lock_path: Path) -> dict[str, Any]:
    try:
        raw = json.loads((lock_path / _OWNER_FILE).read_text())
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _lock_age_seconds(lock_path: Path, metadata: dict[str, Any]) -> float | None:
    started_epoch = metadata.get("started_epoch")
    if isinstance(started_epoch, (int, float)):
        return max(0.0, time.time() - float(started_epoch))
    try:
        return max(0.0, time.time() - lock_path.stat().st_mtime)
    except OSError:
        return None


def is_stale(
    lock_path: Path,
    metadata: dict[str, Any] | None = None,
    *,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
) -> bool:
    metadata = metadata if metadata is not None else read_lock_metadata(lock_path)
    pid = metadata.get("pid")
    if isinstance(pid, int) and not _pid_alive(pid):
        return True
    age = _lock_age_seconds(lock_path, metadata)
    return age is not None and age > stale_after_seconds


def active_key_lock(
    output_root: Path,
    key: str,
    *,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
) -> dict[str, Any] | None:
    lock_path = key_lock_path(output_root, key)
    if not lock_path.exists():
        return None
    metadata = read_lock_metadata(lock_path)
    if is_stale(lock_path, metadata, stale_after_seconds=stale_after_seconds):
        return None
    return {**metadata, "path": str(lock_path)}


def format_busy_message(key: str, metadata: dict[str, Any]) -> str:
    owner = metadata.get("owner") or "unknown owner"
    pid = metadata.get("pid")
    started_at = metadata.get("started_at") or "unknown start time"
    pid_text = f", pid={pid}" if isinstance(pid, int) else ""
    return f"already auditing {key} ({owner}{pid_text}, started {started_at})"


def acquire_key(
    output_root: Path,
    key: str,
    *,
    owner: str,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    lease_duration_seconds: int | None = None,
) -> AuditLock:
    lock_path = key_lock_path(output_root, key)
    token = uuid.uuid4().hex
    metadata = {
        "schema": 1,
        "key": key,
        "owner": owner,
        "pid": os.getpid(),
        "started_at": _now_iso(),
        "started_epoch": time.time(),
        "token": token,
    }

    while True:
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.mkdir()
            try:
                (lock_path / _OWNER_FILE).write_text(
                    json.dumps(metadata, indent=2, sort_keys=True)
                )
            except OSError:
                shutil.rmtree(lock_path, ignore_errors=True)
                raise
            return AuditLock(lock_path, key, token, metadata)
        except FileExistsError:
            current = read_lock_metadata(lock_path)
            if is_stale(
                lock_path,
                current,
                stale_after_seconds=stale_after_seconds,
            ):
                try:
                    shutil.rmtree(lock_path)
                    continue
                except FileNotFoundError:
                    continue
                except OSError:
                    pass
            raise AuditLockBusy(key, lock_path, current) from None
