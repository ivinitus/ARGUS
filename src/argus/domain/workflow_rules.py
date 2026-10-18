"""Deterministic workflow-consistency checks over tester metadata.

Rule catalogue and semantics live in docs/rules.md. configure() must run
before any audit (runtime.py does this): the role sets the rules compare
against are module globals rebuilt from `[policy] statuses`.
"""
from __future__ import annotations

import re as _re
import sys
from typing import Any

from argus.core import redaction
from argus.core.redaction import redact_credentials
from argus.domain.step_refs import human_step_number
from argus.domain.test_steps import has_step_definition

_DEFAULT_STATUSES: dict[str, str] = {
    "pass": "Pass",
    "fail": "Fail",
    "blocked": "Blocked",
    "in_progress": "In Progress",
    "passed_with_issue": "Passed With Issue",
    "not_applicable": "Not Applicable",
    "not_executed": "Not Executed",
    "retest": "Retest",
}

STATUS_PASS = _DEFAULT_STATUSES["pass"]
STATUS_FAIL = _DEFAULT_STATUSES["fail"]
STATUS_BLOCKED = _DEFAULT_STATUSES["blocked"]
STATUS_IN_PROGRESS = _DEFAULT_STATUSES["in_progress"]
STATUS_PASSED_WITH_ISSUE = _DEFAULT_STATUSES["passed_with_issue"]
STATUS_NOT_APPLICABLE = _DEFAULT_STATUSES["not_applicable"]
STATUS_NOT_EXECUTED = _DEFAULT_STATUSES["not_executed"]
STATUS_RETEST = _DEFAULT_STATUSES["retest"]

_STATUS_ALIASES: dict[str, str] = {
    "n/a": "not_applicable",
    "na": "not_applicable",
    "not-applicable": "not_applicable",
    "notapplicable": "not_applicable",
    "notexecuted": "not_executed",
    "not-executed": "not_executed",
    "passedwithissue": "passed_with_issue",
    "passwithrestrictions": "passed_with_issue",
    "pass with restrictions": "passed_with_issue",
    "inprogress": "in_progress",
}

_STATUSES: dict[str, str] = dict(_DEFAULT_STATUSES)
_CONFIGURED_STATUS_ALIASES: dict[str, str] = {}

_NAME_TO_ROLE: dict[str, str] = {}

_BLOCKED_LIKE_STATUSES: tuple[str, ...] = ()


def rule_base(rule: str) -> str:
    """Return the normalized parent rule family for a rule identifier."""
    normalized = rule.upper()
    match = _re.match(r"^(R[0-9]+)", normalized)
    return match.group(1) if match else normalized


def status_role(value: str | None) -> str | None:
    """Map a raw Zephyr status string to its role, or None if unrecognised.

    The only place that decides what a status MEANS: rules compare
    roles, never display names — that is what lets `[policy] statuses`
    reach them. Returning None is meaningful, not a failure: R0 reports
    it rather than letting rules silently match nothing.
    """
    if not value:
        return None
    key = value.strip().lower()
    role = _NAME_TO_ROLE.get(key)
    if role is None:
        role = _NAME_TO_ROLE.get(key.replace(" ", ""))
    return role


def _rebuild_status_sets() -> None:
    """Recompute everything derived from `_STATUSES`."""
    global STATUS_PASS, STATUS_FAIL, STATUS_BLOCKED, STATUS_IN_PROGRESS
    global STATUS_PASSED_WITH_ISSUE, STATUS_NOT_APPLICABLE
    global STATUS_NOT_EXECUTED, STATUS_RETEST
    global _BLOCKED_LIKE_STATUSES, _NAME_TO_ROLE

    STATUS_PASS = _STATUSES["pass"]
    STATUS_FAIL = _STATUSES["fail"]
    STATUS_BLOCKED = _STATUSES["blocked"]
    STATUS_IN_PROGRESS = _STATUSES["in_progress"]
    STATUS_PASSED_WITH_ISSUE = _STATUSES["passed_with_issue"]
    STATUS_NOT_APPLICABLE = _STATUSES["not_applicable"]
    STATUS_NOT_EXECUTED = _STATUSES["not_executed"]
    STATUS_RETEST = _STATUSES["retest"]

    _BLOCKED_LIKE_STATUSES = (STATUS_BLOCKED, STATUS_NOT_APPLICABLE)

    mapping: dict[str, str] = {}
    for role, name in _STATUSES.items():
        if not name:
            continue
        mapping[name.strip().lower()] = role
        mapping[name.strip().lower().replace(" ", "")] = role
    for alias, role in _STATUS_ALIASES.items():
        mapping.setdefault(alias, role)
    for alias, role in _CONFIGURED_STATUS_ALIASES.items():
        mapping[alias] = role
        mapping[alias.replace(" ", "")] = role
    _NAME_TO_ROLE = mapping



_R16_TARGET_ROLES = frozenset({"fail", "blocked"})
_R11_ISSUE_ROLES = frozenset({"fail", "passed_with_issue"})
_EXECUTED_ROLES = frozenset({"pass", "fail", "passed_with_issue"})
_BLOCKED_LIKE_ROLES = frozenset({"blocked", "not_applicable"})
_R18_NA_ROLES = frozenset({"not_applicable"})

_rebuild_status_sets()


def is_executed_status(value: str | None) -> bool:
    """True when the status means the tester actually drove the test."""
    return status_role(value) in _EXECUTED_ROLES


def is_pass_status(value: str | None) -> bool:
    """True when the status is this project's Pass."""
    return status_role(value) == "pass"

_ISSUE_KEYWORDS: tuple[str, ...] = (
    "bug",
    "broken",
    "crash",
    "crashed",
    "crashes",
    "hang",
    "hangs",
    "hung",
    "freeze",
    "freezes",
    "froze",
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

_NEGATED_ISSUE_RE = _re.compile(
    r"\b(?:no|without)\s+(?:known\s+)?"
    r"(?:bugs?|errors?|failures?|issues?|problems?)\b"
    r"|\b(?:bugs?|errors?|failures?|issues?|problems?)\s+"
    r"(?:is\s+|are\s+|was\s+|were\s+)?(?:fixed|resolved)\b",
    flags=_re.I,
)
_BROWSER_NAME_PATTERN = (
    r"firefox|edge|chrome|safari|opera|brave|samsung internet"
)
_BROWSER_SUBSTITUTION_RE = _re.compile(
    rf"\b(?:tested|verified|checked|ran|executed)\b"
    rf".{{0,24}}\b(?:in|on|using|with)\s+"
    rf"(?P<alternate>{_BROWSER_NAME_PATTERN})\b"
    rf".{{0,36}}\b(?:because|since|as|due to)\b"
    rf".{{0,36}}\b(?P<original>{_BROWSER_NAME_PATTERN})\b"
    rf".{{0,36}}\b(?:issues?|problems?|broken|not working|doesn't work|"
    rf"does not work|errors?|failures?|failed)\b",
    flags=_re.I,
)
_ALTERNATE_BROWSER_ALLOWED_RE = _re.compile(
    r"\b(?:any|another|new|different|other|alternate|alternative)\s+"
    r"(?:supported\s+)?browser\b"
    rf"|\b(?:{_BROWSER_NAME_PATTERN})\s+or\s+"
    r"(?:any\s+)?(?:other|another|different|alternate|alternative)\s+browser\b",
    flags=_re.I,
)


def _matched_issue_keywords(comment: str) -> list[str]:
    without_negated_phrases = _NEGATED_ISSUE_RE.sub(" ", comment)
    return [
        keyword
        for keyword in _ISSUE_KEYWORDS
        if _re.search(
            rf"(?<!\w){_re.escape(keyword)}(?!\w)",
            without_negated_phrases,
            flags=_re.I,
        )
    ]


def _is_allowed_browser_substitution(
    steps: list[dict[str, Any]],
    position: int,
    comment: str,
) -> bool:
    """True when a Pass comment documents an explicitly allowed browser swap."""
    step = steps[position]
    if step.get("trace_links"):
        return False
    match = _BROWSER_SUBSTITUTION_RE.search(comment)
    if not match or match.group("alternate").lower() == match.group("original").lower():
        return False

    context_steps = steps[max(0, position - 1):position + 1]
    context = " ".join(
        str(candidate.get(field) or "")
        for candidate in context_steps
        for field in ("description", "expected_result")
    )
    context = _re.sub(r"<[^>]+>", " ", context)
    return bool(_ALTERNATE_BROWSER_ALLOWED_RE.search(context))


def _first_result(metadata: dict[str, Any]) -> dict[str, Any] | None:
    """Return the single test_result dict we audit, or None if missing."""
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
        "page": None,
        "step_index": step_index,
        "description": description,
        "source": "rule",
        "rule": rule,
    }


_UNKNOWN_STATUS_WARNED = False


def _unknown_statuses(tr: dict[str, Any]) -> list[str]:
    """Resolved status names that map to no known role, deduped in order."""
    seen: dict[str, None] = {}
    candidates = [tr.get("status")]
    candidates.extend((s.get("status")) for s in tr.get("steps") or [])
    for value in candidates:
        if not isinstance(value, str) or not value.strip():
            continue
        if status_role(value) is None:
            seen.setdefault(value.strip(), None)
    return list(seen)


def check_rule_engine_health(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """Fire when the rules below cannot be trusted to have run.

    Two triggers: (1) unresolved ids — the status endpoint failed and
    every `status` is None while status_id is set; (2) unknown
    vocabulary — display names this build doesn't recognise ("Passed"
    vs "Pass"). (2) is the worse case because nothing looks broken:
    every rule short-circuits silently and the audit comes back falsely
    clean. Fix is `[policy] statuses`; until then this stays loud.
    """
    tr = _first_result(metadata)
    if not tr:
        return []
    has_unresolved = False
    if isinstance(tr.get("status_id"), int) and tr.get("status") is None:
        has_unresolved = True
    for s in tr.get("steps") or []:
        if isinstance(s.get("status_id"), int) and s.get("status") is None:
            has_unresolved = True
            break
    if has_unresolved:
        return [_finding(
            severity="high",
            step_index=None,
            rule="R0",
            description=(
                "Workflow-consistency rules (R1-R8) were not fully "
                "evaluated: Jira returned numeric status ids but the "
                "project status-name map did not resolve (projectId "
                "missing or status endpoint failed). Re-run the extractor "
                "to refresh the mapping, or treat this audit's "
                "workflow-rule section as incomplete."
            ),
        )]

    unknown = _unknown_statuses(tr)
    if not unknown:
        return []

    global _UNKNOWN_STATUS_WARNED
    if not _UNKNOWN_STATUS_WARNED:
        _UNKNOWN_STATUS_WARNED = True
        sys.stderr.write(
            f"argus: unrecognised execution status {unknown[0]!r}; the "
            f"workflow rules cannot classify it. Set [policy] statuses "
            f"for this project (roles: "
            f"{', '.join(sorted(_DEFAULT_STATUSES))}).\n"
        )
    listed = ", ".join(repr(name) for name in unknown[:5])
    if len(unknown) > 5:
        listed += f", +{len(unknown) - 5} more"
    configured = ", ".join(
        f"{role}={name!r}" for role, name in sorted(_STATUSES.items())
    )
    return [_finding(
        severity="high",
        step_index=None,
        rule="R0",
        description=(
            f"Workflow-consistency rules (R1-R22) could not be evaluated: "
            f"this execution uses status name(s) {listed}, which are not "
            f"in the configured vocabulary ({configured}). Every "
            f"status-dependent rule short-circuits on an unrecognised "
            f"name, so this audit's workflow-rule section is INCOMPLETE "
            f"rather than clean. Fix by setting [policy] statuses in the "
            f"ARGUS config to this project's Zephyr status display names."
        ),
    )]


def check_in_progress_steps(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """Per policy: a step left 'In Progress' is only non-compliant when the tester provides no explanatory comment."""
    tr = _first_result(metadata)
    if not tr:
        return []
    findings: list[dict[str, Any]] = []
    for s in tr.get("steps") or []:
        if status_role(s.get("status")) != "in_progress":
            continue
        if (s.get("comment") or "").strip():
            continue
        step_number = human_step_number(s.get("index"))
        findings.append(_finding(
            severity="medium",
            step_index=s.get("index"),
            rule="R1",
            description=(
                f"Step {step_number} is marked 'In Progress' with no "
                "explanatory comment. Per policy, In Progress is only "
                "acceptable when the tester documents why (waiting on a "
                "dependency, deferred, etc.). Finalise the status or add "
                "a comment."
            ),
        ))
    return findings


def check_fail_step_pass_overall(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """If the overall test is marked Pass but any step is Fail, that's a logical contradiction."""
    tr = _first_result(metadata)
    if not tr:
        return []
    if status_role(tr.get("status")) != "pass":
        return []
    findings: list[dict[str, Any]] = []
    for s in tr.get("steps") or []:
        if status_role(s.get("status")) == "fail":
            step_number = human_step_number(s.get("index"))
            findings.append(_finding(
                severity="high",
                step_index=s.get("index"),
                rule="R2",
                description=(
                    f"Overall test marked 'Pass' but step {step_number} "
                    "is marked 'Fail'. A failed step should propagate to the "
                    "overall result (Fail or Passed With Issue)."
                ),
            ))
    return findings


def check_pass_overall_with_no_executed_steps(
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    """R2b: overall Pass cannot be supported when every step is Not Executed."""
    tr = _first_result(metadata)
    if not tr or status_role(tr.get("status")) != "pass":
        return []
    steps = tr.get("steps") or []
    if not steps:
        return []
    if not all(status_role(step.get("status")) == "not_executed"
               for step in steps):
        return []
    return [_finding(
        severity="high",
        step_index=None,
        rule="R2b",
        description=(
            "Overall test is marked 'Pass', but every test step is marked "
            "'Not Executed'. Execute and record the required steps, or change "
            "the overall result to match the execution."
        ),
    )]


def check_blocked_step_pass_overall(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """DEPRECATED — see module docstring. Kept for historical tests only."""
    tr = _first_result(metadata)
    if not tr:
        return []
    if status_role(tr.get("status")) != "pass":
        return []
    findings: list[dict[str, Any]] = []
    for s in tr.get("steps") or []:
        if status_role(s.get("status")) == "blocked":
            step_number = human_step_number(s.get("index"))
            findings.append(_finding(
                severity="medium",
                step_index=s.get("index"),
                rule="R3",
                description=(
                    f"Overall test marked 'Pass' but step {step_number} "
                    "is marked 'Blocked'. A blocked step should propagate to "
                    "the overall result (Blocked or Passed With Issue)."
                ),
            ))
    return findings


def check_fail_overall_without_failing_step(
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    """If the overall test is marked Fail, at least one step must be Fail or Blocked to justify the verdict."""
    tr = _first_result(metadata)
    if not tr:
        return []
    if status_role(tr.get("status")) != "fail":
        return []
    steps = tr.get("steps") or []
    if not steps:
        return []
    has_any_failing = any(
        status_role(s.get("status")) in _R16_TARGET_ROLES for s in steps
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


def check_pass_step_with_issue_comment(
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    """Detect Pass steps whose comments describe an in-scope issue."""
    tr = _first_result(metadata)
    if not tr:
        return []
    findings: list[dict[str, Any]] = []
    steps = [
        step for step in tr.get("steps") or []
        if isinstance(step, dict)
    ]
    for position, s in enumerate(steps):
        if status_role(s.get("status")) != "pass":
            continue
        comment = str(s.get("comment") or "").strip()
        if not comment:
            continue
        matched = _matched_issue_keywords(comment)
        if matched and _is_allowed_browser_substitution(steps, position, comment):
            continue
        if matched:
            step_number = human_step_number(s.get("index"))
            findings.append(_finding(
                severity="medium",
                step_index=s.get("index"),
                rule="R5",
                description=(
                    f"Step {step_number} is marked 'Pass' but its tester "
                    f"comment contains issue-language ({', '.join(matched[:3])}"
                    f"{'…' if len(matched) > 3 else ''}). "
                    "If the tester observed a real problem, the status "
                    "should be 'Passed With Issue' or 'Fail' instead of "
                    "'Pass'. Comment: "
                    f'"{redact_credentials(s.get("comment") or "")}"'
                ),
            ))
    return findings


def check_passed_with_issue_has_evidence(
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    """Require evidence for a Passed With Issue result."""
    tr = _first_result(metadata)
    if not tr:
        return []
    if status_role(tr.get("status")) != "passed_with_issue":
        return []
    top_comment = (tr.get("comment") or "").strip()
    top_links = tr.get("trace_links") or []
    if top_comment or top_links or _has_execution_defect_reference(tr):
        return []
    for s in tr.get("steps") or []:
        if (s.get("comment") or "").strip():
            return []
        if _has_execution_defect_reference(s):
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




def check_fail_or_blocked_has_documentation(
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    """R16: overall=Fail or Blocked must carry SOME narrative."""
    tr = _first_result(metadata)
    if not tr:
        return []
    status = tr.get("status")
    if status_role(status) not in _R16_TARGET_ROLES:
        return []
    top_comment = (tr.get("comment") or "").strip()
    top_links = tr.get("trace_links") or []
    if top_comment or top_links or _has_execution_defect_reference(tr):
        return []
    for s in tr.get("steps") or []:
        if (s.get("comment") or "").strip():
            return []
        if _has_execution_defect_reference(s):
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


def check_passed_with_issue_requires_fail_step(
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    """Require a Fail step for a Passed With Issue result."""
    tr = _first_result(metadata)
    if not tr:
        return []
    if status_role(tr.get("status")) != "passed_with_issue":
        return []
    steps = tr.get("steps") or []
    if not steps:
        return []
    if any(s.get("status") is None for s in steps):
        return []
    if any(status_role(s.get("status")) == "fail" for s in steps):
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


def check_blocked_overall_requires_blocked_step(
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    """Require a Blocked step for a Blocked overall result."""
    tr = _first_result(metadata)
    if not tr:
        return []
    if status_role(tr.get("status")) != "blocked":
        return []
    steps = tr.get("steps") or []
    if not steps:
        return []
    if any(s.get("status") is None for s in steps):
        return []
    if any(status_role(s.get("status")) == "blocked" for s in steps):
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


_ISSUE_URL_RE = _re.compile(
    r"(?i)(?<![\w/.:@-])(?:https?://)?(?P<host>[a-z0-9.-]+)/"
    r"browse/[A-Z][A-Z0-9_]*(?:-[A-Z][A-Z0-9_]*)*-\d+(?![\w-])"
)
_DEFAULT_DEFECT_KEY_PREFIXES = ("QA-", "BUG-")
_DEFAULT_STOREFRONT_DOMAIN = "example"
_DEFECT_KEY_PREFIXES = _DEFAULT_DEFECT_KEY_PREFIXES
_EXTERNAL_DEFECT_KEY_PREFIXES: tuple[str, ...] = ()
_DEFECT_URL_HOSTS: frozenset[str] = frozenset()
_ENABLED_RULES: frozenset[str] = frozenset()
_DISABLED_RULES: frozenset[str] = frozenset()
_RULE_SEVERITIES: dict[str, str] = {}
_R22_MODE = "advisory"
_EMPTY_STEP_STATUS_OPTIONAL = False
_STOREFRONT_DOMAIN = _DEFAULT_STOREFRONT_DOMAIN
_STOREFRONT_DOMAINS: tuple[str, ...] = (_DEFAULT_STOREFRONT_DOMAIN,)
_DEFECT_KEY_RE = _re.compile(r"\b[A-Z][A-Z0-9]+(?:-[A-Z][A-Z0-9]*)*-\d+\b")


def _contains_defect_reference(value: Any) -> bool:
    text = str(value or "")
    if any(
        match.group("host").lower() in _DEFECT_URL_HOSTS
        for match in _ISSUE_URL_RE.finditer(text)
    ):
        return True
    return any(
        key.startswith(_DEFECT_KEY_PREFIXES + _EXTERNAL_DEFECT_KEY_PREFIXES)
        for key in _DEFECT_KEY_RE.findall(text)
    )


def _has_execution_defect_reference(tr: dict[str, Any]) -> bool:
    """Accept Issues, Comments, or Weblinks on an execution or step."""
    if tr.get("trace_links"):
        return True
    values: list[Any] = [tr.get("comment")]
    for field in ("weblinks", "web_links", "webLinks", "links", "test_case_weblinks"):
        raw = tr.get(field)
        values.extend(raw if isinstance(raw, list) else [raw])
    return any(_contains_defect_reference(value) for value in values)




def check_fail_step_requires_defect_ref(
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    """R11: a step that admits an issue must carry SOME form of defect documentation."""
    tr = _first_result(metadata)
    if not tr:
        return []
    execution_has_reference = _has_execution_defect_reference(tr)
    external_tracker_note = (
        "Configured external issue links are accepted without an Issues attachment. "
        if _DEFECT_URL_HOSTS else ""
    )
    findings: list[dict[str, Any]] = []
    for s in tr.get("steps") or []:
        status = s.get("status")
        role = status_role(status)
        if role not in _R11_ISSUE_ROLES:
            continue
        if execution_has_reference or _has_execution_defect_reference(s):
            continue
        if role == "fail":
            kind = "marked Fail"
            consequence = "the failure is undocumented"
        else:
            kind = "marked 'Passed With Issue'"
            consequence = (
                "the acknowledged issue is undocumented (and PWI without "
                "a tracked defect can mask a real failure)"
            )
        step_number = human_step_number(s.get("index"))
        findings.append(_finding(
            severity="medium",
            step_index=s.get("index"),
            rule="R11",
            description=(
                f"Step {step_number} is {kind} but has no defect reference "
                "in Issues (trace_links), Comments, or Weblinks on the step "
                "or execution. Attach a Jira defect in Issues, or add the "
                "defect key or ticket URL in Comments or Weblinks. "
                f"{external_tracker_note}"
                f"Otherwise {consequence} and reviewers "
                "can't tell what broke."
            ),
        ))
    return findings


def check_defect_refs_in_comment_text(
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    """R10: a Jira defect key in a comment should also use traceLinks.

    Configured external tickets may remain in Comments or Weblinks.
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
        comment = _ISSUE_URL_RE.sub(
            lambda match: "" if match.group("host").lower() in _DEFECT_URL_HOSTS else match.group(0),
            comment,
        )
        keys = [
            k for k in _DEFECT_KEY_RE.findall(comment)
            if k.startswith(_DEFECT_KEY_PREFIXES)
        ]
        if keys:
            ref = keys[0]
            step_number = human_step_number(s.get("index"))
            finding = _finding(
                severity="low",
                step_index=s.get("index"),
                rule="R10",
                description=(
                    f"Step {step_number} comment references defect "
                    f"({ref}) but it is not attached as a trace_link. "
                    "Please attach it via the test "
                    "result's trace_links field too."
                ),
            )
            finding["advisory"] = False
            findings.append(finding)
    return findings


_ACCOUNT_EMAIL_RE = redaction.ACCOUNT_EMAIL_RE



def check_test_account_in_comments(
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    """R22: an executed test should record the test account in a comment."""
    if _R22_MODE == "disabled":
        return []
    tr = _first_result(metadata)
    if not tr:
        return []
    if status_role(tr.get("status")) not in _EXECUTED_ROLES:
        return []
    texts = [tr.get("comment") or ""]
    texts.extend((s.get("comment") or "") for s in tr.get("steps") or [])
    if any(_ACCOUNT_EMAIL_RE.search(t) for t in texts):
        return []
    tester = (tr.get("executed_by_name") or tr.get("executed_by")
              or "The tester")
    finding = _finding(
        severity="low" if _R22_MODE == "advisory" else "medium",
        step_index=None,
        rule="R22",
        description=(
            f"{tester} did not record the test-account identifier in any "
            "execution comment. Add the account identifier used (for "
            "example, user+test-...@example.com) so the run can be "
            "reproduced. Never include a password or other secret."
        ),
    )
    finding["advisory"] = _R22_MODE == "advisory"
    return [finding]



_DEFAULT_TLD_TO_MARKETPLACE = {
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
_TLD_TO_MARKETPLACE = dict(_DEFAULT_TLD_TO_MARKETPLACE)
_TLD_BY_SPECIFICITY = sorted(_TLD_TO_MARKETPLACE.keys(),
                             key=lambda t: -len(t))


def configure(policy) -> None:
    """Apply one process-wide team policy before running audits."""
    global _DEFECT_KEY_PREFIXES, _ENABLED_RULES, _DISABLED_RULES
    global _EXTERNAL_DEFECT_KEY_PREFIXES, _DEFECT_URL_HOSTS
    global _RULE_SEVERITIES
    global _STOREFRONT_DOMAIN, _STOREFRONT_DOMAINS
    global _TLD_TO_MARKETPLACE, _TLD_BY_SPECIFICITY
    global _STATUSES, _CONFIGURED_STATUS_ALIASES
    global _UNKNOWN_STATUS_WARNED, _R22_MODE
    global _EMPTY_STEP_STATUS_OPTIONAL
    merged = dict(_DEFAULT_STATUSES)
    if policy.statuses:
        for role, name in policy.statuses.items():
            if role not in _DEFAULT_STATUSES:
                sys.stderr.write(
                    f"argus: ignoring unknown [policy] statuses key "
                    f"{role!r}; expected one of "
                    f"{', '.join(sorted(_DEFAULT_STATUSES))}\n"
                )
                continue
            if name and name.strip():
                merged[role] = name.strip()
    _STATUSES = merged
    _CONFIGURED_STATUS_ALIASES = {
        alias.strip().lower(): role
        for alias, role in getattr(policy, "status_aliases", {}).items()
        if alias.strip() and role in _DEFAULT_STATUSES
    }
    _rebuild_status_sets()
    _UNKNOWN_STATUS_WARNED = False
    if policy.defect_key_prefixes:
        _DEFECT_KEY_PREFIXES = tuple(policy.defect_key_prefixes)
    _EXTERNAL_DEFECT_KEY_PREFIXES = tuple(
        getattr(policy, "external_defect_key_prefixes", ())
    )
    _DEFECT_URL_HOSTS = frozenset(
        host.strip().lower() for host in getattr(policy, "defect_url_hosts", ())
        if host.strip()
    )
    _ENABLED_RULES = frozenset(rule.upper() for rule in policy.enabled_rules)
    _DISABLED_RULES = frozenset(
        rule.upper()
        for rule in getattr(policy, "disabled_rules", ())
    )
    _RULE_SEVERITIES = {
        rule.upper(): severity.lower()
        for rule, severity in getattr(policy, "rule_severities", {}).items()
    }
    _R22_MODE = getattr(policy, "r22_mode", "advisory")
    _EMPTY_STEP_STATUS_OPTIONAL = bool(
        getattr(policy, "empty_step_status_optional", False)
    )
    if policy.storefront_domain:
        _STOREFRONT_DOMAIN = policy.storefront_domain.lower()
    configured_domains = getattr(policy, "storefront_domains", None)
    _STOREFRONT_DOMAINS = (
        tuple(
            domain.lower()
            for domain in configured_domains
            if domain
        )
        if configured_domains
        else (_STOREFRONT_DOMAIN,)
    )
    if policy.marketplace_tlds:
        _TLD_TO_MARKETPLACE = {
            tld.lower(): marketplace.upper()
            for marketplace, tld in policy.marketplace_tlds.items()
        }
        _TLD_BY_SPECIFICITY = sorted(
            _TLD_TO_MARKETPLACE,
            key=lambda tld: -len(tld),
        )


def reset_configuration() -> None:
    """Restore the built-in policy."""
    global _DEFECT_KEY_PREFIXES, _ENABLED_RULES, _DISABLED_RULES
    global _EXTERNAL_DEFECT_KEY_PREFIXES, _DEFECT_URL_HOSTS
    global _RULE_SEVERITIES
    global _STOREFRONT_DOMAIN, _STOREFRONT_DOMAINS
    global _TLD_TO_MARKETPLACE, _TLD_BY_SPECIFICITY
    global _STATUSES, _CONFIGURED_STATUS_ALIASES
    global _UNKNOWN_STATUS_WARNED, _R22_MODE
    global _EMPTY_STEP_STATUS_OPTIONAL
    _STATUSES = dict(_DEFAULT_STATUSES)
    _CONFIGURED_STATUS_ALIASES = {}
    _rebuild_status_sets()
    _UNKNOWN_STATUS_WARNED = False
    _DEFECT_KEY_PREFIXES = _DEFAULT_DEFECT_KEY_PREFIXES
    _EXTERNAL_DEFECT_KEY_PREFIXES = ()
    _DEFECT_URL_HOSTS = frozenset()
    _ENABLED_RULES = frozenset()
    _DISABLED_RULES = frozenset()
    _RULE_SEVERITIES = {}
    _R22_MODE = "advisory"
    _EMPTY_STEP_STATUS_OPTIONAL = False
    _STOREFRONT_DOMAIN = _DEFAULT_STOREFRONT_DOMAIN
    _STOREFRONT_DOMAINS = (_DEFAULT_STOREFRONT_DOMAIN,)
    _TLD_TO_MARKETPLACE = dict(_DEFAULT_TLD_TO_MARKETPLACE)
    _TLD_BY_SPECIFICITY = sorted(_TLD_TO_MARKETPLACE, key=lambda t: -len(t))


def filter_enabled(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply the team's rule allowlist and severity overrides."""

    def _keep(finding: dict[str, Any]) -> bool:
        rule = str(finding.get("rule") or "").upper()
        if rule == "R0":
            return True
        base = rule_base(rule)
        if base == "R22" and _R22_MODE == "disabled":
            return False
        # A family id (R14) in disabled_rules covers its sub-rules (R14a/b),
        # mirroring how enabled_rules already treats families.
        if rule in _DISABLED_RULES or (base != rule and base in _DISABLED_RULES):
            return False
        if not _ENABLED_RULES:
            return True
        if rule in _ENABLED_RULES:
            return True
        return base != rule and base in _ENABLED_RULES

    kept = (
        [finding for finding in findings if _keep(finding)]
        if _ENABLED_RULES or _DISABLED_RULES or _R22_MODE == "disabled"
        else findings
    )
    out: list[dict[str, Any]] = []
    for finding in kept:
        rule_id = str(finding.get("rule") or "").upper()
        override = _RULE_SEVERITIES.get(rule_id)
        if override is None and rule_id:
            override = _RULE_SEVERITIES.get(rule_base(rule_id))
        if rule_id == "R0":
            # R0 reports an unusable status vocabulary; its severity is fixed.
            override = None
        if override and finding.get("severity") != override:
            finding = dict(finding)
            finding["severity"] = override
        out.append(finding)
    return out


_GENERIC_HOST_PREFIXES = frozenset({"www", "m", "mobile", "amp"})


def _url_host(url: str) -> str:
    """Lower-case hostname of a URL or bare host string ('' when absent)."""
    host = (url or "").strip().lower().removesuffix("#trunc")
    host = _re.sub(r"^(?:https?:)?//", "", host)
    host = host.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    host = host.rsplit("@", 1)[-1].split(":", 1)[0]
    return host


def _is_storefront_host(url: str) -> bool:
    """Return whether a URL hostname itself represents a storefront."""
    host = _url_host(url)
    if not host:
        return False
    for domain in _STOREFRONT_DOMAINS:
        for tld in _TLD_BY_SPECIFICITY:
            suffix = f"{domain}{tld}"
            if host == suffix:
                return True
            if host.endswith(f".{suffix}"):
                chain = host[: -len(f".{suffix}")].split(".")
                meaningful = [
                    label
                    for label in chain
                    if label and label not in _GENERIC_HOST_PREFIXES
                ]
                return len(meaningful) <= 1
    return False


def _detect_marketplace_from_url(url: str) -> str | None:
    """Return the marketplace for a configured storefront URL.

    Only the hostname is consulted: ``example.de/pd?ref=example.com`` is a
    DE storefront, not a US one, so path and query text never participate.
    """
    host = _url_host(url)
    if not host:
        return None
    for domain in _STOREFRONT_DOMAINS:
        for tld in _TLD_BY_SPECIFICITY:
            suffix = f"{domain}{tld}"
            if host == suffix or host.endswith(f".{suffix}"):
                return _TLD_TO_MARKETPLACE[tld]
    return None


def check_marketplace_match(
    metadata: dict[str, Any],
    audit: dict[str, Any],
) -> list[dict[str, Any]]:
    """R13: deterministic marketplace-mismatch finding."""
    tr = _first_result(metadata)
    if not tr:
        return []
    assigned_mp = str(tr.get("marketplace") or "").strip().upper()
    if not assigned_mp:
        return []
    ec = audit.get("env_check") or {}
    # When the env-check produced a confirmed active-navigation set
    # (`marketplace_urls`), that is the authoritative source for R13: the
    # bare `preprod_urls`/`prod_urls` lists retain every model reading —
    # including bottom-of-page hovers and foreign misreads the confirmation
    # pass already rejected — purely as diagnostics, and must not by
    # themselves fire a marketplace-mismatch. Older/simpler env_check blocks
    # that never ran confirmation omit the key; fall back to the raw lists.
    if "marketplace_urls" in ec:
        candidate_urls = ec.get("marketplace_urls") or []
    else:
        candidate_urls = (
            (ec.get("preprod_urls") or [])
            + (ec.get("prod_urls") or [])
        )
    check_urls = [
        url
        for url in candidate_urls
        if _is_storefront_host(url)
    ]
    if not check_urls:
        return []
    detected = []
    for url in check_urls:
        mp = _detect_marketplace_from_url(url)
        if mp:
            detected.append(mp)
    if not detected:
        return []
    unexpected = sorted({
        mp for mp in detected
        if mp != assigned_mp
    })
    if not unexpected:
        return []
    detected_str = ", ".join(unexpected)
    switched_storefront = assigned_mp in detected
    switch_context = (
        f" Screenshots also show the assigned {assigned_mp} storefront, "
        "so the execution switched marketplaces during the run."
        if switched_storefront
        else ""
    )
    return [_finding(
        severity="high",
        step_index=None,
        rule="R13",
        description=(
            f"Marketplace mismatch detected by deterministic check: this "
            f"test was assigned to the {assigned_mp} marketplace (per "
            f"the testrun name), but an active storefront URL detected in "
            f"the screenshots resolves to a different marketplace "
            f"({detected_str}).{switch_context} The tester appears to have "
            f"executed the "
            f"test against the wrong marketplace's storefront — wrong "
            f"domain, wrong currency, wrong locale-specific surfaces. "
            f"Re-run on {assigned_mp} or, if this test is intended to "
            f"be cross-marketplace, update the testrun assignment."
        ),
    )]





def check_streak_break(
    metadata: dict[str, Any],
    history_idx: Any,
) -> list[dict[str, Any]]:
    """Fire when this test case's current status breaks a long streak."""
    tr = _first_result(metadata)
    if not tr:
        return []
    tc_key = tr.get("test_case_key")
    current_status = tr.get("status")
    mp = tr.get("marketplace")
    if not tc_key or not current_status:
        return []

    current_date = tr.get("execution_date")

    history = history_idx.get_history(tc_key, mp)
    if len(history) < 5:
        return []

    exclude_key: str | None = None
    if current_date:
        for entry in history:
            if entry.date == current_date:
                exclude_key = entry.e_key
                break

    from argus.domain.history_index import HistoryIndex

    def _streak_policy_text() -> str:
        from argus.domain import history_index as _hi
        return (f"{_hi._DEFAULT_THRESHOLD} of the last "
                f"{_hi._DEFAULT_WINDOW}")

    findings: list[dict[str, Any]] = []
    canon_current = current_status.strip()

    current_role = status_role(canon_current)
    if current_role == "pass":
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
                        f"{streak_status} in at least "
                        f"{_streak_policy_text()} "
                        f"executions but this run is marked Pass. "
                        + ("No tester comment or defect link explains "
                           "the change — verify the test was actually "
                           "unblocked and executed, not just re-marked."
                           if escalate else
                           "Verify the test was actually unblocked and "
                           "executed, not just re-marked.")
                    ),
                ))
                break

    if current_role in _BLOCKED_LIKE_ROLES:
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
                    f"least {_streak_policy_text()} executions but this "
                    f"run is marked {canon_current}. "
                    + ("No tester comment or defect link explains "
                       "the change — verify what regressed and "
                       "whether the change of coverage is intentional."
                       if escalate else
                       "Verify what regressed and whether the change "
                       "of coverage is intentional.")
                ),
            ))

    return filter_enabled(findings)




def check_immediate_prior_executed(
    metadata: dict[str, Any],
    history_idx: Any,
) -> list[dict[str, Any]]:
    """R17: tester regressed the test from executed → N/A in one sprint."""
    tr = _first_result(metadata)
    if not tr:
        return []
    tc_key = tr.get("test_case_key")
    current_status = (tr.get("status") or "").strip()
    mp = tr.get("marketplace")
    if not tc_key or not current_status:
        return []
    if status_role(current_status) not in _BLOCKED_LIKE_ROLES:
        return []

    history = history_idx.get_history(tc_key, mp)
    if not history:
        return []

    current_date = tr.get("execution_date")
    prior = None
    for entry in history:
        if current_date and entry.date == current_date:
            continue
        prior = entry
        break
    if prior is None:
        return []

    if status_role(prior.status) not in _EXECUTED_ROLES:
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


def _has_documentation(tr: dict[str, Any]) -> bool:
    """True when the tester documented the result anywhere."""
    if (tr.get("comment") or "").strip() or (tr.get("trace_links") or []):
        return True
    for s in tr.get("steps") or []:
        if (s.get("comment") or "").strip():
            return True
        if s.get("trace_links"):
            return True
    return False


_ACCOUNT_ID_RE = _re.compile(r"\bA[A-Z0-9]{8,}\b")
_CREDENTIAL_BOILERPLATE_RE = _re.compile(
    r"\bcopy\s+to\s+clipboard\b|\[redacted\]",
    flags=_re.I,
)


def _has_status_explanation(tr: dict[str, Any]) -> bool:
    """True when comments explain state, excluding credential boilerplate."""
    if tr.get("trace_links"):
        return True
    comments = [tr.get("comment") or ""]
    for step in tr.get("steps") or []:
        if step.get("trace_links"):
            return True
        comments.append(step.get("comment") or "")
    for comment in comments:
        cleaned = redact_credentials(comment)
        cleaned = _ACCOUNT_EMAIL_RE.sub(" ", cleaned)
        cleaned = _ACCOUNT_ID_RE.sub(" ", cleaned)
        cleaned = _CREDENTIAL_BOILERPLATE_RE.sub(" ", cleaned)
        if _re.search(r"[A-Za-z]{3,}", cleaned):
            return True
    return False


def build_r18_finding(
    metadata: dict[str, Any],
) -> dict[str, Any] | None:
    """Construct an R18 finding for an N/A execution."""
    tr = _first_result(metadata)
    if not tr:
        return None
    if status_role(tr.get("status")) not in _R18_NA_ROLES:
        return None
    tc_key = tr.get("test_case_key") or "?"
    mp = tr.get("marketplace") or "unscoped"
    tester = tr.get("executed_by_name") or tr.get("executed_by") or "unknown"
    documented = _has_documentation(tr)
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


def build_r20_finding(
    metadata: dict[str, Any],
    history_idx: Any | None = None,
    current_e_key: str | None = None,
) -> dict[str, Any] | None:
    """Construct an R20 finding for a Blocked execution."""
    tr = _first_result(metadata)
    if not tr:
        return None
    if status_role(tr.get("status")) != "blocked":
        return None
    tc_key = tr.get("test_case_key") or "?"
    mp = tr.get("marketplace") or "unscoped"
    tester = tr.get("executed_by_name") or tr.get("executed_by") or "unknown"
    documented = _has_documentation(tr)

    prior_blocked = None
    if history_idx is not None and tc_key != "?":
        try:
            for entry in history_idx.get_history(tc_key,
                                                 tr.get("marketplace")):
                if current_e_key and entry.e_key == current_e_key:
                    continue
                if (tr.get("execution_date")
                        and entry.date == tr.get("execution_date")):
                    continue
                if status_role(entry.status) == "blocked":
                    prior_blocked = entry
                break
        except Exception as e:
            print(f"[workflow_rules] R20 history lookup failed: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)
            prior_blocked = None

    severity = "medium" if documented and prior_blocked is None else "high"
    desc = (f"Test case {tc_key} (marketplace={mp}) was marked 'Blocked' "
            f"by {tester}. ")
    if prior_blocked is not None:
        prior_date = prior_blocked.date or "unknown date"
        desc += (f"It was ALSO Blocked in the prior execution "
                 f"({prior_blocked.e_key}, {prior_date}) — a recurring "
                 f"blocker suggests an environment or test-data issue "
                 f"nobody is fixing, not a one-off. ")
    if documented:
        desc += ("Verify the blocker is real and actionable (env down, "
                 "missing prerequisite, broken test data) and is being "
                 "tracked to resolution.")
    else:
        desc += ("No tester comment or defect link explains WHAT is "
                 "blocking. An undocumented Blocked is indistinguishable "
                 "from side-stepping a difficult execution — the tester "
                 "must document the blocker or re-run.")
    return _finding(
        severity=severity,
        step_index=None,
        rule="R20",
        description=desc,
    )


def build_r21_finding(
    metadata: dict[str, Any],
) -> dict[str, Any] | None:
    """Construct an R21 finding for an In Progress execution."""
    tr = _first_result(metadata)
    if not tr:
        return None
    if status_role(tr.get("status")) != "in_progress":
        return None
    tc_key = tr.get("test_case_key") or "?"
    mp = tr.get("marketplace") or "unscoped"
    tester = tr.get("executed_by_name") or tr.get("executed_by") or "unknown"
    documented = _has_status_explanation(tr)
    severity = "medium" if documented else "high"
    return _finding(
        severity=severity,
        step_index=None,
        rule="R21",
        description=(
            f"Test case {tc_key} (marketplace={mp}) was left 'In Progress' "
            f"by {tester}. "
            + ("The tester's comment explains the state — verify the "
               "execution is genuinely still underway and will reach a "
               "verdict before the folder closes."
               if documented else
               "No comment explains why the execution was started but "
               "never finished. The tester must complete the run, mark "
               "it Blocked with a documented blocker, or reset it to "
               "Not Executed.")
        ),
    )


_RULES = (
    check_rule_engine_health,
    check_in_progress_steps,
    check_fail_step_pass_overall,
    check_pass_overall_with_no_executed_steps,
    check_fail_overall_without_failing_step,
    check_pass_step_with_issue_comment,
    check_passed_with_issue_has_evidence,
    check_fail_or_blocked_has_documentation,
    check_passed_with_issue_requires_fail_step,
    check_blocked_overall_requires_blocked_step,
    check_fail_step_requires_defect_ref,
    check_defect_refs_in_comment_text,
    check_test_account_in_comments,
)


def _is_ignorable_empty_step(step: dict[str, Any]) -> bool:
    """True for a blank placeholder row a tester never had to execute.

    Only fires under `[policy] empty_step_status_optional`. A row counts
    as an ignorable placeholder when it carries no written instruction or
    expectation (`has_step_definition` is False) AND its status is not an
    actual verdict — a bare ``status_id`` with no resolved name, an empty
    string, ``Not Executed`` or ``In Progress``. A placeholder that DID
    reach a verdict (Fail/Pass/Blocked/Passed With Issue) is preserved so
    R2/R3/R4 and friends still see it.
    """
    if not isinstance(step, dict):
        return False
    if has_step_definition(step):
        return False
    # A placeholder that still documents something — a tester comment or a
    # linked defect — is evidence other rules (R16, R7, R8) rely on; keep it.
    if str(step.get("comment") or "").strip():
        return False
    if step.get("trace_links"):
        return False
    role = status_role(step.get("status"))
    if role is None or role in {"not_executed", "in_progress"}:
        return True
    return False


def _apply_empty_step_exception(
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Return metadata with ignorable placeholder rows dropped, or the input.

    Never mutates the caller's metadata: when any placeholder is dropped a
    shallow copy of the audited result (with a fresh ``steps`` list) is
    returned instead, so callers that inspect the original dict see it
    unchanged.
    """
    if not _EMPTY_STEP_STATUS_OPTIONAL:
        return metadata
    tr = _first_result(metadata)
    if not tr:
        return metadata
    steps = tr.get("steps")
    if not steps:
        return metadata
    kept = [s for s in steps if not _is_ignorable_empty_step(s)]
    if len(kept) == len(steps):
        return metadata
    new_tr = dict(tr)
    new_tr["steps"] = kept
    new_meta = dict(metadata)
    new_meta["test_results"] = [new_tr] + list(metadata.get("test_results", [])[1:])
    return new_meta


def run_workflow_rules(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """Run all enabled rules against `metadata`, return combined findings."""
    metadata = _apply_empty_step_exception(metadata)
    findings: list[dict[str, Any]] = []
    for rule in _RULES:
        findings.extend(rule(metadata))
    return filter_enabled(findings)
