"""Shared "run env_check and merge into the audit" orchestration."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from argus.core import costs as cost_tracking
from argus.domain import execution_policy

log = logging.getLogger("argus.env_check_merge")

STALE_ENV_RULES = (
    "R9", "R9-doc", "R9-amb", "R9-hover", "R9-nav", "R9-susp",
    "R13", "R13-amb", "R19",
)


def is_stale_env_finding(f: dict[str, Any]) -> bool:
    return bool(
        f.get("source") in ("env_check", "device_check")
        or f.get("rule") in STALE_ENV_RULES
    )


def effective_stride(base_stride: int, is_webview: bool) -> int:
    """Webview folders get a tighter sample stride — those folders are where env-compliance ambiguity matters most."""
    if is_webview and base_stride > 1:
        return max(2, base_stride - 1)
    return base_stride


def build_env_check_field(ec: dict[str, Any]) -> dict[str, Any]:
    """The audit["env_check"] block, in its FULL current shape."""
    field = {
        "version": ec["version"],
        "verdict": ec["verdict"],
        "preprod_urls": ec["preprod_urls"],
        "prod_urls": ec["prod_urls"],
        "prod_urls_top": ec.get("prod_urls_top", []),
        "prod_urls_bottom": ec.get("prod_urls_bottom", []),
        "suspect_prod_urls_top": ec.get("suspect_prod_urls_top", []),
        "images_with_preprod": ec["images_with_preprod"],
        "images_with_prod": ec["images_with_prod"],
        "images_scanned": ec["images_scanned"],
        "images_total": ec.get("images_total", ec["images_scanned"]),
        "sample_stride": ec.get("sample_stride", 1),
        "region_crop_height": ec.get("region_crop_height", 0),
        "region_crop_bottom_height": ec.get("region_crop_bottom_height", 0),
        "per_image": ec["per_image"],
        "navigation_notices": ec.get("navigation_notices", []),
    }
    if "contextual_policy" in ec:
        field["contextual_policy"] = ec["contextual_policy"]
    for key in ("marketplace_urls", "marketplace_uncertain_urls"):
        if key in ec:
            field[key] = ec[key]
    return field


@dataclass
class MergeOutcome:
    audit: dict[str, Any]
    ec: dict[str, Any]
    added_finding: bool = False
    had_stale_finding: bool = False


def write_merged_audit(
    audit_path: Path,
    audit: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> None:
    """Write the merged audit.json AND re-render audit.md beside it."""
    from argus.core import atomic_io
    atomic_io.atomic_write_json(audit_path, audit)
    try:
        from argus.pipeline import auditor
        if metadata is None:
            metadata = {}
            metadata_path = audit_path.parent / "metadata.json"
            if metadata_path.exists():
                metadata = json.loads(metadata_path.read_text())
        atomic_io.atomic_write_text(
            audit_path.parent / "audit.md",
            auditor.render_markdown(audit, metadata))
    except Exception:  # pragma: no cover
        log.warning("audit.md render failed for %s (audit.json is canonical)",
                    audit_path.parent.name, exc_info=True)


def run_and_merge(
    audit: dict[str, Any],
    execution_dir: Path,
    *,
    sample_stride: int,
    region_crop_height: int,
    region_crop_bottom_height: int,
    model_api_url: str,
    api_key_env: str | None,
    auxiliary_model_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    usage_tracker: cost_tracking.UsageTracker | None = None,
    model_rates: dict[str, dict[str, float]] | None = None,
    environment_enabled: bool = True,
    marketplace_enabled: bool = True,
    device_enabled: bool = True,
) -> MergeOutcome:
    """Run enabled post-audit checks and merge them into `audit`."""
    from argus.pipeline import auditor
    from argus.pipeline import env_check, env_check_model
    from argus.domain import workflow_rules

    owns_tracker = usage_tracker is None
    tracker = usage_tracker or cost_tracking.UsageTracker()
    rates = model_rates or cost_tracking.default_model_rates()
    if not metadata:
        metadata = {}
        metadata_path = execution_dir / "metadata.json"
        if metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text())
            except (OSError, json.JSONDecodeError):
                metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    else:
        metadata = dict(metadata)
    raw_test_results = metadata.get("test_results")
    test_results = (
        [item for item in raw_test_results if isinstance(item, dict)]
        if isinstance(raw_test_results, list)
        else []
    )
    metadata["test_results"] = test_results
    test_result = test_results[0] if test_results else {}
    ec: dict[str, Any] = {}
    if environment_enabled:
        is_webview = auditor._is_webview_path(execution_dir)
        is_prod_validation = auditor._is_prod_validation_path(execution_dir)
        expected_browser = test_result.get("expected_browser")
        expected_mode = execution_policy.expected_execution_mode(
            metadata,
            execution_dir,
        )
        missing_url_is_advisory = (
            is_webview
            or expected_mode in {
                execution_policy.MOBILE_EMULATION_ALLOWED,
                execution_policy.PHYSICAL_DEVICE,
            }
        )
        from argus.pipeline import model_client
        ec = env_check_model.check_env_compliance(
            execution_dir,
            sample_stride=effective_stride(sample_stride, is_webview),
            model_id=auxiliary_model_id or model_client.auxiliary_model_id(),
            region_crop_height=region_crop_height,
            region_crop_bottom_height=region_crop_bottom_height,
            model_api_url=model_api_url,
            api_key_env=api_key_env,
            is_webview=is_webview,
            is_prod_validation=is_prod_validation,
            missing_url_is_advisory=missing_url_is_advisory,
            expected_browser=expected_browser,
            expected_execution_mode=expected_mode,
            expected_marketplace=(
                test_result.get("marketplace") if marketplace_enabled else None
            ),
            usage_tracker=tracker,
        )
        ec["findings"] = list(ec.get("findings") or [])
        if env_check.has_contextual_environment_policy():
            env_check.apply_contextual_environment_policy(metadata, ec)
        else:
            ec["findings"].extend(
                env_check.check_required_environment_comment(metadata, ec)
            )

    findings = list(audit.get("findings") or [])
    had_stale = any(is_stale_env_finding(f) for f in findings)
    if had_stale:
        audit["findings"] = [
            f for f in findings if not is_stale_env_finding(f)
        ]
        audit["summary"] = auditor._RULE_NOTE_RX.sub(
            "", audit.get("summary") or "").strip()
        base = audit.pop("_verdict_escalated_by_rules_from", None)
        if base is not None:
            audit["overall_verdict"] = base
            audit = auditor._finalize_verdict(audit)
        surviving_rules = [
            f for f in audit["findings"]
            if f.get("source") == "rule"
            and not auditor.is_advisory_finding(f)
        ]
        if surviving_rules:
            audit["summary"] = (
                f"{audit['summary']}\n\n"
                f"{auditor._rule_note(len(surviving_rules))}"
            ).strip()

    if environment_enabled:
        audit["env_check"] = build_env_check_field(ec)
    else:
        audit.pop("env_check", None)

    added = False
    if environment_enabled and ec["findings"]:
        audit = auditor.merge_rule_findings(audit, ec["findings"])
        added = True

    if environment_enabled and marketplace_enabled:
        r13 = workflow_rules.check_marketplace_match(metadata, audit)
        if r13:
            audit = auditor.merge_rule_findings(audit, r13)
            added = True

    if device_enabled:
        try:
            from argus.pipeline import device_check, model_client
            device_result = device_check.evaluate_device_compliance(
                metadata, execution_dir, model_api_url=model_api_url, api_key_env=api_key_env,
                model_id=auxiliary_model_id or model_client.auxiliary_model_id(),
                usage_tracker=tracker)
            audit["device_check"] = {
                key: value
                for key, value in device_result.items()
                if key != "findings"
            }
            r19 = device_result["findings"]
            if r19:
                audit = auditor.merge_rule_findings(audit, r19)
                added = True
        except Exception as e:
            from argus.pipeline import analysis_status
            raise analysis_status.AnalysisIncompleteError(
                "environment",
                f"device-family analysis failed: {type(e).__name__}: {e}",
            ) from e
    else:
        audit.pop("device_check", None)

    if audit.get("schema_version", 0) < auditor.AUDIT_SCHEMA_VERSION:
        audit["schema_version"] = auditor.AUDIT_SCHEMA_VERSION

    audit = auditor._drop_false_positive_model_findings(audit, metadata)
    audit = auditor.enrich_audit(audit, metadata)
    audit = auditor._finalize_verdict(audit)
    from argus.operations import rule_runtime
    if rule_runtime.digest():
        for module in ("env_check", "device_check"):
            if isinstance(audit.get(module), dict):
                audit[module]["policy_sha256"] = rule_runtime.digest()
    if owns_tracker:
        added_cost = tracker.summary(rates)
        merged_cost = cost_tracking.merge_summaries(
            audit.get("cost"), added_cost, rates)
        if merged_cost is not None:
            audit["cost"] = merged_cost
    return MergeOutcome(
        audit=audit, ec=ec,
        added_finding=added, had_stale_finding=had_stale,
    )
