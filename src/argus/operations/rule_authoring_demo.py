"""Bounded local authoring examples, not a general natural-language compiler.

Only complete, recognised sentences are translated. Unrecognised wording is
returned for clarification. Evidence outcomes are fixtures, never model claims.
"""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from argus.domain import team_rules

VALUE_FIELDS = {
    "comment": {"label": "Execution comment", "type": "text"},
    "step_comments": {"label": "Combined step comments", "type": "text"},
    "marketplace": {"label": "Marketplace", "type": "text"},
    "status": {"label": "Execution status", "type": "text"},
    "test_type": {"label": "Test type", "type": "text"},
    "attachment_count": {"label": "Attachment count", "type": "number"},
}
OPERATORS = {
    "text": {"present": "is recorded", "absent": "is blank", "is": "equals",
             "is_not": "does not equal", "contains": "contains", "not_contains": "does not contain",
             "in": "is one of"},
    "number": {"is": "equals", "is_not": "does not equal", "gte": "is at least", "lte": "is at most"},
}
DETERMINISTIC_EXAMPLE = "Failed purchase tests need at least two attachments."
EVIDENCE_EXAMPLE = "For purchase tests, screenshots must show confirmation for the purchased item."
EVIDENCE_REQUIREMENT = "Screenshots must show confirmation for the purchased item."
EVIDENCE_EXPECTED = "A screenshot showing purchase confirmation and the matching item."
EMAIL_REQUIREMENT = "A successful purchase must have a confirmation email in the supplied evidence."
EMAIL_EXPECTED = "An email showing the purchased item and successful transaction."


def validate_check(check: dict) -> None:
    if (
        set(check) != {"type", "field", "operator", "value"}
        or not isinstance(check["field"], str) or check["field"] not in VALUE_FIELDS
        or not isinstance(check["operator"], str)
        or check["operator"] not in OPERATORS[VALUE_FIELDS[check["field"]]["type"]]
    ):
        raise team_rules.RuleValidationError("Choose a supported field and comparison.")
    operator, value = check["operator"], check["value"]
    if operator in {"present", "absent"}:
        if value is not None:
            raise team_rules.RuleValidationError("Presence checks do not take a comparison value.")
    elif VALUE_FIELDS[check["field"]]["type"] == "number":
        if type(value) is not int or not 0 <= value <= 100_000:
            raise team_rules.RuleValidationError("Use a whole number between 0 and 100000.")
    elif operator == "in":
        if not isinstance(value, list) or not 1 <= len(value) <= 30:
            raise team_rules.RuleValidationError("Enter between one and thirty allowed values.")
        for item in value:
            team_rules._text(item, "Allowed value", 200)
    else:
        team_rules._text(value, "Comparison value", 200)


def evaluate_check(check: dict, sample: dict) -> tuple[str, str]:
    field = check["field"]
    actual: Any
    if field == "step_comments":
        actual = "\n".join(sample["steps"]) if isinstance(sample.get("steps"), list) else None
    elif field == "attachment_count":
        actual = len(sample["attachments"]) if isinstance(sample.get("attachments"), list) else None
    else:
        actual = sample.get(field)
    operator, expected = check["operator"], check["value"]
    label = VALUE_FIELDS[field]["label"]
    if operator in {"present", "absent"}:
        supplied = "steps" in sample if field == "step_comments" else field in sample
        if not supplied:
            return "uncertain", f"{label} was not supplied; this check needs review."
        met = bool(str(actual or "").strip()) == (operator == "present")
    elif actual is None:
        return "uncertain", f"{label} was not supplied; this check needs review."
    elif VALUE_FIELDS[field]["type"] == "number":
        met = {"is": actual == expected, "is_not": actual != expected,
               "gte": actual >= expected, "lte": actual <= expected}[operator]
    else:
        actual = str(actual).casefold()
        if operator == "in":
            met = actual in [value.casefold() for value in expected]
        else:
            expected = expected.casefold()
            met = {"is": actual == expected, "is_not": actual != expected,
                   "contains": expected in actual, "not_contains": expected not in actual}[operator]
    requirement = OPERATORS[VALUE_FIELDS[field]["type"]][operator]
    value_text = "" if expected is None else f" {expected!r}"
    return ("met" if met else "violated"), f"{label} {requirement}{value_text}: {'met' if met else 'not met'}."


def evidence_draft(text: str, *, example: bool = False) -> dict:
    return {
        "id": "", "name": "Purchase confirmation evidence" if example else "New evidence rule",
        "enabled": True, "applies_when": "Purchase tests" if example else "All audited executions",
        "case_keys": [], "run_keys": [],
        "requirement": EVIDENCE_REQUIREMENT if example else text,
        "expected_evidence": EVIDENCE_EXPECTED if example else "",
        "examples": "", "severity": "medium", "mode": "advisory", "source_text": text,
    }


def interpret(text: Any) -> dict:
    """Recognise the entire sentence, with no fuzzy matching or silent fallback."""
    text = team_rules._text(text, "Rule description", 4000)
    parts = text.split('"')
    sentence = '"'.join(re.sub(r"\s+", " ", part) if index % 2 == 0 else part
                        for index, part in enumerate(parts)).strip().removesuffix(".")
    result: dict = {"source_text": text, "interpreter": "demo_patterns_v1", "model_calls": 0}
    if sentence.casefold() == EVIDENCE_EXAMPLE.rstrip(".").casefold():
        return {**result, "route": "evidence", "rule": evidence_draft(text, example=True),
                "message": "This requirement needs screenshot interpretation. The demo uses labelled sample outcomes; real audit speed and accuracy are not measured."}
    match = re.fullmatch(
        r"(?:(?P<status>failed|passed|blocked|not run|all) )?"
        r"(?:(?P<test_type>purchase|playback) )?tests "
        r"(?:need|require|must have) (?P<requirement>.+)", sentence, re.IGNORECASE,
    )
    check = None
    if match:
        requirement = match["requirement"]
        count = re.fullmatch(r"(?:at least )?(\d+|one|two|three|four|five|six|seven|eight|nine|ten) attachments?", requirement, re.IGNORECASE)
        if count:
            numbers = dict(zip(("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"), range(1, 11), strict=True))
            minimum = numbers.get(count[1].lower(), int(count[1]) if count[1].isdigit() else 0)
            if 1 <= minimum <= 100:
                check = {"type": "attachment_count", "minimum": minimum}
        elif re.fullmatch(r"(?:a )?defect reference", requirement, re.IGNORECASE):
            check = {"type": "defect_reference", "scope": "all"}
        elif re.fullmatch(r"(?:a )?test account", requirement, re.IGNORECASE):
            check = {"type": "test_account", "scope": "all"}
        elif re.fullmatch(r"(?:an? )?execution comment", requirement, re.IGNORECASE):
            check = {"type": "field_value", "field": "comment", "operator": "present", "value": None}
        else:
            contains = re.fullmatch(r'(execution|step) comments? containing "([^"\n]{1,200})"', requirement, re.IGNORECASE)
            if contains:
                check = {"type": "field_value", "field": "comment" if contains[1].lower() == "execution" else "step_comments",
                         "operator": "contains", "value": contains[2]}
    if match and check:
        conditions = []
        if match["status"] and match["status"].lower() != "all":
            conditions.append({"field": "status", "operator": "is", "value": match["status"].lower().replace(" ", "_")})
        if match["test_type"]:
            conditions.append({"field": "test_type", "operator": "is", "value": match["test_type"].lower()})
        return {**result, "route": "structured", "message": "The complete sentence matches supported checks. Review the conditions and requirement before using this interpretation.",
                "rule": {"id": "", "kind": "structured", "name": sentence[:160], "enabled": True,
                         "match": "all", "conditions": conditions, "check": check,
                         "severity": "medium", "mode": "advisory", "source_text": text}}
    return {**result, "route": "clarify", "rule": None,
            "message": "The demo cannot translate this wording completely. Clarify it using a supported example, build the rule with controls, or explicitly choose AI evidence assessment. No rule has been changed."}


def simulate_evidence(rule: dict, sample: dict) -> dict:
    """Only exact fixture requirements get simulated pass/fail outcomes."""
    result = {"rule": rule["id"], "name": rule["name"], "conditions": [], "simulated": True}
    if not rule["enabled"]:
        return {**result, "status": "not_applicable", "description": "This rule is disabled."}
    if rule["case_keys"] or rule["run_keys"]:
        return {**result, "status": "uncertain", "description": "The sample does not supply the case/run identity needed to assess applicability."}
    signature = (rule["applies_when"], rule["requirement"], rule["expected_evidence"])
    email = signature == ("Purchase tests with a successful transaction", EMAIL_REQUIREMENT, EMAIL_EXPECTED)
    screenshot = signature == ("Purchase tests", EVIDENCE_REQUIREMENT, EVIDENCE_EXPECTED)
    if not email and not screenshot:
        return {**result, "status": "uncertain", "description": "This wording has no labelled simulation. Real evidence assessment is required; no model was called."}
    purchase = sample.get("purchase", sample.get("test_type") == "purchase" if "test_type" in sample else None)
    if purchase is False:
        return {**result, "status": "not_applicable", "description": "The sample is not a purchase test."}
    value = sample.get("email") if email else sample.get("confirmation_matches_item")
    if purchase is None or value is None:
        return {**result, "status": "uncertain", "description": "The sample has incomplete evidence. Needs review (simulated)."}
    return {**result, "status": "met" if value else "violated",
            "description": ("Required confirmation is present." if value else "Required confirmation is missing or does not match.") + " Simulated from labelled sample data."}


def validated_evidence(raw: Any, team: str) -> dict:
    document = team_rules.defaults(team)
    rule = deepcopy(raw)
    if not isinstance(rule, dict):
        raise team_rules.RuleValidationError("Send one evidence rule.")
    source = rule.pop("source_text", None)
    document["custom_rules"] = [rule]
    result = team_rules.validate_document(document)["custom_rules"][0]
    if source is not None:
        result["source_text"] = team_rules._text(source, "Rule description", 4000)
    return result
