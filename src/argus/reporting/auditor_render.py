"""Markdown rendering for audit results."""
from __future__ import annotations

import json
from typing import Any

from argus.domain import finding_inference
from argus.domain.step_refs import human_step_number
from argus.reporting.verdicts import audio_verification_label, requirement_label, verdict_label


def render_markdown(
    audit: dict[str, Any],
    metadata: dict[str, Any],
    *,
    debug: bool = False,
) -> str:
    """Render a stakeholder-friendly audit.md."""
    results = metadata.get("test_results") or []
    tr = results[0] if results else {}
    executed_by = tr.get("executed_by_name") or tr.get("executed_by") or "?"
    tc_key = tr.get("test_case_key")
    tc_name = tr.get("test_case_name")
    if tc_key and tc_name:
        test_case = f"{tc_key} — {tc_name}"
    else:
        test_case = tc_key or tc_name or "?"

    findings = audit.get("findings") or []
    operational = [
        finding for finding in findings
        if isinstance(finding, dict)
        and not finding_inference.is_advisory_finding(finding)
    ]
    advisories = [
        finding for finding in findings
        if isinstance(finding, dict)
        and finding_inference.is_advisory_finding(finding)
    ]
    verdict = audit.get("overall_verdict", "?")
    counts = _lazy_severity_counts(findings)
    total = sum(counts.values())
    if total:
        parts = [f"{n} {k}" for k, n in counts.items() if n]
        findings_summary = f"{total} finding{'s' if total != 1 else ''} ({', '.join(parts)})"
    else:
        findings_summary = "0 findings"

    tester_status = tr.get("status")
    if not tester_status:
        sid = tr.get("status_id")
        tester_status = f"(status_id={sid})" if sid is not None else "?"

    lines = [
        "# Audit Report",
        "",
        f"- **Auditor verdict:** {verdict_label(verdict)}",
        f"- **Tester status:** {tester_status}",
        f"- **Findings:** {findings_summary}",
        f"- **Operational findings:** {len(operational)}",
        f"- **Advisories:** {len(advisories)}",
        f"- **Test case:** {test_case}",
        f"- **Execution ID:** {tr.get('id', '?')}",
        f"- **Executed by:** {executed_by}",
        f"- **Execution date:** {tr.get('execution_date', '?')}",
        "",
        "## Summary",
        "",
        audit.get("summary", ""),
        "",
    ]

    video = audit.get("_video_evidence")
    policy_version = (audit.get("provenance") or {}).get("policy_revision")
    if policy_version is not None:
        lines[2:2] = [f"- **Team rules version:** {policy_version}"]
    if audit.get("review_status") == "required":
        lines += [
            "**Needs review:** unresolved evidence or model disagreement remains.",
            "",
        ]
    assessments = audit.get("requirement_assessments") or []
    if assessments:
        lines += ["## Required Outcomes", ""]
        for row in assessments:
            lines.append(
                f"- **{requirement_label(str(row.get('requirement_id') or ''))}:** "
                f"{str(row.get('status') or 'not_assessed').replace('_', ' ')} — "
                f"{row.get('observation') or ''}"
            )
        lines.append("")
    if isinstance(video, dict):
        lines += [
            f"- **Videos reviewed:** {video.get('videos_reviewed', 0)}",
            f"- **Audio verification:** {audio_verification_label(video.get('audio_verdict'))}",
            "",
        ]

    if operational:
        lines += ["## Operational Findings", ""]
        severity_order = ["high", "medium", "low"]
        shown_ids: set[int] = set()
        for sev in severity_order:
            for f in operational:
                if f.get("severity") == sev:
                    lines.append(_format_finding(f))
                    shown_ids.add(id(f))
        for f in operational:
            if id(f) not in shown_ids:
                lines.append(_format_finding(f))
        lines.append("")
    elif verdict != "pass":
        lines += ["## Operational Findings", "", "_No confident findings._", ""]

    if advisories:
        lines += ["## Advisories", ""]
        for finding in advisories:
            lines.append(_format_finding(finding))
        lines.append("")

    evidence = audit.get("evidence_by_step") or []
    if evidence:
        lines += ["## Evidence Coverage", ""]
        for row in sorted(
            (r for r in evidence if isinstance(r, dict)),
            key=lambda r: r.get("step_index", 0),
        ):
            step = human_step_number(row.get("step_index"))
            pages = row.get("pages") or []
            pages_text = ", ".join(str(p) for p in pages) if pages else "none"
            status = row.get("status", "not_assessed")
            confidence = row.get("confidence", "?")
            reason = row.get("missing_reason")
            suffix = f" — {reason}" if reason else ""
            lines.append(
                f"- **Step {step}:** {status}; pages: {pages_text}; "
                f"confidence: {confidence}{suffix}"
            )
        lines.append("")

    if debug:
        debug_lines = _render_debug_section(audit)
        if debug_lines:
            lines += debug_lines

    return "\n".join(lines) + "\n"


def _format_finding(f: dict[str, Any]) -> str:
    """Single-line bullet for one finding."""
    sev = f.get("severity", "?")
    page = f.get("page")
    step = f.get("step_index")
    desc = f.get("description", "").strip()
    loc_parts = []
    if isinstance(page, int):
        loc_parts.append(f"page {page}")
    if isinstance(step, int):
        loc_parts.append(f"step {human_step_number(step)}")
    locator = ", ".join(loc_parts)
    meta = []
    if f.get("category"):
        meta.append(str(f["category"]).replace("_", " "))
    if f.get("confidence"):
        meta.append(f"confidence: {f['confidence']}")
    if f.get("action"):
        meta.append(f"action: {str(f['action']).replace('_', ' ')}")
    if finding_inference.is_advisory_finding(f):
        meta.append("advisory")
    if f.get("_severity_adjusted_from"):
        meta.append(f"severity adjusted from {f['_severity_adjusted_from']}")
    if f.get("assessment_status") == "needs_review":
        meta.append("needs review: " + str(f.get("review_reason") or "unresolved evidence"))
    meta_text = f" _({'; '.join(meta)})_" if meta else ""
    if locator:
        return f"- **[{sev}]** {locator}: {desc}{meta_text}"
    return f"- **[{sev}]** {desc}{meta_text}"


def _render_debug_section(audit: dict[str, Any]) -> list[str]:
    """Diagnostics for debug=True: chunk summary/metadata, consensus trace, and schema-validation fallbacks."""
    lines: list[str] = []
    chunk_summary = audit.get("_chunk_summary")
    if chunk_summary:
        lines += ["## Diagnostics — per-chunk summary",
                  "",
                  chunk_summary,
                  ""]

    chunk_diag = audit.get("_chunk_diagnostics")
    if chunk_diag:
        lines += ["## Diagnostics — per-chunk metadata", ""]
        for chunk_name in sorted(chunk_diag.keys()):
            lines.append(f"### {chunk_name}")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(chunk_diag[chunk_name], indent=2))
            lines.append("```")
            lines.append("")

    consensus = audit.get("_consensus")
    if consensus:
        lines += ["## Diagnostics — consensus trace", "", "```json",
                  json.dumps(consensus, indent=2), "```", ""]

    for key in ("_invalid_original", "_invalid_repair", "_raw"):
        if key in audit:
            lines += [f"## Diagnostics — {key}", "", "```",
                      json.dumps(audit[key], indent=2) if not isinstance(audit[key], str)
                      else audit[key],
                      "```", ""]

    return lines


def _lazy_severity_counts(findings):
    """Defer-import shim for auditor._severity_counts (avoids import cycle)."""
    from argus.pipeline.auditor import _severity_counts
    return _severity_counts(findings)
