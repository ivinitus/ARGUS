"""R15: per-folder coverage checks."""
from __future__ import annotations

import datetime as _dt
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from argus.domain import workflow_rules


def is_executed_status(name: str | None) -> bool:
    """True when the status means "the tester completed this item"."""
    return workflow_rules.is_executed_status(name)


def _is_not_executed_status(name: str | None) -> bool:
    """True for "no result at all"."""
    return workflow_rules.status_role(name) == "not_executed"


_DEFAULT_COMPLETION_THRESHOLD = 1.0
_DEFAULT_MIN_ASSIGNED_FOR_COMPLETION = 5


class _HasGetHistory(Protocol):
    def get_history(self, tc_key: str, mp: str | None) -> list: ...


@dataclass
class CoverageItem:
    """One row from a Zephyr testrun's items list, normalised."""
    test_case_key: str
    testrun_key: str
    marketplace: str | None
    status: str
    assigned_to: str | None = None
    assigned_to_name: str | None = None
    testrun_name: str | None = None
    test_case_name: str | None = None
    executed_by: str | None = None
    executed_by_name: str | None = None
    e_key: str | None = None


@dataclass
class CoverageSnapshot:
    """All CoverageItems for one folder, plus the folder name."""
    folder_name: str
    items: list[CoverageItem] = field(default_factory=list)


def _finding(
    *,
    severity: str,
    rule: str,
    description: str,
    test_case_key: str | None = None,
    marketplace: str | None = None,
    tester: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """R15 finding dict, shape-compatible with workflow_rules._finding."""
    payload = {
        "severity": severity,
        "page": None,
        "step_index": None,
        "description": description,
        "source": "rule",
        "rule": rule,
        "test_case_key": test_case_key,
        "marketplace": marketplace,
        "tester": tester,
    }
    payload.update(extra)
    return payload


def _check_r15a(
    snapshot: CoverageSnapshot,
    history_idx: _HasGetHistory | None,
) -> list[dict[str, Any]]:
    """Fire one finding per (test_case, marketplace) item left unexecuted."""
    findings: list[dict[str, Any]] = []
    for item in snapshot.items:
        if not _is_not_executed_status(item.status):
            continue

        severity = "medium"
        mp_label = item.marketplace or "unknown MP"
        tester_label = item.assigned_to_name or item.assigned_to or "unassigned"
        description = (
            f"R15a: {item.test_case_key} in {mp_label} is still "
            f"{item.status} in {item.testrun_key}; owner: {tester_label}."
        )
        findings.append(_finding(
            severity=severity,
            rule="R15a",
            description=description,
            test_case_key=item.test_case_key,
            marketplace=item.marketplace,
            tester=item.assigned_to_name or item.assigned_to,
            testrun_key=item.testrun_key,
        ))
    return findings


def _check_r15b(snapshot: CoverageSnapshot) -> list[dict[str, Any]]:
    """Fire when a test case completed in some marketplaces but not all."""
    by_tc: dict[str, list[CoverageItem]] = {}
    for item in snapshot.items:
        if not item.test_case_key:
            continue
        by_tc.setdefault(item.test_case_key, []).append(item)

    findings: list[dict[str, Any]] = []
    for tc_key, items in by_tc.items():
        planned = {it.marketplace for it in items if it.marketplace is not None}
        executed = {
            it.marketplace for it in items
            if is_executed_status(it.status)
            and it.marketplace is not None
        }
        missing = planned - executed
        if not missing or not executed:
            continue
        missing_sorted = sorted(missing)
        executed_sorted = sorted(executed)
        description = (
            f"R15b: {tc_key} has completed coverage in "
            f"{', '.join(executed_sorted)} but not in "
            f"{', '.join(missing_sorted)}."
        )
        findings.append(_finding(
            severity="medium",
            rule="R15b",
            description=description,
            test_case_key=tc_key,
            marketplace=",".join(missing_sorted),
        ))
    return findings


def completion_by_tester(snapshot: CoverageSnapshot) -> list[dict[str, Any]]:
    """Per-assignee completion rollup — EVERY assignee, no thresholds."""
    by_tester: dict[str, list[CoverageItem]] = {}
    for item in snapshot.items:
        bucket = item.assigned_to_name or item.assigned_to
        if not bucket:
            continue
        by_tester.setdefault(bucket, []).append(item)

    rows: list[dict[str, Any]] = []
    for tester, items in sorted(by_tester.items(),
                                key=lambda kv: kv[0].lower()):
        assigned = len(items)
        executed = sum(1 for it in items
                       if is_executed_status(it.status))
        rows.append({
            "tester": tester,
            "assigned_count": assigned,
            "executed_count": executed,
            "completion_pct": (
                int(round(100 * executed / assigned)) if assigned else 0),
        })
    return rows


def _check_r15c(
    snapshot: CoverageSnapshot,
    *,
    completion_threshold: float,
    min_assigned_for_completion: int,
) -> list[dict[str, Any]]:
    """Fire one finding per assignee whose completion rate is below threshold."""
    if min_assigned_for_completion < 1:
        min_assigned_for_completion = 1

    findings: list[dict[str, Any]] = []
    for row in completion_by_tester(snapshot):
        assigned = row["assigned_count"]
        if assigned < min_assigned_for_completion:
            continue
        executed = row["executed_count"]
        rate = executed / assigned
        if rate >= completion_threshold:
            continue
        tester = row["tester"]
        pct = row["completion_pct"]
        threshold_pct = int(round(completion_threshold * 100))
        description = (
            f"R15c: {tester} completed {executed} of {assigned} "
            f"assigned item(s) ({pct}%; target {threshold_pct}%)."
        )
        findings.append(_finding(
            severity="medium",
            rule="R15c",
            description=description,
            tester=tester,
            assigned_count=assigned,
            executed_count=executed,
            completion_pct=pct,
            threshold_pct=threshold_pct,
        ))
    return findings


def compute_gaps(
    snapshot: CoverageSnapshot,
    *,
    history_idx: _HasGetHistory | None = None,
    completion_threshold: float | None = None,
    min_assigned_for_completion: int | None = None,
    **legacy_options: Any,
) -> list[dict[str, Any]]:
    """Run R15a, R15b and R15c and return all findings."""
    if completion_threshold is None:
        completion_threshold = legacy_options.pop(
            "abandonment_threshold", _DEFAULT_COMPLETION_THRESHOLD)
    if min_assigned_for_completion is None:
        min_assigned_for_completion = legacy_options.pop(
            "min_assigned_for_abandonment",
            _DEFAULT_MIN_ASSIGNED_FOR_COMPLETION,
        )
    if legacy_options:
        unexpected = ", ".join(sorted(legacy_options))
        raise TypeError(f"unexpected coverage option(s): {unexpected}")

    out: list[dict[str, Any]] = []
    out.extend(_check_r15a(snapshot, history_idx))
    out.extend(_check_r15b(snapshot))
    out.extend(_check_r15c(
        snapshot,
        completion_threshold=completion_threshold,
        min_assigned_for_completion=min_assigned_for_completion,
    ))
    return workflow_rules.filter_enabled(out)


def _roster_rows(snapshot: CoverageSnapshot) -> list[dict[str, Any]]:
    """Per-execution roster: one row for every item someone attempted."""
    rows: list[dict[str, Any]] = []
    for it in snapshot.items:
        if not it.executed_by or not it.test_case_key:
            continue
        rows.append({
            "test_case_key": it.test_case_key,
            "test_case_name": it.test_case_name,
            "testrun_key": it.testrun_key,
            "testrun_name": it.testrun_name,
            "marketplace": it.marketplace,
            "status": it.status,
            "executed_by": it.executed_by,
            "executed_by_name": it.executed_by_name,
            "assigned_to": it.assigned_to,
            "assigned_to_name": it.assigned_to_name,
            "e_key": it.e_key,
        })
    return rows


def to_coverage_json(
    snapshot: CoverageSnapshot,
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the `_coverage.json` on-disk shape."""
    items_total = len(snapshot.items)
    items_executed = sum(
        1 for it in snapshot.items if is_executed_status(it.status)
    )
    items_unexecuted = items_total - items_executed
    return {
        "schema": 1,
        "folder": snapshot.folder_name,
        "computed_at": _dt.datetime.now(_dt.timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z"),
        "findings": list(findings),
        "items_total": items_total,
        "items_executed": items_executed,
        "items_unexecuted": items_unexecuted,
        "executions": _roster_rows(snapshot),
        "policy_items": [asdict(item) for item in snapshot.items],
        "tester_completion": completion_by_tester(snapshot),
    }
