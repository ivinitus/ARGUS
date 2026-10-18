"""Parallel batch runner."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, TextIO

from argus.domain import finding_inference, workflow_rules
from argus.operations import control as argus_control
from argus.operations import audit_lock
from argus.core import atomic_io
from argus.core import costs as cost_tracking
from argus.pipeline import analysis_status, auditor, model_client
from argus import config as argus_config
from argus.pipeline import extractor
from argus import runtime

log = logging.getLogger("argus.batch")


def _policy_rule_enabled(
    settings: argus_config.ArgusConfig,
    rule: str,
) -> bool:
    """Evaluate one rule directly from the selected profile."""
    normalized = rule.upper()
    disabled = {
        item.upper() for item in settings.policy.disabled_rules
    }
    if normalized in disabled:
        return False
    enabled = {
        item.upper() for item in settings.policy.enabled_rules
    }
    if not enabled:
        return True
    if normalized in enabled:
        return True
    base = workflow_rules.rule_base(normalized)
    return bool(base and base != normalized and base in enabled)


@dataclass
class KeyResult:
    key: str
    status: str
    verdict: str | None = None
    findings: int | None = None
    reason: str | None = None
    tester: str | None = None
    elapsed_s: float = 0.0
    audit_path: Path | None = None


@dataclass(frozen=True)
class ExtractionResult:
    key: str
    status: str
    pages: int
    elapsed_s: float
    reason: str | None = None


def read_keys(stdin_: Iterable[str] | None, keys_file: Path | None) -> list[str]:
    """Read keys from stdin or a file. Blank lines and `#` comments skipped."""
    if keys_file is not None:
        lines = keys_file.read_text().splitlines()
    elif stdin_ is not None:
        lines = list(stdin_)
    else:
        return []
    keys: list[str] = []
    for raw in lines:
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        keys.append(s)
    seen: set[str] = set()
    unique: list[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            unique.append(k)
    return unique


def _has_any_evidence(work_dir: Path) -> bool:
    """True when screenshots, XML-derived images, or video evidence exists."""
    video_manifest = work_dir / "video_evidence.json"
    try:
        videos = json.loads(video_manifest.read_text()).get("videos") or []
    except (OSError, json.JSONDecodeError, AttributeError):
        videos = []
    if any(
        (work_dir / str(video.get("path") or "")).is_file()
        for video in videos
        if isinstance(video, dict)
    ):
        return True
    shots = work_dir / "screenshots"
    if not shots.exists():
        return False
    for _ in shots.rglob("page_*.jpg"):
        return True
    for _ in shots.rglob("page_*.png"):
        return True
    step_dir = shots / "step_attachments"
    if step_dir.is_dir():
        for p in step_dir.iterdir():
            if (p.is_file()
                    and p.suffix.lower() in {".jpg", ".jpeg", ".png",
                                             ".gif", ".webp"}):
                return True
    return False


def _count_extracted_pages(work_dir: Path) -> int:
    shots = work_dir / "screenshots"
    if not shots.exists():
        return 0
    return (
        sum(1 for _ in shots.rglob("page_*.jpg"))
        + sum(1 for _ in shots.rglob("page_*.png"))
    )


def _read_tester(work_dir: Path) -> str | None:
    """Tester display name from metadata.json; falls back to raw userKey, None if metadata is missing/unreadable."""
    meta_path = work_dir / "metadata.json"
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    results = meta.get("test_results") or []
    if not results:
        return None
    tr = results[0]
    return tr.get("executed_by_name") or tr.get("executed_by")


def _read_unsupported_attachments(work_dir: Path) -> list[dict[str, Any]]:
    """Return attached files that extractor metadata says ARGUS cannot audit."""
    meta_path = work_dir / "metadata.json"
    try:
        metadata = json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return extractor.collect_unsupported_attachments(
        metadata.get("test_results") or []
    )


def _unsupported_format_labels(
    attachments: list[dict[str, Any]],
) -> list[str]:
    labels = {
        (
            Path(str(item.get("name") or "")).suffix.lstrip(".").upper()
            or "UNKNOWN"
        )
        for item in attachments
    }
    return sorted(labels)


def _write_no_evidence_audit(
    work_dir: Path,
    key: str,
    tester: str | None,
) -> None:
    """Synthetic fail-verdict audit.json for zero-evidence submissions."""
    audit_path = work_dir / "audit.json"
    md_path = work_dir / "audit.md"
    name_for_msg = f"Tester '{tester}' " if tester else "The tester "
    finding = {
        "severity": "high",
        "page": None,
        "step_index": None,
        "description": (
            f"{name_for_msg}submitted this execution with NO evidence "
            "attached — no session PDF, screenshots, direct images, or "
            "usable video. Per compliance policy a test execution must "
            "include visual evidence of each required result. Without "
            "that evidence the audit cannot verify the test was performed. "
            "The tester must re-submit with a session PDF, images, or a "
            "supported screen recording, or the execution must be re-run."
        ),
        "source": "rule",
        "rule": "R12",
    }
    audit: dict[str, Any] = {
        "overall_verdict": "fail",
        "summary": (
            "No auditable visual evidence was submitted. The tester did "
            "not provide a session PDF, images, or usable video, so the "
            "audit pipeline cannot verify the execution."
        ),
        "findings": [finding],
        "schema_version": auditor.AUDIT_SCHEMA_VERSION,
        "variant_name": "no-evidence",
        "cost": cost_tracking.zero_cost_summary(),
    }
    metadata: dict = {}
    meta_path = work_dir / "metadata.json"
    if meta_path.exists():
        try:
            metadata = json.loads(meta_path.read_text())
        except (OSError, json.JSONDecodeError):
            metadata = {}

    try:
        from argus.domain import workflow_rules as _wr
        rule_findings = _wr.run_workflow_rules(metadata) if metadata else []
        if rule_findings:
            audit["findings"].extend(rule_findings)
    except Exception:  # pragma: no cover
        log.exception("rule engine failed for no-evidence audit %s — "
                      "R6/R11/R16 findings omitted", work_dir.name)

    audit = auditor.enrich_audit(audit, metadata)
    analysis_status.set_module(audit, "extraction", "complete")
    analysis_status.set_module(
        audit,
        "screenshots",
        "incomplete",
        "no PDF page images, tester-attached screenshots, or usable video "
        "were available",
    )
    analysis_status.set_module(audit, "environment", "not_applicable")
    analysis_status.set_module(audit, "xml", "not_applicable")
    analysis_status.set_module(audit, "model", "not_applicable")
    analysis_status.set_module(audit, "guardrail", "not_applicable")
    analysis_status.set_module(audit, "publication", "not_applicable")
    from argus.operations import rule_runtime
    audit = rule_runtime.finalize_synthetic(audit, metadata, work_dir)
    atomic_io.atomic_write_json(audit_path, audit)
    try:
        md_path.write_text(auditor.render_markdown(audit, metadata))
    except Exception:  # pragma: no cover
        log.warning("audit.md render failed for %s (audit.json is canonical)",
                    audit_path.parent.name, exc_info=True)


def _write_unsupported_evidence_audit(
    work_dir: Path,
    key: str,
    tester: str | None,
    attachments: list[dict[str, Any]],
) -> None:
    """Synthetic incomplete audit when files exist but none are auditable."""
    audit_path = work_dir / "audit.json"
    formats = _unsupported_format_labels(attachments)
    format_text = ", ".join(formats)
    count = len(attachments)
    noun = "attachment" if count == 1 else "attachments"
    finding = {
        "title": "Attached evidence format is unsupported",
        "severity": "medium",
        "page": None,
        "step_index": None,
        "description": (
            f"The tester attached {count} {noun}, but the attached "
            f"format ({format_text}) cannot currently be audited by ARGUS. "
            "This is not a zero-evidence submission. Attach a session PDF "
            "or PNG/JPEG screenshots so the execution can be verified."
        ),
        "source": "extractor",
        "category": "insufficient_evidence",
        "action": "add_evidence",
        "confidence": "high",
    }
    audit: dict[str, Any] = {
        "overall_verdict": "concerns",
        "summary": (
            f"Evidence was attached, but its format ({format_text}) is not "
            "currently auditable. Provide PDF or image evidence."
        ),
        "findings": [finding],
        "schema_version": auditor.AUDIT_SCHEMA_VERSION,
        "variant_name": "unsupported-evidence",
        "cost": cost_tracking.zero_cost_summary(),
    }
    metadata: dict[str, Any] = {}
    meta_path = work_dir / "metadata.json"
    if meta_path.exists():
        try:
            metadata = json.loads(meta_path.read_text())
        except (OSError, json.JSONDecodeError):
            metadata = {}

    try:
        from argus.domain import workflow_rules as _wr

        rule_findings = _wr.run_workflow_rules(metadata) if metadata else []
        if rule_findings:
            audit["findings"].extend(rule_findings)
    except Exception:  # pragma: no cover
        log.exception(
            "rule engine failed for unsupported-evidence audit %s",
            work_dir.name,
        )

    audit = auditor.enrich_audit(audit, metadata)
    audit = finding_inference.reconcile_verdict(audit)
    if tester and not audit.get("tester"):
        audit["tester"] = tester
    analysis_status.set_module(audit, "extraction", "complete")
    analysis_status.set_module(
        audit,
        "screenshots",
        "incomplete",
        f"attached file format is unsupported ({format_text})",
    )
    analysis_status.set_module(audit, "environment", "not_applicable")
    analysis_status.set_module(audit, "xml", "not_applicable")
    analysis_status.set_module(audit, "model", "not_applicable")
    analysis_status.set_module(audit, "guardrail", "not_applicable")
    analysis_status.set_module(audit, "publication", "not_applicable")
    from argus.operations import rule_runtime
    audit = rule_runtime.finalize_synthetic(audit, metadata, work_dir)
    atomic_io.atomic_write_json(audit_path, audit)
    try:
        atomic_io.atomic_write_text(
            work_dir / "audit.md",
            auditor.render_markdown(audit, metadata),
        )
    except Exception:  # pragma: no cover
        log.warning(
            "audit.md render failed for unsupported-evidence audit %s",
            key,
            exc_info=True,
        )


def _write_incomplete_audit(
    work_dir: Path,
    key: str,
    module: str,
    diagnostic: str,
    *,
    tester: str | None = None,
) -> Path:
    """Persist a non-compliance-bearing diagnostic for an incomplete run."""
    metadata: dict[str, Any] = {}
    metadata_path = work_dir / "metadata.json"
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text())
        except (OSError, json.JSONDecodeError):
            metadata = {}
    audit: dict[str, Any] = {
        "overall_verdict": "concerns",
        "summary": (
            "Audit analysis did not complete. This execution is excluded "
            "from compliance totals and must be retried."
        ),
        "findings": [],
        "schema_version": auditor.AUDIT_SCHEMA_VERSION,
        "variant_name": "analysis-incomplete",
        "cost": cost_tracking.zero_cost_summary(),
    }
    has_screenshots = _has_any_evidence(work_dir)
    xml_path = work_dir / "xml_evidence.json"
    for name in analysis_status.MODULES:
        status = "not_applicable"
        detail: str | None = None
        if name == "extraction":
            status = "incomplete" if module == name else "complete"
        elif name == "screenshots":
            status = "complete" if has_screenshots else "incomplete"
            if not has_screenshots:
                detail = "no screenshot evidence was available"
        elif name == "xml":
            status = "complete" if xml_path.exists() else "not_applicable"
        if name == module:
            status = "incomplete"
            detail = diagnostic
        analysis_status.set_module(audit, name, status, detail)
    audit = auditor.enrich_audit(audit, metadata)
    audit["analysis_status"] = analysis_status.derive_status(audit)
    if tester and not metadata:
        audit["tester"] = tester
    audit_path = work_dir / "audit.json"
    from argus.operations import rule_runtime
    audit = rule_runtime.finalize_synthetic(audit, metadata, work_dir)
    atomic_io.atomic_write_json(audit_path, audit)
    try:
        atomic_io.atomic_write_text(
            work_dir / "audit.md",
            auditor.render_markdown(audit, metadata),
        )
    except Exception:  # pragma: no cover
        log.warning(
            "audit.md render failed for incomplete audit %s",
            key,
            exc_info=True,
        )
    return audit_path


def _mark_result_incomplete(
    result: "KeyResult",
    module: str,
    diagnostic: str,
) -> None:
    """Update a result and its persisted audit after a deferred failure."""
    result.status = "analysis_incomplete"
    result.reason = f"{module}: {diagnostic}"
    audit_path = result.audit_path
    if audit_path is None or not audit_path.exists():
        return
    try:
        audit = json.loads(audit_path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    analysis_status.set_module(audit, module, "incomplete", diagnostic)
    if module == "guardrail":
        analysis_status.set_module(
            audit,
            "model",
            "incomplete",
            "model analysis stopped after guardrail intervention",
        )
    metadata: dict[str, Any] = {}
    metadata_path = audit_path.parent / "metadata.json"
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text())
        except (OSError, json.JSONDecodeError):
            metadata = {}
    from argus.pipeline import env_check_merge
    env_check_merge.write_merged_audit(audit_path, audit, metadata)


def _write_not_applicable_audit(
    work_dir: Path,
    key: str,
) -> None:
    """Synthetic audit.json for N/A executions."""
    audit_path = work_dir / "audit.json"
    md_path = work_dir / "audit.md"
    metadata: dict = {}
    meta_path = work_dir / "metadata.json"
    if meta_path.exists():
        try:
            metadata = json.loads(meta_path.read_text())
        except (OSError, json.JSONDecodeError):
            metadata = {}

    findings: list[dict] = []
    try:
        from argus.domain import workflow_rules as _wr
        r18 = _wr.build_r18_finding(metadata)
        if r18:
            findings.append(r18)
        rule_findings = _wr.run_workflow_rules(metadata) if metadata else []
        findings.extend(rule_findings)
        try:
            from argus.domain import history_index as _hi
            from argus.pipeline.auditor import _infer_history_root
            hidx = _hi.HistoryIndex.get_shared(_infer_history_root(work_dir))
            r17 = _wr.check_immediate_prior_executed(metadata, hidx)
            if r17:
                findings.extend(r17)
        except Exception:
            log.warning("R17 history check failed for %s — skipped", key,
                        exc_info=True)
    except Exception:  # pragma: no cover
        log.exception("rule engine failed for N/A audit %s — verdict computed "
                      "with zero rule findings", key)

    if any(f.get("severity") == "high" for f in findings):
        verdict = "fail"
    elif findings:
        verdict = "concerns"
    else:
        verdict = "pass"

    audit = {
        "overall_verdict": verdict,
        "summary": (
            "Test case marked Not Applicable. "
            + ("Tester provided no documentation explaining the "
               "determination — review whether the N/A is legitimate "
               "or a side-step."
               if any(f.get("rule") == "R18"
                      and f.get("severity") == "high" for f in findings)
               else "See findings for any related compliance signals.")
        ),
        "findings": findings,
        "schema_version": auditor.AUDIT_SCHEMA_VERSION,
        "variant_name": "not-applicable",
        "cost": cost_tracking.zero_cost_summary(),
    }
    audit = auditor.enrich_audit(audit, metadata)
    for module in analysis_status.MODULES:
        analysis_status.set_module(audit, module, "not_applicable")
    from argus.operations import rule_runtime
    audit = rule_runtime.finalize_synthetic(audit, metadata, work_dir)
    atomic_io.atomic_write_json(audit_path, audit)
    try:
        md_path.write_text(auditor.render_markdown(audit, metadata))
    except Exception:  # pragma: no cover
        log.warning("audit.md render failed for %s (audit.json is canonical)",
                    audit_path.parent.name, exc_info=True)


def _write_blocked_audit(
    work_dir: Path,
    key: str,
) -> None:
    """Synthetic audit.json for Blocked / In Progress executions."""
    audit_path = work_dir / "audit.json"
    md_path = work_dir / "audit.md"
    metadata: dict = {}
    meta_path = work_dir / "metadata.json"
    if meta_path.exists():
        try:
            metadata = json.loads(meta_path.read_text())
        except (OSError, json.JSONDecodeError):
            metadata = {}

    status = ""
    tr_list = metadata.get("test_results") or []
    if tr_list:
        status = (tr_list[0].get("status") or "").strip()
    variant = "in-progress" if status == "In Progress" else "blocked"

    findings: list[dict] = []
    try:
        from argus.domain import workflow_rules as _wr
        hidx = None
        try:
            from argus.domain import history_index as _hi
            from argus.pipeline.auditor import _infer_history_root
            hidx = _hi.HistoryIndex.get_shared(_infer_history_root(work_dir))
        except Exception:
            log.warning("history index unavailable for %s — R20 escalation "
                        "and R17 skipped", key, exc_info=True)
        base = (_wr.build_r20_finding(metadata, hidx,
                                      current_e_key=work_dir.name)
                or _wr.build_r21_finding(metadata))
        if base:
            findings.append(base)
        rule_findings = _wr.run_workflow_rules(metadata) if metadata else []
        findings.extend(rule_findings)
        if hidx is not None:
            try:
                findings.extend(
                    _wr.check_immediate_prior_executed(metadata, hidx))
            except Exception:
                log.warning("R17 history check failed for %s — skipped",
                            key, exc_info=True)
    except Exception:  # pragma: no cover
        log.exception("rule engine failed for %s audit %s — verdict "
                      "computed with zero rule findings", variant, key)

    if variant == "blocked":
        verdict = "concerns" if findings else "pass"
    elif any(f.get("severity") == "high" for f in findings):
        verdict = "fail"
    elif findings:
        verdict = "concerns"
    else:
        verdict = "pass"

    label = "In Progress" if variant == "in-progress" else "Blocked"
    audit = {
        "overall_verdict": verdict,
        "summary": (
            f"Test execution left {label}. "
            + ("Tester provided no documentation explaining the state — "
               "review whether it is legitimate or a side-step."
               if any(f.get("rule") in ("R20", "R21")
                      and f.get("severity") == "high" for f in findings)
               else "See findings for any related compliance signals.")
        ),
        "findings": findings,
        "schema_version": auditor.AUDIT_SCHEMA_VERSION,
        "variant_name": variant,
        "cost": cost_tracking.zero_cost_summary(),
    }
    audit = auditor.enrich_audit(audit, metadata)
    has_screenshots = _has_any_evidence(work_dir)
    analysis_status.set_module(audit, "extraction", "complete")
    analysis_status.set_module(
        audit,
        "screenshots",
        "complete" if has_screenshots else "not_applicable",
    )
    analysis_status.set_module(audit, "environment", "not_applicable")
    analysis_status.set_module(audit, "xml", "not_applicable")
    analysis_status.set_module(audit, "model", "not_applicable")
    analysis_status.set_module(audit, "guardrail", "not_applicable")
    analysis_status.set_module(audit, "publication", "not_applicable")
    from argus.operations import rule_runtime
    audit = rule_runtime.finalize_synthetic(audit, metadata, work_dir)
    atomic_io.atomic_write_json(audit_path, audit)
    try:
        md_path.write_text(auditor.render_markdown(audit, metadata))
    except Exception:  # pragma: no cover
        log.warning("audit.md render failed for %s (audit.json is canonical)",
                    audit_path.parent.name, exc_info=True)


def _run_env_check_for_key(
    result: "KeyResult",
    settings: argus_config.ArgusConfig,
    *,
    lock_held: bool = False,
) -> None:
    """Run configured post-checks and merge the result into audit.json."""
    audit_path = result.audit_path
    if audit_path is None or not audit_path.exists():
        return
    if lock_held:
        _run_env_check_for_key_locked(settings, audit_path)
        return
    try:
        lock = audit_lock.acquire_key(
            settings.extractor.out_dir,
            result.key,
            owner="batch.post_checks",
        )
    except audit_lock.AuditLockBusy as e:
        log.info("env_check skipped for %s: %s", result.key, e)
        return

    with lock:
        _run_env_check_for_key_locked(settings, audit_path)


def _run_env_check_for_key_locked(
    settings: argus_config.ArgusConfig,
    audit_path: Path,
) -> None:
    environment_enabled = (
        settings.policy.environment_check_enabled
        and settings.auditor.env_check_engine != "off"
    )
    marketplace_enabled = (
        environment_enabled and _policy_rule_enabled(settings, "R13")
    )
    device_enabled = _policy_rule_enabled(settings, "R19")
    if not environment_enabled and not device_enabled:
        return

    audit = json.loads(audit_path.read_text())
    from argus.operations import rule_runtime
    policy_hash = rule_runtime.digest()
    execution_dir = audit_path.parent

    from argus.pipeline import env_check_model as engine_module
    from argus.pipeline import env_check_merge

    existing = audit.get("env_check") or {}
    device_current = not device_enabled
    if device_enabled:
        from argus.pipeline import device_check
        device_current = (
            (audit.get("device_check") or {}).get("version")
            == device_check.DEVICE_CHECK_VERSION
        )
    environment_current = (
        not environment_enabled
        or existing.get("version") == engine_module.ENV_CHECK_VERSION
    )
    if environment_current and device_current:
        if policy_hash and (
            (environment_enabled and existing.get("policy_sha256") != policy_hash)
            or (device_enabled and (audit.get("device_check") or {}).get("policy_sha256") != policy_hash)
        ):
            environment_current = False
    if environment_current and device_current:
        analysis_status.set_module(
            audit,
            "environment",
            "complete" if environment_enabled else "not_applicable",
        )
        env_check_merge.write_merged_audit(audit_path, audit)
        return

    outcome = env_check_merge.run_and_merge(
        audit,
        execution_dir,
        sample_stride=settings.auditor.env_check_sample_stride,
        region_crop_height=settings.auditor.env_check_region_crop_height,
        region_crop_bottom_height=(
            settings.auditor.env_check_region_crop_bottom_height),
        model_api_url=settings.auditor.model_api_url,
        api_key_env=settings.auditor.api_key_env,
        model_rates=settings.costs.model_rates,
        environment_enabled=environment_enabled,
        marketplace_enabled=marketplace_enabled,
        device_enabled=device_enabled,
    )
    analysis_status.set_module(
        outcome.audit,
        "environment",
        "complete" if environment_enabled else "not_applicable",
    )
    env_check_merge.write_merged_audit(audit_path, outcome.audit)


def _timed_env_check_for_key(
    result: "KeyResult",
    settings: argus_config.ArgusConfig,
) -> float:
    started = time.monotonic()
    _run_env_check_for_key(result, settings)
    return time.monotonic() - started


def _env_check_worker_count(concurrency: int) -> int:
    """Bound nested env-check fanout independently of audit concurrency."""
    return max(1, min(8, concurrency))


def _post_check_worker_count(
    settings: argus_config.ArgusConfig,
    concurrency: int,
) -> int:
    """Bound the post-check pool, whose active implementations use Haiku."""
    del settings
    return _env_check_worker_count(concurrency)


def _post_checks_enabled(settings: argus_config.ArgusConfig) -> bool:
    return bool(
        (
            settings.policy.environment_check_enabled
            and settings.auditor.env_check_engine != "off"
        )
        or _policy_rule_enabled(settings, "R19")
    )


def _process_key(
    key: str,
    settings: argus_config.ArgusConfig,
    extractor_sem: threading.Semaphore,
    out_dir: Path | None = None,
    on_extraction_complete: Callable[[ExtractionResult], None] | None = None,
    session_provider: Callable[[], Any] | None = None,
) -> KeyResult:
    """Extract + audit one key end-to-end.

    `session_provider` returns a shared, pre-authenticated Jira session (or
    None to let the extractor build its own per key).
    """
    t0 = time.monotonic()
    if argus_control.is_paused():
        return KeyResult(
            key=key, status="skipped",
            reason="argus paused via /argus pause",
            elapsed_s=time.monotonic() - t0,
        )

    try:
        lock = audit_lock.acquire_key(
            settings.extractor.out_dir,
            key,
            owner="batch",
        )
    except audit_lock.AuditLockBusy as e:
        return KeyResult(
            key=key,
            status="skipped",
            reason=str(e),
            elapsed_s=time.monotonic() - t0,
        )

    ex_cfg = extractor.ExtractorConfig.from_settings(
        settings.extractor,
        execution_key=key,
        out_dir=out_dir if out_dir is not None else settings.extractor.out_dir,
        session=session_provider() if session_provider is not None else None,
    )

    with lock:
        with extractor_sem:
            extract_started = time.monotonic()
            try:
                rc, work_dir = extractor.run(ex_cfg)
            except Exception as exc:
                if on_extraction_complete is not None:
                    on_extraction_complete(ExtractionResult(
                        key=key,
                        status="failed",
                        pages=0,
                        elapsed_s=time.monotonic() - extract_started,
                        reason=f"{type(exc).__name__}: {exc}",
                    ))
                raise
            extract_elapsed = time.monotonic() - extract_started
        if rc != 0 or work_dir is None:
            if on_extraction_complete is not None:
                on_extraction_complete(ExtractionResult(
                    key=key,
                    status="failed",
                    pages=0,
                    elapsed_s=extract_elapsed,
                    reason=f"extractor rc={rc}",
                ))
            reason = f"extractor rc={rc}"
            audit_path = None
            if work_dir is not None:
                audit_path = _write_incomplete_audit(
                    work_dir,
                    key,
                    "extraction",
                    reason,
                    tester=_read_tester(work_dir),
                )
            return KeyResult(
                key=key,
                status="auth_failed" if rc == 2 else "analysis_incomplete",
                reason=reason,
                tester=_read_tester(work_dir) if work_dir is not None else None,
                audit_path=audit_path,
                elapsed_s=time.monotonic() - t0,
            )
        if on_extraction_complete is not None:
            on_extraction_complete(ExtractionResult(
                key=key,
                status="ok",
                pages=_count_extracted_pages(work_dir),
                elapsed_s=extract_elapsed,
            ))

        tester = _read_tester(work_dir)

        from argus.domain import desktop_policy
        external_evidence = []
        has_evidence = _has_any_evidence(work_dir)
        if not has_evidence and desktop_policy.is_desktop(settings.auditor.prompt_variant):
            from argus.pipeline import linked_evidence
            external_evidence = desktop_policy.external_evidence(
                linked_evidence.with_pdf_links(
                    json.loads((work_dir / "metadata.json").read_text()), work_dir,
                )
            )
        if not has_evidence and not external_evidence:
            unsupported = _read_unsupported_attachments(work_dir)
            if unsupported:
                _write_unsupported_evidence_audit(
                    work_dir,
                    key,
                    tester,
                    unsupported,
                )
                reason = (
                    "unsupported attachment format(s): "
                    + ", ".join(_unsupported_format_labels(unsupported))
                )
            else:
                _write_no_evidence_audit(work_dir, key, tester)
                reason = "no PDF, image, or usable video evidence"
            return KeyResult(key=key, status="analysis_incomplete",
                             reason=reason,
                             tester=tester,
                             audit_path=work_dir / "audit.json",
                             elapsed_s=time.monotonic() - t0)

        au_cfg = auditor.AuditConfig.from_settings(
            settings.auditor, execution_dir=work_dir,
            cost_rates=settings.costs.model_rates,
        )
        try:
            rc = auditor.run(au_cfg)
        except model_client.ModelInterventionError as exc:
            audit_path = _write_incomplete_audit(
                work_dir, key, "guardrail", str(exc), tester=tester)
            return KeyResult(
                key=key,
                status="analysis_incomplete",
                reason=f"guardrail: {exc}",
                tester=tester,
                audit_path=audit_path,
                elapsed_s=time.monotonic() - t0,
            )
        except model_client.ModelConfigurationError as exc:
            audit_path = _write_incomplete_audit(
                work_dir, key, "guardrail", str(exc), tester=tester)
            return KeyResult(
                key=key,
                status="analysis_incomplete",
                reason=f"guardrail: {exc}",
                tester=tester,
                audit_path=audit_path,
                elapsed_s=time.monotonic() - t0,
            )
        except Exception as exc:
            audit_path = _write_incomplete_audit(
                work_dir,
                key,
                "model",
                f"{type(exc).__name__}: {exc}",
                tester=tester,
            )
            return KeyResult(
                key=key,
                status="analysis_incomplete",
                reason=f"model: {type(exc).__name__}: {exc}",
                tester=tester,
                audit_path=audit_path,
                elapsed_s=time.monotonic() - t0,
            )
        if rc != 0:
            reason = f"auditor rc={rc}"
            audit_path = _write_incomplete_audit(
                work_dir, key, "model", reason, tester=tester)
            return KeyResult(
                key=key,
                status="analysis_incomplete",
                reason=reason,
                tester=tester,
                audit_path=audit_path,
                elapsed_s=time.monotonic() - t0,
            )

        verdict: str | None = None
        findings: int | None = None
        audit_path = work_dir / "audit.json"
        try:
            audit = json.loads(audit_path.read_text())
            verdict = audit.get("overall_verdict")
            findings = len(audit.get("findings") or [])
            if analysis_status.derive_status(audit) == "incomplete":
                return KeyResult(
                    key=key,
                    status="analysis_incomplete",
                    verdict=verdict,
                    findings=findings,
                    reason=analysis_status.diagnostic(audit),
                    tester=tester,
                    elapsed_s=time.monotonic() - t0,
                    audit_path=audit_path,
                )
        except (OSError, json.JSONDecodeError):
            pass

        return KeyResult(key=key, status="ok", verdict=verdict, findings=findings,
                         tester=tester, elapsed_s=time.monotonic() - t0,
                         audit_path=audit_path)


def run_batch(
    keys: list[str],
    settings: argus_config.ArgusConfig,
    concurrency: int,
    extractor_concurrency: int,
    out: TextIO = sys.stdout,
    on_audit_complete: Callable[[KeyResult], None] | None = None,
    on_progress: Callable[[KeyResult, int, int], None] | None = None,
    on_extraction_complete: (
        Callable[[ExtractionResult, int, int], None] | None
    ) = None,
    on_key_finalized: Callable[[KeyResult, int, int], None] | None = None,
    env_check_auto: bool = True,
    out_dir: Path | None = None,
) -> list[KeyResult]:
    """Run `keys` in parallel; one progress line per completion.

    Post-checks run in a bounded pool as audits complete, and each key is
    finalized (`on_key_finalized`, which drives durable checkpointing) as
    soon as its post-check finishes — on the post-check worker, during the
    audit phase — instead of in a serial drain after the last audit.
    Finalization is serialised by one lock because the callback chain
    (progress counters, publisher checkpoint state) is not thread-safe.
    """
    requested_extractor = extractor_concurrency
    extractor_concurrency = max(1, min(extractor_concurrency, concurrency))
    if extractor_concurrency != requested_extractor:
        print(
            f"batch: extractor_concurrency={requested_extractor} clamped to "
            f"{extractor_concurrency} (must be in [1, concurrency])",
            file=out,
        )
        getattr(out, "flush", lambda: None)()
    extractor_sem = threading.Semaphore(extractor_concurrency)
    results: list[KeyResult] = []
    total = len(keys)
    extraction_done = 0
    extraction_lock = threading.Lock()
    finalized_done = 0
    # Guards finalized_done, the finalized set, the incomplete-marking of a
    # result and the on_key_finalized chain (publisher checkpoint sets), all
    # of which are now reached from post-check workers and the main thread.
    finalize_lock = threading.Lock()
    finalized_ids: set[int] = set()
    out_lock = threading.Lock()

    def emit(line: str) -> None:
        with out_lock:
            print(line, file=out)
            getattr(out, "flush", lambda: None)()

    session_lock = threading.Lock()
    session_cache: list[Any] = []

    def batch_session() -> Any:
        """One pooled Jira session for the whole batch (None on failure)."""
        with session_lock:
            if not session_cache:
                try:
                    session_cache.append(
                        _build_batch_session(settings, extractor_concurrency))
                except Exception as exc:
                    log.warning(
                        "shared Jira session unavailable (%s: %s); "
                        "building one per key",
                        type(exc).__name__, exc,
                    )
                    session_cache.append(None)
            return session_cache[0]

    def extraction_callback(result: ExtractionResult) -> None:
        nonlocal extraction_done
        if on_extraction_complete is None:
            return
        with extraction_lock:
            extraction_done += 1
            done = extraction_done
        try:
            on_extraction_complete(result, done, total)
        except Exception as cb_err:
            emit(
                f"[batch] on_extraction_complete raised "
                f"{type(cb_err).__name__}: {cb_err} for {result.key}"
            )

    def finalize(
        result: KeyResult,
        *,
        incomplete: tuple[str, str] | None = None,
    ) -> None:
        """Finalize once per result; `incomplete` marks (module, diagnostic)."""
        nonlocal finalized_done
        with finalize_lock:
            if id(result) in finalized_ids:
                return
            finalized_ids.add(id(result))
            if incomplete is not None:
                _mark_result_incomplete(result, *incomplete)
            if on_key_finalized is None:
                return
            finalized_done += 1
            done = finalized_done
            try:
                on_key_finalized(result, done, total)
            except Exception as cb_err:
                emit(
                    f"[batch] on_key_finalized raised "
                    f"{type(cb_err).__name__}: {cb_err} for {result.key}"
                )

    def post_check(result: KeyResult) -> bool:
        """Post-check one ok key on the env pool, then finalize it."""
        try:
            result.elapsed_s += _timed_env_check_for_key(result, settings)
        except Exception as e:
            diagnostic = (
                e.diagnostic
                if isinstance(e, analysis_status.AnalysisIncompleteError)
                else f"{type(e).__name__}: {e}"
            )
            emit(f"[batch] env_check raised {type(e).__name__}: {e}")
            finalize(result, incomplete=("environment", diagnostic))
            return False
        finalize(result)
        return True

    env_pool: ThreadPoolExecutor | None = None
    env_futs: list[tuple[Future[bool], KeyResult]] = []
    if env_check_auto and _post_checks_enabled(settings):
        env_workers = _post_check_worker_count(settings, concurrency)
        log.info(
            "env_check worker pool: %d (audit concurrency=%d)",
            env_workers,
            concurrency,
        )
        env_pool = ThreadPoolExecutor(
            max_workers=env_workers, thread_name_prefix="post_checks")

    ex = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="argus")
    interrupted = False
    try:
        futures = {
            ex.submit(
                _process_key,
                k,
                settings,
                extractor_sem,
                out_dir,
                extraction_callback,
                batch_session,
            ): k
            for k in keys
        }
        try:
            for fut in as_completed(futures):
                key = futures[fut]
                try:
                    res = fut.result()
                except extractor.AuthError as e:
                    res = KeyResult(key=key, status="auth_failed",
                                    reason=str(e))
                except Exception as e:
                    res = KeyResult(key=key, status="failed",
                                    reason=f"{e.__class__.__name__}: {e}")
                results.append(res)
                with out_lock:
                    _print_progress(res, len(results), total, out=out)
                if on_progress is not None:
                    try:
                        on_progress(res, len(results), total)
                    except Exception as cb_err:
                        emit(f"[batch] on_progress raised "
                             f"{type(cb_err).__name__}: {cb_err} "
                             f"for {res.key}")
                if env_pool is not None and res.status == "ok":
                    env_futs.append((env_pool.submit(post_check, res), res))
                else:
                    finalize(res)
                if on_audit_complete is not None and res.status == "ok":
                    try:
                        on_audit_complete(res)
                    except Exception as cb_err:
                        emit(f"[batch] on_audit_complete raised "
                             f"{type(cb_err).__name__}: {cb_err} "
                             f"for {res.key}")
        except (KeyboardInterrupt, SystemExit):
            # Stop promptly: drop every queued key instead of running the
            # rest of the batch. In-flight keys finish on their own threads
            # and release their audit locks/leases as they exit `with lock`.
            interrupted = True
            ex.shutdown(wait=False, cancel_futures=True)
            emit("[batch] interrupted — cancelled queued keys; in-flight "
                 "keys finish and release their locks")
            raise
    finally:
        if interrupted:
            ex.shutdown(wait=False, cancel_futures=True)
            if env_pool is not None:
                env_pool.shutdown(wait=False, cancel_futures=True)
        else:
            ex.shutdown(wait=True)
            if env_pool is not None:
                _drain_post_checks(env_futs, env_pool, finalize, emit)

    return results


def _build_batch_session(
    settings: argus_config.ArgusConfig,
    extractor_concurrency: int,
) -> Any:
    """One authenticated Jira session for a batch, pooled for the extractor fan-out."""
    from requests.adapters import HTTPAdapter

    session = extractor.build_session(settings.extractor)
    pool_size = max(4, extractor_concurrency * 2)
    adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


_POST_CHECK_DRAIN_TIMEOUT_S = 300.0


def _drain_post_checks(
    env_futs: list[tuple[Future[bool], KeyResult]],
    env_pool: ThreadPoolExecutor,
    finalize: Callable[..., None],
    emit: Callable[[str], None],
) -> None:
    """Wait for the post-check tail after the audit phase.

    Workers finalize their own keys, so this only accounts for stragglers.
    The former 300 s per-future wait is one overall deadline for the tail:
    anything still pending afterwards is marked incomplete and finalized here
    (the worker's later finalize is then a no-op).
    """
    n_done = 0
    n_failed = 0
    n_timed_out = 0
    deadline = time.monotonic() + _POST_CHECK_DRAIN_TIMEOUT_S
    for env_fut, result in env_futs:
        try:
            clean = env_fut.result(
                timeout=max(0.0, deadline - time.monotonic()))
        except FutureTimeoutError:
            n_timed_out += 1
            emit("[batch] env_check timed out after 300s "
                 "(task still running; will not block shutdown)")
            finalize(
                result,
                incomplete=(
                    "environment",
                    "environment check timed out after 300 seconds",
                ),
            )
        except Exception as e:
            n_failed += 1
            diagnostic = (
                e.diagnostic
                if isinstance(e, analysis_status.AnalysisIncompleteError)
                else f"{type(e).__name__}: {e}"
            )
            emit(f"[batch] env_check raised {type(e).__name__}: {e}")
            finalize(result, incomplete=("environment", diagnostic))
        else:
            if clean:
                n_done += 1
            else:
                n_failed += 1
    if env_futs:
        msg = (f"[batch] env_check completed for "
               f"{n_done}/{len(env_futs)} keys")
        if n_failed:
            msg += f" ({n_failed} failed — see warnings above)"
        if n_timed_out:
            msg += f" ({n_timed_out} timed out)"
        emit(msg)
    env_pool.shutdown(wait=(n_timed_out == 0), cancel_futures=True)


def _print_progress(r: KeyResult, done: int, total: int, out: TextIO) -> None:
    if r.status == "ok":
        n = r.findings if r.findings is not None else 0
        v = r.verdict or "?"
        line = (f"[{done}/{total}] {r.key} ok "
                f"({v}, {n} finding{'s' if n != 1 else ''}, {r.elapsed_s:0.1f}s)")
    elif r.status == "skipped":
        line = f"[{done}/{total}] {r.key} skipped ({r.reason or 'no screenshots'})"
    elif r.status == "auth_failed":
        line = f"[{done}/{total}] {r.key} AUTH_FAILED  {r.reason or ''}"
    elif r.status == "analysis_incomplete":
        line = (
            f"[{done}/{total}] {r.key} ANALYSIS_INCOMPLETE  "
            f"{r.reason or ''}"
        )
    else:
        line = f"[{done}/{total}] {r.key} FAILED  {r.reason or ''}"
    print(line, file=out)
    getattr(out, "flush", lambda: None)()


def write_failed_keys(results: list[KeyResult], out_dir: Path) -> Path | None:
    """Write failed keys to <out_dir>/_failed_keys.txt for retry."""
    failed = [
        r.key
        for r in results
        if r.status in ("failed", "auth_failed", "analysis_incomplete")
    ]
    path = out_dir / "_failed_keys.txt"
    if not failed:
        path.unlink(missing_ok=True)
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(failed) + "\n")
    return path


def format_skipped_by_tester(results: list[KeyResult]) -> str | None:
    """Skipped-keys block grouped by tester (largest group first)."""
    skipped = [r for r in results if r.status == "skipped"]
    if not skipped:
        return None
    groups: dict[str, list[str]] = {}
    for r in skipped:
        name = r.tester or "unknown"
        groups.setdefault(name, []).append(r.key)
    lines = ["needs evidence (tester didn't attach any PDF or image):"]
    for name in sorted(groups, key=lambda n: (-len(groups[n]), n.lower())):
        ks = groups[name]
        lines.append(f"  {name} ({len(ks)}): {', '.join(ks)}")
    return "\n".join(lines)


def summarize(results: list[KeyResult]) -> str:
    ok = sum(1 for r in results if r.status == "ok")
    skipped = sum(1 for r in results if r.status == "skipped")
    failed = sum(1 for r in results if r.status == "failed")
    auth = sum(1 for r in results if r.status == "auth_failed")
    incomplete = sum(
        1 for r in results if r.status == "analysis_incomplete")
    parts = [f"{ok} succeeded"]
    if skipped:
        parts.append(f"{skipped} skipped")
    if incomplete:
        parts.append(f"{incomplete} analysis-incomplete")
    parts.append(f"{failed} failed")
    parts.append(f"{auth} auth-failed")
    return ", ".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ARGUS parallel batch runner")
    parser.add_argument("--keys-file", type=Path,
                        help="file with one execution key per line "
                             "(default: read from stdin)")
    parser.add_argument("--config", type=Path, default=None,
                        help="path to config.toml (default: ./config.toml)")
    parser.add_argument("--concurrency", type=int, default=None,
                        help="total parallel keys (overrides config.toml)")
    parser.add_argument("--extractor-concurrency", type=int, default=None,
                        help="parallel extract calls (overrides config.toml)")
    parser.add_argument("--debug", action="store_true",
                        help="include per-chunk breakdown and raw diagnostics "
                             "in audit.md (audit.json always carries full data)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    settings = argus_config.load(args.config)
    runtime.configure(settings)
    extractor.configure_logging(args.verbose or settings.logging.verbose)

    if args.debug:
        settings.auditor.debug_output = True

    concurrency = args.concurrency or settings.batch.concurrency
    extractor_concurrency = args.extractor_concurrency or settings.batch.extractor_concurrency

    if args.keys_file:
        keys = read_keys(None, args.keys_file)
    else:
        if sys.stdin.isatty():
            parser.error("no keys: provide --keys-file or pipe keys via stdin")
        keys = read_keys(sys.stdin, None)

    if not keys:
        print("batch: no keys to process", file=sys.stderr)
        return 1

    print(f"batch: {len(keys)} keys, concurrency={concurrency} "
          f"(extractor={extractor_concurrency})", file=sys.stderr)

    results = run_batch(keys, settings, concurrency, extractor_concurrency, out=sys.stderr)

    failed_path = write_failed_keys(results, settings.extractor.out_dir)
    summary = summarize(results)
    print(f"batch: {summary}", file=sys.stderr)
    skipped_block = format_skipped_by_tester(results)
    if skipped_block:
        print(skipped_block, file=sys.stderr)
    if failed_path:
        print(f"batch: failed keys -> {failed_path} "
              f"(retry: argus batch --keys-file {failed_path})",
              file=sys.stderr)

    if any(r.status == "auth_failed" for r in results):
        return 2
    if any(
        r.status in ("failed", "analysis_incomplete")
        for r in results
    ):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
