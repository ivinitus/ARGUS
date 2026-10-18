"""Shared finding-triage inference — single source of truth."""
from __future__ import annotations

import re
from typing import Any

from argus.core.redaction import redact_credentials

VALID_SEVERITIES = {"high", "medium", "low"}
VALID_FINDING_CATEGORIES = {
    "tester_error",
    "product_issue",
    "environment_issue",
    "insufficient_evidence",
    "policy_exception",
}
VALID_FINDING_ACTIONS = {
    "re_run",
    "attach_defect",
    "fix_status",
    "add_evidence",
    "review_manually",
    "none",
}

DETERMINISTIC_SOURCES = frozenset({"rule", "env_check", "device_check"})

_TITLE_CONTEXT_PREFIX = re.compile(
    r"^(?:the\s+)?(?:test\s+)?(?:step|expected result|requirement)\s+"
    r"(?:asks|requires|expects|instructs|says|is\s+to)\b",
    re.IGNORECASE,
)
_TITLE_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_TITLE_CONTRAST_SPLIT = re.compile(
    r"\b(?:but|however|instead)\b[:,]?\s*",
    re.IGNORECASE,
)
_BACKGROUND_NOTIFICATION = re.compile(
    r"\bbackground (?:system )?notification\b",
    re.IGNORECASE,
)
_OUTSIDE_TESTED_FLOW = re.compile(
    r"\bnot part of (?:the )?tested flow\b",
    re.IGNORECASE,
)
_QUOTED_ALERT_TEXT = re.compile(
    r"\b(?:notification|alert)\s+(?:says|shows|reads)\s+"
    r"(?P<quote>['\"])(?P<message>.*?)(?P=quote)",
    re.IGNORECASE,
)
_TOPIC_AUXILIARY = re.compile(
    r"\b(?:has|have|had|is|are|was|were|will|would|can|could|"
    r"does|do|did)\b",
    re.IGNORECASE,
)
_WHILE_CONTEXT = re.compile(
    r"\bwhile\s+(?:the\s+)?"
    r"(?P<context>[a-z][a-z0-9_-]*(?:\s+[a-z][a-z0-9_-]*){0,2})"
    r"\s+(?:still\s+)?(?:shows?|displays?|remains?|continues?|"
    r"contains?|is|was|has)\b",
    re.IGNORECASE,
)
_LEADING_TOPIC_WORDS = frozenset({
    "a",
    "an",
    "our",
    "the",
    "their",
    "this",
    "your",
})

INSUFFICIENT_EVIDENCE_TERMS = (
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
STRONG_CONTRADICTION_TERMS: tuple[str, ...] = (
    "contradict",
    "wrong outcome",
    "does not match the expected",
    "not match expected",
    "instead of the expected",
    "opposite of",
    "different test",
    "unrelated to the test",
    "wrong device",
    "wrong browser",
    "invalidates",
)

ENVIRONMENT_TERMS = (
    "production url",
    "prod url",
    "feature-preprod",
    "marketplace",
    "currency",
    "locale",
    "storefront",
    "example.",
)
PRODUCT_ISSUE_TERMS = (
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


def _title_comparison_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _opening_sentence(description: str) -> str:
    return _TITLE_SENTENCE_SPLIT.split(description, maxsplit=1)[0].rstrip(" .")


def _title_repeats_description(title: str, description: str) -> bool:
    title_key = _title_comparison_key(title)
    opening_key = _title_comparison_key(_opening_sentence(description))
    if not title_key or not opening_key:
        return False
    if title_key == opening_key:
        return True
    return (
        len(title_key.split()) >= 12
        and (
            title_key.startswith(opening_key)
            or opening_key.startswith(title_key)
        )
    )


def _notification_topic(description: str) -> str | None:
    """Extract a short subject from quoted alert text without product cases."""
    match = _QUOTED_ALERT_TEXT.search(description)
    if not match:
        return None
    message = match.group("message").strip()
    topic = _TOPIC_AUXILIARY.split(message, maxsplit=1)[0]
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]*", topic)
    while words and words[0].casefold() in _LEADING_TOPIC_WORDS:
        words.pop(0)
    return " ".join(words[:6]) or None


def _concurrent_context(description: str) -> str | None:
    """Extract the short UI context named by a concurrent ``while`` clause."""
    match = _WHILE_CONTEXT.search(description)
    if not match:
        return None
    context = " ".join(match.group("context").split()).strip(" .,:;-")
    if not context:
        return None
    if context.casefold().endswith(" still"):
        context = context[:-6].rstrip()
    return context[0].upper() + context[1:]


def _summarise_repetitive_title(description: str) -> str | None:
    """Summarise common verbose observations by their significance."""
    if (
        _BACKGROUND_NOTIFICATION.search(description)
        and _OUTSIDE_TESTED_FLOW.search(description)
    ):
        topic = _notification_topic(description)
        subject = f"{topic} alert" if topic else "background notification"
        context = _concurrent_context(description)
        activity = f"{context} verification" if context else "testing"
        return f"Unrelated {subject} appears during {activity}"
    return None


def derive_finding_title(finding: dict[str, Any]) -> str:
    """Return a meaningful headline without clipping the description."""
    description_value = finding.get("description")
    description = (
        " ".join(redact_credentials(description_value).split())
        if isinstance(description_value, str) and description_value.strip()
        else ""
    )
    explicit = finding.get("title")
    if isinstance(explicit, str) and explicit.strip():
        explicit_title = (
            " ".join(redact_credentials(explicit).split()).rstrip(" .")
        )
        if explicit_title and (
            not description
            or not _title_repeats_description(
                explicit_title,
                description,
            )
        ):
            return explicit_title
        summary = _summarise_repetitive_title(description)
        if summary:
            return summary

    if not description:
        return "Finding"
    summary = _summarise_repetitive_title(description)
    if summary:
        return summary
    sentences = [
        sentence.strip()
        for sentence in _TITLE_SENTENCE_SPLIT.split(description)
        if sentence.strip()
    ]

    candidate = ""
    for sentence in sentences:
        if _TITLE_CONTEXT_PREFIX.match(sentence):
            contrast = _TITLE_CONTRAST_SPLIT.split(sentence, maxsplit=1)
            if len(contrast) == 2 and contrast[1].strip():
                candidate = contrast[1].strip()
                break
            continue
        candidate = sentence
        break

    if not candidate:
        candidate = sentences[-1] if sentences else description
    candidate = re.sub(
        r"^(?:however|but|instead)[,:]?\s+",
        "",
        candidate,
        flags=re.IGNORECASE,
    ).rstrip(" .")
    if candidate:
        candidate = candidate[0].upper() + candidate[1:]
    return candidate or "Finding"
VISUAL_CLAIM_TERMS = (
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


def infer_finding_category(finding: dict[str, Any]) -> str:
    """Best-effort category for old model/rule findings."""
    if finding.get("rule") in NO_ACTION_RULES:
        return "policy_exception"
    category = finding.get("category")
    if category in VALID_FINDING_CATEGORIES:
        return category
    desc = (finding.get("description") or "").lower()
    if any(t in desc for t in INSUFFICIENT_EVIDENCE_TERMS):
        return "insufficient_evidence"
    rule = finding.get("rule")
    source = finding.get("source")
    if source == "env_check" or rule in {"R9", "R9-doc", "R13"}:
        return "environment_issue"
    if rule in {"R12", "R15a", "R15b", "R15c"}:
        return "insufficient_evidence"
    if rule in {
        "R0", "R1", "R2", "R2b", "R4", "R5", "R6", "R7", "R8", "R10",
        "R11", "R14", "R16", "R17", "R18", "R20", "R21",
    }:
        return "tester_error"
    if any(t in desc for t in ENVIRONMENT_TERMS):
        return "environment_issue"
    if any(t in desc for t in PRODUCT_ISSUE_TERMS):
        return "product_issue"
    return "tester_error"


def infer_finding_action(finding: dict[str, Any], category: str) -> str:
    rule = finding.get("rule")
    if rule in NO_ACTION_RULES:
        return "none"
    if rule == "R22":
        return "add_evidence"
    action = finding.get("action")
    if action in VALID_FINDING_ACTIONS:
        return action
    if category == "insufficient_evidence":
        return "add_evidence"
    if category == "environment_issue":
        return "re_run"
    if category == "product_issue":
        return "attach_defect"
    if category == "policy_exception":
        return "review_manually"
    if rule in {"R2", "R2b", "R4", "R5", "R6", "R7", "R8", "R16", "R18"}:
        return "fix_status"
    if rule in {"R10", "R11"}:
        return "attach_defect"
    return "review_manually"


def infer_finding_confidence(finding: dict[str, Any], category: str) -> str:
    confidence = finding.get("confidence")
    if confidence in VALID_SEVERITIES:
        return confidence
    if finding.get("source") in DETERMINISTIC_SOURCES:
        return "high"
    if isinstance(finding.get("page"), int):
        return "medium"
    if category == "insufficient_evidence" and isinstance(
        finding.get("step_index"), int
    ):
        return "medium"
    return "low"


def looks_like_unpage_grounded_visual_claim(finding: dict[str, Any]) -> bool:
    if finding.get("source") in DETERMINISTIC_SOURCES:
        return False
    if finding.get("source") == "video" and any(
        isinstance(ref, dict) and isinstance(ref.get("start_seconds"), (int, float))
        for ref in (finding.get("_video_evidence_refs") or [])
    ):
        return False
    if isinstance(finding.get("page"), int):
        return False
    desc = (finding.get("description") or "").lower()
    if any(t in desc for t in INSUFFICIENT_EVIDENCE_TERMS):
        return False
    return any(t in desc for t in VISUAL_CLAIM_TERMS + PRODUCT_ISSUE_TERMS)


def is_contradiction_escalation_candidate(finding: dict[str, Any]) -> bool:
    """True when a medium model finding's own text asserts a contradiction."""
    if finding.get("severity") != "medium":
        return False
    if finding.get("_audio_verification_gap"):
        return False
    if finding.get("assessment_status") == "needs_review":
        return False
    if finding.get("source") in {"rule", "env_check", "device_check"}:
        return False
    if "_severity_adjusted_from" in finding:
        return False
    if not (isinstance(finding.get("page"), int)
            or isinstance(finding.get("step_index"), int)):
        return False
    desc = (finding.get("description") or "").lower()
    return any(t in desc for t in STRONG_CONTRADICTION_TERMS)


def apply_contradiction_escalation(finding: dict[str, Any]) -> bool:
    """Escalate a contradiction-worded medium to high, in place."""
    if not is_contradiction_escalation_candidate(finding):
        return False
    finding["_severity_adjusted_from"] = "medium"
    finding["severity"] = "high"
    finding["action"] = "review_manually"
    return True


NUDGE_RULES = frozenset({"R9-nav", "R9-susp", "R22"})
NO_ACTION_RULES = frozenset({"R9-nav", "R9-susp"})

STATUS_RULES = frozenset({"R20"})

_ORDER_DIAGNOSTIC_TERMS = ("order diagnostics", "claim code", "claim-code")


def is_advisory_finding(finding: dict[str, Any]) -> bool:
    """Return whether a finding is visible but verdict-neutral."""
    explicit = finding.get("advisory")
    if isinstance(explicit, bool):
        return explicit
    if finding.get("rule") in NUDGE_RULES:
        return True
    if finding.get("severity") != "low":
        return False
    if finding.get("source") in DETERMINISTIC_SOURCES:
        return False
    description = str(finding.get("description") or "").lower()
    return not any(term in description for term in _ORDER_DIAGNOSTIC_TERMS)


def derive_verdict(findings: list[dict[str, Any]]) -> str:
    """Derive a verdict exclusively from final normalized findings."""
    usable = [f for f in findings if isinstance(f, dict)]
    has_high = any(
        f.get("severity") == "high"
        and f.get("assessment_status") != "needs_review"
        and not is_advisory_finding(f)
        and f.get("rule") not in STATUS_RULES
        for f in usable
    )
    if has_high:
        return "fail"
    if any(not is_advisory_finding(f) for f in usable):
        return "concerns"
    return "pass"


def reconcile_verdict(audit: dict[str, Any]) -> dict[str, Any]:
    """Reconcile overall_verdict with final findings."""
    findings = [
        f for f in (audit.get("findings") or []) if isinstance(f, dict)
    ]
    severity_rank = {"pass": 0, "concerns": 1, "fail": 2}
    proposed = derive_verdict(findings)
    needs_review = (
        audit.get("review_status") == "required"
        or any(f.get("assessment_status") == "needs_review" for f in findings)
    )
    if needs_review and proposed == "pass":
        proposed = "concerns"
    current = audit.get("overall_verdict", "concerns")
    if proposed == current and not needs_review:
        return audit

    out = dict(audit)
    if needs_review:
        out["review_status"] = "required"
    out["overall_verdict"] = proposed
    if proposed == current:
        return out
    if severity_rank.get(proposed, 0) > severity_rank.get(current, 0):
        out["_verdict_escalated_from"] = current
        out.pop("_verdict_demoted_from", None)
    else:
        out["_verdict_demoted_from"] = current
        out.pop("_verdict_escalated_from", None)
    return out


def require_review(finding: dict[str, Any], reason: str) -> dict[str, Any]:
    """Retain an unresolved candidate without calling it a confirmed deviation."""
    out = dict(finding)
    out["assessment_status"] = "needs_review"
    out["review_reason"] = reason
    out["confidence"] = "low"
    out["action"] = "review_manually"
    out["advisory"] = False
    if out.get("severity") == "high":
        out["_severity_adjusted_from"] = "high"
        out["severity"] = "medium"
    return out


def normalise_findings_for_schema(
    findings: list[Any],
) -> list[Any]:
    """Backfill category/action/confidence, demote un-grounded high claims, escalate contradiction-worded mediums."""
    out: list[Any] = []
    for raw in findings:
        if not isinstance(raw, dict):
            out.append(raw)
            continue
        f = dict(raw)
        if isinstance(f.get("description"), str):
            f["description"] = redact_credentials(f["description"])
        if f.get("source") == "custom_rule":
            # The custom evaluator has already grounded and classified this
            # finding and applied the lead's severity/advisory setting.
            out.append(f)
            continue
        f["title"] = derive_finding_title(f)
        category = infer_finding_category(f)
        f["category"] = category
        f["action"] = infer_finding_action(f, category)
        f["confidence"] = infer_finding_confidence(f, category)
        if "advisory" not in f and f.get("severity") == "low":
            description = str(f.get("description") or "").lower()
            f["advisory"] = (
                f.get("source") not in DETERMINISTIC_SOURCES
                and not any(
                    term in description for term in _ORDER_DIAGNOSTIC_TERMS
                )
            )

        if "_severity_adjusted_from" not in f:
            if (
                f.get("severity") == "high"
                and category != "insufficient_evidence"
                and looks_like_unpage_grounded_visual_claim(f)
            ):
                f["_severity_adjusted_from"] = "high"
                f["severity"] = "medium"
                f["confidence"] = "low"
                f["action"] = "review_manually"
            else:
                apply_contradiction_escalation(f)
        out.append(f)
    return out
