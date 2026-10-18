from unittest.mock import Mock

import pytest

from argus import config
from argus.pipeline import test_cases


def session_with(*payloads):
    session = Mock()
    session.get.side_effect = [
        Mock(status_code=200, json=Mock(return_value=value)) for value in payloads
    ]
    return session


def result(key, status=22, case_id=107672):
    return {
        "key": key, "testCase": {"id": case_id},
        "testRun": {"key": "MOBILE-C11087"}, "testResultStatusId": status,
    }


def test_case_lookup_validates_key_and_exact_response():
    session = session_with({"id": 107672, "key": "MOBILE-T40812"})
    assert test_cases.find_case_by_key(session, "https://jira", "mobile-t40812")["id"] == 107672
    assert session.get.call_args.args[0].endswith("/testcase/MOBILE-T40812")
    with pytest.raises(ValueError, match="invalid"):
        test_cases.find_case_by_key(session, "https://jira", "../MOBILE-T40812")
    assert session.get.call_count == 1
    with pytest.raises(ValueError, match="invalid"):
        test_cases.find_case_by_key(
            session_with({"id": 1, "key": "MOBILE-T1"}), "https://jira", "MOBILE-T40812",
        )


def test_case_history_follows_actual_data_envelope_and_server_page_sizes():
    session = session_with(
        {"data": [result("MOBILE-E1")], "totalCount": 3},
        {"data": [result("MOBILE-E2"), result("MOBILE-E3")], "totalCount": 3},
    )
    assert [r["key"] for r in test_cases.get_case_results(session, "https://jira", 107672)] == [
        "MOBILE-E1", "MOBILE-E2", "MOBILE-E3",
    ]
    assert [c.kwargs["params"]["startAt"] for c in session.get.call_args_list] == [0, 1]


def test_incomplete_history_and_cross_case_results_fail_before_auditing():
    repeated = {"data": [result("MOBILE-E1")], "totalCount": 2}
    with pytest.raises(ValueError, match="pagination"):
        test_cases.get_case_results(session_with(repeated, repeated), "https://jira", 107672)
    with pytest.raises(ValueError, match="unexpected test case"):
        test_cases.get_case_results(
            session_with({"data": [result("MOBILE-E1", case_id=10)], "totalCount": 1}),
            "https://jira", 107672,
        )


@pytest.fixture
def case_resolution(monkeypatch):
    settings = config.load(profile="mobile")
    settings.extractor.project_id = "10001"
    from argus.operations import teams
    monkeypatch.setattr(teams, "resolve_jira_team", lambda *a: teams.resolve_team("mobile"))
    monkeypatch.setattr(test_cases, "find_case_by_key", lambda *a: {
        "id": 107672, "key": "MOBILE-T40812", "projectId": 10001,
    })
    monkeypatch.setattr(test_cases.extractor, "get_project_status_ids", lambda *a: {
        "PASS": 22, "FAIL": 23, "RETEST": 731, "NOT_EXECUTED": 20,
    })
    monkeypatch.setattr(test_cases.extractor, "find_test_run_by_key", lambda *a: {
        "id": 39119, "key": "MOBILE-C11087", "projectId": 10001, "folderId": 12,
    })
    monkeypatch.setattr(test_cases.extractor, "find_folder_name_by_id", lambda *a: "Android")
    return settings


def test_case_selects_completed_executions_and_deduplicates(case_resolution, monkeypatch):
    monkeypatch.setattr(test_cases, "get_case_results", lambda *a: [
        result("MOBILE-E1877024", 731), result("MOBILE-E1924728"),
        result("MOBILE-E1924728"), result("MOBILE-E1924729", 20),
    ])
    assert test_cases.executed_keys(case_resolution, object(), "MOBILE-T40812") == ["MOBILE-E1924728"]


def test_case_with_missing_run_folder_fails_before_folder_lookup(case_resolution, monkeypatch):
    monkeypatch.setattr(test_cases, "get_case_results", lambda *args: [result("MOBILE-E1")])
    monkeypatch.setattr(test_cases.extractor, "find_test_run_by_key", lambda *args: {
        "key": "MOBILE-C11087", "projectId": 10001,
    })
    folder = Mock()
    monkeypatch.setattr(test_cases.extractor, "find_folder_name_by_id", folder)
    with pytest.raises(ValueError, match="Could not resolve the folder"):
        test_cases.executed_keys(case_resolution, object(), "MOBILE-T40812")
    folder.assert_not_called()


def test_case_with_no_completed_executions_reports_case_error(case_resolution, monkeypatch):
    monkeypatch.setattr(test_cases, "get_case_results", lambda *a: [result("MOBILE-E1", 731)])
    with pytest.raises(ValueError, match="no completed executions"):
        test_cases.executed_keys(case_resolution, object(), "MOBILE-T40812")


def test_case_project_mismatch_does_not_read_execution_history(case_resolution, monkeypatch):
    case_resolution.extractor.project_id = "10000"
    history = Mock()
    monkeypatch.setattr(test_cases, "get_case_results", history)
    with pytest.raises(ValueError, match="selected profile project"):
        test_cases.executed_keys(case_resolution, object(), "MOBILE-T40812")
    history.assert_not_called()


def test_case_executions_from_other_team_are_not_audited(case_resolution, monkeypatch):
    monkeypatch.setattr(test_cases, "get_case_results", lambda *a: [result("MOBILE-E1")])
    case_resolution.general.team_slug = "web"
    with pytest.raises(ValueError, match="no completed executions"):
        test_cases.executed_keys(case_resolution, object(), "MOBILE-T40812")
