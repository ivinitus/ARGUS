"""Unit tests for folder_coverage.py — R15 per-folder coverage-gap detection."""
from __future__ import annotations

import json

from argus.config import models
from argus.domain import folder_coverage as cov
from argus.domain import workflow_rules


def _item(
    *,
    test_case_key: str,
    testrun_key: str = "QA-C1",
    marketplace: str | None = "US",
    status: str = "Pass",
    assigned_to: str | None = "JIRAUSER1",
    assigned_to_name: str | None = "Tester One",
) -> cov.CoverageItem:
    return cov.CoverageItem(
        test_case_key=test_case_key,
        testrun_key=testrun_key,
        marketplace=marketplace,
        status=status,
        assigned_to=assigned_to,
        assigned_to_name=assigned_to_name,
    )


class _StubHistoryEntry:
    """Mirror of history_index.HistoryEntry, just enough for folder_coverage.py."""

    def __init__(self, status: str):
        self.status = status


class _StubIndex:
    """Tiny stand-in for HistoryIndex so tests don't touch disk."""

    def __init__(self, buckets: dict[str, list[_StubHistoryEntry]]):
        self._buckets = buckets

    def get_history(self, tc_key: str, mp: str | None):
        bucket_key = f"{tc_key}|{mp or '_'}"
        return list(self._buckets.get(bucket_key, []))


def test_r15a_fires_on_unexecuted_item():
    snap = cov.CoverageSnapshot(
        folder_name="F",
        items=[_item(test_case_key="QA-T1", status="Not Executed")],
    )
    findings = cov.compute_gaps(snap)
    r15a = [f for f in findings if f["rule"] == "R15a"]
    assert len(r15a) == 1
    f = r15a[0]
    assert f["severity"] == "medium"
    assert f["test_case_key"] == "QA-T1"
    assert f["testrun_key"] == "QA-C1"
    assert f["source"] == "rule"
    assert "Not Executed" in f["description"]


def test_r15a_does_not_fire_on_executed_item():
    snap = cov.CoverageSnapshot(
        folder_name="F",
        items=[_item(test_case_key="QA-T1", status="Pass")],
    )
    findings = cov.compute_gaps(snap)
    assert [f for f in findings if f["rule"] == "R15a"] == []


def test_r15a_does_not_fire_on_blocked_or_in_progress():
    snap = cov.CoverageSnapshot(
        folder_name="F",
        items=[
            _item(test_case_key="QA-T1", marketplace="BR",
                  status="Blocked"),
            _item(test_case_key="QA-T2", marketplace="BR",
                  status="In Progress"),
        ],
    )
    history = _StubIndex({
        "QA-T1|BR": [
            _StubHistoryEntry("Blocked"),
            _StubHistoryEntry("Blocked"),
        ],
    })
    findings = cov.compute_gaps(snap, history_idx=history)
    assert [f for f in findings if f["rule"] == "R15a"] == []


def test_r15b_fires_on_marketplace_hole():
    """Same TC executed in US/UK, NotExecuted in BR → R15b cites BR."""
    snap = cov.CoverageSnapshot(
        folder_name="F",
        items=[
            _item(test_case_key="QA-T44880",
                  marketplace="US", status="Pass"),
            _item(test_case_key="QA-T44880",
                  marketplace="UK", status="Pass"),
            _item(test_case_key="QA-T44880",
                  marketplace="BR", status="Not Executed"),
        ],
    )
    findings = cov.compute_gaps(snap)
    r15b = [f for f in findings if f["rule"] == "R15b"]
    assert len(r15b) == 1
    f = r15b[0]
    assert f["test_case_key"] == "QA-T44880"
    assert "BR" in (f["marketplace"] or "")
    assert "BR" in f["description"]
    assert "US" in f["description"]


def test_r15b_does_not_fire_when_TC_fully_skipped():
    """All MPs NotExecuted → R15a fires (3 times); R15b does NOT."""
    snap = cov.CoverageSnapshot(
        folder_name="F",
        items=[
            _item(test_case_key="QA-T1",
                  marketplace="US", status="Not Executed"),
            _item(test_case_key="QA-T1",
                  marketplace="UK", status="Not Executed"),
            _item(test_case_key="QA-T1",
                  marketplace="BR", status="Not Executed"),
        ],
    )
    findings = cov.compute_gaps(snap)
    r15a = [f for f in findings if f["rule"] == "R15a"]
    r15b = [f for f in findings if f["rule"] == "R15b"]
    assert len(r15a) == 3
    assert r15b == []


def test_r15c_fires_on_low_completion():
    """Tester assigned 10, executed 4 (40%) -> fires (below 100% target)."""
    items = (
        [_item(test_case_key=f"T{i}", status="Pass",
               assigned_to_name="Alice", assigned_to="JIRAUSER_A")
         for i in range(4)]
        + [_item(test_case_key=f"T{i+4}", status="Blocked",
                  assigned_to_name="Alice", assigned_to="JIRAUSER_A")
           for i in range(3)]
        + [_item(test_case_key=f"T{i+7}", status="Not Executed",
                  assigned_to_name="Alice", assigned_to="JIRAUSER_A")
           for i in range(3)]
    )
    snap = cov.CoverageSnapshot(folder_name="F", items=items)
    findings = cov.compute_gaps(snap)
    r15c = [f for f in findings if f["rule"] == "R15c"]
    assert len(r15c) == 1
    f = r15c[0]
    assert f["tester"] == "Alice"
    assert f["assigned_count"] == 10
    assert f["executed_count"] == 4
    assert f["completion_pct"] == 40
    assert f["threshold_pct"] == 100
    assert f["description"] == (
        "R15c: Alice completed 4 of 10 assigned item(s) (40%; target 100%)."
    )


def test_r15c_does_not_fire_below_min_assigned():
    """Tester assigned 3, executed 0 → below min_assigned threshold (5)."""
    items = [
        _item(test_case_key=f"T{i}", status="Blocked",
              assigned_to_name="Bob", assigned_to="JIRAUSER_B")
        for i in range(3)
    ]
    snap = cov.CoverageSnapshot(folder_name="F", items=items)
    findings = cov.compute_gaps(snap)
    r15c = [f for f in findings if f["rule"] == "R15c"]
    assert r15c == []


def test_r15c_does_not_fire_when_completion_is_100_percent():
    """Tester assigned 10, completed 10 -> meets the target."""
    items = [
        _item(test_case_key=f"T{i}", status="Pass",
              assigned_to_name="Carol", assigned_to="JIRAUSER_C")
        for i in range(10)
    ]
    snap = cov.CoverageSnapshot(folder_name="F", items=items)
    findings = cov.compute_gaps(snap)
    r15c = [f for f in findings if f["rule"] == "R15c"]
    assert r15c == []


def test_r15c_legacy_threshold_keyword_names_still_work():
    items = [
        _item(test_case_key="T1", status="Pass",
              assigned_to_name="Dana", assigned_to="JIRAUSER_D"),
        _item(test_case_key="T2", status="Not Executed",
              assigned_to_name="Dana", assigned_to="JIRAUSER_D"),
    ]
    snap = cov.CoverageSnapshot(folder_name="F", items=items)

    findings = cov.compute_gaps(
        snap,
        abandonment_threshold=0.75,
        min_assigned_for_abandonment=2,
    )

    r15c = [f for f in findings if f["rule"] == "R15c"]
    assert len(r15c) == 1
    assert r15c[0]["tester"] == "Dana"


def test_to_coverage_json_shape():
    snap = cov.CoverageSnapshot(
        folder_name="Targeted_regression_22_May",
        items=[
            _item(test_case_key="T1", status="Pass"),
            _item(test_case_key="T2", status="Blocked"),
        ],
    )
    findings = cov.compute_gaps(snap)
    payload = cov.to_coverage_json(snap, findings)

    assert payload["schema"] == 1
    assert payload["folder"] == "Targeted_regression_22_May"
    assert payload["items_total"] == 2
    assert payload["items_executed"] == 1
    assert payload["items_unexecuted"] == 1
    assert payload["computed_at"].endswith("Z")
    assert isinstance(payload["findings"], list)
    serialised = json.dumps(payload)
    assert "schema" in serialised


def test_to_coverage_json_includes_all_tester_completion():
    snap = cov.CoverageSnapshot(
        folder_name="F",
        items=[
            _item(test_case_key="T1", status="Pass",
                  assigned_to_name="Ada", assigned_to="JIRAUSER_1"),
            _item(test_case_key="T2", status="Pass",
                  assigned_to_name="Ada", assigned_to="JIRAUSER_1"),
            _item(test_case_key="T3", status="Not Executed",
                  assigned_to_name="Bob", assigned_to="JIRAUSER_2"),
        ],
    )
    payload = cov.to_coverage_json(snap, [])
    rows = payload["tester_completion"]
    assert [r["tester"] for r in rows] == ["Ada", "Bob"]
    assert rows[0] == {"tester": "Ada", "assigned_count": 2,
                       "executed_count": 2, "completion_pct": 100}
    assert rows[1] == {"tester": "Bob", "assigned_count": 1,
                       "executed_count": 0, "completion_pct": 0}


def test_to_coverage_json_roster_keeps_attempted_items():
    snap = cov.CoverageSnapshot(
        folder_name="F",
        items=[
            cov.CoverageItem(
                test_case_key="T1",
                testrun_key="QA-C1",
                marketplace="US",
                status="Pass",
                executed_by="JIRAUSER_1",
                executed_by_name="Ada",
                e_key="E-1",
            ),
            cov.CoverageItem(
                test_case_key="T2",
                testrun_key="QA-C1",
                marketplace="US",
                status="Not Executed",
                executed_by="JIRAUSER_2",
                executed_by_name="Bob",
                e_key="E-2",
            ),
        ],
    )

    payload = cov.to_coverage_json(snap, [])

    assert [row["e_key"] for row in payload["executions"]] == ["E-1", "E-2"]


def test_coverage_no_findings_clean_folder():
    """Every item Pass → 0 findings."""
    items = [
        _item(test_case_key=f"T{i}", marketplace="US", status="Pass",
              assigned_to_name=f"Tester{i}",
              assigned_to=f"JIRAUSER_{i}")
        for i in range(8)
    ]
    snap = cov.CoverageSnapshot(folder_name="F", items=items)
    findings = cov.compute_gaps(snap)
    assert findings == []
    payload = cov.to_coverage_json(snap, findings)
    assert payload["items_unexecuted"] == 0
    assert payload["items_executed"] == 8


def test_compute_gaps_filters_disabled_r15_subrules(monkeypatch):
    monkeypatch.setattr(
        cov,
        "_check_r15a",
        lambda *args: [{"rule": "R15a", "severity": "high"}],
    )
    monkeypatch.setattr(
        cov,
        "_check_r15b",
        lambda *args: [{"rule": "R15b", "severity": "high"}],
    )
    monkeypatch.setattr(
        cov,
        "_check_r15c",
        lambda *args, **kwargs: [{"rule": "R15c", "severity": "high"}],
    )
    workflow_rules.configure(models.PolicySettings(enabled_rules=["R15a"]))
    try:
        findings = cov.compute_gaps(cov.CoverageSnapshot("F", []))
    finally:
        workflow_rules.reset_configuration()

    assert [finding["rule"] for finding in findings] == ["R15a"]
