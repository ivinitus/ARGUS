"""TABLET accepts secondary projects and audits objectives without placeholder statuses."""
from copy import deepcopy
import re
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from argus import config
from argus.domain import workflow_rules
from argus.operations import manual_batch
from argus.operations.teams import resolve_jira_team, resolve_team, team_supports_jira_project
from argus.pipeline import auditor, extractor, orchestrator, test_cases, test_plans



@pytest.fixture(autouse=True)
def configured_project_routing(monkeypatch):
    """Exercise configurable multi-project routing without private profile defaults."""
    real_load = config.load
    def load(*args, **kwargs):
        settings = real_load(*args, **kwargs)
        if settings.general.profile == "tablet":
            settings.extractor.additional_project_ids = ["10001"]
            settings.extractor.execution_key_pattern = r"^(QA|MOBILE)-E[0-9]+$"
            settings.policy.defect_key_prefixes.append("MOBILE-")
        elif settings.general.profile == "mobile":
            settings.extractor.project_id = "10001"
        return settings
    monkeypatch.setattr(config, "load", load)

@pytest.fixture
def tablet():
    settings = config.load(profile="tablet")
    workflow_rules.configure(settings.policy)
    yield settings
    workflow_rules.reset_configuration()


def metadata(steps, status="Pass", comment="Account: qa@example.com"):
    return {"test_results": [{
        "test_case_objective": "Complete account setup",
        "test_case_precondition": "An account without a saved default payment method",
        "status": status,
        "comment": comment,
        "steps": steps,
    }]}


def test_tablet_accepts_both_projects_and_keeps_environment_checks_off(tablet):
    assert tablet.extractor.project_ids == ("10000", "10001")
    for key in ("QA-E123", "MOBILE-E123"):
        assert re.fullmatch(tablet.extractor.execution_key_pattern, key)
    assert not re.fullmatch(tablet.extractor.execution_key_pattern, "PORTAL-E123")
    assert not tablet.policy.environment_check_enabled
    assert not tablet.auditor.env_check_inline
    assert tablet.auditor.env_check_engine == "off"
    assert team_supports_jira_project(resolve_team("tablet"), 10001)


@pytest.mark.parametrize("project", ["10000", "10001"])
@pytest.mark.parametrize("folder", [
    "Tablet regression Release", "Tablet regression OOBE",
    "Tablet regression", "Tablet regression\t Release",
])
def test_tablet_folder_routing_in_both_projects(project, folder):
    assert resolve_jira_team(project, folder).slug == "tablet"


@pytest.mark.parametrize("project, expected", [("10000", None), ("10001", None)])
@pytest.mark.parametrize("folder", ["Foster regression", "TABLETSIL Release", "Regression TABLETSIL"])
def test_similar_names_do_not_route_to_tablet(project, expected, folder):
    team = resolve_jira_team(project, folder)
    assert (team.slug if team else None) == expected


@pytest.mark.parametrize("status", [None, "", "Not Executed", "In Progress"])
@pytest.mark.parametrize("blank", [None, "", " \n ", "<p>&nbsp;<br></p>", "<div>\u200b</div>"])
def test_tablet_empty_steps_need_no_status(tablet, status, blank):
    meta = metadata([{
        "index": 0, "description": blank, "expected_result": blank,
        "status": status, "status_id": 999,
    }])
    original = deepcopy(meta)
    assert workflow_rules.run_workflow_rules(meta) == []
    assert meta == original


@pytest.mark.parametrize("overall", ["Fail", "Blocked", "Passed With Issue"])
def test_tablet_objective_only_execution_does_not_require_a_failed_or_blocked_step(tablet, overall):
    meta = metadata(
        [{"index": 0, "description": "", "expected_result": "", "status": "Not Executed"}],
        status=overall, comment="Account qa@example.com: setup could not complete; ticket attached.",
    )
    assert workflow_rules.run_workflow_rules(meta) == []


def test_tablet_retains_comments_and_links_on_placeholder_rows(tablet):
    meta = metadata([{
        "index": 0, "status": None,
        "comment": "Account qa@example.com: setup failed; tracked by the linked defect.",
        "trace_links": ["QA-123"],
    }], status="Passed With Issue", comment="")
    assert workflow_rules.run_workflow_rules(meta) == []


@pytest.mark.parametrize("definition", [
    {"description": "Complete setup"},
    {"expected_result": "Setup completes"},
    {"description": '<p><img src="instruction.png"></p>'},
    {"description": '<iframe src="instruction.html"></iframe>'},
    {"description": '<svg><path d="M0 0L10 10"/></svg>'},
    {"expected_result": '<canvas width="200" height="100"></canvas>'},
    {"description": '<picture><source srcset="instruction.webp"></picture>'},
    {"expected_result": '<source src="expected.webm">'},
    {"expected_result": '<math><mspace width="1em"/></math>'},
])
def test_tablet_real_steps_still_require_execution(tablet, definition):
    meta = metadata([
        {"index": 0, "status": None, "status_id": 999, "description": "<p>&nbsp;</p>"},
        {"index": 1, "status": "Not Executed", **definition},
    ])
    assert [f["rule"] for f in workflow_rules.run_workflow_rules(meta)] == ["R2b"]


def test_tablet_explicit_failure_is_not_hidden_by_an_empty_step(tablet):
    findings = workflow_rules.run_workflow_rules(metadata([{
        "index": 0, "status": "Fail", "trace_links": ["QA-123"],
    }]))
    assert [finding["rule"] for finding in findings] == ["R2"]


def test_tablet_actual_unresolved_status_is_still_reported(tablet):
    meta = metadata([{
        "index": 0, "description": "Complete setup", "status": None, "status_id": 999,
    }])
    assert [f["rule"] for f in workflow_rules.run_workflow_rules(meta)] == ["R0"]
    meta = metadata([])
    meta["test_results"][0].update(status=None, status_id=999)
    assert [f["rule"] for f in workflow_rules.run_workflow_rules(meta)] == ["R0"]


def test_tablet_secondary_project_defect_comment_documents_a_failure(tablet):
    meta = metadata([{
        "index": 0, "description": "Complete setup", "status": "Fail",
        "comment": "Setup failed; see MOBILE-123",
    }], status="Fail")
    assert workflow_rules.check_fail_step_requires_defect_ref(meta) == []


def test_empty_step_exception_does_not_leak_to_other_teams(tablet):
    meta = metadata([{"index": 0, "status": "Not Executed"}])
    assert workflow_rules.run_workflow_rules(meta) == []
    workflow_rules.configure(config.load(profile="web").policy)
    assert [f["rule"] for f in workflow_rules.run_workflow_rules(meta)] == ["R2b"]


@pytest.mark.parametrize("steps", [
    [],
    [{"index": 0, "description": "<p>&nbsp;</p>", "status": "Not Executed"}],
])
def test_tablet_prompt_uses_objective_and_precondition_without_steps(steps):
    block = auditor.build_steps_block(metadata(steps), variant_name="tablet_v2")
    assert "Objective: <tester_text>Complete account setup</tester_text>" in block
    assert "Precondition: <tester_text>An account without a saved default payment method" in block
    assert "no written steps" in block
    assert "TABLET does not require written steps or statuses on empty" in block


def test_tablet_prompt_distinguishes_placeholders_from_written_steps():
    block = auditor.build_steps_block(metadata([
        {"index": 0, "description": "<p><br></p>"},
        {"index": 1, "description": "Complete setup", "expected_result": "Welcome appears"},
    ]), variant_name="tablet_v2")
    assert "Empty placeholder; no test step or status is required." in block
    assert "Step 2 (step_index=1)" in block
    assert "<tester_text>Welcome appears</tester_text>" in block


def test_tablet_folder_lookup_searches_secondary_project(tablet, monkeypatch):
    lookup = Mock(side_effect=[None, {"id": 42}])
    monkeypatch.setattr(extractor, "find_folder_by_name", lookup)
    assert extractor.find_folder_for_profile(object(), tablet.extractor, "[TABLET] Release") == (
        "10001", {"id": 42},
    )
    assert [call.args[3] for call in lookup.call_args_list] == ["10000", "10001"]


def test_tablet_duplicate_folder_names_require_a_project_specific_key(tablet, monkeypatch):
    monkeypatch.setattr(extractor, "find_folder_by_name", Mock(return_value={"id": 42}))
    with pytest.raises(ValueError, match="multiple configured Jira projects"):
        extractor.find_folder_for_profile(object(), tablet.extractor, "[TABLET] Release")


def test_tablet_manual_runs_select_each_runs_project_without_changing_profile(tablet, monkeypatch):
    monkeypatch.setattr(extractor, "build_session", lambda _: object())
    monkeypatch.setattr(extractor, "find_test_run_by_key", lambda session, base, key: {
        "key": key, "projectId": 10001 if key.startswith("MOBILE") else 10000,
        "folderId": 42,
    })
    monkeypatch.setattr(extractor, "find_folder_name_by_id", lambda *args: "Tablet regression Release")
    audit = Mock(return_value=0)
    monkeypatch.setattr(manual_batch.orchestrator, "_run_folder_list", audit)
    assert manual_batch.run_targets(tablet, run_keys=["MOBILE-C1", "QA-C2"]) == 0
    assert [call.args[1].extractor.project_ids for call in audit.call_args_list] == [
        ("10001",), ("10000",),
    ]
    assert [call.kwargs["selected_run_keys"] for call in audit.call_args_list] == [
        {"MOBILE-C1"}, {"QA-C2"},
    ]
    assert all(call.args[1].general.team_slug == "tablet" for call in audit.call_args_list)
    assert tablet.extractor.project_ids == ("10000", "10001")


def test_tablet_secondary_project_test_case_uses_secondary_project_statuses(tablet, monkeypatch):
    monkeypatch.setattr(test_cases, "find_case_by_key", lambda *args: {
        "id": 5, "key": "MOBILE-T1", "projectId": 10001,
    })
    statuses = Mock(return_value={"PASS": 22})
    monkeypatch.setattr(extractor, "get_project_status_ids", statuses)
    monkeypatch.setattr(test_cases, "get_case_results", lambda *args: [{
        "key": "MOBILE-E1", "testResultStatusId": 22, "testRun": {"key": "MOBILE-C1"},
    }])
    monkeypatch.setattr(extractor, "find_test_run_by_key", lambda *args: {
        "key": "MOBILE-C1", "projectId": 10001, "folderId": 42,
    })
    monkeypatch.setattr(extractor, "find_folder_name_by_id", lambda *args: "Tablet regression Release")
    assert test_cases.executed_keys(tablet, object(), "MOBILE-T1") == ["MOBILE-E1"]
    assert statuses.call_args.args[2] == "10001"


@pytest.mark.parametrize("use_folder", [False, True])
def test_tablet_secondary_project_plan_launch_accepts_keys_and_folders(tablet, monkeypatch, use_folder):
    plan = {"key": "MOBILE-P1", "id": 5, "name": "TABLET Release", "projectId": 10001}
    runs = [{"key": "MOBILE-C1", "id": 6, "projectId": 10001}]
    monkeypatch.setattr(extractor, "build_session", lambda _: object())
    monkeypatch.setattr(test_plans, "find_plan_by_key", lambda *args: plan)
    monkeypatch.setattr(
        extractor, "find_folder_by_name",
        lambda session, base, name, project, **kwargs: (
            {"id": 42} if project == "10001" else None
        ),
    )
    plans = Mock(return_value=[plan])
    monkeypatch.setattr(test_plans, "get_plans_in_folder", plans)
    monkeypatch.setattr(test_plans, "get_runs_in_plan", lambda *args: runs)
    audit = Mock(return_value=0)
    monkeypatch.setattr(orchestrator, "_run_test_plan", audit)
    targets = {"plan_folders": ["TABLET Release"]} if use_folder else {"plan_keys": ["MOBILE-P1"]}
    assert manual_batch.run_targets(tablet, **targets) == 0
    audit.assert_called_once_with(plan, runs, tablet)
    if use_folder:
        assert plans.call_args.args[2] == "10001"


def test_tablet_single_secondary_project_execution_resolves_folder_in_secondary_project(
    tablet, monkeypatch, tmp_path,
):
    tablet.extractor.out_dir = tmp_path / "output"
    session = Mock()
    session.get.return_value = Mock(
        status_code=200,
        json=Mock(return_value={"testRun": {"folderId": 42, "projectId": 10001}}),
    )
    monkeypatch.setattr(extractor, "build_session", lambda _: session)
    folder = Mock(return_value="[TABLET] Release")
    monkeypatch.setattr(extractor, "find_folder_name_by_id", folder)
    audit = Mock(return_value=0)
    monkeypatch.setattr(orchestrator, "_run_single_locked", audit)
    monkeypatch.setattr(orchestrator, "_announce_audit_phase", Mock())
    monkeypatch.setattr(orchestrator.events, "emit", Mock())
    args = SimpleNamespace(
        execution_key="MOBILE-E1", testrun_id=None, item_id=None,
        skip_extract=False, skip_audit=True,
    )
    assert orchestrator._run_single_prepared(args, tablet, run_id="tablet-test") == 0
    assert folder.call_args.args[2:] == ("10001", 42)
    assert audit.call_args.args[2] == tablet.extractor.out_dir / "TABLET_Release"
    assert tablet.extractor.project_id == "10000"
