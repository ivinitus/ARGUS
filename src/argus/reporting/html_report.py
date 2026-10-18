"""Self-contained HTML report renderer."""
from __future__ import annotations

import base64
import collections
import html as _html_lib
import json
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from argus.domain import finding_inference, folder_coverage
from argus.domain.step_refs import human_step_number

from . import report as _base
from . import static_assets
from .report import (
    AuditSummary,
    _ARGUS_STATUS_ATTENTION_RANK,
    _CURRENT_SCHEMA_VERSION,
    _TRIAGE_ACTION_LABELS,
    _TRIAGE_ACTION_ORDER,
    _TRIAGE_CATEGORY_LABELS,
    _TRIAGE_CATEGORY_ORDER,
    _audit_join_key_from_row,
    _argus_exec_key_link,
    _argus_status_pill,
    _argus_tester_cell,
    _finding_action,
    _finding_actions_for_summary,
    _finding_categories_for_summary,
    _is_flagged,
    _is_stale,
    _index_roster_audits,
    _jira_browse_anchor,
    _load_coverage_json,
    _render_coverage_html,
    _resolve_tester_label,
)
from .ui import DEFAULT_UI_CONTEXT, ReportUiContext
from .verdicts import (
    VERDICT_LABELS as _ARGUS_VERDICT_LABELS, audio_verification_label,
    requirement_label, verdict_label,
)

def _html_escape(s: object) -> str:
    """Escape for HTML text content. None renders as empty string."""
    if s is None:
        return ""
    return _html_lib.escape(str(s), quote=True)


_STEP_FILENAME_RE = re.compile(r"^step_(\d+)_")


def _resolve_page_images(exec_dir: Path) -> list[Path]:
    """Return the flattened, sorted list of PDF-split page images for one execution."""
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
    """Return a ``{step_index: [attachment_path, ...]}`` map for an execution's tester-attached step images."""
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


def _resolve_top_level_attachments(exec_dir: Path) -> list[Path]:
    """Return tester-attached images NOT tied to a specific step."""
    step_dir = exec_dir / "screenshots" / "step_attachments"
    if not step_dir.exists():
        return []
    exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    out: list[Path] = []
    for p in sorted(step_dir.iterdir()):
        if not p.is_file() or p.suffix.lower() not in exts:
            continue
        if p.name.startswith("top_"):
            out.append(p)
    return out


def _ordered_screenshot_images(
    page_images: list[Path],
    top_attachments: list[Path],
    step_attachments: dict[int, list[Path]],
) -> list[Path]:
    """Return every screenshot once, in a stable reviewer-friendly order."""
    candidates = [
        *page_images,
        *top_attachments,
        *(
            image
            for step_index in sorted(step_attachments)
            for image in step_attachments[step_index]
        ),
    ]
    images: list[Path] = []
    seen: set[Path] = set()
    for image in candidates:
        if image in seen:
            continue
        seen.add(image)
        images.append(image)
    return images


def _image_thumb_html(
    img_path: Path,
    out_dir: Path,
    alt: str,
) -> str:
    """Render a single clickable thumbnail for a screenshot."""
    try:
        rel = img_path.relative_to(out_dir)
    except ValueError:
        return ""
    rel_str = str(rel).replace("\\", "/")
    escaped = _html_escape(rel_str)
    return (
        f'<a href="{escaped}" target="_blank" class="thumb">'
        f'<img src="{escaped}" loading="lazy" '
        f'alt="{_html_escape(alt)}">'
        f'</a>'
    )

_ARGUS_CSV_HEADERS = [
    "Execution key", "Tester", "Status", "Verdict", "Test run key",
    "Test run", "Test case", "Marketplace", "Execution date", "Analysis status",
    "Analysis diagnostic", "Findings count", "High", "Medium", "Low", "Rules", "Findings",
    "Review status",
]


def _audit_csv_values(s: AuditSummary) -> list:
    """Export the report's full values, including unloaded detail text."""
    return [
        s.key, _resolve_tester_label(s.tester) if s.tester else "Unknown", s.tester_status or "",
        _ARGUS_VERDICT_LABELS.get(s.verdict or "", s.verdict or "Unknown"),
        s.testrun_key or "", s.testrun_name or "", s.test_case or "",
        s.marketplace or "", s.execution_date or "", s.analysis_status,
        s.analysis_diagnostic or "",
        s.findings_count,
        *(s.severity_counts.get(severity, 0) for severity in ("high", "medium", "low")),
        "; ".join(s.rules_fired),
        "\n".join(str(f.get("description") or "") for f in s.findings),
        "Needs review" if s.review_status == "required" else "",
    ]


def _unaudited_csv_values(row: dict) -> list:
    """Keep unassessed executions in exports without inventing audit results."""
    test_case = " — ".join(filter(None, [
        row.get("test_case_key"), row.get("test_case_name"),
    ]))
    values = {
        "Execution key": row.get("e_key") or "",
        "Tester": _resolve_tester_label(
            row.get("executed_by_name") or row.get("executed_by")),
        "Status": row.get("status") or "",
        "Verdict": "Not audited",
        "Test run key": row.get("testrun_key") or "",
        "Test run": row.get("testrun_name") or "",
        "Test case": test_case,
        "Marketplace": row.get("marketplace") or "",
        "Execution date": row.get("execution_date") or "",
        "Analysis status": "not_audited",
    }
    return [values.get(column, "") for column in _ARGUS_CSV_HEADERS]


def _run_id(key: str | None) -> str:
    return (key or "").strip().upper() or "_unknown"


def _execution_day(value: str | None) -> str:
    """Use the calendar date recorded by Jira, without browser timezone shifts."""
    try:
        return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return "_unknown"


def _audited_day(value: str | None) -> str:
    """UTC calendar date ARGUS finished the audit (audit.json "audited_at").

    Audit timestamps are canonical UTC (auditor writes an ISO-8601 'Z' string);
    the API path keeps that UTC default, so the audit-date dimension is computed
    in UTC rather than shifted to the tester's local calendar the way execution
    dates are. Unaudited or unparseable values collapse to "" so a row with no
    audit is simply excluded when an audit-date range is active.
    """
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).date().isoformat()


@dataclass
class _ReportRun:
    key: str
    name: str = ""
    audits: list[AuditSummary] = field(default_factory=list)
    unaudited: list[dict] = field(default_factory=list)

    @property
    def label(self) -> str:
        if self.key == "_unknown":
            return "Run not recorded"
        return f"{self.key} — {self.name}" if self.name else self.key

    @property
    def counts(self) -> dict[str, int]:
        complete = [
            audit for audit in self.audits
            if audit.analysis_status != "incomplete"
        ]
        return {
            "executions": len(self.audits) + len(self.unaudited),
            "completed": len(complete),
            "incomplete": len(self.audits) - len(complete),
            "unaudited": len(self.unaudited),
            "deviations": sum(audit.verdict == "fail" for audit in complete),
            "findings": sum(audit.findings_count for audit in complete),
        }


def _group_report_runs(
    summaries: list[AuditSummary], roster: list[dict],
) -> list[_ReportRun]:
    """Partition this report's executions by run identity, for every profile."""
    roster_by_key = {row["e_key"]: row for row in roster if row.get("e_key")}
    latest: dict[str, AuditSummary] = {}
    for summary in summaries:
        previous = latest.get(summary.key)
        if previous and (previous.execution_date or "") > (summary.execution_date or ""):
            continue
        row = roster_by_key.get(summary.key, {})
        # Missing extraction metadata must not strand a known failed execution
        # outside its run. The exact E-key is the only fallback join here.
        latest[summary.key] = replace(
            summary,
            testrun_key=summary.testrun_key or row.get("testrun_key"),
            testrun_name=summary.testrun_name or row.get("testrun_name"),
            execution_date=summary.execution_date or row.get("execution_date"),
        )
    audits = list(latest.values())
    indexed = _index_roster_audits(audits)
    runs: dict[str, _ReportRun] = {}

    def group(key: str | None, name: str | None) -> _ReportRun:
        identity = _run_id(key)
        run = runs.setdefault(identity, _ReportRun(identity))
        if name:
            run.name = name
        return run

    for summary in audits:
        group(summary.testrun_key, summary.testrun_name).audits.append(summary)
    seen: set[tuple] = set()
    for row in roster:
        join_key = _audit_join_key_from_row(row)
        identity = _run_id(row.get("testrun_key"))
        if identity in runs and row.get("testrun_name"):
            runs[identity].name = row["testrun_name"]
        if (
            not folder_coverage.is_executed_status(row.get("status"))
            or join_key in indexed or join_key in seen
        ):
            continue
        seen.add(join_key)
        group(row.get("testrun_key"), row.get("testrun_name")).unaudited.append(row)
    return sorted(
        runs.values(),
        key=lambda run: (run.key == "_unknown", run.name.casefold(), run.key),
    )


def _group_run_days(run: _ReportRun) -> list[_ReportRun]:
    days: dict[str, _ReportRun] = {}
    for audit in run.audits:
        day = _execution_day(audit.execution_date)
        days.setdefault(day, _ReportRun(day)).audits.append(audit)
    for row in run.unaudited:
        day = _execution_day(row.get("execution_date"))
        days.setdefault(day, _ReportRun(day)).unaudited.append(row)
    return [days[day] for day in sorted(days, reverse=True) if day != "_unknown"] + (
        [days["_unknown"]] if "_unknown" in days else []
    )


def _argus_render_day_heading(run: _ReportRun, day: _ReportRun) -> str:
    label = "Execution date not recorded" if day.key == "_unknown" else day.key
    counts = day.counts
    return (
        '<tr class="argus-day-heading" '
        f'data-run="{_html_escape(run.key)}" data-day="{_html_escape(day.key)}">'
        f'<td colspan="6"><strong>{_html_escape(label)}</strong>'
        '<span class="argus-run-totals">'
        f'{counts["executions"]} executions · {counts["completed"]} completed · '
        f'{counts["incomplete"]} incomplete · {counts["unaudited"]} not audited · '
        f'{counts["deviations"]} deviations'
        '</span></td></tr>'
    )


def _argus_render_run_drawer(runs: list[_ReportRun]) -> str:
    cards = []
    columns = (
        ("executions", "Executions"), ("completed", "Completed"),
        ("incomplete", "Incomplete"), ("unaudited", "Not audited"),
        ("deviations", "Deviations"), ("findings", "Findings"),
    )
    for run in runs:
        counts = run.counts
        cells = "".join(
            f'<div><dt>{label}</dt><dd data-run-count="{column}">'
            f'{counts[column]}</dd></div>'
            for column, label in columns
        )
        cards.append(
            f'<li class="argus-run-card" data-run-summary="{_html_escape(run.key)}">'
            '<button type="button" class="argus-run-select" '
            f'data-run-select="{_html_escape(run.key)}">'
            f'{_html_escape(run.label)}</button>'
            f'<dl class="argus-run-counts">{cells}</dl>'
            '<button type="button" class="cs-btn" disabled '
            f'data-run-export="{_html_escape(run.key)}" '
            f'title="Export every execution in {_html_escape(run.label)}">'
            'Export run CSV</button></li>'
        )
    return (
        '<dialog id="argus-runs-drawer" class="argus-runs-drawer" '
        'aria-labelledby="argus-runs-title">'
        '<div class="argus-runs-header"><h2 id="argus-runs-title">Test runs'
        f'<span class="cs-counter">({len(runs)})</span></h2>'
        '<button type="button" id="argus-runs-close" class="argus-runs-close" '
        'aria-label="Close test runs">&times;</button></div>'
        '<div class="argus-runs-tools">'
        '<label for="argus-runs-search">Find a run</label>'
        '<input type="search" id="argus-runs-search" class="argus-input" '
        'placeholder="Search run name or key" autocomplete="off" autofocus>'
        '<div class="argus-runs-tools-row">'
        f'<span id="argus-runs-count" role="status">{len(runs)} runs</span>'
        '<button type="button" class="cs-btn" data-run-select="">Show all runs</button>'
        '</div><p>Choose a run to view its executions. Counts and run exports cover '
        'all executions in that run, regardless of report filters. Incomplete and '
        'unaudited executions are excluded from deviation counts.</p></div>'
        '<div class="argus-runs-scroll">'
        f'<ul class="argus-run-cards">{"".join(cards)}</ul>'
        '<p id="argus-runs-empty" class="argus-no-matches" hidden>'
        'No runs match your search.</p></div></dialog>'
    )


def _argus_render_run_heading(run: _ReportRun) -> str:
    counts = run.counts
    return (
        f'<tr class="argus-run-heading" data-run="{_html_escape(run.key)}">'
        '<td colspan="6"><button type="button" class="argus-run-toggle" '
        f'data-run-toggle="{_html_escape(run.key)}" aria-expanded="true">'
        '<span class="argus-run-chevron" aria-hidden="true">▾</span>'
        f'<strong>{_html_escape(run.label)}</strong>'
        '<span class="argus-run-totals">'
        f'{counts["executions"]} executions · {counts["completed"]} completed · '
        f'{counts["incomplete"]} incomplete · {counts["unaudited"]} not audited'
        '</span></button></td></tr>'
    )


_ARGUS_RULE_HEADLINES = {
    "R0":     "Workflow rule engine couldn't run",
    "R1":     "Step left In Progress without explanation",
    "R2":     "Pass overall but a step is marked Fail",
    "R2b":    "Pass overall but every step is Not Executed",
    "R4":     "Fail overall but no step is marked Fail or Blocked",
    "R5":     "Step marked Pass but the comment describes an issue",
    "R6":     "Passed With Issue with no documentation",
    "R7":     "Passed With Issue but no step is marked Fail",
    "R8":     "Blocked overall but no step is marked Blocked",
    "R9":     "Production URL detected in URL bar",
    "R9-doc": "Fallback environment used without a blocker explanation",
    "R9-amb": "Webview/Mobile test — URLs not visible in any screenshot",
    "R9-hover": "Page contains hardcoded prod link (product issue, not tester)",
    "R9-nav": "Approved informational page opened on production",
    "R9-susp": "Suspected OCR misread of a preprod URL (not a violation)",
    "R10":    "Defect key pasted in comment instead of attached as trace_link",
    "R11":    "Failed/Passed-With-Issue step has no defect documentation",
    "R12":    "No evidence submitted — no PDF, images, or usable video",
    "R13":    "Marketplace mismatch — test ran on the wrong storefront",
    "R19":    "Execution mode or device family does not match the run",
    "R19-amb": "Execution mode or device family could not be verified",
    "R22":    "Test-account identifier not recorded in the comments",
    "R-runtime": "ARGUS model assessment did not complete reliably",
}

_ARGUS_SEVERITY_LABEL = {
    "high": "High",
    "medium": "Medium",
    "low": "Low",
}


_ARGUS_MODEL_PATTERNS = [
    ("cloud player",                  "Cloud / web player verification missing"),
    ("web player",                    "Cloud / web player verification missing"),
    ("chrome://inspect",              "Missing chrome://inspect evidence"),
    ("diagnostic_tool",                         "Internal tool (Diagnostic tool) verification gap"),
    ("diagnostic",                       "Diagnostic reference / Monetary Amounts mismatch"),
    ("monetary amount",               "Diagnostic reference / Monetary Amounts mismatch"),
    ("reference image",               "Diagnostic reference / Monetary Amounts mismatch"),
    ("pre-order",                     "Pre-order verification missing"),
    ("preorder",                      "Pre-order verification missing"),
    ("price guarantee",               "Pre-order verification missing"),
    ("submit preferences",            "Onboarding preferences verification"),
    ("last position",                 "Last-position-here (LPH) verification"),
    ("lph ",                          "Last-position-here (LPH) verification"),
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
    ("undo",                          "Undo-cancel verification missing"),
    ("reactivate",                    "Undo-cancel verification missing"),
    ("hard cancel",                   "Cancellation flow incomplete"),
    ("cancellation",                  "Cancellation flow incomplete"),
    ("cancel",                        "Cancellation flow incomplete"),
    ("switch membership",             "Membership switch verification missing"),
    ("membership type",               "Membership switch verification missing"),
    ("post-completion",               "Post-completion playback state missing"),
    ("completion of the last",        "Post-completion playback state missing"),
    ("seek bar",                      "Playback state verification missing"),
    ("play button",                   "Playback state verification missing"),
    ("playback",                      "Playback state verification missing"),
    ("chapter",                       "Chapter-list verification missing"),
    ("login",                         "Login flow verification missing"),
    ("sign in",                       "Login flow verification missing"),
    ("sign-in",                       "Login flow verification missing"),
    ("marketplace",                   "Marketplace / locale mismatch"),
    ("storefront",                    "Marketplace / locale mismatch"),
    ("wrong currency",                "Marketplace / locale mismatch"),
    ("example.com.au",                "Marketplace / locale mismatch"),
    ("example.de",                    "Marketplace / locale mismatch"),
    ("duplicate",                     "Duplicate screenshot evidence"),
    ("reused",                        "Duplicate screenshot evidence"),
    ("identical",                     "Duplicate screenshot evidence"),
    ("text mismatch",                 "Expected text mismatch"),
    ("different text",                "Expected text mismatch"),
    ("does not match",                "Expected text mismatch"),
    ("doesn't match",                 "Expected text mismatch"),
    ("wrong outcome",                 "Wrong outcome category"),
    ("free trial",                    "Wrong outcome category"),
    ("contradicts",                   "Direct screenshot contradiction"),
    ("credits will be added",         "Credit-count verification missing"),
    ("credits added",                 "Credit-count verification missing"),
    (" credit",                       "Credit-count verification missing"),
    ("next token",                    "Credit / token verification missing"),
    ("lock symbol",                   "Credit / token verification missing"),
    ("token-purchased",               "Credit / token verification missing"),
    ("token purchase",                "Credit / token verification missing"),
    ("listening to 5%",               "Title duration / playback threshold mismatch"),
    ("5% listening threshold",        "Title duration / playback threshold mismatch"),
    ("duration between",              "Title duration / playback threshold mismatch"),
    ("title used",                    "Title duration / playback threshold mismatch"),
    ("ua string",                     "Wrong device family in DevTools"),
    ("user-agent",                    "Wrong device family in DevTools"),
    ("user agent",                    "Wrong device family in DevTools"),
    ("device toolbar",                "Wrong device family in DevTools"),
    ("sm-s931",                       "Wrong device family in DevTools"),
    ("galaxy s",                      "Wrong device family in DevTools"),
    ("prod.example.com",              "Production URL — environment compliance"),
    ("production host",               "Production URL — environment compliance"),
    ("production url",                "Production URL — environment compliance"),
    ("not a feature-preprod",         "Production URL — environment compliance"),
    ("no 'feature-preprod'",          "Production URL — environment compliance"),
    ("ascolta ora",                   "Locale-specific text mismatch"),
    ("scarica",                       "Locale-specific text mismatch"),
    ("écouter maintenant",            "Locale-specific text mismatch"),
    ("anhören",                       "Locale-specific text mismatch"),
    ("locale",                        "Locale-specific text mismatch"),
    ("translation",                   "Locale-specific text mismatch"),
    ("blocked with no explanatory",   "Blocked step without explanation"),
    ("blocked with the comment",      "Blocked step without explanation"),
    ("marked blocked",                "Blocked step without explanation"),
    ("not applicable without",        "Not Applicable without justification"),
    ("n/a without",                   "Not Applicable without justification"),
    ("only one was returned",         "Partial multi-item action"),
    ("only one of",                   "Partial multi-item action"),
    ("plural",                        "Partial multi-item action"),
    ("titles (plural)",               "Partial multi-item action"),
    ("both removed",                  "Partial multi-item action"),
    ("custom message",                "Required UI element missing"),
    ("listen now button",             "Required UI element missing"),
    ("download button",               "Required UI element missing"),
    ("required cta",                  "Required UI element missing"),
    ("does not show the",             "Required UI element missing"),
    ("not displayed",                 "Required UI element missing"),
    ("express checkout",              "Wrong checkout / payment flow"),
    ("with cash",                     "Wrong checkout / payment flow"),
    ("payment method",                "Wrong checkout / payment flow"),
    ("standard 2-step",               "Wrong checkout / payment flow"),
    ("console shows",                 "DevTools console errors unacknowledged"),
    ("devtools console",              "DevTools console errors unacknowledged"),
    ("red badge",                     "DevTools console errors unacknowledged"),
    ("promotion",                     "Promo / custom message verification missing"),
    ("plan comparison",               "Promo / custom message verification missing"),
    ("comparison page",               "Promo / custom message verification missing"),
    ("alert message",                 "Alert / error text mismatch"),
    ("renewal-failed",                "Alert / error text mismatch"),
    ("unable to renew",               "Alert / error text mismatch"),
    ("404",                           "Error state in screenshot"),
    ("stack trace",                   "Error state in screenshot"),
    ("something went wrong",          "Error state in screenshot"),
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

_ENVIRONMENT_ISSUE_LABELS = frozenset({
    "Marketplace / locale mismatch",
    "Production URL — environment compliance",
    "Locale-specific text mismatch",
})


def _smart_finding_fallback(description: str) -> str:
    """Build a meaningful one-line label from the model's raw description when no `_ARGUS_MODEL_PATTERNS` needle matched."""
    if not description:
        return "Other model finding"
    text = description.strip()
    for sep in (". ", "! ", "? ", "\n"):
        cut = text.find(sep)
        if 20 <= cut <= 200:
            text = text[:cut]
            break
    if len(text) > _ISSUE_LABEL_MAX_CHARS:
        cut = text.rfind(" ", 0, _ISSUE_LABEL_MAX_CHARS - 1)
        text = text[:cut if cut > _ISSUE_LABEL_MAX_CHARS // 2 else _ISSUE_LABEL_MAX_CHARS - 1] + "…"
    return text


def _argus_issue_label(f: dict) -> str:
    """Plain-language one-line label for a single finding."""
    rule = f.get("rule")
    if rule and rule in _ARGUS_RULE_HEADLINES:
        return _ARGUS_RULE_HEADLINES[rule]
    desc_raw = f.get("description") or ""
    desc_lower = desc_raw.lower()
    if "test-data hygiene" in desc_lower and "review" in desc_lower:
        return "Inappropriate review test data"
    category = f.get("category")
    has_explicit_non_environment_category = (
        category in finding_inference.VALID_FINDING_CATEGORIES
        and category != "environment_issue"
    )
    for needle, label in _ARGUS_MODEL_PATTERNS:
        if (
            has_explicit_non_environment_category
            and label in _ENVIRONMENT_ISSUE_LABELS
        ):
            continue
        if needle in desc_lower:
            return label
    return _smart_finding_fallback(desc_raw)


_SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1}


def _issue_chip(
    label: str,
    tester: str,
    audits: int,
    *,
    findings: int | None = None,
    severity: str | None = None,
) -> str:
    """Render one clickable tester-issue filter chip."""
    detail = ""
    if findings is not None and findings != audits:
        detail = f", {findings} findings"
    if severity:
        detail += f", worst severity {severity}"
    severity_dot = (
        f'<i class="argus-issue-sev argus-issue-sev-'
        f'{_html_escape(severity)}" aria-hidden="true"></i>'
        if severity else ""
    )
    return (
        '<button type="button" class="argus-issue-chip" '
        f'data-issue="{_html_escape(label.lower())}" '
        f'data-tester="{_html_escape(tester)}" '
        f'title="Click to show {audits} '
        f'audit{"s" if audits != 1 else ""} for '
        f'{_html_escape(tester)} with this issue{_html_escape(detail)}">'
        f'{severity_dot}{_html_escape(label)} '
        f'<span class="argus-issue-count">{audits}</span></button>'
    )


def _argus_per_tester_summary(
    summaries: list[AuditSummary],
    roster: list[dict] | None = None,
) -> list[dict]:
    """Build the per-tester rollup data for the top-of-report panel."""
    by_tester: dict[str, list[AuditSummary]] = collections.defaultdict(list)
    for s in summaries:
        by_tester[_resolve_tester_label(s.tester) if s.tester else "Unknown"
                  ].append(s)

    audited_by_key = _index_roster_audits(summaries)

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
        complete_group = [
            s for s in group if s.analysis_status != "incomplete"
        ]
        incomplete = len(group) - len(complete_group)
        verdicts = {"pass": 0, "concerns": 0, "fail": 0}
        for s in complete_group:
            v = s.verdict or "unknown"
            if v in verdicts:
                verdicts[v] += 1
        issue_counter: collections.Counter = collections.Counter()
        issue_audits: dict[str, set[str]] = collections.defaultdict(set)
        issue_worst: dict[str, str] = {}
        action_counter: collections.Counter = collections.Counter()
        for s in complete_group:
            for f in s.findings:
                if finding_inference.is_advisory_finding(f):
                    continue
                label = _argus_issue_label(f)
                issue_counter[label] += 1
                issue_audits[label].add(s.key)
                severity = str(f.get("severity") or "low")
                if _SEVERITY_RANK.get(severity, 0) > _SEVERITY_RANK.get(
                    issue_worst.get(label, ""),
                    0,
                ):
                    issue_worst[label] = severity
                action_counter[_finding_action(f)] += 1
        top_issues = issue_counter.most_common(3)
        all_issues = sorted(
            (
                {
                    "label": label,
                    "findings": count,
                    "audits": len(issue_audits[label]),
                    "severity": issue_worst.get(label, "low"),
                }
                for label, count in issue_counter.items()
            ),
            key=lambda row: (
                -_SEVERITY_RANK.get(row["severity"], 0),
                -row["audits"],
                -row["findings"],
                row["label"].lower(),
            ),
        )
        top_actions = [
            (action, action_counter[action])
            for action in _TRIAGE_ACTION_ORDER
            if action_counter[action]
        ][:3]
        high_finding_audits = sum(
            1 for s in complete_group if s.has_high_finding
        )

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
                (
                    "incomplete"
                    if audit is not None
                    and audit.analysis_status == "incomplete"
                    else (audit.verdict or "—") if audit is not None else "—"
                ),
                er.get("assigned_to_name") or er.get("assigned_to"),
            ))

        rows.append({
            "tester": tester,
            "audits": len(complete_group),
            "incomplete": incomplete,
            "executions": len(exec_rows) if roster else len(group),
            "not_audited": not_audited,
            "verdicts": verdicts,
            "high_finding_audits": high_finding_audits,
            "top_issues": top_issues,
            "all_issues": all_issues,
            "top_actions": top_actions,
            "rows": detail_rows,
        })

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
    """Synthetic main-table rows for executions ARGUS has NOT audited."""
    audited_keys = _index_roster_audits(summaries)
    rows: list[str] = []
    extra_testers: set[str] = set()
    idx = start_idx
    for er in sorted(
            roster,
            key=lambda r: ((_resolve_tester_label(
                r.get("executed_by_name") or r.get("executed_by")) or "").lower(),
                r.get("test_case_key") or "")):
        if not folder_coverage.is_executed_status(er.get("status")):
            continue
        jk = _audit_join_key_from_row(er)
        if jk in audited_keys:
            continue
        tc = er.get("test_case_key") or "—"
        mp = er.get("marketplace")
        status = er.get("status") or "—"
        tester = _resolve_tester_label(
            er.get("executed_by_name") or er.get("executed_by"))
        extra_testers.add(tester)
        run_key = er.get("testrun_key") or ""
        run_label = er.get("testrun_name") or run_key
        if run_label and run_key:
            run_url = (f"{_base._JIRA_BASE_URL}/secure/Tests.jspa"
                       f"#/testPlayer/{run_key}")
            run_inner = (f'<a class="argus-testrun-link" '
                         f'href="{_html_escape(run_url)}" target="_blank" '
                         f'rel="noopener">{_html_escape(run_label)}</a>')
        else:
            run_inner = _html_escape(run_label)
        mp_badge = (f'<span class="argus-mp-badge" title="Marketplace">'
                    f'{_html_escape(mp)}</span> ') if mp else ""
        tc_name = er.get("test_case_name")
        tc_link = _jira_browse_anchor(tc) if tc != "—" else _html_escape(tc)
        if tc_name:
            tc_link = f'{tc_link} — {_html_escape(tc_name)}'
        e_key = er.get("e_key")
        key_cell = (f'<span class="argus-key">{_argus_exec_key_link(e_key)}</span>'
                    if e_key else
                    f'<span class="argus-key">{_html_escape(tc)}</span>')
        search_blob = " ".join(filter(None, [
            tc, tc_name or "", e_key or "", tester, run_key, run_label,
            mp or "", status])).lower()
        rows.append(
            f'<tr class="argus-row argus-row-unaudited" data-idx="{idx}" '
            f'data-key="{_html_escape(e_key or tc)}" '
            f'data-run="{_html_escape(_run_id(run_key))}" '
            f'data-day="{_execution_day(er.get("execution_date"))}" '
            f'data-audited="" '
            f'data-csv="{_html_escape(json.dumps(_unaudited_csv_values(er), ensure_ascii=False))}" '
            f'data-verdict="unaudited" '
            'data-analysis-status="not_audited" '
            f'data-status="{_html_escape(status if status != "—" else "")}" '
            f'data-tester="{_html_escape(tester)}" '
            f'data-flagged="false" data-high="false" '
            f'data-findings="0" data-issues="" '
            f'data-actions="" data-categories="" '
            f'data-search="{_html_escape(search_blob)}">'
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
            f'<td>{_argus_status_pill(status)}</td>'
            f'<td><span class="argus-muted">—</span></td>'
            f'<td class="argus-tc">'
            f'<div class="argus-tc-run" '
            f'title="{_html_escape(run_label)}">{mp_badge}{run_inner}</div>'
            f'<div class="argus-tc-name" '
            f'title="{_html_escape(f"{tc} — {tc_name}" if tc_name else tc)}">'
            f'{tc_link}</div></td>'
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
    return finding_inference.derive_finding_title(f)


def _argus_format_test_case(s: AuditSummary) -> str:
    """Compact test-case label for the table row."""
    return s.test_case or ""


def _render_audit_history(exec_dir: Path) -> str:
    """Render a `<details>` block listing prior audits archived under `<exec_dir>/audit.json.history/`."""
    history_dir = exec_dir / "audit.json.history"
    if not history_dir.is_dir():
        return ""
    entries: list[tuple[str, str, int]] = []
    for archived in sorted(history_dir.glob("audit_*.json")):
        try:
            data = json.loads(archived.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        stem = archived.stem
        ts_part = stem[len("audit_"):] if stem.startswith("audit_") else stem
        if "T" in ts_part:
            date_part, time_part = ts_part.split("T", 1)
            display_ts = f"{date_part}T{time_part.replace('-', ':')}Z"
        else:
            display_ts = ts_part
        own_ts = data.get("audited_at")
        if isinstance(own_ts, str) and own_ts:
            display_ts = own_ts
        verdict = str(data.get("overall_verdict") or "unknown")
        n_findings = len(data.get("findings") or [])
        entries.append((display_ts, verdict, n_findings))

    if not entries:
        return ""

    items = "".join(
        f"<li>{_html_escape(ts)} — verdict={_html_escape(verdict_label(v))}, "
        f"{n} finding{'s' if n != 1 else ''}</li>"
        for (ts, v, n) in entries
    )
    return (
        '<details class="argus-audit-history">'
        f'<summary>Previous audits ({len(entries)})</summary>'
        f'<ul>{items}</ul>'
        '</details>'
    )


_RELATED_STATUS_TERMS: dict[str, tuple[str, ...]] = {
    "blocked": ("blocked",),
    "fail": ("fail", "failed", "failing"),
    "in progress": ("in progress",),
    "passed with issue": ("passed with issue", "passed with issues"),
    "not applicable": ("not applicable",),
}


def _argus_related_step_indices(
    finding: dict,
    step_statuses: dict[int, str],
) -> list[int]:
    """Infer related non-passing steps for an execution-level finding."""
    if isinstance(finding.get("step_index"), int):
        return []
    description = str(finding.get("description") or "").lower()
    mentioned_statuses = {
        status
        for status, terms in _RELATED_STATUS_TERMS.items()
        if any(
            re.search(rf"(?<!\w){re.escape(term)}(?!\w)", description)
            for term in terms
        )
    }
    if not mentioned_statuses:
        return []
    return sorted(
        index
        for index, status in step_statuses.items()
        if status.strip().lower() in mentioned_statuses
    )


def _argus_render_finding_card(
    f: dict,
    step_descriptions: dict[int, str],
    step_statuses: dict[int, str],
    out_dir: Path,
    page_images: list[Path],
    step_attachments: dict[int, list[Path]],
) -> str:
    """Render one finding as an auditor-friendly card <div>."""
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
    if finding_inference.is_advisory_finding(f):
        triage_bits.append(("Type", "Advisory"))
    if f.get("_triage_from_model"):
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
    related_steps = _argus_related_step_indices(f, step_statuses)
    loc_parts = []
    if isinstance(page, int):
        loc_parts.append(f"Page {page}")
    if isinstance(step, int):
        loc_parts.append(f"Step {human_step_number(step)}")
    elif related_steps:
        related_numbers = ", ".join(
            str(human_step_number(index)) for index in related_steps
        )
        loc_parts.append(
            f"Steps {related_numbers}" if len(related_steps) > 1
            else f"Step {related_numbers}"
        )
    locator = " · ".join(loc_parts)
    step_description = (
        step_descriptions.get(step, "") if isinstance(step, int) else ""
    )
    step_context_html = (
        '<div class="argus-finding-step">'
        '<span class="argus-finding-step-label">Test step:</span> '
        f'{_html_escape(step_description)}'
        '</div>'
        if step_description else ""
    )
    if not step_context_html and related_steps:
        related_items = "".join(
            '<li>'
            f'<strong>Step {human_step_number(index)}'
            f' · {_html_escape(step_statuses.get(index, "?"))}:</strong> '
            f'{_html_escape(step_descriptions.get(index, ""))}'
            '</li>'
            for index in related_steps
        )
        step_context_html = (
            '<div class="argus-finding-step argus-finding-related-steps">'
            '<span class="argus-finding-step-label">'
            'Affected test steps:</span>'
            f'<ul>{related_items}</ul>'
            '</div>'
        )

    thumb_html = ""
    if isinstance(page, int) and 1 <= page <= len(page_images):
        thumb_html = _image_thumb_html(
            page_images[page - 1], out_dir,
            alt=f"Page {page} — {sev_label}",
        )
    elif isinstance(page, int) and page_images:
        thumb_html = (
            f'<div class="argus-thumb-missing thumb-missing argus-muted">'
            f'(page {page} — image unavailable; execution has '
            f'{len(page_images)} pages)'
            f'</div>'
        )
    elif isinstance(step, int) and step in step_attachments:
        thumb_html = "".join(
            _image_thumb_html(p, out_dir,
                              alt=(
                                  f"Step {human_step_number(step)} attachment"
                              ))
            for p in step_attachments[step]
        )

    thumb_wrap = (f'<div class="argus-thumbs">{thumb_html}</div>'
                  if thumb_html else "")

    return (
        f'<div class="{sev_cls}">'
        f'<div class="argus-finding-head">'
        f'<span class="{sev_dot_cls}" title="{sev_label}"></span>'
        f'<span class="argus-finding-headline">{headline}</span>'
        + (f'<span class="argus-finding-loc">{_html_escape(locator)}</span>'
           if locator else "")
        + '</div>'
        + step_context_html
        + (f'<div class="argus-finding-body">{desc}</div>'
           if desc and desc != headline else "")
        + triage_html
        + thumb_wrap
        + '</div>'
    )


def _argus_render_findings_section(
    s: AuditSummary,
    out_dir: Path,
    page_images: list[Path],
    step_attachments: dict[int, list[Path]],
) -> str:
    """All finding cards for one audit, or the clean-pass fallback."""
    operational = [
        finding for finding in s.findings
        if not finding_inference.is_advisory_finding(finding)
    ]
    advisories = [
        finding for finding in s.findings
        if finding_inference.is_advisory_finding(finding)
    ]
    if not operational and not advisories:
        return (
            '<div class="argus-no-findings">'
            'No findings — execution looks clean.'
            '</div>'
        )

    def render_group(label: str, findings: list[dict]) -> str:
        if not findings:
            return ""
        cards = "".join(
            _argus_render_finding_card(
                finding,
                s.step_descriptions,
                s.step_statuses,
                out_dir,
                page_images,
                step_attachments,
            )
            for finding in findings
        )
        return (
            f'<section class="argus-finding-group">'
            f'<h4>{_html_escape(label)} ({len(findings)})</h4>'
            f'{cards}</section>'
        )

    return (
        render_group("Operational findings", operational)
        + render_group("Advisories", advisories)
    )


def _argus_render_screenshot_stream(
    images: list[Path],
    out_dir: Path,
) -> str:
    """Render the audit-level screenshot disclosure whenever evidence exists."""
    strip = "".join(
        _image_thumb_html(image, out_dir, alt=f"Screenshot {number}")
        for number, image in enumerate(images, start=1)
    )
    if not strip:
        return ""
    count = len(images)
    return (
        '<div class="argus-screenshot-stream">'
        '<details class="argus-thumb-fallback">'
        f'<summary>View {count} screenshot'
        f'{"s" if count != 1 else ""}</summary>'
        f'<div class="argus-thumb-strip">{strip}</div>'
        '</details>'
        '</div>'
    )


def _argus_render_evidence_section(
    s: AuditSummary,
    out_dir: Path,
    page_images: list[Path],
) -> str:
    """`Evidence coverage by step` collapsible, or "" when absent."""
    if not s.evidence_by_step:
        return ""

    def _page_link(p: object, _imgs: list[Path] = page_images) -> str:
        """Render one evidence page ref, hyperlinked to its screenshot when the page image resolves."""
        if (isinstance(p, int) and 1 <= p <= len(_imgs)):
            try:
                rel = _imgs[p - 1].relative_to(out_dir)
            except ValueError:
                return _html_escape(str(p))
            href = _html_escape(str(rel).replace("\\", "/"))
            return f'<a href="{href}" target="_blank">{p}</a>'
        return _html_escape(str(p))

    rows = []
    for row in sorted(
        (r for r in s.evidence_by_step if isinstance(r, dict)),
        key=lambda r: r.get("step_index", 0),
    ):
        step = human_step_number(row.get("step_index"))
        status = str(row.get("status") or "not_assessed").replace("_", " ")
        confidence = row.get("confidence") or "?"
        pages = row.get("pages") or []
        pages_text = (
            ", ".join(_page_link(p) for p in pages)
            if pages else "none"
        )
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
    return (
        '<details class="argus-evidence-coverage" open>'
        '<summary>Evidence coverage by step</summary>'
        '<ul>' + "".join(rows) + '</ul>'
        '</details>'
    )


def _argus_render_detail_panel(
    s: AuditSummary,
    out_dir: Path,
    page_images: list[Path],
    step_attachments: dict[int, list[Path]],
    screenshot_images: list[Path],
) -> str:
    """The expandable per-audit detail panel (findings, evidence, env violations, dismissals, forensic history, audit.md link)."""
    findings_html = _argus_render_findings_section(
        s, out_dir, page_images, step_attachments)
    screenshot_stream_html = _argus_render_screenshot_stream(
        screenshot_images, out_dir)

    env_html = ""
    ec = s.env_check or {}
    urls = ec.get("prod_urls") or []
    heading = "Production URLs detected:"
    contextual_policy = ec.get("contextual_policy")
    if isinstance(contextual_policy, dict):
        urls = []
        heading = "Environment mismatch:"
        if contextual_policy.get("applied") and ec.get("verdict") == "violation":
            urls = sorted(
                set(contextual_policy.get("observed_hosts") or [])
                - set(contextual_policy.get("allowed_hosts") or [])
            )
    if urls:
        env_html = (
            '<div class="argus-env-violation">'
            f'<strong>{heading}</strong> '
            + ", ".join(f"<code>{_html_escape(u)}</code>"
                        for u in urls[:5])
            + (f' <span class="argus-muted">(+{len(urls) - 5} more)</span>'
               if len(urls) > 5 else "")
            + '</div>'
        )

    evidence_html = _argus_render_evidence_section(s, out_dir, page_images)
    if s.review_status == "required":
        evidence_html = (
            '<p><strong>Needs review:</strong> unresolved evidence or model '
            'disagreement remains.</p>' + evidence_html
        )
    if s.requirement_assessments:
        evidence_html += "<h4>Required outcomes</h4><ul>" + "".join(
            "<li><strong>"
            + _html_escape(requirement_label(str(row.get("requirement_id") or "")))
            + ":</strong> " + _html_escape(str(row.get("status") or "").replace("_", " "))
            + " — " + _html_escape(row.get("observation") or "") + "</li>"
            for row in s.requirement_assessments if isinstance(row, dict)
        ) + "</ul>"
    video_html = ""
    if s.video_evidence:
        video_html = (
            '<p><strong>Videos reviewed:</strong> '
            + _html_escape(s.video_evidence.get("videos_reviewed", 0))
            + ' · <strong>Audio verification:</strong> '
            + _html_escape(audio_verification_label(s.video_evidence.get("audio_verdict")))
            + '</p>'
        )
    analysis_html = ""
    if s.analysis_status == "incomplete":
        module_rows = []
        for module, outcome in s.analysis_modules.items():
            if not isinstance(outcome, dict):
                continue
            module_rows.append(
                "<li>"
                f"<strong>{_html_escape(module)}</strong>: "
                f"{_html_escape(outcome.get('status') or 'unknown')}"
                + (
                    f" - {_html_escape(outcome.get('diagnostic'))}"
                    if outcome.get("diagnostic") else ""
                )
                + "</li>"
            )
        analysis_html = (
            '<div class="argus-analysis-incomplete" role="alert">'
            '<strong>Required analysis incomplete.</strong> Completed audit '
            'results, findings, and evidence are shown below. This execution '
            'is excluded from compliance totals until the incomplete module '
            'is retried.'
            + (
                f'<p>{_html_escape(s.analysis_diagnostic)}</p>'
                if s.analysis_diagnostic else ""
            )
            + (
                f'<ul>{"".join(module_rows)}</ul>'
                if module_rows else ""
            )
            + '</div>'
        )

    locator_html = ""
    if s.xml_nodes:
        rows = []
        try:
            execution_relative = s.path.relative_to(out_dir)
        except ValueError:
            execution_relative = None
        for node in s.xml_nodes[:250]:
            label = (
                node.get("resource_id")
                or node.get("accessibility_id")
                or node.get("text")
                or node.get("label")
                or node.get("value")
                or ""
            )
            screenshot = node.get("screenshot")
            screenshot_html = ""
            screenshot_path: Path | None = None
            if (
                execution_relative is not None
                and isinstance(screenshot, str)
                and screenshot
            ):
                screenshot_path = Path(screenshot)
                valid_screenshot = (
                    not screenshot_path.is_absolute()
                    and "\\" not in screenshot
                    and all(
                        part not in {"", ".", ".."}
                        for part in screenshot_path.parts
                    )
                )
            else:
                valid_screenshot = False
            if (
                valid_screenshot
                and execution_relative is not None
                and screenshot_path is not None
            ):
                screenshot_href = (
                    execution_relative / screenshot_path
                ).as_posix()
                screenshot_html = (
                    f' <a href="{_html_escape(screenshot_href)}" '
                    'target="_blank" rel="noopener">screenshot</a>'
                )
            rows.append(
                "<tr>"
                f"<td>{_html_escape(node.get('platform'))}</td>"
                f"<td>{_html_escape(node.get('step_index'))}</td>"
                f"<td>{_html_escape(node.get('element_type'))}</td>"
                f"<td>{_html_escape(label)}</td>"
                f"<td><code>{_html_escape(node.get('xpath'))}</code>"
                f"{screenshot_html}</td>"
                "</tr>"
            )
        locator_html = (
            '<details class="argus-locator-evidence">'
            f'<summary>Locator evidence ({len(s.xml_nodes)} nodes)</summary>'
            '<div class="argus-locator-scroll"><table>'
            '<thead><tr><th>Platform</th><th>Step</th><th>Element</th>'
            '<th>Identifier / text</th><th>Stable XPath</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>'
            + (
                '<p class="argus-muted">Showing the first 250 nodes.</p>'
                if len(s.xml_nodes) > 250 else ""
            )
            + '</details>'
        )

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
                f'<span class="argus-dismissed-tag">Dismissed</span> '
                f'by <strong>{by}</strong>'
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

    try:
        rel_path = s.path.relative_to(out_dir)
    except ValueError:
        rel_path = s.path
    audit_md_href = _html_escape(f"{rel_path}/audit.md")

    history_html = _render_audit_history(s.path)

    return (
        '<div class="argus-detail">'
        + analysis_html
        + dismissed_html
        + env_html
        + screenshot_stream_html
        + '<div class="argus-findings">' + findings_html + '</div>'
        + evidence_html
        + video_html
        + locator_html
        + history_html
        + '<div class="argus-detail-foot">'
        + f'<a href="{audit_md_href}" class="argus-link">'
          f'Open full audit.md →</a>'
        + '</div>'
        + '</div>'
    )


DETAIL_CHUNK = 50
DETAILS_DIRNAME = "_argus_details"


def _detail_cell(
    index: int,
    detail_html: str,
    detail_sink: dict[int, str] | None,
) -> str:
    """Inline a detail panel or leave a slot for lazy chunk loading."""
    if detail_sink is None:
        return f'<td colspan="6">{detail_html}</td>'
    detail_sink[index] = detail_html
    return (
        '<td colspan="6" class="argus-detail-slot" '
        f'data-chunk="{index // DETAIL_CHUNK}"></td>'
    )


def detail_chunk_maps(
    detail_sink: dict[int, str],
) -> dict[str, dict[str, str]]:
    """Group detail panels into stable, page-sized JSON payloads."""
    chunks: dict[int, dict[str, str]] = {}
    for index, detail_html in detail_sink.items():
        chunks.setdefault(
            index // DETAIL_CHUNK,
            {},
        )[str(index)] = detail_html
    return {
        f"{chunk:04d}.json": payload
        for chunk, payload in sorted(chunks.items())
    }


def _argus_render_audit_row(
    s: AuditSummary,
    i: int,
    out_dir: Path,
    *,
    detail_sink: dict[int, str] | None = None,
) -> str:
    """One audit = two <tr>s: the summary row + its hidden detail row."""
    verdict = s.verdict or "unknown"
    verdict_label = _ARGUS_VERDICT_LABELS.get(verdict,
                                               verdict.capitalize())
    verdict_cls = (f"argus-badge argus-verdict-{verdict}"
                   if verdict in ("pass", "concerns", "fail", "incomplete")
                   else "argus-badge argus-verdict-unknown")

    page_images = _resolve_page_images(s.path)
    step_attachments = _resolve_step_attachments(s.path)
    top_attachments = _resolve_top_level_attachments(s.path)
    screenshot_images = _ordered_screenshot_images(
        page_images, top_attachments, step_attachments)

    detail_html = _argus_render_detail_panel(
        s, out_dir, page_images, step_attachments, screenshot_images)

    flagged_attr = "true" if _is_flagged(s) else "false"
    high_attr = "true" if s.has_high_finding else "false"
    search_blob = " ".join(filter(None, [
        s.key, s.tester or "", s.test_case or "",
        s.testrun_name or "", s.marketplace or "",
        s.tester_status or "",
    ])).lower()
    issue_labels = "|".join(
        _argus_issue_label(f).lower()
        for f in s.findings
        if not finding_inference.is_advisory_finding(f)
    )
    action_labels = "|".join(sorted(_finding_actions_for_summary(s)))
    category_labels = "|".join(sorted(_finding_categories_for_summary(s)))

    tester_name = s.tester or "Unknown"
    test_case = _argus_format_test_case(s) or "—"
    testrun_label = s.testrun_name or ""
    if s.marketplace:
        mp_badge = (f'<span class="argus-mp-badge" '
                    f'title="Assigned marketplace">{_html_escape(s.marketplace)}'
                    f'</span> ')
    else:
        mp_badge = ""
    if testrun_label and s.testrun_key:
        run_url = (f"{_base._JIRA_BASE_URL}/secure/Tests.jspa"
                   f"#/testPlayer/{s.testrun_key}")
        testrun_inner = (
            f'<a class="argus-testrun-link" '
            f'href="{_html_escape(run_url)}" target="_blank" '
            f'rel="noopener" '
            f'title="Open test run {_html_escape(s.testrun_key)} '
            f'in Jira Test Player">'
            f'{_html_escape(testrun_label)}</a>'
        )
    elif testrun_label:
        testrun_inner = _html_escape(testrun_label)
    else:
        testrun_inner = ""
    if testrun_inner:
        testrun_html = (f'<div class="argus-tc-run" '
                        f'title="{_html_escape(testrun_label)}">{mp_badge}'
                        f'{testrun_inner}</div>')
    else:
        testrun_html = ""
    if test_case and " — " in test_case:
        tc_key_part, tc_desc_part = test_case.split(" — ", 1)
        tc_name_html = (_jira_browse_anchor(tc_key_part.strip())
                        + " — " + _html_escape(tc_desc_part))
    elif test_case and re.fullmatch(
        r"[A-Z][A-Z0-9_]*-T\d+",
        test_case.strip(),
    ):
        tc_name_html = _jira_browse_anchor(test_case.strip())
    else:
        tc_name_html = _html_escape(test_case)
    tc_cell_html = (testrun_html
                    + f'<div class="argus-tc-name" '
                    f'title="{_html_escape(test_case)}">'
                    + tc_name_html + '</div>')
    if s.policy_revision is not None:
        tc_cell_html += (
            '<div class="argus-muted" title="Published team rules used for this audit">'
            f'Rules v{_html_escape(str(s.policy_revision))}</div>'
        )

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

    row_classes = ["argus-row"]
    if "R12" in s.rules_fired:
        row_classes.append("argus-row-noevd")
    if s.analysis_status == "incomplete":
        row_classes.append("argus-row-incomplete")
    return (
        f'<tr class="{" ".join(row_classes)}" data-idx="{i}" '
        f'tabindex="0" aria-expanded="false" '
        f'data-key="{_html_escape(s.key)}" '
        f'data-run="{_html_escape(_run_id(s.testrun_key))}" '
        f'data-day="{_execution_day(s.execution_date)}" '
        f'data-audited="{_audited_day(s.audited_at)}" '
        f'data-csv="{_html_escape(json.dumps(_audit_csv_values(s), ensure_ascii=False))}" '
        f'data-verdict="{_html_escape(verdict)}" '
        f'data-analysis-status="{_html_escape(s.analysis_status)}" '
        f'data-status="{_html_escape(s.tester_status or "")}" '
        f'data-tester="{_html_escape(tester_name)}" '
                f'data-flagged="{flagged_attr}" '
        f'data-high="{high_attr}" '
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
           'submitted no PDF, images, or usable video">NO EVIDENCE</span>'
           if "R12" in s.rules_fired else '')
        + (' <span class="argus-na-badge" title="Tester marked '
           'this test Not Applicable">NOT APPLICABLE</span>'
           if "R18" in s.rules_fired else '')
        + (' <span class="argus-blocked-badge" title="Execution marked '
           'Blocked — verify the blocker is real">BLOCKED</span>'
           if "R20" in s.rules_fired else '')
        + (' <span class="argus-inprog-badge" title="Execution left '
           'In Progress — never reached a verdict">IN PROGRESS</span>'
           if "R21" in s.rules_fired else '')
        + (' <span class="argus-incomplete-badge" title="Required '
           'analysis did not complete">INCOMPLETE</span>'
           if s.analysis_status == "incomplete" else '')
        + (' <span class="argus-incomplete-badge" title="Unresolved evidence '
           'or model disagreement">NEEDS REVIEW</span>'
           if s.review_status == "required" else '')
        + '</td>'
        f'<td>{_argus_tester_cell(tester_name)}</td>'
        f'<td>{_argus_status_pill(s.tester_status or "")}</td>'
        f'<td><span class="{verdict_cls}">{_html_escape(verdict_label)}</span></td>'
        f'<td class="argus-tc">{tc_cell_html}</td>'
        f'<td class="argus-num">'
        f'<span class="argus-finding-count">{s.findings_count}</span> '
        f'{sev_pills_html}</td>'
        f'</tr>'
        f'<tr class="argus-detail-row hidden" data-detail="{i}">'
        f'{_detail_cell(i, detail_html, detail_sink)}'
        f'</tr>'
    )


def _argus_render_stat_cards(
    total: int,
    verdict_counts: collections.Counter,
    coverage_tile: str = "",
) -> str:
    """Summary tiles that double as filter shortcuts."""
    def share(value: int) -> str:
        return f"{round(100 * value / total)}% of audits" if total else "—"

    def tile(
        action: str,
        value_attr: str,
        title: str,
        label: str,
        value: int,
        value_cls: str,
        description: str,
    ) -> str:
        return (
            f'<button type="button" class="argus-stat" '
            f'data-stat-action="{action}"{value_attr} title="{title}">'
            f'<div class="argus-stat-label">{label}</div>'
            f'<div class="argus-stat-value{value_cls}">{value}</div>'
            f'<span class="metric-description">{description}</span></button>'
        )

    return (
        '<section class="argus-stats">'
        + tile(
            "reset", "", "Show all audits", "Audits", total, "",
            "All audited executions",
        )
        + tile(
            "verdict", ' data-stat-value="pass"',
            "Filter to compliant audits", "Compliant",
            verdict_counts.get("pass", 0), " argus-stat-pass",
            share(verdict_counts.get("pass", 0)),
        )
        + tile(
            "verdict", ' data-stat-value="concerns"',
            "Filter to audits with concerns", "Concerns",
            verdict_counts.get("concerns", 0), " argus-stat-concerns",
            share(verdict_counts.get("concerns", 0)),
        )
        + tile(
            "verdict", ' data-stat-value="fail"',
            "Filter to audits with deviations", "Deviation",
            verdict_counts.get("fail", 0), " argus-stat-fail",
            share(verdict_counts.get("fail", 0)),
        )
        + coverage_tile
        + '</section>'
    )


def _argus_render_coverage_tile(coverage: dict | None) -> str:
    """Execution coverage summary tile opening the coverage drawer."""
    if not coverage:
        return ""
    total = int(coverage.get("items_total") or 0)
    executed = min(total, max(0, int(coverage.get("items_executed") or 0)))
    if total <= 0:
        return ""
    below = sum(
        1
        for row in coverage.get("tester_completion") or []
        if (row.get("completion_pct") or 0) < 100
    )
    percent = round(100 * executed / total)
    return (
        '<button type="button" class="argus-stat cs-coverage-stat" '
        'id="coverage-link" title="Open the coverage panel">'
        '<div class="argus-stat-label">Execution coverage</div>'
        f'<div class="argus-stat-value">{percent}%</div>'
        f'<span class="metric-description">{executed} of {total} executed · '
        f'{below} tester{"s" if below != 1 else ""} below 100%</span></button>'
    )


def _argus_render_no_evidence_panel(summaries: list[AuditSummary]) -> str:
    """Top-of-report R12 banner naming every no-evidence tester + key."""
    no_evidence = [s for s in summaries if "R12" in s.rules_fired]
    if not no_evidence:
        return ""
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
            f'<span class="argus-noevd-keys">{key_chips}</span></li>'
        )
    return (
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


def _argus_render_tester_panel(per_tester: list[dict]) -> str:
    """Per-tester scoreboard panel, or "" when there are no testers."""
    if not per_tester:
        return ""
    tester_rows_html: list[str] = []
    for r in per_tester:
        v = r["verdicts"]
        incomplete = r.get("incomplete", 0)
        verdict_pills = (
            f'<span class="argus-mini-pill argus-mini-pass" '
            f'title="Compliant">{v["pass"]} Compliant</span>'
            f'<span class="argus-mini-pill argus-mini-concerns" '
            f'title="Concerns">{v["concerns"]} Concerns</span>'
            f'<span class="argus-mini-pill argus-mini-fail" '
            f'title="Deviation">{v["fail"]} Deviation</span>'
            + (
                f'<span class="argus-mini-pill argus-mini-incomplete" '
                f'title="Excluded from compliance totals">'
                f'{incomplete} Incomplete</span>'
                if incomplete else ""
            )
        )
        audits_n = r["audits"]
        if audits_n:
            bar_title = (f'{v["pass"]} compliant / {v["concerns"]} concerns'
                         f' / {v["fail"]} deviation of {audits_n} audits')
            bar_segs = "".join(
                f'<i class="argus-tbar-{cls}" '
                f'style="flex-basis:{100.0 * n / audits_n:.1f}%"></i>'
                for cls, n in (("pass", v["pass"]),
                               ("concerns", v["concerns"]),
                               ("fail", v["fail"]))
                if n
            )
            tester_bar = (f'<span class="argus-tester-bar" '
                          f'aria-hidden="true" '
                          f'title="{_html_escape(bar_title)}">'
                          f'{bar_segs}</span>')
        else:
            tester_bar = ""
        all_issues = r.get("all_issues") or [
            {
                "label": label,
                "findings": count,
                "audits": count,
                "severity": None,
            }
            for label, count in r["top_issues"]
        ]
        if r["top_issues"]:
            by_label = {row["label"]: row for row in all_issues}
            compact = " ".join(
                _issue_chip(
                    label,
                    r["tester"],
                    by_label.get(label, {}).get("audits", count),
                    findings=count,
                    severity=by_label.get(label, {}).get("severity"),
                )
                for label, count in r["top_issues"]
            )
            full = "".join(
                f'<li>{_issue_chip(row["label"], r["tester"], row["audits"], findings=row["findings"], severity=row["severity"])}</li>'
                for row in all_issues
            )
            issue_chips = (
                f'<div class="argus-issues-compact">{compact}</div>'
                f'<div class="argus-issues-full">'
                f'<ul class="argus-issue-list">{full}</ul></div>'
            )
        else:
            issue_chips = ('<span class="argus-no-issues">'
                           'No findings — clean</span>')
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
            f'data-tester="{_html_escape(r["tester"])}" '
            f'title="Filter the audits table to '
            f'{_html_escape(r["tester"])}">'
            f'{_html_escape(r["tester"])}</button></td>'
            f'<td class="argus-num">{execs_cell}</td>'
            f'<td class="argus-num">{r["audits"]}</td>'
            f'<td class="argus-num argus-verdict-mini">'
            f'{tester_bar}{verdict_pills}</td>'
            f'<td class="argus-issues-cell">{issue_chips}</td>'
            '<td class="cs-chev"><button type="button" class="cs-expand" '
            'aria-expanded="false" title="Show all issues" '
            f'aria-label="Show all issues for '
            f'{_html_escape(r["tester"])}"></button></td>'
            f'</tr>'
        )
    return (
        '<section class="argus-tester-panel">'
        '<div class="argus-tester-header">'
        '<h2 id="tester-overview">Tester overview</h2>'
        '</div>'
        '<table class="argus-tester-table">'
        '<thead><tr>'
        '<th>Tester</th>'
        '<th class="argus-num">Executions</th>'
        '<th class="argus-num">Audited</th>'
        '<th class="argus-num">Verdicts</th>'
        '<th>Issues</th>'
        '<th></th>'
        '</tr></thead>'
        f'<tbody>{"".join(tester_rows_html)}</tbody>'
        '</table>'
        '</section>'
    )


def _argus_render_filters_bar(
    total: int,
    tester_set: list[str],
    status_set: list[str] | None = None,
    action_set: set[str] | None = None,
    category_set: set[str] | None = None,
    runs: list[_ReportRun] | None = None,
    execution_days: list[str] | None = None,
) -> str:
    """Filters bar for report, triage, and reviewer facets."""
    tester_options = "\n".join(
        f'<option value="{_html_escape(t)}">{_html_escape(t)}</option>'
        for t in tester_set
    )
    status_options = "\n".join(
        f'<option value="{_html_escape(st)}">{_html_escape(st)}</option>'
        for st in (status_set or [])
    )
    action_options = "\n".join(
        f'<option value="{action}">'
        f'{_html_escape(_TRIAGE_ACTION_LABELS[action])}</option>'
        for action in _TRIAGE_ACTION_ORDER
        if action in (action_set or set())
    )
    category_options = "\n".join(
        f'<option value="{category}">'
        f'{_html_escape(_TRIAGE_CATEGORY_LABELS[category])}</option>'
        for category in _TRIAGE_CATEGORY_ORDER
        if category in (category_set or set())
    )
    run_options = "".join(
        f'<option value="{_html_escape(run.key)}">{_html_escape(run.label)}</option>'
        for run in (runs or [])
    )
    day_filter = ""
    if execution_days is not None:
        day_options = "".join(
            f'<option value="{day}">'
            f'{"Date not recorded" if day == "_unknown" else day}</option>'
            for day in execution_days
        )
        day_filter = (
            '<select id="argus-day" class="argus-select" '
            'aria-label="Filter by execution date">'
            f'<option value="">All execution dates</option>{day_options}</select>'
        )
    # Two independent custom date-range pickers, always present in the bar.
    # Execution date filters the tester's Jira calendar date (data-day);
    # audit date filters the UTC day ARGUS ran the audit (data-audited).
    # Each from/to pair is inclusive and composes (AND) with every other
    # filter, exactly like the discrete facet selects. Wrapped in a
    # <details> so the four inputs stay collapsed until a reviewer needs
    # them and don't crowd the bar.
    date_ranges = (
        '<details class="argus-daterange" id="argus-daterange">'
        '<summary class="argus-select argus-daterange-summary" '
        'title="Filter by execution date and/or audit date ranges">'
        'Date ranges</summary>'
        '<div class="argus-daterange-panel">'
        '<div class="argus-daterange-group">'
        '<span class="argus-daterange-label">Execution date</span>'
        '<input type="date" id="argus-exec-from" class="argus-input" '
        'aria-label="Execution date from">'
        '<span class="argus-daterange-sep">to</span>'
        '<input type="date" id="argus-exec-to" class="argus-input" '
        'aria-label="Execution date to">'
        '</div>'
        '<div class="argus-daterange-group">'
        '<span class="argus-daterange-label">Audit date</span>'
        '<input type="date" id="argus-audit-from" class="argus-input" '
        'aria-label="Audit date from">'
        '<span class="argus-daterange-sep">to</span>'
        '<input type="date" id="argus-audit-to" class="argus-input" '
        'aria-label="Audit date to">'
        '</div>'
        '</div>'
        '</details>'
    )
    return (
        '<div class="argus-filters" id="argus-filters">'
        '<select id="argus-run" class="argus-select" aria-label="Filter by test run">'
        f'<option value="">All runs</option>{run_options}</select>'
        '<div class="argus-run-picker" id="argus-run-picker" hidden>'
        '<button type="button" id="argus-run-picker-open" class="argus-select" '
        'aria-expanded="false" aria-controls="argus-run-picker-panel" aria-haspopup="listbox">'
        '<span class="argus-run-picker-prefix">Run:</span> '
        '<span id="argus-run-picker-label">All runs</span>'
        '<span aria-hidden="true" class="cs-caret">▼</span></button>'
        '<div id="argus-run-picker-panel" class="argus-run-picker-panel" hidden>'
        '<input type="search" id="argus-run-picker-search" class="argus-input" '
        'role="combobox" aria-label="Search test runs" aria-autocomplete="list" '
        'aria-expanded="false" aria-controls="argus-run-picker-options" '
        'placeholder="Search run name or key" autocomplete="off">'
        '<ul id="argus-run-picker-options" role="listbox" aria-label="Test runs"></ul>'
        '<p id="argus-run-picker-empty" role="status" hidden>No runs match your search.</p>'
        '</div></div>'
        '<button type="button" id="argus-runs-open" class="cs-btn" '
        'aria-haspopup="dialog" aria-controls="argus-runs-drawer" '
        'aria-expanded="false" hidden>View runs'
        f'<span class="cs-counter">({len(runs or [])})</span></button>'
        f'{day_filter}'
        f'{date_ranges}'
        '<input type="text" id="argus-search" '
        'placeholder="Search key / tester / test case…  (press / to focus)" '
        'class="argus-input">'
        '<select id="argus-verdict" class="argus-select" '
        'title="Filter by verdict">'
        '<option value="">All verdicts</option>'
        '<option value="pass">Compliant</option>'
        '<option value="concerns">Concerns</option>'
        '<option value="fail">Deviation</option>'
        '<option value="incomplete">Incomplete</option>'
        '<option value="unaudited">Not audited</option>'
        '</select>'
        '<select id="argus-status" class="argus-select" '
        'title="Filter by the tester\'s execution status">'
        '<option value="">All statuses</option>'
        f'{status_options}'
        '</select>'
        '<select id="argus-tester" class="argus-select" '
        'title="Filter by tester">'
        '<option value="">All testers</option>'
        f'{tester_options}'
        '</select>'
        '<select id="argus-action" class="argus-select" '
        'title="Filter by requested action">'
        '<option value="">All actions</option>'
        f'{action_options}'
        '</select>'
        '<select id="argus-category" class="argus-select" '
        'title="Filter by finding category">'
        '<option value="">All categories</option>'
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
        '<button type="button" id="argus-expand-all" '
        'class="argus-btn-clear" '
        'title="Expand detail panels for all visible rows">'
        'Expand all</button>'
        '<button type="button" id="argus-collapse-all" '
        'class="argus-btn-clear" '
        'title="Collapse all detail panels">Collapse all</button>'
        '<span id="argus-count" class="argus-muted argus-count">'
        f'{total} of {total}</span>'
        '</div>'
    )


def _render_argus_html_report(
    out_dir: Path,
    summaries: list[AuditSummary],
    variant: str | None = None,
    *,
    detail_sink: dict[int, str] | None = None,
    ui_context: ReportUiContext = DEFAULT_UI_CONTEXT,
) -> str:
    _coverage_data = _load_coverage_json(out_dir)
    _roster = (_coverage_data or {}).get("executions") or []
    runs = _group_report_runs(summaries, _roster)
    group_by_day = ui_context.dashboard_slug == "tablet"
    run_days = {run.key: _group_run_days(run) for run in runs} if group_by_day else {}
    execution_days = sorted(
        {day.key for days in run_days.values() for day in days if day.key != "_unknown"},
        reverse=True,
    )
    if any(day.key == "_unknown" for days in run_days.values() for day in days):
        execution_days.append("_unknown")
    summaries = [summary for run in runs for summary in run.audits]
    execution_total = sum(run.counts["executions"] for run in runs)
    total = len(summaries)
    complete_summaries = [
        s for s in summaries if s.analysis_status != "incomplete"
    ]
    verdict_counts = collections.Counter(
        s.verdict or "unknown" for s in complete_summaries)

    per_tester = _argus_per_tester_summary(summaries, roster=_roster)
    stale_count = sum(1 for s in summaries if _is_stale(s))

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

    audit_rows: list[str] = []
    index = 0
    for run in runs:
        audit_rows.append(_argus_render_run_heading(run))
        for group in run_days[run.key] if group_by_day else [run]:
            if group_by_day:
                audit_rows.append(_argus_render_day_heading(run, group))
            for summary in group.audits:
                audit_rows.append(_argus_render_audit_row(
                    summary, index, out_dir, detail_sink=detail_sink,
                ))
                index += 1
            unaudited_rows, _ = _argus_unaudited_rows(
                group.audits, group.unaudited, start_idx=index)
            audit_rows.extend(unaudited_rows)
            index += len(unaudited_rows)

    stat_cards = _argus_render_stat_cards(
        total,
        verdict_counts,
        _argus_render_coverage_tile(_coverage_data),
    )

    no_evidence_panel = _argus_render_no_evidence_panel(summaries)

    action_queue_panel = ""

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

    tester_panel = _argus_render_tester_panel(per_tester)

    status_names = {
        (s.tester_status or "").strip() for s in summaries
    } | {
        (er.get("status") or "").strip() for er in _roster
    }
    status_names.discard("")
    status_set = sorted(
        status_names,
        key=lambda st: (_ARGUS_STATUS_ATTENTION_RANK.get(
            st.upper().replace("_", " "), 8), st.lower()),
    )

    action_set: set[str] = set()
    category_set: set[str] = set()
    for summary in complete_summaries:
        action_set |= _finding_actions_for_summary(summary)
        category_set |= _finding_categories_for_summary(summary)
    filters_html = _argus_render_filters_bar(
        execution_total,
        tester_set,
        status_set,
        action_set,
        category_set,
        runs,
        execution_days if group_by_day else None,
    )

    def _sort_th(sort_key: str, label: str,
                 extra_cls: str = "", numeric: bool = False) -> str:
        return (
            f'<th data-sort-key="{sort_key}" '
            + ('data-sort-numeric="1" ' if numeric else '')
            + f'aria-sort="none" class="argus-sortable{extra_cls}">'
            f'<button type="button" class="argus-th-btn">'
            f'{label} <span class="argus-sort-indicator"></span>'
            f'</button></th>'
        )
    table_html = (
        '<table class="argus-table" id="argus-table">'
        '<thead><tr>'
        + _sort_th("key", "Execution key")
        + _sort_th("tester", "Tester")
        + _sort_th("status", "Status")
        + _sort_th("verdict", "Verdict")
        + _sort_th("test_case", "Test run / case")
        + _sort_th("findings", "Findings",
                   extra_cls=" argus-num", numeric=True)
        + '</tr></thead>'
        f'<tbody id="argus-tbody">{"".join(audit_rows)}</tbody>'
        '</table>'
    )

    coverage_html = _render_coverage_html(out_dir)
    coverage_button = ""
    trigger = re.search(
        r'<button type="button" class="argus-coverage-trigger[^"]*"'
        r'[^>]*>.*?</button>',
        coverage_html,
        re.S,
    )
    if trigger:
        coverage_html = coverage_html.replace(trigger.group(0), "", 1)
        coverage_button = re.sub(
            r'class="argus-coverage-trigger[^"]*"',
            'class="argus-coverage-trigger cs-btn"',
            trigger.group(0),
            count=1,
        )
        coverage_button = re.sub(
            r'>.*?</button>',
            '>View coverage</button>',
            coverage_button,
            count=1,
            flags=re.S,
        )

    folder_name = out_dir.name or str(out_dir)
    try:
        folder_name = (
            json.loads((out_dir / "_folder_meta.json").read_text())
            .get("folder_name")
            or folder_name
        )
    except (OSError, ValueError):
        pass
    folder_title = _html_escape(folder_name)
    dashboard = _html_escape(ui_context.dashboard_name)
    rendered_at = time.strftime("%B %d, %Y at %H:%M")
    page_head = (
        '<nav class="cs-crumbs" aria-label="Breadcrumbs">'
        f'<a href="../">{dashboard}</a><span class="sep">›</span>'
        '<a href="../">Audit reports</a><span class="sep">›</span>'
        f'<span>{folder_title}</span></nav>'
        '<section class="cs-header"><div>'
        f'<span class="page-eyebrow">{dashboard}</span>'
        f'<h1>{folder_title}</h1>'
        f'<p>Execution compliance report for {execution_total} test execution'
        f'{"s" if execution_total != 1 else ""} across {len(runs)} run'
        f'{"s" if len(runs) != 1 else ""}.</p></div>'
        f'<div class="cs-actions">{coverage_button}'
        '<button type="button" class="cs-btn" id="argus-export-csv" disabled '
        f'data-csv-headers="{_html_escape(json.dumps(_ARGUS_CSV_HEADERS))}" '
        f'data-filename="{_html_escape(out_dir.name or "argus")}.csv" '
        'title="Export executions matching the current filters, across every page">'
        'Export to CSV</button>'
        '<button type="button" class="cs-btn" id="argus-export-all-csv" disabled '
        'title="Export every execution in this report, regardless of filters">'
        'Export all CSV</button></div></section>'
        '<div class="metadata-row">'
        f'<span><strong>Last updated</strong>{rendered_at}</span>'
        + (
            f'<span><strong>Variant</strong>{_html_escape(variant)}</span>'
            if variant else ""
        )
        + '</div>'
    )
    summary_container = (
        '<div class="cs-container argus-summary"><div class="cs-container-header">'
        '<h2>Audit summary</h2></div>'
        f'{stat_cards}</div>'
    )
    audits_container = (
        '<div class="cs-container cs-audits">'
        '<div class="cs-container-header">'
        f'<h2>Executions by run{" and day" if group_by_day else ""}'
        f'<span class="cs-counter">({execution_total})</span></h2>'
        '<nav class="cs-pagination" id="cs-pagination" '
        'aria-label="Pagination"></nav>'
        f'</div>{filters_html}{table_html}'
        '<p id="argus-no-matches" class="argus-no-matches" hidden>'
        'No executions match these filters.</p></div>'
    )

    if execution_total == 0:
        body_main = (
            page_head
            + '<section class="argus-empty cs-container">'
                     '<p>No audits found in this folder.</p>'
                     '</section>'
        )
    else:
        body_main = (
            page_head
            + no_evidence_panel
            + summary_container
            + legacy_panel
            + action_queue_panel
            + f'<div class="content-grid">{audits_container}'
            f'<aside class="right-column">{tester_panel}</aside></div>'
        )

    return (
        '<!DOCTYPE html>\n'
        '<html lang="en" data-theme="light">\n'
        '<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<title>{folder_title} — ARGUS</title>\n'
        + (
            '<meta name="argus-detail-base" '
            'content="__ARGUS_DETAIL_BASE__">\n'
            if detail_sink is not None else ""
        )
        + f'{_cs_head_extra(ui_context)}\n'
        f'{_cs_page_css(ui_context)}'
        '</head>\n'
        '<body>\n'
        f'{_cs_topnav("/", ui_context)}'
        '<div class="application-layout" id="application-layout">'
        f'{_cs_sidenav("../", "reports", True, ui_context)}'
        f'<main class="argus-main main-content">{body_main}</main>'
        '</div>\n'
        f'{coverage_html}'
        + (_argus_render_run_drawer(runs) if execution_total else "")
        + f'<script>{_ARGUS_JS}</script>\n'
        '</body>\n'
        '</html>\n'
    )

_ASSETS_DIR = Path(__file__).parent / "assets"
_ARGUS_CSS = (_ASSETS_DIR / "argus.css").read_text(encoding="utf-8")
_ARGUS_JS = (_ASSETS_DIR / "argus.js").read_text(encoding="utf-8")
_CS_GLOBAL_CSS = (
    _ASSETS_DIR / "base.css"
).read_text(encoding="utf-8")
_CS_TOKENS_LIGHT = (
    _ASSETS_DIR / "tokens.css"
).read_text(encoding="utf-8")
_ARGUS_CS_CSS = (
    _ASSETS_DIR / "dashboard.css"
).read_text(encoding="utf-8")
_APP_MARK = (
    '<svg class="cs-mark" width="34" height="34" viewBox="0 0 34 34" '
    'xmlns="http://www.w3.org/2000/svg" aria-hidden="true">'
    '<rect width="34" height="34" rx="10" fill="#4f46e5"/>'
    '<path d="M6.5 17s4-7 10.5-7 10.5 7 10.5 7-4 7-10.5 7S6.5 17 6.5 17Z" '
    'fill="none" stroke="white" stroke-width="1.8"/>'
    '<circle cx="17" cy="17" r="3.3" fill="white"/></svg>'
)


def _cs_icon(name: str) -> str:
    """Small, self-contained navigation icons with consistent stroke weight."""
    paths = {
        "reports": '<rect x="5" y="3" width="14" height="18" rx="2"/>'
                   '<path d="M9 8h6M9 12h6M9 16h3"/>',
        "teams": '<rect x="3" y="3" width="7" height="7" rx="1.5"/>'
                 '<rect x="14" y="3" width="7" height="7" rx="1.5"/>'
                 '<rect x="3" y="14" width="7" height="7" rx="1.5"/>'
                 '<rect x="14" y="14" width="7" height="7" rx="1.5"/>',
        "rules": '<path d="M4 6h16M4 12h16M4 18h16"/>'
                 '<circle cx="8" cy="6" r="2" fill="var(--argus-surface)"/>'
                 '<circle cx="16" cy="12" r="2" fill="var(--argus-surface)"/>'
                 '<circle cx="10" cy="18" r="2" fill="var(--argus-surface)"/>',
        "coverage": '<circle cx="12" cy="12" r="9"/>'
                    '<path d="m8 12 3 3 5-6"/>',
        "testers": '<circle cx="9" cy="8" r="3"/>'
                   '<path d="M3 21v-3a6 6 0 0 1 12 0v3M17 5a3 3 0 0 1 0 6'
                   'm1 4a5 5 0 0 1 3 4v2"/>',
        "theme": '<path d="M20.5 13A8.5 8.5 0 0 1 11 3.5'
                 ' 8.5 8.5 0 1 0 20.5 13Z"/>',
        "menu": '<path d="M4 6h16M4 12h16M4 18h16"/>',
    }
    return (
        '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="1.7" stroke-linecap="round" '
        f'stroke-linejoin="round" aria-hidden="true">{paths[name]}</svg>'
    )


def _cs_page_css(ui_context: ReportUiContext = DEFAULT_UI_CONTEXT) -> str:
    """Return all dashboard styles in their intended cascade order."""
    font_css = static_assets.font_face_css(
        base_path=ui_context.asset_base_path,
    )
    return (
        f'<style id="argus-legacy">{_ARGUS_CSS}</style>\n'
        f'<style id="argus-fonts">{font_css}</style>\n'
        f'<style id="argus-base">{_CS_GLOBAL_CSS}</style>\n'
        f'<style id="argus-tokens">{_CS_TOKENS_LIGHT}</style>\n'
        f'<style id="argus-theme">{_ARGUS_CS_CSS}</style>\n'
    )


def _cs_head_extra(
    ui_context: ReportUiContext = DEFAULT_UI_CONTEXT,
) -> str:
    """Favicon, font preloads, and pre-paint theme initialization."""
    preloads = static_assets.preload_links(
        base_path=ui_context.asset_base_path,
    )
    return (
        '<link rel="icon" type="image/svg+xml" '
        f'href="data:image/svg+xml;base64,{base64.b64encode(_APP_MARK.encode()).decode()}">'
        f'{preloads}'
        "<script>document.documentElement.classList.add('argus-js');"
        "try{var m=localStorage.getItem('cs.theme');"
        "if(m==='dark')document.documentElement"
        ".setAttribute('data-cs-theme','dark');}catch(e){}</script>"
        f'<script>{(_ASSETS_DIR / "shell.js").read_text()}</script>'
    )


def _team_href(ui_context: ReportUiContext, slug: str) -> str:
    base = ui_context.team_base_path.rstrip("/")
    return (
        f"{base}/{_html_escape(slug)}/"
        if base else f"/{_html_escape(slug)}/"
    )


def _dashboard_href(
    ui_context: ReportUiContext,
    fallback: str,
) -> str:
    if ui_context.dashboard_slug:
        return _team_href(ui_context, ui_context.dashboard_slug)
    return fallback


def _cs_docs_link(ui_context: ReportUiContext) -> str:
    if not ui_context.docs_url:
        return ""
    return (
        '<a class="cs-util cs-util-ext" '
        f'href="{_html_escape(ui_context.docs_url)}" '
        'target="_blank" rel="noopener noreferrer" '
        'title="Open the ARGUS documentation in a new tab">'
        'Documentation<span class="cs-ext-icon" '
        'aria-hidden="true">&#8599;</span></a>'
    )


def _cs_ticket_link(ui_context: ReportUiContext) -> str:
    if not ui_context.ticket_template_url:
        return ""
    return (
        '<div class="nav-divider nav-utility-divider"></div>'
        '<a class="nav-link nav-utility-link" '
        f'href="{_html_escape(ui_context.ticket_template_url)}" '
        'target="_blank" rel="noopener noreferrer" '
        'title="Open the ARGUS issue template in a new tab">'
        'Report Issue'
        '<span class="nav-external" aria-hidden="true">&#8599;</span>'
        '</a>'
    )


def _cs_topnav(
    root: str,
    ui_context: ReportUiContext = DEFAULT_UI_CONTEXT,
) -> str:
    """ARGUS application top navigation."""
    dashboard_href = _dashboard_href(ui_context, root)
    return (
        '<header class="cs-topnav" role="banner">'
        '<div class="cs-topnav-left">'
        '<button type="button" class="cs-nav-toggle" id="cs-nav-toggle" '
        'aria-label="Toggle navigation" aria-expanded="true">'
        f'{_cs_icon("menu")}</button>'
        f'<a class="cs-identity" href="{root}">'
        f'{_APP_MARK}'
        '<span class="cs-wordmark">ARGUS</span></a>'
        '<span class="cs-product-label">Quality workspace</span></div>'
        '<nav class="cs-utils" aria-label="Utilities">'
        f'{_cs_docs_link(ui_context)}'
        f'<a class="cs-util" href="{dashboard_href}">'
        f'{_html_escape(ui_context.dashboard_name)}</a>'
        '<span class="cs-util-sep"></span>'
        '<button type="button" class="cs-util cs-gear" '
        'id="cs-theme-toggle" title="Switch visual mode" '
        f'aria-label="Switch visual mode">{_cs_icon("theme")}</button>'
        '</nav></header>'
    )


def _cs_sidenav(
    root: str,
    active: str,
    on_report: bool,
    ui_context: ReportUiContext = DEFAULT_UI_CONTEXT,
    *,
    hub_home: bool = False,
    rules_enabled: bool | None = None,
) -> str:
    """ARGUS dashboard and team side navigation."""
    def link(
        href: str,
        icon: str,
        label: str,
        is_active: bool,
        extra: str = "",
    ) -> str:
        css_class = "nav-link nav-link-active" if is_active else "nav-link"
        return (
            f'<a class="{css_class}" href="{href}"{extra}>'
            f'<span class="nav-icon" aria-hidden="true">{icon}</span>'
            f'{_html_escape(label)}</a>'
        )

    if hub_home:
        title = "ARGUS"
        items = [link("/", _cs_icon("teams"), "Teams", active == "teams")]
    else:
        title = ui_context.dashboard_name
        items = [link(root, _cs_icon("reports"), "Audit reports", active == "reports")]
        if rules_enabled is None:
            from argus.operations.rule_runtime import enabled
            rules_enabled = enabled(ui_context.dashboard_slug)
        if rules_enabled:
            items.append(link(
                _team_href(ui_context, ui_context.dashboard_slug) + "rules",
                _cs_icon("rules"), "Team rules", active == "rules",
            ))
        if on_report:
            items.append(
                link(
                    "#",
                    _cs_icon("coverage"),
                    "Coverage",
                    active == "coverage",
                    ' id="nav-coverage"',
                )
            )
            items.append(
                link(
                    "#tester-overview",
                    _cs_icon("testers"),
                    "Testers",
                    active == "testers",
                )
            )
    if ui_context.teams:
        items.append(
            '<div class="nav-divider"></div>'
            '<p class="nav-section-label">Teams</p>'
        )
        if not hub_home:
            items.append(link("/", _cs_icon("teams"), "All teams", False))
        for team in ui_context.teams:
            items.append(
                link(
                    _team_href(ui_context, team.slug),
                    _cs_icon("teams"),
                    team.name,
                    (
                        not hub_home
                        and team.slug == ui_context.dashboard_slug
                    ),
                )
            )
    return (
        '<aside class="side-navigation" aria-label="Primary">'
        '<div class="side-navigation-header">'
        '<div><span class="side-navigation-caption">Workspace</span>'
        f'<span class="side-navigation-title">{_html_escape(title)}</span></div>'
        '<button class="nav-collapse" id="nav-collapse" type="button" '
        'aria-label="Collapse navigation">‹</button></div>'
        f'<nav>{"".join(items)}{_cs_ticket_link(ui_context)}</nav></aside>'
    )
