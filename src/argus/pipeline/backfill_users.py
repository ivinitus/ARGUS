"""Heal existing metadata.json files with unresolved JIRAUSER keys."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from argus.core import atomic_io
from argus.core import exec_paths
from argus import config as argus_config
from argus.pipeline import extractor
from argus.core import users
from argus import runtime


def _scan_unresolved(metadata: dict) -> set[str]:
    """Return all JIRAUSER..."""
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
                    and user_key.startswith("JIRAUSER")
                    and not user_name):
                unresolved.add(user_key)
    return unresolved


def _apply_resolutions(metadata: dict,
                       resolutions: dict[str, str]) -> bool:
    """Update metadata in-place with the given JIRAUSER -> displayName map."""
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
    """Write JSON atomically (tempfile + os.replace)."""
    atomic_io.atomic_write_json(path, payload)


def backfill(
    out_dir: Path,
    *,
    dry_run: bool = False,
    settings: argus_config.ArgusConfig | None = None,
) -> dict[str, int]:
    """Walk every metadata.json under out_dir and heal unresolved JIRAUSER keys."""
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

    if settings is None:
        settings = argus_config.load()
        session = extractor.build_session()
    else:
        session = extractor.build_session(settings.extractor)
    base_url = settings.extractor.base_url

    cache = users.get_default_cache()
    seen_keys: dict[str, str | None] = {}

    for path in sorted(out_dir.rglob("metadata.json")):
        if exec_paths.is_excluded(path):
            continue
        counters["files_scanned"] += 1
        try:
            metadata = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue

        unresolved = _scan_unresolved(metadata)
        if not unresolved:
            continue
        counters["files_with_unresolved"] += 1

        resolutions: dict[str, str] = {}
        for user_key in unresolved:
            if user_key in seen_keys:
                resolved = seen_keys[user_key]
            else:
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

    if not dry_run:
        cache.save()

    return counters


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Heal metadata.json files whose JIRAUSER keys went "
                    "unresolved on the original audit run.")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="output dir to scan (default: from config.toml).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would change without writing.")
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args(argv)

    settings = argus_config.load(args.config)
    runtime.configure(settings)
    out_dir = (args.out_dir or settings.extractor.out_dir).resolve()
    extractor.configure_logging(False)

    print(f"backfilling user resolutions under {out_dir}")
    if args.dry_run:
        print("(dry-run: no files will be written)")
    counters = backfill(out_dir, dry_run=args.dry_run, settings=settings)
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
