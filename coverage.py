"""R15: per-folder coverage-gap detection.

R0-R14 fire per-execution and can't detect tests that NEVER ran.
R15 fires per-folder via three sub-rules:
  R15a planned-but-not-executed (NotExecuted/InProgress/Blocked)
  R15b marketplace coverage hole (ran in some MPs, missing in others)
  R15c tester abandonment (>=N assigned, <60% executed)

Pure-data layer: operates on data argus._run_folder_list already
gathered (testrun items + status map + user cache); no Tracker calls.
Findings match workflow_rules shape, plus rule="R15a"|"R15b"|"R15c".
Persisted as <folder>/_coverage.json (per-folder, not per-audit).
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Protocol


# Statuses we count as "successfully executed" for R15b's marketplace-
# coverage check — i.e., the tester ran the test in this MP, even if
# they reported issues. The narrow original set {Pass, Fail} produced
# a false positive on QA-T16594/ES (Passed With Issue twice on
# ES counted as "missing on ES"); broaden to include any terminal
# test-management status the tester actually reached.
#
# Excluded by design (these ARE gap candidates):
#   Not Executed / In Progress  — tester never reached a verdict
#   Blocked                     — tester tried but couldn't run
#
# Included:
#   Pass                       — straight-line success
#   Fail                       — terminal failure (still executed)
#   Passed With Issue          — completed with caveats; tester engaged
#   Pass With Restrictions     — variant that means "executed"
# Aligns with the principle that R15b answers "was the MP covered at
# all?" not "did the MP pass?". Pass-rate is a different question.
_EXECUTED_STATUS_NAMES = frozenset({
    "Pass",
    "Fail",
    "Passed With Issue",
    "PassedWithIssue",
    "Pass With Restrictions",
    "PassWithRestrictions",
})

# Statuses that R15a fires on. Blocked is included because a Blocked
# item in a folder context typically means "tester didn't even attempt
# it" — test-management's Blocked status is the catch-all for "this couldn't be
# run for any reason". If the same TC was Blocked in the previous
# folder too, we escalate severity (operator pattern: same Blocked TC
# appearing folder after folder is a real workflow problem).
_R15A_STATUS_NAMES = frozenset({
    "Not Executed",
    "NotExecuted",
    "In Progress",
    "InProgress",
    "Blocked",
    # Not Applicable used to be in this set, but N/A executions are
    # now surfaced as synthetic audits (variant=not-applicable) in
    # the MAIN audit table via R18 + argus._run_folder_list's N/A
    # processing block. Keeping them in R15a as well would double-
    # count: each N/A would appear once in the audit table AND once
    # in the coverage drawer. R15a stays focused on items that have
    # NEVER produced an executable testresult — Not Executed,
    # In Progress, Blocked.
})

# R15c policy thresholds. Defaults chosen so a tester with <5 items
# assigned doesn't fire even at 0% executed (small samples are noisy);
# 60% is the project's stated "active" threshold.
_DEFAULT_ABANDONMENT_THRESHOLD = 0.6
_DEFAULT_MIN_ASSIGNED_FOR_ABANDONMENT = 5


# A protocol so coverage.py doesn't hard-import history_index — keeps
# tests free to pass a stub object that just exposes get_history.
class _HasGetHistory(Protocol):
    def get_history(self, tc_key: str, mp: str | None) -> list: ...


@dataclass
class CoverageItem:
    """One row from a test-management testrun's items list, normalised.

    Built once during folder enumeration (extractor.build_coverage_items)
    and consumed by compute_gaps. Includes BOTH executed and unexecuted
    items — R15a needs the unexecuted ones, R15b needs both, R15c needs
    the assignment data on every row.
    """
    test_case_key: str           # e.g. "QA-T44880"
    testrun_key: str             # e.g. "QA-C17363"
    marketplace: str | None      # parsed from testrun name; None when not encoded
    status: str                  # resolved name: "Pass" / "Not Executed" / etc
    assigned_to: str | None = None
    assigned_to_name: str | None = None
    testrun_name: str | None = None  # e.g. "E2E Flow - GMB - IT Desktop - Edge"
    test_case_name: str | None = None  # e.g. "Verify ALOP purchase ..."
    # The PLANNED owner (assigned_to) vs the person who ACTUALLY ran it
    # (executed_by) are different facts. assigned_to drives R15a/R15c
    # ("assigned but never executed" / abandonment); executed_by is the
    # source of truth for the per-tester execution roster — only an
    # executed result carries it (test-management's userKey), so it's None for
    # Not-Executed items. Never substitute one for the other: a case can
    # be assigned to X and executed by Y.
    executed_by: str | None = None
    executed_by_name: str | None = None
    e_key: str | None = None     # the E-key if executed; None otherwise


@dataclass
class CoverageSnapshot:
    """All CoverageItems for one folder, plus the folder name.

    Folder name is the slugified subdir — same string argus uses to scope
    the output dir, so the on-disk artefact's folder field matches the
    location it lives in.
    """
    folder_name: str
    items: list[CoverageItem] = field(default_factory=list)


# Finding constructors
def _finding(
    *,
    severity: str,
    rule: str,
    description: str,
    test_case_key: str | None = None,
    marketplace: str | None = None,
    tester: str | None = None,
) -> dict[str, Any]:
    """R15 finding dict, shape-compatible with workflow_rules._finding.

    `page` and `step_index` are always None — folder-level findings have
    no page/step locator. The extra context fields (test_case_key /
    marketplace / tester) are R15-specific so the dashboard renderer can
    present them in tables without re-parsing the description.
    """
    return {
        "severity": severity,
        "page": None,
        "step_index": None,
        "description": description,
        "source": "rule",
        "rule": rule,
        # R15-only context. Other rules don't carry these — the report
        # renderer must tolerate missing keys.
        "test_case_key": test_case_key,
        "marketplace": marketplace,
        "tester": tester,
    }


# R15a: planned-but-not-executed
def _check_r15a(
    snapshot: CoverageSnapshot,
    history_idx: _HasGetHistory | None,
) -> list[dict[str, Any]]:
    """Fire one finding per (test_case, marketplace) item left unexecuted.

    Severity defaults to medium. Escalates to high when the same
    (test_case_key, marketplace) was Blocked in the previous folder too —
    a recurring Blocked is operator-actionable noise (suggests an env or
    test-data issue, not a tester misstep). We use history_index for the
    cross-folder check; if no index is provided (e.g. unit tests, or a
    fresh checkout), we just stay at medium.
    """
    findings: list[dict[str, Any]] = []
    for item in snapshot.items:
        if item.status not in _R15A_STATUS_NAMES:
            continue

        severity = "medium"
        # History escalation: the previous Blocked entry for THIS
        # (TC, MP) bumps us to high. We only check Blocked-flavoured
        # history because NotExecuted / InProgress in prior folders
        # don't usually persist in metadata (the items never produced
        # an E-key + audit, so they don't appear in the index).
        if history_idx is not None and item.status == "Blocked":
            try:
                history = history_idx.get_history(
                    item.test_case_key, item.marketplace)
            except Exception:
                # History index is best-effort; never let it break R15a.
                history = []
            # Defensive: history can be empty or contain entries with no
            # status. We escalate only when at least one prior entry is
            # also Blocked.
            for entry in history:
                prior_status = getattr(entry, "status", None)
                if prior_status == "Blocked":
                    severity = "high"
                    break

        mp_label = item.marketplace or "unknown MP"
        tester_label = item.assigned_to_name or item.assigned_to or "unassigned"
        description = (
            f"R15a: {item.test_case_key} in {mp_label} "
            f"(testrun {item.testrun_key}) is {item.status}; "
            f"assigned to {tester_label} but never executed."
        )
        findings.append(_finding(
            severity=severity,
            rule="R15a",
            description=description,
            test_case_key=item.test_case_key,
            marketplace=item.marketplace,
            tester=item.assigned_to_name or item.assigned_to,
        ))
    return findings


# R15b: marketplace coverage hole
def _check_r15b(snapshot: CoverageSnapshot) -> list[dict[str, Any]]:
    """Fire one finding per test case that ran somewhere but not everywhere.

    Logic:
      planned  = {mp for items of this TC where mp is not None}
      executed = {mp for items of this TC where status in {Pass, Fail}}
      missing  = planned - executed
    Fire only when len(executed) > 0 — a fully-skipped TC is R15a, not
    R15b. The "wholly unexecuted" case has no executed reference point
    so we cannot tell whether the missing MPs are real coverage or just
    "this test isn't ready". R15a catches that case per-item.
    """
    # Bucket items by test_case_key for one pass over the data.
    by_tc: dict[str, list[CoverageItem]] = {}
    for item in snapshot.items:
        # Skip items without a TC key — happens rarely with malformed
        # test-management data, never in normal use.
        if not item.test_case_key:
            continue
        by_tc.setdefault(item.test_case_key, []).append(item)

    findings: list[dict[str, Any]] = []
    for tc_key, items in by_tc.items():
        planned = {it.marketplace for it in items if it.marketplace is not None}
        executed = {
            it.marketplace for it in items
            if it.status in _EXECUTED_STATUS_NAMES
            and it.marketplace is not None
        }
        missing = planned - executed
        # Both gates — gap exists AND we have a reference execution.
        if not missing or not executed:
            continue
        # Stable, alphabetised list for deterministic output.
        missing_sorted = sorted(missing)
        executed_sorted = sorted(executed)
        description = (
            f"R15b: {tc_key} executed in "
            f"{', '.join(executed_sorted)} but missing in "
            f"{', '.join(missing_sorted)} (marketplace coverage hole)."
        )
        findings.append(_finding(
            severity="medium",
            rule="R15b",
            description=description,
            test_case_key=tc_key,
            # Marketplace field on R15b finding lists the *missing* MPs
            # joined — the renderer can split if it wants individual
            # rows. Single string keeps the schema dict-flat.
            marketplace=",".join(missing_sorted),
        ))
    return findings


# R15c: tester abandonment
def _check_r15c(
    snapshot: CoverageSnapshot,
    *,
    abandonment_threshold: float,
    min_assigned_for_abandonment: int,
) -> list[dict[str, Any]]:
    """Fire one finding per tester whose execution rate is below threshold.

    Bucketing key is `assigned_to_name` (display name) when resolvable,
    falling back to `assigned_to` (raw userKey) so we don't merge two
    distinct testers who both happened to be unresolved at the moment.
    Items with no assignee are ignored — there's no one to attribute the
    abandonment to.
    """
    if min_assigned_for_abandonment < 1:
        # Defensive: a 0 threshold would fire on every empty bucket.
        min_assigned_for_abandonment = 1

    by_tester: dict[str, list[CoverageItem]] = {}
    for item in snapshot.items:
        bucket = item.assigned_to_name or item.assigned_to
        if not bucket:
            continue
        by_tester.setdefault(bucket, []).append(item)

    findings: list[dict[str, Any]] = []
    for tester, items in by_tester.items():
        assigned = len(items)
        if assigned < min_assigned_for_abandonment:
            continue
        executed = sum(1 for it in items
                       if it.status in _EXECUTED_STATUS_NAMES)
        if assigned <= 0:
            continue
        rate = executed / assigned
        if rate >= abandonment_threshold:
            continue
        # Render rate as an integer percentage to keep description stable.
        pct = int(round(rate * 100))
        description = (
            f"R15c: {tester} assigned {assigned} item(s) but executed "
            f"only {executed} ({pct}% < "
            f"{int(abandonment_threshold * 100)}% threshold)."
        )
        findings.append(_finding(
            severity="medium",
            rule="R15c",
            description=description,
            tester=tester,
        ))
    return findings


# Public API
def compute_gaps(
    snapshot: CoverageSnapshot,
    *,
    history_idx: _HasGetHistory | None = None,
    abandonment_threshold: float = _DEFAULT_ABANDONMENT_THRESHOLD,
    min_assigned_for_abandonment: int = _DEFAULT_MIN_ASSIGNED_FOR_ABANDONMENT,
) -> list[dict[str, Any]]:
    """Run R15a + R15b + R15c and return all findings.

    Order of returned findings: R15a items first (one per skipped item),
    then R15b (one per TC with a hole), then R15c (one per behind tester).
    Within each rule the order is stable but not specified — callers that
    need a particular order should sort by description or test_case_key.
    """
    out: list[dict[str, Any]] = []
    out.extend(_check_r15a(snapshot, history_idx))
    out.extend(_check_r15b(snapshot))
    out.extend(_check_r15c(
        snapshot,
        abandonment_threshold=abandonment_threshold,
        min_assigned_for_abandonment=min_assigned_for_abandonment,
    ))
    return out


def _roster_rows(snapshot: CoverageSnapshot) -> list[dict[str, Any]]:
    """Per-execution roster: one row for every item someone ACTUALLY ran.

    Source of truth is `executed_by` (test-management userKey on the executed
    result), NOT `assigned_to` — a case assigned to X but executed by Y
    is credited to Y. Items nobody executed have `executed_by is None`
    and are omitted here; they're already covered by R15a in `findings`.

    `assigned_to`/`assigned_to_name` are carried as a REFERENCE so the
    renderer can surface assignee≠executor divergence (a manager signal),
    but they never decide inclusion. Rows with no test_case_key (rare
    malformed test-management data) are skipped, mirroring R15b's guard.
    """
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
    """Build the `_coverage.json` on-disk shape.

    Schema:
        {
          "schema": 1,
          "folder": <folder_name>,
          "computed_at": ISO-8601 UTC,
          "findings": [...],
          "items_total": int,
          "items_executed": int,
          "items_unexecuted": int,
          "executions": [ {test_case_key, testrun_key, marketplace,
                           status, executed_by, executed_by_name,
                           assigned_to, assigned_to_name}, ... ]
        }

    `items_unexecuted` is items_total - items_executed (no overlap;
    "executed" means Pass/Fail, "unexecuted" is everything else
    including Blocked / InProgress / NotExecuted). Reporting both
    headline numbers up-front lets the dashboard render the
    'X items, Y executed, Z unexecuted (W%)' line without re-walking
    the items list.

    `executions` (schema-additive; old readers ignore it) is the
    per-tester roster keyed on the real executor — see _roster_rows.
    Note it can be LARGER than items_executed: the headline counter
    only counts Pass/Fail, while the roster credits every status a
    tester actually ran (Passed With Issue, Blocked, In Progress, ...).
    """
    items_total = len(snapshot.items)
    items_executed = sum(
        1 for it in snapshot.items if it.status in _EXECUTED_STATUS_NAMES
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
    }
