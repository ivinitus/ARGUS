"""Aggregate tester-compliance report across all executions under output/."""
from __future__ import annotations

import argparse
import collections
import dataclasses
import html as _html_lib
import json
import logging
import re
import sys
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

from argus import config as argus_config
from argus.core import atomic_io
from argus.domain import finding_inference
from argus.core import exec_paths
from argus import runtime
from argus.reporting.ui import (
    DEFAULT_UI_CONTEXT,
    ReportUiContext,
    from_settings as ui_context_from_settings,
)
from argus.reporting.verdicts import verdict_label

log = logging.getLogger("argus.report")


_CURRENT_SCHEMA_VERSION = 8



_STRUCTURAL_RULES = frozenset({"R0", "R2", "R4", "R8", "R9", "R19"})

_RULE_SOURCES = frozenset({"rule", "env_check", "device_check"})

_TRIAGE_CATEGORY_ORDER = (
    "tester_error",
    "insufficient_evidence",
    "product_issue",
    "environment_issue",
    "policy_exception",
)
_VALID_FINDING_CATEGORIES = frozenset(_TRIAGE_CATEGORY_ORDER)

_TRIAGE_ACTION_ORDER = (
    "add_evidence",
    "re_run",
    "fix_status",
    "attach_defect",
    "review_manually",
)
_VALID_FINDING_ACTIONS = frozenset(_TRIAGE_ACTION_ORDER)

_TRIAGE_ACTION_LABELS = {
    "add_evidence": "Add evidence",
    "re_run": "Re-run",
    "fix_status": "Fix status",
    "attach_defect": "Attach defect",
    "review_manually": "Review manually",
}

_TRIAGE_CATEGORY_LABELS = {
    "tester_error": "Tester error",
    "insufficient_evidence": "Insufficient evidence",
    "product_issue": "Product issue",
    "environment_issue": "Environment issue",
    "policy_exception": "Policy exception",
}


def _html_escape(value: object) -> str:
    """Escape a value for HTML text or attribute content."""
    if value is None:
        return ""
    return _html_lib.escape(str(value), quote=True)


def _int_value(value: object, default: int) -> int:
    """Return an integer for JSON scalar input, or a caller-provided default."""
    if isinstance(value, (int, float, str, bytes, bytearray)):
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    return default


class _PlainTextParser(HTMLParser):
    """Extract readable text from Zephyr's HTML step descriptions."""

    _BLOCK_TAGS = frozenset({
        "br", "div", "li", "ol", "p", "table", "td", "th", "tr", "ul",
    })
    _IGNORED_TAGS = frozenset({"script", "style"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del attrs
        if tag in self._IGNORED_TAGS:
            self._ignored_depth += 1
        elif not self._ignored_depth and tag in self._BLOCK_TAGS:
            self.parts.append(" ")

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)
        if tag in self._IGNORED_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._IGNORED_TAGS and self._ignored_depth:
            self._ignored_depth -= 1
        elif not self._ignored_depth and tag in self._BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


def _plain_step_description(value: object) -> str:
    """Normalize one optional HTML step description for report display."""
    if not isinstance(value, str) or not value.strip():
        return ""
    parser = _PlainTextParser()
    try:
        parser.feed(value)
        parser.close()
    except (ValueError, AssertionError):
        return ""
    return " ".join("".join(parser.parts).split())


@dataclass
class AuditSummary:
    """One execution's worth of audit + metadata, pre-digested for rendering."""
    key: str
    path: Path
    verdict: str | None
    findings_count: int
    severity_counts: dict[str, int]
    rules_fired: list[str]
    has_high_finding: bool
    schema_version: int | None
    tester: str | None
    tester_status: str | None
    test_case: str | None
    testrun_name: str | None = None
    testrun_key: str | None = None
    marketplace: str | None = None
    execution_date: str | None = None
    # UTC ISO-8601 timestamp ARGUS finished this audit (audit.json "audited_at").
    # Distinct from execution_date (the tester's Jira calendar date). Used to
    # filter the report by when the audit ran, independently of execution date.
    audited_at: str | None = None
    findings: list[dict] = dataclasses.field(default_factory=list)
    step_descriptions: dict[int, str] = dataclasses.field(default_factory=dict)
    step_statuses: dict[int, str] = dataclasses.field(default_factory=dict)
    evidence_by_step: list[dict] = dataclasses.field(default_factory=list)
    env_check: dict | None = None
    acknowledgments: list[dict] = dataclasses.field(default_factory=list)
    analysis_status: str = "complete"
    analysis_modules: dict = dataclasses.field(default_factory=dict)
    analysis_diagnostic: str | None = None
    xml_nodes: list[dict] = dataclasses.field(default_factory=list)
    video_evidence: dict = dataclasses.field(default_factory=dict)
    review_status: str = "none"
    requirement_assessments: list[dict] = dataclasses.field(default_factory=list)
    policy_revision: int | str | None = None


@dataclass(frozen=True)
class RenderedReportBundle:
    """Canonical lazy report plus a self-contained archive representation."""

    canonical_html: str
    archive_html: str
    detail_chunks: dict[str, dict[str, str]]


def is_complete_latest_run_manifest(manifest: dict[str, Any]) -> bool:
    """Return whether a manifest has the complete canonical v1 shape."""
    return (
        manifest.get("version") == 1
        and isinstance(manifest.get("folder_name"), str)
        and isinstance(manifest.get("started_at"), str)
        and isinstance(manifest.get("completed_at"), str)
        and all(
            isinstance(manifest.get(field), list)
            for field in (
                "scope_keys",
                "selected_keys",
                "audit_keys",
                "failed_keys",
            )
        )
        and isinstance(manifest.get("statuses"), dict)
    )


def _scan_output_dir(
    out_dir: Path,
    variant: str | None = None,
) -> list[AuditSummary]:
    """Build an AuditSummary per audit.json under out_dir."""
    from argus.pipeline import analysis_status

    summaries: list[AuditSummary] = []
    failed_keys: set[str] = set()
    if not out_dir.exists():
        return summaries
    if variant is None:
        audit_files = sorted(exec_paths.iter_latest_audit_jsons(out_dir))
        manifest_path = out_dir / "_latest_run.json"
        if manifest_path.is_file():
            manifest = _safe_read_metadata(manifest_path)
            audit_keys = manifest.get("audit_keys")
            if (
                is_complete_latest_run_manifest(manifest)
                and isinstance(audit_keys, list)
            ):
                allowed = {
                    key
                    for key in audit_keys
                    if isinstance(key, str) and key
                }
                scope = {
                    key for key in manifest["scope_keys"]
                    if isinstance(key, str) and key
                }
                failed_keys = {
                    key for key in manifest["failed_keys"]
                    if isinstance(key, str) and key in scope
                }
                allowed.update(failed_keys)
                audit_files = [
                    path
                    for path in audit_files
                    if path.parent.name in allowed
                ]
    else:
        audit_files = sorted(out_dir.rglob(f"_replay/{variant}/audit.json"))
    for audit_path in audit_files:
        try:
            audit = json.loads(audit_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if (
            variant is None
            and audit_path.parent.name in failed_keys
            and analysis_status.derive_status(audit) != "incomplete"
        ):
            # A failed attempt may leave an older completed audit on disk.
            # Show its current incomplete diagnostic only, never that result.
            continue
        if variant is None:
            exec_dir = audit_path.parent
        else:
            exec_dir = audit_path.parent.parent.parent
        metadata = _safe_read_metadata(exec_dir / "metadata.json")
        summaries.append(_build_summary(audit, metadata, exec_dir))
    return summaries


def _safe_read_metadata(path: Path) -> dict:
    """Read metadata.json, returning an empty dict on any failure."""
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _report_enrich_audit(audit: dict) -> dict:
    """Backfill v6 triage fields without importing the audit runtime."""
    enriched = dict(audit)
    enriched["findings"] = finding_inference.normalise_findings_for_schema(
        audit.get("findings") or []
    )
    return enriched


def _build_summary(audit: dict, metadata: dict,
                   exec_dir: Path) -> AuditSummary:
    from argus.pipeline import analysis_status as _analysis_status
    raw_triage_flags = [
        (
            rf.get("category") in _VALID_FINDING_CATEGORIES
            and rf.get("action") in _VALID_FINDING_ACTIONS
            and rf.get("confidence") in {"low", "medium", "high"}
        )
        for rf in (audit.get("findings") or [])
        if isinstance(rf, dict)
    ]
    try:
        from argus.pipeline import auditor as _auditor
        audit = _auditor.enrich_audit(audit, metadata)
    except Exception as e:
        log.debug("audit enrichment fell back to the report-local path: "
                  "%s: %s", type(e).__name__, e)
        audit = _report_enrich_audit(audit)
    audit = finding_inference.reconcile_verdict(audit)
    findings = audit.get("findings") or []
    for f, from_model in zip(findings, raw_triage_flags, strict=False):
        if isinstance(f, dict):
            f["_triage_from_model"] = from_model
    severity_counts: dict[str, int] = collections.Counter()
    rules_fired: list[str] = []
    has_high = False
    for f in findings:
        sev = f.get("severity", "unknown")
        severity_counts[sev] += 1
        if sev == "high":
            has_high = True
        if f.get("source") in _RULE_SOURCES:
            rule = f.get("rule")
            if rule:
                rules_fired.append(rule)

    tr_list = metadata.get("test_results") or []
    tr = tr_list[0] if tr_list else {}
    tester = tr.get("executed_by_name") or tr.get("executed_by")
    tester_status = tr.get("status")
    tc_key = tr.get("test_case_key")
    tc_name = tr.get("test_case_name")
    test_case: str | None
    if tc_key and tc_name:
        test_case = f"{tc_key} — {tc_name}"
    else:
        test_case = tc_key or tc_name

    step_descriptions: dict[int, str] = {}
    step_statuses: dict[int, str] = {}
    for step in tr.get("steps") or []:
        if not isinstance(step, dict):
            continue
        index = step.get("index")
        if not isinstance(index, int):
            continue
        description = _plain_step_description(step.get("description"))
        if description:
            step_descriptions[index] = description
        status = step.get("status")
        if isinstance(status, str) and status.strip():
            step_statuses[index] = status.strip()

    xml_nodes: list[dict] = []
    xml_path = exec_dir / "xml_evidence.json"
    if xml_path.exists():
        try:
            xml_payload = json.loads(xml_path.read_text())
            candidates = xml_payload.get("nodes")
            if isinstance(candidates, list):
                xml_nodes = [
                    node for node in candidates
                    if isinstance(node, dict)
                ]
        except (OSError, json.JSONDecodeError):
            xml_nodes = []

    completion = _analysis_status.derive_status(audit)
    analysis_modules = audit.get("analysis_modules")
    if not isinstance(analysis_modules, dict):
        analysis_modules = {}
    return AuditSummary(
        key=exec_dir.name,
        path=exec_dir,
        verdict=audit.get("overall_verdict"),
        findings_count=len(findings),
        severity_counts=dict(severity_counts),
        rules_fired=rules_fired,
        has_high_finding=has_high,
        schema_version=audit.get("schema_version"),
        tester=tester,
        tester_status=tester_status,
        test_case=test_case,
        testrun_name=tr.get("testrun_name"),
        testrun_key=tr.get("testrun_key"),
        marketplace=tr.get("marketplace"),
        execution_date=tr.get("execution_date"),
        audited_at=audit.get("audited_at"),
        policy_revision=(audit.get("provenance") or {}).get("policy_revision"),
        findings=findings,
        step_descriptions=step_descriptions,
        step_statuses=step_statuses,
        evidence_by_step=audit.get("evidence_by_step") or [],
        env_check=audit.get("env_check"),
        acknowledgments=audit.get("acknowledgments") or [],
        analysis_status=completion,
        analysis_modules=analysis_modules,
        analysis_diagnostic=_analysis_status.diagnostic(audit),
        xml_nodes=xml_nodes,
        video_evidence=(
            audit["_video_evidence"] if isinstance(audit.get("_video_evidence"), dict) else {}
        ),
        review_status=audit.get("review_status", "none"),
        requirement_assessments=audit.get("requirement_assessments") or [],
    )


def _is_flagged(s: AuditSummary) -> bool:
    """An audit is flagged if it warrants a human eyeball."""
    if s.analysis_status == "incomplete":
        return True
    if s.review_status == "required":
        return True
    if any(a.get("type") == "dismissed" for a in s.acknowledgments):
        return False
    if s.verdict == "fail":
        return True
    if s.has_high_finding:
        return True
    if any(r in _STRUCTURAL_RULES for r in _operational_rules(s)):
        return True
    return False


def _is_divergent(s: AuditSummary) -> bool:
    """Detect disagreement with a tester's configured Pass status."""
    from argus.domain import workflow_rules as _wr
    return s.analysis_status != "incomplete" \
        and _wr.is_pass_status(s.tester_status) \
        and s.verdict in {"concerns", "fail"}


def _is_stale(s: AuditSummary) -> bool:
    """Audit was produced by an older schema version than the current one."""
    v = s.schema_version
    return v is None or v < _CURRENT_SCHEMA_VERSION


def _finding_action(finding: dict) -> str:
    action = finding.get("action")
    return action if action in _VALID_FINDING_ACTIONS else "review_manually"


def _finding_category(finding: dict) -> str:
    category = finding.get("category")
    return (
        category if category in _VALID_FINDING_CATEGORIES
        else "tester_error"
    )


def _is_advisory(finding: dict) -> bool:
    return finding_inference.is_advisory_finding(finding)


def _operational_findings(s: AuditSummary) -> list[dict]:
    return [
        finding for finding in s.findings
        if isinstance(finding, dict) and not _is_advisory(finding)
    ]


def _advisory_findings(s: AuditSummary) -> list[dict]:
    return [
        finding for finding in s.findings
        if isinstance(finding, dict) and _is_advisory(finding)
    ]


def _operational_rules(s: AuditSummary) -> set[str]:
    if not s.findings:
        return set(s.rules_fired)
    return {
        str(finding["rule"])
        for finding in _operational_findings(s)
        if finding.get("source") in _RULE_SOURCES and finding.get("rule")
    }


def _finding_actions_for_summary(s: AuditSummary) -> set[str]:
    return {_finding_action(f) for f in _operational_findings(s)}


def _finding_categories_for_summary(s: AuditSummary) -> set[str]:
    return {_finding_category(f) for f in _operational_findings(s)}


def _action_counter(summaries: Iterable[AuditSummary]) -> collections.Counter:
    counter: collections.Counter = collections.Counter()
    for s in summaries:
        for f in _operational_findings(s):
            counter[_finding_action(f)] += 1
    return counter


def _category_counter(summaries: Iterable[AuditSummary]) -> collections.Counter:
    counter: collections.Counter = collections.Counter()
    for s in summaries:
        for f in _operational_findings(s):
            counter[_finding_category(f)] += 1
    return counter


def generate_report(out_dir: Path, variant: str | None = None,
                    *, summaries: list[AuditSummary] | None = None) -> str:
    """Scan `out_dir` and return the full markdown report as a string."""
    if summaries is None:
        summaries = _scan_output_dir(out_dir, variant=variant)
    parts = [
        _render_header(out_dir, summaries, variant=variant),
        _render_action_queue(summaries),
        _render_divergence(summaries),
        _render_flagged(summaries),
        _render_rule_distribution(summaries),
        _render_coverage(out_dir),
        _render_per_tester(summaries, out_dir),
        _render_stale(summaries),
        _render_failed_keys(out_dir),
    ]
    return "\n\n".join(p for p in parts if p).rstrip() + "\n"


def _render_header(
    out_dir: Path,
    summaries: list[AuditSummary],
    variant: str | None = None,
) -> str:
    total = len(summaries)
    variant_label = f" (variant {variant})" if variant else ""
    if total == 0:
        return (
            f"# Tester-compliance report{variant_label}\n\n"
            f"Scanned `{out_dir}` — no audits found.\n"
        )
    complete_summaries = [
        s for s in summaries if s.analysis_status != "incomplete"
    ]
    verdict_counts = collections.Counter(
        s.verdict or "unknown" for s in complete_summaries
    )
    incomplete = sum(
        1 for s in summaries if s.analysis_status == "incomplete")
    testers = {s.tester for s in summaries if s.tester}
    sev_total: collections.Counter[str] = collections.Counter()
    for s in complete_summaries:
        sev_total.update(s.severity_counts)
    rule_hits = sum(len(s.rules_fired) for s in complete_summaries)
    operational_findings = sum(
        len(_operational_findings(s)) for s in complete_summaries
    )
    advisory_findings = sum(
        len(_advisory_findings(s)) for s in complete_summaries
    )
    divergent = sum(1 for s in summaries if _is_divergent(s))
    flagged = sum(1 for s in summaries if _is_flagged(s))

    lines = [
        f"# Tester-compliance report{variant_label}",
        "",
        f"- **Scanned directory:** `{out_dir}`",
        f"- **Variant:** {variant or 'canonical'}",
        f"- **Audits scanned:** {total}",
        f"- **Analysis complete:** {total - incomplete}",
        f"- **Analysis incomplete (excluded from compliance totals):** "
        f"{incomplete}",
        f"- **Unique testers:** {len(testers)}",
        "- **Verdicts:** "
        + ", ".join(f"{verdict_label(k)}={verdict_counts[k]}"
                    for k in ("pass", "concerns", "fail", "unknown")
                    if verdict_counts[k]),
        f"- **Total findings:** {sum(sev_total.values())} "
        f"(high={sev_total['high']}, medium={sev_total['medium']}, "
        f"low={sev_total['low']})",
        f"- **Operational findings:** {operational_findings}",
        f"- **Advisories (excluded from compliance actions):** "
        f"{advisory_findings}",
        f"- **Rule-sourced findings:** {rule_hits}",
        f"- **Flagged for review:** {flagged} "
        f"({_pct(flagged, total)}%)",
        f"- **Tester-Pass vs Auditor-Concerns divergence:** {divergent} "
        f"({_pct(divergent, total)}%)",
    ]
    return "\n".join(lines)


def _render_action_queue(summaries: list[AuditSummary]) -> str:
    complete_summaries = [
        s for s in summaries if s.analysis_status != "incomplete"
    ]
    if not complete_summaries:
        return ""
    actions = _action_counter(complete_summaries)
    if not actions:
        return ""
    categories = _category_counter(complete_summaries)
    lines = [
        "## Action queue",
        "",
        "Findings grouped by the next QA action. This is the fastest way "
        "to turn the audit into follow-up work.",
        "",
        "| Action | Findings |",
        "| --- | ---: |",
    ]
    for action in _TRIAGE_ACTION_ORDER:
        if actions[action]:
            lines.append(
                f"| {_TRIAGE_ACTION_LABELS[action]} | {actions[action]} |"
            )
    lines.extend([
        "",
        "| Category | Findings |",
        "| --- | ---: |",
    ])
    for category in _TRIAGE_CATEGORY_ORDER:
        if categories[category]:
            lines.append(
                f"| {_TRIAGE_CATEGORY_LABELS[category]} | "
                f"{categories[category]} |"
            )
    return "\n".join(lines)


def _render_divergence(summaries: list[AuditSummary]) -> str:
    """Per-tester Pass-vs-Concerns divergence table."""
    if not summaries:
        return ""
    by_tester: dict[str, list[AuditSummary]] = collections.defaultdict(list)
    for s in summaries:
        by_tester[s.tester or "unknown"].append(s)

    from argus.domain import workflow_rules as _wr
    rows: list[tuple[str, int, int, int]] = []
    for name, group in by_tester.items():
        total = len(group)
        pass_claimed = sum(1 for s in group
                           if _wr.is_pass_status(s.tester_status))
        divergent = sum(1 for s in group if _is_divergent(s))
        rows.append((name, total, pass_claimed, divergent))

    def _sort_key(row: tuple[str, int, int, int]) -> tuple[float, int, str]:
        name, total, _, divergent = row
        rate = divergent / total if total else 0
        return (-rate, -divergent, name.lower())

    rows.sort(key=_sort_key)

    lines = [
        "## Tester-Pass vs Auditor-Concerns divergence",
        "",
        "Executions where the tester marked the overall test **Pass** but "
        "the auditor returned **Concerns** or **Deviation**. This is the "
        "primary tester-compliance signal — it represents evidence the "
        "tester either did not read the screenshots they submitted or "
        "did not document an issue they observed.",
        "",
        "| Tester | Audits | Tester-Pass | Divergent | Rate |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name, total, pass_claimed, divergent in rows:
        rate = _pct(divergent, total) if total else 0
        lines.append(
            f"| {name} | {total} | {pass_claimed} | {divergent} | {rate}% |"
        )
    return "\n".join(lines)


def _render_flagged(summaries: list[AuditSummary]) -> str:
    flagged = [s for s in summaries if _is_flagged(s)]
    if not flagged:
        return (
            "## Flagged for review\n\n"
            "_None — no audits need review or have incomplete analysis, a Deviation verdict, high-severity findings, "
            "or structural rule firings (R0/R2/R4/R8/R9)._"
        )
    def _order(s: AuditSummary) -> tuple[int, str]:
        rank = (
            0 if s.analysis_status == "incomplete"
            else 1 if s.verdict == "fail"
            else 2 if s.has_high_finding
            else 3 if s.review_status == "required"
            else 4
        )
        return (rank, s.key)

    lines = [
        "## Flagged for review",
        "",
        f"{len(flagged)} execution(s) need human review. Criteria: "
        "incomplete analysis, Needs review, Deviation verdict, any operational high-severity finding, or any structural "
        "rule fire (R0 — rule engine degraded; R2 — overall Pass with "
        "failing step; R4 — overall Fail with no failing step; "
        "R8 — overall Blocked with no Blocked step; "
        "R9 — prod URL detected in the active address bar; "
        "R19 — proven execution-mode or device-family mismatch).",
        "",
    ]
    for s in sorted(flagged, key=_order):
        reason_parts = []
        if s.analysis_status == "incomplete":
            reason_parts.append(
                "analysis incomplete"
                + (
                    f" ({s.analysis_diagnostic})"
                    if s.analysis_diagnostic else ""
                )
            )
        if s.review_status == "required":
            reason_parts.append("Needs review")
        if s.analysis_status != "incomplete" and s.verdict == "fail":
            reason_parts.append("verdict=Deviation")
        if s.analysis_status != "incomplete" and s.has_high_finding:
            n_high = s.severity_counts.get("high", 0)
            reason_parts.append(f"{n_high} high-severity finding(s)")
        struct_rules = [
            r for r in _operational_rules(s)
            if s.analysis_status != "incomplete" and r in _STRUCTURAL_RULES
        ]
        if struct_rules:
            reason_parts.append("rules=" + ",".join(sorted(set(struct_rules))))
        tester = s.tester or "unknown"
        tc = s.test_case or "(unknown test case)"
        reason = "; ".join(reason_parts) or "flagged"
        lines.append(f"- **{s.key}** [{tester}] — {reason}")
        lines.append(f"    - Test case: {tc}")
        lines.append(f"    - Tester status: {s.tester_status or '?'} → "
                     f"Auditor: {verdict_label(s.verdict)}")
        lines.append(f"    - Path: `{s.path}`")
    return "\n".join(lines)


def _render_rule_distribution(summaries: list[AuditSummary]) -> str:
    operational_hits: collections.Counter[str] = collections.Counter()
    advisory_hits: collections.Counter[str] = collections.Counter()
    for s in summaries:
        if s.analysis_status == "incomplete":
            continue
        for finding in s.findings:
            if (
                not isinstance(finding, dict)
                or finding.get("source") not in _RULE_SOURCES
                or not finding.get("rule")
            ):
                continue
            target = advisory_hits if _is_advisory(finding) else operational_hits
            target[str(finding["rule"])] += 1
    rule_hits = operational_hits + advisory_hits
    if not rule_hits:
        return ""

    known = ["R0", "R1", "R2", "R2b", "R4", "R5", "R6", "R7", "R8",
             "R9", "R9-doc", "R9-amb", "R9-hover", "R9-nav", "R9-susp",
             "R10", "R11", "R12", "R13",
             "R14a", "R14b", "R16", "R17", "R18", "R19", "R19-amb",
             "R20", "R21", "R22", "R-runtime"]
    known_hits = [(r, rule_hits[r]) for r in known if rule_hits[r]]
    unknown_hits = [(r, n) for r, n in rule_hits.items() if r not in known]
    ordered = known_hits + sorted(unknown_hits)

    descriptions = {
        "R0": "rule-engine degraded (status map didn't resolve)",
        "R1": "step marked 'In Progress' with no explanatory comment",
        "R2": "overall Pass but a step is Fail (contradiction)",
        "R2b": "overall Pass but every step is Not Executed "
                "(contradiction)",
        "R3": "DEPRECATED (historical): overall Pass + Blocked step",
        "R4": "overall Fail but no Fail/Blocked step (contradiction)",
        "R5": "Pass step with issue-language in tester comment",
        "R6": "Passed With Issue overall without comment or trace link",
        "R7": "Passed With Issue overall but no step is Fail",
        "R8": "overall Blocked but no step is Blocked (contradiction)",
        "R9": "prod URL detected in URL bar (deterministic env_check)",
        "R9-doc": "fallback environment used without a tester comment "
                  "explaining the preprod blocker",
        "R9-amb": "environment URL not verified; advisory for Mobile/Webview",
        "R9-hover": "historical page-body / hover URL finding (retired)",
        "R9-nav": "approved informational page opened on production — "
                  "advisory only, does not invalidate execution",
        "R9-susp": "suspected OCR misread — bare-apex prod URL read in "
                   "a session that is otherwise on feature-preprod "
                   "(domain-highlight transcription artefact); not "
                   "counted as a violation",
        "R10": "Jira defect key pasted in step comment instead of "
               "attached as trace_link",
        "R11": "Fail or Passed-With-Issue step has neither trace_links "
               "nor a defect ref in the comment (undocumented issue)",
        "R12": "Tester submitted zero auditable evidence — no PDF, images, "
               "or usable video",
        "R13": "Marketplace mismatch — assigned MP doesn't match the "
               ".example.<tld> seen in screenshots",
        "R14a": "status streak break — Pass after the same test case "
                "was Blocked/N-A in most recent executions",
        "R14b": "status streak break — Blocked/N-A after the same test "
                "case passed in most recent executions",
        "R16": "Fail/Blocked execution with no comment and no trace "
               "link (undocumented failure state)",
        "R17": "marked Not Applicable / Blocked although the immediate "
               "prior execution of the same test case was executed",
        "R18": "Not Applicable execution without a comment explaining "
               "why the test no longer applies",
        "R19": "proven execution-mode or assigned OS-family mismatch",
        "R19-amb": "execution mode or OS family could not be verified "
                   "(advisory)",
        "R20": "Blocked execution — synthetic audit; escalates when the "
               "same test case was also Blocked in the prior execution",
        "R21": "In Progress execution left unfinished — synthetic "
               "audit, no evidence to verify",
        "R22": "executed test has no non-secret test-account identifier",
        "R-runtime": "ARGUS model assessment failed after bounded retries; "
                     "re-audit required",
    }
    lines = [
        "## Workflow-rule firings",
        "",
        "Breakdown of deterministic rule hits across the scanned "
        "corpus. R0-R8 are pure functions of tester metadata "
        "(workflow_rules); R9 is sourced from env_check (OCR URL "
        "extraction). Each represents an inconsistent or "
        "non-compliant state the tester should not have submitted.",
        "",
        "| Rule | Operational | Advisory | Description |",
        "| --- | ---: | ---: | --- |",
    ]
    for rule, _n in ordered:
        desc = descriptions.get(rule, "(unknown rule)")
        lines.append(
            f"| {rule} | {operational_hits[rule]} | "
            f"{advisory_hits[rule]} | {desc} |"
        )
    return "\n".join(lines)


def _audit_join_key(s: "AuditSummary") -> tuple:
    """Legacy join key for roster rows without an execution key."""
    tc = (s.test_case or "").split(" — ", 1)[0].strip() or None
    return (tc, s.testrun_key, s.marketplace)


def _index_roster_audits(
    summaries: list["AuditSummary"],
) -> dict[tuple, "AuditSummary"]:
    """Match exact executions, retaining the legacy join for older rosters."""
    indexed: dict[tuple, AuditSummary] = {}
    for summary in summaries:
        for key in ((summary.key,), _audit_join_key(summary)):
            previous = indexed.get(key)
            if (
                previous is None
                or (summary.execution_date or "")
                >= (previous.execution_date or "")
            ):
                indexed[key] = summary
    return indexed


def _render_per_tester(summaries: list["AuditSummary"],
                       out_dir: Path) -> str:
    """Per-tester EXECUTION roster (every execution, audited or not)."""
    coverage = _load_coverage_json(out_dir)
    roster = (coverage or {}).get("executions")
    if not roster:
        return _render_per_tester_legacy(summaries)

    audited_by_key = _index_roster_audits(summaries)

    by_tester: dict[str, list[dict]] = collections.defaultdict(list)
    for row in roster:
        label = _resolve_tester_label(
            row.get("executed_by_name") or row.get("executed_by"))
        by_tester[label].append(row)

    lines = [
        "## Per-tester execution roster",
        "",
        "Every execution credited to the person who *ran* it "
        "(`executed_by`), not who it was assigned to. `audited=no` marks "
        "executions ARGUS has not audited (no evidence attached, or "
        "produced before they were in scope).",
        "",
    ]
    def _rank(name: str) -> tuple:
        rows = by_tester[name]
        not_audited = sum(
            1 for r in rows
            if _audit_join_key_from_row(r) not in audited_by_key)
        return (-not_audited, -len(rows), name.lower())

    for name in sorted(by_tester.keys(), key=_rank):
        rows = by_tester[name]
        total = len(rows)
        joined = [(r, audited_by_key.get(_audit_join_key_from_row(r)))
                  for r in rows]
        audited_n = sum(
            1 for _, a in joined
            if a is not None and a.analysis_status != "incomplete"
        )
        incomplete_n = sum(
            1 for _, a in joined
            if a is not None and a.analysis_status == "incomplete"
        )
        verdicts = collections.Counter(
            a.verdict or "unknown"
            for _, a in joined
            if a is not None and a.analysis_status != "incomplete"
        )
        v_parts = [f"{verdict_label(k)}={verdicts[k]}" for k in
                   ("pass", "concerns", "fail", "unknown") if verdicts[k]]
        lines.append(f"### {name}")
        lines.append("")
        lines.append(
            f"- Executions: {total} (audited {audited_n}, "
            f"incomplete {incomplete_n}, "
            f"not audited {total - audited_n - incomplete_n})")
        lines.append(
            f"- Verdicts (audited): {', '.join(v_parts) or '(none audited)'}")
        lines.append("")
        lines.append("| Test case | MP | Status | Audited | Verdict |")
        lines.append("| --- | --- | --- | --- | --- |")
        for row, audit in sorted(
                joined,
                key=lambda ra: (ra[1] is not None,
                                ra[0].get("test_case_key") or "")):
            tc = row.get("test_case_key") or "—"
            mp = row.get("marketplace") or "—"
            status = row.get("status") or "—"
            is_aud = "yes" if audit is not None else "no"
            verdict = (
                "incomplete"
                if audit is not None and audit.analysis_status == "incomplete"
                else (audit.verdict or "—") if audit is not None else "—"
            )
            asg = row.get("assigned_to_name") or row.get("assigned_to")
            exe = row.get("executed_by_name") or row.get("executed_by")
            mp_cell = mp
            if asg and exe and asg != exe:
                mp_cell = f"{mp} ⚠️(assigned: {asg})"
            lines.append(
                f"| {tc} | {mp_cell} | {status} | {is_aud} | {verdict_label(verdict)} |")
        lines.append("")
    return "\n".join(lines).rstrip()


def _audit_join_key_from_row(row: dict) -> tuple:
    """Use execution identity even if descriptive metadata has changed."""
    if row.get("e_key"):
        return (row["e_key"],)
    return (row.get("test_case_key"),
            row.get("testrun_key"),
            row.get("marketplace"))


def _render_per_tester_legacy(summaries: list["AuditSummary"]) -> str:
    """Audited-only per-tester rollup — fallback when no roster exists."""
    if not summaries:
        return ""
    by_tester: dict[str, list[AuditSummary]] = collections.defaultdict(list)
    for s in summaries:
        by_tester[s.tester or "unknown"].append(s)

    lines = [
        "## Per-tester audit summary",
        "",
    ]
    for name in sorted(by_tester.keys(), key=str.lower):
        group = by_tester[name]
        complete_group = [
            s for s in group if s.analysis_status != "incomplete"
        ]
        incomplete = len(group) - len(complete_group)
        verdicts = collections.Counter(
            s.verdict or "unknown" for s in complete_group
        )
        sev: collections.Counter[str] = collections.Counter()
        rules: collections.Counter[str] = collections.Counter()
        for s in complete_group:
            sev.update(s.severity_counts)
            rules.update(s.rules_fired)
        top_rules = ", ".join(f"{r}×{n}" for r, n in rules.most_common(3)) \
            or "(none)"
        v_parts = [f"{verdict_label(k)}={verdicts[k]}" for k in
                   ("pass", "concerns", "fail", "unknown") if verdicts[k]]
        sev_parts = [f"{k}={sev[k]}" for k in
                     ("high", "medium", "low") if sev[k]]
        lines.append(f"### {name}")
        lines.append("")
        lines.append(f"- Audits complete: {len(complete_group)}")
        lines.append(f"- Audits incomplete: {incomplete}")
        lines.append(f"- Verdicts: {', '.join(v_parts) or '(none)'}")
        if sev_parts:
            lines.append(f"- Findings: {', '.join(sev_parts)}")
        else:
            lines.append("- Findings: none")
        lines.append(f"- Top rules: {top_rules}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _render_stale(summaries: list[AuditSummary]) -> str:
    stale = [s for s in summaries if _is_stale(s)]
    if not stale:
        return ""
    lines = [
        "## Stale audits (re-audit recommended)",
        "",
        f"{len(stale)} audit(s) were written against an older schema "
        f"version (< {_CURRENT_SCHEMA_VERSION}). They may predate the "
        "current workflow-rule set (R0/R4/R6/R7/R8) or env_check (R9) "
        "and their findings are not directly comparable to current "
        "audits. Re-run the auditor (or env_check_backfill) to "
        "normalise.",
        "",
    ]
    for s in sorted(stale, key=lambda s: s.key):
        v = s.schema_version if s.schema_version is not None else "missing"
        lines.append(f"- **{s.key}** [{s.tester or 'unknown'}] "
                     f"schema_version={v}  `{s.path}`")
    return "\n".join(lines)


def read_report_failed_keys(out_dir: Path) -> list[str]:
    """Read retry markers belonging to the current report's execution scope."""
    failed_path = out_dir / "_failed_keys.txt"
    try:
        text = failed_path.read_text()
    except FileNotFoundError:
        return []
    keys = [line.strip() for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")]
    manifest = _safe_read_metadata(out_dir / "_latest_run.json")
    if is_complete_latest_run_manifest(manifest):
        scope = {
            key for key in manifest["scope_keys"] if isinstance(key, str)
        }
        keys = [key for key in keys if key in scope]
    return keys


def _render_failed_keys(out_dir: Path) -> str:
    """Section for current-scope keys that never produced an audit."""
    failed_path = out_dir / "_failed_keys.txt"
    keys = read_report_failed_keys(out_dir)
    if not keys:
        return ""
    lines = [
        "## Failed keys (no audit produced)",
        "",
        f"{len(keys)} key(s) failed the audit pipeline and never "
        f"produced an `audit.json`. Entries below are from "
        f"`{failed_path}`; per-key error details (when available) are "
        "read from `_last_error.json` next to the matching metadata.",
        "",
    ]
    errors_by_key = _collect_last_errors(out_dir)
    for key in keys:
        err_summary = _summarize_last_error(errors_by_key.get(key))
        if err_summary:
            lines.append(f"- **{key}** — {err_summary}")
        else:
            lines.append(f"- **{key}** — (no _last_error.json found)")
    return "\n".join(lines)


def _load_coverage_json(out_dir: Path) -> dict | None:
    """Read `<out_dir>/_coverage.json` if present and well-formed."""
    from argus.core.coverage_scope import covers_restored_scope

    manifest = _safe_read_metadata(out_dir / "_latest_run.json")
    if (
        is_complete_latest_run_manifest(manifest)
        and isinstance(manifest.get("execution_key"), str)
    ):
        return None
    path = out_dir / "_coverage.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("schema") != 1:
        return None
    if not covers_restored_scope(data, manifest):
        return None
    return data


def _render_coverage(out_dir: Path) -> str:
    """Markdown render of `_coverage.json` (R15 a/b/c findings)."""
    data = _load_coverage_json(out_dir)
    if data is None:
        return ""
    findings = data.get("findings") or []
    items_total = int(data.get("items_total") or 0)
    items_executed = int(data.get("items_executed") or 0)
    items_unexecuted = int(data.get("items_unexecuted") or 0)
    pct = _pct(items_executed, items_total) if items_total else 0

    if items_total == 0 and not findings:
        return ""

    lines = [
        "## Coverage",
        "",
        f"{items_total} item(s) planned, {items_executed} completed "
        f"({pct}%), {items_unexecuted} not completed.",
        "",
    ]

    r15a = [f for f in findings if f.get("rule") == "R15a"]
    r15c = [f for f in findings if f.get("rule") == "R15c"]

    if not findings:
        lines.append("_No coverage findings._")
        return "\n".join(lines)

    if r15a:
        lines.append("### Planned but not executed")
        lines.append("")
        by_tester: dict[str, list[dict]] = collections.defaultdict(list)
        for f in r15a:
            by_tester[_resolve_tester_label(f.get("tester"))].append(f)
        ranked = sorted(by_tester.items(),
                        key=lambda kv: (-len(kv[1]), kv[0].lower()))
        for tester, group in ranked:
            runs = [
                _coverage_planned_run_label(s)
                for s in _coverage_planned_run_summaries(group)
            ]
            lines.append(
                f"- **{tester}** — {len(group)} not executed: "
                f"{', '.join(runs)}"
            )
        lines.append("")

    attempted = _attempted_incomplete_by_tester(data)
    if attempted:
        lines.append("### Attempted but not completed")
        lines.append("")
        for tester, rows in attempted:
            items = ", ".join(
                f"{r.get('test_case_key')} "
                f"({r.get('marketplace') or '?'}: {r.get('status')})"
                for r in rows
            )
            lines.append(
                f"- **{tester}** — {len(rows)} attempted but not "
                f"completed: {items}"
            )
        lines.append("")

    if r15c:
        lines.append("### Tester completion")
        lines.append("")
        lines.append("| Tester | Completion |")
        lines.append("| --- | --- |")
        for raw_tester, summary in _tester_completion_rows(data, r15c):
            lines.append(
                f"| {_resolve_tester_label(raw_tester)} | {summary} |")

    return "\n".join(lines).rstrip()


def _attempted_incomplete_by_tester(
    data: dict,
) -> list[tuple[str, list[dict]]]:
    """Roster executions ATTEMPTED but not completed, grouped by executor."""
    try:
        from argus.domain.folder_coverage import is_executed_status
    except Exception as e:
        log.debug("attempted-executions section skipped, folder_coverage "
                  "unavailable: %s: %s", type(e).__name__, e)
        return []
    by: dict[str, list[dict]] = {}
    for r in data.get("executions") or []:
        if is_executed_status(r.get("status")):
            continue
        who = _resolve_tester_label(
            r.get("executed_by_name") or r.get("executed_by"))
        by.setdefault(who, []).append(r)
    return sorted(by.items(), key=lambda kv: (-len(kv[1]), kv[0].lower()))


def _tester_completion_rows(
    data: dict,
    r15c: list[dict],
) -> list[tuple[str | None, str]]:
    """(raw_tester, completion_summary) rows for the R15c table."""
    tester_completion = data.get("tester_completion") or []
    low_by_tester = {f.get("tester"): f for f in r15c}
    rows: list[tuple[str | None, str]] = []
    if tester_completion:
        for row in tester_completion:
            f = low_by_tester.get(row.get("tester"))
            if f is not None:
                summary = _coverage_completion_summary(f)
            else:
                summary = (
                    f"{row.get('executed_count')}/{row.get('assigned_count')}"
                    f" completed ({row.get('completion_pct')}%)"
                )
            rows.append((row.get("tester"), summary))
    else:
        for f in sorted(r15c, key=lambda f: f.get("tester") or ""):
            rows.append((f.get("tester"), _coverage_completion_summary(f)))
    return rows


_JIRA_BASE_URL = "https://jira.example.com"
_JIRA_TESTS_BASE = f"{_JIRA_BASE_URL}/secure/Tests.jspa"


def configure(base_url: str) -> None:
    """Configure Jira links for the selected team profile."""
    global _JIRA_BASE_URL, _JIRA_TESTS_BASE
    _JIRA_BASE_URL = base_url.rstrip("/")
    _JIRA_TESTS_BASE = f"{_JIRA_BASE_URL}/secure/Tests.jspa"


def _resolve_tester_label(tester: str | None) -> str:
    """Return the best human label for a tester field captured at coverage-write time."""
    if not tester:
        return "(unassigned)"
    if not tester.startswith("JIRAUSER"):
        return tester
    try:
        from argus.core import users
        resolved = users.get_default_cache().get_resolved(tester)
        if resolved:
            return resolved
    except Exception as e:
        log.debug("tester name resolution failed for %s: %s: %s",
                  tester, type(e).__name__, e)
    return tester


def _coverage_completion_summary(f: dict) -> str:
    """Format an R15c finding without parsing its prose when structured counts are available."""
    assigned = _int_value(f.get("assigned_count"), -1)
    completed = _int_value(f.get("executed_count"), -1)
    if assigned > 0 and completed >= 0:
        pct = _int_value(f.get("completion_pct"), _pct(completed, assigned))
        target = _int_value(f.get("threshold_pct"), 100)
        return f"{completed}/{assigned} completed ({pct}%; target {target}%)"

    desc = (f.get("description") or "").removeprefix("R15c: ").strip()
    tester = str(f.get("tester") or "").strip()
    if tester and desc.startswith(f"{tester} "):
        desc = desc[len(tester) + 1:].strip()
    old_match = re.search(
        r"assigned\s+(\d+)\s+item\(s\)\s+but\s+executed\s+only\s+"
        r"(\d+)\s+\((\d+)%\s+<\s+(\d+)%\s+threshold\)",
        desc,
        flags=re.IGNORECASE,
    )
    if old_match:
        old_assigned, old_completed, old_pct, _old_target = old_match.groups()
        return (
            f"{old_completed}/{old_assigned} completed "
            f"({old_pct}%; target 100%)"
        )
    return desc or "Below completion target"


def _coverage_testrun_key(f: dict) -> str:
    """Return a testrun key for an R15a finding, including legacy payloads."""
    key = str(f.get("testrun_key") or "").strip()
    if key:
        return key
    desc = str(f.get("description") or "")
    match = re.search(r"\b([A-Z][A-Z0-9_]*-C\d+)\b", desc)
    return match.group(1) if match else ""


def _coverage_planned_run_summaries(findings: list[dict]) -> list[dict[str, object]]:
    """Collapse planned-but-not-executed findings to one row per run/MP."""
    counts: collections.Counter[tuple[str, str]] = collections.Counter()
    for f in findings:
        run = _coverage_testrun_key(f) or "unknown run"
        marketplace = str(f.get("marketplace") or "unknown marketplace").strip()
        counts[(run, marketplace or "unknown marketplace")] += 1

    summaries: list[dict[str, object]] = []
    for (run, marketplace), count in sorted(
        counts.items(),
        key=lambda kv: (kv[0][0], kv[0][1]),
    ):
        summaries.append({
            "testrun_key": run,
            "marketplace": marketplace,
            "count": count,
        })
    return summaries


def _coverage_planned_run_label(summary: dict[str, object]) -> str:
    run = str(summary.get("testrun_key") or "unknown run")
    marketplace = str(summary.get("marketplace") or "unknown marketplace")
    count = _int_value(summary.get("count"), 0)
    return f"{run} ({marketplace}: {count})"


def _coverage_planned_run_html(summary: dict[str, object]) -> str:
    run = str(summary.get("testrun_key") or "unknown run")
    marketplace = str(summary.get("marketplace") or "unknown marketplace")
    count = _int_value(summary.get("count"), 0)
    run_html = _jira_browse_anchor(run) if re.search(r"-C\d+\b", run) else _html_escape(run)
    return f'{run_html} <span class="argus-muted">({_html_escape(marketplace)}: {count})</span>'


def _coverage_group_html(tester: str, count_label: str, chips: str) -> str:
    """One per-tester group in the coverage drawer: a header line (tester name + count pill) over a chip row."""
    tester_cls = "argus-cov-tester"
    if tester == "(unassigned)":
        tester_cls += " is-unassigned"
    return (
        '<div class="argus-cov-group">'
        '<div class="argus-cov-group-head">'
        f'<span class="{tester_cls}">{_html_escape(tester)}</span>'
        f'<span class="argus-cov-count">{_html_escape(count_label)}</span>'
        '</div>'
        f'<div class="argus-cov-chips">{chips}</div>'
        '</div>'
    )


def _coverage_run_chip(summary: dict[str, object]) -> str:
    """A planned-run chip: linked C-key + marketplace badge + item count."""
    run = str(summary.get("testrun_key") or "unknown run")
    marketplace = str(summary.get("marketplace") or "?")
    count = _int_value(summary.get("count"), 0)
    run_html = (_jira_browse_anchor(run)
                if re.search(r"-C\d+\b", run) else _html_escape(run))
    return (
        '<span class="argus-cov-chip">'
        f'<span class="argus-cov-chip-key">{run_html}</span>'
        f'<span class="argus-mp-badge">{_html_escape(marketplace)}</span>'
        f'<span class="argus-cov-chip-n">×{count}</span>'
        '</span>'
    )


def _coverage_status_chip(row: dict) -> str:
    """An attempted-item chip: linked T-key + marketplace badge + a status tag colour-coded like the verdict pills."""
    tc = row.get("test_case_key") or ""
    tc_html = _jira_browse_anchor(tc) if tc else "(unknown)"
    marketplace = row.get("marketplace") or "?"
    status = str(row.get("status") or "?")
    status_cls = "is-" + re.sub(r"[^a-z0-9]+", "-", status.lower()).strip("-")
    return (
        '<span class="argus-cov-chip">'
        f'<span class="argus-cov-chip-key">{tc_html}</span>'
        f'<span class="argus-mp-badge">{_html_escape(marketplace)}</span>'
        f'<span class="argus-cov-status {status_cls}">'
        f'{_html_escape(status)}</span>'
        '</span>'
    )


def _jira_browse_anchor(key: str) -> str:
    """Wrap a Zephyr or Jira key in a clickable link."""
    if not key:
        return ""
    safe = _html_escape(key)
    if "-T" in key:
        url = f"{_JIRA_TESTS_BASE}#/testCase/{safe}"
    elif "-E" in key:
        url = f"{_JIRA_TESTS_BASE}#/testPlayer/testExecution/{safe}"
    elif "-C" in key:
        url = f"{_JIRA_TESTS_BASE}#/testPlayer/{safe}"
    else:
        url = f"{_JIRA_BASE_URL}/browse/{safe}"
    return (
        f'<a href="{url}" '
        f'target="_blank" rel="noopener" '
        f'title="Open {safe} in Jira">{safe}</a>'
    )


def _argus_tester_cell(tester_name: str) -> str:
    """Tester name as plain text in the main audit table."""
    return f'<span class="argus-tester-name">{_html_escape(tester_name)}</span>'


_ARGUS_STATUS_ATTENTION_RANK = {
    "BLOCKED": 0,
    "IN PROGRESS": 1,
    "FAIL": 2,
    "PASSED WITH ISSUE": 3,
    "RETEST": 4,
    "NOT EXECUTED": 5,
    "NOT APPLICABLE": 6,
    "INACTIVE": 7,
    "PASS": 9,
}


def _argus_status_pill(status: str) -> str:
    """Colored pill for a Zephyr execution status."""
    if not status:
        return '<span class="argus-muted">—</span>'
    s = status.strip().upper().replace("_", " ")
    cls = {
        "PASS": "argus-status-pass",
        "FAIL": "argus-status-fail",
        "PASSED WITH ISSUE": "argus-status-pwi",
        "BLOCKED": "argus-status-blocked",
        "IN PROGRESS": "argus-status-inprogress",
        "RETEST": "argus-status-retest",
        "NOT EXECUTED": "argus-status-notexecuted",
        "INACTIVE": "argus-status-inactive",
        "NOT APPLICABLE": "argus-status-na",
    }.get(s, "argus-status-neutral")
    return (f'<span class="argus-status-pill {cls}">'
            f'{_html_escape(status)}</span>')


def _argus_exec_key_link(key: str) -> str:
    """Link an execution key to its Zephyr Test Player view."""
    if not key:
        return ""
    safe = _html_escape(key)
    if "-E" not in key:
        return safe
    url = f"{_JIRA_TESTS_BASE}#/testPlayer/testExecution/{safe}"
    return (
        f'<a href="{url}" target="_blank" rel="noopener" '
        f'class="argus-key-link" '
        f'title="Open execution {safe} in Jira Test Player">{safe}</a>'
    )


def _render_coverage_html(out_dir: Path) -> str:
    """HTML render of `_coverage.json` as a slide-in drawer."""
    data = _load_coverage_json(out_dir)
    if data is None:
        return ""
    findings = data.get("findings") or []
    items_total = int(data.get("items_total") or 0)
    items_executed = int(data.get("items_executed") or 0)
    items_unexecuted = int(data.get("items_unexecuted") or 0)
    pct = _pct(items_executed, items_total) if items_total else 0

    if items_total == 0 and not findings:
        return ""

    r15a = [f for f in findings if f.get("rule") == "R15a"]
    r15c = [f for f in findings if f.get("rule") == "R15c"]
    attempted = _attempted_incomplete_by_tester(data)
    attempted_count = sum(len(rows) for _, rows in attempted)

    total_findings = len(r15a) + len(r15c)
    trigger_label = "Coverage"
    trigger_count = (
        f'<span class="argus-coverage-trigger-count">{total_findings}</span>'
        if total_findings else ""
    )
    trigger_icon = "!" if total_findings else "OK"
    trigger_class = (
        "argus-coverage-trigger"
        + (" is-clean" if total_findings == 0 else "")
    )
    trigger = (
        f'<button type="button" class="{trigger_class}" '
        f'id="argus-coverage-open" '
        f'aria-controls="argus-coverage-drawer" '
        f'aria-expanded="false" '
        f'title="Open coverage panel">'
        '<span class="argus-coverage-trigger-icon" aria-hidden="true">'
        f'{trigger_icon}</span>'
        f'{trigger_label}'
        f'{trigger_count}'
        f'</button>'
    )

    parts = [
        '<div class="argus-coverage-backdrop" '
        'id="argus-coverage-backdrop" hidden></div>',
        '<aside class="argus-coverage-drawer" '
        'id="argus-coverage-drawer" '
        'role="dialog" aria-modal="true" '
        'aria-labelledby="argus-coverage-drawer-title" '
        'aria-hidden="true">',
        '<div class="argus-coverage-drawer-header">',
        '<h2 id="argus-coverage-drawer-title">Coverage</h2>',
        '<button type="button" class="argus-coverage-close" '
        'id="argus-coverage-close" '
        'aria-label="Close coverage panel">×</button>',
        '</div>',
        '<div class="argus-coverage-drawer-sub">',
        f'{items_total} item(s) planned · {items_executed} completed '
        f'({pct}%) · <strong>{items_unexecuted} not completed</strong>',
        '</div>',
    ]

    if not total_findings and not attempted_count:
        parts.append(
            '<p class="argus-coverage-clean">'
            'No coverage findings.</p>')
        parts.append('</aside>')
        return trigger + "".join(parts)


    r15a_panel = ""
    if r15a:
        by_tester: dict[str, list[dict]] = collections.defaultdict(list)
        for f in r15a:
            label = _resolve_tester_label(f.get("tester"))
            by_tester[label].append(f)
        ranked = sorted(by_tester.items(),
                        key=lambda kv: (-len(kv[1]), kv[0].lower()))
        groups = []
        for tester, group in ranked:
            chips = "".join(
                _coverage_run_chip(s)
                for s in _coverage_planned_run_summaries(group)
            )
            groups.append(_coverage_group_html(
                tester, f"{len(group)} not executed", chips))
        r15a_panel = "".join(groups)

    attempted_panel = ""
    if attempted:
        attempted_panel = "".join(
            _coverage_group_html(
                tester, f"{len(arows)} open",
                "".join(_coverage_status_chip(r) for r in arows))
            for tester, arows in attempted
        )

    r15c_panel = ""
    if r15c:
        rows = []
        for raw_tester, desc in _tester_completion_rows(data, r15c):
            tester = _resolve_tester_label(raw_tester)
            rows.append(
                f'<tr><td>{_html_escape(tester)}</td>'
                f'<td>{_html_escape(desc)}</td></tr>'
            )
        r15c_panel = (
            '<table class="argus-coverage-table">'
            '<thead><tr><th>Tester</th><th>Completion</th></tr></thead>'
            '<tbody>' + "".join(rows) + '</tbody>'
            '</table>'
        )

    tabs_meta = [
        ("planned", "Planned but not executed", len(r15a), r15a_panel),
        ("attempted", "Attempted but not completed", attempted_count,
         attempted_panel),
        ("completion", "Tester completion", len(r15c), r15c_panel),
    ]
    tabs_meta = [(rule, label, n, panel)
                 for rule, label, n, panel in tabs_meta
                 if n > 0 and panel]
    if tabs_meta:
        tab_buttons: list[str] = []
        tab_panels: list[str] = []
        first = True
        for rule, label, n, panel in tabs_meta:
            sel = "true" if first else "false"
            hidden = "" if first else " hidden"
            tab_buttons.append(
                f'<button role="tab" '
                f'id="argus-cov-tab-{rule}" '
                f'aria-controls="argus-cov-panel-{rule}" '
                f'aria-selected="{sel}" '
                f'tabindex="{0 if first else -1}" '
                f'class="argus-coverage-tab" '
                f'data-rule="{rule}">'
                f'{_html_escape(label)} <span class="argus-muted">'
                f'({n})</span>'
                f'</button>'
            )
            tab_panels.append(
                f'<div role="tabpanel" '
                f'id="argus-cov-panel-{rule}" '
                f'aria-labelledby="argus-cov-tab-{rule}" '
                f'class="argus-coverage-panel"{hidden}>'
                f'{panel}'
                f'</div>'
            )
            first = False
        parts.append(
            '<div role="tablist" class="argus-coverage-tabs" '
            'aria-label="Coverage categories">'
            + "".join(tab_buttons)
            + '</div>'
        )
        parts.extend(tab_panels)

    parts.append('</aside>')
    return trigger + "".join(parts)


def _collect_last_errors(out_dir: Path) -> dict[str, Path]:
    """Map E-key dir name -> its _last_error.json path, in ONE tree walk."""
    found: dict[str, Path] = {}
    for candidate in out_dir.rglob("_last_error.json"):
        found.setdefault(candidate.parent.name, candidate)
    return found


def _summarize_last_error(candidate: Path | None) -> str | None:
    """One-line error summary from a _last_error.json path (None-safe)."""
    if candidate is None:
        return None
    try:
        err = json.loads(candidate.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if "exception_class" in err:
        return (f"{err['exception_class']}: "
                f"{err.get('exception_message', '')}"
                f" ({candidate})")
    if "failure_reason" in err:
        return f"{err['failure_reason']} ({candidate})"
    return f"error recorded at {candidate}"


def _find_last_error(out_dir: Path, key: str) -> str | None:
    """Back-compat single-key lookup (tests/tools)."""
    for candidate in out_dir.rglob(f"{key}/_last_error.json"):
        summary = _summarize_last_error(candidate)
        if summary:
            return summary
    return None


def _pct(n: int, total: int) -> int:
    """Integer percent for headline-readable output. 0 when total is 0."""
    if total <= 0:
        return 0
    return round(100 * n / total)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Aggregate tester-compliance report across output/"
    )
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="directory to scan (default: from config.toml "
                             "extractor.out_dir)")
    parser.add_argument("--config", type=Path, default=None,
                        help="path to config.toml (default: ./config.toml)")
    parser.add_argument("--variant", type=str, default=None,
                        help="read replay audits for this variant "
                             "(e.g. v3, v4) instead of canonical audit.json. "
                             "Output filename gains a _<variant> suffix so "
                             "canonical and per-variant reports coexist.")
    parser.add_argument("--stdout", action="store_true",
                        help="also print the report to stdout")
    parser.add_argument("--no-report-file", action="store_true",
                        help="skip writing _report.md (useful with --stdout "
                             "for dry runs)")
    parser.add_argument("--html", action="store_true",
                        help="also emit an HTML report (_report.html or "
                             "_report_<variant>.html) — self-contained, "
                             "sortable, filterable, one-click expand to "
                             "per-audit findings and env_check URLs.")
    parser.add_argument("--no-markdown", action="store_true",
                        help="skip writing the markdown report (useful when "
                             "only the HTML is wanted).")
    args = parser.parse_args(argv)

    settings = argus_config.load(args.config)
    runtime.configure(settings)
    out_dir = args.out_dir or settings.extractor.out_dir
    summaries = scan_output_dir(out_dir, variant=args.variant)
    report = generate_report(out_dir, variant=args.variant,
                             summaries=summaries)

    if not args.no_report_file and not args.no_markdown:
        out_dir.mkdir(parents=True, exist_ok=True)
        import datetime as _dt
        stamp = _dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        variant_suffix = f"_{args.variant}" if args.variant else ""
        filename = f"_report{variant_suffix}_{stamp}.md"
        report_archive = out_dir / filename
        report_canonical = out_dir / f"_report{variant_suffix}.md"
        atomic_io.atomic_write_text(report_canonical, report)
        atomic_io.atomic_write_text(report_archive, report)
        print(f"[ARGUS] wrote {report_canonical}", file=sys.stderr)
        print(f"[ARGUS] archive: {report_archive.name}", file=sys.stderr)

    if args.html and not args.no_report_file:
        out_dir.mkdir(parents=True, exist_ok=True)
        import datetime as _dt2
        stamp = _dt2.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        variant_suffix = f"_{args.variant}" if args.variant else ""
        ui_context = ui_context_from_settings(settings)
        html_canonical = write_html_report(
            out_dir,
            variant=args.variant,
            summaries=summaries,
            ui_context=ui_context,
        )
        html_archive = out_dir / f"_argus{variant_suffix}_{stamp}.html"
        archive_html = generate_html_report(
            out_dir,
            variant=args.variant,
            summaries=summaries,
            ui_context=ui_context,
        )
        atomic_io.atomic_write_text(html_archive, archive_html)
        print(f"[ARGUS] open: file://{html_canonical.resolve()}",
              file=sys.stderr)
        print(f"[ARGUS] archive: {html_archive.name}", file=sys.stderr)

    if args.stdout:
        sys.stdout.write(report)

    return 0


def scan_output_dir(
    out_dir: Path,
    variant: str | None = None,
) -> list[AuditSummary]:
    """Public reusable output scan."""
    return _scan_output_dir(out_dir, variant=variant)


def generate_html_report(
    out_dir: Path,
    variant: str | None = None,
    *,
    summaries: list[AuditSummary] | None = None,
    ui_context: ReportUiContext = DEFAULT_UI_CONTEXT,
) -> str:
    """Render the self-contained HTML report."""
    if summaries is None:
        summaries = _scan_output_dir(out_dir, variant=variant)
    return _html_report._render_argus_html_report(
        out_dir,
        summaries,
        variant=variant,
        ui_context=ui_context,
    )


def generate_html_report_bundle(
    out_dir: Path,
    variant: str | None = None,
    *,
    summaries: list[AuditSummary] | None = None,
    ui_context: ReportUiContext = DEFAULT_UI_CONTEXT,
) -> RenderedReportBundle:
    """Render a lazy canonical page and a self-contained archive."""
    if summaries is None:
        summaries = _scan_output_dir(out_dir, variant=variant)
    detail_sink: dict[int, str] = {}
    canonical_html = _html_report._render_argus_html_report(
        out_dir,
        summaries,
        variant=variant,
        detail_sink=detail_sink,
        ui_context=ui_context,
    )
    archive_html = _html_report._render_argus_html_report(
        out_dir,
        summaries,
        variant=variant,
        ui_context=ui_context,
    )
    return RenderedReportBundle(
        canonical_html=canonical_html,
        archive_html=archive_html,
        detail_chunks=_html_report.detail_chunk_maps(detail_sink),
    )


def write_html_report(
    out_dir: Path,
    variant: str | None = None,
    *,
    summaries: list[AuditSummary] | None = None,
    ui_context: ReportUiContext = DEFAULT_UI_CONTEXT,
) -> Path:
    """Write a locally served lazy report and its detail chunks."""
    bundle = generate_html_report_bundle(
        out_dir,
        variant=variant,
        summaries=summaries,
        ui_context=ui_context,
    )
    suffix = f"_{variant}" if variant else ""
    details_dir = out_dir / f"{_html_report.DETAILS_DIRNAME}{suffix}"
    details_dir.mkdir(parents=True, exist_ok=True)
    for stale in details_dir.glob("*.json"):
        if stale.name not in bundle.detail_chunks:
            stale.unlink()
    for name, payload in bundle.detail_chunks.items():
        atomic_io.atomic_write_text(
            details_dir / name,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )
    canonical = out_dir / f"_argus{suffix}.html"
    canonical_html = bundle.canonical_html.replace(
        "__ARGUS_DETAIL_BASE__",
        f"{details_dir.name}/",
    )
    atomic_io.atomic_write_text(canonical, canonical_html)
    return canonical


from . import html_report as _html_report  # noqa: E402 — cycle with html_report


def __getattr__(name: str):
    """Expose historical private HTML helpers during the migration window."""
    try:
        return getattr(_html_report, name)
    except AttributeError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
