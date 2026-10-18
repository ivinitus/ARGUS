"""Shared execution-directory lookup across historical output layouts.

Three extractor layouts coexist on disk under output/ — flat
(<out>/<E-key>/), testrun-slug (<out>/<run>/<E-key>/), and per-tester
(<out>/<run>/<tester>/<E-key>/) — so lookups must search, not assume.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator


EXCLUDED_PARTS = frozenset({"_replay", "_stale_backups"})


def is_excluded(path: Path) -> bool:
    return bool(EXCLUDED_PARTS.intersection(path.parts))


def _literal_pattern(key: str) -> str:
    """Escape glob metacharacters so an E-key is matched literally."""
    return re.sub(r"([*?\[])", r"[\1]", key)


def iter_audit_jsons(out_dir: Path) -> Iterator[Path]:
    if not out_dir.exists():
        return
    for path in out_dir.rglob("audit.json"):
        if is_excluded(path):
            continue
        yield path


def iter_latest_audit_jsons(out_dir: Path) -> Iterator[Path]:
    """Select the newest saved copy of each exact Jira execution identity."""
    latest: dict[str, tuple[int, str, Path]] = {}
    for path in iter_audit_jsons(out_dir):
        key = path.parent.name
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*-E[0-9]+", key):
            yield path
            continue
        candidate = (path.stat().st_mtime_ns, str(path), path)
        if key not in latest or candidate[:2] > latest[key][:2]:
            latest[key] = candidate
    for key in sorted(latest):
        yield latest[key][2]


def find_execution_dir(
    out_dir: Path,
    key: str,
    *,
    skip_replay: bool = False,
) -> Path | None:
    """Find an execution directory, optionally excluding replays."""
    if not out_dir.exists():
        return None
    flat = out_dir / key
    if flat.is_dir():
        return flat
    for candidate in out_dir.rglob(_literal_pattern(key)):
        if not candidate.is_dir():
            continue
        if "_stale_backups" in candidate.parts:
            continue
        if skip_replay and "_replay" in candidate.parts:
            continue
        return candidate
    return None


def find_audit_json(out_dir: Path, key: str) -> Path | None:
    if not out_dir.exists():
        return None
    if re.fullmatch(r"[A-Z][A-Z0-9_]*-E[0-9]+", key):
        candidates = [
            path for path in out_dir.rglob(f"{_literal_pattern(key)}/audit.json")
            if not is_excluded(path)
        ]
        return max(
            candidates, key=lambda path: (path.stat().st_mtime_ns, str(path)),
            default=None,
        )
    for candidate in out_dir.rglob(_literal_pattern(key)):
        if not candidate.is_dir():
            continue
        if is_excluded(candidate):
            continue
        audit_path = candidate / "audit.json"
        if audit_path.exists():
            return audit_path
    return None
