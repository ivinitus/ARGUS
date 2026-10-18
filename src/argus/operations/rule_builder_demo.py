"""Local prototype: fixed Python checks over explicit sample execution facts.

These definitions are accepted only by DemoRuleService. Hosted publication and
workers retain their existing document schema until this design is reviewed.
No user-supplied code, expressions, or model calls are executed here.
"""
from __future__ import annotations

from copy import deepcopy
import json
import re
from time import perf_counter
from typing import Any, Callable
import uuid

from argus.domain import team_rules
from argus.operations import rule_authoring_demo as authoring
from argus.operations.rule_service import RuleError, RuleService, initialize, revision_key
from argus.operations.teams import resolve_team

FIELDS: dict[str, dict[str, Any]] = {
    "status": {"label": "Execution status", "values": {
        "failed": "Failed", "passed": "Passed", "blocked": "Blocked", "not_run": "Not run",
    }},
    "test_type": {"label": "Test type", "values": {
        "purchase": "Purchase", "playback": "Playback",
    }},
    "marketplace": {"label": "Marketplace", "values": {
        "US": "US", "UK": "UK", "DE": "DE",
    }},
}
CHECKS = {
    "defect_reference": {
        "label": "Defect reference is recorded",
        "help": "Finds a DEMO-123 style reference in the selected comments. It does not verify the linked defect.",
    },
    "test_account": {
        "label": "Test account is recorded",
        "help": "Finds an email-format account identifier in the selected comments. It does not verify the account.",
    },
    "attachment_count": {
        "label": "Minimum attachment count",
        "help": "Counts attached files. File contents are not assessed by this check.",
    },
    "field_value": {
        "label": "Field comparison",
        "help": "Check recorded fields, text, allowed values, or counts. Text comparisons ignore case. Missing input requires review.",
    },
}
SCOPES = {
    "all": "Execution and step comments",
    "execution": "Execution comments",
    "steps": "Step comments",
}
SAMPLES: dict[str, dict] = {
    "DEMO-E201": {
        "title": "Failed purchase · missing defect",
        "status": "failed", "test_type": "purchase", "marketplace": "US",
        "comment": "Checkout failed at payment.", "steps": ["Payment returned an error."],
        "attachments": ["checkout.png"],
        "confirmation_matches_item": False,
    },
    "DEMO-E202": {
        "title": "Failed purchase · defect recorded",
        "status": "failed", "test_type": "purchase", "marketplace": "UK",
        "comment": "Test account: audit@example.test",
        "steps": ["Payment failed. Defect DEMO-42 records the issue."],
        "attachments": ["checkout.png", "error.png"],
        "confirmation_matches_item": True,
    },
    "DEMO-E203": {
        "title": "Passed playback · no defect",
        "status": "passed", "test_type": "playback", "marketplace": "US",
        "comment": "Playback completed.", "steps": [], "attachments": [],
    },
    "DEMO-E204": {
        "title": "Blocked purchase · missing account",
        "status": "blocked", "test_type": "purchase", "marketplace": "DE",
        "comment": "A required dependency is unavailable.", "steps": [], "attachments": [],
    },
}


def catalog() -> dict:
    return {"fields": FIELDS, "checks": CHECKS, "scopes": SCOPES,
            "valueFields": authoring.VALUE_FIELDS, "operators": authoring.OPERATORS,
            "textExamples": {"structured": authoring.DETERMINISTIC_EXAMPLE, "evidence": authoring.EVIDENCE_EXAMPLE}}


def validate_rule(raw: Any) -> dict:
    fields = {"id", "kind", "name", "enabled", "match", "conditions", "check", "severity", "mode"}
    if not isinstance(raw, dict) or set(raw) - {"source_text"} != fields or raw["kind"] != "structured":
        raise team_rules.RuleValidationError("A Python check contains missing or unsupported fields.")
    rule = deepcopy(raw)
    if rule["id"] == "":
        rule["id"] = "C-" + uuid.uuid4().hex
    if not isinstance(rule["id"], str) or not team_rules.CUSTOM_ID.fullmatch(rule["id"]):
        raise team_rules.RuleValidationError("Use a valid rule identifier.")
    rule["name"] = team_rules._text(rule["name"], "Rule name", 160)
    if "source_text" in rule:
        rule["source_text"] = team_rules._text(rule["source_text"], "Rule description", 4000)
    if type(rule["enabled"]) is not bool:
        raise team_rules.RuleValidationError("Rule enabled must be true or false.")
    if rule["severity"] not in ("low", "medium", "high") or rule["mode"] not in ("advisory", "enforce"):
        raise team_rules.RuleValidationError("Choose a supported severity and impact.")
    if rule["match"] not in ("all", "any"):
        raise team_rules.RuleValidationError("Choose all or any conditions.")
    conditions = rule["conditions"]
    if not isinstance(conditions, list) or len(conditions) > 5:
        raise team_rules.RuleValidationError("Use at most five conditions; no conditions applies to all executions.")
    for condition in conditions:
        if (
            not isinstance(condition, dict) or set(condition) != {"field", "operator", "value"}
            or not all(isinstance(value, str) for value in condition.values())
            or condition["field"] not in FIELDS
            or condition["operator"] not in ("is", "is_not")
            or condition["value"] not in FIELDS[condition["field"]]["values"]
        ):
            raise team_rules.RuleValidationError("Choose a supported condition, operator, and value.")
    check = rule["check"]
    if not isinstance(check, dict) or not isinstance(check.get("type"), str) or check["type"] not in CHECKS:
        raise team_rules.RuleValidationError("Choose a supported Python check.")
    if check["type"] == "field_value":
        authoring.validate_check(check)
    elif check["type"] == "attachment_count":
        if set(check) != {"type", "minimum"} or type(check["minimum"]) is not int or not 1 <= check["minimum"] <= 100:
            raise team_rules.RuleValidationError("Minimum attachments must be an integer between 1 and 100.")
    elif set(check) != {"type", "scope"} or check["scope"] not in tuple(SCOPES):
        raise team_rules.RuleValidationError("Choose which comments to check.")
    return rule


def validate_document(raw: Any) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("custom_rules"), list):
        return team_rules.validate_document(raw)
    rules = raw["custom_rules"]
    if len(rules) > team_rules.MAX_CUSTOM_RULES:
        raise team_rules.RuleValidationError(f"Use at most {team_rules.MAX_CUSTOM_RULES} custom rules.")
    def structured(rule: Any) -> bool:
        return isinstance(rule, dict) and rule.get("kind") == "structured"
    result = team_rules.validate_document({**raw, "custom_rules": [
        {k: v for k, v in rule.items() if k != "source_text"} if isinstance(rule, dict) else rule
        for rule in rules if not structured(rule)
    ]})
    evidence = iter(result["custom_rules"])
    result["custom_rules"] = [validate_rule(rule) if structured(rule) else next(evidence) for rule in rules]
    for source, rule in zip(rules, result["custom_rules"], strict=True):
        if "source_text" in source:
            rule["source_text"] = team_rules._text(source["source_text"], "Rule description", 4000)
    identifiers = [rule["id"] for rule in result["custom_rules"]]
    if len(set(identifiers)) != len(identifiers):
        raise team_rules.RuleValidationError("Custom rule identifiers must be unique.")
    if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > team_rules.MAX_DOCUMENT_BYTES:
        raise team_rules.RuleValidationError("Policy is too large.")
    return result


def evaluate(rule: dict, sample: dict) -> dict:
    """Evaluate registered operations, retaining unknown applicability/evidence."""
    trace = []
    for condition in rule["conditions"]:
        field, value = condition["field"], sample.get(condition["field"])
        matched = None if value is None else (
            value == condition["value"] if condition["operator"] == "is" else value != condition["value"]
        )
        trace.append({"field": field, "actual": value, "matched": matched})
    result = {"rule": rule["id"], "name": rule["name"], "conditions": trace}
    matches = [item["matched"] for item in trace]
    unknown = None in matches
    applies = not trace or (all(matches) if rule["match"] == "all" else any(matches))
    uncertain = unknown and (False not in matches if rule["match"] == "all" else True not in matches)
    if rule["enabled"] and uncertain:
        return {**result, "status": "uncertain", "description": "The supplied data cannot establish whether this rule applies. Needs review."}
    if not rule["enabled"] or not applies:
        return {**result, "status": "not_applicable", "description": (
            "This rule is disabled." if not rule["enabled"] else "The execution does not match the rule conditions."
        )}
    check = rule["check"]
    if check["type"] == "field_value":
        status, description = authoring.evaluate_check(check, sample)
        return {**result, "status": status, "description": description}
    if check["type"] == "attachment_count":
        if not isinstance(sample.get("attachments"), list):
            return {**result, "status": "uncertain", "description": "The attachment inventory was not supplied. Needs review."}
        count = len(sample["attachments"])
        met = count >= check["minimum"]
        description = f"{count} attachment(s) recorded; at least {check['minimum']} required."
    else:
        texts = []
        if check["scope"] in {"all", "execution"}:
            texts.append(sample.get("comment") or "")
        if check["scope"] in {"all", "steps"}:
            texts.extend(sample.get("steps") or [])
        pattern = r"\bDEMO-\d+\b" if check["type"] == "defect_reference" else r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
        found = next((match.group() for text in texts if (match := re.search(pattern, text))), None)
        complete = (
            (check["scope"] == "steps" or isinstance(sample.get("comment"), str))
            and (check["scope"] == "execution" or isinstance(sample.get("steps"), list))
        )
        if found is None and not complete:
            return {**result, "status": "uncertain", "description": "The selected comments were not fully supplied. Needs review."}
        met = found is not None
        noun = "Defect reference" if check["type"] == "defect_reference" else "Account identifier"
        description = (
            f"{noun} {found} found in {SCOPES[check['scope']].lower()}."
            if found else f"No {noun.lower()} found in {SCOPES[check['scope']].lower()}."
        )
    return {**result, "status": "met" if met else "violated", "description": description}


def apply(document: dict, sample: dict, audit: dict) -> dict:
    assessments = []
    rank = {"pass": 0, "concerns": 1, "fail": 2}
    for rule in document["custom_rules"]:
        if rule.get("kind") != "structured":
            continue
        assessment = evaluate(rule, sample)
        assessments.append(assessment)
        if assessment["status"] not in {"violated", "uncertain"}:
            continue
        advisory = rule["mode"] == "advisory" or rule["severity"] == "low"
        finding = {
            "rule": rule["id"], "title": rule["name"], "source": "structured_rule",
            "description": assessment["description"], "severity": rule["severity"],
            "advisory": advisory, "confidence": "high",
        }
        proposed = "pass" if advisory else {"high": "fail", "medium": "concerns"}[rule["severity"]]
        if assessment["status"] == "uncertain":
            finding.update(assessment_status="needs_review", confidence="low")
            if not advisory:
                audit["review_status"] = "required"
                proposed = "concerns"
        audit["findings"].append(finding)
        if rank[proposed] > rank[audit["overall_verdict"]]:
            audit["overall_verdict"] = proposed
    audit["structured_assessments"] = assessments
    return audit


def definition_key(rule: dict) -> str:
    """Compare execution definitions, independent of names, IDs, and impact."""
    if rule.get("kind") == "structured":
        conditions = sorted({json.dumps(row, sort_keys=True) for row in rule["conditions"]})
        check = deepcopy(rule["check"])
        if check.get("operator") == "in":
            check["value"] = sorted({value.casefold() for value in check["value"]})
        elif check["type"] == "field_value" and isinstance(check.get("value"), str):
            check["value"] = check["value"].casefold()
        # The generic count comparison and the original count preset execute the same operation.
        if check["type"] == "attachment_count":
            check = {"type": "field_value", "field": "attachment_count", "operator": "gte", "value": check["minimum"]}
        value = {"kind": "structured", "match": rule["match"] if len(conditions) > 1 else "all",
                 "conditions": conditions, "check": check}
    else:
        value = {key: rule[key] for key in ("applies_when", "requirement", "expected_evidence", "examples")}
        value.update({key: sorted(set(rule[key])) for key in ("case_keys", "run_keys")})
        value = {key: item.strip() if isinstance(item, str) else item for key, item in value.items()}
    return team_rules.fingerprint(value)


def related_rules(rule: dict, document: dict) -> list[dict]:
    matches = []
    key = definition_key(rule)
    for existing in document["custom_rules"]:
        if existing["id"] == rule["id"]:
            continue
        duplicate = key == definition_key(existing)
        same_check = (
            rule.get("kind") == existing.get("kind") == "structured"
            and rule["check"]["type"] == existing["check"]["type"]
            and (rule["check"]["type"] != "field_value" or rule["check"]["field"] == existing["check"]["field"])
        )
        same_name = rule["name"].casefold() == existing["name"].casefold()
        if duplicate or same_check or same_name:
            matches.append({
                "id": existing["id"], "name": existing["name"], "enabled": existing["enabled"],
                "kind": "duplicate" if duplicate else "overlap",
                "description": "The same execution definition already exists." if duplicate else
                    "These rules share a check or name. Their conditions or requirements may differ; compare them.",
            })
    if rule.get("kind") == "structured":
        builtin = {"defect_reference": "R11", "test_account": "R22", "attachment_count": "R12"}.get(rule["check"]["type"])
        if rule["check"]["type"] == "field_value" and rule["check"]["field"] == "attachment_count":
            builtin = "R12"
        if builtin:
            policy = document["policy"]
            enabled = builtin not in policy["disabled_rules"] and (not policy["enabled_rules"] or builtin in policy["enabled_rules"])
            if builtin == "R22":
                enabled = enabled and policy["r22_mode"] != "disabled"
            matches.append({"id": builtin, "name": team_rules.RULE_NAMES[builtin], "enabled": enabled, "kind": "overlap",
                            "description": "This built-in check covers related evidence. Its scope and assessment may differ."})
    elif not matches:
        # Free-text equivalence cannot be established by string comparison.
        for existing in document["custom_rules"]:
            if existing["id"] != rule["id"] and existing.get("kind") != "structured":
                matches.append({"id": existing["id"], "name": existing["name"], "enabled": existing["enabled"],
                                "kind": "review", "description": "Existing text rule. Different wording may overlap; semantic equivalence has not been assessed."})
    return matches


class DemoRuleService(RuleService):
    def __init__(self, store: Any, can_edit: Callable[[str, str], bool],
                 launch_preview: Callable[[str, str, str], Any], reports: Any = None,
                 *, settings_only: bool = True):
        super().__init__(store, can_edit, launch_preview, reports)
        self.settings_only = settings_only

    def validate_document(self, document: Any) -> dict:
        return validate_document(document)

    def _change(self, team: str, route: str, actor: str, body: dict,
                request_id: str, request_hash: str) -> dict:
        if route == "draft" and set(body) == {"version", "document"}:
            head = initialize(self.store, team)
            if type(body["version"]) is int and body["version"] == head["version"]:
                baseline = self.store.get(team, head.get("draft_key") or revision_key(head["active_revision"]))["document"]
                candidate = self.validate_document(body["document"])
                if self.settings_only and candidate["evidence_policy"] != baseline["evidence_policy"]:
                    raise RuleError(400, "The shared evidence policy is read-only in this demo. Create or edit an evidence rule instead.")
                old_keys = {rule["id"]: definition_key(rule) for rule in baseline["custom_rules"]}
                seen: dict[str, dict] = {}
                for rule in candidate["custom_rules"]:
                    key = definition_key(rule)
                    if key in seen and not (
                        old_keys.get(rule["id"]) == key == old_keys.get(seen[key]["id"])
                    ):
                        raise RuleError(400, f'This duplicates "{seen[key]["name"]}". Edit or re-enable the existing rule.')
                    seen[key] = rule
        return super()._change(team, route, actor, body, request_id, request_hash)

    def handle(self, team: str, route: str, method: str, actor: str,
               body: dict, request_id: str = "", query: dict | None = None) -> dict:
        if route not in {"builder/test", "builder/interpret", "builder/inspect"} or method != "POST":
            return super().handle(team, route, method, actor, body, request_id, query)
        resolved = resolve_team(team)
        if resolved is None or resolved.slug != team:
            raise RuleError(404, "Unknown team.")
        if not self.can_edit(team, actor):
            raise RuleError(403, "You don't have permission to edit these rules.")
        try:
            if route == "builder/interpret":
                if set(body) != {"text"}:
                    raise RuleError(400, "Send one rule description.")
                return authoring.interpret(body["text"])
            if set(body) != {"rule"}:
                raise RuleError(400, "Send one rule to inspect or test.")
            raw = body["rule"]
            rule = validate_rule(raw) if isinstance(raw, dict) and raw.get("kind") == "structured" else authoring.validated_evidence(raw, team)
        except team_rules.RuleValidationError as exc:
            raise RuleError(400, str(exc)) from exc
        state = self.state(team, actor)
        matches = related_rules(rule, (state["draft"] or state["active"])["document"])
        if route == "builder/inspect":
            return {"matches": matches, "semantic_comparison": "not_performed"}
        structured = rule.get("kind") == "structured"
        started = perf_counter()
        results = [
            {"execution": key, "sample": sample, **(evaluate(rule, sample) if structured else authoring.simulate_evidence(rule, sample))}
            for key, sample in SAMPLES.items()
        ]
        return {"engine": "python" if structured else "simulation", "model_calls": 0, "results": results,
                "matches": matches, "elapsed_ms": round((perf_counter() - started) * 1000, 3),
                "production_latency_ms": None, "production_accuracy": None, "simulated": not structured}
