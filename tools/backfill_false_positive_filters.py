"""Reconcile historical audits with current false-positive policy."""
from __future__ import annotations

import argparse
import collections
import copy
import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from argus.core import atomic_io, exec_paths
from argus.core.redaction import redact_credentials
from argus.domain import execution_policy, finding_inference, workflow_rules
from argus.pipeline import auditor
from argus.reporting.auditor_render import render_markdown

MANIFEST_VERSION = 3
_INVALID_MOBILE_R19_RE = re.compile(
    r"\b(?:desktop|devtools|emulat(?:ed|ion)|device toolbar|browser profile)\b",
    flags=re.I,
)


@dataclass(frozen=True)
class ReconcileResult:
    audit: dict[str, Any]
    metadata: dict[str, Any]
    model_findings_removed: int
    r21_refreshed: bool
    transformations: dict[str, int]

    @property
    def changed(self) -> bool:
        return any(self.transformations.values())


@dataclass(frozen=True)
class _Change:
    audit_path: Path
    metadata_path: Path
    markdown_path: Path
    original_audit: dict[str, Any]
    original_metadata: dict[str, Any]
    result: ReconcileResult


def _sanitize_value(value: Any) -> tuple[Any, int]:
    if isinstance(value, str):
        sanitized = redact_credentials(value)
        return sanitized, int(sanitized != value)
    if isinstance(value, list):
        output: list[Any] = []
        changed = 0
        for item in value:
            clean, item_changed = _sanitize_value(item)
            output.append(clean)
            changed += item_changed
        return output, changed
    if isinstance(value, dict):
        output_dict: dict[str, Any] = {}
        changed = 0
        for key, item in value.items():
            clean, item_changed = _sanitize_value(item)
            output_dict[str(key)] = clean
            changed += item_changed
        return output_dict, changed
    return value, 0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _debug_enabled(audit: dict[str, Any]) -> bool:
    return any(
        key in audit
        for key in (
            "_chunk_summary",
            "_chunk_diagnostics",
            "_consensus",
            "_invalid_original",
            "_invalid_repair",
            "_raw",
            "_runtime_diagnostic",
        )
    )


def _is_invalid_mobile_r19(
    finding: dict[str, Any],
    metadata: dict[str, Any],
    execution_dir: Path,
) -> bool:
    if finding.get("rule") != "R19":
        return False
    if execution_policy.expected_execution_mode(
        metadata,
        execution_dir,
    ) != execution_policy.MOBILE_EMULATION_ALLOWED:
        return False
    return bool(
        _INVALID_MOBILE_R19_RE.search(
            str(finding.get("description") or "")
        )
    )


def _apply_rule_policy(
    audit: dict[str, Any],
    metadata: dict[str, Any],
    execution_dir: Path,
) -> tuple[dict[str, Any], collections.Counter[str]]:
    transformations: collections.Counter[str] = collections.Counter()
    updated = dict(audit)
    findings: list[Any] = []
    expected_mode = execution_policy.expected_execution_mode(
        metadata,
        execution_dir,
    )
    advisory_missing_url = expected_mode in {
        execution_policy.MOBILE_EMULATION_ALLOWED,
        execution_policy.PHYSICAL_DEVICE,
    }
    for raw in updated.get("findings") or []:
        if not isinstance(raw, dict):
            findings.append(raw)
            continue
        finding = dict(raw)
        rule = finding.get("rule")
        if rule == "R9-hover":
            transformations["r9_hover_removed"] += 1
            continue
        if _is_invalid_mobile_r19(finding, metadata, execution_dir):
            transformations["invalid_mobile_r19_removed"] += 1
            continue
        if rule == "R9-amb" and advisory_missing_url:
            replacement = {
                **finding,
                "severity": "low",
                "category": "insufficient_evidence",
                "action": "add_evidence",
                "confidence": "low",
                "advisory": True,
            }
            if replacement != finding:
                transformations["r9_amb_demoted"] += 1
            finding = replacement
        findings.append(finding)
    updated["findings"] = findings
    return updated, transformations


def reconcile_audit(
    audit: dict[str, Any],
    metadata: dict[str, Any],
    execution_dir: Path | None = None,
) -> ReconcileResult:
    """Apply current policy and redact persisted tester-authored text."""
    execution_dir = execution_dir or Path("")
    sanitized_metadata, metadata_redactions = _sanitize_value(metadata)
    sanitized_audit, audit_redactions = _sanitize_value(audit)
    transformations: collections.Counter[str] = collections.Counter()
    transformations["metadata_values_redacted"] = metadata_redactions
    transformations["audit_values_redacted"] = audit_redactions

    original_findings = list(sanitized_audit.get("findings") or [])
    updated = auditor._drop_false_positive_model_findings(
        copy.deepcopy(sanitized_audit),
        sanitized_metadata,
    )
    filtered_findings = list(updated.get("findings") or [])
    removed = len(original_findings) - len(filtered_findings)
    transformations["model_findings_removed"] = removed

    updated, policy_changes = _apply_rule_policy(
        updated,
        sanitized_metadata,
        execution_dir,
    )
    transformations.update(policy_changes)

    filtered_findings = list(updated.get("findings") or [])
    old_r21 = [
        finding for finding in filtered_findings
        if isinstance(finding, dict)
        and finding.get("source") == "rule"
        and finding.get("rule") == "R21"
    ]
    r21_refreshed = False
    if old_r21:
        replacement = workflow_rules.build_r21_finding(sanitized_metadata)
        comparison_fields = (
            "severity",
            "page",
            "step_index",
            "description",
            "source",
            "rule",
        )

        def core(finding: dict[str, Any]) -> dict[str, Any]:
            return {key: finding.get(key) for key in comparison_fields}

        expected = [core(replacement)] if replacement else []
        r21_refreshed = [core(finding) for finding in old_r21] != expected
        if r21_refreshed:
            transformations["r21_refreshed"] += 1
            kept = [
                finding for finding in filtered_findings
                if not (
                    isinstance(finding, dict)
                    and finding.get("source") == "rule"
                    and finding.get("rule") == "R21"
                )
            ]
            if replacement is not None:
                kept.append(replacement)
            updated["findings"] = kept

    if any(transformations.values()):
        updated = auditor.enrich_audit(updated, sanitized_metadata)
        updated = auditor._finalize_verdict(updated)
    else:
        updated = audit
        sanitized_metadata = metadata

    return ReconcileResult(
        audit=updated,
        metadata=sanitized_metadata,
        model_findings_removed=removed,
        r21_refreshed=r21_refreshed,
        transformations=dict(transformations),
    )


def _metrics(audits: list[dict[str, Any]]) -> dict[str, Any]:
    verdicts: collections.Counter[str] = collections.Counter()
    severity: collections.Counter[str] = collections.Counter()
    operational = advisories = 0
    for audit in audits:
        verdicts[str(audit.get("overall_verdict") or "unknown")] += 1
        for finding in audit.get("findings") or []:
            if not isinstance(finding, dict):
                continue
            severity[str(finding.get("severity") or "unknown")] += 1
            if finding_inference.is_advisory_finding(finding):
                advisories += 1
            else:
                operational += 1
    return {
        "audits": len(audits),
        "verdicts": dict(sorted(verdicts.items())),
        "severity": dict(sorted(severity.items())),
        "operational_findings": operational,
        "advisories": advisories,
        "total_findings": operational + advisories,
    }


def _artifact_record(
    *,
    root: Path,
    target: Path,
    backup: Path,
    before_sha256: str,
) -> dict[str, str]:
    return {
        "target": str(target.relative_to(root)),
        "backup": str(backup.relative_to(root)),
        "before_sha256": before_sha256,
        "backup_sha256": _sha256(backup),
        "after_sha256": _sha256(target),
    }


def backfill(
    root: Path,
    *,
    apply: bool,
) -> tuple[int, int, int, Path | None]:
    """Reconcile audits under root, optionally writing guarded changes."""
    root = root.resolve()
    changes: list[_Change] = []
    before_all: list[dict[str, Any]] = []
    after_all: list[dict[str, Any]] = []
    for audit_path in sorted(exec_paths.iter_audit_jsons(root)):
        metadata_path = audit_path.with_name("metadata.json")
        if not metadata_path.exists():
            continue
        original_audit = json.loads(audit_path.read_text())
        original_metadata = json.loads(metadata_path.read_text())
        result = reconcile_audit(
            original_audit,
            original_metadata,
            audit_path.parent,
        )
        before_all.append(original_audit)
        after_all.append(result.audit)
        if result.changed:
            changes.append(_Change(
                audit_path=audit_path,
                metadata_path=metadata_path,
                markdown_path=audit_path.with_name("audit.md"),
                original_audit=original_audit,
                original_metadata=original_metadata,
                result=result,
            ))

    removed = sum(row.result.model_findings_removed for row in changes)
    r21_count = sum(row.result.r21_refreshed for row in changes)
    if not apply or not changes:
        return len(changes), removed, r21_count, None

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d_%H%M%SZ")
    backup_root = root / f"_false_positive_backup_{stamp}"
    backup_root.mkdir(parents=True, exist_ok=False)
    artifact_specs: list[tuple[Path, Path, str]] = []
    backups: dict[Path, Path | None] = {}

    for change in changes:
        relative_dir = change.audit_path.parent.relative_to(root)
        destination = backup_root / relative_dir
        destination.mkdir(parents=True, exist_ok=True)

        clean_original_audit, _ = _sanitize_value(change.original_audit)
        audit_backup = destination / "audit.original.json"
        atomic_io.atomic_write_json(audit_backup, clean_original_audit)
        artifact_specs.append((
            change.audit_path,
            audit_backup,
            _sha256(change.audit_path),
        ))
        backups[change.audit_path] = audit_backup

        clean_original_metadata, _ = _sanitize_value(change.original_metadata)
        metadata_backup = destination / "metadata.original.json"
        atomic_io.atomic_write_json(metadata_backup, clean_original_metadata)
        artifact_specs.append((
            change.metadata_path,
            metadata_backup,
            _sha256(change.metadata_path),
        ))
        backups[change.metadata_path] = metadata_backup

        markdown_backup: Path | None = None
        if change.markdown_path.exists():
            markdown_backup = destination / "audit.original.md"
            atomic_io.atomic_write_text(
                markdown_backup,
                redact_credentials(change.markdown_path.read_text()),
            )
            artifact_specs.append((
                change.markdown_path,
                markdown_backup,
                _sha256(change.markdown_path),
            ))
        backups[change.markdown_path] = markdown_backup

    touched: list[_Change] = []
    try:
        for change in changes:
            touched.append(change)
            atomic_io.atomic_write_json(change.audit_path, change.result.audit)
            atomic_io.atomic_write_json(
                change.metadata_path,
                change.result.metadata,
            )
            atomic_io.atomic_write_text(
                change.markdown_path,
                render_markdown(
                    change.result.audit,
                    change.result.metadata,
                    debug=_debug_enabled(change.result.audit),
                ),
            )
    except Exception:
        for change in reversed(touched):
            for target in (
                change.audit_path,
                change.metadata_path,
                change.markdown_path,
            ):
                backup = backups[target]
                if backup is None:
                    target.unlink(missing_ok=True)
                else:
                    atomic_io.atomic_write_text(target, backup.read_text())
        raise

    transformation_totals: collections.Counter[str] = collections.Counter()
    for change in changes:
        transformation_totals.update(change.result.transformations)
    artifacts = [
        _artifact_record(
            root=root,
            target=target,
            backup=backup,
            before_sha256=before_hash,
        )
        for target, backup, before_hash in artifact_specs
    ]
    manifest = {
        "version": MANIFEST_VERSION,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "root": str(root),
        "audits_updated": len(changes),
        "transformations": dict(sorted(transformation_totals.items())),
        "metrics": {
            "before": _metrics(before_all),
            "after": _metrics(after_all),
        },
        "artifacts": artifacts,
        "backup_content_sanitized": True,
    }
    atomic_io.atomic_write_json(backup_root / "manifest.json", manifest)
    return len(changes), removed, r21_count, backup_root


def restore(backup_root: Path) -> int:
    """Restore a v3 backup only when every migrated target is unchanged."""
    backup_root = backup_root.resolve()
    manifest_path = backup_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("version") != MANIFEST_VERSION:
        raise ValueError("restore requires a version 3 manifest")
    root = Path(str(manifest["root"])).resolve()
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("manifest has no artifact list")

    verified: list[tuple[Path, Path]] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ValueError("manifest artifact is invalid")
        target = (root / str(artifact["target"])).resolve()
        backup = (root / str(artifact["backup"])).resolve()
        if root not in target.parents or backup_root not in backup.parents:
            raise ValueError("manifest artifact escapes the approved root")
        if not target.exists() or not backup.exists():
            raise FileNotFoundError(f"restore artifact missing: {target}")
        if _sha256(target) != artifact.get("after_sha256"):
            raise RuntimeError(
                f"refusing restore; migrated file changed afterward: {target}"
            )
        if _sha256(backup) != artifact.get("backup_sha256"):
            raise RuntimeError(f"refusing restore; backup hash mismatch: {backup}")
        verified.append((target, backup))

    for target, backup in verified:
        atomic_io.atomic_write_text(target, backup.read_text())
    return len(verified)


def sanitize_backup(backup_root: Path) -> int:
    """Redact an older backup in place and refresh v3 backup hashes."""
    backup_root = backup_root.resolve()
    changed = 0
    for path in sorted(backup_root.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        original = path.read_text()
        if path.suffix == ".json":
            clean, _ = _sanitize_value(json.loads(original))
            rendered = json.dumps(clean, indent=2)
        else:
            rendered = redact_credentials(original)
        if rendered != original:
            atomic_io.atomic_write_text(path, rendered)
            changed += 1

    manifest_path = backup_root / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        for artifact in manifest.get("artifacts") or []:
            backup = Path(str(manifest["root"])) / str(artifact["backup"])
            if backup.exists():
                artifact["backup_sha256"] = _sha256(backup)
        manifest["backup_content_sanitized"] = True
        manifest["sanitized_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        atomic_io.atomic_write_json(manifest_path, manifest)
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--restore", type=Path)
    parser.add_argument("--sanitize-backup", type=Path)
    args = parser.parse_args()

    actions = sum(bool(value) for value in (
        args.apply,
        args.restore,
        args.sanitize_backup,
    ))
    if actions > 1:
        parser.error("--apply, --restore, and --sanitize-backup are exclusive")
    if args.restore:
        count = restore(args.restore)
        print(f"restored {count} artifact(s) from sanitized backup")
        return 0
    if args.sanitize_backup:
        count = sanitize_backup(args.sanitize_backup)
        print(f"sanitized {count} backup artifact(s)")
        return 0
    if args.root is None:
        parser.error("--root is required for dry-run or --apply")

    from argus import config as argus_config
    from argus import runtime

    runtime.configure(argus_config.load(args.config))
    audits, removed, r21_count, backup = backfill(
        args.root,
        apply=args.apply,
    )
    action = "updated" if args.apply else "would update"
    print(
        f"{action} {audits} audit(s); removed {removed} model finding(s); "
        f"refreshed {r21_count} R21 finding(s)"
    )
    if backup is not None:
        print(f"backup: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
