from __future__ import annotations

from unittest import mock

import pytest
import requests

from argus import config as argus_config
from argus.operations import manual_batch


def test_manual_batch_groups_selected_runs_by_folder(monkeypatch):
    settings = argus_config.ArgusConfig()
    monkeypatch.setattr(manual_batch.config, "load", lambda: settings)
    monkeypatch.setattr(manual_batch.runtime, "configure", lambda unused: None)
    monkeypatch.setattr(
        manual_batch.extractor,
        "configure_logging",
        lambda unused: None,
    )
    monkeypatch.setattr(
        manual_batch.extractor,
        "build_session",
        lambda unused: object(),
    )
    monkeypatch.setattr(
        manual_batch.extractor,
        "find_test_run_by_key",
        lambda session, base_url, key: {
            "id": 1,
            "key": key,
            "projectId": settings.extractor.project_id,
            "folderId": 47,
        },
    )
    monkeypatch.setattr(
        manual_batch.extractor,
        "find_folder_name_by_id",
        lambda *unused: "Targeted regression - 03 sep 2026(WEB)",
    )
    run_folder = mock.MagicMock(return_value=0)
    run_single = mock.MagicMock(return_value=0)
    monkeypatch.setattr(
        manual_batch.orchestrator,
        "_run_folder_list",
        run_folder,
    )
    monkeypatch.setattr(
        manual_batch.orchestrator,
        "_run_single",
        run_single,
    )

    assert manual_batch.main([
        "--run",
        "QA-C1",
        "--run",
        "QA-C2",
        "--key",
        "QA-E3",
    ]) == 0

    run_folder.assert_called_once_with(
        "Targeted regression - 03 sep 2026(WEB)",
        settings,
        selected_run_keys={"QA-C1", "QA-C2"},
    )
    assert run_single.call_args.args[0].execution_key == "QA-E3"


def test_case_batch_audits_resolved_executions_once_without_folder_lookup(monkeypatch):
    settings = argus_config.load(profile="mobile")
    monkeypatch.setattr(manual_batch.config, "load", lambda: settings)
    monkeypatch.setattr(manual_batch.runtime, "configure", lambda _: None)
    monkeypatch.setattr(manual_batch.extractor, "build_session", lambda _: object())
    monkeypatch.setattr(
        manual_batch.test_cases, "executed_keys",
        lambda *args: ["MOBILE-E1924728", "MOBILE-E1924729"],
    )
    single = mock.Mock(return_value=0)
    folder = mock.Mock()
    monkeypatch.setattr(manual_batch.orchestrator, "_run_single", single)
    monkeypatch.setattr(manual_batch.orchestrator, "_run_folder_list", folder)
    assert manual_batch.main([
        "--case", "MOBILE-T40812", "--case", "MOBILE-T40813", "--key", "MOBILE-E1924728",
    ]) == 0
    assert [c.args[0].execution_key for c in single.call_args_list] == [
        "MOBILE-E1924728", "MOBILE-E1924729",
    ]
    folder.assert_not_called()


def test_old_launcher_folder_argument_routes_t_key_to_case_resolution(monkeypatch):
    settings = argus_config.load(profile="mobile")
    dispatch = mock.Mock(return_value=0)
    monkeypatch.setattr(manual_batch, "run_targets", dispatch)
    assert manual_batch.orchestrator._run_folder_list("mobile-t40812 ", settings) == 0
    dispatch.assert_called_once_with(settings, case_keys=["MOBILE-T40812"])


@pytest.fixture
def plan_batch(monkeypatch):
    settings = argus_config.ArgusConfig()
    settings.extractor.project_id = "10001"
    monkeypatch.setattr(manual_batch.config, "load", lambda: settings)
    monkeypatch.setattr(manual_batch.runtime, "configure", lambda _: None)
    monkeypatch.setattr(manual_batch.extractor, "build_session", lambda _: object())
    monkeypatch.setattr(
        manual_batch.test_plans, "find_plan_by_key",
        lambda session, base, key: {
            "id": int(key.split("-P")[1]), "key": key, "projectId": 10001,
            "name": f"Release Test Plan {key}",
        },
    )
    monkeypatch.setattr(
        manual_batch.extractor, "find_folder_name_by_id",
        lambda session, base, project, folder: f"Folder {folder}",
    )
    audit = mock.Mock(return_value=0)
    monkeypatch.setattr(manual_batch.orchestrator, "_run_test_plan", audit)
    monkeypatch.setattr(
        manual_batch.orchestrator, "_run_folder_list",
        lambda *args, **kwargs: pytest.fail("plans must not audit the original run folders"),
    )
    return settings, audit


def _run(key, folder=10):
    return {"id": int(key.split("-C")[1]), "key": key, "projectId": 10001, "folderId": folder}


def test_each_plan_keeps_its_own_runs_and_duplicate_plan_requests_run_once(plan_batch, monkeypatch):
    settings, audit = plan_batch
    monkeypatch.setattr(
        manual_batch.test_plans, "get_runs_in_plan",
        lambda session, base, plan_id: (
            [_run("MOBILE-C1"), _run("MOBILE-C2")]
            if plan_id == 1 else [_run("MOBILE-C2"), _run("MOBILE-C3", 20)]
        ),
    )
    assert manual_batch.main([
        "--plan", "MOBILE-P1", "--plan", "MOBILE-P2", "--plan", "MOBILE-P1",
    ]) == 0
    first, second = audit.call_args_list
    assert first.args[0]["key"] == "MOBILE-P1"
    assert first.args[0]["name"] == "Release Test Plan MOBILE-P1"
    assert first.args[1] == [_run("MOBILE-C1"), _run("MOBILE-C2")]
    assert second.args[0]["key"] == "MOBILE-P2"
    assert second.args[1] == [_run("MOBILE-C2"), _run("MOBILE-C3", 20)]
    assert first.args[2] is second.args[2] is settings


def test_plan_folder_can_expand_more_runs_than_manual_reference_limit(plan_batch, monkeypatch):
    settings, audit = plan_batch
    find_folder = mock.Mock(return_value={"id": 10099})
    monkeypatch.setattr(manual_batch.extractor, "find_folder_by_name", find_folder)
    monkeypatch.setattr(
        manual_batch.test_plans, "get_plans_in_folder",
        lambda *args: [{
            "id": 1, "key": "MOBILE-P1", "projectId": 10001, "name": "Release Test Plan MOBILE-P1",
        }],
    )
    runs = [_run(f"MOBILE-C{i}") for i in range(1, 26)]
    monkeypatch.setattr(manual_batch.test_plans, "get_runs_in_plan", lambda *args: runs)
    folder_name = mock.Mock(return_value="Folder 10")
    monkeypatch.setattr(manual_batch.extractor, "find_folder_name_by_id", folder_name)
    assert manual_batch.main(["--plan-folder", "Android plans"]) == 0
    assert find_folder.call_args.kwargs == {"folder_type": "testplan"}
    folder_name.assert_not_called()
    assert audit.call_count == 1
    assert audit.call_args.args[0]["key"] == "MOBILE-P1"
    assert audit.call_args.args[1] == runs
    assert audit.call_args.args[2] is settings


@pytest.mark.parametrize("runs", [
    [],
    [{**_run("MOBILE-C1"), "projectId": 10000}],
])
def test_empty_or_cross_project_plan_does_not_start_audit(plan_batch, monkeypatch, runs):
    settings, audit = plan_batch
    monkeypatch.setattr(manual_batch.test_plans, "get_runs_in_plan", lambda *args: runs)
    assert manual_batch.main(["--plan", "MOBILE-P1"]) == 1
    audit.assert_not_called()


def test_missing_plan_does_not_start_audit(plan_batch, monkeypatch):
    settings, audit = plan_batch
    monkeypatch.setattr(manual_batch.test_plans, "find_plan_by_key", lambda *args: None)
    assert manual_batch.main(["--plan", "MOBILE-P1"]) == 1
    audit.assert_not_called()


@pytest.mark.parametrize("operation", ["find_plan_by_key", "get_runs_in_plan"])
def test_plan_network_failure_reports_error_before_starting_audit(
    plan_batch, monkeypatch, capsys, operation,
):
    settings, audit = plan_batch
    monkeypatch.setattr(
        manual_batch.test_plans, operation,
        mock.Mock(side_effect=requests.ConnectionError("Jira unavailable")),
    )
    assert manual_batch.main(["--plan", "MOBILE-P1"]) == 1
    assert "[ARGUS] Jira unavailable" in capsys.readouterr().err
    audit.assert_not_called()


@pytest.mark.parametrize(("option", "value", "keyword"), [
    ("--test-plan", "MOBILE-P1", "plan_keys"),
    ("--test-plan-folder", "Android plans", "plan_folders"),
])
def test_audit_cli_dispatches_plan_targets(plan_batch, monkeypatch, option, value, keyword):
    settings, audit = plan_batch
    dispatch = mock.Mock(return_value=0)
    monkeypatch.setattr(manual_batch, "run_targets", dispatch)
    monkeypatch.setattr(manual_batch.config, "load", lambda *args: settings)
    assert manual_batch.orchestrator.main([option, value]) == 0
    assert dispatch.call_args.args == (settings,)
    assert dispatch.call_args.kwargs[keyword] == [value]
