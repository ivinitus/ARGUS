"""Remove retired account/device checks from saved Reader audits."""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import shutil
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any

from argus import config, runtime
from argus.core import atomic_io, exec_paths, paths
from argus.domain import finding_inference, workflow_rules
from argus.operations import audit_index, audit_lock
from argus.pipeline import analysis_status, batch
from argus.reporting.auditor_render import render_markdown

_RETIRED_RULES = frozenset({"R19", "R22"})


def reconcile(audit: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    """Reconcile only retired Reader policy; preserve other analysis and history."""
    updated = copy.deepcopy(audit)
    counts = {"R19": 0, "R22": 0, "device_results": 0, "device_failures": 0}
    kept = []
    for finding in audit.get("findings") or []:
        rule = (
            workflow_rules.rule_base(str(finding.get("rule") or "").upper())
            if isinstance(finding, dict) else ""
        )
        if rule in _RETIRED_RULES:
            counts[rule] += 1
        else:
            kept.append(copy.deepcopy(finding))
    if counts["R19"] or counts["R22"]:
        updated["findings"] = kept
    if "device_check" in updated:
        updated.pop("device_check")
        counts["device_results"] = 1
    modules = updated.get("analysis_modules")
    environment = modules.get("environment") if isinstance(modules, dict) else None
    if (
        isinstance(environment, dict)
        and environment.get("status") == "incomplete"
        and str(environment.get("diagnostic") or "").startswith("device-family analysis failed:")
    ):
        analysis_status.set_module(updated, "environment", "not_applicable")
        counts["device_failures"] = 1
    if not any(counts.values()):
        return audit, counts

    if counts["R19"] or counts["R22"]:
        # The original wording remains in the backup. Do not keep a summary
        # that still instructs the tester to satisfy a retired requirement.
        updated["summary"] = (
            "Applied Reader policy: account recording and device-family checks are optional. "
            f"{len(kept)} finding{'s' if len(kept) != 1 else ''} remain. "
            "Visual and audio analysis was not rerun."
        )
        updated.pop("_verdict_escalated_by_rules_from", None)
    updated = finding_inference.reconcile_verdict(updated)
    return updated, counts


def _read_audit(path: Path, root: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError(f"audit is outside the Reader output root: {path}")
    audit = json.loads(path.read_text())
    if not isinstance(audit, dict) or not isinstance(audit.get("findings", []), list):
        raise ValueError(f"invalid audit: {path}")
    return audit


def _write_change(
    path: Path, updated: dict[str, Any], backup: Path,
) -> None:
    """Back up both artifacts and roll back this pair if a write fails."""
    markdown = path.with_name("audit.md")
    metadata_path = path.with_name("metadata.json")
    for artifact in (markdown, metadata_path):
        if artifact.is_symlink():
            raise ValueError(f"refusing linked audit artifact: {artifact}")
    metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
    rendered = render_markdown(updated, metadata)
    backup.mkdir(parents=True, exist_ok=False)
    originals = [path, *([markdown] if markdown.exists() else [])]
    for artifact in originals:
        shutil.copy2(artifact, backup / artifact.name)
        (backup / artifact.name).chmod(0o600)
    before_stat = path.stat()
    try:
        audit_lock.ensure_ownership()
        atomic_io.atomic_write_text(markdown, rendered)
        atomic_io.atomic_write_json(path, updated)
        # Reports choose the latest duplicate by mtime. A policy correction
        # must not make an older saved execution look like a new audit.
        os.utime(path, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))
    except Exception:
        for artifact in originals:
            original = backup / artifact.name
            atomic_io.atomic_write_text(artifact, original.read_text())
            saved = original.stat()
            os.utime(artifact, ns=(saved.st_atime_ns, saved.st_mtime_ns))
        if markdown not in originals:
            markdown.unlink(missing_ok=True)
        raise


def backfill(settings: config.ArgusConfig, *, apply: bool = False) -> dict[str, Any]:
    """Visit all canonical Reader copies, excluding replay/backup artifacts."""
    root = paths.output_root(settings)
    if settings.general.profile != "reader" or settings.general.team_slug != "reader":
        raise ValueError("backfill-reader-policy requires the Reader profile and team")
    if any(batch._policy_rule_enabled(settings, rule) for rule in _RETIRED_RULES):
        raise ValueError("Reader must disable both R19 and R22 before cleanup")
    if not root.is_dir():
        raise ValueError(f"Reader output root does not exist: {root}")
    result: dict[str, Any] = {
        "team": "reader", "apply": apply, "root": str(root),
        "scanned": 0, "changed": 0, "R19": 0, "R22": 0,
        "device_results": 0, "device_failures": 0, "folders": [],
        "errors": [], "backup_root": None,
    }
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_root = root / "_stale_backups" / f"reader-policy-{stamp}-{uuid.uuid4().hex[:8]}"
    folders: set[str] = set()
    index_roots: set[Path] = set()
    records = []
    for path in sorted(exec_paths.iter_audit_jsons(root)):
        result["scanned"] += 1
        relative = path.relative_to(root)
        try:
            audit = _read_audit(path, root)
            for parent in path.parents:
                if parent == root.parent:
                    break
                if (parent / audit_index.DB_FILENAME).exists():
                    index_roots.add(parent)
            updated, counts = reconcile(audit)
            if updated == audit:
                continue
            if apply:
                with audit_lock.acquire_key(root, path.parent.name, owner="reader-policy-backfill"):
                    # A concurrent audit may have completed after the preview.
                    audit = _read_audit(path, root)
                    updated, counts = reconcile(audit)
                    if updated == audit:
                        continue
                    _write_change(path, updated, backup_root / relative.parent)
                result["backup_root"] = str(backup_root)
            result["changed"] += 1
            for rule, count in counts.items():
                result[rule] += count
            if len(relative.parts) > 2:
                folders.add(relative.parts[0])
            records.append({
                "path": str(relative), "removed": counts,
                "previous_verdict": audit.get("overall_verdict"),
                "verdict": updated.get("overall_verdict"),
            })
        except (OSError, ValueError, audit_lock.AuditLockBusy) as exc:
            result["errors"].append({"path": str(relative), "error": str(exc)})
    result["folders"] = sorted(folders)
    if apply:
        for index_root in sorted(index_roots):
            try:
                # Also retry a previously failed index refresh when all
                # audit files have already been reconciled.
                audit_index.sync(index_root, force=True)
            except (OSError, sqlite3.Error) as exc:
                result["errors"].append({"path": str(index_root), "error": str(exc)})
    if apply and result["changed"]:
        atomic_io.atomic_write_json(
            backup_root / "manifest.json", {**result, "audits": records},
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="back up and update saved audits")
    args = parser.parse_args(argv)
    try:
        settings = config.load(profile="reader")
        if args.apply:
            runtime.configure(settings)
        result = backfill(settings, apply=args.apply)
    except (OSError, ValueError, config.ConfigError) as exc:
        print(f"Reader cleanup failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
