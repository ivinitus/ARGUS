from unittest.mock import Mock

import pytest

from argus.pipeline import extractor, test_plans


def session_with(*payloads):
    session = Mock()
    session.get.side_effect = [
        Mock(status_code=200, json=Mock(return_value=payload)) for payload in payloads
    ]
    return session


def test_plan_search_checks_exact_key_and_validates_before_request():
    session = session_with({"total": 2, "results": [
        {"id": 4, "key": "MOBILE-P3"}, {"id": 5, "key": "MOBILE-P30"},
    ]})
    assert test_plans.find_plan_by_key(session, "https://jira", "mobile-p3")["id"] == 4
    assert session.get.call_args.kwargs["params"]["query"] == 'testPlan.key IN ("MOBILE-P3")'
    with pytest.raises(ValueError, match="invalid"):
        test_plans.find_plan_by_key(session, "https://jira", 'MOBILE-P3") OR 1=1')
    assert session.get.call_count == 1


def test_plan_folder_search_follows_server_page_sizes():
    session = session_with(
        {"total": 3, "results": [{"id": 1, "key": "MOBILE-P1"}]},
        {"total": 3, "results": [{"id": 2, "key": "MOBILE-P2"}, {"id": 3, "key": "MOBILE-P3"}]},
    )
    assert len(test_plans.get_plans_in_folder(session, "https://jira", "10001", 10099)) == 3
    calls = session.get.call_args_list
    assert [call.kwargs["params"]["startAt"] for call in calls] == [0, 1]
    assert "testPlan.folderTreeId IN (10099)" in calls[0].kwargs["params"]["query"]


def test_linked_runs_use_trace_links_and_deduplicate_runs():
    run = {"id": 73, "key": "MOBILE-C19", "folderId": 10239, "projectId": 10001}
    session = session_with([
        {"id": 390, "testRun": run}, {"id": 391, "testRun": run},
        {"id": 392, "testRun": {**run, "key": "MOBILE-C20", "archived": True}},
    ])
    assert test_plans.get_runs_in_plan(session, "https://jira", 4) == [run]
    call = session.get.call_args
    assert call.args[0] == "https://jira/rest/tests/1.0/testplan/4/tracelinks/testrun"
    assert "testRun(" in call.kwargs["params"]["fields"]


def test_linked_runs_accept_unpaginated_response_larger_than_page_size():
    links = [{"id": i, "testRun": {"id": i, "key": f"MOBILE-C{i}"}} for i in range(150)]
    session = session_with(links, links)
    assert len(test_plans.get_runs_in_plan(session, "https://jira", 4)) == 150
    assert session.get.call_count == 2


@pytest.mark.parametrize("payload", [{}, {"results": None}, [{"id": 1}]])
def test_malformed_links_fail_instead_of_producing_empty_audit(payload):
    with pytest.raises(ValueError):
        test_plans.get_runs_in_plan(session_with(payload), "https://jira", 4)


def test_incomplete_pagination_fails():
    session = session_with(
        {"total": 10, "results": [{"id": 1}]},
        {"total": 10, "results": [{"id": 1}]},
    )
    with pytest.raises(ValueError, match="pagination"):
        test_plans.get_plans_in_folder(session, "https://jira", "10001", 1)


def test_plan_folder_tree_uses_requested_endpoint():
    tree = {"children": [{"id": 7, "name": "Plans", "children": []}]}
    session = session_with(tree, tree)
    assert extractor.find_folder_by_name(
        session, "https://jira", "Plans", "10001", folder_type="testplan",
    )["id"] == 7
    assert extractor.find_folder_name_by_id(
        session, "https://jira", "10001", 7, folder_type="testplan",
    ) == "Plans"
    assert all(
        call.args[0].endswith("/project/10001/foldertree/testplan")
        for call in session.get.call_args_list
    )
