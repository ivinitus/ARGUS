"""Aggregate tester-compliance report across all executions under output/.

Walks <out_dir>/<testrun_slug>/<tester_slug>/<E-KEY>/ once and produces
a markdown report (and optionally HTML via _render_argus_html_report)
covering: flagged count, tester-Pass-vs-auditor-Concerns divergence,
rule-firing histogram, stale audits, failed keys.

CLI:
    python report.py [--out-dir DIR] [--stdout] [--html]
"""
from __future__ import annotations

import argparse
import collections
import dataclasses
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import config as argus_config


# Kept in sync with auditor.AUDIT_SCHEMA_VERSION by policy (no import to
# avoid coupling report.py to the audit pipeline).
_CURRENT_SCHEMA_VERSION = 6


# Force-flag rules regardless of model verdict (deterministic
# contradictions). R3 deprecated (Pass+Blocked is acceptable per
# 2026-05-09 policy); R7 surfaces but doesn't force-flag.
# R9 is env_check-sourced but treated structural — pixel evidence.
_STRUCTURAL_RULES = frozenset({"R0", "R2", "R4", "R8", "R9"})

_RULE_SOURCES = frozenset({"rule", "env_check"})

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

_REPORT_INSUFFICIENT_EVIDENCE_TERMS = (
    "no evidence",
    "missing evidence",
    "not evidenced",
    "not otherwise evidenced",
    "not shown",
    "not otherwise shown",
    "not present",
    "no screenshot",
    "no library screenshot",
    "no purchase history screenshot",
    "no inbox screenshot",
    "no email screenshot",
    "screenshot missing",
    "not provided",
    "not verified",
    "not otherwise verified",
    "unable to verify",
    "requires verifying",
    "required verification",
)

_REPORT_ENVIRONMENT_TERMS = (
    "production url",
    "prod url",
    "feature-preprod",
    "marketplace",
    "currency",
    "locale",
    "storefront",
    "exampleapp.",
)

_REPORT_PRODUCT_ISSUE_TERMS = (
    "crash",
    "stack trace",
    "404",
    "500",
    "5xx",
    "broken",
    "error",
    "failed to load",
    "something went wrong",
)

_REPORT_VISUAL_CLAIM_TERMS = (
    "screenshot",
    "page",
    "visible",
    "shown",
    "screen",
    "image",
    "ui",
    "button",
    "banner",
    "modal",
    "order summary",
)


@dataclass
class AuditSummary:
    """One execution's worth of audit + metadata, pre-digested for rendering.

    Best-effort: fields whose source file is unreadable remain None.
    Consumers must handle None, not KeyError.
    """
    key: str
    path: Path
    # Auditor outputs
    verdict: str | None            # "pass" | "concerns" | "fail" | None (unparseable)
    findings_count: int
    severity_counts: dict[str, int]  # {"high": N, "medium": N, "low": N}
    rules_fired: list[str]           # ["R0", "R3", ...] — preserves firing order
    has_high_finding: bool           # fast flag for flagged-list membership
    schema_version: int | None       # None == pre-schema-version audit
    # Tester-side ground truth (from metadata.json)
    tester: str | None
    tester_status: str | None        # "Pass"/"Fail"/"Passed With Issue"/... or None
    test_case: str | None            # "T44880 — ..." or None
    testrun_name: str | None = None  # e.g. "E2E_Flow - GMA - AU Desktop - Chrome"
    testrun_key: str | None = None   # e.g. "QA-C17608" — used to deep-link Tracker
    marketplace: str | None = None   # e.g. "AU", "DE", "BR" — None when not parseable
    execution_date: str | None = None
    # Raw finding objects — only consumed by the HTML renderer so
    # per-audit expansion can show them inline. Kept as a list of
    # dicts rather than a bespoke type so the shape stays in sync
    # with audit.json automatically.
    findings: list[dict] = dataclasses.field(default_factory=list)
    evidence_by_step: list[dict] = dataclasses.field(default_factory=list)
    # env_check verdict/URLs, or None when audit.json had no env_check
    # field (pre-v4 schema audits).
    env_check: dict | None = None
    # Layer 3 acknowledgments — append-only list of operator actions
    # (dismissals today; future: explicit acks, escalations). Each
    # entry: {"type": str, "by": user, "at": iso, "reason": str|None}.
    # Empty for audits that have never been touched by /argus dismiss.
    acknowledgments: list[dict] = dataclasses.field(default_factory=list)


def _scan_output_dir(
    out_dir: Path,
    variant: str | None = None,
) -> list[AuditSummary]:
    """Build an AuditSummary per audit.json under out_dir.

    variant=None reads canonical audit.json (excludes _replay/);
    variant="vN" reads <exec>/_replay/<variant>/audit.json instead.
    Parse errors are silent so one bad file doesn't kill the report.
    """
    summaries: list[AuditSummary] = []
    if not out_dir.exists():
        return summaries
    if variant is None:
        audit_files = [
            p for p in sorted(out_dir.rglob("audit.json"))
            if "_replay" not in p.parts
        ]
    else:
        # Path shape is <exec_dir>/_replay/<variant>/audit.json, so
        # exec_dir is three levels up from each audit file.
        audit_files = sorted(out_dir.rglob(f"_replay/{variant}/audit.json"))
    for audit_path in audit_files:
        try:
            audit = json.loads(audit_path.read_text())
        except (OSError, json.JSONDecodeError):
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


def _report_infer_finding_category(finding: dict) -> str:
    category = finding.get("category")
    if category in _VALID_FINDING_CATEGORIES:
        return category
    desc = (finding.get("description") or "").lower()
    if any(t in desc for t in _REPORT_INSUFFICIENT_EVIDENCE_TERMS):
        return "insufficient_evidence"
    rule = finding.get("rule")
    source = finding.get("source")
    if source == "env_check" or rule in {"R9", "R13"}:
        return "environment_issue"
    if rule in {"R12", "R15a", "R15b", "R15c"}:
        return "insufficient_evidence"
    if rule in {
        "R0", "R1", "R2", "R4", "R5", "R6", "R7", "R8", "R10",
        "R11", "R14", "R16", "R17", "R18",
    }:
        return "tester_error"
    if any(t in desc for t in _REPORT_ENVIRONMENT_TERMS):
        return "environment_issue"
    if any(t in desc for t in _REPORT_PRODUCT_ISSUE_TERMS):
        return "product_issue"
    return "tester_error"


def _report_infer_finding_action(finding: dict, category: str) -> str:
    action = finding.get("action")
    if action in _VALID_FINDING_ACTIONS:
        return action
    rule = finding.get("rule")
    if category == "insufficient_evidence":
        return "add_evidence"
    if category == "environment_issue":
        return "re_run"
    if category == "product_issue":
        return "attach_defect"
    if category == "policy_exception":
        return "review_manually"
    if rule in {"R2", "R4", "R5", "R6", "R7", "R8", "R16", "R18"}:
        return "fix_status"
    if rule in {"R10", "R11"}:
        return "attach_defect"
    return "review_manually"


def _report_infer_finding_confidence(finding: dict, category: str) -> str:
    confidence = finding.get("confidence")
    if confidence in {"low", "medium", "high"}:
        return confidence
    if finding.get("source") in {"rule", "env_check"}:
        return "high"
    if isinstance(finding.get("page"), int):
        return "high"
    if category == "insufficient_evidence" and isinstance(
        finding.get("step_index"), int
    ):
        return "medium"
    return "low"


def _report_finding_has_unpage_grounded_visual_claim(finding: dict) -> bool:
    if finding.get("source") in {"rule", "env_check"}:
        return False
    if isinstance(finding.get("page"), int):
        return False
    desc = (finding.get("description") or "").lower()
    if any(t in desc for t in _REPORT_INSUFFICIENT_EVIDENCE_TERMS):
        return False
    return any(t in desc for t in (
        _REPORT_VISUAL_CLAIM_TERMS + _REPORT_PRODUCT_ISSUE_TERMS
    ))


def _report_enrich_audit(audit: dict) -> dict:
    """Backfill v6 triage fields without importing the audit runtime.

    report.py must work in lightweight local environments that may not have
    LLM Runtime/boto3 dependencies installed. Keep this fallback aligned with
    auditor.enrich_audit for old audit.json files while preserving the
    original schema_version so stale-audit warnings remain honest.
    """
    enriched = dict(audit)
    findings = []
    for raw in audit.get("findings") or []:
        if not isinstance(raw, dict):
            continue
        f = dict(raw)
        category = _report_infer_finding_category(f)
        f["category"] = category
        f["action"] = _report_infer_finding_action(f, category)
        f["confidence"] = _report_infer_finding_confidence(f, category)
        if (
            f.get("severity") == "high"
            and category != "insufficient_evidence"
            and _report_finding_has_unpage_grounded_visual_claim(f)
        ):
            f["_severity_adjusted_from"] = "high"
            f["severity"] = "medium"
            f["confidence"] = "low"
            f["action"] = "review_manually"
        findings.append(f)
    enriched["findings"] = findings
    return enriched


def _build_summary(audit: dict, metadata: dict,
                   exec_dir: Path) -> AuditSummary:
    try:
        import auditor as _auditor
        audit = _auditor.enrich_audit(audit, metadata)
    except Exception:
        # Reports must remain readable even if the audit pipeline module
        # cannot be imported in a constrained environment.
        audit = _report_enrich_audit(audit)
    findings = audit.get("findings") or []
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

    # Tester side.
    tr_list = metadata.get("test_results") or []
    tr = tr_list[0] if tr_list else {}
    tester = tr.get("executed_by_name") or tr.get("executed_by")
    tester_status = tr.get("status")
    tc_key = tr.get("test_case_key")
    tc_name = tr.get("test_case_name")
    if tc_key and tc_name:
        test_case = f"{tc_key} — {tc_name}"
    else:
        test_case = tc_key or tc_name

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
        findings=findings,
        evidence_by_step=audit.get("evidence_by_step") or [],
        env_check=audit.get("env_check"),
        acknowledgments=audit.get("acknowledgments") or [],
    )


# Analysis helpers (pure, testable without fixtures)
def _is_flagged(s: AuditSummary) -> bool:
    """An audit is flagged if it warrants a human eyeball.

    Criteria (any one):
      - verdict is "fail"
      - any high-severity finding (model or rule)
      - any structural rule fired (R0/R2/R4/R8/R9)

    Layer 3 short-circuit: a "dismissed" acknowledgment overrides the
    flag entirely. The operator already eyeballed it and decided it
    was a false positive — pulling it back into the flagged list on
    the next dashboard render would defeat the dismiss command. The
    findings stay in audit.json (auditor logic isn't mutated); only
    the *flag* derived from them is suppressed. Other ack types
    (e.g. "acknowledged" if added later) do NOT suppress — only
    explicit dismissal does.
    """
    if any(a.get("type") == "dismissed" for a in s.acknowledgments):
        return False
    if s.verdict == "fail":
        return True
    if s.has_high_finding:
        return True
    if any(r in _STRUCTURAL_RULES for r in s.rules_fired):
        return True
    return False


def _is_divergent(s: AuditSummary) -> bool:
    """Tester claimed clean Pass, auditor disagreed.

    The money question for compliance review: executions where the tester
    marked the overall test "Pass" but the auditor surfaced any concern.
    Not a policy matter — it's the signal that the tester either didn't
    read the screenshots or isn't documenting problems they saw.
    """
    return s.tester_status == "Pass" and s.verdict in {"concerns", "fail"}


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


def _finding_actions_for_summary(s: AuditSummary) -> set[str]:
    return {_finding_action(f) for f in s.findings if isinstance(f, dict)}


def _finding_categories_for_summary(s: AuditSummary) -> set[str]:
    return {_finding_category(f) for f in s.findings if isinstance(f, dict)}


def _action_counter(summaries: Iterable[AuditSummary]) -> collections.Counter:
    counter: collections.Counter = collections.Counter()
    for s in summaries:
        for f in s.findings:
            if isinstance(f, dict):
                counter[_finding_action(f)] += 1
    return counter


def _category_counter(summaries: Iterable[AuditSummary]) -> collections.Counter:
    counter: collections.Counter = collections.Counter()
    for s in summaries:
        for f in s.findings:
            if isinstance(f, dict):
                counter[_finding_category(f)] += 1
    return counter


# Rendering
def generate_report(out_dir: Path, variant: str | None = None) -> str:
    """Scan `out_dir` and return the full markdown report as a string.

    When `variant` is set, reads replay audits for that variant
    (e.g. v3, v4) instead of the canonical audit.json files. Use for
    variant-aware compliance reports without overwriting the canonical
    report.
    """
    summaries = _scan_output_dir(out_dir, variant=variant)
    parts = [
        _render_header(out_dir, summaries, variant=variant),
        _render_action_queue(summaries),
        _render_divergence(summaries),
        _render_flagged(summaries),
        _render_rule_distribution(summaries),
        # R15 coverage gaps: per-folder, sourced from _coverage.json
        # (separate from audit.json — the gaps describe missing
        # executions, not findings on existing audits).
        _render_coverage(out_dir),
        _render_per_tester(summaries, out_dir),
        _render_stale(summaries),
        _render_failed_keys(out_dir),
    ]
    # Sections can opt out by returning "" — filter those out so the
    # rendered report is compact on a small corpus.
    return "\n\n".join(p for p in parts if p).rstrip() + "\n"


def _render_header(
    out_dir: Path,
    summaries: list[AuditSummary],
    variant: str | None = None,
) -> str:
    total = len(summaries)
    # Variant label for the report title — blank when reading canonical
    # audits, `(variant v4)` etc when reading replay outputs. Keeps the
    # header honest so readers know which prompt/ruleset generation the
    # numbers below come from.
    variant_label = f" (variant {variant})" if variant else ""
    if total == 0:
        return (
            f"# Tester-compliance report{variant_label}\n\n"
            f"Scanned `{out_dir}` — no audits found.\n"
        )
    verdict_counts = collections.Counter(s.verdict or "unknown"
                                         for s in summaries)
    testers = {s.tester for s in summaries if s.tester}
    sev_total = collections.Counter()
    for s in summaries:
        sev_total.update(s.severity_counts)
    rule_hits = sum(len(s.rules_fired) for s in summaries)
    divergent = sum(1 for s in summaries if _is_divergent(s))
    flagged = sum(1 for s in summaries if _is_flagged(s))

    lines = [
        f"# Tester-compliance report{variant_label}",
        "",
        f"- **Scanned directory:** `{out_dir}`",
        f"- **Variant:** {variant or 'canonical'}",
        f"- **Audits scanned:** {total}",
        f"- **Unique testers:** {len(testers)}",
        f"- **Verdicts:** "
        + ", ".join(f"{k}={verdict_counts[k]}"
                    for k in ("pass", "concerns", "fail", "unknown")
                    if verdict_counts[k]),
        f"- **Total findings:** {sum(sev_total.values())} "
        f"(high={sev_total['high']}, medium={sev_total['medium']}, "
        f"low={sev_total['low']})",
        f"- **Rule-sourced findings:** {rule_hits}",
        f"- **Flagged for review:** {flagged} "
        f"({_pct(flagged, total)}%)",
        f"- **Tester-Pass vs Auditor-Concerns divergence:** {divergent} "
        f"({_pct(divergent, total)}%)",
    ]
    return "\n".join(lines)


def _render_action_queue(summaries: list[AuditSummary]) -> str:
    if not summaries:
        return ""
    actions = _action_counter(summaries)
    if not actions:
        return ""
    categories = _category_counter(summaries)
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
    """Per-tester Pass-vs-Concerns divergence table.

    The most compliance-relevant view. Shows each tester's divergence
    rate (tester said Pass, auditor said not-pass) sorted worst-first
    so the QA lead sees who needs coaching at a glance.
    """
    if not summaries:
        return ""
    by_tester: dict[str, list[AuditSummary]] = collections.defaultdict(list)
    for s in summaries:
        by_tester[s.tester or "unknown"].append(s)

    rows: list[tuple[str, int, int, int]] = []
    for name, group in by_tester.items():
        total = len(group)
        pass_claimed = sum(1 for s in group if s.tester_status == "Pass")
        divergent = sum(1 for s in group if _is_divergent(s))
        rows.append((name, total, pass_claimed, divergent))

    # Sort by divergence rate descending, then by absolute count, then
    # by name. A tester with 5/5 divergence ranks above one with 10/20
    # even though both are 100%/50%; we want worst-rate-worst-behaved
    # first, with raw count as the tiebreak.
    def _sort_key(row: tuple[str, int, int, int]) -> tuple[float, int, str]:
        name, total, _, divergent = row
        rate = divergent / total if total else 0
        return (-rate, -divergent, name.lower())

    rows.sort(key=_sort_key)

    lines = [
        "## Tester-Pass vs Auditor-Concerns divergence",
        "",
        "Executions where the tester marked the overall test **Pass** but "
        "the auditor returned **concerns** or **fail**. This is the "
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
            "_None — no audits have fail verdict, high-severity findings, "
            "or structural rule firings (R0/R2/R4/R8/R9)._"
        )
    # Order: highest severity first, then by key for stability.
    def _order(s: AuditSummary) -> tuple[int, str]:
        rank = (
            0 if s.verdict == "fail"
            else 1 if s.has_high_finding
            else 2  # structural-rule-only
        )
        return (rank, s.key)

    lines = [
        "## Flagged for review",
        "",
        f"{len(flagged)} execution(s) need a human eyeball. Criteria: "
        "`fail` verdict, any high-severity finding, or any structural "
        "rule fire (R0 — rule engine degraded; R2 — overall Pass with "
        "failing step; R4 — overall Fail with no failing step; "
        "R8 — overall Blocked with no Blocked step; "
        "R9 — prod URL detected by deterministic OCR env check).",
        "",
    ]
    for s in sorted(flagged, key=_order):
        reason_parts = []
        if s.verdict == "fail":
            reason_parts.append("verdict=fail")
        if s.has_high_finding:
            n_high = s.severity_counts.get("high", 0)
            reason_parts.append(f"{n_high} high-severity finding(s)")
        struct_rules = [r for r in s.rules_fired if r in _STRUCTURAL_RULES]
        if struct_rules:
            reason_parts.append("rules=" + ",".join(sorted(set(struct_rules))))
        tester = s.tester or "unknown"
        tc = s.test_case or "(unknown test case)"
        reason = "; ".join(reason_parts) or "flagged"
        lines.append(f"- **{s.key}** [{tester}] — {reason}")
        lines.append(f"    - Test case: {tc}")
        lines.append(f"    - Tester status: {s.tester_status or '?'} → "
                     f"Auditor: {s.verdict or '?'}")
        lines.append(f"    - Path: `{s.path}`")
    return "\n".join(lines)


def _render_rule_distribution(summaries: list[AuditSummary]) -> str:
    rule_hits = collections.Counter()
    for s in summaries:
        rule_hits.update(s.rules_fired)
    if not rule_hits:
        return ""  # omit section when no rules fired anywhere

    # Canonical rule ordering for readability — unknown rules at end.
    # R3 absent from ordering: deprecated per policy 2026-05-09. If a
    # stale audit still carries R3 findings they'll fall into the
    # unknown_hits bucket below and render after the known rules, so
    # historical data isn't silently dropped.
    known = ["R0", "R1", "R2", "R4", "R5", "R6", "R7", "R8", "R9",
             "R9-amb", "R9-hover", "R10", "R11", "R12", "R13"]
    known_hits = [(r, rule_hits[r]) for r in known if rule_hits[r]]
    unknown_hits = [(r, n) for r, n in rule_hits.items() if r not in known]
    ordered = known_hits + sorted(unknown_hits)

    descriptions = {
        "R0": "rule-engine degraded (status map didn't resolve)",
        "R1": "step marked 'In Progress' with no explanatory comment",
        "R2": "overall Pass but a step is Fail (contradiction)",
        "R3": "DEPRECATED (historical): overall Pass + Blocked step",
        "R4": "overall Fail but no Fail/Blocked step (contradiction)",
        "R5": "Pass step with issue-language in tester comment",
        "R6": "Passed With Issue overall without comment or trace link",
        "R7": "Passed With Issue overall but no step is Fail",
        "R8": "overall Blocked but no step is Blocked (contradiction)",
        "R9": "prod URL detected in URL bar (deterministic env_check)",
        "R9-amb": "Webview folder with 0 exampleapp URLs detected "
                  "(env_check ambiguous — cannot verify env compliance)",
        "R9-hover": "page-body / status-bar hover region contains "
                    "hardcoded prod URL — product issue, tester "
                    "is not at fault",
        "R10": "defect ref pasted in step comment instead of "
               "attached as trace_link",
        "R11": "Fail or Passed-With-Issue step has neither trace_links "
               "nor a defect ref in the comment (undocumented issue)",
        "R12": "Tester submitted zero evidence — no PDF, no screenshots, "
               "nothing the audit can verify against",
        "R13": "Marketplace mismatch — assigned MP doesn't match the "
               ".exampleapp.<tld> seen in screenshots",
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
        "| Rule | Fires | Description |",
        "| --- | ---: | --- |",
    ]
    for rule, n in ordered:
        desc = descriptions.get(rule, "(unknown rule)")
        lines.append(f"| {rule} | {n} | {desc} |")
    return "\n".join(lines)


def _audit_join_key(s: "AuditSummary") -> tuple:
    """Join key linking an AuditSummary to a roster execution row.

    A test case can be executed in several marketplaces / testruns, so
    the TC key alone isn't unique — we key on (test_case_key, testrun_key,
    marketplace). AuditSummary.test_case is "<key> — <name>"; take the
    key prefix.
    """
    tc = (s.test_case or "").split(" — ", 1)[0].strip() or None
    return (tc, s.testrun_key, s.marketplace)


def _render_per_tester(summaries: list["AuditSummary"],
                       out_dir: Path) -> str:
    """Per-tester EXECUTION roster (every execution, audited or not).

    Built from `_coverage.json`'s `executions` roster (source of truth =
    who actually ran each case) LEFT-JOINED with the audited summaries
    (which add the auditor verdict). This is intentionally broader than
    the old audited-only rollup: a tester who executed cases that were
    never audited (failed the PASS/FAIL gate historically, or skipped for
    no attachments) now appears with those executions flagged
    `audited=no`. Testers with zero audits still appear.

    Falls back to the legacy audited-only rollup when the roster is
    absent (older `_coverage.json`, or folders that never ran R15) so old
    folders keep rendering.
    """
    coverage = _load_coverage_json(out_dir)
    roster = (coverage or {}).get("executions")
    if not roster:
        return _render_per_tester_legacy(summaries)

    # Index audited executions by join key for the left join.
    audited_by_key: dict[tuple, "AuditSummary"] = {}
    for s in summaries:
        # On a key collision (same TC/MP/run audited twice as separate
        # exec dirs) keep the most recent execution_date.
        jk = _audit_join_key(s)
        prev = audited_by_key.get(jk)
        if prev is None or (s.execution_date or "") >= (prev.execution_date or ""):
            audited_by_key[jk] = s

    # Group roster rows by RESOLVED executor label (heals stale
    # USER... keys + merges with any display-name buckets).
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
    # Worst-first: most not-audited executions surface at the top so a
    # manager sees the biggest audit gaps immediately.
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
        audited_n = sum(1 for _, a in joined if a is not None)
        verdicts = collections.Counter(
            a.verdict or "unknown" for _, a in joined if a is not None)
        v_parts = [f"{k}={verdicts[k]}" for k in
                   ("pass", "concerns", "fail", "unknown") if verdicts[k]]
        lines.append(f"### {name}")
        lines.append("")
        lines.append(
            f"- Executions: {total} "
            f"(audited {audited_n}, not audited {total - audited_n})")
        lines.append(
            f"- Verdicts (audited): {', '.join(v_parts) or '(none audited)'}")
        lines.append("")
        lines.append("| Test case | MP | Status | Audited | Verdict |")
        lines.append("| --- | --- | --- | --- | --- |")
        # Sort rows: not-audited first, then by test case key.
        for row, audit in sorted(
                joined,
                key=lambda ra: (ra[1] is not None,
                                ra[0].get("test_case_key") or "")):
            tc = row.get("test_case_key") or "—"
            mp = row.get("marketplace") or "—"
            status = row.get("status") or "—"
            is_aud = "yes" if audit is not None else "no"
            verdict = (audit.verdict or "—") if audit is not None else "—"
            # Surface assignee≠executor divergence inline — a manager
            # signal that planning and execution drifted.
            asg = row.get("assigned_to_name") or row.get("assigned_to")
            exe = row.get("executed_by_name") or row.get("executed_by")
            mp_cell = mp
            if asg and exe and asg != exe:
                mp_cell = f"{mp} ⚠️(assigned: {asg})"
            lines.append(
                f"| {tc} | {mp_cell} | {status} | {is_aud} | {verdict} |")
        lines.append("")
    return "\n".join(lines).rstrip()


def _audit_join_key_from_row(row: dict) -> tuple:
    """Roster-row side of the join key — mirrors _audit_join_key."""
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

    # Sort alphabetically for this section — the divergence section
    # already does the worst-first ranking.
    lines = [
        "## Per-tester audit summary",
        "",
    ]
    for name in sorted(by_tester.keys(), key=str.lower):
        group = by_tester[name]
        total = len(group)
        verdicts = collections.Counter(s.verdict or "unknown" for s in group)
        sev = collections.Counter()
        rules = collections.Counter()
        for s in group:
            sev.update(s.severity_counts)
            rules.update(s.rules_fired)
        top_rules = ", ".join(f"{r}×{n}" for r, n in rules.most_common(3)) \
            or "(none)"
        v_parts = [f"{k}={verdicts[k]}" for k in
                   ("pass", "concerns", "fail", "unknown") if verdicts[k]]
        sev_parts = [f"{k}={sev[k]}" for k in
                     ("high", "medium", "low") if sev[k]]
        lines.append(f"### {name}")
        lines.append("")
        lines.append(f"- Audits: {total}")
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


def _render_failed_keys(out_dir: Path) -> str:
    """Section for keys in `_failed_keys.txt` that never produced an audit.

    If _last_error.json is present for any of them, inline the captured
    exception class + message so the report is one-stop — no need to
    open the file.
    """
    failed_path = out_dir / "_failed_keys.txt"
    if not failed_path.exists():
        return ""
    keys = [line.strip() for line in failed_path.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")]
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
    for key in keys:
        err_summary = _find_last_error(out_dir, key)
        if err_summary:
            lines.append(f"- **{key}** — {err_summary}")
        else:
            lines.append(f"- **{key}** — (no _last_error.json found)")
    return "\n".join(lines)


def _load_coverage_json(out_dir: Path) -> dict | None:
    """Read `<out_dir>/_coverage.json` if present and well-formed.

    Returns None when the file is missing, unreadable, or has the wrong
    schema — coverage rendering then opts out cleanly (returns "")
    instead of breaking the whole report.
    """
    path = out_dir / "_coverage.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("schema") != 1:
        return None
    return data


def _render_coverage(out_dir: Path) -> str:
    """Markdown render of `_coverage.json` (R15 a/b/c findings).

    Returns "" when no _coverage.json is present (older folders, or
    runs that disabled R15) so the section silently opts out.

    Layout follows the existing rendering style: one header line +
    short context paragraph + per-rule subsections. R15a is grouped by
    tester (largest groups first) so the operator immediately sees who
    owes the most missing executions; R15b is a compact table; R15c is
    a tester-rate table.
    """
    data = _load_coverage_json(out_dir)
    if data is None:
        return ""
    findings = data.get("findings") or []
    items_total = int(data.get("items_total") or 0)
    items_executed = int(data.get("items_executed") or 0)
    items_unexecuted = int(data.get("items_unexecuted") or 0)
    pct = _pct(items_executed, items_total) if items_total else 0

    # No findings AND nothing scanned: omit the section entirely. Empty
    # findings on a real folder still renders so the operator sees the
    # "0 gaps" reassurance.
    if items_total == 0 and not findings:
        return ""

    lines = [
        "## Coverage gaps (R15)",
        "",
        f"{items_total} item(s) planned, {items_executed} executed "
        f"({pct}%), {items_unexecuted} unexecuted.",
        "",
    ]

    r15a = [f for f in findings if f.get("rule") == "R15a"]
    r15b = [f for f in findings if f.get("rule") == "R15b"]
    r15c = [f for f in findings if f.get("rule") == "R15c"]

    if not findings:
        lines.append("_No coverage gaps detected._")
        return "\n".join(lines)

    # --- R15a: planned-but-not-executed, grouped by tester. -------------
    if r15a:
        lines.append("### R15a — Planned but not executed")
        lines.append("")
        # Group by RESOLVED tester label (heals stale USER... entries
        # captured during a 429 burst — see _resolve_tester_label).
        by_tester: dict[str, list[dict]] = collections.defaultdict(list)
        for f in r15a:
            by_tester[_resolve_tester_label(f.get("tester"))].append(f)
        ranked = sorted(by_tester.items(),
                        key=lambda kv: (-len(kv[1]), kv[0].lower()))
        for tester, group in ranked:
            tcs = ", ".join(sorted({
                # Show TC + MP suffix so two MPs of the same TC don't
                # collapse to one entry.
                f"{f.get('test_case_key', '?')}"
                + (f"/{f.get('marketplace')}" if f.get("marketplace") else "")
                for f in group
            }))
            lines.append(f"- **{tester}** — {len(group)} skipped: {tcs}")
        lines.append("")

    # --- R15b: marketplace coverage holes, table form. ------------------
    if r15b:
        lines.append("### R15b — Marketplace coverage holes")
        lines.append("")
        lines.append("| Test case | Missing marketplaces |")
        lines.append("| --- | --- |")
        for f in sorted(r15b, key=lambda f: f.get("test_case_key", "")):
            tc = f.get("test_case_key") or "(unknown)"
            missing = f.get("marketplace") or ""
            lines.append(f"| {tc} | {missing} |")
        lines.append("")

    # --- R15c: tester abandonment, percentage table. --------------------
    if r15c:
        lines.append("### R15c — Tester abandonment")
        lines.append("")
        lines.append("| Tester | Status |")
        lines.append("| --- | --- |")
        for f in sorted(r15c, key=lambda f: f.get("tester") or ""):
            tester = f.get("tester") or "(unknown)"
            # The description carries the assigned/executed/% triplet
            # in stable form ("R15c: <name> assigned N item(s) ...").
            # Strip the leading "R15c: " for the table cell.
            desc = (f.get("description") or "").removeprefix("R15c: ")
            lines.append(f"| {tester} | {desc} |")

    return "\n".join(lines).rstrip()


_JIRA_TESTS_BASE = "https://tracker.exampleapp.com/secure/Tests.jspa"


def _resolve_tester_label(tester: str | None) -> str:
    """Return the best human label for a tester field captured at
    coverage-write time.

    A `_coverage.json` entry can carry a raw `USER12345` string in
    its `tester` field when the user-key resolution failed at that
    moment (e.g. a Tracker 429 burst poisoned the cache). The cache
    self-heals on the next successful lookup, but the JSON file on disk
    keeps the stale value. By looking up the persistent users cache
    AT RENDER TIME we automatically replace any since-resolved
    USER... strings with the real display name, without rewriting
    the source file.

    Falls back to the original string when:
      - the cache module isn't importable (defensive: report.py must
        not break when run standalone)
      - the key isn't a USER pattern (already a real name)
      - the cache also has no resolution (still failed; show raw key
        so the operator sees there's something to investigate)
    """
    if not tester:
        return "(unassigned)"
    if not tester.startswith("USER"):
        return tester
    try:
        import users
        resolved = users.get_default_cache().get_resolved(tester)
        if resolved:
            return resolved
    except Exception:
        pass
    return tester


def _tracker_browse_anchor(key: str) -> str:
    """Wrap a QA-T... or QA-E... key in a clickable Tracker link.

    test-management Scale uses fragment-based deep links rooted at
    /secure/Tests.jspa, NOT the standard /browse/<KEY> path:

        QA-T...  → /secure/Tests.jspa#/testCase/QA-T...
        QA-E...  → /secure/Tests.jspa#/testPlayer/testExecution/QA-E...
        QA-C...  → /secure/Tests.jspa#/testPlayer/QA-C...

    /browse/<KEY> works for ordinary Tracker issues (BUG keys), but
    test-case / execution / testrun keys are rendered by the test-management
    plugin at the fragments above. Routing the wrong key shape
    silently 404s in test-management's UI.

    Opens in a new tab to keep the operator's place in ARGUS, with
    rel="noopener" to prevent window.opener leakage.
    """
    if not key:
        return ""
    safe = _html_escape(key)
    if "-T" in key:
        url = f"{_JIRA_TESTS_BASE}#/testCase/{safe}"
    elif "-E" in key:
        # Execution keys route through testExecution/, e.g.
        # /secure/Tests.jspa#/testPlayer/testExecution/QA-E185534
        url = f"{_JIRA_TESTS_BASE}#/testPlayer/testExecution/{safe}"
    elif "-C" in key:
        # Testrun keys route straight to the player.
        url = f"{_JIRA_TESTS_BASE}#/testPlayer/{safe}"
    else:
        # Fallback for anything else (bug keys, etc.) — standard browse.
        url = f"https://tracker.exampleapp.com/browse/{safe}"
    return (
        f'<a href="{url}" '
        f'target="_blank" rel="noopener" '
        f'title="Open {safe} in Tracker">{safe}</a>'
    )


def _argus_tester_cell(tester_name: str) -> str:
    """Tester name as a click-to-filter link (reuses argus-tester-link JS).

    Clicking sets the tester dropdown to this name and filters the table
    to all their executions — the same behaviour as the summary panel's
    tester links.
    """
    safe = _html_escape(tester_name)
    return (f'<button type="button" class="argus-tester-link" '
            f'data-tester="{safe}" '
            f'title="Show all executions by {safe}">{safe}</button>')


def _argus_status_pill(status: str) -> str:
    """Colored pill for a test-management execution status.

    Color spec (operator request): Blocked=blue, In Progress=dark
    yellow, Retest=orange. Pass/Fail/Passed-With-Issue reuse the page's
    semantic verdict palette. Unknown statuses fall back to neutral.
    """
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
    """Link an execution key (QA-E...) to its test-management Test Player view.

    Clicking the execution ID in the audits table opens the live run at
    /secure/Tests.jspa#/testPlayer/testExecution/<E-KEY>. Falls back to
    plain text for non-execution keys. Opens in a new tab; the row's
    click-to-expand handler ignores clicks that land on an <a>.
    """
    if not key:
        return ""
    safe = _html_escape(key)
    if "-E" not in key:
        return safe
    url = f"{_JIRA_TESTS_BASE}#/testPlayer/testExecution/{safe}"
    return (
        f'<a href="{url}" target="_blank" rel="noopener" '
        f'class="argus-key-link" '
        f'title="Open execution {safe} in Tracker Test Player">{safe}</a>'
    )


def _render_coverage_html(out_dir: Path) -> str:
    """HTML render of `_coverage.json` as a slide-in drawer.

    Renders TWO pieces stitched together by the caller (or used
    directly via _splice_coverage_drawer): a small header pill that
    operators click to open the drawer, and the drawer itself
    (off-screen until opened, holding R15a/b/c tabs).

    Drawer keeps the data dense + scannable while not stealing scroll
    space in the main report. Closes via the X button, ESC key, or
    clicking the backdrop. JS gated by the trigger element existing,
    so a no-JS user sees the drawer always-open via CSS fallback.

    Returns "" when no _coverage.json is present.
    """
    data = _load_coverage_json(out_dir)
    if data is None:
        return ""
    findings = data.get("findings") or []
    items_total = int(data.get("items_total") or 0)
    items_executed = int(data.get("items_executed") or 0)
    items_unexecuted = int(data.get("items_unexecuted") or 0)
    pct = _pct(items_executed, items_total) if items_total else 0

    # Same opt-out as markdown: nothing scanned AND no findings means
    # no-op (e.g. running --html on an old folder predating R15).
    if items_total == 0 and not findings:
        return ""

    r15a = [f for f in findings if f.get("rule") == "R15a"]
    r15b = [f for f in findings if f.get("rule") == "R15b"]
    r15c = [f for f in findings if f.get("rule") == "R15c"]

    # Trigger pill — sticks in the header row. Number is the total
    # finding count so a glance tells the operator how much pent-up
    # gap there is. Click opens the drawer.
    total_findings = len(findings)
    trigger = (
        f'<button type="button" class="argus-coverage-trigger" '
        f'id="argus-coverage-open" '
        f'aria-controls="argus-coverage-drawer" '
        f'aria-expanded="false" '
        f'title="Open coverage gaps panel">'
        f'<span class="argus-coverage-trigger-icon" aria-hidden="true">⚠</span>'
        f'Coverage gaps '
        f'<span class="argus-coverage-trigger-count">{total_findings}</span>'
        f'</button>'
    )

    parts = [
        # Drawer backdrop — clicking dismisses. Hidden by default; JS
        # un-hides when the trigger is clicked.
        '<div class="argus-coverage-backdrop" '
        'id="argus-coverage-backdrop" hidden></div>',
        # Drawer itself — fixed-position slide-in panel from the right.
        # `aria-hidden` toggled by JS; the [hidden] attribute is NOT
        # used here so the CSS transform animation can play out.
        '<aside class="argus-coverage-drawer" '
        'id="argus-coverage-drawer" '
        'role="dialog" aria-modal="true" '
        'aria-labelledby="argus-coverage-drawer-title" '
        'aria-hidden="true">',
        '<div class="argus-coverage-drawer-header">',
        '<h2 id="argus-coverage-drawer-title">Coverage gaps (R15)</h2>',
        '<button type="button" class="argus-coverage-close" '
        'id="argus-coverage-close" '
        'aria-label="Close coverage panel">×</button>',
        '</div>',
        '<div class="argus-coverage-drawer-sub">',
        f'{items_total} item(s) planned · {items_executed} executed '
        f'({pct}%) · <strong>{items_unexecuted} unexecuted</strong>',
        '</div>',
    ]

    if not findings:
        parts.append(
            '<p class="argus-coverage-clean">'
            'No coverage gaps detected.</p>')
        parts.append('</aside>')
        # Pair the empty drawer with a no-finding trigger so the operator
        # still sees that coverage was checked and came up clean.
        return trigger + "".join(parts)

    # Build each rule's panel content first; the tab strip below
    # references which ones are non-empty and uses that to decide which
    # tab to default-select.

    # --- R15a panel: grouped-by-tester list -----------------------------
    r15a_panel = ""
    if r15a:
        # Bucket findings by the *resolved* tester label, not the raw
        # field captured at coverage-write time. _resolve_tester_label
        # heals stale USER... strings via the persistent users
        # cache so two findings for the same human (one at write time
        # = "Sudar Manikandan E", another = "USER24822" because
        # of a 429 burst) merge into a single bucket on render.
        by_tester: dict[str, list[dict]] = collections.defaultdict(list)
        for f in r15a:
            label = _resolve_tester_label(f.get("tester"))
            by_tester[label].append(f)
        ranked = sorted(by_tester.items(),
                        key=lambda kv: (-len(kv[1]), kv[0].lower()))
        rows = []
        for tester, group in ranked:
            # Build a list of clickable Tracker links per test case + an
            # optional "/MP" suffix for the marketplace context. Sort
            # for deterministic output across re-renders. Each QA-T
            # key is its own anchor so an operator can click straight
            # into Tracker; the "/MP" suffix is plain text since markets
            # aren't navigable resources.
            seen: list[str] = []
            for f in group:
                tc = f.get("test_case_key") or ""
                mp = f.get("marketplace")
                rendered = _tracker_browse_anchor(tc) if tc else "?"
                if mp:
                    rendered += _html_escape(f"/{mp}")
                seen.append(rendered)
            # De-dupe identical (tc, mp) pairs while preserving order.
            seen_unique = list(dict.fromkeys(seen))
            tcs_html = ", ".join(seen_unique)
            rows.append(
                f'<li><strong>{_html_escape(tester)}</strong> '
                f'<span class="argus-muted">'
                f'({len(group)} skipped)</span>: '
                f'{tcs_html}</li>'
            )
        r15a_panel = (
            '<ul class="argus-coverage-list">'
            + "".join(rows)
            + '</ul>'
        )

    # --- R15b panel: marketplace-coverage-hole table --------------------
    r15b_panel = ""
    if r15b:
        rows = []
        for f in sorted(r15b, key=lambda f: f.get("test_case_key", "")):
            tc = f.get("test_case_key") or ""
            tc_html = _tracker_browse_anchor(tc) if tc else "(unknown)"
            missing = f.get("marketplace") or ""
            rows.append(
                f'<tr><td>{tc_html}</td>'
                f'<td>{_html_escape(missing)}</td></tr>'
            )
        r15b_panel = (
            '<table class="argus-coverage-table">'
            '<thead><tr><th>Test case</th><th>Missing marketplaces</th></tr></thead>'
            '<tbody>' + "".join(rows) + '</tbody>'
            '</table>'
        )

    # --- R15c panel: tester abandonment table ---------------------------
    r15c_panel = ""
    if r15c:
        rows = []
        for f in sorted(r15c, key=lambda f: f.get("tester") or ""):
            tester = f.get("tester") or "(unknown)"
            desc = (f.get("description") or "").removeprefix("R15c: ")
            rows.append(
                f'<tr><td>{_html_escape(tester)}</td>'
                f'<td>{_html_escape(desc)}</td></tr>'
            )
        r15c_panel = (
            '<table class="argus-coverage-table">'
            '<thead><tr><th>Tester</th><th>Status</th></tr></thead>'
            '<tbody>' + "".join(rows) + '</tbody>'
            '</table>'
        )

    # Build the tab strip + tabpanels. Each panel is always rendered
    # (with `hidden` when not active) so a no-JS user can still see
    # everything by un-hiding panels via DevTools — and screen-readers
    # see the full content via the ARIA roles.
    #
    # The first non-empty rule's tab is selected by default. Tabs for
    # rules that produced zero findings are skipped entirely so the
    # operator never clicks an empty tab.
    tabs_meta = [
        ("R15a", "Planned but not executed", len(r15a), r15a_panel),
        ("R15b", "Marketplace coverage holes", len(r15b), r15b_panel),
        ("R15c", "Tester abandonment", len(r15c), r15c_panel),
    ]
    tabs_meta = [(rule, label, n, panel)
                 for rule, label, n, panel in tabs_meta
                 if n > 0 and panel]
    if tabs_meta:
        # `selected` is a one-shot flag flipped True after the first tab
        # is emitted, so exactly one tab gets aria-selected="true".
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
                f'{rule} <span class="argus-muted">'
                f'({n})</span> · {_html_escape(label)}'
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
            'aria-label="Coverage gap rules">'
            + "".join(tab_buttons)
            + '</div>'
        )
        parts.extend(tab_panels)

    parts.append('</aside>')
    # Trigger pill goes BEFORE the drawer in DOM order so the trigger
    # renders next to the brand in the header row regardless of the
    # drawer's z-index. The drawer + backdrop are positioned fixed so
    # their DOM location doesn't affect layout.
    return trigger + "".join(parts)


def _find_last_error(out_dir: Path, key: str) -> str | None:
    """Return a one-line error summary from _last_error.json for `key`.

    Walks the tree because the key's full path varies with testrun and
    tester slugs (`output/<testrun>/<tester>/<key>/_last_error.json`).
    """
    for candidate in out_dir.rglob(f"{key}/_last_error.json"):
        try:
            err = json.loads(candidate.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if "exception_class" in err:
            return (f"{err['exception_class']}: "
                    f"{err.get('exception_message', '')}"
                    f" ({candidate})")
        if "failure_reason" in err:
            return f"{err['failure_reason']} ({candidate})"
        return f"error recorded at {candidate}"
    return None


# Small utilities
def _pct(n: int, total: int) -> int:
    """Integer percent for headline-readable output. 0 when total is 0."""
    if total <= 0:
        return 0
    return round(100 * n / total)


# CLI
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
    out_dir = args.out_dir or settings.extractor.out_dir
    report = generate_report(out_dir, variant=args.variant)

    if not args.no_report_file and not args.no_markdown:
        out_dir.mkdir(parents=True, exist_ok=True)
        # Timestamp the filename so re-running doesn't clobber prior
        # reports — useful when iterating on a folder over multiple days
        # or comparing cron runs. Variant suffix (e.g. _v4) preserved
        # so canonical and per-variant reports stay distinct.
        import datetime as _dt
        stamp = _dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        variant_suffix = f"_{args.variant}" if args.variant else ""
        filename = f"_report{variant_suffix}_{stamp}.md"
        report_path = out_dir / filename
        report_path.write_text(report)
        # Single stderr line so the user sees where the artefact landed
        # without polluting stdout when --stdout is also on.
        print(f"[argus] wrote {report_path}", file=sys.stderr)

    if args.html and not args.no_report_file:
        out_dir.mkdir(parents=True, exist_ok=True)
        import datetime as _dt2
        stamp = _dt2.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        variant_suffix = f"_{args.variant}" if args.variant else ""
        # Canonical: `_argus.html` (or `_argus_<variant>.html`) — stable
        # path so bookmarks survive re-runs. Plus a timestamped archive.
        html_canonical = out_dir / f"_argus{variant_suffix}.html"
        html_archive = out_dir / f"_argus{variant_suffix}_{stamp}.html"
        html_text = generate_html_report(out_dir, variant=args.variant)
        html_canonical.write_text(html_text)
        html_archive.write_text(html_text)
        print(f"[ARGUS] open: file://{html_canonical.resolve()}",
              file=sys.stderr)
        print(f"[ARGUS] archive: {html_archive.name}", file=sys.stderr)

    if args.stdout:
        sys.stdout.write(report)

    return 0



# HTML renderer
# Self-contained explorable report: one HTML file, inline CSS + vanilla JS,
# no external dependencies. Each audit row expands in place to show its
# findings, env_check URLs, and a link to the canonical audit.md on disk.
# Client-side filter (free-text search + verdict/tester dropdowns + boolean
# toggles for high-severity / flagged-only) and column sort.
#
# Design constraints:
#   * No JS frameworks — ships as one static file, viewable in any modern
#     browser without network access.
#   * Safe against XSS — all user-sourced strings (tester names, finding
#     descriptions, test-case titles) pass through _html_escape before
#     being embedded. audit-path strings also escaped so a malicious
#     directory name can't break out of href="".
#   * Stable under empty inputs — renders an empty-state message
#     instead of breaking if no audits were scanned.
#   * Compatible with the same generate_report flow so CLI adds a
#     single flag (--html) and produces the .html side-by-side with .md.

import html as _html_lib
import json as _json_lib
import datetime as _dt


def _html_escape(s: object) -> str:
    """Escape for HTML text content. None renders as empty string."""
    if s is None:
        return ""
    return _html_lib.escape(str(s), quote=True)


# Filename shape written by extractor._step_attachment_path:
#   step_NNN_<id>_<origname>    -> step-attributed attachment
#   top_<id>_<origname>         -> top-level (no specific step)
_STEP_FILENAME_RE = re.compile(r"^step_(\d+)_")


def _resolve_page_images(exec_dir: Path) -> list[Path]:
    """Return the flattened, sorted list of PDF-split page images for
    one execution.

    Must match ``auditor.load_page_images`` exactly so a finding's
    ``page`` field (1-indexed) lines up with the right image here.
    Specifically: walks ``<exec_dir>/screenshots/*/`` (skipping the
    ``step_attachments`` subdir), globs ``page_*.jpg`` + ``page_*.png``,
    sorts by stem within each PDF subdir, then concatenates across
    subdirs in subdir-name order. That produces the same global page
    index the auditor used when stamping ``finding.page``.

    Returns an empty list when the execution has no screenshots dir —
    caller handles that gracefully (no thumbnails rendered).
    """
    shots = exec_dir / "screenshots"
    if not shots.exists():
        return []
    pages: list[Path] = []
    for subdir in sorted(shots.iterdir()):
        if subdir.is_dir() and subdir.name != "step_attachments":
            found = (
                sorted(subdir.glob("page_*.jpg"))
                + sorted(subdir.glob("page_*.png"))
            )
            pages.extend(sorted(found, key=lambda p: p.stem))
    return pages


def _resolve_step_attachments(exec_dir: Path) -> dict[int, list[Path]]:
    """Return a ``{step_index: [attachment_path, ...]}`` map for an
    execution's tester-attached step images.

    Empty dict when the execution has no ``step_attachments/`` dir
    (most executions don't; only tests where the tester uploads
    per-step images directly rather than via a session PDF use it).
    Files are filtered to the same image extensions
    auditor.load_step_attachment_images accepts.
    """
    step_dir = exec_dir / "screenshots" / "step_attachments"
    result: dict[int, list[Path]] = {}
    if not step_dir.exists():
        return result
    exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    for p in sorted(step_dir.iterdir()):
        if not p.is_file() or p.suffix.lower() not in exts:
            continue
        m = _STEP_FILENAME_RE.match(p.name)
        if not m:
            continue
        idx = int(m.group(1))
        result.setdefault(idx, []).append(p)
    return result


def _image_thumb_html(
    img_path: Path,
    out_dir: Path,
    alt: str,
) -> str:
    """Render a single clickable thumbnail for a screenshot.

    Click opens full-size in a new tab. ``loading="lazy"`` means the
    browser only fetches the image when the detail row is scrolled
    into view — critical when a big report has hundreds of
    thumbnails in collapsed detail rows.

    Returns empty string when the image can't be located under
    out_dir (e.g. audits scanned from an unrelated tree). Caller
    concatenates the result so empty-string is a safe no-op.
    """
    try:
        rel = img_path.relative_to(out_dir)
    except ValueError:
        return ""
    rel_str = str(rel).replace("\\", "/")  # Windows-safe for href
    escaped = _html_escape(rel_str)
    return (
        f'<a href="{escaped}" target="_blank" class="thumb">'
        f'<img src="{escaped}" loading="lazy" '
        f'alt="{_html_escape(alt)}">'
        f'</a>'
    )

def generate_html_report(
    out_dir: Path,
    variant: str | None = None,
) -> str:
    """Scan `out_dir` and return the HTML report as a string.

    Mirrors `generate_report` (markdown) but emits a single self-
    contained HTML document suitable for opening in a browser. Shares
    the same `_scan_output_dir` + `AuditSummary` data flow so markdown
    and HTML always reflect the same underlying data.

    Routes to the ARGUS Cloudscape-styled renderer (see
    `_render_argus_html_report` below).
    """
    summaries = _scan_output_dir(out_dir, variant=variant)
    return _render_argus_html_report(out_dir, summaries, variant=variant)


# ARGUS HTML renderer — Cloudscape-styled, auditor-friendly
# Design goals:
#   * UI language: Cloud Cloudscape — dark navy header, white card surfaces,
#     blue accent (#0972d3), pill-style verdict badges, sortable table.
#   * Content: drop everything an auditor doesn't need.
#       NO divergence-by-tester table, NO stale-audits section, NO
#       failed-keys section, NO workflow-rule firings table at the top,
#       NO env_check version / sample stride / region crop heights, NO
#       schema version, NO source-of-finding badges, NO timestamp
#       footer, NO per-chunk diagnostics or model trace blocks.
#       KEEP verdict, tester name, test case, findings (rewritten
#       in plain language), inline screenshots, simple filters
#       (search + verdict + flagged-only).
#   * Safe against XSS: every user-sourced string passes through
#     `_html_escape` before being embedded.
#   * Self-contained: one .html file, inline CSS + vanilla JS, no
#     network deps. Double-click to open offline.
#
# Brand: ARGUS (the project's user-facing name). Private Python
# code, log prefixes, module names stay as `argus`.

# Display labels for verdicts. The DASHBOARD-facing strings deliberately
# diverge from the JSON schema's `overall_verdict` field ("pass" / "concerns"
# / "fail") so operators never confuse the AUDIT verdict with the TESTER'S
# own test-management status. Tester says "Pass"; ARGUS says "Compliant" — same
# audit, different speakers. "Deviation" is the audit-flavoured Fail
# (high-severity finding present); "Concerns" stays neutral because it
# already reads as audit-language.
#
# DO NOT change the JSON keys ("pass" / "concerns" / "fail") — every R-rule,
# the replay harness, history_index, the 1300+ historical audits, and
# 505 tests depend on those exact strings.
_ARGUS_VERDICT_LABELS = {
    "pass":     "Compliant",
    "concerns": "Concerns",
    "fail":     "Deviation",
}

# Plain-language headlines for rule findings — auditor-friendly
# replacements for the bare source/rule jargon.
_ARGUS_RULE_HEADLINES = {
    "R0":     "Workflow rule engine couldn't run",
    "R1":     "Step left In Progress without explanation",
    "R2":     "Pass overall but a step is marked Fail",
    "R4":     "Fail overall but no step is marked Fail or Blocked",
    "R5":     "Step marked Pass but the comment describes an issue",
    "R6":     "Passed With Issue with no documentation",
    "R7":     "Passed With Issue but no step is marked Fail",
    "R8":     "Blocked overall but no step is marked Blocked",
    "R9":     "Production URL detected in URL bar",
    "R9-amb": "Webview/Mobile test — URLs not visible in any screenshot",
    "R9-hover": "Page contains hardcoded prod link (product issue, not tester)",
    "R10":    "Defect link pasted in comment instead of attached as trace_link",
    "R11":    "Failed/Passed-With-Issue step has no defect documentation",
    "R12":    "No evidence submitted — tester attached no PDF or screenshots",
    "R13":    "Marketplace mismatch — test ran on the wrong storefront",
}

_ARGUS_SEVERITY_LABEL = {
    "high": "High",
    "medium": "Medium",
    "low": "Low",
}


# Plain-language buckets for model-sourced findings. The model's
# `description` field is free text, so we pattern-match on stable
# substrings to bucket findings into auditor-friendly categories.
# Order matters: earlier patterns win when a description matches
# multiple. SPECIFIC patterns (named tools, specific UI surfaces)
# come first; GENERIC fallback patterns ("not evidenced", "no
# screenshot") at the end so they only fire when nothing more
# specific matches.
#
# Designed against the actual May 15 corpus (~210 audits, 600+
# findings) — patterns selected to bucket ≥95% of unique findings
# into <=20 categories. Re-tune by running the report.py keyword-
# frequency analysis when the corpus shifts.
_ARGUS_MODEL_PATTERNS = [
    # --- Specific named UI surfaces / tools -------------------------------
    ("cloud player",                  "Cloud / web player verification missing"),
    ("web player",                    "Cloud / web player verification missing"),
    ("chrome://inspect",              "Missing chrome://inspect evidence"),
    ("omega",                         "Private tool (Omega) verification gap"),
    ("digicon",                       "Digicon reference / Monetary Amounts mismatch"),
    ("monetary amount",               "Digicon reference / Monetary Amounts mismatch"),
    ("reference image",               "Digicon reference / Monetary Amounts mismatch"),
    ("pre-order",                     "Pre-order verification missing"),
    ("preorder",                      "Pre-order verification missing"),
    ("price guarantee",               "Pre-order verification missing"),
    ("submit preferences",            "Onboarding preferences verification"),
    ("last position",                 "Last-position-here (LPH) verification"),
    ("lph ",                          "Last-position-here (LPH) verification"),
    # --- Specific verification screens that have their own bucket --------
    ("inbox screenshot",              "Missing email screenshot"),
    ("missing email",                 "Missing email screenshot"),
    ("email confirmation",            "Missing email screenshot"),
    ("email screenshot",              "Missing email screenshot"),
    ("purchase history",              "Missing purchase-history screenshot"),
    (" typ ",                         "Missing TYP screenshot"),
    ("typ screenshot",                "Missing TYP screenshot"),
    ("typ page",                      "Missing TYP screenshot"),
    ("thank you page",                "Missing TYP screenshot"),
    ("wishlist",                      "Wishlist verification missing"),
    (" cart ",                        "Cart verification missing"),
    # --- Membership lifecycle (cancel / undo / switch) -------------------
    ("undo",                          "Undo-cancel verification missing"),
    ("reactivate",                    "Undo-cancel verification missing"),
    ("hard cancel",                   "Cancellation flow incomplete"),
    ("cancellation",                  "Cancellation flow incomplete"),
    ("cancel",                        "Cancellation flow incomplete"),
    ("switch membership",             "Membership switch verification missing"),
    ("membership type",               "Membership switch verification missing"),
    # --- Playback / chapter / seek-bar verification ----------------------
    ("post-completion",               "Post-completion playback state missing"),
    ("completion of the last",        "Post-completion playback state missing"),
    ("seek bar",                      "Playback state verification missing"),
    ("play button",                   "Playback state verification missing"),
    ("playback",                      "Playback state verification missing"),
    ("chapter",                       "Chapter-list verification missing"),
    # --- Auth / login flows ----------------------------------------------
    ("login",                         "Login flow verification missing"),
    ("sign in",                       "Login flow verification missing"),
    ("sign-in",                       "Login flow verification missing"),
    # --- Marketplace / locale --------------------------------------------
    ("marketplace",                   "Marketplace / locale mismatch"),
    ("storefront",                    "Marketplace / locale mismatch"),
    ("wrong currency",                "Marketplace / locale mismatch"),
    ("exampleapp.com.au",                "Marketplace / locale mismatch"),
    ("exampleapp.de",                    "Marketplace / locale mismatch"),
    # --- Evidence integrity ----------------------------------------------
    ("duplicate",                     "Duplicate screenshot evidence"),
    ("reused",                        "Duplicate screenshot evidence"),
    ("identical",                     "Duplicate screenshot evidence"),
    # --- Text / outcome contradictions -----------------------------------
    ("text mismatch",                 "Expected text mismatch"),
    ("different text",                "Expected text mismatch"),
    ("does not match",                "Expected text mismatch"),
    ("doesn't match",                 "Expected text mismatch"),
    ("wrong outcome",                 "Wrong outcome category"),
    ("free trial",                    "Wrong outcome category"),
    ("contradicts",                   "Direct screenshot contradiction"),
    # --- Quantitative / count contradictions -----------------------------
    ("credits will be added",         "Credit-count verification missing"),
    ("credits added",                 "Credit-count verification missing"),
    (" credit",                       "Credit-count verification missing"),
    # --- Token / lock / library-state checks ----------------------------
    # Surfaced 2026-06-05 from the unbucketed corpus — token-purchase
    # flows have several distinct verification gaps the previous
    # patterns rolled into "Other".
    ("next token",                    "Credit / token verification missing"),
    ("lock symbol",                   "Credit / token verification missing"),
    ("token-purchased",               "Credit / token verification missing"),
    ("token purchase",                "Credit / token verification missing"),
    # --- Title duration / playback-threshold mismatch -------------------
    # Common pattern: "Step N requires listening to 5% of a title with
    # duration between 60 and 1200 minutes" — tester picks a title
    # that's outside the duration band, so the 5% threshold is wrong.
    ("listening to 5%",               "Title duration / playback threshold mismatch"),
    ("5% listening threshold",        "Title duration / playback threshold mismatch"),
    ("duration between",              "Title duration / playback threshold mismatch"),
    ("title used",                    "Title duration / playback threshold mismatch"),
    # --- Wrong device family in DevTools (mobile-emulation tests) ------
    # Distinct from "Marketplace / locale" — this is the platform
    # version (Samsung UA on iPhone-scoped, Galaxy on iPhone, etc.).
    ("ua string",                     "Wrong device family in DevTools"),
    ("user-agent",                    "Wrong device family in DevTools"),
    ("user agent",                    "Wrong device family in DevTools"),
    ("device toolbar",                "Wrong device family in DevTools"),
    ("sm-s931",                       "Wrong device family in DevTools"),
    ("galaxy s",                      "Wrong device family in DevTools"),
    # --- Production URL where preprod required (env compliance miss) ---
    # The env_check rule R9 catches the deterministic case via OCR.
    # The model also notices in narrative — we want both to bucket
    # under the same operator-recognisable label.
    ("prod.exampleapp.com",              "Production URL — environment compliance"),
    ("production host",               "Production URL — environment compliance"),
    ("production url",                "Production URL — environment compliance"),
    ("not a feature-preprod",         "Production URL — environment compliance"),
    ("no 'feature-preprod'",          "Production URL — environment compliance"),
    # --- Locale-specific text mismatch ----------------------------------
    # "Listen now" vs "Ascolta ora", "Scarica" vs "Download" — the
    # tester ran a non-English MP and the model flagged the localised
    # strings as "different text" without realising the locale context.
    # Patterns aim at the visible non-English snippets we've seen.
    ("ascolta ora",                   "Locale-specific text mismatch"),
    ("scarica",                       "Locale-specific text mismatch"),
    ("écouter maintenant",            "Locale-specific text mismatch"),
    ("anhören",                       "Locale-specific text mismatch"),
    ("locale",                        "Locale-specific text mismatch"),
    ("translation",                   "Locale-specific text mismatch"),
    # --- Blocked/N-A step without explanation ---------------------------
    # Distinct from R-rule territory: the model itself flags this when
    # it sees a Blocked/N-A status with no comment. R8/R11/R16/R18
    # cover specific structural cases; the model's ad-hoc flag is
    # complementary.
    ("blocked with no explanatory",   "Blocked step without explanation"),
    ("blocked with the comment",      "Blocked step without explanation"),
    ("marked blocked",                "Blocked step without explanation"),
    ("not applicable without",        "Not Applicable without justification"),
    ("n/a without",                   "Not Applicable without justification"),
    # --- Partial multi-item action (BOGO, multi-step requirements) -----
    # "Step requires returning N titles, only M shown" — a partial-
    # completion pattern the previous catch-alls didn't catch because
    # the description doesn't mention "missing" or "not visible".
    ("only one was returned",         "Partial multi-item action"),
    ("only one of",                   "Partial multi-item action"),
    ("plural",                        "Partial multi-item action"),
    ("titles (plural)",               "Partial multi-item action"),
    ("both removed",                  "Partial multi-item action"),
    # --- Required UI element missing ------------------------------------
    # Specific surfaces the model flags often: CTAs, custom messages,
    # banners. Distinct from "Expected verification not evidenced"
    # (which is the generic catch-all) — these have a specific UI
    # noun and operators want to filter on it.
    ("custom message",                "Required UI element missing"),
    ("listen now button",             "Required UI element missing"),
    ("download button",               "Required UI element missing"),
    ("required cta",                  "Required UI element missing"),
    ("does not show the",             "Required UI element missing"),
    ("not displayed",                 "Required UI element missing"),
    # --- Wrong checkout / payment flow ----------------------------------
    # Express vs standard, cash vs credit, missing payment screen.
    ("express checkout",              "Wrong checkout / payment flow"),
    ("with cash",                     "Wrong checkout / payment flow"),
    ("payment method",                "Wrong checkout / payment flow"),
    ("standard 2-step",               "Wrong checkout / payment flow"),
    # --- DevTools console errors not acknowledged ----------------------
    ("console shows",                 "DevTools console errors unacknowledged"),
    ("devtools console",              "DevTools console errors unacknowledged"),
    ("red badge",                     "DevTools console errors unacknowledged"),
    # --- Promo / custom message / plan-comparison verification --------
    ("promotion",                     "Promo / custom message verification missing"),
    ("plan comparison",               "Promo / custom message verification missing"),
    ("comparison page",               "Promo / custom message verification missing"),
    # --- Alert / error message expected text ---------------------------
    ("alert message",                 "Alert / error text mismatch"),
    ("renewal-failed",                "Alert / error text mismatch"),
    ("unable to renew",               "Alert / error text mismatch"),
    # --- Error states ----------------------------------------------------
    ("404",                           "Error state in screenshot"),
    ("stack trace",                   "Error state in screenshot"),
    ("something went wrong",          "Error state in screenshot"),
    # --- Generic fallbacks (catch-alls — must come LAST) -----------------
    ("not visible",                   "Expected entity missing from screenshot"),
    ("absent",                        "Expected entity missing from screenshot"),
    ("not evidenced",                 "Expected verification not evidenced"),
    ("no screenshot",                 "Expected verification not evidenced"),
    ("missing",                       "Expected verification not evidenced"),
    ("not provided",                  "Expected verification not evidenced"),
    ("not verified",                  "Expected verification not evidenced"),
    ("not shown",                     "Expected verification not evidenced"),
    ("evidence",                      "Evidence gap"),
]


_ISSUE_LABEL_MAX_CHARS = 80


def _smart_finding_fallback(description: str) -> str:
    """Build a meaningful one-line label from the model's raw description
    when no `_ARGUS_MODEL_PATTERNS` needle matched.

    Until 2026-06-05, anything unmatched fell into a single "Other
    model finding" bucket — operators reviewing 196 such findings
    across the corpus had no way to tell them apart without expanding
    each row. The smart fallback now extracts the first sentence (or
    first ~80 chars) so even a one-of-a-kind finding still carries
    informative signal in the issue chip.

    Strategy:
      1. Take the first sentence (up to the first '.', '!', '?', or
         '\\n'). Findings authored by the model usually open with the
         contradiction — that's the most useful summary line.
      2. Strip leading boilerplate ("Step N requires ", "The tester ",
         etc. — these eat the budget without adding signal).
      3. Trim to _ISSUE_LABEL_MAX_CHARS, ending on a word boundary so
         we don't render "step 2 expected the title to be displaye…"
         mid-word.
    """
    if not description:
        return "Other model finding"
    # 1. First sentence
    text = description.strip()
    for sep in (". ", "! ", "? ", "\n"):
        cut = text.find(sep)
        if 20 <= cut <= 200:  # plausible-length sentence
            text = text[:cut]
            break
    # 2. Trim
    if len(text) > _ISSUE_LABEL_MAX_CHARS:
        # Word-boundary trim: back up to the last space before the limit.
        cut = text.rfind(" ", 0, _ISSUE_LABEL_MAX_CHARS - 1)
        text = text[:cut if cut > _ISSUE_LABEL_MAX_CHARS // 2 else _ISSUE_LABEL_MAX_CHARS - 1] + "…"
    return text


def _argus_issue_label(f: dict) -> str:
    """Plain-language one-line label for a single finding.

    For rule/env_check findings, return the friendly label from
    `_ARGUS_RULE_HEADLINES`. For model-sourced findings, bucket
    against `_ARGUS_MODEL_PATTERNS`. Anything still unmatched gets a
    smart fallback (first sentence of the description, truncated)
    instead of the previous "Other model finding" black box.
    """
    rule = f.get("rule")
    if rule and rule in _ARGUS_RULE_HEADLINES:
        return _ARGUS_RULE_HEADLINES[rule]
    desc_raw = f.get("description") or ""
    desc_lower = desc_raw.lower()
    for needle, label in _ARGUS_MODEL_PATTERNS:
        if needle in desc_lower:
            return label
    return _smart_finding_fallback(desc_raw)


def _argus_per_tester_summary(
    summaries: list[AuditSummary],
    roster: list[dict] | None = None,
) -> list[dict]:
    """Build the per-tester rollup data for the top-of-report panel.

    For each tester, returns a dict:
      {
        "tester": str,
        "audits": int,
        "executions": int,              # from roster (>= audits)
        "not_audited": int,             # executed but no audit
        "verdicts": {"pass": N, "concerns": N, "fail": N},
        "high_finding_audits": int,
        "top_issues": [(label, count), ...],
        "rows": [(test_case_key, mp, status, audited_bool, verdict,
                  assigned_name), ...]   # full execution list for <details>
      }

    When `roster` (the `executions` list from _coverage.json) is given,
    the tester set is the UNION of executors and audited testers — so a
    tester who executed but produced no audit still appears. Without a
    roster it falls back to the audited-only view (older folders).

    Sorted: testers with most not-audited executions first (the biggest
    audit gaps a manager should see), then most fails, then most flagged.
    """
    by_tester: dict[str, list[AuditSummary]] = collections.defaultdict(list)
    for s in summaries:
        by_tester[_resolve_tester_label(s.tester) if s.tester else "Unknown"
                  ].append(s)

    # Index audited summaries by join key + group roster by executor.
    audited_by_key: dict[tuple, AuditSummary] = {}
    for s in summaries:
        jk = _audit_join_key(s)
        prev = audited_by_key.get(jk)
        if prev is None or (s.execution_date or "") >= (prev.execution_date or ""):
            audited_by_key[jk] = s

    roster_by_tester: dict[str, list[dict]] = collections.defaultdict(list)
    if roster:
        for row in roster:
            label = _resolve_tester_label(
                row.get("executed_by_name") or row.get("executed_by"))
            roster_by_tester[label].append(row)

    all_testers = set(by_tester) | set(roster_by_tester)

    rows: list[dict] = []
    for tester in all_testers:
        group = by_tester.get(tester, [])
        verdicts = {"pass": 0, "concerns": 0, "fail": 0}
        for s in group:
            v = s.verdict or "unknown"
            if v in verdicts:
                verdicts[v] += 1
        issue_counter: collections.Counter = collections.Counter()
        action_counter: collections.Counter = collections.Counter()
        for s in group:
            for f in s.findings:
                issue_counter[_argus_issue_label(f)] += 1
                action_counter[_finding_action(f)] += 1
        top_issues = issue_counter.most_common(3)
        top_actions = [
            (action, action_counter[action])
            for action in _TRIAGE_ACTION_ORDER
            if action_counter[action]
        ][:3]
        high_finding_audits = sum(1 for s in group if s.has_high_finding)

        exec_rows = roster_by_tester.get(tester, [])
        detail_rows = []
        not_audited = 0
        for er in sorted(exec_rows,
                         key=lambda r: (r.get("test_case_key") or "",
                                        r.get("marketplace") or "")):
            audit = audited_by_key.get(_audit_join_key_from_row(er))
            is_aud = audit is not None
            if not is_aud:
                not_audited += 1
            detail_rows.append((
                er.get("test_case_key") or "—",
                er.get("marketplace") or "—",
                er.get("status") or "—",
                is_aud,
                (audit.verdict or "—") if is_aud else "—",
                er.get("assigned_to_name") or er.get("assigned_to"),
            ))

        rows.append({
            "tester": tester,
            "audits": len(group),
            "executions": len(exec_rows) if roster else len(group),
            "not_audited": not_audited,
            "verdicts": verdicts,
            "high_finding_audits": high_finding_audits,
            "top_issues": top_issues,
            "top_actions": top_actions,
            "rows": detail_rows,
        })

    # Sort: most not-audited execs desc (biggest gaps), then most fails,
    # then most-flagged, then alphabetical.
    def _sort_key(r):
        flagged = r["verdicts"]["concerns"] + r["verdicts"]["fail"]
        return (
            -r["not_audited"],
            -r["verdicts"]["fail"],
            -flagged,
            r["tester"].lower(),
        )
    rows.sort(key=_sort_key)
    return rows


def _argus_unaudited_rows(
    summaries: list["AuditSummary"],
    roster: list[dict],
    start_idx: int,
) -> tuple[list[str], set[str]]:
    """Synthetic main-table rows for executions ARGUS has NOT audited.

    The roster (from _coverage.json) lists every execution; the audited
    subset already has real rows. Anything in the roster with no matching
    AuditSummary becomes a row here so a tester's full body of work shows
    in the one audits table — clicking a tester name (the existing filter)
    then surfaces ALL their executions, audited and not.

    Rows share the main table's shape + data-* attributes so search,
    the tester dropdown, and sorting all work. Verdict is the synthetic
    "unaudited" so they're visually distinct and filterable. Returns
    (row_html_list, extra_tester_names) — the second so the caller can
    add execution-only testers to the filter dropdown.

    `start_idx` keeps data-idx unique across the real audit rows (detail
    panels key off it); unaudited rows have no detail panel.
    """
    audited_keys = {_audit_join_key(s) for s in summaries}
    rows: list[str] = []
    extra_testers: set[str] = set()
    idx = start_idx
    # Stable order: tester, then test case.
    for er in sorted(
            roster,
            key=lambda r: ((_resolve_tester_label(
                r.get("executed_by_name") or r.get("executed_by")) or "").lower(),
                r.get("test_case_key") or "")):
        jk = (er.get("test_case_key"), er.get("testrun_key"),
              er.get("marketplace"))
        if jk in audited_keys:
            continue  # already has a real audit row
        tc = er.get("test_case_key") or "—"
        mp = er.get("marketplace")
        status = er.get("status") or "—"
        tester = _resolve_tester_label(
            er.get("executed_by_name") or er.get("executed_by"))
        extra_testers.add(tester)
        run_key = er.get("testrun_key") or ""
        # Prefer the human run NAME (e.g. "E2E Flow - GMB - IT Desktop -
        # Edge"); fall back to the key when the roster predates name
        # capture. Link it into Tracker's Test Player like audited rows do.
        run_label = er.get("testrun_name") or run_key
        if run_label and run_key:
            run_url = (f"https://tracker.exampleapp.com/secure/Tests.jspa"
                       f"#/testPlayer/{run_key}")
            run_inner = (f'<a class="argus-testrun-link" '
                         f'href="{_html_escape(run_url)}" target="_blank" '
                         f'rel="noopener">{_html_escape(run_label)}</a>')
        else:
            run_inner = _html_escape(run_label)
        mp_badge = (f'<span class="argus-mp-badge" title="Marketplace">'
                    f'{_html_escape(mp)}</span> ') if mp else ""
        tc_name = er.get("test_case_name")
        tc_link = _tracker_browse_anchor(tc) if tc != "—" else _html_escape(tc)
        # Append the test-case name (linked key — name), matching audited
        # rows, so the reviewer sees what the test actually is.
        if tc_name:
            tc_link = f'{tc_link} — {_html_escape(tc_name)}'
        # Execution key (QA-E...) — resolved + cached during coverage
        # build. Shown as the row's primary key (linked to Test Player),
        # matching audited rows. Falls back to the test-case key when the
        # roster predates E-key capture.
        e_key = er.get("e_key")
        key_cell = (f'<span class="argus-key">{_argus_exec_key_link(e_key)}</span>'
                    if e_key else
                    f'<span class="argus-key">{_html_escape(tc)}</span>')
        search_blob = " ".join(filter(None, [
            tc, tc_name or "", e_key or "", tester, run_key, run_label,
            mp or "", status])).lower()
        rows.append(
            f'<tr class="argus-row argus-row-unaudited" data-idx="{idx}" '
            f'data-verdict="unaudited" '
            f'data-tester="{_html_escape(tester)}" '
            f'data-flagged="false" data-high="false" '
            f'data-has-model="false" data-has-ocr="false" '
            f'data-has-rule="false" data-findings="0" data-issues="" '
            f'data-actions="" data-categories="" '
            f'data-search="{_html_escape(search_blob)}">'
            # Bookmark star — same as audited rows so the key column
            # stays aligned and unaudited execs are bookmarkable too.
            f'<td><button type="button" class="argus-bookmark" '
            f'data-key="{_html_escape(e_key or tc)}" '
            f'aria-label="Bookmark this execution" '
            f'title="Bookmark for follow-up (saved in your browser)">'
            f'<span class="argus-bookmark-icon">☆</span>'
            f'</button>'
            f'{key_cell} '
            f'<span class="argus-unaudited-badge" '
            f'title="Executed but not audited by ARGUS">NOT AUDITED</span>'
            f'</td>'
            f'<td>{_argus_tester_cell(tester)}</td>'
            # Verdict column shows the ACTUAL execution status (color-coded
            # — Blocked=blue, In Progress=dark yellow, Retest=orange) so it's
            # clear at a glance WHY it wasn't audited, instead of a generic
            # "Not audited". The NOT AUDITED badge by the key already flags
            # the audit state.
            f'<td>{_argus_status_pill(status)}</td>'
            f'<td class="argus-tc">'
            f'<div class="argus-tc-run">{mp_badge}{run_inner}</div>'
            f'<div class="argus-tc-name">{tc_link}</div></td>'
            f'<td class="argus-num"><span class="argus-muted">—</span></td>'
            f'</tr>'
        )
        idx += 1
    return rows, extra_testers


def _argus_finding_headline(f: dict) -> str:
    """Plain-language one-line headline for a single finding."""
    rule = f.get("rule")
    if rule and rule in _ARGUS_RULE_HEADLINES:
        return _ARGUS_RULE_HEADLINES[rule]
    desc = (f.get("description") or "").strip()
    # Take the first sentence (up to ~110 chars). Most model findings
    # start with a step reference + the core issue in the first
    # sentence; the rest is rationale we render as the body.
    head = desc.split(". ", 1)[0]
    if len(head) > 110:
        head = head[:107].rsplit(" ", 1)[0] + "..."
    return head or "Finding"


def _argus_format_test_case(s: AuditSummary) -> str:
    """Compact test-case label for the table row.

    Returns "<key> — <name>" when both are present; falls back to whichever
    is available. Empty string if neither — caller renders an em dash.
    """
    return s.test_case or ""


def _render_audit_history(exec_dir: Path) -> str:
    """Render a `<details>` block listing prior audits archived under
    `<exec_dir>/audit.json.history/` (Layer 2 forensic trail).

    Returns an empty string when the history dir doesn't exist or is
    empty, so the caller can splice the result into a detail panel
    unconditionally without an `if`. Each archived entry shows its
    timestamp (parsed from filename), verdict, and finding count — the
    minimum a reviewer needs to decide whether to open it.

    Why a small helper instead of inlining
    --------------------------------------
    Keeps the per-row render loop readable, AND lets the data-extraction
    logic be reused by future export formats without copy-pasting the
    file-glob+JSON-parse code.

    Robustness
    ----------
    Silent on per-file parse failures — one corrupt archive shouldn't
    eat the whole history block. We sort by filename (ISO-prefixed
    timestamps sort lexicographically the same as chronologically),
    which is the cheapest way to get oldest-first ordering without
    re-reading mtimes.
    """
    history_dir = exec_dir / "audit.json.history"
    if not history_dir.is_dir():
        return ""
    entries: list[tuple[str, str, int]] = []  # (display_ts, verdict, n)
    for archived in sorted(history_dir.glob("audit_*.json")):
        try:
            data = json.loads(archived.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        # Filename shape: audit_YYYY-MM-DDTHH-MM-SS.json. Recover the
        # display timestamp by re-inserting colons in the time portion.
        stem = archived.stem  # audit_YYYY-MM-DDTHH-MM-SS
        ts_part = stem[len("audit_"):] if stem.startswith("audit_") else stem
        if "T" in ts_part:
            date_part, time_part = ts_part.split("T", 1)
            display_ts = f"{date_part}T{time_part.replace('-', ':')}Z"
        else:
            display_ts = ts_part
        # Prefer the audit's own audited_at when present — more truthful
        # than the filename if anything weird happened during archive.
        own_ts = data.get("audited_at")
        if isinstance(own_ts, str) and own_ts:
            display_ts = own_ts
        verdict = str(data.get("overall_verdict") or "unknown")
        n_findings = len(data.get("findings") or [])
        entries.append((display_ts, verdict, n_findings))

    if not entries:
        return ""

    items = "".join(
        f"<li>{_html_escape(ts)} — verdict={_html_escape(v)}, "
        f"{n} finding{'s' if n != 1 else ''}</li>"
        for (ts, v, n) in entries
    )
    return (
        '<details class="argus-audit-history">'
        f'<summary>Previous audits ({len(entries)})</summary>'
        f'<ul>{items}</ul>'
        '</details>'
    )


def _render_argus_html_report(
    out_dir: Path,
    summaries: list[AuditSummary],
    variant: str | None = None,
) -> str:
    total = len(summaries)
    verdict_counts = collections.Counter(s.verdict or "unknown"
                                         for s in summaries)
    # Note: a "flagged" audit is structurally identical to a "fail"
    # audit because auditor._finalize_verdict already promotes any
    # audit with a high-severity finding (or structural R-rule) to
    # verdict=fail before the audit lands. So the historical
    # "Flagged" stat tile mirrored the "Deviation" tile exactly,
    # adding no signal. We render "Divergent" instead — testers who
    # claimed Pass on something ARGUS flagged. That's the real
    # compliance signal a QA lead is looking for.
    flagged_count = sum(1 for s in summaries if _is_flagged(s))
    divergent_count = sum(1 for s in summaries if _is_divergent(s))
    divergent_pct = (
        round(100 * divergent_count / total) if total else 0
    )

    # Per-tester roster from _coverage.json (executions credited to who
    # ran each test). Loaded once up front: drives both the summary panel
    # and the synthetic "not audited" rows appended to the table below.
    _coverage_data = _load_coverage_json(out_dir)
    _roster = (_coverage_data or {}).get("executions") or []
    per_tester = _argus_per_tester_summary(summaries, roster=_roster)
    action_counts = _action_counter(summaries)
    category_counts = _category_counter(summaries)
    stale_count = sum(1 for s in summaries if _is_stale(s))

    # Unique testers for the dropdown filter. Sorted alphabetically,
    # case-insensitive. "Unknown" sinks to the bottom. Includes testers
    # who only have unaudited executions (no AuditSummary) so the filter
    # can still surface them.
    tester_names = {(s.tester or "Unknown") for s in summaries}
    for er in _roster:
        nm = _resolve_tester_label(
            er.get("executed_by_name") or er.get("executed_by"))
        if nm:
            tester_names.add(nm)
    tester_set = sorted(
        tester_names,
        key=lambda t: (t == "Unknown", t.lower()),
    )

    # --- Build per-audit rows + their detail panels --------------------------
    audit_rows: list[str] = []
    for i, s in enumerate(summaries):
        verdict = s.verdict or "unknown"
        verdict_label = _ARGUS_VERDICT_LABELS.get(verdict,
                                                   verdict.capitalize())
        verdict_cls = (f"argus-badge argus-verdict-{verdict}"
                       if verdict in ("pass", "concerns", "fail")
                       else "argus-badge argus-verdict-unknown")

        # Resolve images once per audit. Used for inline thumbnails.
        page_images = _resolve_page_images(s.path)
        step_attachments = _resolve_step_attachments(s.path)

        # Findings — rewritten in auditor-friendly form.
        findings_html_parts: list[str] = []
        for f in s.findings:
            sev = f.get("severity", "low")
            sev_cls = (f"argus-finding argus-finding-{sev}"
                       if sev in ("high", "medium", "low")
                       else "argus-finding argus-finding-low")
            sev_dot_cls = (f"argus-sev-dot argus-sev-{sev}"
                           if sev in ("high", "medium", "low")
                           else "argus-sev-dot argus-sev-low")
            sev_label = _ARGUS_SEVERITY_LABEL.get(sev, "Low")
            headline = _html_escape(_argus_finding_headline(f))
            desc = _html_escape((f.get("description") or "").strip())
            triage_bits = []
            if f.get("category"):
                triage_bits.append(
                    ("Category", str(f["category"]).replace("_", " "))
                )
            if f.get("confidence"):
                triage_bits.append(("Confidence", str(f["confidence"])))
            if f.get("action"):
                triage_bits.append(
                    ("Action", str(f["action"]).replace("_", " "))
                )
            if f.get("_severity_adjusted_from"):
                triage_bits.append(
                    ("Adjusted", f"from {f['_severity_adjusted_from']}")
                )
            triage_html = (
                '<div class="argus-finding-triage">'
                + "".join(
                    f'<span class="argus-chip"><strong>{_html_escape(label)}:</strong> '
                    f'{_html_escape(value)}</span>'
                    for label, value in triage_bits
                )
                + '</div>'
                if triage_bits else ""
            )

            page = f.get("page")
            step = f.get("step_index")
            loc_parts = []
            if isinstance(page, int):
                loc_parts.append(f"Page {page}")
            if isinstance(step, int):
                loc_parts.append(f"Step {step}")
            locator = " · ".join(loc_parts)

            # Thumbnail resolution. Page reference → that page image.
            # Else step → step attachment(s). Else nothing.
            thumb_html = ""
            if isinstance(page, int) and 1 <= page <= len(page_images):
                thumb_html = _image_thumb_html(
                    page_images[page - 1], out_dir,
                    alt=f"Page {page} — {sev_label}",
                )
            elif isinstance(page, int) and page_images:
                # Page out-of-range — surface the mismatch instead of
                # rendering nothing or a broken image link, so a reviewer
                # can spot a model hallucination.
                thumb_html = (
                    f'<div class="argus-thumb-missing thumb-missing argus-muted">'
                    f'(page {page} — image unavailable; execution has '
                    f'{len(page_images)} pages)'
                    f'</div>'
                )
            elif isinstance(step, int) and step in step_attachments:
                thumb_html = "".join(
                    _image_thumb_html(p, out_dir,
                                      alt=f"Step {step} attachment")
                    for p in step_attachments[step]
                )

            # Fallback: finding has no page/step reference (e.g. env_check
            # or a whole-execution observation), but the execution DOES
            # have screenshots. Rather than show nothing, offer a collapsed
            # "view N screenshots" toggle revealing all pages so a reviewer
            # can always reach the evidence.
            if not thumb_html and page_images:
                strip = "".join(
                    _image_thumb_html(p, out_dir, alt=f"Page {n}")
                    for n, p in enumerate(page_images, start=1)
                )
                if strip:
                    npg = len(page_images)
                    thumb_html = (
                        f'<details class="argus-thumb-fallback">'
                        f'<summary>view {npg} screenshot'
                        f'{"s" if npg != 1 else ""}</summary>'
                        f'<div class="argus-thumb-strip">{strip}</div>'
                        f'</details>'
                    )

            thumb_wrap = (f'<div class="argus-thumbs">{thumb_html}</div>'
                          if thumb_html else "")

            findings_html_parts.append(
                f'<div class="{sev_cls}">'
                f'<div class="argus-finding-head">'
                f'<span class="{sev_dot_cls}" title="{sev_label}"></span>'
                f'<span class="argus-finding-headline">{headline}</span>'
                + (f'<span class="argus-finding-loc">{_html_escape(locator)}</span>'
                   if locator else "")
                + f'</div>'
                + (f'<div class="argus-finding-body">{desc}</div>'
                   if desc and desc != headline else "")
                + triage_html
                + thumb_wrap
                + '</div>'
            )

        if not findings_html_parts:
            findings_html = ('<div class="argus-no-findings">'
                             'No findings — execution looks clean.</div>')
        else:
            findings_html = "".join(findings_html_parts)

        # env_check inline (only show prod URLs — preprod is the
        # expected state, not interesting to a reviewer).
        env_html = ""
        ec = s.env_check or {}
        prod = ec.get("prod_urls") or []
        if prod:
            env_html = (
                '<div class="argus-env-violation">'
                '<strong>Production URLs detected:</strong> '
                + ", ".join(f"<code>{_html_escape(u)}</code>"
                            for u in prod[:5])
                + (f' <span class="argus-muted">(+{len(prod) - 5} more)</span>'
                   if len(prod) > 5 else "")
                + '</div>'
            )

        evidence_html = ""
        if s.evidence_by_step:
            rows = []
            for row in sorted(
                (r for r in s.evidence_by_step if isinstance(r, dict)),
                key=lambda r: r.get("step_index", 0),
            ):
                step = row.get("step_index", "?")
                status = str(row.get("status") or "not_assessed").replace("_", " ")
                confidence = row.get("confidence") or "?"
                pages = row.get("pages") or []
                pages_text = ", ".join(str(p) for p in pages) if pages else "none"
                reason = row.get("missing_reason")
                reason_html = (
                    f'<span class="argus-muted"> — {_html_escape(reason)}</span>'
                    if reason else ""
                )
                rows.append(
                    '<li>'
                    f'<strong>Step {_html_escape(step)}</strong>: '
                    f'{_html_escape(status)} · pages {pages_text} · '
                    f'confidence {_html_escape(confidence)}'
                    f'{reason_html}'
                    '</li>'
                )
            evidence_html = (
                '<details class="argus-evidence-coverage" open>'
                '<summary>Evidence coverage by step</summary>'
                '<ul>' + "".join(rows) + '</ul>'
                '</details>'
            )

        # Layer 3 dismissal banner. Surface every "dismissed" ack so a
        # reviewer sees the human override at the top of the detail
        # panel; without this the audit would silently drop out of
        # flagged-only views with no indication why. Multiple dismissals
        # (dismiss → undismiss → dismiss again) get listed in order so
        # the audit log is complete on the page.
        dismissed = [a for a in s.acknowledgments
                     if a.get("type") == "dismissed"]
        if dismissed:
            entries = []
            for a in dismissed:
                by = _html_escape(a.get("by") or "unknown")
                at = _html_escape(a.get("at") or "")
                reason = a.get("reason")
                reason_html = (f" — {_html_escape(reason)}"
                               if reason else "")
                entries.append(
                    f'<div class="argus-dismissed-line">'
                    f':mute: dismissed by <strong>{by}</strong>'
                    + (f' on {at}' if at else '')
                    + reason_html + '</div>'
                )
            dismissed_html = (
                '<div class="argus-dismissed">'
                + "".join(entries)
                + '</div>'
            )
        else:
            dismissed_html = ""

        # Resolve relative path for the audit.md link.
        try:
            rel_path = s.path.relative_to(out_dir)
        except ValueError:
            rel_path = s.path
        audit_md_href = _html_escape(f"{rel_path}/audit.md")

        # Layer 2 forensic trail: prior audits archived under
        # <exec_dir>/audit.json.history/ when this execution was
        # re-audited. Empty string when nothing archived (first audit) —
        # safe to splice unconditionally.
        history_html = _render_audit_history(s.path)

        # Detail panel HTML.
        detail_html = (
            '<div class="argus-detail">'
            + dismissed_html
            + env_html
            + '<div class="argus-findings">' + findings_html + '</div>'
            + evidence_html
            + history_html
            + f'<div class="argus-detail-foot">'
            + f'<a href="{audit_md_href}" class="argus-link">'
              f'Open full audit.md →</a>'
            + '</div>'
            + '</div>'
        )

        # Row data attributes for client-side filtering + sort.
        flagged_attr = "true" if _is_flagged(s) else "false"
        high_attr = "true" if s.has_high_finding else "false"
        # Per-source flags: which kinds of findings does this audit
        # carry? Used by the source-filter dropdown.
        has_model = any(
            f.get("source") not in ("rule", "env_check")
            and f.get("rule") != "R9"
            and f.get("rule") != "R9-amb"
            for f in s.findings
        )
        has_ocr = any(
            f.get("source") == "env_check" or f.get("rule") in ("R9", "R9-amb")
            for f in s.findings
        )
        has_rule = any(
            f.get("source") == "rule"
            and f.get("rule") not in ("R9", "R9-amb")
            for f in s.findings
        )
        search_blob = " ".join(filter(None, [
            s.key, s.tester or "", s.test_case or "",
            s.testrun_name or "", s.marketplace or "",
        ])).lower()
        # Issue-label list (semicolon-separated, lowercased) so the
        # per-tester top-issue chips can filter the audits table to
        # "this tester's audits with this issue type". Each label is
        # the same plain-language string the chip displays, derived
        # via _argus_issue_label() so chip and row stay in sync.
        issue_labels = "|".join(
            _argus_issue_label(f).lower() for f in s.findings
        )
        action_labels = "|".join(sorted(_finding_actions_for_summary(s)))
        category_labels = "|".join(sorted(_finding_categories_for_summary(s)))

        tester_name = s.tester or "Unknown"
        test_case = _argus_format_test_case(s) or "—"
        # Test run + marketplace come from metadata.json (testRun.name +
        # parse_marketplace_from_testrun_name). Surface them as the
        # primary line of the test-case column so the operator sees the
        # actual run grouping at a glance — the test-case key/name is
        # secondary context. Marketplace renders as a small coloured
        # badge so DE / FR / AU / etc. are scannable while sorting.
        testrun_label = s.testrun_name or ""
        if s.marketplace:
            mp_badge = (f'<span class="argus-mp-badge" '
                        f'title="Assigned marketplace">{_html_escape(s.marketplace)}'
                        f'</span> ')
        else:
            mp_badge = ""
        # Link the test-run name straight into Tracker's Test Player when
        # we have its key (e.g. QA-C17608). External link opens
        # in a new tab so the operator's place in ARGUS is preserved.
        # rel="noopener" prevents window.opener leakage to the linked
        # site. target click stops propagation so the row doesn't also
        # toggle (handled in JS by closest('a, button, ...')).
        if testrun_label and s.testrun_key:
            run_url = (f"https://tracker.exampleapp.com/secure/Tests.jspa"
                       f"#/testPlayer/{s.testrun_key}")
            testrun_inner = (
                f'<a class="argus-testrun-link" '
                f'href="{_html_escape(run_url)}" target="_blank" '
                f'rel="noopener" '
                f'title="Open test run {_html_escape(s.testrun_key)} '
                f'in Tracker Test Player">'
                f'{_html_escape(testrun_label)}</a>'
            )
        elif testrun_label:
            testrun_inner = _html_escape(testrun_label)
        else:
            testrun_inner = ""
        if testrun_inner:
            testrun_html = (f'<div class="argus-tc-run">{mp_badge}'
                            f'{testrun_inner}</div>')
        else:
            testrun_html = ""
        # Link the test-case key (QA-T...) to its test-management test case,
        # keeping the trailing " — description" as plain text.
        if test_case and " — " in test_case:
            tc_key_part, tc_desc_part = test_case.split(" — ", 1)
            tc_name_html = (_tracker_browse_anchor(tc_key_part.strip())
                            + " — " + _html_escape(tc_desc_part))
        elif test_case and test_case.strip().startswith("QA-T"):
            tc_name_html = _tracker_browse_anchor(test_case.strip())
        else:
            tc_name_html = _html_escape(test_case)
        tc_cell_html = (testrun_html
                        + f'<div class="argus-tc-name">'
                        + tc_name_html + '</div>')

        # Severity inline pills next to the count, so the operator can
        # triage at a glance without expanding every row.
        sev_counts = s.severity_counts
        sev_pills = []
        for sev_key, sev_short, sev_pill_cls in (
            ("high", "H", "argus-pill-high"),
            ("medium", "M", "argus-pill-medium"),
            ("low", "L", "argus-pill-low"),
        ):
            n = sev_counts.get(sev_key, 0)
            if n:
                sev_pills.append(
                    f'<span class="argus-pill {sev_pill_cls}" '
                    f'title="{_ARGUS_SEVERITY_LABEL[sev_key]} severity">'
                    f'{n}{sev_short}</span>'
                )
        sev_pills_html = (" ".join(sev_pills)
                          if sev_pills else '<span class="argus-muted">—</span>')

        # Visually flag zero-evidence rows. R12 is the only deterministic
        # rule that fires when the tester literally attached nothing —
        # render with a left-border highlight + a "no evidence" badge so
        # the row stands out even when the audits table is sorted/filtered.
        row_classes = ["argus-row"]
        if "R12" in s.rules_fired:
            row_classes.append("argus-row-noevd")
        audit_rows.append(
            f'<tr class="{" ".join(row_classes)}" data-idx="{i}" '
            f'data-verdict="{_html_escape(verdict)}" '
            f'data-tester="{_html_escape(tester_name)}" '
            f'data-flagged="{flagged_attr}" '
            f'data-high="{high_attr}" '
            f'data-has-model="{"true" if has_model else "false"}" '
            f'data-has-ocr="{"true" if has_ocr else "false"}" '
            f'data-has-rule="{"true" if has_rule else "false"}" '
            f'data-findings="{s.findings_count}" '
            f'data-issues="{_html_escape(issue_labels)}" '
            f'data-actions="{_html_escape(action_labels)}" '
            f'data-categories="{_html_escape(category_labels)}" '
            f'data-search="{_html_escape(search_blob)}">'
            f'<td><button type="button" class="argus-bookmark" '
            f'data-key="{_html_escape(s.key)}" '
            f'aria-label="Bookmark this audit" '
            f'title="Bookmark for follow-up (saved in your browser)">'
            f'<span class="argus-bookmark-icon">☆</span>'
            f'</button>'
            f'<span class="argus-key">{_argus_exec_key_link(s.key)}</span>'
            + (' <span class="argus-noevd-badge" title="Tester '
               'submitted no PDF or screenshots">NO EVIDENCE</span>'
               if "R12" in s.rules_fired else '')
            # NOT APPLICABLE badge for R18 audits — same visual
            # treatment as NO EVIDENCE since both are synthetic-audit
            # variants the operator should distinguish at a glance
            # from real Pass/Fail/Concerns audits.
            + (' <span class="argus-na-badge" title="Tester marked '
               'this test Not Applicable">NOT APPLICABLE</span>'
               if "R18" in s.rules_fired else '')
            + '</td>'
            f'<td>{_argus_tester_cell(tester_name)}</td>'
            f'<td><span class="{verdict_cls}">{_html_escape(verdict_label)}</span></td>'
            f'<td class="argus-tc">{tc_cell_html}</td>'
            f'<td class="argus-num">'
            f'<span class="argus-finding-count">{s.findings_count}</span> '
            f'{sev_pills_html}</td>'
            f'</tr>'
            f'<tr class="argus-detail-row hidden" data-detail="{i}">'
            f'<td colspan="5">{detail_html}</td>'
            f'</tr>'
        )

    # Append synthetic rows for executions ARGUS has NOT audited, so a
    # tester's full body of work lives in this one table — filtering by
    # tester then surfaces audited + unaudited together. These carry
    # verdict="unaudited" and no detail panel.
    if _roster:
        unaudited_rows, _ = _argus_unaudited_rows(
            summaries, _roster, start_idx=len(summaries))
        audit_rows.extend(unaudited_rows)

    # --- Top-level scaffolding -----------------------------------------------
    folder_label = _html_escape(out_dir.name or str(out_dir))
    variant_chip = (f' <span class="argus-chip">variant {_html_escape(variant)}</span>'
                    if variant else "")

    # Stat cards double as filter shortcuts: clicking Pass/Concerns/Fail
    # sets the verdict dropdown; clicking Flagged toggles the flagged-only
    # checkbox. Audits card resets all filters. Keyboard-accessible via
    # the <button> element so screen readers announce them as actionable.
    stat_cards = (
        '<section class="argus-stats">'
        '<button type="button" class="argus-stat" '
        'data-stat-action="reset" title="Show all audits">'
        '<div class="argus-stat-label">Audits</div>'
        f'<div class="argus-stat-value">{total}</div></button>'
        '<button type="button" class="argus-stat" '
        'data-stat-action="verdict" data-stat-value="pass" '
        'title="Filter to compliant audits (no findings)">'
        '<div class="argus-stat-label">Compliant</div>'
        '<div class="argus-stat-value argus-stat-pass">'
        f'{verdict_counts.get("pass", 0)}</div></button>'
        '<button type="button" class="argus-stat" '
        'data-stat-action="verdict" data-stat-value="concerns" '
        'title="Filter to audits with concerns">'
        '<div class="argus-stat-label">Concerns</div>'
        '<div class="argus-stat-value argus-stat-concerns">'
        f'{verdict_counts.get("concerns", 0)}</div></button>'
        '<button type="button" class="argus-stat" '
        'data-stat-action="verdict" data-stat-value="fail" '
        'title="Filter to audits with deviations (high-severity findings)">'
        '<div class="argus-stat-label">Deviation</div>'
        '<div class="argus-stat-value argus-stat-fail">'
        f'{verdict_counts.get("fail", 0)}</div></button>'
        '<button type="button" class="argus-stat" '
        'data-stat-action="divergent" '
        'title="Tester said Pass; ARGUS flagged the audit. The real '
        'compliance signal a QA lead acts on.">'
        '<div class="argus-stat-label">Divergent</div>'
        f'<div class="argus-stat-value">{divergent_count}'
        f'<span class="argus-stat-pct"> · {divergent_pct}%</span>'
        '</div></button>'
        '</section>'
    )

    # --- No-evidence callout ------------------------------------------------
    # Zero-evidence submissions (R12) are the most clear-cut compliance
    # call: tester attached no PDF and no per-step screenshots. R12
    # already shows up in the audits table and the per-tester rollup,
    # but it's high-priority enough to deserve a top-of-report banner
    # that names every offending tester + key. Skipped silently when no
    # R12 audits exist so clean folders don't carry empty UI noise.
    no_evidence = [s for s in summaries if "R12" in s.rules_fired]
    if no_evidence:
        # Group by tester so an auditor sees "Akalyaa E (1)" not 5 keys
        # blurred together.
        by_tester: dict[str, list[AuditSummary]] = collections.defaultdict(list)
        for s in no_evidence:
            by_tester[s.tester or "Unknown"].append(s)
        rows: list[str] = []
        for tester in sorted(by_tester, key=lambda t: t.lower()):
            keys = sorted(by_tester[tester], key=lambda s: s.key)
            key_chips = " ".join(
                f'<button type="button" class="argus-noevd-key" '
                f'data-key="{_html_escape(s.key)}">'
                f'{_html_escape(s.key)}</button>'
                for s in keys
            )
            rows.append(
                f'<li><span class="argus-noevd-tester">'
                f'{_html_escape(tester)}</span> '
                f'<span class="argus-noevd-count">'
                f'({len(keys)} key{"s" if len(keys) != 1 else ""})</span>'
                f' {key_chips}</li>'
            )
        no_evidence_panel = (
            '<section id="argus-noevd-panel" '
            'class="argus-noevd-panel" role="alert" '
            'aria-label="No-evidence audits">'
            '<button type="button" id="argus-noevd-close" '
            'class="argus-noevd-close" aria-label="Dismiss banner" '
            'title="Dismiss">&times;</button>'
            '<div class="argus-noevd-header">'
            '<span class="argus-noevd-icon" aria-hidden="true">!</span>'
            '<div>'
            f'<h2>No evidence submitted — {len(no_evidence)} '
            f'audit{"s" if len(no_evidence) != 1 else ""}</h2>'
            '<p class="argus-noevd-sub">'
            'These executions were submitted without any PDF or '
            'screenshot. The audit pipeline cannot verify the test was '
            'performed. Tester must re-submit with evidence, or the '
            'execution must be re-run. Click a key to filter the table.'
            '</p>'
            '</div>'
            '</div>'
            f'<ul class="argus-noevd-list">{"".join(rows)}</ul>'
            '</section>'
        )
    else:
        no_evidence_panel = ""

    # --- Action queue -------------------------------------------------------
    # This panel answers the manager's first question: what work should
    # happen next? It uses the v6 triage fields, including report-time
    # inference for legacy audit.json files.
    action_items = [
        (action, action_counts[action])
        for action in _TRIAGE_ACTION_ORDER
        if action_counts[action]
    ]
    if action_items:
        action_buttons = "".join(
            f'<button type="button" class="argus-action-card" '
            f'data-action-filter="{_html_escape(action)}" '
            f'title="Show findings needing: '
            f'{_html_escape(_TRIAGE_ACTION_LABELS[action])}">'
            f'<span class="argus-action-label">'
            f'{_html_escape(_TRIAGE_ACTION_LABELS[action])}</span>'
            f'<span class="argus-action-count">{count}</span>'
            f'</button>'
            for action, count in action_items
        )
        category_bits = " ".join(
            f'<span class="argus-action-chip">'
            f'{_html_escape(_TRIAGE_CATEGORY_LABELS[category])}: '
            f'<strong>{count}</strong></span>'
            for category, count in (
                (category, category_counts[category])
                for category in _TRIAGE_CATEGORY_ORDER
                if category_counts[category]
            )
        )
        action_queue_panel = (
            '<section class="argus-action-panel">'
            '<div class="argus-action-header">'
            '<h2>Action queue</h2>'
            '<p>Findings grouped by the next QA action.</p>'
            '</div>'
            f'<div class="argus-action-grid">{action_buttons}</div>'
            + (f'<div class="argus-action-categories">{category_bits}</div>'
               if category_bits else '')
            + '</section>'
        )
    else:
        action_queue_panel = ""

    # --- Legacy schema notice ---------------------------------------------
    if stale_count:
        legacy_panel = (
            '<section class="argus-legacy-panel" role="note">'
            '<strong>Legacy audits detected.</strong> '
            f'{stale_count} of {total} audit'
            f'{"s are" if stale_count != 1 else " is"} older than schema '
            f'v{_CURRENT_SCHEMA_VERSION}. Category/action chips are '
            'inferred for those audits; re-audit to enable model-native '
            'step evidence coverage.'
            '</section>'
        )
    else:
        legacy_panel = ""

    # --- Per-tester summary panel -------------------------------------------
    # A scoreboard: each tester's executions / audited / verdicts. Clicking
    # a tester name filters the audits table below to all their executions
    # (audited rows + the synthetic unaudited rows appended above). per_tester
    # is computed up front (near the roster load); rendered here.
    if per_tester:
        tester_rows_html: list[str] = []
        for r in per_tester:
            v = r["verdicts"]
            verdict_pills = (
                f'<span class="argus-mini-pill argus-mini-pass">'
                f'{v["pass"]}P</span>'
                f'<span class="argus-mini-pill argus-mini-concerns">'
                f'{v["concerns"]}C</span>'
                f'<span class="argus-mini-pill argus-mini-fail">'
                f'{v["fail"]}F</span>'
            )
            if r["top_issues"]:
                issue_chips = " ".join(
                    f'<button type="button" class="argus-issue-chip" '
                    f'data-issue="{_html_escape(label.lower())}" '
                    f'data-tester="{_html_escape(r["tester"])}" '
                    f'title="Click to show {count} '
                    f'audit{"s" if count != 1 else ""} for '
                    f'{_html_escape(r["tester"])} with this issue">'
                    f'{_html_escape(label)} <span class="argus-issue-count">'
                    f'{count}</span></button>'
                    for label, count in r["top_issues"]
                )
            else:
                issue_chips = ('<span class="argus-no-issues">'
                               'No findings — clean</span>')
            if r["top_actions"]:
                action_chips = " ".join(
                    f'<button type="button" class="argus-action-chip-btn" '
                    f'data-action-filter="{_html_escape(action)}" '
                    f'data-tester="{_html_escape(r["tester"])}" '
                    f'title="Show {_html_escape(r["tester"])} audits needing '
                    f'{_html_escape(_TRIAGE_ACTION_LABELS[action])}">'
                    f'{_html_escape(_TRIAGE_ACTION_LABELS[action])} '
                    f'<span class="argus-issue-count">{count}</span>'
                    f'</button>'
                    for action, count in r["top_actions"]
                )
            else:
                action_chips = ('<span class="argus-no-issues">'
                                'No actions</span>')
            # Executions cell: total with a not-audited badge when any
            # execution lacks an audit — the manager's "audit gap" signal.
            # Clicking the count filters the table to this tester so their
            # full execution list (audited + unaudited rows) shows below.
            execs = r["executions"]
            if r["not_audited"]:
                execs_cell = (
                    f'{execs} '
                    f'<span class="argus-mini-pill argus-mini-fail" '
                    f'title="{r["not_audited"]} executed but not audited">'
                    f'{r["not_audited"]}✗</span>'
                )
            else:
                execs_cell = str(execs)
            tester_rows_html.append(
                f'<tr>'
                f'<td><button type="button" class="argus-tester-link" '
                f'data-tester="{_html_escape(r["tester"])}">'
                f'{_html_escape(r["tester"])}</button></td>'
                f'<td class="argus-num">{execs_cell}</td>'
                f'<td class="argus-num">{r["audits"]}</td>'
                f'<td class="argus-num argus-verdict-mini">{verdict_pills}</td>'
                f'<td>{action_chips}</td>'
                f'<td>{issue_chips}</td>'
                f'</tr>'
            )
        tester_panel = (
            '<section class="argus-tester-panel">'
            '<div class="argus-tester-header">'
            '<h2>Per-tester summary</h2>'
            '</div>'
            '<table class="argus-tester-table">'
            '<thead><tr>'
            '<th>Tester</th>'
            '<th class="argus-num">Executions</th>'
            '<th class="argus-num">Audited</th>'
            '<th class="argus-num">Verdicts</th>'
            '<th>Actions</th>'
            '<th>Top issues</th>'
            '</tr></thead>'
            f'<tbody>{"".join(tester_rows_html)}</tbody>'
            '</table>'
            '</section>'
        )
    else:
        # No testers at all — no panel.
        tester_panel = ""

    # Filters bar — search + verdict + tester + high-only + flagged-only
    # + source. Plus a "Clear filters" button and a live-updating count
    # of how many rows are visible right now (helpful when triaging a
    # 130-key folder down to "show me only fail+model-finding").
    tester_options = "\n".join(
        f'<option value="{_html_escape(t)}">{_html_escape(t)}</option>'
        for t in tester_set
    )
    action_options = "\n".join(
        f'<option value="{_html_escape(action)}">'
        f'{_html_escape(_TRIAGE_ACTION_LABELS[action])}</option>'
        for action in _TRIAGE_ACTION_ORDER
        if action_counts[action]
    )
    category_options = "\n".join(
        f'<option value="{_html_escape(category)}">'
        f'{_html_escape(_TRIAGE_CATEGORY_LABELS[category])}</option>'
        for category in _TRIAGE_CATEGORY_ORDER
        if category_counts[category]
    )
    filters_html = (
        '<div class="argus-filters" id="argus-filters">'
        '<input type="text" id="argus-search" '
        'placeholder="Search key / tester / test case…  (press / to focus)" '
        'class="argus-input">'
        '<select id="argus-verdict" class="argus-select" '
        'title="Filter by verdict">'
        '<option value="">All verdicts</option>'
        '<option value="pass">Compliant</option>'
        '<option value="concerns">Concerns</option>'
        '<option value="fail">Deviation</option>'
        '<option value="unaudited">Not audited</option>'
        '</select>'
        '<select id="argus-tester" class="argus-select" '
        'title="Filter by tester">'
        '<option value="">All testers</option>'
        f'{tester_options}'
        '</select>'
        '<select id="argus-source" class="argus-select" '
        'title="Filter by which subsystem flagged the audit">'
        '<option value="">Any source</option>'
        '<option value="has-model">Has model finding</option>'
        '<option value="has-ocr">Has OCR finding (R9)</option>'
        '<option value="has-rule">Has rule finding (R0-R11)</option>'
        '<option value="only-model">Model-only (no OCR)</option>'
        '<option value="only-ocr">OCR-only (no model)</option>'
        '</select>'
        '<select id="argus-action" class="argus-select" '
        'title="Filter by required QA action">'
        '<option value="">Any action</option>'
        f'{action_options}'
        '</select>'
        '<select id="argus-category" class="argus-select" '
        'title="Filter by finding category">'
        '<option value="">Any category</option>'
        f'{category_options}'
        '</select>'
        '<label class="argus-toggle">'
        '<input type="checkbox" id="argus-high-only"> '
        'High only'
        '</label>'
        '<label class="argus-toggle">'
        '<input type="checkbox" id="argus-flagged-only"> '
        'Flagged only'
        '</label>'
        '<label class="argus-toggle">'
        '<input type="checkbox" id="argus-bookmarked-only"> '
        '<span class="argus-bookmark-icon">★</span> Bookmarked only'
        '</label>'
        '<button type="button" id="argus-clear" class="argus-btn-clear" '
        'title="Reset all filters">Clear</button>'
        '<span id="argus-count" class="argus-muted argus-count">'
        f'{total} of {total}</span>'
        '</div>'
    )

    # Sortable table — every <th> is clickable. data-sort-key tells the
    # JS sort routine whether to compare as text or number, and which
    # row attribute to read.
    table_html = (
        '<table class="argus-table" id="argus-table">'
        '<thead><tr>'
        '<th data-sort-key="key" class="argus-sortable">'
        'Execution key <span class="argus-sort-indicator"></span></th>'
        '<th data-sort-key="tester" class="argus-sortable">'
        'Tester <span class="argus-sort-indicator"></span></th>'
        '<th data-sort-key="verdict" class="argus-sortable">'
        'Verdict <span class="argus-sort-indicator"></span></th>'
        '<th data-sort-key="test_case" class="argus-sortable">'
        'Test run / case <span class="argus-sort-indicator"></span></th>'
        '<th data-sort-key="findings" data-sort-numeric="1" '
        'class="argus-sortable argus-num">'
        'Findings <span class="argus-sort-indicator"></span></th>'
        '</tr></thead>'
        f'<tbody id="argus-tbody">{"".join(audit_rows)}</tbody>'
        '</table>'
    )

    # --- R15 coverage gaps panel ------------------------------------------
    # Sourced from <out_dir>/_coverage.json (per-folder, never per-audit).
    # Rendered as a slide-in drawer (trigger pill in the header,
    # off-screen panel that opens on click) so the page's main scroll
    # isn't dominated by hundreds of skipped-key bullets. Returns ""
    # when no _coverage.json is present.
    #
    # The returned HTML is two pieces concatenated: a header pill
    # (positioned next to the brand) plus a fixed-position drawer +
    # backdrop. We splice both into the page below the body's <header>
    # element via document.body, so the drawer's z-index isn't trapped
    # inside the main content's stacking context.
    coverage_html = _render_coverage_html(out_dir)

    if total == 0:
        body_main = ('<section class="argus-empty">'
                     '<p>No audits found in this folder.</p>'
                     '</section>')
    else:
        body_main = (stat_cards + legacy_panel + no_evidence_panel
                     + action_queue_panel + tester_panel + filters_html
                     + table_html)

    css = _ARGUS_CSS
    js = _ARGUS_JS

    return (
        '<!DOCTYPE html>\n'
        '<html lang="en">\n'
        '<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<title>ARGUS — {folder_label}</title>\n'
        f'<style>{css}</style>\n'
        '</head>\n'
        '<body>\n'
        # The brand wordmark is a link back to the dashboard index (/)
        # so an operator viewing a per-folder report can click ARGUS
        # to return to the folder list. Root-relative href works because
        # both the index and per-folder reports live under the same
        # argus_serve mount point (/ and /<folder>/_argus.html).
        '<header class="argus-header">'
        '<div class="argus-header-inner">'
        '<a href="/" class="argus-brand-link">'
        '<div class="argus-brand">'
        '<span class="argus-logo">ARGUS</span>'
        '<span class="argus-tagline">QS execution auditor</span>'
        '</div>'
        '</a>'
        '<div class="argus-folder">'
        f'<span class="argus-folder-name">{folder_label}</span>'
        f'{variant_chip}'
        '</div>'
        '</div>'
        '</header>\n'
        f'<main class="argus-main">{body_main}</main>\n'
        # Coverage drawer + its trigger live at body-end so they're
        # outside the main scroll's stacking context. CSS positions
        # both as fixed (trigger top-right of viewport, drawer slides
        # in from the right edge), so DOM order doesn't affect layout.
        f'{coverage_html}'
        f'<script>{js}</script>\n'
        '</body>\n'
        '</html>\n'
    )

# Cloud Cloudscape-derived palette + the IIFE that wires up filters,
# bookmarks, sort, and the coverage drawer. Both blobs live next to
# this module under report_assets/ so the file stays editable. Read
# at import time, so module-level access (_ARGUS_CSS, _ARGUS_JS) is
# unchanged for callers.
_ASSETS_DIR = Path(__file__).parent / "report_assets"
_ARGUS_CSS = (_ASSETS_DIR / "argus.css").read_text(encoding="utf-8")
_ARGUS_JS = (_ASSETS_DIR / "argus.js").read_text(encoding="utf-8")



if __name__ == "__main__":
    sys.exit(main())
