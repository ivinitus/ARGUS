"""Unit tests for coverage.py — R15 per-folder coverage-gap detection.

R15 has three sub-rules:
    R15a — planned-but-not-executed (per item)
    R15b — marketplace coverage hole (per test case)
    R15c — tester abandonment (per assignee)

Tests use a stub HistoryIndex (same pattern as
tests/test_history_and_streak.py) so we never touch disk for the
escalation path.
"""
from __future__ import annotations

import json

import coverage as cov


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _item(
    *,
    test_case_key: str,
    testrun_key: str = "QA-C1",
    marketplace: str | None = "US",
    status: str = "Pass",
    assigned_to: str | None = "USER1",
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
    """Mirror of history_index.HistoryEntry, just enough for coverage.py."""

    def __init__(self, status: str):
        self.status = status


class _StubIndex:
    """Tiny stand-in for HistoryIndex so tests don't touch disk."""

    def __init__(self, buckets: dict[str, list[_StubHistoryEntry]]):
        self._buckets = buckets

    def get_history(self, tc_key: str, mp: str | None):
        bucket_key = f"{tc_key}|{mp or '_'}"
        return list(self._buckets.get(bucket_key, []))


# ---------------------------------------------------------------------------
# R15a — planned-but-not-executed
# ---------------------------------------------------------------------------
def test_r15a_fires_on_unexecuted_item():
    snap = cov.CoverageSnapshot(
        folder_name="F",
        items=[_item(test_case_key="QA-T1", status="Blocked")],
    )
    findings = cov.compute_gaps(snap)
    r15a = [f for f in findings if f["rule"] == "R15a"]
    assert len(r15a) == 1
    f = r15a[0]
    assert f["severity"] == "medium"
    assert f["test_case_key"] == "QA-T1"
    assert f["source"] == "rule"
    assert "Blocked" in f["description"]


def test_r15a_does_not_fire_on_executed_item():
    snap = cov.CoverageSnapshot(
        folder_name="F",
        items=[_item(test_case_key="QA-T1", status="Pass")],
    )
    findings = cov.compute_gaps(snap)
    assert [f for f in findings if f["rule"] == "R15a"] == []


def test_r15a_severity_escalates_with_history():
    """Same (TC, MP) Blocked in prior folder too → escalate to high."""
    snap = cov.CoverageSnapshot(
        folder_name="F",
        items=[_item(test_case_key="QA-T1",
                     marketplace="BR",
                     status="Blocked")],
    )
    history = _StubIndex({
        "QA-T1|BR": [
            _StubHistoryEntry("Blocked"),
            _StubHistoryEntry("Blocked"),
        ],
    })
    findings = cov.compute_gaps(snap, history_idx=history)
    r15a = [f for f in findings if f["rule"] == "R15a"]
    assert len(r15a) == 1
    assert r15a[0]["severity"] == "high"


def test_r15a_no_escalation_when_history_clean():
    """Prior folder was Pass — no escalation, severity stays medium."""
    snap = cov.CoverageSnapshot(
        folder_name="F",
        items=[_item(test_case_key="QA-T1",
                     marketplace="BR",
                     status="Blocked")],
    )
    history = _StubIndex({
        "QA-T1|BR": [_StubHistoryEntry("Pass")],
    })
    findings = cov.compute_gaps(snap, history_idx=history)
    r15a = [f for f in findings if f["rule"] == "R15a"]
    assert r15a[0]["severity"] == "medium"


# ---------------------------------------------------------------------------
# R15b — marketplace coverage hole
# ---------------------------------------------------------------------------
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
    # The missing-MP list is in `marketplace`; BR must appear there.
    assert "BR" in (f["marketplace"] or "")
    # Description names both the executed and missing MPs.
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
    assert r15b == []  # no executed reference point → no MP hole


# ---------------------------------------------------------------------------
# R15c — tester abandonment
# ---------------------------------------------------------------------------
def test_r15c_fires_on_low_completion():
    """Tester assigned 10, executed 4 (40%) → fires (below 60% default)."""
    items = (
        # 4 executed (Pass)
        [_item(test_case_key=f"T{i}", status="Pass",
               assigned_to_name="Alice", assigned_to="USER_A")
         for i in range(4)]
        # 6 unexecuted (Blocked / NotExecuted mix)
        + [_item(test_case_key=f"T{i+4}", status="Blocked",
                  assigned_to_name="Alice", assigned_to="USER_A")
           for i in range(3)]
        + [_item(test_case_key=f"T{i+7}", status="Not Executed",
                  assigned_to_name="Alice", assigned_to="USER_A")
           for i in range(3)]
    )
    snap = cov.CoverageSnapshot(folder_name="F", items=items)
    findings = cov.compute_gaps(snap)
    r15c = [f for f in findings if f["rule"] == "R15c"]
    assert len(r15c) == 1
    f = r15c[0]
    assert f["tester"] == "Alice"
    # Description includes both raw counts and the percentage.
    assert "10" in f["description"]
    assert "4" in f["description"]
    assert "%" in f["description"]


def test_r15c_does_not_fire_below_min_assigned():
    """Tester assigned 3, executed 0 → below min_assigned threshold (5)."""
    items = [
        _item(test_case_key=f"T{i}", status="Blocked",
              assigned_to_name="Bob", assigned_to="USER_B")
        for i in range(3)
    ]
    snap = cov.CoverageSnapshot(folder_name="F", items=items)
    findings = cov.compute_gaps(snap)
    r15c = [f for f in findings if f["rule"] == "R15c"]
    assert r15c == []


def test_r15c_does_not_fire_when_completion_above_threshold():
    """Tester assigned 10, executed 8 → 80% > 60% threshold."""
    items = (
        [_item(test_case_key=f"T{i}", status="Pass",
               assigned_to_name="Carol", assigned_to="USER_C")
         for i in range(8)]
        + [_item(test_case_key=f"T{i+8}", status="Blocked",
                  assigned_to_name="Carol", assigned_to="USER_C")
           for i in range(2)]
    )
    snap = cov.CoverageSnapshot(folder_name="F", items=items)
    findings = cov.compute_gaps(snap)
    r15c = [f for f in findings if f["rule"] == "R15c"]
    assert r15c == []


# ---------------------------------------------------------------------------
# to_coverage_json shape
# ---------------------------------------------------------------------------
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

    # Schema sanity.
    assert payload["schema"] == 1
    assert payload["folder"] == "Targeted_regression_22_May"
    assert payload["items_total"] == 2
    assert payload["items_executed"] == 1
    assert payload["items_unexecuted"] == 1
    assert payload["computed_at"].endswith("Z")
    # findings round-trips through json.dumps without surprises.
    assert isinstance(payload["findings"], list)
    serialised = json.dumps(payload)
    assert "schema" in serialised


def test_coverage_no_findings_clean_folder():
    """Every item Pass → 0 findings."""
    items = [
        _item(test_case_key=f"T{i}", marketplace="US", status="Pass",
              assigned_to_name=f"Tester{i}",
              assigned_to=f"USER_{i}")
        for i in range(8)
    ]
    snap = cov.CoverageSnapshot(folder_name="F", items=items)
    findings = cov.compute_gaps(snap)
    assert findings == []
    payload = cov.to_coverage_json(snap, findings)
    assert payload["items_unexecuted"] == 0
    assert payload["items_executed"] == 8
