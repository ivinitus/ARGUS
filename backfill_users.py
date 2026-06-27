"""Heal existing metadata.json files with unresolved USER keys.

The bug
-------
extractor.fetch_user_display_name (pre-2026-05-26) cached failures in a
per-session dict. A transient 429/503 on the FIRST attempt poisoned that
key for the run, and metadata.json was written with raw USER keys
plus None display-names. The dashboard then bucketed those executions
under "unknown" or showed "USER12345" where a tester name belonged.

99% of stored metadata.json files (1172 / 1183 on the current corpus)
have at least one USER reference somewhere — assignedTo, executedBy,
or both. Many of those have None *_name fields that should be resolvable
now that the persistent user cache (users.py) is in place.

What this script does
---------------------
1. Walks output/<folder>/.../metadata.json under the configured out_dir.
2. For each metadata.json:
     a. Find every USER key referenced (executedBy, assignedTo,
        plus any future fields we add — defensive scan over the whole
        test_results dict).
     b. For each key whose paired *_name field is None, try to resolve
        via users.UserCache.resolve.
     c. If newly resolved, update the metadata.json *in place* with the
        new display names (executed_by_name and assigned_to_name).
3. Atomic write per file. Idempotent — safe to re-run; a second pass
   over an already-healed file is a no-op (every name is non-None).

Cost
----
Resolutions are cached in users.json. The first run pays the Tracker API
cost once per unique unresolved key (typically a few dozen). Subsequent
runs against new folders are essentially free.

CLI
---
    python backfill_users.py                   # walks default out_dir
    python backfill_users.py --out-dir output  # explicit out_dir
    python backfill_users.py --dry-run         # report what would change

Output is a per-folder summary, then a grand-total line so an operator
running this for the first time on the existing corpus can see the
impact.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import config as argus_config
import extractor
import users


def _scan_unresolved(metadata: dict) -> set[str]:
    """Return all USER... keys whose paired *_name field is None.

    Two field pairs we know about today:
      executed_by  / executed_by_name
      assigned_to  / assigned_to_name

    A future test_results field with a similar shape would need to be
    added here. We deliberately scan only known pairs (rather than
    every USER-shaped string in the document) to avoid resolving
    a USER reference inside, say, a comment field where it might
    just be a bug-link reference and not an actual user we control.
    """
    unresolved: set[str] = set()
    results = metadata.get("test_results") or []
    for tr in results:
        if not isinstance(tr, dict):
            continue
        for key_field, name_field in (
            ("executed_by", "executed_by_name"),
            ("assigned_to", "assigned_to_name"),
        ):
            user_key = tr.get(key_field)
            user_name = tr.get(name_field)
            if (isinstance(user_key, str)
                    and user_key.startswith("USER")
                    and not user_name):
                unresolved.add(user_key)
    return unresolved


def _apply_resolutions(metadata: dict,
                       resolutions: dict[str, str]) -> bool:
    """Update metadata in-place with the given USER -> displayName map.

    Returns True iff any field was changed (so the caller knows whether
    to write back).
    """
    changed = False
    results = metadata.get("test_results") or []
    for tr in results:
        if not isinstance(tr, dict):
            continue
        for key_field, name_field in (
            ("executed_by", "executed_by_name"),
            ("assigned_to", "assigned_to_name"),
        ):
            user_key = tr.get(key_field)
            if not isinstance(user_key, str):
                continue
            new_name = resolutions.get(user_key)
            if new_name and tr.get(name_field) != new_name:
                tr[name_field] = new_name
                changed = True
    return changed


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write JSON atomically (tempfile + os.replace).

    Preserves the original 2-space indent that extractor.write_metadata
    uses so diffs in `git status` (or the operator's eyeball) only show
    the actual data changes, not whitespace churn.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8",
        dir=str(path.parent), delete=False,
        prefix=f".{path.name}.", suffix=".tmp",
    ) as tmp:
        json.dump(payload, tmp, indent=2)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def backfill(out_dir: Path, *, dry_run: bool = False) -> dict[str, int]:
    """Walk every metadata.json under out_dir and heal unresolved USER keys.

    Returns counters:
      files_scanned        — total metadata.json read
      files_with_unresolved — files that had at least one None *_name
      keys_attempted       — unique USER keys we tried to resolve
      keys_newly_resolved  — keys that successfully resolved (transitions
                             from failure-state to success in users.json)
      files_updated        — files where at least one *_name field was
                             written
      keys_still_failing   — keys that remained unresolved after this run
    """
    counters = {
        "files_scanned": 0,
        "files_with_unresolved": 0,
        "keys_attempted": 0,
        "keys_newly_resolved": 0,
        "files_updated": 0,
        "keys_still_failing": 0,
    }
    if not out_dir.exists():
        return counters

    # Build a single session for all Tracker calls. Reuse extractor's
    # auth-aware constructor so PAT + SSO cookies are wired up.
    session = extractor.build_session()
    settings = argus_config.load()
    base_url = settings.extractor.base_url

    cache = users.get_default_cache()
    seen_keys: dict[str, str | None] = {}

    for path in sorted(out_dir.rglob("metadata.json")):
        counters["files_scanned"] += 1
        try:
            metadata = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue

        unresolved = _scan_unresolved(metadata)
        if not unresolved:
            continue
        counters["files_with_unresolved"] += 1

        # For each unresolved key, look up via the persistent cache.
        # seen_keys de-duplicates within this run so the same USER
        # across 200 files only generates one Tracker call (after the
        # first; subsequent files hit the cache).
        resolutions: dict[str, str] = {}
        for user_key in unresolved:
            if user_key in seen_keys:
                resolved = seen_keys[user_key]
            else:
                # Was this previously a known failure that has now
                # become a success (or vice versa)? cache.resolve
                # handles backoff privately — if we tried 5 minutes
                # ago and the backoff says "wait", it returns None
                # without a Tracker call.
                was_resolved_before = cache.is_resolved(user_key)
                resolved = cache.resolve(session, base_url, user_key)
                seen_keys[user_key] = resolved
                counters["keys_attempted"] += 1
                if resolved and not was_resolved_before:
                    counters["keys_newly_resolved"] += 1
                if not resolved:
                    counters["keys_still_failing"] += 1
            if resolved:
                resolutions[user_key] = resolved

        if not resolutions:
            continue

        if _apply_resolutions(metadata, resolutions):
            counters["files_updated"] += 1
            if not dry_run:
                _atomic_write_json(path, metadata)

    # Persist the cache after the walk so subsequent runs benefit from
    # everything we resolved.
    if not dry_run:
        cache.save()

    return counters


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Heal metadata.json files whose USER keys went "
                    "unresolved on the original audit run.")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="output dir to scan (default: from config.toml).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would change without writing.")
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args(argv)

    settings = argus_config.load(args.config)
    out_dir = (args.out_dir or settings.extractor.out_dir).resolve()
    extractor.configure_logging(False)

    print(f"backfilling user resolutions under {out_dir}")
    if args.dry_run:
        print("(dry-run: no files will be written)")
    counters = backfill(out_dir, dry_run=args.dry_run)
    print()
    print(f"files_scanned:         {counters['files_scanned']}")
    print(f"files_with_unresolved: {counters['files_with_unresolved']}")
    print(f"keys_attempted:        {counters['keys_attempted']}")
    print(f"keys_newly_resolved:   {counters['keys_newly_resolved']}")
    print(f"files_updated:         {counters['files_updated']}")
    print(f"keys_still_failing:    {counters['keys_still_failing']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
