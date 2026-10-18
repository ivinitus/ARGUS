"""Curated operator event stream for the tmux console."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from argus import config as argus_config


_MAX_LOG_BYTES = 1024 * 1024
_ACTIVITY_LABELS = {"AUDIT", "EXTRACT", "PROGRESS", "COMPLETE"}
_ALERT_LABELS = {
    "FLAGGED",
    "WARNING",
    "RECOVERED",
    "ERROR",
    "COST WARNING",
    "COST CRITICAL",
}


def _event_path(settings: argus_config.ArgusConfig) -> Path:
    return settings.general.state_dir / "events.log"


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def _rotate_locked(path: Path) -> None:
    try:
        if path.stat().st_size < _MAX_LOG_BYTES:
            return
    except FileNotFoundError:
        return
    backup = path.with_suffix(path.suffix + ".1")
    try:
        backup.unlink(missing_ok=True)
        path.replace(backup)
    except OSError as exc:
        sys.stderr.write(
            f"[events] log rotation of {path} failed: "
            f"{type(exc).__name__}: {exc}\n")


def emit(
    label: str,
    message: str,
    *,
    settings: argus_config.ArgusConfig,
    timestamp: str | None = None,
) -> None:
    """Append one concise event. Logging failures never affect callers."""
    label = label.upper()
    if label not in _ACTIVITY_LABELS | _ALERT_LABELS:
        return
    clean_message = " ".join(str(message).split())
    if len(clean_message) > 240:
        clean_message = clean_message[:237].rstrip() + "..."
    record = {
        "timestamp": timestamp or _now_iso(),
        "label": label,
        "message": clean_message,
    }
    path = _event_path(settings)
    lock_path = path.with_suffix(path.suffix + ".lock")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            _rotate_locked(path)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(
                    record, ensure_ascii=False, separators=(",", ":")
                ) + "\n")
    except OSError as exc:
        sys.stderr.write(f"[events] could not append to {path}: {exc}\n")


def _body_fields(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in body.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip().lower()] = value.strip()
    return fields


def _clean_title(title: str) -> str:
    title = re.sub(r"^:[a-z0-9_+-]+:\s*", "", title,
                   flags=re.IGNORECASE)
    title = re.sub(r"^ARGUS\s+[—-]\s+", "", title,
                   flags=re.IGNORECASE)
    return " ".join(title.split())


def emit_notification(
    level: str,
    title: str,
    body: str,
    *,
    settings: argus_config.ArgusConfig,
) -> None:
    """Translate notify events into compact Activity or Alerts records."""
    clean_title = _clean_title(title)
    lowered = clean_title.lower()
    fields = _body_fields(body)

    if lowered == "audit started":
        folder = fields.get("folder", "unknown folder")
        keys = fields.get("keys", "?").split(" ", 1)[0]
        emit("AUDIT", f'Started {keys} keys in "{folder}"',
             settings=settings)
        return

    if lowered in {"audit done", "audit done with failures"}:
        result = fields.get("result", clean_title)
        result = result.replace(" ok", " passed")
        elapsed = fields.get("elapsed")
        message = result if not elapsed else f"{result} | {elapsed}"
        emit("COMPLETE", message, settings=settings)
        if lowered == "audit done with failures":
            emit("ERROR", f"Audit completed with failures | {result}",
                 settings=settings)
        return

    if "new deviation" in lowered:
        count_match = re.match(r"(\d+) new deviations?", lowered)
        count = int(count_match.group(1)) if count_match else 0
        first = next((line for line in body.splitlines()
                      if line.startswith("- ")), "")
        if count == 1 and first:
            key = first[2:].split(" ", 1)[0]
            detail = first.rsplit(" — ", 1)[-1]
            detail = re.sub(r"^marked [^;]+;\s*", "", detail)
            emit("FLAGGED", f"{key} | {detail}", settings=settings)
        else:
            emit("FLAGGED", clean_title, settings=settings)
        return

    if lowered.startswith("cost budget warning"):
        emit("COST WARNING", body or clean_title, settings=settings)
        return
    if lowered.startswith("cost budget exceeded"):
        emit("COST CRITICAL", body or clean_title, settings=settings)
        return
    if lowered.startswith("cost explorer recovered"):
        emit("RECOVERED", "Cost Explorer monitoring restored",
             settings=settings)
        return

    if level == "error":
        emit("ERROR", clean_title, settings=settings)
    elif level == "warn":
        emit("WARNING", clean_title, settings=settings)
    elif "recover" in lowered or "restored" in lowered:
        emit("RECOVERED", clean_title, settings=settings)
    elif lowered.startswith("r12 self-heal"):
        emit("AUDIT", clean_title, settings=settings)


def _decode(line: str) -> dict[str, Any] | None:
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict):
        return None
    if not all(isinstance(record.get(key), str)
               for key in ("timestamp", "label", "message")):
        return None
    return record


def _matches(record: dict[str, Any], stream: str) -> bool:
    labels = _ACTIVITY_LABELS if stream == "activity" else _ALERT_LABELS
    return record["label"] in labels


def _format(record: dict[str, Any]) -> str:
    timestamp = record["timestamp"]
    try:
        parsed = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        clock = parsed.astimezone().strftime("%H:%M")
    except ValueError:
        clock = timestamp
    return f"{clock} {record['label']:<13} {record['message']}"


def _initial_records(path: Path, stream: str, lines: int) -> list[str]:
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rendered = []
    for line in raw_lines:
        record = _decode(line)
        if record is not None and _matches(record, stream):
            rendered.append(_format(record))
    return rendered[-lines:]


def follow(
    settings: argus_config.ArgusConfig,
    stream: str,
    *,
    lines: int = 50,
    poll_seconds: float = 0.5,
) -> int:
    """Print recent matching events, then follow across log rotation."""
    path = _event_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)

    for rendered in _initial_records(path, stream, lines):
        print(rendered)
    sys.stdout.flush()

    handle = path.open("r", encoding="utf-8")
    handle.seek(0, os.SEEK_END)
    inode = os.fstat(handle.fileno()).st_ino
    try:
        while True:
            line = handle.readline()
            if line:
                record = _decode(line)
                if record is not None and _matches(record, stream):
                    print(_format(record), flush=True)
                continue
            time.sleep(poll_seconds)
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_ino != inode or stat.st_size < handle.tell():
                handle.close()
                handle = path.open("r", encoding="utf-8")
                inode = os.fstat(handle.fileno()).st_ino
    except KeyboardInterrupt:
        return 0
    finally:
        handle.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Follow the curated ARGUS operator event stream.")
    parser.add_argument("stream", choices=("activity", "alerts"))
    parser.add_argument("--lines", type=int, default=50)
    args = parser.parse_args(argv)
    settings = argus_config.load()
    return follow(settings, args.stream, lines=max(0, args.lines))


if __name__ == "__main__":
    raise SystemExit(main())
