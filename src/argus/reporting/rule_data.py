"""Small, protected report catalogs used to select evidence for rule tests."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import re

from argus.core import exec_paths
from argus.domain.team_rules import fingerprint

FOLDER = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,159}$")
EXECUTION = re.compile(r"^[A-Z][A-Z0-9_]*-E[0-9]+$")
IGNORED_DIRECTORIES = {"_replay", "_stale_backups"}
IGNORED_FILES = {"audit.json", "audit.md", "_last_error.json"}


def input_fingerprint(directory: Path) -> str:
    """Hash evaluation inputs, excluding generated audit output and leases."""
    manifest = {}
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if (
            set(relative.parts) & IGNORED_DIRECTORIES
            or path.name in IGNORED_FILES or path.name.endswith(".lock")
        ):
            continue
        if path.is_file():
            path.resolve().relative_to(directory.resolve())
            with path.open("rb") as stream:
                manifest[str(relative)] = hashlib.file_digest(stream, "sha256").hexdigest()
    return fingerprint(manifest)


def build_manifest(folder_root: Path, team: str, folder: str, stamp: str) -> dict:
    from argus.pipeline.analysis_status import derive_status

    title = folder_root.name
    meta = folder_root / "_folder_meta.json"
    if meta.is_file():
        title = json.loads(meta.read_text()).get("folder_name") or title
    items = []
    allowed = None
    latest = folder_root / "_latest_run.json"
    if latest.is_file():
        from argus.reporting.report import is_complete_latest_run_manifest
        run = json.loads(latest.read_text())
        if is_complete_latest_run_manifest(run):
            allowed = set(run["audit_keys"]) | set(run["failed_keys"])
    for path in exec_paths.iter_latest_audit_jsons(folder_root):
        key = path.parent.name
        if not EXECUTION.fullmatch(key) or allowed is not None and key not in allowed:
            continue
        try:
            audit = json.loads(path.read_text())
            metadata = json.loads((path.parent / "metadata.json").read_text())
            tr = (metadata.get("test_results") or [{}])[0]
            items.append({
                "key": key, "title": tr.get("test_case_name") or tr.get("test_case_key") or key,
                "run": tr.get("testrun_key") or tr.get("test_run_key") or "",
                "run_name": tr.get("testrun_name") or tr.get("test_run_name") or "",
                "status": tr.get("status") or "", "verdict": audit.get("overall_verdict"),
                "analysis_status": derive_status(audit), "execution_date": tr.get("execution_date") or "",
                "source": str(Path(folder_root.name) / path.parent.relative_to(folder_root)),
                "input_sha256": input_fingerprint(path.parent),
            })
        except (OSError, ValueError, TypeError, KeyError):
            # Incomplete extraction cannot provide an immutable input selection.
            continue
    result = {"schema": 1, "team": team, "folder": folder, "title": title,
              "timestamp": stamp, "items": sorted(items, key=lambda item: item["key"])}
    result["revision"] = fingerprint(result)
    return result




class LocalRuleReports:
    """Read saved evidence from explicitly mounted, local report roots."""

    def __init__(self, roots: dict[str, Path]):
        self.roots = {team: Path(root).resolve() for team, root in roots.items()}

    def _folder(self, team: str, folder: str) -> Path:
        from argus.operations.rule_service import RuleError

        root = self.roots.get(team)
        if root is None or not FOLDER.fullmatch(folder) or folder in {".", ".."}:
            raise RuleError(404, "Unknown report.")
        path = root / folder
        if path.is_symlink() or not path.is_dir() or path.resolve().parent != root:
            raise RuleError(404, "Unknown report.")
        # Reject external files before reading metadata or hashing evidence.
        if any(child.is_symlink() for child in path.rglob("*")):
            raise RuleError(400, "Saved report evidence must not contain symbolic links.")
        return path

    def reports(self, team: str) -> dict:
        root = self.roots.get(team)
        if root is None or not root.is_dir():
            return {"reports": []}
        rows = []
        for folder in sorted(root.iterdir()):
            if (
                not folder.is_dir() or folder.is_symlink()
                or folder.name.startswith("_") or not FOLDER.fullmatch(folder.name)
            ):
                continue
            manifest = self.manifest(team, folder.name)
            if manifest["items"]:
                rows.append({
                    "folder": folder.name,
                    "title": manifest["title"],
                    "timestamp": manifest["timestamp"],
                    "count": len(manifest["items"]),
                })
        return {"reports": rows}

    def manifest(self, team: str, folder: str) -> dict:
        from argus.operations.rule_service import RuleError

        path = self._folder(team, folder)
        try:
            # A repeat read is stable; actual evidence changes invalidate previews.
            modified = max(
                (child.stat().st_mtime for child in path.rglob("*") if child.is_file()),
                default=path.stat().st_mtime,
            )
            stamp = datetime.fromtimestamp(modified, timezone.utc).isoformat()
            return build_manifest(path, team, folder, stamp)
        except (OSError, ValueError, TypeError) as exc:
            raise RuleError(503, "Saved report evidence is incomplete or unreadable.") from exc


def selection_page(manifest: dict, query: dict) -> dict:
    from argus.operations.rule_service import RuleError

    search = str(query.get("q") or "").casefold().strip()[:200]
    try:
        offset = int(query.get("cursor") or 0)
    except (TypeError, ValueError) as exc:
        raise RuleError(400, "Invalid page.") from exc
    if offset < 0:
        raise RuleError(400, "Invalid page.")
    rows = [row for row in manifest["items"] if search in " ".join(
        str(row.get(key) or "") for key in ("key", "title", "run", "run_name", "status")
    ).casefold()]
    return {
        **{key: manifest[key] for key in ("folder", "title", "timestamp", "revision")},
        "items": [{k: v for k, v in row.items() if k not in {"source", "input_sha256"}}
                  for row in rows[offset:offset + 100]],
        "total": len(rows), "next_cursor": offset + 100 if offset + 100 < len(rows) else None,
    }
