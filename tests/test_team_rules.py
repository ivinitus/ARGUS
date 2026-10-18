"""Self-service rule publication and worker invariants; all dependencies are local."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

import pytest

from argus.config.models import ArgusConfig
from argus.domain import team_rules, workflow_rules
from argus.operations import rule_runtime
from argus.operations.rule_demo import finish_sample_preview
from argus.operations.rule_service import RuleError, RuleService, active_snapshot, revision_key
from argus.operations.rule_store import RuleConflict, SqliteRuleStore
from argus.operations.teams import TEAMS
from argus.pipeline import custom_rules


def custom(**changes):
    return {
        "id": "C-" + "a" * 32, "name": "Confirmation email", "enabled": True,
        "applies_when": "Purchase tests", "case_keys": [], "run_keys": [],
        "requirement": "A successful purchase has a confirmation email.",
        "expected_evidence": "An email showing the transaction.", "examples": "",
        "severity": "medium", "mode": "enforce", **changes,
    }


@pytest.fixture(autouse=True)
def reset_runtime():
    rule_runtime.reset()
    workflow_rules.reset_configuration()
    yield
    rule_runtime.reset()
    workflow_rules.reset_configuration()


@pytest.fixture
def service():
    store = SqliteRuleStore(":memory:")
    api = RuleService(store, lambda team, actor: actor == f"{team}-lead",
                      lambda team, identifier, actor: finish_sample_preview(store, team, identifier))
    return api


def change(service, action, body, key=None, *, team="desktop", actor=None):
    return service.handle(team, action, "POST", actor or f"{team}-lead", body, key or uuid.uuid4().hex)


def draft(service, document=None, *, team="desktop"):
    state = service.state(team, f"{team}-lead")
    if document is None:
        document = deepcopy(state["active"]["document"])
        document["custom_rules"] = [custom()]
    return change(service, "draft", {"version": state["version"], "document": document}, team=team)


def publish(service, *, team="desktop"):
    saved = draft(service, team=team)
    tried = change(service, "preview", {"version": saved["version"], "executions": ["DEMO-E101"]}, team=team)
    return change(service, "publish", {
        "version": tried["version"], "preview_id": tried["preview"]["id"], "note": "Require purchase email",
    }, team=team)


@pytest.mark.parametrize("team", TEAMS)
def test_packaged_policy_and_prompt_are_preserved(team):
    from argus.pipeline.auditor_prompts import resolve_prompt_variant
    document = team_rules.validate_document(team_rules.defaults(team.slug))
    variant = team_rules.defaults_variant(team.profile)
    prompt = resolve_prompt_variant(variant)
    assert team_rules.compile_prompt(prompt, document, variant) == prompt
    assert document["custom_rules"] == []


@pytest.mark.parametrize("alter", [
    lambda doc: doc["policy"]["disabled_rules"].append("R0"),
    lambda doc: doc["policy"]["rule_severities"].update(R0="low"),
    lambda doc: doc["policy"].update(completion_threshold=1.1),
    lambda doc: doc["policy"].update(min_assigned_for_completion=True),
    lambda doc: doc["policy"].update(r22_mode="unknown"),
    lambda doc: doc["policy"].update(unknown=True),
    lambda doc: doc["policy"].update(statuses={"unknown": "Pass"}),
    lambda doc: doc["policy"].update(environment_allowed_host_exceptions={"T1": ["https://bad/path"]}),
    lambda doc: doc["custom_rules"].append(custom(severity="critical")),
    lambda doc: doc["custom_rules"].extend([custom(), custom()]),
    lambda doc: doc.update(schema_version=True),
])
def test_invalid_policy_is_rejected(alter):
    document = team_rules.defaults("web")
    alter(document)
    with pytest.raises(team_rules.RuleValidationError):
        team_rules.validate_document(document)


def test_only_own_team_lead_can_write(service):
    state = service.state("desktop", "viewer")
    assert state["can_edit"] is False
    with pytest.raises(RuleError) as error:
        change(service, "draft", {"version": state["version"], "document": state["active"]["document"]}, actor="web-lead")
    assert error.value.status == 403
    assert service.state("web", "desktop-lead")["can_edit"] is False


def test_publication_needs_completed_exact_preview(service):
    saved = draft(service)
    with pytest.raises(RuleError, match="exact draft"):
        change(service, "publish", {"version": saved["version"], "preview_id": "a" * 32, "note": "Test"})
    tried = change(service, "preview", {"version": saved["version"], "executions": ["DEMO-E101"]})
    changed = change(service, "draft", {"version": tried["version"], "document": saved["draft"]["document"]})
    # Saving even the same content creates a new draft identity.
    with pytest.raises(RuleError, match="exact draft"):
        change(service, "publish", {"version": changed["version"], "preview_id": tried["preview"]["id"], "note": "Test"})


def test_disabled_test_account_policy_publishes_and_reaches_worker(service):
    document = team_rules.defaults("desktop")
    document["policy"]["r22_mode"] = "disabled"
    saved = draft(service, document)
    tried = change(service, "preview", {
        "version": saved["version"], "executions": ["DEMO-E101"],
    })
    change(service, "publish", {
        "version": tried["version"], "preview_id": tried["preview"]["id"],
        "note": "Disable the test-account check",
    })
    settings = ArgusConfig()
    settings.general.team_slug = "desktop"
    settings.auditor.rule_snapshot = active_snapshot(service.store, "desktop")
    rule_runtime.configure(settings)
    assert settings.policy.r22_mode == "disabled"
    workflow_rules.configure(settings.policy)
    assert workflow_rules.check_test_account_in_comments({
        "test_results": [{"status": "Pass", "comment": ""}],
    }) == []


def test_idempotent_save_and_publish_and_conflicts(service):
    state = service.state("desktop", "desktop-lead")
    body = {"version": state["version"], "document": state["active"]["document"]}
    identifier = uuid.uuid4().hex
    saved = change(service, "draft", body, identifier)
    assert change(service, "draft", body, identifier) == saved
    with pytest.raises(RuleError) as error:
        change(service, "draft", body)
    assert error.value.status == 409
    other = deepcopy(body)
    other["document"]["policy"]["r22_mode"] = "enforced"
    with pytest.raises(RuleError, match="different input"):
        change(service, "draft", other, identifier)
    tried = change(service, "preview", {"version": saved["version"], "executions": ["DEMO-E101"]})
    body = {"version": tried["version"], "preview_id": tried["preview"]["id"], "note": "Publish"}
    key = uuid.uuid4().hex
    result = change(service, "publish", body, key)
    assert change(service, "publish", body, key) == result
    assert len(service.store.query("desktop", "version#")) == 2


def test_restore_creates_draft_without_changing_active_or_history(service):
    result = publish(service)
    original = service.store.get("desktop", revision_key(1))
    saved = change(service, "restore", {"version": result["version"], "revision": 1})
    assert saved["draft"]["document"] == original["document"]
    assert active_snapshot(service.store, "desktop")["revision"] == 2
    assert service.store.get("desktop", revision_key(1)) == original






def test_snapshot_integrity_and_immutable_store(service):
    service.state("desktop", "desktop-lead")
    with pytest.raises(RuleConflict):
        service.store.put("desktop", revision_key(1), {})
    data = active_snapshot(service.store, "desktop")
    data["document"]["policy"]["r22_mode"] = "enforced"
    settings = ArgusConfig()
    settings.auditor.rule_snapshot = data
    with pytest.raises(ValueError, match="fingerprint"):
        rule_runtime.configure(settings)




@pytest.mark.parametrize("mode,severity,expected", [
    ("enforce", "high", "fail"), ("enforce", "medium", "concerns"),
    ("enforce", "low", "pass"), ("advisory", "high", "pass"),
])
def test_custom_rule_severity_and_advisory_survive_reconciliation(monkeypatch, tmp_path, mode, severity, expected):
    from argus.domain.finding_inference import normalise_findings_for_schema, reconcile_verdict
    document = team_rules.validate_document(team_rules.defaults("web"))
    document["custom_rules"] = [custom(mode=mode, severity=severity)]
    cfg = SimpleNamespace(rule_snapshot={"revision": 2, "document": document, "sha256": team_rules.fingerprint(document)},
                          execution_dir=tmp_path)
    monkeypatch.setattr(custom_rules, "assess", lambda *args: [{
        "id": custom()["id"], "status": "violated", "kind": "missing_evidence",
        "confidence": "high", "evidence_refs": [], "description": "The purchase has no confirmation email.",
    }])
    audit = custom_rules.apply(cfg, {"test_results": [{}]}, {"overall_verdict": "pass", "findings": []}, [], [])
    assert audit["overall_verdict"] == expected
    audit["findings"] = normalise_findings_for_schema(audit["findings"])
    assert reconcile_verdict(audit)["overall_verdict"] == expected
    assert audit["findings"][0]["severity"] == severity
    assert audit["provenance"]["policy_revision"] == 2


def test_custom_rule_invalid_evidence_or_model_control_fields():
    rule = custom()
    row = {"id": rule["id"], "status": "violated", "kind": "contradiction", "confidence": "high",
           "evidence_refs": ["made-up"], "description": "Wrong amount", "severity": "low", "advisory": True}
    with pytest.raises(ValueError, match="reference"):
        custom_rules.validate({"assessments": [row]}, [rule], {"metadata"})
    row["evidence_refs"] = ["metadata"]
    clean = custom_rules.validate({"assessments": [row]}, [rule], {"metadata"})[0]
    assert "severity" not in clean and "advisory" not in clean


def test_failed_custom_evaluation_is_incomplete_when_enforcing(monkeypatch, tmp_path):
    document = team_rules.validate_document(team_rules.defaults("web"))
    document["custom_rules"] = [custom()]
    cfg = SimpleNamespace(rule_snapshot={"revision": 2, "document": document, "sha256": team_rules.fingerprint(document)},
                          execution_dir=tmp_path)
    monkeypatch.setattr(custom_rules, "assess", Mock(side_effect=ValueError("bad output")))
    audit = custom_rules.apply(cfg, {}, {"overall_verdict": "pass", "findings": []}, [], [])
    assert audit["analysis_status"] == "incomplete"
    assert audit["findings"] == []


@pytest.mark.parametrize("fails", [False, True])
def test_advisory_uncertainty_and_failure_preserve_verdict(monkeypatch, tmp_path, fails):
    from argus.domain.finding_inference import normalise_findings_for_schema, reconcile_verdict
    from argus.operations.rule_preview import summarize
    document = team_rules.validate_document(team_rules.defaults("web"))
    document["custom_rules"] = [custom(mode="advisory", severity="high")]
    cfg = SimpleNamespace(rule_snapshot={"revision": 2, "document": document,
                                        "sha256": team_rules.fingerprint(document)},
                          execution_dir=tmp_path)
    response = [{"id": custom()["id"], "status": "uncertain", "kind": "missing_evidence",
                 "confidence": "low", "evidence_refs": [], "description": "Email could not be confirmed."}]
    monkeypatch.setattr(custom_rules, "assess", Mock(
        return_value=response, side_effect=ValueError("bad output") if fails else None,
    ))
    audit = custom_rules.apply(cfg, {}, {"overall_verdict": "pass", "findings": []}, [], [])
    audit["findings"] = normalise_findings_for_schema(audit["findings"])
    assert reconcile_verdict(audit)["overall_verdict"] == "pass"
    assert audit["findings"][0]["advisory"] is True
    assert audit.get("analysis_status") != "incomplete"
    if fails:
        assert "could not" in audit["findings"][0]["description"]
        assert summarize(audit)["analysis_status"] == "incomplete"


@pytest.mark.parametrize("outcome", ["complete", "incomplete", "crash"])
def test_production_preview_isolated_inputs_and_failed_publication(monkeypatch, tmp_path, outcome):
    import json
    from argus import runtime
    from argus.domain.history_index import HistoryIndex
    from argus.operations import audit_lock, rule_preview

    store = SqliteRuleStore(":memory:")
    service = RuleService(store, lambda team, actor: True, Mock())
    saved = draft(service)
    tried = change(service, "preview", {"version": saved["version"], "executions": ["ARGUS-E1"]})
    identifier = tried["preview"]["id"]
    settings = ArgusConfig()
    settings.general.team_slug = "desktop"
    settings.general.state_dir = tmp_path / "state"
    settings.extractor.out_dir = tmp_path / "outputs"
    source = settings.extractor.out_dir / "folder" / "ARGUS-E1"
    source.mkdir(parents=True)
    original = {"overall_verdict": "pass", "findings": []}
    (source / "audit.json").write_text(json.dumps(original))
    (source / "metadata.json").write_text('{"test_results": [{"status": "Pass"}]}')
    (source / "evidence.png").write_bytes(b"frozen evidence")
    (source / "audit.md").write_text("Canonical report")
    monkeypatch.setattr(runtime, "configure", lambda config: None)
    monkeypatch.setattr(HistoryIndex, "get_shared", Mock(return_value=SimpleNamespace(snapshot=lambda: {})))
    audit_lock.reset()
    calls = []

    def replay(command, **options):
        work = rule_preview.Path(command[command.index("--evaluate") + 1])
        snapshot_path = rule_preview.Path(command[command.index("--snapshot") + 1])
        snapshot = json.loads(snapshot_path.read_text())
        calls.append(snapshot["revision"])
        assert work != source
        assert (work / "evidence.png").read_bytes() == b"frozen evidence"
        assert not (work / "audit.md").exists()
        assert options["check"] is True
        if outcome == "crash":
            raise RuntimeError("worker failed")
        (work / "audit.json").write_text(json.dumps({
            **original, "analysis_status": outcome,
            "overall_verdict": "concerns" if snapshot["revision"] == "draft" else "pass",
        }))

    monkeypatch.setattr(rule_preview.subprocess, "run", replay)
    if outcome == "crash":
        with pytest.raises(RuntimeError, match="worker failed"):
            rule_preview.run_preview(store, settings, identifier)
    else:
        rule_preview.run_preview(store, settings, identifier)
        assert calls == [1, "draft"]
    preview = store.get("desktop", "preview#" + identifier)
    assert preview["phase"] == ("complete" if outcome == "complete" else "failed")
    assert json.loads((source / "audit.json").read_text()) == original
    assert (source / "audit.md").read_text() == "Canonical report"
    assert (source / "evidence.png").read_bytes() == b"frozen evidence"
    if outcome == "complete":
        assert len(preview["results"][0]["input_sha256"]) == 64
        published = change(service, "publish", {
            "version": tried["version"], "preview_id": identifier, "note": "Verified preview",
        })
        assert published["active"]["revision"] == 2
    else:
        with pytest.raises(RuleError, match="exact draft"):
            change(service, "publish", {
                "version": tried["version"], "preview_id": identifier, "note": "Must be rejected",
            })
    # Duplicate delivery cannot rerun or change a terminal result.
    rule_preview.run_preview(store, settings, identifier)
    assert store.get("desktop", "preview#" + identifier) == preview


def test_draft_coverage_uses_complete_roster(tmp_path):
    import json
    from argus.domain.folder_coverage import CoverageItem, CoverageSnapshot, to_coverage_json
    from argus.operations.rule_preview import compare_coverage
    before = team_rules.validate_document(team_rules.defaults("web"))
    after = deepcopy(before)
    after["policy"]["completion_threshold"] = 0.5
    after["policy"]["min_assigned_for_completion"] = 1
    rows = [CoverageItem(f"T{i}", "RUN", "US", "Pass" if i < 3 else "Not Executed", assigned_to="tester") for i in range(5)]
    (tmp_path / "_coverage.json").write_text(json.dumps(to_coverage_json(CoverageSnapshot("folder", rows), [])))
    result = compare_coverage([tmp_path], {"before": {"document": before}, "after": {"document": after}}, {})
    assert any(f["rule"] == "R15c" for f in result[0]["before"])
    assert not any(f["rule"] == "R15c" for f in result[0]["after"])
