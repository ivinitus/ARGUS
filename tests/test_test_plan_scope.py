"""A plan's title names its output, and only its linked runs enter that output."""
from types import SimpleNamespace
from unittest.mock import Mock
import json

import pytest

from argus.config import ArgusConfig
from argus.pipeline import batch, orchestrator


TITLE = "Android 26.36.00 Release Test Plan"
PLAN = {"id": 514, "key": "MOBILE-P367", "name": TITLE, "projectId": 10001}
RUNS = [
    {"id": 39137, "key": "MOBILE-C11105", "projectId": 10001, "folderId": 10105},
    {"id": 39119, "key": "MOBILE-C11087", "projectId": 10001, "folderId": 20000},
]


@pytest.fixture
def plan_audit(tmp_path, monkeypatch):
    settings = ArgusConfig()
    settings.extractor.project_id = "10001"
    settings.extractor.out_dir = tmp_path / "output_mobile"
    settings.general.state_dir = tmp_path / "state"
    settings.extractor.execution_key_pattern = r"^MOBILE-E[0-9]+$"
    root = settings.extractor.out_dir / "Android_26.36.00_Release_Test_Plan"
    monkeypatch.setattr(orchestrator.extractor, "build_session", lambda _: object())
    for name in ("find_folder_by_name", "find_folder_name_by_id", "get_test_runs_in_folder"):
        monkeypatch.setattr(
            orchestrator.extractor, name,
            Mock(side_effect=AssertionError("plan audits must not resolve or list run folders")),
        )
    monkeypatch.setattr(
        orchestrator.extractor, "get_project_status_ids",
        lambda *args: {"PASS": 39, "FAIL": 40},
    )
    monkeypatch.setattr("argus.operations.control.is_paused", lambda: False)
    for name in ("_announce_audit_phase", "_finish_audit_announcement",
                 "_print_by_tester", "_record_run_cost"):
        monkeypatch.setattr(orchestrator, name, Mock())
    for name in ("post_audit_started", "post_audit_done"):
        monkeypatch.setattr(orchestrator.notify, name, Mock())
    for name in ("mark_audit_running", "mark_audit_done"):
        monkeypatch.setattr(f"argus.operations.control.{name}", Mock())
    for name in ("start_audit_run", "mark_audit_publishing", "checkpoint_audit_key"):
        monkeypatch.setattr(f"argus.reporting.publisher.{name}", Mock())
    monkeypatch.setattr(
        "argus.core.users.get_default_cache", lambda: SimpleNamespace(save=lambda: None),
    )
    enumerate_items = Mock(return_value=orchestrator._FolderEnumeration(
        current_keys=["MOBILE-E1", "MOBILE-E2"],
        emitted_keys=["MOBILE-E1", "MOBILE-E2"],
        emitted_user_keys=[None, None],
        total_executed=2,
    ))
    monkeypatch.setattr(orchestrator, "_enumerate_folder_items", enumerate_items)
    coverage = Mock()
    publish = Mock(return_value=True)
    monkeypatch.setattr(orchestrator, "_write_folder_coverage", coverage)
    monkeypatch.setattr(orchestrator, "_publish_folder_reports", publish)

    def audit_keys(keys, config, **kwargs):
        results = []
        for key in keys:
            path = kwargs["out_dir"] / "linked-run" / key / "audit.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "execution_key": key, "overall_verdict": "pass",
                "findings": [], "analysis_status": "complete",
            }))
            (path.parent / "metadata.json").write_text("{}")
            results.append(batch.KeyResult(key=key, status="ok", audit_path=path))
        return results

    audit = Mock(side_effect=audit_keys)
    monkeypatch.setattr(orchestrator.batch, "run_batch", audit)
    return settings, root, enumerate_items, coverage, publish, audit


def test_plan_title_isolates_batch_coverage_and_reports_and_reuses_same_folder(plan_audit):
    settings, root, enumerate_items, coverage, publish, audit = plan_audit
    original_folder = settings.extractor.out_dir / "Original_Jira_Run_Folder"
    original_folder.mkdir(parents=True)
    untouched = original_folder / "_argus.html"
    untouched.write_text("Existing unrelated folder report")

    assert orchestrator._run_test_plan(PLAN, [*RUNS, RUNS[0]], settings) == 0
    assert enumerate_items.call_args.args[2] == RUNS
    assert enumerate_items.call_args.kwargs["selected_run_keys"] == {
        "MOBILE-C11105", "MOBILE-C11087",
    }
    audit.assert_called_once()
    assert audit.call_args.args[0] == ["MOBILE-E1", "MOBILE-E2"]
    assert audit.call_args.kwargs["out_dir"] == root
    assert coverage.call_args.args[2] == TITLE
    assert coverage.call_args.args[3] == RUNS
    assert coverage.call_args.args[6] == root
    publish.assert_called_once_with(root, settings)
    meta = json.loads((root / "_folder_meta.json").read_text())
    assert meta["folder_name"] == TITLE
    assert meta["test_plan_key"] == "MOBILE-P367"
    assert meta["test_plan_id"] == 514
    assert set(meta["test_run_keys"]) == {"MOBILE-C11105", "MOBILE-C11087"}
    manifest = json.loads((root / "_latest_run.json").read_text())
    assert manifest["folder_name"] == TITLE
    assert manifest["scope_keys"] == ["MOBILE-E1", "MOBILE-E2"]
    assert manifest["audit_keys"] == ["MOBILE-E1", "MOBILE-E2"]

    # The next request uses the same plan cache and output, even with fewer linked runs.
    enumerate_items.return_value = orchestrator._FolderEnumeration(
        current_keys=["MOBILE-E1"], total_skipped_dup=1,
    )
    assert orchestrator._run_test_plan(PLAN, RUNS[:1], settings) == 0
    assert set(enumerate_items.call_args.args[5]) == {"MOBILE-E1", "MOBILE-E2"}
    audit.assert_called_once()
    manifest = json.loads((root / "_latest_run.json").read_text())
    assert manifest["scope_keys"] == ["MOBILE-E1"]
    assert manifest["audit_keys"] == ["MOBILE-E1"]
    assert {
        path for path in settings.extractor.out_dir.iterdir()
        if not path.name.startswith("_")
    } == {original_folder, root}
    assert untouched.read_text() == "Existing unrelated folder report"


@pytest.mark.parametrize("existing_meta", [
    {"folder_name": TITLE, "test_plan_key": "MOBILE-P999"},
    {"folder_name": TITLE},
])
def test_plan_cannot_mix_with_an_existing_different_scope(plan_audit, existing_meta):
    settings, root, enumerate_items, coverage, publish, audit = plan_audit
    root.mkdir(parents=True)
    marker = root / "_folder_meta.json"
    before = json.dumps(existing_meta)
    marker.write_text(before)
    assert orchestrator._run_test_plan(PLAN, RUNS, settings) == 1
    assert marker.read_text() == before
    enumerate_items.assert_not_called()
    audit.assert_not_called()
    publish.assert_not_called()


def test_regular_folder_audit_cannot_write_inside_plan_output(plan_audit, monkeypatch):
    settings, root, enumerate_items, coverage, publish, audit = plan_audit
    root.mkdir(parents=True)
    (root / "_folder_meta.json").write_text(json.dumps({
        "folder_name": TITLE, "test_plan_key": "MOBILE-P367",
    }))
    monkeypatch.setattr(
        orchestrator.extractor, "find_folder_by_name", lambda *args: {"id": 10105},
    )
    assert orchestrator._run_folder_list(TITLE, settings) == 1
    enumerate_items.assert_not_called()
    audit.assert_not_called()


@pytest.mark.parametrize("contents", ["{", "[]"])
@pytest.mark.parametrize("request_plan", [False, True])
def test_unreadable_ownership_cannot_be_reset_by_a_different_scope(
    plan_audit, monkeypatch, contents, request_plan,
):
    settings, root, enumerate_items, coverage, publish, audit = plan_audit
    root.mkdir(parents=True)
    marker = root / "_folder_meta.json"
    marker.write_text(contents)
    monkeypatch.setattr(
        orchestrator.extractor, "find_folder_by_name", lambda *args: {"id": 10105},
    )
    result = (
        orchestrator._run_test_plan(PLAN, RUNS, settings)
        if request_plan else orchestrator._run_folder_list(TITLE, settings)
    )
    assert result == 1
    assert marker.read_text() == contents
    enumerate_items.assert_not_called()
    audit.assert_not_called()
    publish.assert_not_called()


@pytest.mark.parametrize("plan", [
    {**PLAN, "name": ""},
    {**PLAN, "projectId": 10000},
])
def test_plan_without_valid_title_or_project_creates_no_output(plan_audit, plan):
    settings, root, enumerate_items, coverage, publish, audit = plan_audit
    assert orchestrator._run_test_plan(plan, RUNS, settings) == 1
    assert not settings.extractor.out_dir.exists()


def test_tablet_houstonqa_plan_uses_its_own_project_statuses(plan_audit, monkeypatch):
    settings, root, enumerate_items, coverage, publish, audit = plan_audit
    settings.extractor.project_id = "10000"
    settings.extractor.additional_project_ids = ["10001"]
    statuses = Mock(return_value={"PASS": 22, "FAIL": 23})
    monkeypatch.setattr(orchestrator.extractor, "get_project_status_ids", statuses)
    assert orchestrator._run_test_plan(PLAN, RUNS, settings) == 0
    assert statuses.call_args.args[2] == "10001"
    assert enumerate_items.call_args.args[3] == {22, 23}
    assert settings.extractor.project_id == "10000"


def test_tablet_houstonqa_folder_uses_its_own_runs_and_statuses(plan_audit, monkeypatch):
    settings, root, enumerate_items, coverage, publish, audit = plan_audit
    settings.extractor.project_id = "10000"
    settings.extractor.additional_project_ids = ["10001"]
    monkeypatch.setattr(
        orchestrator.extractor, "find_folder_by_name",
        lambda session, base, name, project: {"id": 10105} if project == "10001" else None,
    )
    runs = Mock(return_value=RUNS[:1])
    statuses = Mock(return_value={"PASS": 22, "FAIL": 23})
    monkeypatch.setattr(orchestrator.extractor, "get_test_runs_in_folder", runs)
    monkeypatch.setattr(orchestrator.extractor, "get_project_status_ids", statuses)
    assert orchestrator._run_folder_list(TITLE, settings) == 0
    assert runs.call_args.args[3] == "10001"
    assert statuses.call_args.args[2] == "10001"
    assert enumerate_items.call_args.args[2] == RUNS[:1]
