"""Deterministic workflow-consistency checks over tester metadata.

Pure functions of the extractor's metadata dict; no vision model. Rules:
    R0  rule engine health (status map didn't resolve)
    R1  step=In Progress + no comment
    R2  overall=Pass + step=Fail (contradiction)
    R3  DEPRECATED (Pass + Blocked is acceptable per policy update)
    R4  overall=Fail + no Fail/Blocked step (structural)
    R5  step=Pass but comment contains issue-keywords (heuristic)
    R6  overall=PassedWithIssue + no comment + no traceLinks
    R7  overall=PassedWithIssue + no step=Fail (PWI needs a failing step)
    R8  overall=Blocked + no step=Blocked (structural)
    R10 defect URL pasted in comment instead of trace_links (nudge)
    R11 step=Fail|PWI + no trace_links + no defect ref in comment
    R12 zero evidence — fired by batch._process_key, not here
    R13 marketplace mismatch (testrun-name MP vs URL .exampleapp.<tld>)

Findings: {severity, page, step_index, description, source="rule", rule}.
Conservative: rules short-circuit when fields are None; R0 surfaces the
blind spot when status names didn't resolve. Disable globally by removing
from `_RULES`.
"""
from __future__ import annotations

from typing import Any

# QA display names from extractor.get_project_status_map. Other
# projects with different labels make this module a silent no-op.
STATUS_PASS = "Pass"
STATUS_FAIL = "Fail"
STATUS_BLOCKED = "Blocked"
STATUS_IN_PROGRESS = "In Progress"
STATUS_PASSED_WITH_ISSUE = "Passed With Issue"

# R5 keyword list — substring match on the lowercased comment. Kept short
# and high-confidence so we don't over-fire on idioms like "critical path"
# or "error handling" which mention an issue-word but aren't reporting one.
_ISSUE_KEYWORDS: tuple[str, ...] = (
    "bug",
    "broken",
    "crash",
    "hang",
    "freeze",
    "error",
    "failed",
    "failure",
    "not working",
    "doesn't work",
    "does not work",
    "incorrect",
    "wrong",
    "issue",
)


def _first_result(metadata: dict[str, Any]) -> dict[str, Any] | None:
    """Return the single test_result dict we audit, or None if missing.

    v1 assumes one test result per execution (same assumption as auditor.py).
    """
    results = metadata.get("test_results") or []
    return results[0] if results else None


def _finding(
    *,
    severity: str,
    step_index: int | None,
    rule: str,
    description: str,
) -> dict[str, Any]:
    """Finding dict matching the shape the auditor emits, plus rule tags."""
    return {
        "severity": severity,
        "page": None,  # rule findings are metadata-sourced, no page number
        "step_index": step_index,
        "description": description,
        "source": "rule",
        "rule": rule,
    }


# R0: meta-rule — rule engine couldn't run (status map didn't resolve)
def check_rule_engine_health(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """Fire when the metadata has status_ids but no resolved status names.

    extractor.get_project_status_map is best-effort: if the per-project
    status endpoint errors out (auth hiccup, unknown projectId, HTTP 5xx
    burst), it returns {} and every `status` field in metadata.json ends
    up None while `status_id` is still populated. R1–R6 then short-circuit
    silently because they compare against name constants, and audit.json
    looks falsely clean.

    R0 surfaces this blind spot as a finding so the operator can see that
    the compliance layer did not run. Fires ONCE per execution — the
    condition is global to the metadata, not per-step.
    """
    tr = _first_result(metadata)
    if not tr:
        return []
    # Any resolvable identity: top-level or any step has a numeric status_id
    # but the resolved name is missing. Both must be checked because a
    # partial resolution (top resolved, steps not, or vice versa) is also
    # a sign the rules below cannot run reliably.
    has_unresolved = False
    # Top-level
    if isinstance(tr.get("status_id"), int) and tr.get("status") is None:
        has_unresolved = True
    # Any step
    for s in tr.get("steps") or []:
        if isinstance(s.get("status_id"), int) and s.get("status") is None:
            has_unresolved = True
            break
    if not has_unresolved:
        return []
    # Severity bumped to "high" so the rule-engine-failed signal forces
    # verdict escalation via merge_rule_findings + _finalize_verdict.
    # The previous "medium" severity left audits sitting at "concerns"
    # while R1-R8 were silently bypassed — operator could miss the
    # rule-layer outage entirely. (Beta P5: E178919 case.)
    return [_finding(
        severity="high",
        step_index=None,
        rule="R0",
        description=(
            "Workflow-consistency rules (R1-R8) were not fully evaluated: "
            "Tracker returned numeric status ids but the project status-name "
            "map did not resolve (projectId missing or status endpoint "
            "failed). Re-run the extractor to refresh the mapping, or "
            "treat this audit's workflow-rule section as incomplete."
        ),
    )]


# R1: step still "In Progress" AND no explanatory comment
def check_in_progress_steps(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """Per policy: a step left 'In Progress' is only non-compliant when
    the tester provides no explanatory comment. An In-Progress step that
    carries a non-empty tester comment is treated as intentional (for
    instance: waiting on a dependency, deferred for another tester) and
    does not fire this rule.

    Still medium severity — In Progress is never a finalised status,
    and leaving it without explanation means no one can tell whether
    the step passed, failed, or was abandoned.
    """
    tr = _first_result(metadata)
    if not tr:
        return []
    findings: list[dict[str, Any]] = []
    for s in tr.get("steps") or []:
        if s.get("status") != STATUS_IN_PROGRESS:
            continue
        # Policy: an explanatory comment makes the status acceptable.
        # Whitespace-only comments do not count.
        if (s.get("comment") or "").strip():
            continue
        findings.append(_finding(
            severity="medium",
            step_index=s.get("index"),
            rule="R1",
            description=(
                f"Step {s.get('index')} is marked 'In Progress' with no "
                "explanatory comment. Per policy, In Progress is only "
                "acceptable when the tester documents why (waiting on a "
                "dependency, deferred, etc.). Finalise the status or add "
                "a comment."
            ),
        ))
    return findings


# R2: overall Pass but a step is Fail
def check_fail_step_pass_overall(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """If the overall test is marked Pass but any step is Fail, that's a
    logical contradiction — a failed step should propagate to the overall
    verdict (Fail or at minimum Passed With Issue)."""
    tr = _first_result(metadata)
    if not tr:
        return []
    if tr.get("status") != STATUS_PASS:
        return []
    findings: list[dict[str, Any]] = []
    for s in tr.get("steps") or []:
        if s.get("status") == STATUS_FAIL:
            findings.append(_finding(
                severity="high",
                step_index=s.get("index"),
                rule="R2",
                description=(
                    f"Overall test marked 'Pass' but step {s.get('index')} "
                    "is marked 'Fail'. A failed step should propagate to the "
                    "overall result (Fail or Passed With Issue)."
                ),
            ))
    return findings


# R3: DEPRECATED — overall=Pass AND step=Blocked
# Retained as a function so the historical reading is preserved (and unit-
# testable in isolation) but NOT registered in `_RULES` and therefore
# never runs in production.
#
# Per QA policy (confirmed 2026-05-09): a Blocked step that is not
# a real blocker is acceptable with overall Pass. Only a Blocked step
# that genuinely prevents the core objective should bubble up as
# overall=Blocked. "Real blocker" is a semantic judgement the model
# makes from screenshots; metadata alone cannot distinguish it, so this
# rule's blanket Pass+Blocked flag produced ~39 false positives across
# the first 145-audit corpus.
def check_blocked_step_pass_overall(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """DEPRECATED — see module docstring. Kept for historical tests only."""
    tr = _first_result(metadata)
    if not tr:
        return []
    if tr.get("status") != STATUS_PASS:
        return []
    findings: list[dict[str, Any]] = []
    for s in tr.get("steps") or []:
        if s.get("status") == STATUS_BLOCKED:
            findings.append(_finding(
                severity="medium",
                step_index=s.get("index"),
                rule="R3",
                description=(
                    f"Overall test marked 'Pass' but step {s.get('index')} "
                    "is marked 'Blocked'. A blocked step should propagate to "
                    "the overall result (Blocked or Passed With Issue)."
                ),
            ))
    return findings


# R4: overall Fail but no Fail/Blocked step (logical inconsistency)
def check_fail_overall_without_failing_step(
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    """If the overall test is marked Fail, at least one step must be Fail
    or Blocked to justify the verdict. All-Pass steps with overall Fail
    means either the overall or the per-step statuses are wrong — not a
    policy reading, just arithmetic.

    This rule is the structural mirror of R2. Unlike R2 it's silent when
    no steps exist (empty test — can't enforce a contradiction).
    """
    tr = _first_result(metadata)
    if not tr:
        return []
    if tr.get("status") != STATUS_FAIL:
        return []
    steps = tr.get("steps") or []
    if not steps:
        return []
    # Unresolved step statuses (None) can't be judged — skip to avoid a
    # false positive when R0 has already flagged the map-resolution issue.
    has_any_failing = any(
        s.get("status") in (STATUS_FAIL, STATUS_BLOCKED) for s in steps
    )
    has_any_unresolved = any(s.get("status") is None for s in steps)
    if has_any_failing or has_any_unresolved:
        return []
    return [_finding(
        severity="high",
        step_index=None,
        rule="R4",
        description=(
            "Overall test marked 'Fail' but no step is marked 'Fail' or "
            "'Blocked'. A failed test must have at least one failing or "
            "blocking step to justify the overall verdict — the overall "
            "status or the per-step statuses are inconsistent."
        ),
    )]


# R5: Pass step but tester comment describes an issue
def check_pass_step_with_issue_comment(
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    """A step marked 'Pass' whose tester comment contains issue-keywords
    (bug, broken, error, crash, 'not working', etc.) was probably
    mismarked — the correct status is 'Passed With Issue' or 'Fail'."""
    tr = _first_result(metadata)
    if not tr:
        return []
    findings: list[dict[str, Any]] = []
    for s in tr.get("steps") or []:
        if s.get("status") != STATUS_PASS:
            continue
        comment = (s.get("comment") or "").lower()
        if not comment:
            continue
        matched = [kw for kw in _ISSUE_KEYWORDS if kw in comment]
        if matched:
            findings.append(_finding(
                severity="medium",
                step_index=s.get("index"),
                rule="R5",
                description=(
                    f"Step {s.get('index')} is marked 'Pass' but its tester "
                    f"comment contains issue-language ({', '.join(matched[:3])}"
                    f"{'…' if len(matched) > 3 else ''}). "
                    "If the tester observed a real problem, the status "
                    "should be 'Passed With Issue' or 'Fail' instead of "
                    "'Pass'. Comment: "
                    f'"{s.get("comment")}"'
                ),
            ))
    return findings


# R6: Passed With Issue overall but no comment AND no trace_links
def check_passed_with_issue_has_evidence(
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    """'Passed With Issue' is a downgrade from Pass: the tester saw a
    problem worth documenting but judged the overall flow as acceptable.
    That downgrade is meaningless without documentation — either a comment
    explaining the issue or a linked defect (traceLinks). Neither present
    means the claim is unsupported.

    Fires once per execution (it's an overall-status rule).
    """
    tr = _first_result(metadata)
    if not tr:
        return []
    if tr.get("status") != STATUS_PASSED_WITH_ISSUE:
        return []
    top_comment = (tr.get("comment") or "").strip()
    top_links = tr.get("trace_links") or []
    if top_comment or top_links:
        return []
    # Also give credit for step-level evidence: if any step has a
    # comment or a trace link, the tester did document somewhere.
    # This prevents flagging a tester who wrote detail on the offending
    # step but left the overall comment blank.
    for s in tr.get("steps") or []:
        if (s.get("comment") or "").strip():
            return []
        if s.get("trace_links"):
            return []
    return [_finding(
        severity="medium",
        step_index=None,
        rule="R6",
        description=(
            "Overall test marked 'Passed With Issue' but the tester "
            "provided no explanatory comment and no linked defect "
            "(traceLinks) anywhere in the execution. A 'Passed With Issue' "
            "verdict requires documentation of the issue — either a "
            "comment or a linked bug — otherwise the downgrade is "
            "unsupported."
        ),
    )]


# R16: overall=Fail or Blocked but no comment AND no trace_links
# Parallel to R6 (PWI without documentation). A "Fail" or "Blocked"
# overall verdict is a hard claim that something went wrong; without
# any explanatory text or linked defect anywhere in the execution,
# the claim is unsupported — the same way a PWI without docs is.
#
# Surfaced 2026-05-29 by QA-E183670: tester marked overall=Fail
# AND step 0 = Fail, no comment anywhere, no trace_links anywhere,
# AND no evidence (R12 fired). R12 alone said "no evidence" — but
# the audit was silent on the empty narrative. Operators reviewing
# a flagged audit need both signals: "tester provided no proof" AND
# "tester provided no explanation". They're independent gaps.
#
# Severity: high. Failures are first-class compliance signals; an
# undocumented one is worse than an undocumented PWI (R6 is medium)
# because Fail/Blocked are louder claims about what went wrong.
#
# Same step-level credit as R6: a comment OR trace_link on any step
# excuses the overall-level emptiness. The tester documented somewhere,
# even if the overall comment was left blank.
_R16_TARGET_STATUSES = (STATUS_FAIL, STATUS_BLOCKED)


def check_fail_or_blocked_has_documentation(
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    """R16: overall=Fail or Blocked must carry SOME narrative.

    Either a top-level comment, OR top-level traceLinks, OR a comment/
    traceLink on at least one step. If none of those exist the verdict
    is unsupported and the tester needs to add documentation before
    the audit can be considered complete.

    Pure metadata function. Fires alongside R12 in the no-evidence
    case — the two findings are independent: R12 says "no proof",
    R16 says "no explanation".
    """
    tr = _first_result(metadata)
    if not tr:
        return []
    status = tr.get("status")
    if status not in _R16_TARGET_STATUSES:
        return []
    top_comment = (tr.get("comment") or "").strip()
    top_links = tr.get("trace_links") or []
    if top_comment or top_links:
        return []
    # Step-level credit: any comment or trace_link anywhere = documented.
    for s in tr.get("steps") or []:
        if (s.get("comment") or "").strip():
            return []
        if s.get("trace_links"):
            return []
    return [_finding(
        severity="high",
        step_index=None,
        rule="R16",
        description=(
            f"Overall test marked '{status}' but the tester provided "
            "no explanatory comment and no linked defect (traceLinks) "
            "anywhere in the execution — neither at the overall level "
            "nor on any step. A Fail or Blocked verdict requires "
            "documentation: either a comment describing what went wrong, "
            "or a linked bug ticket. Without either, the claim is "
            "unsupported. Tester must add a comment or attach a "
            "traceLink before the audit can be considered complete."
        ),
    )]


# R7: Passed With Issue overall but no step is Fail
def check_passed_with_issue_requires_fail_step(
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    """Per policy (confirmed 2026-05-09): "only fail steps should be
    passed with issue." A Passed With Issue overall is the correct
    downgrade when a step failed but did not prevent the core objective.
    Blocked steps do NOT qualify for PWI — Blocked-but-not-real-blocker
    should resolve to Pass, and Blocked-that-is-real-blocker should
    resolve to Blocked overall. R7 fires when overall=PWI but no step
    has status=Fail.

    Silent when step statuses are unresolved (R0 handles that case).
    """
    tr = _first_result(metadata)
    if not tr:
        return []
    if tr.get("status") != STATUS_PASSED_WITH_ISSUE:
        return []
    steps = tr.get("steps") or []
    if not steps:
        return []
    if any(s.get("status") is None for s in steps):
        return []
    if any(s.get("status") == STATUS_FAIL for s in steps):
        return []
    return [_finding(
        severity="medium",
        step_index=None,
        rule="R7",
        description=(
            "Overall test marked 'Passed With Issue' but no step is "
            "marked 'Fail'. Per policy, Passed With Issue is only the "
            "correct overall when a step failed without preventing the "
            "core objective — Blocked steps do not qualify. Change the "
            "overall to Pass (if the issue is non-blocking) or to "
            "Blocked (if it really blocks), or mark the offending step "
            "as Fail."
        ),
    )]


# R8: Blocked overall but no step is Blocked
def check_blocked_overall_requires_blocked_step(
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    """Per policy (confirmed 2026-05-09): "if the overall status is
    blocked then the steps must have a blocked." An overall Blocked
    verdict that contains no Blocked step is structurally inconsistent
    — there is no real blocker recorded.

    High severity because it's a structural contradiction the tester
    must resolve before submission.
    """
    tr = _first_result(metadata)
    if not tr:
        return []
    if tr.get("status") != STATUS_BLOCKED:
        return []
    steps = tr.get("steps") or []
    if not steps:
        return []
    if any(s.get("status") is None for s in steps):
        return []
    if any(s.get("status") == STATUS_BLOCKED for s in steps):
        return []
    return [_finding(
        severity="high",
        step_index=None,
        rule="R8",
        description=(
            "Overall test marked 'Blocked' but no step is marked "
            "'Blocked'. Per policy, an overall Blocked verdict requires "
            "at least one step flagged as the real blocker. Mark the "
            "blocking step as Blocked, or change the overall to match "
            "the actual step statuses."
        ),
    )]


# R10: defect ref pasted in comment text instead of attached as trace_link
import re as _re

_DEFECT_URL_RE = _re.compile(
    r"(?i)\b(?:issues\.example\.com|tracker\.example\.com|"
    r"tickets\.example\.com)/[A-Z0-9-]+",
)
_DEFECT_KEY_PREFIXES = ("QA-", "EXAMPLEAPP-", "OFFERS-")
# Matches both single-segment (QA-12345) and multi-segment
# (QA-BUG-12345, EXAMPLEAPP-FEAT-99) defect keys.
_DEFECT_KEY_RE = _re.compile(r"\b[A-Z][A-Z0-9]+(?:-[A-Z][A-Z0-9]*)*-\d+\b")


# Step statuses that "admit an issue" — they require defect documentation
# per R11. Both Fail and Passed With Issue qualify: the tester explicitly
# acknowledged something went wrong (in PWI's case: it broke but didn't
# block the core flow). Plain Pass is excluded — no issue to document.
# Blocked is excluded — that's a non-execution state, not an admitted
# issue (and may legitimately be a non-blocker per R3-deprecation policy).
# In Progress is excluded — R1 handles "abandoned with no comment".
_R11_ISSUE_STATUSES = (STATUS_FAIL, STATUS_PASSED_WITH_ISSUE)


def check_fail_step_requires_defect_ref(
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    """R11: a step that admits an issue must carry SOME form of defect
    documentation.

    Triggering statuses (Per QA lead, 2026-05-23):
      * Fail              — tester explicitly failed the step
      * Passed With Issue — tester acknowledged a problem but the step
                            still produced its expected outcome

    Either status requires the tester to point at where the issue is
    tracked. Two acceptable forms:
      (a) tag the issue via the step's `trace_links` field (a real
          attached defect — the canonical path), OR
      (b) paste the ticket / defect ref into the step `comment` (URL
          like issues.example.com/D... or a defect key like
          QA-BUG-12345).

    A Fail or PWI step with NO trace_links AND NO defect ref in the
    comment is an UNDOCUMENTED ISSUE — the audit reader can't tell what
    broke or where it's tracked. Medium severity: not severe enough to
    fail the audit on its own, but a real compliance gap that QA leads
    will rework before sign-off.

    Why both Fail and PWI: a tester who marks PWI is admitting "yes
    something was off, but the test still effectively passed". That
    admission still has to point somewhere — otherwise PWI becomes a
    way to bypass R11's Fail-must-be-documented rule by mis-classifying
    a real failure as "minor issue, moving on".

    Silent on Pass (no issue admitted), Blocked (non-execution state,
    R3 deprecated), and In Progress (R1's territory).

    Companion to R6 (which checks PWI at the EXECUTION level for any
    documentation anywhere) and R10 (which nudges the tester to also
    use trace_links when they only pasted in the comment). R11 is the
    stricter per-step gate.

    Multi-fire: emits one finding per offending step (so a test with
    3 undocumented Fail/PWI steps produces 3 R11 findings).
    """
    tr = _first_result(metadata)
    if not tr:
        return []
    findings: list[dict[str, Any]] = []
    for s in tr.get("steps") or []:
        status = s.get("status")
        if status not in _R11_ISSUE_STATUSES:
            continue
        trace_links = s.get("trace_links") or []
        if trace_links:
            continue  # form (a): canonical path satisfied
        comment = s.get("comment") or ""
        urls = _DEFECT_URL_RE.findall(comment)
        keys = [
            k for k in _DEFECT_KEY_RE.findall(comment)
            if k.startswith(_DEFECT_KEY_PREFIXES)
        ]
        if urls or keys:
            continue  # form (b): comment carries a defect ref (R10 will
                       # nudge separately to also attach via trace_links)
        # Status-specific wording so the finding is actionable.
        if status == STATUS_FAIL:
            kind = "marked Fail"
            consequence = "the failure is undocumented"
        else:  # STATUS_PASSED_WITH_ISSUE
            kind = "marked 'Passed With Issue'"
            consequence = (
                "the acknowledged issue is undocumented (and PWI without "
                "a tracked defect can mask a real failure)"
            )
        findings.append(_finding(
            severity="medium",
            step_index=s.get("index"),
            rule="R11",
            description=(
                f"Step {s.get('index')} is {kind} but has neither a "
                "linked defect (trace_links) nor a defect reference in "
                "the comment. Per policy, any step that admits an issue "
                "must point at where it's tracked — attach the ticket "
                "via trace_links or paste the ticket URL / defect key "
                "(QA-BUG-NNN, issues.example.com/D...) into the "
                f"step comment. Otherwise {consequence} and reviewers "
                "can't tell what broke."
            ),
        ))
    return findings


def check_defect_refs_in_comment_text(
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    """R10: tester pasted a defect ref into a step COMMENT instead of
    attaching it via traceLinks.

    R6 only inspects `trace_links`. Testers commonly paste private
    ExampleCompany ticket URLs (issues.example.com/D..., issues.example.com/...) or
    Tracker keys (QA-BUG-12345) directly into the rich-text comment
    field. R6 then sees no defect reference and downgrades the audit's
    completeness signal incorrectly — and downstream report tooling
    can't link the comment to a tracked bug.

    Low severity: this is a workflow nudge, not a compliance violation.
    Fires per-step (so a single execution can produce multiple R10
    findings if the tester pasted refs across several steps). Silent
    when the step already has trace_links set — the tester did the
    right thing, the comment is just additional context.
    """
    tr = _first_result(metadata)
    if not tr:
        return []
    findings: list[dict[str, Any]] = []
    for s in tr.get("steps") or []:
        comment = s.get("comment") or ""
        if not comment:
            continue
        if s.get("trace_links"):
            continue
        urls = _DEFECT_URL_RE.findall(comment)
        keys = [
            k for k in _DEFECT_KEY_RE.findall(comment)
            if k.startswith(_DEFECT_KEY_PREFIXES)
        ]
        if urls or keys:
            ref = (urls + keys)[0]
            findings.append(_finding(
                severity="low",
                step_index=s.get("index"),
                rule="R10",
                description=(
                    f"Step {s.get('index')} comment references defect "
                    f"({ref}) but it is not attached as a trace_link. "
                    "Reviewers and downstream R6 checks won't see the "
                    "link without it — please attach via the test "
                    "result's trace_links field too."
                ),
            ))
    return findings


# R13: marketplace mismatch (env_check-aware, not in _RULES tuple)
# R13 needs the audit's env_check output to compare assigned MP vs the
# .exampleapp.<tld> in observed preprod URLs. Unlike R0-R11 (pure metadata)
# it is NOT included in the `_RULES` tuple — `auditor.run` calls it
# explicitly after env_check populates audit["env_check"].

# Maps a .exampleapp.<tld> suffix back to its marketplace code. Mirror of
# extractor._MARKETPLACE_TLD; kept here as a private constant so this
# module has no import dependency on extractor.py (avoids a cycle —
# extractor already imports config, and we want workflow_rules
# importable from anywhere).
_TLD_TO_MARKETPLACE = {
    ".com.au": "AU",
    ".com.br": "BR",
    ".co.uk": "UK",
    ".co.jp": "JP",
    ".com": "US",
    ".de": "DE",
    ".fr": "FR",
    ".es": "ES",
    ".it": "IT",
    ".ca": "CA",
    ".in": "IN",
}
# Order matters for substring matching: we test the longer suffixes
# first so ".com" doesn't shadow ".com.au" or ".com.br".
_TLD_BY_SPECIFICITY = sorted(_TLD_TO_MARKETPLACE.keys(),
                             key=lambda t: -len(t))


def _detect_marketplace_from_url(url: str) -> str | None:
    """Return the MP code for the .exampleapp.<tld> in `url`, or None."""
    s = (url or "").lower()
    if "exampleapp" not in s:
        return None
    for tld in _TLD_BY_SPECIFICITY:
        if f"exampleapp{tld}" in s:
            return _TLD_TO_MARKETPLACE[tld]
    return None


def check_marketplace_match(
    metadata: dict[str, Any],
    audit: dict[str, Any],
) -> list[dict[str, Any]]:
    """R13: deterministic marketplace-mismatch finding.

    Compares the assigned marketplace (parsed from the testrun name
    by extractor.parse_marketplace_from_testrun_name and recorded as
    `metadata.test_results[0].marketplace`) against the .exampleapp.<tld>
    detected in the audit's env_check `preprod_urls` list.

    Fires HIGH when:
      (a) `marketplace` is set on the test result (parser found a
          recognised MP token in the testrun name), AND
      (b) at least one preprod URL has a .exampleapp.<tld> we can map
          back to an MP code, AND
      (c) all detected MP codes from URLs are NON-EMPTY but DIFFERENT
          from the assigned MP.
      (We require ALL URLs to disagree, not just one — a mixed-MP
      execution is technically possible if the test has cross-MP
      verification steps, and we want to be conservative.)

    Silent when:
      * `marketplace` is None on metadata (testrun name didn't yield
        a recognised token — rare; we don't punish the tester for a
        parser miss)
      * env_check found 0 preprod URLs (R9-amb already covers that)
      * Any preprod URL agrees with the assigned MP (cross-MP test
        scenario; conservative non-flag)

    Pure function — no I/O. Designed to be called from auditor.run
    after env_check populates audit["env_check"].
    """
    tr = _first_result(metadata)
    if not tr:
        return []
    assigned_mp = tr.get("marketplace")
    if not assigned_mp:
        return []
    ec = audit.get("env_check") or {}
    preprod_urls = ec.get("preprod_urls") or []
    if not preprod_urls:
        return []
    detected = []
    for url in preprod_urls:
        mp = _detect_marketplace_from_url(url)
        if mp:
            detected.append(mp)
    if not detected:
        return []  # could happen if URLs are present but on a TLD we
                   # don't have in the map — leave the LLM to call it
    # Conservative: only fire when EVERY detected MP differs from the
    # assigned one. If even one URL agrees, accept the test as
    # multi-MP cross-verification.
    if any(mp == assigned_mp for mp in detected):
        return []
    detected_sample = sorted(set(detected))
    detected_str = ", ".join(detected_sample)
    return [_finding(
        severity="high",
        step_index=None,
        rule="R13",
        description=(
            f"Marketplace mismatch detected by deterministic check: this "
            f"test was assigned to the {assigned_mp} marketplace (per "
            f"the testrun name), but every preprod URL detected in the "
            f"screenshots resolves to a different storefront "
            f"({detected_str}). The tester appears to have executed the "
            f"test against the wrong marketplace's storefront — wrong "
            f"domain, wrong currency, wrong locale-specific surfaces. "
            f"Re-run on {assigned_mp} or, if this test is intended to "
            f"be cross-marketplace, update the testrun assignment."
        ),
    )]


# R14: status-streak break — history-aware rule
# Unlike R0-R13 (pure functions of one execution's metadata), R14 needs
# CROSS-EXECUTION context: "is THIS test case's current status
# suspiciously different from its long-running history?" That history
# lives in history_index.HistoryIndex, populated from every audited
# E-key under output/. R14 takes the index as an argument and lives
# OUTSIDE the _RULES aggregator (which is metadata-only); the
# auditor.py post-audit pipeline calls it explicitly, same as R13.
#
# Two sub-rules:
#   R14a: Pass after persistent Blocked. The test case has been
#         consistently Blocked (>= threshold of last `window`
#         executions in this marketplace) but is now Pass. Suspicious:
#         did the tester actually unblock and re-execute, or did
#         they just flip the status without doing the work?
#   R14b: Blocked / Not Applicable after persistent Pass. The test
#         case has been consistently Pass but is now Blocked or N/A.
#         Suspicious: what regressed? Did the test break? Was
#         coverage dropped silently?
#
# Severity:
#   - Default: medium. Streak-break alone is suspicious but not
#     proof of misbehaviour — sometimes a test legitimately becomes
#     blocked, or a long-blocked test legitimately gets unblocked.
#   - Escalates to high when combined with absent context: empty
#     tester comment AND no traceLinks. The tester's claim that
#     "things changed" needs documentation; without any, the
#     change-of-status is unsupported.
#
# Skipped silently when:
#   - History has fewer than 5 entries — too few to call anything a
#     "streak". Statistical floor; not enough data to compare against.
#   - Current status equals the streak status — by definition no break.
#   - The current execution itself is included in history → excluded
#     via exclude_e_key so it doesn't dilute the streak it's
#     supposed to break.
#
# Per-marketplace scope: T44880's BR history is independent of its US
# history (mirrors how testers actually work). The history index keys
# by (test_case_key, marketplace) for exactly this reason.

# Streak-comparison statuses. We treat Blocked and Not Applicable as
# semantically equivalent for "test wasn't actually executed" — both
# mean the tester didn't perform the steps. This means R14b fires for
# either Blocked-or-N/A after Pass streak, and the streak-detection
# logic in history_index.is_streak treats them as the same canonical
# bucket via _STATUS_ALIASES.
_BLOCKED_LIKE_STATUSES = frozenset({"Blocked", "Not Applicable"})


def check_streak_break(
    metadata: dict[str, Any],
    history_idx: Any,  # history_index.HistoryIndex (typed Any to avoid
                       # circular import at module load time)
) -> list[dict[str, Any]]:
    """Fire when this test case's current status breaks a long streak.

    Two trigger paths (R14a, R14b). Each independent — both can fire
    on the same execution if the streak goes from Blocked to Fail
    (Fail is neither Blocked-like nor Pass, so neither rule fires;
    the function only fires on transitions INTO Pass or INTO Blocked-
    like).

    Skips when the current execution is itself the only history entry
    (fresh corpus, just-added folder) — that's not a streak break,
    that's the start of a streak.
    """
    tr = _first_result(metadata)
    if not tr:
        return []
    tc_key = tr.get("test_case_key")
    current_status = tr.get("status")
    current_e_key = (
        tr.get("id") and f"QA-E{tr['id']}"
    ) or None
    # Prefer the explicit testresult key if metadata has it (extractor
    # writes it as testrun_key when E-keys are involved). The robust
    # path is: take whichever key is in the parent dir of metadata.json,
    # but here we only have the dict — so derive from tr.id. R14 still
    # works without it; exclude_e_key=None just keeps history intact.
    mp = tr.get("marketplace")
    if not tc_key or not current_status:
        return []

    # Find the current execution's E-key by looking it up in history —
    # the entry whose execution_date matches tr.execution_date is the
    # current one. Cheaper than threading e_key through metadata.
    current_date = tr.get("execution_date")

    history = history_idx.get_history(tc_key, mp)
    if len(history) < 5:
        return []

    # Identify which history entry IS the current execution so we can
    # exclude it from the streak window. Match on execution_date —
    # within a (tc_key, mp) bucket that's a unique identifier.
    exclude_key: str | None = None
    if current_date:
        for entry in history:
            if entry.date == current_date:
                exclude_key = entry.e_key
                break

    # Local import to avoid circular dependency: history_index doesn't
    # import workflow_rules, but at module load time importing it from
    # the top of this file would create a chain via auditor.py.
    from history_index import HistoryIndex

    findings: list[dict[str, Any]] = []
    canon_current = current_status.strip()

    # R14a: Pass after persistent Blocked
    if canon_current == STATUS_PASS:
        # Only fires when the streak was a Blocked-like status. Both
        # "Blocked" and "Not Applicable" qualify — same compliance
        # category for our purposes (test not executed).
        for streak_status in _BLOCKED_LIKE_STATUSES:
            if HistoryIndex.is_streak(history, streak_status,
                                      exclude_e_key=exclude_key):
                comment = (tr.get("comment") or "").strip()
                trace_links = tr.get("trace_links") or []
                escalate = not comment and not trace_links
                severity = "high" if escalate else "medium"
                findings.append(_finding(
                    severity=severity,
                    step_index=None,
                    rule="R14a",
                    description=(
                        f"Status streak break: test case {tc_key} "
                        f"(marketplace={mp or 'unscoped'}) was "
                        f"{streak_status} in at least 8 of the last 10 "
                        f"executions but this run is marked Pass. "
                        + ("No tester comment or defect link explains "
                           "the change — verify the test was actually "
                           "unblocked and executed, not just re-marked."
                           if escalate else
                           "Verify the test was actually unblocked and "
                           "executed, not just re-marked.")
                    ),
                ))
                break  # only one R14a finding per execution

    # R14b: Blocked / N-A after persistent Pass
    if canon_current in _BLOCKED_LIKE_STATUSES:
        if HistoryIndex.is_streak(history, STATUS_PASS,
                                  exclude_e_key=exclude_key):
            comment = (tr.get("comment") or "").strip()
            trace_links = tr.get("trace_links") or []
            escalate = not comment and not trace_links
            severity = "high" if escalate else "medium"
            findings.append(_finding(
                severity=severity,
                step_index=None,
                rule="R14b",
                description=(
                    f"Status streak break: test case {tc_key} "
                    f"(marketplace={mp or 'unscoped'}) was Pass in at "
                    f"least 8 of the last 10 executions but this run "
                    f"is marked {canon_current}. "
                    + ("No tester comment or defect link explains "
                       "the change — verify what regressed and "
                       "whether the change of coverage is intentional."
                       if escalate else
                       "Verify what regressed and whether the change "
                       "of coverage is intentional.")
                ),
            ))

    return findings


# R17: immediate prior execution was performed, current is N/A or Blocked
# R14b (streak break) needs >=8 of 10 prior Pass executions to fire —
# strong evidence but slow. R17 catches the same anti-pattern at the
# very first instance: a test case that was Pass or Fail OR Passed With
# Issue in its IMMEDIATE prior execution but is now marked Not Applicable
# (or Blocked).
#
# Real-world scenario this surfaces:
#   * Sprint N: Rishekesavan executes T44880/BR, marks it Pass.
#   * Sprint N+1: Rishekesavan finds T44880/BR hard to set up, marks
#     it "Not Applicable" so it disappears from the queue.
#   * R14b can't fire — only 2 prior entries, threshold is 8/10.
#   * R17 fires immediately: previous run executed it, current call
#     of N/A is suspicious without explanation.
#
# Per-marketplace scope (same as R14): T44880/BR's history is independent
# of T44880/US's. Otherwise a tester legitimately running a CA test for
# the first time after a US run would false-positive.
#
# Severity policy:
#   * high   — current execution has no comment AND no traceLinks
#              anywhere. Tester provided zero explanation for the
#              "this doesn't apply now" claim.
#   * medium — current has a comment OR traceLink. Reviewer should
#              still verify but the tester documented something.
#
# Skip cases (silent, by design):
#   * History has only the current entry (no prior to compare against).
#   * Current status is not in the N/A-like set (R17 only triggers on
#     transitions INTO N/A or Blocked).
#   * Immediate prior was itself N/A or Blocked or InProgress — that
#     means the test wasn't actually executed in the prior sprint
#     either, so no regression vs the previous tester.
#
# Same statuses R14 considers "executed" for streak purposes:
_R17_PRIOR_EXECUTED_STATUSES = frozenset({
    "Pass",
    "Fail",
    "Passed With Issue",
    "PassedWithIssue",
})

# What the CURRENT run must be marked as for R17 to even consider firing.
# Both Blocked and N/A are "didn't perform" markers; only one can be
# the canonical name in any given test-management install but R14's
# _STATUS_ALIASES handles the synonyms — we mirror that here.
_R17_CURRENT_NA_STATUSES = frozenset({
    "Not Applicable",
    "N/A",
    "Blocked",
})


def check_immediate_prior_executed(
    metadata: dict[str, Any],
    history_idx: Any,  # history_index.HistoryIndex
) -> list[dict[str, Any]]:
    """R17: tester regressed the test from executed → N/A in one sprint.

    Companion to R14b (which requires a streak). R17 catches the
    same anti-pattern on the FIRST sprint it appears.

    Pure metadata + history function. No LLM Runtime, no Tracker calls — the
    history index is in-process state populated at audit time.
    """
    tr = _first_result(metadata)
    if not tr:
        return []
    tc_key = tr.get("test_case_key")
    current_status = (tr.get("status") or "").strip()
    mp = tr.get("marketplace")
    if not tc_key or not current_status:
        return []
    if current_status not in _R17_CURRENT_NA_STATUSES:
        return []

    history = history_idx.get_history(tc_key, mp)
    if not history:
        # Empty history → no prior to compare. The "need at least one
        # prior" minimum is enforced AFTER excluding the current
        # execution below, since a 1-entry history might just be the
        # current run that the index already saw.
        return []

    current_date = tr.get("execution_date")
    # Find the immediate prior entry: skip past the current execution
    # if it's already in history (incremental indexer might have
    # picked it up before R17 ran), then take the next one.
    prior = None
    for entry in history:
        if current_date and entry.date == current_date:
            continue
        prior = entry
        break
    if prior is None:
        return []

    if prior.status not in _R17_PRIOR_EXECUTED_STATUSES:
        return []

    comment = (tr.get("comment") or "").strip()
    trace_links = tr.get("trace_links") or []
    severity = "high" if not comment and not trace_links else "medium"

    return [_finding(
        severity=severity,
        step_index=None,
        rule="R17",
        description=(
            f"Test case {tc_key} (marketplace={mp or 'unscoped'}) was "
            f"{prior.status} in the immediate prior execution "
            f"({prior.e_key} by {prior.tester or 'unknown'}, "
            f"{prior.date or 'unknown date'}) but this run is marked "
            f"{current_status}. "
            + ("No tester comment or defect link explains why the "
               "test no longer applies — verify the test legitimately "
               "stopped applying (test case retired, prerequisite "
               "removed, scope change) rather than the tester "
               "side-stepping a difficult execution."
               if severity == "high" else
               "Verify the test legitimately stopped applying rather "
               "than the tester side-stepping a difficult execution.")
        ),
    )]


# R18: unexplained Not Applicable (per-execution)
# Fires from the synthetic audit.json that batch.py writes for any
# execution test-management returns as Not Applicable / N/A. Companion to R17
# (history-aware) and R12 (no evidence): different severity ladder,
# different focus, but same "flag the side-stepping pattern" goal.
#
# Why surface N/A as audits in the main table at all
# --------------------------------------------------
# Coverage R15a was the only place N/A items appeared previously, and
# a coverage drawer is too easy to miss. Operators reviewing the audit
# table need to see them as first-class items so the same filter /
# search / sort tools work on them.
#
# Severity ladder (matches the policy in R17):
#   high   — no comment AND no traceLinks AND R17 also fires (prior
#            sprint executed it).
#   medium — has documentation OR R17 doesn't fire (no executed prior).
#
# R18 is the *base* finding the synthetic audit always carries; R17
# fires alongside it from the same metadata when history is suspicious.
# Both findings are independent — R18 says "this is N/A", R17 says
# "this used to be executed". Together they make the case the
# dashboard surfaces.
def build_r18_finding(
    metadata: dict[str, Any],
) -> dict[str, Any] | None:
    """Construct an R18 finding for an N/A execution. None when not N/A.

    Does not look at history — that's R17's job, called separately so
    R17 can also fire on real (non-N/A) audits if its conditions match.

    Severity: high when no comment + no traceLinks; medium otherwise.
    The "high when prior was executed" escalation lives in R17, NOT
    here — this rule's only signal is "the tester documented OR didn't
    document the N/A claim."
    """
    tr = _first_result(metadata)
    if not tr:
        return None
    if (tr.get("status") or "").strip() not in ("Not Applicable", "N/A"):
        return None
    tc_key = tr.get("test_case_key") or "?"
    mp = tr.get("marketplace") or "unscoped"
    tester = tr.get("executed_by_name") or tr.get("executed_by") or "unknown"
    top_comment = (tr.get("comment") or "").strip()
    top_links = tr.get("trace_links") or []
    documented = bool(top_comment) or bool(top_links)
    # Step-level credit (same as R6/R16): any step with comment or
    # trace_link counts as documentation.
    if not documented:
        for s in tr.get("steps") or []:
            if (s.get("comment") or "").strip():
                documented = True
                break
            if s.get("trace_links"):
                documented = True
                break
    severity = "medium" if documented else "high"
    return _finding(
        severity=severity,
        step_index=None,
        rule="R18",
        description=(
            f"Test case {tc_key} (marketplace={mp}) was marked "
            f"'Not Applicable' by {tester}. "
            + ("Verify the N/A determination is legitimate — "
               "test case retired, prerequisite removed, "
               "scope change."
               if documented else
               "No tester comment or defect link explains why the test "
               "doesn't apply. Verify the determination is legitimate "
               "(test case retired, prerequisite removed, scope change) "
               "rather than the tester side-stepping a difficult "
               "execution.")
        ),
    )


# Aggregator
# R3 deliberately absent: per policy Pass+Blocked is acceptable when the
# Blocked step isn't a real blocker, and "real blocker" is a semantic
# judgement only the model can make. See check_blocked_step_pass_overall
# docstring for the full deprecation rationale.
_RULES = (
    check_rule_engine_health,
    check_in_progress_steps,
    check_fail_step_pass_overall,
    check_fail_overall_without_failing_step,
    check_pass_step_with_issue_comment,
    check_passed_with_issue_has_evidence,
    check_fail_or_blocked_has_documentation,  # R16
    check_passed_with_issue_requires_fail_step,
    check_blocked_overall_requires_blocked_step,
    check_fail_step_requires_defect_ref,
    check_defect_refs_in_comment_text,
)


def run_workflow_rules(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """Run all enabled rules against `metadata`, return combined findings.

    Ordering: findings follow rule order
        R0 → R1 → R2 → R4 → R5 → R6 → R7 → R8

    R3 is deprecated and absent from the aggregator — see module
    docstring. R0 leads intentionally: if the rule engine is degraded
    the reader should see that before any per-step finding. Within a
    rule, findings follow step order. No deduplication across rules —
    a single step can legitimately trip multiple rules.
    """
    findings: list[dict[str, Any]] = []
    for rule in _RULES:
        findings.extend(rule(metadata))
    return findings
