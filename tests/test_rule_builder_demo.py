from copy import deepcopy
from unittest.mock import Mock
import uuid

import pytest

from argus.domain import team_rules
from argus.operations import rule_builder_demo as builder
from argus.operations.rule_demo import DemoRuleReports, finish_sample_preview, sample_audit
from argus.operations.rule_service import RuleError, RuleService
from argus.operations.rule_store import SqliteRuleStore


@pytest.fixture
def rule():
    return {
        "id": "", "kind": "structured", "name": "Failed tests need a defect",
        "enabled": True, "match": "all",
        "conditions": [{"field": "status", "operator": "is", "value": "failed"}],
        "check": {"type": "defect_reference", "scope": "all"},
        "severity": "medium", "mode": "enforce",
    }


def test_python_evaluation_is_repeatable_and_uses_recorded_scope(rule):
    outcomes = [builder.evaluate(rule, sample) for sample in builder.SAMPLES.values()]
    assert [row["status"] for row in outcomes] == ["violated", "met", "not_applicable", "not_applicable"]
    assert outcomes == [builder.evaluate(rule, sample) for sample in builder.SAMPLES.values()]
    assert "DEMO-42" in outcomes[1]["description"]
    rule["check"]["scope"] = "execution"
    assert builder.evaluate(rule, builder.SAMPLES["DEMO-E202"])["status"] == "violated"
    rule["check"]["scope"] = "steps"
    assert builder.evaluate(rule, builder.SAMPLES["DEMO-E202"])["status"] == "met"


def test_conditions_account_and_count_checks(rule):
    rule["conditions"].append({"field": "marketplace", "operator": "is", "value": "DE"})
    assert builder.evaluate(rule, builder.SAMPLES["DEMO-E204"])["status"] == "not_applicable"
    rule["match"] = "any"
    assert builder.evaluate(rule, builder.SAMPLES["DEMO-E204"])["status"] == "violated"
    rule["conditions"] = [{"field": "status", "operator": "is_not", "value": "not_run"}]
    assert builder.evaluate(rule, {})["status"] == "uncertain"
    rule["check"] = {"type": "test_account", "scope": "execution"}
    assert builder.evaluate(rule, builder.SAMPLES["DEMO-E202"])["status"] == "met"
    assert builder.evaluate(rule, builder.SAMPLES["DEMO-E201"])["status"] == "violated"
    rule["check"] = {"type": "attachment_count", "minimum": 2}
    assert builder.evaluate(rule, builder.SAMPLES["DEMO-E202"])["status"] == "met"
    assert builder.evaluate(rule, builder.SAMPLES["DEMO-E201"])["status"] == "violated"
    rule["enabled"] = False
    assert builder.evaluate(rule, builder.SAMPLES["DEMO-E201"])["status"] == "not_applicable"


@pytest.mark.parametrize("mode,severity,verdict", [
    ("advisory", "high", "pass"), ("enforce", "low", "pass"),
    ("enforce", "medium", "concerns"), ("enforce", "high", "fail"),
])
def test_verdict_impact(rule, mode, severity, verdict):
    rule.update(mode=mode, severity=severity)
    document = {**team_rules.defaults("desktop"), "custom_rules": [rule]}
    audit = sample_audit(builder.validate_document(document), "DEMO-E201")
    assert audit["overall_verdict"] == verdict
    assert any(finding["source"] == "structured_rule" for finding in audit["findings"])
    assert sample_audit(builder.validate_document(document), "DEMO-E202")["findings"] == []


@pytest.mark.parametrize("update", [
    {"id": None}, {"enabled": "true"}, {"match": "python"}, {"conditions": None},
    {"conditions": [{"field": "status", "operator": "eval", "value": "failed"}]},
    {"conditions": [{"field": "status", "operator": "is", "value": "__import__('os')"}]},
    {"conditions": [{"field": "unknown", "operator": "is", "value": "failed"}]},
    {"conditions": [{}] * 6},
    {"check": {"type": "python", "code": "print('hello')"}},
    {"check": {"type": "defect_reference", "scope": "all", "code": "print('hello')"}},
    {"check": {"type": "test_account", "scope": "unknown"}},
    {"check": {"type": "attachment_count", "minimum": True}},
    {"check": {"type": "attachment_count", "minimum": 1.5}},
    {"check": {"type": "attachment_count", "minimum": 0}},
    {"check": {"type": "attachment_count", "minimum": 101}},
    {"severity": "critical"}, {"mode": "override"}, {"code": "print('hello')"},
])
def test_rejects_unsupported_rule_definitions(rule, update):
    with pytest.raises(team_rules.RuleValidationError):
        builder.validate_rule({**rule, **update})


def test_existing_schema_stays_strict_and_demo_preserves_legacy_documents(rule):
    default = team_rules.defaults("desktop")
    assert builder.validate_document(default) == team_rules.validate_document(default)
    document = {**default, "custom_rules": [rule]}
    with pytest.raises(team_rules.RuleValidationError):
        team_rules.validate_document(document)
    validated = builder.validate_document(document)
    assert team_rules.CUSTOM_ID.fullmatch(validated["custom_rules"][0]["id"])
    duplicate = deepcopy(validated)
    duplicate["custom_rules"] *= 2
    with pytest.raises(team_rules.RuleValidationError, match="unique"):
        builder.validate_document(duplicate)
    store = SqliteRuleStore(":memory:")
    service = RuleService(store, lambda *_: True, Mock())
    state = service.state("desktop", "local-user")
    with pytest.raises(RuleError, match="unsupported"):
        service.handle("desktop", "draft", "POST", "local-user",
                       {"version": state["version"], "document": document}, uuid.uuid4().hex)


def test_demo_preview_save_publish_restore_and_model_calls(rule, monkeypatch):
    invoke = Mock(side_effect=AssertionError("The Python builder must not call a model"))
    monkeypatch.setattr("argus.pipeline.auditor.invoke_model", invoke)
    store = SqliteRuleStore(":memory:")
    service = builder.DemoRuleService(
        store, lambda *_: True,
        lambda team, identifier, actor: finish_sample_preview(store, team, identifier),
        DemoRuleReports(), settings_only=False,
    )
    def post(route, body):
        return service.handle("desktop", route, "POST", "local-user", body, uuid.uuid4().hex)
    result = post("builder/test", {"rule": rule})
    assert result["model_calls"] == 0
    assert len(result["results"]) == 4
    state = service.state("desktop", "local-user")
    document = {**state["active"]["document"], "custom_rules": [rule]}
    saved = post("draft", {"version": state["version"], "document": document})
    assert service.state("desktop", "local-user")["draft"]["document"] == saved["draft"]["document"]
    selection = DemoRuleReports().manifest("desktop", "rule-builder")
    preview = post("preview", {
        "version": saved["version"], "executions": list(builder.SAMPLES),
        "report": {"folder": "rule-builder", "revision": selection["revision"]},
    })
    result = store.get("desktop", "preview#" + preview["preview"]["id"])
    assert result["phase"] == "complete"
    assert result["results"][0]["after"]["overall_verdict"] == "concerns"
    published = post("publish", {
        "version": preview["version"], "preview_id": preview["preview"]["id"], "note": "Require a defect",
    })
    assert published["active"]["document"] == saved["draft"]["document"]
    restored = post("restore", {"version": published["version"], "revision": 1})
    assert restored["draft"]["document"]["custom_rules"] == []
    invoke.assert_not_called()


def test_guided_demo_edits_both_rule_types_and_protects_shared_policy(rule):
    store = SqliteRuleStore(":memory:")
    service = builder.DemoRuleService(store, lambda *_: True, Mock())
    def save(document, version):
        return service.handle("desktop", "draft", "POST", "local-user",
                              {"version": version, "document": document}, uuid.uuid4().hex)
    state = service.state("desktop", "local-user")
    document = deepcopy(state["active"]["document"])
    document["evidence_policy"] += "\nA new evidence requirement."
    with pytest.raises(RuleError, match="read-only"):
        save(document, state["version"])
    assert service.state("desktop", "local-user")["version"] == state["version"]
    seeded = save({**state["active"]["document"], "custom_rules": [rule]}, state["version"])
    document = deepcopy(seeded["draft"]["document"])
    document["custom_rules"][0]["conditions"][0]["value"] = "blocked"
    updated = save(document, seeded["version"])
    assert updated["draft"]["document"]["custom_rules"][0]["conditions"][0]["value"] == "blocked"
    invalid = deepcopy(updated["draft"]["document"])
    invalid["custom_rules"][0]["check"] = {"type": "python", "code": "print('unsupported')"}
    with pytest.raises(RuleError, match="supported Python check"):
        save(invalid, updated["version"])
    preview = service.handle("desktop", "builder/test", "POST", "local-user", {"rule": rule})
    assert preview["model_calls"] == 0
    evidence = {
        "id": "", "name": "Email evidence", "enabled": True, "applies_when": "Purchase tests",
        "case_keys": [], "run_keys": [], "requirement": "A confirmation email must be recorded.",
        "expected_evidence": "Confirmation email", "examples": "", "severity": "medium", "mode": "advisory",
    }
    document = deepcopy(updated["draft"]["document"])
    document["custom_rules"].append(evidence)
    seeded = save(document, updated["version"])
    document = deepcopy(seeded["draft"]["document"])
    document["custom_rules"][1]["requirement"] = "Changed free-text requirement."
    seeded = save(document, seeded["version"])
    document = deepcopy(seeded["draft"]["document"])
    document["custom_rules"][1].update(enabled=False, severity="high", mode="advisory")
    saved = save(document, seeded["version"])
    assert saved["draft"]["document"] == document
    document["custom_rules"].pop(0)
    removed = save(document, saved["version"])
    assert len(removed["draft"]["document"]["custom_rules"]) == 1


def test_account_setting_preview_uses_existing_check_and_severity(monkeypatch):
    invoke = Mock(side_effect=AssertionError("Account configuration must not call a model"))
    monkeypatch.setattr("argus.pipeline.auditor.invoke_model", invoke)
    document = team_rules.validate_document(team_rules.defaults("desktop"))
    document["policy"].update(enabled_rules=["R0", "R-RUNTIME", "R22"], disabled_rules=[])
    document["policy"]["rule_severities"].pop("R22", None)
    for mode, verdict, severity in [("disabled", "pass", None), ("advisory", "pass", "low"), ("enforced", "concerns", "medium")]:
        document["policy"]["r22_mode"] = mode
        missing = sample_audit(document, "DEMO-E201")
        assert missing["overall_verdict"] == verdict
        assert ([row["severity"] for row in missing["findings"]] or [None]) == [severity]
        assert sample_audit(document, "DEMO-E202")["findings"] == []
    document["policy"]["rule_severities"]["R22"] = "high"
    assert sample_audit(document, "DEMO-E201")["overall_verdict"] == "fail"
    invoke.assert_not_called()
