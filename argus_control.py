"""Cooperative pause/resume state for ARGUS cron + audit pipeline.

`/argus pause` flips a shared state file at ~/.argus/control.json;
every component (cron wrapper, argus folder mode, batch loop) checks
before doing work and exits silently when paused. Atomic writes
(tempfile + os.replace), never raises (any read failure -> unpaused
default), lock-free (last-writer-wins os.replace).

Schema (paused is the only field on the hot path):
    { schema: 1, paused: bool,
      paused_at, paused_by, reason: str|null,
      bot_heartbeat, bot_pid: int|null }

bot_heartbeat lives here so daily_summary can detect a dead bot
without a second state file.

CLI (for when the bot is down):
    python argus_control.py status | pause "reason" | resume
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

# Single source of truth path. Hard-coded (not a config knob) on purpose:
# every component needs to agree on the location, and making it
# configurable opens the door to "cron wrapper checked /tmp/control.json
# but the bot wrote to ~/.argus/control.json" desynchronisation bugs.
STATE_PATH = Path.home() / ".argus" / "control.json"
SCHEMA = 1

# Default snapshot — returned on any read failure or first-ever invocation.
# `paused=False` is the correct default: a fresh checkout, a corrupt
# state file, or a missing one should not block the audit pipeline.
_DEFAULT: dict[str, Any] = {
    "schema": SCHEMA,
    "paused": False,
    "paused_at": None,
    "paused_by": None,
    "reason": None,
    "bot_heartbeat": None,
    "bot_pid": None,
    # current_audit: when a folder audit is in flight, this is set to
    # {"folder": str, "started_at": iso, "n_keys": int, "pid": int}.
    # When no audit is running, this is None. The ARGUS dashboard reads
    # this to render the "AUDITING NOW" banner.
    #
    # Set by argus_control.mark_audit_running, cleared by mark_audit_done.
    # If a process gets SIGKILLed mid-audit, the field stays populated.
    # current_audit() detects this via PID liveness + age and reports
    # `stale=True` so the dashboard can surface a warning rather than
    # falsely show an audit "in progress" forever.
    "current_audit": None,
}

# A current_audit older than this is treated as stale even if the PID
# happens to be alive (could be a recycled PID for an unrelated process).
# 2 hours is comfortably past the longest observed folder run on a 209-
# key sprint (~18 min) plus headroom for retries / LLM Runtime throttling.
_AUDIT_STALENESS_THRESHOLD_SECONDS = 2 * 3600


def _now_iso() -> str:
    """UTC timestamp in ISO-8601 with 'Z' suffix, no microseconds.

    Matches the format used by env_check / report.py and other ARGUS
    modules so a grep for timestamps across logs lines up consistently.
    """
    return _dt.datetime.now(_dt.timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def _read() -> dict[str, Any]:
    """Read the state file. Returns a copy of _DEFAULT on any failure.

    Wrong-schema files are also treated as default — a one-time
    "everything is unpaused" reset is the right answer when the schema
    bumps. Bot/wrapper will write the new shape on the next state change.
    """
    try:
        if not STATE_PATH.exists():
            return dict(_DEFAULT)
        raw = json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return dict(_DEFAULT)
    if raw.get("schema") != SCHEMA:
        return dict(_DEFAULT)
    # Fill any fields the file might be missing (forward-compat).
    out = dict(_DEFAULT)
    out.update({k: raw.get(k, _DEFAULT[k]) for k in _DEFAULT})
    return out


def _write(state: dict[str, Any]) -> bool:
    """Atomic write to STATE_PATH. Returns True on success, False otherwise.

    Uses tempfile + os.replace so a partial-write (SIGKILL between
    open() and write()) cannot corrupt the file. Failures swallow the
    OSError and return False — caller decides whether to retry, log, or
    ignore.
    """
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8",
            dir=str(STATE_PATH.parent), delete=False,
            prefix=f".{STATE_PATH.name}.", suffix=".tmp",
        ) as tmp:
            json.dump(state, tmp, indent=2)
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, STATE_PATH)
        return True
    except OSError as e:
        sys.stderr.write(f"[argus_control] write failed: "
                         f"{type(e).__name__}: {e}\n")
        return False


# Public API — read side
def is_paused() -> bool:
    """Single hot-path check used by cron_wrapper.sh, argus.py, etc.

    Always returns False on error (bias toward operating). Callers do
    NOT need to wrap this in try/except.
    """
    return bool(_read().get("paused"))


def status() -> dict[str, Any]:
    """Return the full current snapshot. Safe to log / serialise.

    Used by `/argus status` in the bot and by daily_summary.py.
    """
    return _read()


def bot_heartbeat_age_seconds() -> float | None:
    """How long since the bot last wrote a heartbeat, in seconds.

    None when no heartbeat has ever been written (bot never started, or
    state file was reset). Used by daily_summary.py to flag a dead bot
    in the heartbeat post.
    """
    state = _read()
    hb = state.get("bot_heartbeat")
    if not hb:
        return None
    try:
        # Tolerate both 'Z' and '+00:00' suffixes.
        if hb.endswith("Z"):
            hb = hb[:-1] + "+00:00"
        ts = _dt.datetime.fromisoformat(hb)
        now = _dt.datetime.now(_dt.timezone.utc)
        return (now - ts).total_seconds()
    except (ValueError, TypeError):
        return None


# Public API — write side
def pause(by: str, reason: str | None = None) -> bool:
    """Set paused=true. Returns True on success.

    `by` should be the Chat user_id or alias of whoever requested the
    pause (the bot fills this from the slash command sender). `reason`
    is an optional free-form string echoed in /argus status.
    """
    state = _read()
    state["paused"] = True
    state["paused_at"] = _now_iso()
    state["paused_by"] = by
    state["reason"] = reason
    return _write(state)


def resume(by: str) -> bool:
    """Set paused=false. Returns True on success.

    Idempotent: calling resume() on an already-running pipeline is a
    no-op for behaviour but updates `paused_by` so /argus status shows
    who last touched the switch.
    """
    state = _read()
    state["paused"] = False
    state["paused_at"] = None
    state["paused_by"] = by  # last toucher, not last pauser
    state["reason"] = None
    return _write(state)


def update_heartbeat(pid: int | None = None) -> bool:
    """Bot daemon calls this every minute.

    `pid` lets daily_summary.py check that the recorded PID is still
    alive (cheap signal-0 kill check) — catches a bot that crashed
    without releasing the heartbeat field.
    """
    state = _read()
    state["bot_heartbeat"] = _now_iso()
    if pid is not None:
        state["bot_pid"] = pid
    return _write(state)


def mark_audit_running(folder: str, n_keys: int,
                       pid: int | None = None) -> bool:
    """Record that a folder audit is starting. Called by argus._run_folder_list.

    `pid` defaults to os.getpid() — the worker process that will run
    the batch. Used by current_audit() to detect crash leaks via the
    signal-0 liveness check.
    """
    if pid is None:
        pid = os.getpid()
    state = _read()
    state["current_audit"] = {
        "folder": folder,
        "started_at": _now_iso(),
        "n_keys": n_keys,
        "pid": pid,
    }
    return _write(state)


def mark_audit_done() -> bool:
    """Clear the current_audit field. Called by argus._run_folder_list
    in a try/finally so it fires on success AND on raised exceptions.

    The only way the field stays populated past completion is a SIGKILL
    that bypasses Python's atexit / finally handlers. current_audit()'s
    staleness detector covers that case.
    """
    state = _read()
    state["current_audit"] = None
    return _write(state)


def _is_pid_alive(pid: int) -> bool:
    """Cheap liveness check via signal 0. No-op for the OS; raises if
    the PID is dead or unreachable. Returns True iff the PID exists
    and we have permission to signal it.
    """
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def current_audit() -> dict[str, Any] | None:
    """Return the current_audit record with a `stale` flag, or None.

    Shape:
        {"folder": ..., "started_at": iso, "n_keys": int, "pid": int,
         "stale": bool, "age_seconds": int}

    `stale` is True when:
      - The recorded PID is no longer alive (process crashed / killed), OR
      - The audit started more than _AUDIT_STALENESS_THRESHOLD_SECONDS ago
        (longer than any plausible folder audit, even on the slowest
        LLM Runtime day).

    Callers (the dashboard banner) render a different message for
    stale vs running. Stale = orange warning ("last run died"), running
    = blue/green "audit in progress".
    """
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


# CLI — for debugging when the Chat bot is down
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Manually inspect or flip the ARGUS pause state.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="Print the current state file.")
    p_pause = sub.add_parser("pause", help="Pause the audit pipeline.")
    p_pause.add_argument("reason", nargs="?", default=None,
                         help="Optional reason string.")
    sub.add_parser("resume", help="Resume the audit pipeline.")
    args = parser.parse_args(argv)

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
    return 1  # unreachable


if __name__ == "__main__":
    sys.exit(main())
