"""Once-a-day Slack heartbeat for Project ARGUS."""
from __future__ import annotations

import argparse
import datetime as _dt
import re
import sys
from pathlib import Path

from argus import config as argus_config
from argus.operations import notify
from argus import runtime

_TAIL_BYTES = 1_000_000

_EVENT_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) "
    r"\[(?P<level>info|warn|error)\] (?P<title>.+)$"
)


def _read_tail(path: Path, n_bytes: int) -> str:
    """Best-effort tail read. Returns '' on any failure or empty file."""
    try:
        if not path.exists():
            return ""
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > n_bytes:
                f.seek(size - n_bytes)
                f.readline()
            return f.read().decode("utf-8", errors="replace")
    except OSError as e:
        sys.stderr.write(f"[daily_summary] could not read {path}: "
                         f"{type(e).__name__}: {e}\n")
        return ""


def _events_in_window(text: str, since: _dt.datetime) -> list[dict]:
    """Parse events from log text, keeping only those at-or-after `since`."""
    out: list[dict] = []
    for line in text.splitlines():
        m = _EVENT_RE.match(line)
        if not m:
            continue
        try:
            ts = _dt.datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if ts < since:
            continue
        out.append({
            "ts": ts,
            "level": m.group("level"),
            "title": m.group("title"),
        })
    return out


_LEVEL_COUNTER_KEYS = {"warn": "warns", "error": "errors"}


def _summarise(events: list[dict]) -> dict[str, int | list[str]]:
    """Bucket events by their meaning."""
    counts: dict[str, int] = {
        "folder_runs": 0,
        "flagged_keys": 0,
        "new_flagged_batches": 0,
        "model_dead": 0,
        "errors": 0,
        "warns": 0,
        "info": 0,
    }
    flagged_examples: list[str] = []
    for ev in events:
        title = ev["title"]
        level = ev["level"]
        level_key = _LEVEL_COUNTER_KEYS.get(level, level)
        counts[level_key] = counts.get(level_key, 0) + 1
        if title.startswith("folder run"):
            counts["folder_runs"] += 1
        if title.startswith("flagged "):
            counts["flagged_keys"] += 1
            parts = title.split()
            if len(parts) >= 2 and parts[0] == "flagged":
                key = parts[1]
                if key not in flagged_examples and len(flagged_examples) < 5:
                    flagged_examples.append(key)
        if title.startswith("new flagged keys") or "new deviation" in title:
            counts["new_flagged_batches"] += 1
        if "model" in title.lower():
            counts["model_dead"] += 1
    return {**counts, "flagged_key_examples": flagged_examples}


def _format_body(stats: dict[str, int | list[str]],
                 hours: int,
                 since: _dt.datetime) -> str:
    """Compose the multi-line body. One screenful, scannable."""
    lines = [
        f"window: last {hours}h (since {since.strftime('%Y-%m-%d %H:%M')} local)",
        f"folder runs: {stats['folder_runs']}",
        f"flagged keys: {stats['flagged_keys']}",
        f"new-flagged batches: {stats['new_flagged_batches']}",
    ]
    examples = stats.get("flagged_key_examples") or []
    if isinstance(examples, list) and examples:
        lines.append("recent flagged: " + ", ".join(examples))
    if stats.get("model_dead"):
        lines.append(f"model-dead skips: {stats['model_dead']}")
    lines.append(f"events: info={stats.get('info', 0)} "
                 f"warn={stats.get('warns', 0)} "
                 f"error={stats.get('errors', 0)}")
    return "\n".join(lines)


def run(hours: int = 24,
        settings: argus_config.ArgusConfig | None = None) -> int:
    """Read the notify log over the last `hours`, post the summary."""
    if settings is None:
        settings = argus_config.load()
    since = _dt.datetime.now() - _dt.timedelta(hours=hours)
    text = _read_tail(settings.notify.log_path, _TAIL_BYTES)
    events = _events_in_window(text, since)
    stats = _summarise(events)
    body = _format_body(stats, hours, since)
    title = "daily summary"
    notify.post("info", title, body, settings=settings)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Post a daily ARGUS heartbeat to Slack from notify.log.")
    parser.add_argument(
        "--hours", type=int, default=24,
        help="window size in hours (default 24).")
    parser.add_argument(
        "--config", type=Path, default=None,
        help="path to config.toml (default: ./config.toml).")
    args = parser.parse_args(argv)

    settings = argus_config.load(args.config)
    runtime.configure(settings)
    return run(hours=args.hours, settings=settings)


if __name__ == "__main__":
    sys.exit(main())
