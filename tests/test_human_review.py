import copy
import itertools
import json
import random
import uuid

import pytest

from argus.domain.review_assignment import allocate, eligible, reviewer_aliases, validate_roster
from argus.operations.review_identities import IdentityUnavailable, JiraReviewerResolver
from argus.operations.review_service import ReviewError, ReviewService
from argus.operations.review_store import SqliteReviewStore
from argus.reporting.review_data import build_documents, digest, with_review_link


def roster(n=3):
    return [{"alias": f"user{i}", "jira_key": f"JIRA{i}", "name": f"Reviewer {i}"} for i in range(n)]


def resolve_reviewers(aliases):
    return [r for r in roster(50) if r["alias"] in aliases]


def test_optimal_balancing_matches_exhaustive_solutions():
    rng = random.Random(53)
    reviewers = roster()
    for _ in range(50):
        cases = [{"key": str(i), "executor": rng.choice(["JIRA0", "JIRA1", "JIRA2", "other"])}
                 for i in range(rng.randint(1, 7))]
        result = allocate(cases, reviewers, seed="stable")
        scores = []
        choices = [[r["alias"] for r in reviewers if eligible(case, r)] for case in cases]
        for candidate in itertools.product(*choices):
            scores.append(sum(candidate.count(r["alias"]) ** 2 for r in reviewers))
        assert sum(count ** 2 for count in result["counts"].values()) == min(scores)
        assert len(result["assignments"]) == len(cases)
        assert result == allocate(list(reversed(cases)), list(reversed(reviewers)), seed="stable")


@pytest.mark.parametrize("reviewer_count", [5, 50])
def test_thousand_cases_fixed_work_and_executor_exclusion(reviewer_count):
    cases = [{"key": f"E{i}", "executor": f"JIRA{i % reviewer_count}"} for i in range(1000)]
    result = allocate(cases, roster(reviewer_count), seed="big", fixed={"E0": "user1"})
    assert result["counts"] == dict.fromkeys(
        [f"user{i}" for i in range(reviewer_count)], 1000 // reviewer_count,
    )
    assert result["assignments"]["E0"] == "user1"
    assert all(result["assignments"][c["key"]] != f"user{i % reviewer_count}"
               for i, c in enumerate(cases))


def test_balancing_with_fixed_work_matches_exhaustive_solutions():
    rng = random.Random(64)
    reviewers = roster()
    for _ in range(50):
        cases = [{"key": str(i), "executor": rng.choice(["JIRA0", "JIRA1", "JIRA2", "other"])}
                 for i in range(rng.randint(2, 7))]
        fixed = {case["key"]: rng.choice([r["alias"] for r in reviewers if eligible(case, r)])
                 for case in cases if rng.choice([True, False])}
        choices = [[fixed[case["key"]]] if case["key"] in fixed
                   else [r["alias"] for r in reviewers if eligible(case, r)] for case in cases]
        minimum = min(sum(candidate.count(r["alias"]) ** 2 for r in reviewers)
                      for candidate in itertools.product(*choices))
        result = allocate(cases, reviewers, seed="fixed", fixed=fixed)
        assert sum(count ** 2 for count in result["counts"].values()) == minimum
        assert all(result["assignments"][key] == owner for key, owner in fixed.items())


def test_impossible_balance_and_unknown_executors():
    cases = [{"key": str(i), "executor": "JIRA0"} for i in range(10)]
    result = allocate(cases + [{"key": "unknown", "executor": None}], roster(2), seed="s")
    assert result["counts"] == {"user0": 0, "user1": 10}
    assert "unknown" in result["unassigned"]
    assert len(allocate(cases, roster(1), seed="s")["unassigned"]) == 10


@pytest.mark.parametrize("change", ["duplicate_alias", "duplicate_key", "missing_key", "bad_alias"])
def test_identity_validation(change):
    rows = [{**r, "confirmed": True} for r in roster()]
    if change == "duplicate_alias":
        rows[1]["alias"] = rows[0]["alias"].upper()
    elif change == "duplicate_key":
        rows[1]["jira_key"] = rows[0]["jira_key"].lower()
    elif change == "missing_key":
        rows[0]["jira_key"] = ""
    else:
        rows[0]["alias"] = "<script>"
    with pytest.raises(ValueError):
        validate_roster(rows)


class Data:
    def __init__(self, count=6):
        self.items = [{
            "key": f"QA-E{i}", "executor": f"JIRA{i % 3}", "revision": f"revision{i}",
            "run": "QA-C1", "test_case": f"Case {i}", "analysis": "complete",
            "ready": True, "verdict": "pass",
        } for i in range(count)]
        self.details = {c["key"]: {
            **c, "findings": [], "steps": [{"index": 0}], "evidence": [{"url": "x.png"}],
        } for c in self.items}

    def manifest(self, team, folder):
        return {"items": copy.deepcopy(self.items), "revision": digest(self.items),
                "coverage_total": len(self.items) + 2, "coverage_missing": 2}

    def detail(self, team, folder, case):
        return copy.deepcopy(self.details[case["key"]])


@pytest.fixture
def workspace():
    service = ReviewService(SqliteReviewStore(":memory:"), Data(), resolve_reviewers)

    def call(action, body=None, actor="coordinator", query=None, request_id=None):
        if body is not None:
            state = service.handle("desktop", "HS_Reg", "queue", "GET", actor, {}, {})
            body = {"version": state["version"], "report_revision": state["report_revision"], **body}
        return service.handle(
            "desktop", "HS_Reg", action, "POST" if body is not None else "GET",
            actor, body or {}, query or {}, request_id or uuid.uuid4().hex,
        )
    return service, call


def assign(call):
    preview = call("preview", {"roster": [{"alias": r["alias"]} for r in roster()]})
    call("allocate", {"preview_id": preview["preview_id"]})
    return preview


def save(call, key, actor, **values):
    detail = call(f"items/{key}")
    return call(f"items/{key}", {
        "revision": detail["case"]["revision"], "state": "reviewed", "decisions": {},
        "attested": True, "review_version": detail["review_version"], **values,
    }, actor=actor)


def test_full_review_and_queue_coverage(workspace):
    _, call = workspace
    preview = assign(call)
    for key, actor in preview["assignments"].items():
        save(call, key, actor)
    queue = call("queue")
    assert queue["totals"] == {"reviewed": 6, "pending": 0, "investigation": 0, "unassigned": 2}
    assert queue["total"] == 8
    assert len(call("queue", actor="user0", query={"mine": "1"})["items"]) == 2
    filtered = call("queue", actor="coordinator", query={"reviewer": "user1"})
    assert filtered["filtered_total"] == 2
    assert all(row["assignee"] == "user1" for row in filtered["items"])
    assert filtered["total"] == 8
    assert call("queue", query={"reviewer": "unassigned-user"})["filtered_total"] == 0


def test_run_names_remain_available_when_filtering_and_paginating(workspace):
    service, call = workspace
    service.data.items[0].update(run="QA-C2", run_name="Membership purchases")
    service.data.items[1]["run_name"] = "Podcast purchases"
    for query in ({"limit": "1", "offset": "2"}, {"run": "QA-C2"},
                  {"reviewer": "unassigned-user"}):
        queue = call("queue", query=query)
        assert queue["runs"] == ["QA-C1", "QA-C2"]
        assert queue["run_names"] == {
            "QA-C1": "Podcast purchases", "QA-C2": "Membership purchases",
        }
        if query.get("run"):
            assert [item["key"] for item in queue["items"]] == ["QA-E0"]


@pytest.mark.parametrize("query", [{"offset": "abc"}, {"limit": "x"}, {"offset": {"invalid": True}}])
def test_invalid_pagination_has_a_readable_error(workspace, query):
    _, call = workspace
    with pytest.raises(ReviewError, match="Offset and limit must be integers.") as error:
        call("queue", query=query)
    assert error.value.status == 400


@pytest.mark.parametrize("verdict", ["pass", "concerns", "fail"])
def test_verdict_filter_combines_with_review_filters_before_pagination(workspace, verdict):
    service, call = workspace
    service.data = Data(12)
    for index, item in enumerate(service.data.items):
        item.update(verdict=["pass", "concerns", "fail"][index % 3], run=f"RUN{index % 2}")
        service.data.details[item["key"]].update(item)
    assign(call)
    all_cases = call("queue")
    filtered = call("queue", query={"verdict": verdict})
    assert filtered["filtered_total"] == 4
    assert all(item["verdict"] == verdict for item in filtered["items"])
    assert filtered["totals"] == all_cases["totals"]
    assert filtered["loads"] == all_cases["loads"]
    page = call("queue", query={"verdict": verdict, "offset": "1", "limit": "1"})
    assert page["items"] == filtered["items"][1:2]
    assert page["filtered_total"] == 4
    selected = filtered["items"][0]
    save(call, selected["key"], selected["assignee"], state="investigation")
    combined = call("queue", query={
        "verdict": verdict, "reviewer": selected["assignee"],
        "run": selected["run"], "state": "investigation",
    })
    assert [item["key"] for item in combined["items"]] == [selected["key"]]
    assert len(call("queue")["items"]) == 12


def test_reviewer_progress_counts_all_states_independently_of_queue_filters(workspace):
    service, call = workspace
    service.data = Data(12)
    assigned = assign(call)["assignments"]
    owner = next(iter(assigned.values()))
    keys = [key for key, alias in assigned.items() if alias == owner]
    assert len(keys) == 4
    save(call, keys[0], owner, state="in_progress")
    save(call, keys[1], owner, state="investigation")
    save(call, keys[2], owner)
    for query in ({}, {"state": "reviewed", "reviewer": owner, "limit": "1"}, {"run": "another-run"}):
        queue = call("queue", query=query)
        assert queue["loads"][owner] == {
            "assigned": 4, "pending": 1, "in_progress": 1, "investigation": 1, "reviewed": 1,
        }
        for load in queue["loads"].values():
            assert load["assigned"] == sum(load[state] for state in (
                "pending", "in_progress", "investigation", "reviewed",
            ))
        assert sum(load["pending"] + load["in_progress"] for load in queue["loads"].values()) == queue["totals"]["pending"]
        assert sum(load["investigation"] for load in queue["loads"].values()) == queue["totals"]["investigation"]
        assert sum(load["reviewed"] for load in queue["loads"].values()) == queue["totals"]["reviewed"]
        assert sum(load["assigned"] for load in queue["loads"].values()) + queue["totals"]["unassigned"] == queue["total"]
        if not query:
            rows = {item["key"]: item for item in queue["items"]}
            assert [rows[key]["review_state"] for key in keys] == [
                "in_progress", "investigation", "reviewed", "pending",
            ]
            assert rows[keys[0]]["state"] == "pending"
    awaiting = call("queue", query={"state": "pending", "reviewer": owner})
    assert {item["key"] for item in awaiting["items"]} == {keys[0], keys[3]}


def test_non_assigned_reviewer_and_scope_denied(workspace):
    service, call = workspace
    preview = assign(call)
    key = next(iter(preview["assignments"]))
    with pytest.raises(ReviewError, match="Only the assigned") as error:
        save(call, key, "coordinator")
    assert error.value.status == 403
    with pytest.raises(ReviewError):
        call("items/QA-E999")
    # A second registered vertical now passes the workspace gate...
    assert service.handle("web", "HS_Reg", "queue", "GET", "user0", {}, {})
    # ...but an unregistered team and a bad folder are still rejected.
    with pytest.raises(ReviewError):
        service.handle("not-a-team", "HS_Reg", "queue", "GET", "user0", {}, {})
    with pytest.raises(ReviewError):
        service.handle("desktop", "../other", "queue", "GET", "user0", {}, {})


def test_completed_and_started_work_preserved_and_transfer_logged(workspace):
    _, call = workspace
    preview = assign(call)
    key, owner = next(iter(preview["assignments"].items()))
    save(call, key, owner, state="in_progress", notes="Started checking")
    new = call("preview", {"roster": [{**r, "confirmed": True} for r in roster()]})
    assert new["assignments"][key] == owner
    call("allocate", {"preview_id": new["preview_id"]})
    detail = call(f"items/{key}")
    new_owner = next(r["alias"] for r in roster() if r["alias"] != owner and eligible(detail["case"], r))
    call("transfer", {"key": key, "assignee": new_owner, "reason": "Reviewer unavailable"})
    detail = call(f"items/{key}")
    assert detail["assignee"] == new_owner
    assert detail["review"]["notes"] == "Started checking"
    assert detail["history"][-1]["action"] == "transfer"
    save(call, key, new_owner)
    with pytest.raises(ReviewError, match="Completed"):
        call("transfer", {"key": key, "assignee": owner, "reason": "Cannot change completed"})


def test_mapping_change_rejected(workspace):
    service, call = workspace
    preview = assign(call)
    key, owner = next(iter(preview["assignments"].items()))
    save(call, key, owner, state="in_progress")
    with pytest.raises(ReviewError, match="identity cannot"):
        service.resolve_reviewers = lambda aliases: [
            {**r, "jira_key": r["jira_key"] + "changed"} for r in resolve_reviewers(aliases)
        ]
        call("preview", {"roster": [{"alias": r["alias"]} for r in roster()]})


def test_removed_coordinator_can_reassign_started_work_and_save_allocation(workspace):
    _, call = workspace
    original = assign(call)
    key, owner = next(iter(original["assignments"].items()))
    save(call, key, owner, state="in_progress", notes="Keep the saved investigation")
    preview = call("preview", {
        "roster": [{"alias": row["alias"]} for row in roster() if row["alias"] != owner],
    }, actor=owner)
    target = preview["assignments"][key]
    assert target != owner
    assert preview["reassignments"][key]["from"] == owner
    assert preview["reassignments"][key]["to"] == target
    assert call(f"items/{key}")["assignee"] == owner
    assert len(call(f"items/{key}")["history"]) == 1
    call("allocate", {"preview_id": preview["preview_id"]}, actor=owner)
    detail = call(f"items/{key}")
    assert detail["assignee"] == target
    assert detail["review"]["notes"] == "Keep the saved investigation"
    assert detail["history"][-1]["action"] == "transfer"
    assert detail["history"][-1]["actor"] == owner
    assert detail["history"][-1]["from"] == owner
    assert detail["history"][-1]["to"] == target
    assert owner not in {r["alias"] for r in call("queue")["roster"]}
    with pytest.raises(ReviewError, match="Only the assigned"):
        save(call, key, owner)
    save(call, key, target)
    assert call(f"items/{key}")["review"]["actor"] == target


def test_completed_reviews_keep_attribution_after_roster_removal(workspace):
    service, call = workspace
    original = assign(call)
    key, owner = next(iter(original["assignments"].items()))
    save(call, key, owner)
    selected = [{"alias": row["alias"]} for row in roster() if row["alias"] != owner]
    for _ in range(2):
        preview = call("preview", {"roster": selected}, actor=owner)
        assert preview["assignments"][key] == owner
        assert preview["completed_preserved"] == 1
        call("allocate", {"preview_id": preview["preview_id"]}, actor=owner)
        queue = call("queue")
        assert owner not in {r["alias"] for r in queue["roster"]}
        assert queue["retained_roster"] == [r for r in roster() if r["alias"] == owner]
        assert queue["totals"]["reviewed"] == 1
        assert queue["loads"][owner] == {
            "assigned": 1, "pending": 0, "in_progress": 0, "investigation": 0, "reviewed": 1,
        }
        retained = call("queue", query={"reviewer": owner})
        assert retained["filtered_total"] == 1 and retained["items"][0]["key"] == key
        assert call(f"items/{key}")["review"]["actor"] == owner
        assert len(call(f"items/{key}")["history"]) == 1
        other = next(k for k in preview["assignments"] if k != key)
        with pytest.raises(ReviewError, match="eligible reviewer"):
            call("transfer", {"key": other, "assignee": owner, "reason": "Removed reviewer"})
    # A changed audit reopens the execution without assigning new work to a retired reviewer.
    next(c for c in service.data.items if c["key"] == key)["revision"] = "new-audit"
    assert call(f"items/{key}")["assignee"] is None
    assert call("queue")["totals"]["reviewed"] == 0


def test_removed_started_work_with_no_eligible_reviewer_preserves_notes(workspace):
    _, call = workspace
    original = assign(call)
    key, owner = next(iter(original["assignments"].items()))
    detail = call(f"items/{key}")
    executor = next(r["alias"] for r in roster() if r["jira_key"] == detail["case"]["executor"])
    save(call, key, owner, state="investigation", notes="Evidence needs checking")
    preview = call("preview", {"roster": [{"alias": executor}]}, actor=owner)
    assert key in preview["unassigned"]
    assert preview["reassignments"][key]["to"] is None
    call("allocate", {"preview_id": preview["preview_id"]}, actor=owner)
    detail = call(f"items/{key}")
    assert detail["assignee"] is None
    assert detail["review"]["notes"] == "Evidence needs checking"
    assert detail["history"][-1]["to"] is None


def test_reassignment_preview_cannot_overwrite_work_completed_after_preview(workspace):
    _, call = workspace
    original = assign(call)
    key, owner = next(iter(original["assignments"].items()))
    save(call, key, owner, state="in_progress")
    preview = call("preview", {
        "roster": [{"alias": row["alias"]} for row in roster() if row["alias"] != owner],
    }, actor=owner)
    save(call, key, owner)
    with pytest.raises(ReviewError, match="stale"):
        call("allocate", {"preview_id": preview["preview_id"]}, actor=owner)
    assert call(f"items/{key}")["review"]["state"] == "reviewed"
    assert all(event.get("action") != "transfer" for event in call(f"items/{key}")["history"])


def test_only_resolved_jira_keys_control_exclusions(workspace):
    _, call = workspace
    preview = call("preview", {"roster": [
        {"alias": " USER0 ", "jira_key": "fake-key", "name": "Fake", "confirmed": True},
    ]})
    assert preview["roster"] == roster(1)
    assert all(key not in preview["assignments"] for key in ["QA-E0", "QA-E3"])
    assert len(preview["assignments"]) == 4


@pytest.mark.parametrize("value", [[], [{}], ["user0"], [{"alias": "<script>"}],
                                   [{"alias": "user0"}, {"alias": "USER0"}]])
def test_alias_input_validation(value):
    with pytest.raises(ValueError):
        reviewer_aliases(value)


def test_failed_resolution_leaves_roster_and_version_unchanged(workspace):
    service, call = workspace
    assign(call)
    before = call("queue")

    def unavailable(aliases):
        raise IdentityUnavailable("Jira lookup is unavailable.")

    service.resolve_reviewers = unavailable
    with pytest.raises(ReviewError) as error:
        call("preview", {"roster": [{"alias": "user0"}]})
    assert error.value.status == 503
    assert call("queue") == before
    service.resolve_reviewers = lambda aliases: []
    with pytest.raises(ReviewError):
        call("preview", {"roster": [{"alias": "user0"}]})
    assert call("queue") == before


@pytest.mark.parametrize("status,changes,expected", [
    (200, {}, None),
    (404, {}, ValueError),
    (200, {"active": False}, ValueError),
    (200, {"name": "somebodyelse"}, ValueError),
    (200, {"key": ""}, ValueError),
    (302, {}, IdentityUnavailable),
    (503, {}, IdentityUnavailable),
])
def test_jira_alias_resolution_is_exact_and_read_only(monkeypatch, status, changes, expected):
    from unittest.mock import MagicMock

    provider = MagicMock()
    provider.get_pat.return_value = "test-token"
    session = MagicMock()
    session.__enter__.return_value = session
    session.get.return_value.status_code = status
    session.get.return_value.json.return_value = {
        "name": "user0", "key": "JIRA0", "displayName": "Reviewer 0", "active": True, **changes,
    }
    monkeypatch.setattr("argus.operations.review_identities.requests.Session", lambda: session)
    resolver = JiraReviewerResolver(provider, "https://jira.example")
    if expected:
        with pytest.raises(expected):
            resolver(["user0"])
    else:
        assert resolver(["user0"]) == roster(1)
    args, kwargs = session.get.call_args
    assert args == ("https://jira.example/rest/api/2/user",)
    assert kwargs["params"] == {"username": "user0"}
    assert kwargs["allow_redirects"] is False
    provider.get_pat.assert_called_once()




def test_review_link_moves_to_report_sidebar_and_is_idempotent():
    from argus.reporting.html_report import _cs_sidenav

    sidebar = _cs_sidenav("../", "coverage", True)
    html = '<body><p data-argus-review-link><a href="review.html">Old link</a></p>' + sidebar + '</body>'
    result = with_review_link(html)
    assert result.count("data-argus-review-link") == 1
    assert "Old link" not in result
    assert result.index("<nav>") < result.index("data-argus-review-link") < result.index("</nav>")
    assert with_review_link(result) == result


def test_preview_conflicts_and_idempotent_save(workspace):
    _, call = workspace
    preview = assign(call)
    next_preview = call("preview", {"roster": [{**r, "confirmed": True} for r in roster()]})
    key, owner = next(iter(preview["assignments"].items()))
    save(call, key, owner, state="in_progress")
    with pytest.raises(ReviewError, match="stale"):
        call("allocate", {"preview_id": next_preview["preview_id"]})
    state = call("queue")
    body = {"revision": call(f"items/{key}")["case"]["revision"], "state": "reviewed",
            "attested": True, "decisions": {}, "version": state["version"],
            "report_revision": state["report_revision"],
            "review_version": call(f"items/{key}")["review_version"]}
    request = uuid.uuid4().hex
    first = call(f"items/{key}", body, actor=owner, request_id=request)
    second = call(f"items/{key}", body, actor=owner, request_id=request)
    assert first == second
    assert len(call(f"items/{key}")["history"]) == 2
    with pytest.raises(ReviewError, match="different input"):
        call(f"items/{key}", {**body, "notes": "changed"}, actor=owner, request_id=request)


def test_changed_audit_reopens_and_changed_executor_unassigns(workspace):
    service, call = workspace
    preview = assign(call)
    key, owner = next(iter(preview["assignments"].items()))
    save(call, key, owner)
    assert call("queue")["totals"]["reviewed"] == 1
    case = next(c for c in service.data.items if c["key"] == key)
    case["revision"] = "new"
    assert call("queue")["totals"]["reviewed"] == 0
    assert call(f"items/{key}")["history"]
    case["executor"] = next(r["jira_key"] for r in roster() if r["alias"] == owner)
    assert call(f"items/{key}")["assignee"] is None
    with pytest.raises(ReviewError):
        save(call, key, owner)


def test_finding_resolution_and_incomplete_blockers(workspace):
    service, call = workspace
    preview = assign(call)
    key, owner = next(iter(preview["assignments"].items()))
    service.data.details[key]["findings"] = [{"id": "finding1"}]
    with pytest.raises(ReviewError, match="resolve every finding"):
        save(call, key, owner)
    with pytest.raises(ReviewError, match="Explain"):
        save(call, key, owner, decisions={"finding1": {"decision": "reject"}})
    with pytest.raises(ReviewError, match="resolve every finding"):
        save(call, key, owner, decisions={"finding1": {"decision": "investigate", "reason": "unclear"}})
    save(call, key, owner, decisions={"finding1": {"decision": "confirm"}})
    next(c for c in service.data.items if c["key"] == key)["ready"] = False
    with pytest.raises(ReviewError, match="Check all steps"):
        save(call, key, owner, decisions={"finding1": {"decision": "confirm"}})


@pytest.mark.parametrize(("state", "decisions"), [
    ("investigation", {}),
    ("investigation", {"finding1": {"decision": "investigate", "reason": "Check recording"}}),
    ("in_progress", {"finding1": {"decision": "investigate"}}),
])
def test_investigation_is_saved_without_notes_and_survives_progress_saves(workspace, state, decisions):
    service, call = workspace
    preview = assign(call)
    key, owner = next(iter(preview["assignments"].items()))
    service.data.details[key]["findings"] = [{"id": "finding1"}]
    result = save(call, key, owner, state=state, decisions=decisions, notes="")
    assert result["state"] == "investigation"
    detail = call(f"items/{key}")
    assert detail["review"]["state"] == "investigation"
    assert detail["review"]["notes"] == ""
    assert detail["history"][-1]["state"] == "investigation"
    queue = call("queue", query={"state": "investigation"})
    assert queue["totals"]["investigation"] == 1
    assert [item["key"] for item in queue["items"]] == [key]
    save(call, key, owner, state="in_progress", decisions=decisions, notes="Follow-up added")
    assert call(f"items/{key}")["review"]["state"] == "investigation"
    save(call, key, owner, decisions={"finding1": {"decision": "confirm"}})
    assert call("queue")["totals"]["investigation"] == 0
    assert call("queue")["totals"]["reviewed"] == 1


def test_manifest_scope_hashes_missing_coverage_and_read_only(tmp_path):
    folder = tmp_path / "HS_Reg"
    for key in ("QA-E1", "QA-E2"):
        execution = folder / key
        execution.mkdir(parents=True)
        (execution / "audit.json").write_text(json.dumps({
            "analysis_status": "complete", "overall_verdict": "pass", "findings": [],
        }))
        (execution / "metadata.json").write_text(json.dumps({"test_results": [{
            "executed_by": "JIRA1", "test_case_key": "QA-T1",
            "steps": [{"index": 0, "description": "Check price", "expected_result": "Price is $1"}],
        }]}))
        (execution / "image.png").write_bytes(b"original evidence")
    (folder / "_coverage.json").write_text(json.dumps({"items_total": 3, "executions": []}))
    (folder / "_latest_run.json").write_text(json.dumps({
        "version": 1, "folder_name": "HS_Reg", "started_at": "now", "completed_at": "now",
        "scope_keys": ["QA-E1"], "selected_keys": ["QA-E1"],
        "audit_keys": ["QA-E1"], "failed_keys": [], "statuses": {},
    }))
    before = {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    manifest, details, _ = build_documents(folder, "desktop")
    assert [item["key"] for item in manifest["items"]] == ["QA-E1"]
    assert manifest["coverage_missing"] == 2
    assert details["QA-E1"]["steps"][0]["expected"] == "Price is $1"
    assert before == {p: p.read_bytes() for p in before}
    (folder / "_argus.html").write_text("new presentation")
    assert build_documents(folder, "desktop")[0] == manifest
    (folder / "QA-E1/image.png").write_bytes(b"new evidence")
    assert build_documents(folder, "desktop")[0]["revision"] != manifest["revision"]


class PublishedData:
    """Keep immutable detail documents across successive local publications."""

    def __init__(self, folder):
        self.folder = folder
        self.documents = {}
        self.publish()

    def publish(self, legacy=False):
        self.latest, details, _ = build_documents(self.folder, "desktop")
        if legacy:
            for item in self.latest["items"]:
                item.pop("review_revision")
            for detail in details.values():
                detail.pop("review_revision")
        self.documents.update({
            (key, detail["revision"]): detail for key, detail in details.items()
        })

    def manifest(self, team, folder):
        return copy.deepcopy(self.latest)

    def detail(self, team, folder, case):
        try:
            return copy.deepcopy(self.documents[(case["key"], case["revision"])])
        except KeyError:
            raise ReviewError(404, "Review document not found.") from None


def write_review_execution(folder, key):
    execution = folder / key
    execution.mkdir(parents=True, exist_ok=True)
    (execution / "audit.json").write_text(json.dumps({
        "analysis_status": "complete", "overall_verdict": "concerns",
        "findings": [{"rule": "R11", "description": "No linked defect", "severity": "high"}],
        "audited_at": "2026-09-20T10:00:00Z",
    }))
    (execution / "metadata.json").write_text(json.dumps({"test_results": [{
        "executed_by": "JIRA1", "test_case_key": "QA-T1", "testrun_key": "QA-C1",
        "steps": [{"index": 0, "description": "Check price", "expected_result": "Price is $1",
                   "status": "Pass"}],
    }]}))
    (execution / "image.png").write_bytes(b"original evidence")
    return execution


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("state", ["reviewed", "in_progress", "investigation"])
def test_new_publication_preserves_existing_review_and_allocation(workspace, tmp_path, legacy, state):
    service, call = workspace
    key = "QA-E1"
    execution = write_review_execution(tmp_path / "HS_Reg", key)
    service.data = PublishedData(execution.parent)
    service.data.publish(legacy=legacy)
    owner = assign(call)["assignments"][key]
    detail = call(f"items/{key}")
    decisions = {f["id"]: {"decision": "confirm", "reason": ""} for f in detail["case"]["findings"]}
    save(call, key, owner, state=state, notes="Already checked this evidence", decisions=decisions)
    saved = call(f"items/{key}")

    # A new audit arrives alongside a refresh of the existing execution.
    write_review_execution(execution.parent, "QA-E2")
    audit_path = execution / "audit.json"
    audit_data = json.loads(audit_path.read_text())
    audit_data.update(audited_at="2026-09-21T10:00:00Z", token_usage={"output": 1200})
    audit_path.write_text(json.dumps(audit_data))
    metadata_path = execution / "metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["extracted_at"] = "2026-09-21T10:00:00Z"
    metadata["test_results"][0]["executed_by_name"] = "Updated display name"
    metadata_path.write_text(json.dumps(metadata))
    service.data.publish()

    current = call(f"items/{key}")
    assert current["case"]["revision"] != saved["case"]["revision"]
    assert current["review"] == saved["review"]
    assert current["history"] == saved["history"]
    assert current["review_version"] == saved["review_version"]
    assert current["assignee"] == owner
    queue = call("queue")
    assert next(row for row in queue["items"] if row["key"] == key)["review_state"] == state
    assert queue["totals"]["unassigned"] == 1
    assert assign(call)["assignments"][key] == owner
    assert call(f"items/{key}")["review"] == saved["review"]

    # A stale browser still has to reload the current document before saving.
    with pytest.raises(ReviewError, match="Audit or evidence changed"):
        call(f"items/{key}", {
            "revision": saved["case"]["revision"], "review_version": saved["review_version"],
            "state": state, "decisions": decisions,
        }, actor=owner)
    save(call, key, owner, state=state, decisions=decisions, notes="Follow-up")
    assert call(f"items/{key}")["review"]["review_revision"] == current["case"]["review_revision"]


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("change", ["evidence", "step", "finding", "rule", "executor", "verdict", "incomplete"])
def test_changed_review_content_reopens_only_the_changed_case(workspace, tmp_path, legacy, change):
    service, call = workspace
    folder = tmp_path / "HS_Reg"
    execution = write_review_execution(folder, "QA-E1")
    write_review_execution(folder, "QA-E2")
    service.data = PublishedData(folder)
    service.data.publish(legacy=legacy)
    for key, owner in assign(call)["assignments"].items():
        detail = call(f"items/{key}")["case"]
        save(call, key, owner, decisions={f["id"]: {"decision": "confirm"} for f in detail["findings"]})
    if change == "evidence":
        (execution / "image.png").write_bytes(b"changed evidence")
    elif change in {"step", "executor"}:
        path = execution / "metadata.json"
        metadata = json.loads(path.read_text())
        if change == "step":
            metadata["test_results"][0]["steps"][0]["expected_result"] = "Price is $2"
        else:
            metadata["test_results"][0]["executed_by"] = "another-executor"
        path.write_text(json.dumps(metadata))
    else:
        path = execution / "audit.json"
        audit_data = json.loads(path.read_text())
        if change == "finding":
            audit_data["findings"][0]["description"] = "New issue"
        elif change == "rule":
            audit_data["findings"][0]["rule"] = "R10"
        elif change == "verdict":
            audit_data["overall_verdict"] = "fail"
        else:
            audit_data["analysis_status"] = "incomplete"
        path.write_text(json.dumps(audit_data))
    service.data.publish()
    changed = call("items/QA-E1")
    assert changed["review"] == {}
    assert changed["history"][0]["state"] == "reviewed"
    assert call("items/QA-E2")["review"]["state"] == "reviewed"
    assert call("queue")["totals"]["reviewed"] == 1


def test_missing_legacy_document_does_not_restore_unverified_review(workspace, tmp_path):
    service, call = workspace
    execution = write_review_execution(tmp_path / "HS_Reg", "QA-E1")
    service.data = PublishedData(execution.parent)
    service.data.publish(legacy=True)
    owner = assign(call)["assignments"]["QA-E1"]
    save(call, "QA-E1", owner, state="in_progress")
    service.data.documents.clear()
    path = execution / "audit.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "audited_at": "new"}))
    service.data.publish()
    assert call("items/QA-E1")["review"] == {}








def test_other_case_saves_do_not_invalidate_draft_but_same_case_saves_do(workspace):
    _, call = workspace
    preview = assign(call)
    (key, owner), (other, other_owner) = list(preview["assignments"].items())[:2]
    detail = call(f"items/{key}")
    draft = {"revision": detail["case"]["revision"], "review_version": detail["review_version"],
             "state": "in_progress", "version": detail["version"],
             "report_revision": detail["report_revision"], "notes": "My independent draft"}
    save(call, other, other_owner)
    call(f"items/{key}", draft, actor=owner)
    with pytest.raises(ReviewError, match="review changed"):
        call(f"items/{key}", draft, actor=owner)
