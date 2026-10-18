"""Cooperative pause/resume state for ARGUS cron + audit pipeline."""
from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import fcntl

from argus.core import atomic_io
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

STATE_PATH = Path.home() / ".argus" / "control.json"
SCHEMA = 1

_DEFAULT: dict[str, Any] = {
    "schema": SCHEMA,
    "paused": False,
    "paused_at": None,
    "paused_by": None,
    "reason": None,
    "bot_heartbeat": None,
    "bot_pid": None,
    "current_audit": None,
}

_AUDIT_STALENESS_THRESHOLD_SECONDS = 2 * 3600


def configure(state_dir: Path) -> None:
    global STATE_PATH
    STATE_PATH = state_dir.expanduser() / "control.json"




def _now_iso() -> str:
    """UTC timestamp in ISO-8601 with 'Z' suffix, no microseconds."""
    return _dt.datetime.now(_dt.timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def _read() -> dict[str, Any]:
    """Read the state file."""
    try:
        if not STATE_PATH.exists():
            return dict(_DEFAULT)
        raw = json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return dict(_DEFAULT)
    if raw.get("schema") != SCHEMA:
        return dict(_DEFAULT)
    out = dict(_DEFAULT)
    out.update({k: raw.get(k, _DEFAULT[k]) for k in _DEFAULT})
    return out


def _write(state: dict[str, Any]) -> bool:
    """Atomic write to STATE_PATH."""
    try:
        atomic_io.atomic_write_json(STATE_PATH, state)
        return True
    except OSError as e:
        sys.stderr.write(f"[argus_control] write failed: "
                         f"{type(e).__name__}: {e}\n")
        return False


@contextlib.contextmanager
def _state_write_lock():
    """Serialize local read-modify-write control state transitions."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_path = STATE_PATH.with_name(f".{STATE_PATH.name}.lock")
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def is_paused() -> bool:
    """Single hot-path check used by cron_wrapper.sh and the orchestrator."""
    return bool(_read().get("paused"))


def status() -> dict[str, Any]:
    """Return the full current snapshot."""
    return _read()


def bot_heartbeat_age_seconds() -> float | None:
    """How long since the bot last wrote a heartbeat, in seconds."""
    state = _read()
    hb = state.get("bot_heartbeat")
    if not hb:
        return None
    try:
        if hb.endswith("Z"):
            hb = hb[:-1] + "+00:00"
        ts = _dt.datetime.fromisoformat(hb)
        now = _dt.datetime.now(_dt.timezone.utc)
        return (now - ts).total_seconds()
    except (ValueError, TypeError):
        return None


def pause(by: str, reason: str | None = None) -> bool:
    """Set paused=true."""
    with _state_write_lock():
        state = _read()
        state["paused"] = True
        state["paused_at"] = _now_iso()
        state["paused_by"] = by
        state["reason"] = reason
        return _write(state)


def resume(by: str) -> bool:
    """Set paused=false."""
    with _state_write_lock():
        state = _read()
        state["paused"] = False
        state["paused_at"] = None
        state["paused_by"] = by
        state["reason"] = None
        return _write(state)


def update_heartbeat(pid: int | None = None) -> bool:
    """Bot daemon calls this every minute."""
    with _state_write_lock():
        state = _read()
        state["bot_heartbeat"] = _now_iso()
        if pid is not None:
            state["bot_pid"] = pid
        return _write(state)


def mark_audit_running(
    folder: str,
    n_keys: int,
    pid: int | None = None,
    owner_token: str | None = None,
    *,
    phase: str = "auditing",
    requested_by: str | None = None,
    target: str | None = None,
) -> str | None:
    """Record an audit start and return its effective ownership token.

    ``phase`` is ``"preparing"`` while the task is still enumerating Jira
    and ``"auditing"`` (the default) once keys are being processed, so the
    dashboard and ``/argus status`` show progress before the first result.
    """
    owner_token = owner_token or uuid.uuid4().hex
    if pid is None:
        pid = os.getpid()
    with _state_write_lock():
        state = _read()
        record: dict[str, Any] = {
            "folder": folder,
            "started_at": _now_iso(),
            "n_keys": n_keys,
            "pid": pid,
            "owner_token": owner_token,
            "phase": phase,
        }
        if requested_by:
            record["requested_by"] = requested_by
        if target:
            record["target"] = target
        state["current_audit"] = record
        return owner_token if _write(state) else None


def mark_audit_done(owner_token: str | None = None) -> bool:
    """Clear current_audit only when the caller still owns it."""
    with _state_write_lock():
        state = _read()
        current = state.get("current_audit")
        if owner_token is not None and (
            not isinstance(current, dict)
            or current.get("owner_token") != owner_token
        ):
            return False
        state["current_audit"] = None
        return _write(state)


def _is_pid_alive(pid: int) -> bool:
    """Cheap liveness check via signal 0."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def current_audit() -> dict[str, Any] | None:
    """Return the current_audit record with a `stale` flag, or None."""
    state = _read()
    record = state.get("current_audit")
    if not isinstance(record, dict):
        return None
    started = record.get("started_at")
    age_seconds: int | None = None
    try:
        if started:
            ts_str = started[:-1] + "+00:00" if started.endswith("Z") else started
            ts = _dt.datetime.fromisoformat(ts_str)
            age_seconds = int(
                (_dt.datetime.now(_dt.timezone.utc) - ts).total_seconds())
    except (ValueError, TypeError):
        age_seconds = None
    pid = record.get("pid")
    stale = False
    if isinstance(pid, int) and not _is_pid_alive(pid):
        stale = True
    if (age_seconds is not None
            and age_seconds > _AUDIT_STALENESS_THRESHOLD_SECONDS):
        stale = True
    return {
        **record,
        "stale": stale,
        "age_seconds": age_seconds if age_seconds is not None else 0,
    }


def main(argv: list[str] | None = None) -> int:
    from argus import config as argus_config, runtime

    parser = argparse.ArgumentParser(
        description="Manually inspect or flip the ARGUS pause state.")
    parser.add_argument("--config", type=Path, default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="Print the current state file.")
    p_pause = sub.add_parser("pause", help="Pause the audit pipeline.")
    p_pause.add_argument("reason", nargs="?", default=None,
                         help="Optional reason string.")
    sub.add_parser("resume", help="Resume the audit pipeline.")
    args = parser.parse_args(argv)
    runtime.configure(argus_config.load(args.config))

    operator = os.environ.get("USER", "unknown")
    if args.cmd == "status":
        print(json.dumps(status(), indent=2))
        return 0
    if args.cmd == "pause":
        ok = pause(by=operator, reason=args.reason)
        print("paused" if ok else "FAILED to pause")
        return 0 if ok else 1
    if args.cmd == "resume":
        ok = resume(by=operator)
        print("resumed" if ok else "FAILED to resume")
        return 0 if ok else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
