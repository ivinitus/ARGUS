"""Report selection must preserve the exact saved inputs and team boundary."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

import pytest

from argus.config.models import ArgusConfig
from argus.operations.rule_service import RuleError, RuleService
from argus.operations.rule_store import SqliteRuleStore
from argus.reporting.rule_data import build_manifest, input_fingerprint, selection_page


def execution(root, title="Purchase", payload=b"email image"):
    source = root / "ARGUS-E1"
    source.mkdir(parents=True)
    (source / "audit.json").write_text(json.dumps({"overall_verdict": "pass", "findings": []}))
    (source / "metadata.json").write_text(json.dumps({"test_results": [{
        "test_case_name": title, "status": "Pass", "testrun_key": "ARGUS-C1",
        "testrun_name": "Checkout", "execution_date": "2026-10-07",
    }]}))
    (source / "image.png").write_bytes(payload)
    return source


def test_manifest_filters_saved_scope_and_does_not_depend_on_generated_audits(tmp_path):
    source = execution(tmp_path)
    manifest = build_manifest(tmp_path, "web", "report", "2026-10-07")
    assert manifest["items"][0]["title"] == "Purchase"
    original = input_fingerprint(source)
    (source / "audit.json").write_text('{"overall_verdict":"fail"}')
    (source / "audit.md").write_text("Rendered output")
    (source / "worker.lock").write_text("lease")
    assert input_fingerprint(source) == original
    (source / "image.png").write_bytes(b"changed evidence")
    assert input_fingerprint(source) != original
    outside = tmp_path / "outside.txt"
    outside.write_text("not execution data")
    (source / "outside.txt").symlink_to(outside)
    with pytest.raises(ValueError):
        input_fingerprint(source)




def test_catalog_pagination_and_unknown_version():
    manifest = {"folder": "report", "title": "Report", "timestamp": "now", "revision": "digest",
                "items": [{"key": f"ARGUS-E{i}", "title": "Purchase"} for i in range(130)]}
    first = selection_page(manifest, {})
    assert len(first["items"]) == 100 and first["next_cursor"] == 100
    second = selection_page(manifest, {"cursor": "100"})
    assert len(second["items"]) == 30 and second["next_cursor"] is None
    for cursor in ["-1", "invalid"]:
        with pytest.raises(RuleError):
            selection_page(manifest, {"cursor": cursor})
    api = RuleService(SqliteRuleStore(":memory:"), lambda *_: True, Mock())
    api.state("web", "editor")
    assert api.handle("web", "versions/1", "GET", "editor", {})["revision"] == 1
    with pytest.raises(RuleError, match="not found"):
        api.handle("web", "versions/2", "GET", "editor", {})
    api.can_edit = lambda *_: False
    with pytest.raises(RuleError) as denied:
        api.handle("web", "versions/1", "GET", "viewer", {})
    assert denied.value.status == 403


@pytest.mark.parametrize("mutation", ["none", "evidence", "missing", "stale_catalog"])
def test_selected_report_is_pinned_and_newer_duplicate_cannot_replace_it(tmp_path, monkeypatch, mutation):
    from argus import runtime
    from argus.domain.history_index import HistoryIndex
    from argus.operations import audit_lock, rule_preview

    output = tmp_path / "output"
    source = execution(output / "chosen-report", payload=b"chosen evidence")
    execution(output / "newer-report", payload=b"different newer evidence")
    manifest = build_manifest(source.parent, "web", "chosen-report", "2026-10-07")
    catalog = Mock()
    catalog.manifest.return_value = manifest
    store = SqliteRuleStore(":memory:")
    api = RuleService(store, lambda *_: True, Mock(), catalog)
    state = api.state("web", "editor")
    saved = api.handle("web", "draft", "POST", "editor", {
        "version": state["version"], "document": state["active"]["document"],
    }, uuid.uuid4().hex)
    selection = {"folder": "chosen-report", "revision": manifest["revision"]}
    body = {"version": saved["version"], "executions": ["ARGUS-E1"], "report": selection}
    if mutation == "stale_catalog":
        selection["revision"] = "old revision"
        with pytest.raises(RuleError, match="report changed"):
            api.handle("web", "preview", "POST", "editor", body, uuid.uuid4().hex)
        return
    tried = api.handle("web", "preview", "POST", "editor", body, uuid.uuid4().hex)
    identifier = tried["preview"]["id"]
    settings = ArgusConfig()
    settings.general.team_slug = "web"
    settings.general.state_dir = tmp_path / "state"
    settings.extractor.out_dir = output
    monkeypatch.setattr(runtime, "configure", lambda *_: None)
    monkeypatch.setattr(HistoryIndex, "get_shared", Mock(return_value=SimpleNamespace(snapshot=lambda: {})))
    audit_lock.reset()
    if mutation == "evidence":
        (source / "image.png").write_bytes(b"changed after selection")
    elif mutation == "missing":
        (source / "audit.json").unlink()

    def replay(command, **kwargs):
        work = Path(command[command.index("--evaluate") + 1])
        assert (work / "image.png").read_bytes() == b"chosen evidence"
        (work / "audit.json").write_text('{"overall_verdict":"pass","findings":[]}')
    runner = Mock(side_effect=replay)
    monkeypatch.setattr(rule_preview.subprocess, "run", runner)
    if mutation != "none":
        with pytest.raises(rule_preview.PreviewInputError):
            rule_preview.run_preview(store, settings, identifier)
        assert store.get("web", "preview#" + identifier)["phase"] == "failed"
        assert runner.call_count == 0
    else:
        rule_preview.run_preview(store, settings, identifier)
        result = store.get("web", "preview#" + identifier)
        assert result["phase"] == "complete" and runner.call_count == 2
        assert result["results"][0]["input_sha256"] == manifest["items"][0]["input_sha256"]
        assert (source / "image.png").read_bytes() == b"chosen evidence"


def test_catalog_default_metadata_matches_representative_engine_findings():
    from argus.domain import workflow_rules
    from argus.config.models import PolicySettings
    from argus.domain.rule_catalog import FIXED, metadata
    workflow_rules.configure(PolicySettings())
    try:
        assert metadata("R9-AMB")["parent"] == "R9"
        assert metadata("R14A")["default_severities"] == ["medium", "high"]
        assert FIXED["R0"] == "high"
        for documented, expected in [(False, "high"), (True, "medium")]:
            finding = workflow_rules.build_r21_finding({"test_results": [{
                "status": "In Progress", "comment": "Waiting for service" if documented else "",
            }]})
            assert finding["severity"] == expected
            assert expected in metadata("R21")["default_severities"]
    finally:
        workflow_rules.reset_configuration()
