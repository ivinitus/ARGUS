"""Per-test-case status history for R14 (status-streak-break).

Indexes every metadata.json under output/ by (test_case_key, marketplace)
so R14 can ask "has THIS case in THIS MP been suspiciously consistent?"
Index walks output/ at startup, mtime-incremental on subsequent loads.

Cache: ~/.argus/history_index.json (atomic via tempfile+os.replace).
    entries: { "<tc_key>|<mp_or_'_'>": [{date, status, e_key, folder, tester}, ...] }
    file_mtimes: { "<abs_path>": <unix_ts> }

Public API:
    HistoryIndex.load_or_build(out_dir) -> HistoryIndex
    .get_history(tc_key, mp) -> list[entry]  # newest first
    .is_streak(history, status, n=10, threshold=8) -> bool
    .rebuild() / .save()

Hot-path safe: ~1ms lookup per audit.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# Cache lives next to the rest of ARGUS state. Single file, atomic
# writes (tempfile + os.replace) so a SIGKILL can't corrupt it.
_CACHE_PATH = Path.home() / ".argus" / "history_index.json"
_CACHE_SCHEMA = 1

# Placeholder used when a metadata file has marketplace=None (folders
# that don't encode an MP in the testrun name, e.g. Additional Non
# Automated). Keeps these in their own bucket instead of contaminating
# real per-MP histories.
_NO_MP = "_"

# Streak defaults — R14's policy threshold. Exposed as a function arg
# so tests can override and operators can tune via config later.
_DEFAULT_WINDOW = 10
_DEFAULT_THRESHOLD = 8

# Statuses we treat as semantically equivalent for streak purposes.
# "N/A" appears in some test-management installations as the canonical name for
# the Not Applicable status; treat both identically. R14 cares about
# Blocked-or-N/A vs Pass — we don't want a streak interrupted just
# because the tester used a different short name.
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
    date: str          # ISO-8601 string, kept as string (sortable lexically)
    status: str
    e_key: str
    folder: str
    tester: str | None = None

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "status": self.status,
            "e_key": self.e_key,
            "folder": self.folder,
            "tester": self.tester,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "HistoryEntry":
        return cls(
            date=d.get("date") or "",
            status=d.get("status") or "",
            e_key=d.get("e_key") or "",
            folder=d.get("folder") or "",
            tester=d.get("tester"),
        )


@dataclass
class HistoryIndex:
    """In-memory + on-disk cached index of test-case execution history.

    Use load_or_build() to get one; never construct directly unless
    you're a test. The mtime tracking in `file_mtimes` is what makes
    incremental rebuilds cheap on a 1000-audit corpus.
    """
    entries: dict[str, list[HistoryEntry]] = field(default_factory=dict)
    file_mtimes: dict[str, float] = field(default_factory=dict)
    built_at: str = ""
    files_indexed: int = 0

    # ------------------------------------------------------------------
    # Construction / persistence
    # ------------------------------------------------------------------
    @classmethod
    def load_or_build(cls, out_dir: Path) -> "HistoryIndex":
        """Load the cache, then incrementally update against `out_dir`.

        First call (no cache): full walk of out_dir. Subsequent calls:
        only re-read metadata.json files whose mtime exceeded the
        cached value. New files added; deleted files removed from
        entries.
        """
        idx = cls._load_cache()
        idx._refresh_from_disk(out_dir)
        return idx

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
            _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
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
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8",
                dir=str(_CACHE_PATH.parent), delete=False,
                prefix=f".{_CACHE_PATH.name}.", suffix=".tmp",
            ) as tmp:
                json.dump(payload, tmp, indent=2)
                tmp_path = Path(tmp.name)
            os.replace(tmp_path, _CACHE_PATH)
            return True
        except OSError as e:
            sys.stderr.write(f"[history_index] save failed: "
                             f"{type(e).__name__}: {e}\n")
            return False

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------
    def _refresh_from_disk(self, out_dir: Path) -> None:
        """Incrementally re-read changed metadata files under out_dir.

        Walks every metadata.json (cheap — file count is bounded by
        audited E-keys, ~1000 on the current corpus). For each:
          - If file mtime <= cached mtime: skip (already indexed).
          - Otherwise: re-read, replace any prior entry with the same
            e_key in entries[(tc_key, mp)].
        Then prunes entries whose source file no longer exists.

        Side-effect: updates file_mtimes, built_at, files_indexed.
        Does NOT save; caller decides when to persist.
        """
        if not out_dir.exists():
            return

        seen_paths: set[str] = set()
        changed = 0
        for path in out_dir.rglob("metadata.json"):
            abs_path = str(path.resolve())
            seen_paths.add(abs_path)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
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

        # Prune deletions: any cached file_mtime path that's no longer
        # on disk means the audit was rm'd. Drop the corresponding
        # entry from entries[*] too.
        deleted = [p for p in self.file_mtimes if p not in seen_paths]
        for p in deleted:
            self.file_mtimes.pop(p, None)

        if changed or deleted:
            # Cheap full re-sort: history lists are short (<50 per key
            # typically). Cleaner than maintaining sort order on each
            # incremental upsert.
            for lst in self.entries.values():
                lst.sort(key=lambda e: e.date, reverse=True)

        self.built_at = _dt.datetime.now(_dt.timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z")
        self.files_indexed = len(self.file_mtimes)

    @staticmethod
    def _read_metadata_entry(path: Path) -> tuple[str, str, HistoryEntry] | None:
        """Parse one metadata.json into (tc_key, mp, HistoryEntry).

        Returns None when the file is unreadable, lacks the test_case_key
        field, or is otherwise unusable. Silent failure is correct here:
        the index walk is best-effort, and a single corrupt metadata
        file shouldn't kill the whole rebuild.
        """
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
        # The folder name is usually 4 levels above metadata.json
        # (output/<folder>/<testrun_slug>/<tester_slug>/<E-key>/metadata.json).
        # Walk up until we hit the output/ root or run out of parents.
        folder = ""
        for parent in path.parents:
            grandparent = parent.parent
            if grandparent.name == "output":
                folder = parent.name
                break
        entry = HistoryEntry(
            date=tr.get("execution_date") or "",
            status=tr.get("status") or "",
            e_key=path.parent.name,
            folder=folder,
            tester=tr.get("executed_by_name") or tr.get("executed_by"),
        )
        return tc_key, str(mp), entry

    def _upsert_entry(self, tc_key: str, mp: str, entry: HistoryEntry) -> None:
        """Replace any existing entry for the same e_key, else append.

        Re-running an audit on a key (which happens often) would
        otherwise duplicate it in history. e_key uniqueness within a
        history list is the right invariant — same execution =
        same record.
        """
        bucket_key = f"{tc_key}|{mp}"
        bucket = self.entries.setdefault(bucket_key, [])
        for i, existing in enumerate(bucket):
            if existing.e_key == entry.e_key:
                bucket[i] = entry
                return
        bucket.append(entry)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------
    def get_history(self, tc_key: str,
                    mp: str | None) -> list[HistoryEntry]:
        """Return all known executions of `tc_key` in marketplace `mp`,
        newest first. Empty list when nothing's indexed.

        `mp=None` queries the "unknown marketplace" bucket (folders
        that don't encode an MP in the testrun name, e.g. Additional
        Non Automated). Pass an explicit two-letter MP (e.g. "BR")
        for marketplace-scoped queries.
        """
        bucket_key = f"{tc_key}|{mp or _NO_MP}"
        return list(self.entries.get(bucket_key, []))

    @staticmethod
    def is_streak(history: list[HistoryEntry], status: str,
                  *,
                  exclude_e_key: str | None = None,
                  window: int = _DEFAULT_WINDOW,
                  threshold: int = _DEFAULT_THRESHOLD) -> bool:
        """Return True iff `history` shows >= threshold of `status` in
        the most recent `window` entries.

        `exclude_e_key`: drops the named E-key BEFORE evaluating the
        streak. Use this when checking history for the CURRENT
        execution — we want history excluding the current one,
        otherwise a Pass-after-Blocked would already have its Pass
        counted in the window and dilute the streak it's supposed to
        break.

        Status comparison uses _normalise_status so "N/A" / "Not
        Applicable" / "n/a" all match the same canonical form.
        """
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


# CLI — for backfill + debugging
def main(argv: list[str] | None = None) -> int:
    import argparse
    import config as argus_config

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
    out_dir = (args.out_dir or settings.extractor.out_dir).resolve()

    if args.cmd == "rebuild":
        # Wipe the cache + rebuild from scratch.
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
        # Top 5 most-executed test cases, for sanity.
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
            # Show every MP bucket for this test case.
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

    return 1  # unreachable


if __name__ == "__main__":
    sys.exit(main())
