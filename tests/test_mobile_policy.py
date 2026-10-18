"""Mobile permits empty steps and does not require account recording."""
from copy import deepcopy

import pytest

from argus import config
from argus.domain import workflow_rules
from argus.pipeline import auditor


@pytest.fixture(autouse=True)
def native_policy():
    workflow_rules.configure(config.load(profile="mobile").policy)
    yield
    workflow_rules.reset_configuration()


def metadata(steps, status="Pass", comment=""):
    return {"test_results": [{
        "test_case_objective": "Play an audiobook",
        "test_case_precondition": "An audiobook is in the library",
        "status": status, "comment": comment, "steps": steps,
    }]}


@pytest.mark.parametrize("steps", [None, []])
def test_no_steps_or_account_require_no_findings(steps):
    assert workflow_rules.run_workflow_rules(metadata(steps)) == []


@pytest.mark.parametrize("blank", [None, "", " \n ", "<p>&nbsp;<br></p>", "<div>\u200b</div>"])
@pytest.mark.parametrize("status", [None, "", "Not Executed", "In Progress"])
def test_empty_placeholder_needs_no_status_or_account(blank, status):
    meta = metadata([{
        "index": 0, "description": blank, "expected_result": blank,
        "status": status, "status_id": 999,
    }])
    original = deepcopy(meta)
    assert workflow_rules.run_workflow_rules(meta) == []
    assert meta == original


@pytest.mark.parametrize("definition", [
    {"description": "Start playback"},
    {"expected_result": "Playback starts"},
    {"description": '<img src="instruction.png">'},
])
def test_written_steps_still_require_execution(definition):
    meta = metadata([
        {"index": 0, "status": None, "status_id": 999},
        {"index": 1, "status": "Not Executed", **definition},
    ])
    assert [f["rule"] for f in workflow_rules.run_workflow_rules(meta)] == ["R2b"]


def test_explicit_failure_on_placeholder_still_contradicts_pass():
    meta = metadata([{"index": 0, "status": "Fail", "trace_links": ["MOBILE-123"]}])
    assert [f["rule"] for f in workflow_rules.run_workflow_rules(meta)] == ["R2"]


@pytest.mark.parametrize("overall", ["Fail", "Blocked", "Passed With Issue"])
def test_placeholder_documentation_is_preserved_without_account(overall):
    meta = metadata([{
        "index": 0, "status": None, "comment": "Playback is unavailable.",
        "trace_links": ["MOBILE-123"],
    }], status=overall)
    assert workflow_rules.run_workflow_rules(meta) == []


@pytest.mark.parametrize(("profile", "expected_rules"), [
    ("web", ["R22"]),
    ("desktop", ["R22"]),
    ("tablet", ["R22"]),
    ("reader", []),
    ("tv", ["R22"]),
    ("portal", ["R22"]),
])
def test_account_recording_follows_team_policy(profile, expected_rules):
    meta = metadata([{"index": 0, "description": "Start playback", "status": "Pass"}])
    assert workflow_rules.run_workflow_rules(meta) == []
    workflow_rules.configure(config.load(profile=profile).policy)
    assert [f["rule"] for f in workflow_rules.run_workflow_rules(meta)] == expected_rules


def test_empty_step_exception_does_not_leak_to_web():
    meta = metadata([{"index": 0, "status": "Not Executed"}], comment="qa@example.com")
    assert workflow_rules.run_workflow_rules(meta) == []
    workflow_rules.configure(config.load(profile="web").policy)
    assert [f["rule"] for f in workflow_rules.run_workflow_rules(meta)] == ["R2b"]


@pytest.mark.parametrize("steps", [[], [{"index": 0, "description": "<p>&nbsp;</p>"}]])
def test_prompt_uses_stored_intent_without_steps(steps):
    block = auditor.build_steps_block(metadata(steps), variant_name="mobile_v2")
    assert "Objective: <tester_text>Play an audiobook</tester_text>" in block
    assert "Mobile does not require written steps or statuses on empty" in block
    assert "no written steps" in block


def test_prompt_marks_only_empty_rows_as_placeholders():
    block = auditor.build_steps_block(metadata([
        {"index": 0, "description": "<p><br></p>", "comment": "Playback is unavailable.",
         "trace_links": ["MOBILE-123"]},
        {"index": 1, "description": "Start playback", "expected_result": "Playback starts"},
    ]), variant_name="mobile_v2")
    assert block.count("Empty placeholder; no test step or status is required.") == 1
    assert "Playback is unavailable." in block and "MOBILE-123" in block
    assert "Step 2 (step_index=1)" in block
    assert "<tester_text>Playback starts</tester_text>" in block
