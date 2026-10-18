"""Per-test-case status history for R14 (status-streak-break)."""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from argus.core import atomic_io
from argus.core import exec_paths

_CACHE_PATH = Path.home() / ".argus" / "history_index.json"
_CACHE_SCHEMA = 2

_NO_MP = "_"

_DEFAULT_WINDOW = 10
_DEFAULT_THRESHOLD = 8

_SHARED_LOCK = threading.Lock()
_SHARED: dict[str, tuple[float, "HistoryIndex"]] = {}
_SHARED_TTL_S = 60.0
# Effective refresh interval for `get_shared`; `configure()` raises it from
# batch.history_refresh_seconds so a batch rebuilds (an rglob over the output
# root under the global lock) at most once per interval.
_refresh_interval_s = _SHARED_TTL_S
_frozen_snapshot: "HistoryIndex | None" = None


def configure(
    state_dir: Path,
    *,
    refresh_interval_s: float | None = None,
) -> None:
    """Relocate history persistence and discard instances for the old path."""
    global _CACHE_PATH, _refresh_interval_s, _frozen_snapshot
    _frozen_snapshot = None
    new_path = state_dir.expanduser() / "history_index.json"
    with _SHARED_LOCK:
        if refresh_interval_s is not None:
            _refresh_interval_s = max(0.0, float(refresh_interval_s))
        if new_path != _CACHE_PATH:
            _CACHE_PATH = new_path
            _SHARED.clear()

_STATUS_ALIASES: dict[str, str] = {
    "n/a": "Not Applicable",
    "na": "Not Applicable",
    "not-applicable": "Not Applicable",
    "notapplicable": "Not Applicable",
}


def _normalise_status(s: str | None) -> str | None:
    """Canonicalise status text for streak comparison."""
    if not s:
        return None
    key = s.strip().lower()
    return _STATUS_ALIASES.get(key, s.strip())


@dataclass
class HistoryEntry:
    date: str
    status: str
    e_key: str
    folder: str
    tester: str | None = None
    verdict: str | None = None
    highs: int = 0

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "status": self.status,
            "e_key": self.e_key,
            "folder": self.folder,
            "tester": self.tester,
            "verdict": self.verdict,
            "highs": self.highs,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "HistoryEntry":
        return cls(
            date=d.get("date") or "",
            status=d.get("status") or "",
            e_key=d.get("e_key") or "",
            folder=d.get("folder") or "",
            tester=d.get("tester"),
            verdict=d.get("verdict"),
            highs=d.get("highs") or 0,
        )


@dataclass
class HistoryIndex:
    """In-memory + on-disk cached index of test-case execution history."""
    entries: dict[str, list[HistoryEntry]] = field(default_factory=dict)
    file_mtimes: dict[str, float] = field(default_factory=dict)
    built_at: str = ""
    files_indexed: int = 0

    @classmethod
    def load_or_build(cls, out_dir: Path) -> "HistoryIndex":
        """Load the cache, then incrementally update against `out_dir`."""
        idx = cls._load_cache()
        idx._refresh_from_disk(out_dir)
        return idx

    @classmethod
    def get_shared(cls, out_dir: Path,
                   *, max_age_s: float | None = None) -> "HistoryIndex":
        """Process-cached shared index for `out_dir` (thread-safe).

        `max_age_s` defaults to the configured refresh interval.
        """
        if _frozen_snapshot is not None:
            return _frozen_snapshot
        if max_age_s is None:
            max_age_s = _refresh_interval_s
        key = str(Path(out_dir).resolve())
        with _SHARED_LOCK:
            cached = _SHARED.get(key)
            if cached is not None and time.monotonic() - cached[0] < max_age_s:
                return cached[1]
            idx = cls.load_or_build(Path(out_dir))
            idx.save()
            _SHARED[key] = (time.monotonic(), idx)
            return idx

    def snapshot(self) -> dict:
        """Portable input for comparing two policies against the same history."""
        return {key: [entry.to_dict() for entry in entries] for key, entries in self.entries.items()}

    @classmethod
    def use_snapshot(cls, snapshot: dict) -> None:
        """Pin read-only history in an isolated policy-preview subprocess."""
        global _frozen_snapshot
        _frozen_snapshot = cls(entries={
            key: [HistoryEntry.from_dict(entry) for entry in entries]
            for key, entries in snapshot.items()
        })

    @classmethod
    def _load_cache(cls) -> "HistoryIndex":
        if not _CACHE_PATH.exists():
            return cls()
        try:
            raw = json.loads(_CACHE_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            return cls()
        if raw.get("schema") != _CACHE_SCHEMA:
            return cls()
        entries = {
            key: [HistoryEntry.from_dict(d) for d in lst]
            for key, lst in (raw.get("entries") or {}).items()
        }
        return cls(
            entries=entries,
            file_mtimes=dict(raw.get("file_mtimes") or {}),
            built_at=raw.get("built_at") or "",
            files_indexed=int(raw.get("files_indexed") or 0),
        )

    def save(self) -> bool:
        """Atomic write to ~/.argus/history_index.json."""
        try:
            payload = {
                "schema": _CACHE_SCHEMA,
                "built_at": self.built_at,
                "files_indexed": self.files_indexed,
                "entries": {
                    key: [e.to_dict() for e in lst]
                    for key, lst in self.entries.items()
                },
                "file_mtimes": self.file_mtimes,
            }
            atomic_io.atomic_write_json(_CACHE_PATH, payload)
            return True
        except OSError as e:
            sys.stderr.write(f"[history_index] save failed: "
                             f"{type(e).__name__}: {e}\n")
            return False

    def _refresh_from_disk(self, out_dir: Path) -> None:
        """Incrementally re-read changed metadata files under out_dir."""
        if not out_dir.exists():
            return

        seen_paths: set[str] = set()
        changed = 0
        for path in out_dir.rglob("metadata.json"):
            if exec_paths.is_excluded(path):
                continue
            abs_path = str(path.resolve())
            seen_paths.add(abs_path)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            try:
                mtime = max(mtime, (path.parent / "audit.json")
                            .stat().st_mtime)
            except OSError:
                pass
            cached_mtime = self.file_mtimes.get(abs_path)
            if cached_mtime is not None and cached_mtime >= mtime:
                continue

            entry_data = self._read_metadata_entry(path)
            if entry_data is None:
                continue
            tc_key, mp, entry = entry_data
            self._upsert_entry(tc_key, mp, entry)
            self.file_mtimes[abs_path] = mtime
            changed += 1

        # Callers refresh one folder at a time, so only paths under this
        # scan root can be judged missing; and a missing execution must take
        # its history entries with it, or ghosts keep feeding R14/R17/R20.
        root_prefix = str(out_dir.resolve()).rstrip(os.sep) + os.sep
        deleted = [
            p for p in self.file_mtimes
            if p.startswith(root_prefix) and p not in seen_paths
        ]
        for p in deleted:
            self.file_mtimes.pop(p, None)
            self._drop_entries_for_execution(Path(p).parent.name)

        if changed or deleted:
            for lst in self.entries.values():
                lst.sort(key=lambda e: e.date, reverse=True)

        self.built_at = _dt.datetime.now(_dt.timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z")
        self.files_indexed = len(self.file_mtimes)

    @staticmethod
    def _read_metadata_entry(path: Path) -> tuple[str, str, HistoryEntry] | None:
        """Parse one metadata.json into (tc_key, mp, HistoryEntry)."""
        try:
            meta = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        results = meta.get("test_results") or []
        if not results:
            return None
        tr = results[0]
        tc_key = tr.get("test_case_key")
        if not tc_key:
            return None
        mp = tr.get("marketplace") or _NO_MP
        folder = ""
        for parent in path.parents:
            if (parent / "_folder_meta.json").exists():
                folder = parent.name
                break
            grandparent = parent.parent
            if grandparent.name == "output":
                folder = parent.name
                break
        verdict = None
        highs = 0
        try:
            audit = json.loads((path.parent / "audit.json").read_text())
            verdict = audit.get("overall_verdict")
            highs = sum(1 for f in audit.get("findings") or []
                        if isinstance(f, dict)
                        and f.get("severity") == "high")
        except (OSError, json.JSONDecodeError):
            pass
        entry = HistoryEntry(
            date=tr.get("execution_date") or "",
            status=tr.get("status") or "",
            e_key=path.parent.name,
            folder=folder,
            tester=tr.get("executed_by_name") or tr.get("executed_by"),
            verdict=verdict,
            highs=highs,
        )
        return tc_key, str(mp), entry

    def _drop_entries_for_execution(self, e_key: str) -> None:
        """Remove every entry that came from a now-missing execution dir."""
        if not e_key:
            return
        for bucket_key in list(self.entries):
            kept = [e for e in self.entries[bucket_key] if e.e_key != e_key]
            if kept:
                self.entries[bucket_key] = kept
            else:
                del self.entries[bucket_key]

    def _upsert_entry(self, tc_key: str, mp: str, entry: HistoryEntry) -> None:
        """Replace any existing entry for the same e_key, else append."""
        bucket_key = f"{tc_key}|{mp}"
        bucket = self.entries.setdefault(bucket_key, [])
        for i, existing in enumerate(bucket):
            if existing.e_key == entry.e_key:
                bucket[i] = entry
                return
        bucket.append(entry)

    def get_history(self, tc_key: str,
                    mp: str | None) -> list[HistoryEntry]:
        """Return all known executions of `tc_key` in marketplace `mp`, newest first."""
        bucket_key = f"{tc_key}|{mp or _NO_MP}"
        return list(self.entries.get(bucket_key, []))

    @staticmethod
    def is_streak(history: list[HistoryEntry], status: str,
                  *,
                  exclude_e_key: str | None = None,
                  window: int = _DEFAULT_WINDOW,
                  threshold: int = _DEFAULT_THRESHOLD) -> bool:
        """Return True iff `history` shows >= threshold of `status` in the most recent `window` entries."""
        canon_status = _normalise_status(status)
        if not canon_status:
            return False
        relevant = [
            e for e in history
            if exclude_e_key is None or e.e_key != exclude_e_key
        ][:window]
        if len(relevant) < threshold:
            return False
        matches = sum(
            1 for e in relevant
            if _normalise_status(e.status) == canon_status
        )
        return matches >= threshold


def main(argv: list[str] | None = None) -> int:
    import argparse
    from argus import config as argus_config

    parser = argparse.ArgumentParser(
        description="Build / query the per-test-case history index.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("rebuild",
                   help="Force-rebuild the index from scratch (drops the cache "
                        "first, then walks output/).")
    p_query = sub.add_parser("query",
                             help="Show the history of one test case.")
    p_query.add_argument("test_case_key")
    p_query.add_argument("--mp", default=None,
                         help="Marketplace filter (e.g. BR). Omit for all.")
    sub.add_parser("stats",
                   help="Print summary statistics about the index.")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="output dir to scan (default: from config.toml).")
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args(argv)

    settings = argus_config.load(args.config)
    from argus import runtime
    runtime.configure(settings)
    out_dir = (args.out_dir or settings.extractor.out_dir).resolve()

    if args.cmd == "rebuild":
        if _CACHE_PATH.exists():
            _CACHE_PATH.unlink()
        idx = HistoryIndex.load_or_build(out_dir)
        idx.save()
        print(f"indexed {idx.files_indexed} metadata files")
        print(f"unique (test_case, mp) buckets: {len(idx.entries)}")
        total_entries = sum(len(v) for v in idx.entries.values())
        print(f"total history entries: {total_entries}")
        return 0

    if args.cmd == "stats":
        idx = HistoryIndex.load_or_build(out_dir)
        idx.save()
        print(f"built_at:        {idx.built_at}")
        print(f"files_indexed:   {idx.files_indexed}")
        print(f"buckets:         {len(idx.entries)}")
        total = sum(len(v) for v in idx.entries.values())
        print(f"total entries:   {total}")
        ranked = sorted(
            idx.entries.items(), key=lambda kv: -len(kv[1]))[:5]
        print("top 5 buckets by execution count:")
        for k, v in ranked:
            print(f"  {k}: {len(v)} executions")
        return 0

    if args.cmd == "query":
        idx = HistoryIndex.load_or_build(out_dir)
        idx.save()
        if args.mp:
            history = idx.get_history(args.test_case_key, args.mp)
            print(f"{args.test_case_key} | {args.mp}: {len(history)} entries")
            for e in history[:20]:
                print(f"  {e.date}  {e.status:<20s}  {e.e_key}  "
                      f"({e.tester or '?'}) [{e.folder}]")
        else:
            prefix = args.test_case_key + "|"
            matches = sorted(
                k for k in idx.entries if k.startswith(prefix))
            if not matches:
                print(f"no history for {args.test_case_key}")
                return 0
            for bucket_key in matches:
                _, mp = bucket_key.split("|", 1)
                bucket = idx.entries[bucket_key]
                mp_label = mp if mp != _NO_MP else "(no MP)"
                print(f"\n{args.test_case_key} | {mp_label}: "
                      f"{len(bucket)} entries")
                for e in bucket[:20]:
                    print(f"  {e.date}  {e.status:<20s}  {e.e_key}  "
                          f"({e.tester or '?'}) [{e.folder}]")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
