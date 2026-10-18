"""Replay immutable policy snapshots against isolated copies of saved executions."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from argus.core import atomic_io, exec_paths
from argus.domain import team_rules
from argus.operations import rule_runtime
from argus.operations.rule_service import REQUEST_ID, now, revision_key
from argus.operations.rule_store import RuleConflict
from argus.reporting.rule_data import input_fingerprint


class PreviewInputError(ValueError):
    pass


def summarize(audit: dict) -> dict:
    from argus.pipeline.analysis_status import derive_status

    return {
        "overall_verdict": audit.get("overall_verdict", "concerns"),
        # Advisory failures remain verdict-neutral in normal audits, but a
        # publication preview must successfully evaluate the selected rules.
        "analysis_status": "incomplete" if audit.get("_custom_rule_diagnostic") else derive_status(audit),
        "findings": [{k: f[k] for k in (
            "rule", "title", "description", "severity", "advisory", "assessment_status",
        ) if k in f} for f in (audit.get("findings") or []) if isinstance(f, dict)],
    }


def copy_execution(source: Path, destination: Path, root: Path) -> None:
    """Freeze inputs without following links outside the selected team's output."""
    source.resolve().relative_to(root.resolve())
    for path in source.rglob("*"):
        if path.is_symlink():
            path.resolve().relative_to(root.resolve())
    shutil.copytree(
        source, destination,
        ignore=shutil.ignore_patterns("_replay", "_stale_backups", "*.lock", "audit.md", "_last_error.json"),
    )


def evaluate_local(settings: Any, snapshot: dict, execution: Path, history: dict | None = None) -> dict:
    """Used only in a fresh subprocess so process-wide policy caches cannot mix."""
    from argus import runtime
    from argus.pipeline import auditor, batch
    from argus.operations import audit_lock

    settings.auditor.rule_snapshot = deepcopy(snapshot)
    settings.general.state_dir = execution.parent / "state"
    settings.extractor.out_dir = execution.parent
    runtime.configure(settings)
    if history is not None:
        from argus.domain.history_index import HistoryIndex
        HistoryIndex.use_snapshot(history)
    # Source leases are held by the parent. Children operate only on copies.
    audit_lock.reset()
    audit_path = execution / "audit.json"
    audit_path.unlink(missing_ok=True)
    metadata = json.loads((execution / "metadata.json").read_text())
    from argus.domain.workflow_rules import status_role
    status = status_role((metadata.get("test_results") or [{}])[0].get("status"))
    if status == "not_applicable":
        batch._write_not_applicable_audit(execution, execution.name)
    elif status in {"blocked", "in_progress"} and not batch._has_any_evidence(execution):
        batch._write_blocked_audit(execution, execution.name)
    elif not batch._has_any_evidence(execution):
        batch._write_no_evidence_audit(execution, execution.name, None)
    else:
        cfg = auditor.AuditConfig.from_settings(settings.auditor, execution, cost_rates=settings.costs.model_rates)
        if auditor.run(cfg) != 0:
            raise RuntimeError("Evidence replay failed")
        if status in {"blocked", "in_progress"}:
            from argus.pipeline.orchestrator import _merge_blocked_base_finding
            _merge_blocked_base_finding(execution)
        if batch._post_checks_enabled(settings):
            batch._run_env_check_for_key_locked(settings, audit_path)
    return json.loads(audit_path.read_text())


def run_preview(store: Any, settings: Any, identifier: str) -> None:
    from argus import runtime
    from argus.operations import audit_lock

    team = settings.general.team_slug
    key = "preview#" + identifier
    preview = store.get(team, key)
    if not preview or preview.get("phase") != "pending":
        return
    running = {**preview, "phase": "running", "started_at": now()}
    try:
        store.put(team, key, running, phase="pending")
    except RuleConflict:
        return
    try:
        runtime.configure(settings)
        source_root = settings.extractor.out_dir
        scratch = settings.general.state_dir / "rule-previews" / identifier
        scratch.mkdir(parents=True, exist_ok=False)
        active = store.get(team, revision_key(preview["active_revision"]))
        draft = store.get(team, preview["draft_key"])
        snapshots = {"before": active, "after": {
            "revision": "draft", "document": draft["document"], "sha256": draft["sha256"],
        }}
        if draft["sha256"] != preview["draft_sha256"]:
            raise ValueError("Draft fingerprint changed")
        from argus.domain.history_index import HistoryIndex
        history = HistoryIndex.get_shared(source_root).snapshot()
        history_path = scratch / "history.json"
        atomic_io.atomic_write_json(history_path, history)
        by_key = {path.parent.name: path.parent for path in exec_paths.iter_latest_audit_jsons(source_root)}
        results = []
        coverage_copies: dict[Path, Path] = {}
        for execution_key in preview["executions"]:
            source = by_key.get(execution_key)
            selected = preview.get("sources", {}).get(execution_key)
            if selected:
                relative = Path(selected["source"])
                if relative.is_absolute() or ".." in relative.parts:
                    raise PreviewInputError("The selected source is invalid. Regenerate the report.")
                matches = [
                    path.parent for path in exec_paths.iter_audit_jsons(source_root)
                    if path.parent.name == execution_key
                    and path.parent.parts[-len(relative.parts):] == relative.parts
                ]
                if len(matches) != 1:
                    raise PreviewInputError(f"Saved inputs for {execution_key} are unavailable. Regenerate the report.")
                source = matches[0]
            if source is None:
                raise PreviewInputError(f"{execution_key} has no saved audit in this team.")
            frozen = scratch / "inputs" / execution_key
            with audit_lock.acquire_key(source_root, execution_key, owner="rules-preview"):
                copy_execution(source, frozen, source_root)
                if source.parent not in coverage_copies:
                    folder_copy = scratch / "coverage" / str(len(coverage_copies))
                    folder_copy.mkdir(parents=True)
                    coverage_copies[source.parent] = folder_copy
                    roster = source.parent / "_coverage.json"
                    if roster.is_file():
                        roster.resolve().relative_to(source_root.resolve())
                        shutil.copyfile(roster, folder_copy / "_coverage.json")
            item: dict[str, Any] = {
                "execution": execution_key, "input_sha256": input_fingerprint(frozen),
            }
            if selected:
                if item["input_sha256"] != selected["input_sha256"]:
                    raise PreviewInputError(f"Saved evidence for {execution_key} changed. Reload the report and test again.")
                item["title"] = selected["title"]
            for label, snapshot in snapshots.items():
                if team_rules.fingerprint(snapshot["document"]) != snapshot["sha256"]:
                    raise ValueError("Preview policy fingerprint does not match")
                work = scratch / label / execution_key
                shutil.copytree(frozen, work)
                snapshot_path = scratch / (label + ".json")
                atomic_io.atomic_write_json(snapshot_path, snapshot)
                log_path = scratch / f"{execution_key}-{label}.log"
                with log_path.open("w") as log:
                    subprocess.run(
                        [sys.executable, "-m", "argus", "rules-preview", "--evaluate",
                         str(work), "--snapshot", str(snapshot_path)],
                        check=True, stdout=log, stderr=log, timeout=3600,
                        env={**os.environ, "ARGUS_RULE_PREVIEW_CHILD": "1",
                             "ARGUS_RULE_PREVIEW_HISTORY": str(history_path)},
                    )
                item[label] = summarize(json.loads((work / "audit.json").read_text()))
            results.append(item)
        complete = all(
            item[label]["analysis_status"] != "incomplete"
            for item in results for label in ("before", "after")
        )
        result = {
            **running, "phase": "complete" if complete else "failed",
            "results": results, "completed_at": now(),
        }
        result["coverage"] = compare_coverage(
            list(coverage_copies.values()),
            snapshots, history,
        )
        if not complete:
            result["diagnostic"] = "One or more evidence assessments did not complete. Resolve them and run another preview."
        # Bounded result storage must never silently truncate findings.
        if len(json.dumps(result).encode()) > 300_000:
            raise ValueError("Preview has too many findings. Select fewer executions.")
        store.put(team, key, result, phase="running")
    except Exception as exc:
        store.put(team, key, {
            **running, "phase": "failed", "completed_at": now(),
            "diagnostic": str(exc) if isinstance(exc, PreviewInputError) else (
                f"Rule tests could not complete ({type(exc).__name__}). Check saved evidence and worker logs, then test again."
            ),
        }, phase="running")
        raise


def main(argv: list[str] | None = None) -> int:
    from argus import config

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview")
    parser.add_argument("--evaluate", type=Path)
    parser.add_argument("--snapshot", type=Path)
    args = parser.parse_args(argv)
    settings = config.load()
    if args.evaluate:
        if os.environ.get("ARGUS_RULE_PREVIEW_CHILD") != "1" or not args.snapshot:
            parser.error("Evaluation is an internal preview worker operation.")
        snapshot = json.loads(args.snapshot.read_text())
        history_path = os.environ.get("ARGUS_RULE_PREVIEW_HISTORY")
        history = json.loads(Path(history_path).read_text()) if history_path else None
        evaluate_local(settings, snapshot, args.evaluate, history)
        return 0
    if not args.preview or not REQUEST_ID.fullmatch(args.preview):
        parser.error("A valid preview identifier is required.")
    run_preview(rule_runtime.store_from_environment(), settings, args.preview)
    return 0


def compare_coverage(folders: list[Path], snapshots: dict, history: dict) -> list[dict]:
    """Coverage rules need the full saved roster, including unexecuted items."""
    from argus.config.models import PolicySettings
    from argus.domain import folder_coverage, workflow_rules
    from argus.domain.history_index import HistoryEntry, HistoryIndex

    a, b = (snapshots[label]["document"]["policy"] for label in ("before", "after"))
    keys = ("completion_threshold", "min_assigned_for_completion", "enabled_rules",
            "disabled_rules", "rule_severities", "statuses", "status_aliases")
    if all(a[key] == b[key] for key in keys):
        return []
    # Non-coverage R-rule edits do not require an unrelated folder snapshot.
    def coverage_policy(policy: dict) -> dict:
        return {**{key: policy[key] for key in keys if key not in {
            "enabled_rules", "disabled_rules", "rule_severities",
        }}, "enabled": [r for r in ("R15A", "R15B", "R15C") if (
            not policy["enabled_rules"] or r in policy["enabled_rules"] or "R15" in policy["enabled_rules"]
        ) and r not in policy["disabled_rules"] and "R15" not in policy["disabled_rules"]],
            "severities": {k: v for k, v in policy["rule_severities"].items() if k.startswith("R15")}}
    if coverage_policy(a) == coverage_policy(b):
        return []
    index = HistoryIndex(entries={k: [HistoryEntry.from_dict(e) for e in v] for k, v in history.items()})
    results = []
    try:
        for folder in sorted(set(folders)):
            path = folder / "_coverage.json"
            data = json.loads(path.read_text()) if path.is_file() else {}
            if "policy_items" not in data:
                raise ValueError("Re-audit the selected folder to save a complete roster before previewing coverage changes.")
            snapshot = folder_coverage.CoverageSnapshot(
                folder_name=data["folder"],
                items=[folder_coverage.CoverageItem(**item) for item in data["policy_items"]],
            )
            result: dict[str, Any] = {"folder": snapshot.folder_name}
            for label, policy in (("before", a), ("after", b)):
                workflow_rules.configure(PolicySettings(**policy))
                result[label] = folder_coverage.compute_gaps(
                    snapshot, history_idx=index, completion_threshold=policy["completion_threshold"],
                    min_assigned_for_completion=policy["min_assigned_for_completion"],
                )
            results.append(result)
    finally:
        workflow_rules.configure(PolicySettings(**a))
    return results
