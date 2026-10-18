"""Run identity, counts, scope, and exports use the same code for every team."""
from dataclasses import replace
from html.parser import HTMLParser
import json
from pathlib import Path

import pytest

from argus import config
from argus.reporting import html_report, report, ui


def audit(tmp_path: Path, key, run_key, name="Same run title", **overrides):
    return report.AuditSummary(
        key=key, path=tmp_path / key, verdict="pass", findings_count=0,
        severity_counts={}, rules_fired=[], has_high_finding=False,
        schema_version=report._CURRENT_SCHEMA_VERSION,
        tester="Tester", tester_status="Pass", test_case="EXAMPLE-T1 — Test case",
        testrun_key=run_key, testrun_name=name,
        **overrides,
    )


def roster_row(key, run_key, name="Same run title"):
    return {
        "e_key": key, "testrun_key": run_key, "testrun_name": name,
        "test_case_key": "EXAMPLE-T1", "test_case_name": "Test case",
        "executed_by_name": "Tester", "status": "Pass",
    }


def coverage(tmp_path, rows):
    (tmp_path / "_coverage.json").write_text(json.dumps({
        "schema": 1, "executions": rows, "findings": [],
        "items_total": len(rows), "items_executed": len(rows),
    }))


class Capture(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.rows = []
        self.headings = []
        self.headers = []
        self.run_options = []
        self.day_options = []
        self.day_headings = []
        self.in_day_select = False
        self.in_run_select = False
        self.run_cards = []
        self.in_run_drawer = False
        self.date_inputs = []
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("id", "").startswith(
                ("argus-exec-", "argus-audit-")):
            self.date_inputs.append(attrs["id"])
        if tag == "select" and attrs.get("id") == "argus-run":
            self.in_run_select = True
        if tag == "select" and attrs.get("id") == "argus-day":
            self.in_day_select = True
        if tag == "option" and self.in_run_select:
            self.run_options.append(attrs["value"])
        if tag == "option" and self.in_day_select:
            self.day_options.append(attrs["value"])
        if attrs.get("id") == "argus-export-csv":
            self.headers = json.loads(attrs["data-csv-headers"])
        if tag == "dialog" and attrs.get("id") == "argus-runs-drawer":
            assert "open" not in attrs
            self.in_run_drawer = True
        if "data-run-summary" in attrs:
            assert self.in_run_drawer
            self.run_cards.append(attrs["data-run-summary"])
        if tag != "tr":
            return
        classes = attrs.get("class", "").split()
        if "argus-row" in classes:
            self.rows.append(attrs)
        if "argus-run-heading" in classes:
            self.headings.append(attrs["data-run"])
        if "argus-day-heading" in classes:
            self.day_headings.append((attrs["data-run"], attrs["data-day"]))

    def handle_endtag(self, tag):
        if tag == "select":
            self.in_run_select = False
            self.in_day_select = False
        if tag == "dialog":
            self.in_run_drawer = False


@pytest.mark.parametrize("profile,prefix", [
    ("web", "QA"), ("desktop", "QA"), ("tablet", "QA"),
    ("mobile", "MOBILE"), ("reader", "QA"), ("tv", "QA"),
    ("portal", "PORTAL"),
])
def test_every_team_groups_and_exports_by_run_key(tmp_path, profile, prefix):
    settings = config.load(profile=profile)
    run_a, run_b = f"{prefix}-C1", f"{prefix}-C2"
    key_a, key_b, key_c = (f"{prefix}-E{i}" for i in (1, 2, 3))
    summaries = [audit(tmp_path, key_a, run_a), audit(tmp_path, key_b, run_b)]
    coverage(tmp_path, [roster_row(key_c, run_a)])

    bundle = report.generate_html_report_bundle(
        tmp_path, summaries=summaries, ui_context=ui.from_settings(settings))
    parsed = Capture(bundle.canonical_html)
    assert parsed.headings == [run_a, run_b]
    assert parsed.run_options == ["", run_a, run_b]
    assert parsed.run_cards == [run_a, run_b]
    assert [(r["data-key"], r["data-run"]) for r in parsed.rows] == [
        (key_a, run_a), (key_c, run_a), (key_b, run_b),
    ]
    for row in parsed.rows:
        exported = dict(zip(parsed.headers, json.loads(row["data-csv"]), strict=True))
        assert exported["Execution key"] == row["data-key"]
        assert exported["Test run key"] == row["data-run"]
    assert set(bundle.detail_chunks["0000.json"]) == {"0", "2"}
    assert key_a in bundle.detail_chunks["0000.json"]["0"]
    assert key_b in bundle.detail_chunks["0000.json"]["2"]
    assert Capture(bundle.archive_html).rows == parsed.rows
    assert settings.general.team_name in bundle.canonical_html
    assert bool(parsed.day_headings) == (profile == "tablet")



@pytest.mark.parametrize("profile", ["web", "desktop", "tablet", "mobile"])
def test_date_range_pickers_are_universal_and_audit_day_is_utc(tmp_path, profile):
    settings = config.load(profile=profile)
    prefix = "MOBILE" if profile == "mobile" else "QA"
    run = f"{prefix}-C1"
    summaries = [
        # Execution near midnight IST + audit late-UTC: proves the two
        # dimensions are computed on different clocks and stay independent.
        audit(tmp_path, f"{prefix}-E1", run,
              execution_date="2026-09-21T00:30:00+05:30",
              audited_at="2026-09-23T23:30:00Z"),
        audit(tmp_path, f"{prefix}-E2", run,
              execution_date="2026-09-22T09:00:00+05:30",
              audited_at="2026-09-24T02:15:00Z"),
        # An audit with no audited_at must carry an empty audit-date cell,
        # never crash and never fall into a range.
        replace(audit(tmp_path, f"{prefix}-E3", run,
                      execution_date="2026-09-22T09:00:00+05:30"),
                audited_at=None),
    ]
    bundle = report.generate_html_report_bundle(
        tmp_path, summaries=summaries, ui_context=ui.from_settings(settings))
    parsed = Capture(bundle.canonical_html)

    # Both custom ranges (four inputs) render for every team...
    assert parsed.date_inputs == [
        "argus-exec-from", "argus-exec-to", "argus-audit-from", "argus-audit-to",
    ]
    # ...while the discrete execution-date dropdown stays Tablet-only, and day
    # grouping (headings) stays Tablet-only — neither regressed.
    assert bool(parsed.day_options) == (profile == "tablet")
    assert bool(parsed.day_headings) == (profile == "tablet")

    by_key = {r["data-key"]: r for r in parsed.rows}
    # Audit date is the UTC calendar day of audited_at (no local shift):
    # 2026-09-23T23:30Z stays 2026-09-23.
    assert by_key[f"{prefix}-E1"]["data-audited"] == "2026-09-23"
    assert by_key[f"{prefix}-E2"]["data-audited"] == "2026-09-24"
    # Missing audited_at -> empty audit-date cell (excluded from audit ranges).
    assert by_key[f"{prefix}-E3"]["data-audited"] == ""
    # Execution date keeps its Jira calendar semantics, independent of audit day.
    assert by_key[f"{prefix}-E1"]["data-day"] == "2026-09-21"
    assert by_key[f"{prefix}-E2"]["data-day"] == "2026-09-22"


def test_run_counts_deduplicate_and_keep_failed_analysis_out_of_deviations(tmp_path):
    passing = audit(tmp_path, "EXAMPLE-E1", "EXAMPLE-C1")
    deviation = replace(audit(tmp_path, "EXAMPLE-E2", "EXAMPLE-C1"),
                        verdict="fail", findings_count=2)
    incomplete = replace(
        audit(tmp_path, "EXAMPLE-E3", None, name=None),
        analysis_status="incomplete", verdict="fail", findings_count=5,
        analysis_diagnostic="model: request failed",
    )
    old = audit(tmp_path, "EXAMPLE-E5", "EXAMPLE-C2",
                execution_date="2026-09-09")
    newer = replace(old, execution_date="2026-09-10", verdict="concerns")
    rows = [
        roster_row("EXAMPLE-E1", "EXAMPLE-C1"),
        roster_row("EXAMPLE-E3", "EXAMPLE-C1"),
        roster_row("EXAMPLE-E4", "EXAMPLE-C1"),
        roster_row("EXAMPLE-E4", "EXAMPLE-C1"),
    ]
    first, second = html_report._group_report_runs(
        [passing, deviation, incomplete, newer, old], rows)
    assert first.counts == {
        "executions": 4, "completed": 2, "incomplete": 1,
        "unaudited": 1, "deviations": 1, "findings": 2,
    }
    assert first.audits[2].testrun_key == "EXAMPLE-C1"
    assert incomplete.testrun_key is None  # Report enrichment is not a persisted mutation.
    assert second.audits == [newer]
    assert second.counts["executions"] == 1


def test_unaudited_only_run_is_visible_and_exportable(tmp_path):
    row = roster_row("EXAMPLE-E1", "EXAMPLE-C1", 'A "quoted", run\nsecond line')
    coverage(tmp_path, [row, row])
    page = report.generate_html_report(tmp_path)
    parsed = Capture(page)
    assert len(parsed.rows) == 1
    assert "No audits found" not in page
    values = dict(zip(parsed.headers, json.loads(parsed.rows[0]["data-csv"]), strict=True))
    assert values["Verdict"] == "Not audited"
    assert values["Analysis status"] == "not_audited"
    assert values["Findings count"] == ""
    assert values["Test run"] == row["testrun_name"]


def test_missing_run_is_explicit_and_run_labels_are_escaped(tmp_path):
    dangerous = '<img src=x onerror="alert(1)">'
    summaries = [
        audit(tmp_path, "EXAMPLE-E1", "EXAMPLE-C1", dangerous),
        audit(tmp_path, "EXAMPLE-E2", None, "Unidentified run"),
    ]
    page = report.generate_html_report(tmp_path, summaries=summaries)
    parsed = Capture(page)
    assert parsed.headings == ["EXAMPLE-C1", "_unknown"]
    assert "Run not recorded" in page
    assert dangerous not in page
    assert "&lt;img" in page


def test_run_grouping_stays_inside_the_current_report_scope(tmp_path):
    folder = tmp_path / "Plan"
    folder.mkdir()
    for key, run in (("EXAMPLE-E1", "EXAMPLE-C1"), ("EXAMPLE-E2", "EXAMPLE-C2")):
        execution = folder / key
        execution.mkdir()
        (execution / "audit.json").write_text(json.dumps({
            "overall_verdict": "pass", "findings": [],
        }))
        (execution / "metadata.json").write_text(json.dumps({
            "test_results": [{"testrun_key": run, "testrun_name": "Same title"}],
        }))
    (folder / "_latest_run.json").write_text(json.dumps({
        "version": 1, "folder_name": "Plan",
        "started_at": "2026-09-11T00:00:00Z",
        "completed_at": "2026-09-11T00:01:00Z",
        "scope_keys": ["EXAMPLE-E1"], "selected_keys": ["EXAMPLE-E1"],
        "audit_keys": ["EXAMPLE-E1"], "failed_keys": [], "statuses": {},
    }))
    parsed = Capture(report.generate_html_report(folder))
    assert parsed.headings == ["EXAMPLE-C1"]
    assert [row["data-key"] for row in parsed.rows] == ["EXAMPLE-E1"]


def test_csv_retains_incomplete_diagnostic(tmp_path):
    summary = replace(
        audit(tmp_path, "EXAMPLE-E1", "EXAMPLE-C1"),
        analysis_status="incomplete",
        analysis_diagnostic='extraction: attachment "42" failed',
    )
    parsed = Capture(report.generate_html_report(tmp_path, summaries=[summary]))
    values = dict(zip(parsed.headers, json.loads(parsed.rows[0]["data-csv"]), strict=True))
    assert values["Analysis diagnostic"] == summary.analysis_diagnostic


def test_csv_resolves_the_same_tester_on_audited_and_unaudited_rows(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from argus.core import users

    cache = Mock()
    cache.get_resolved.return_value = "Example Tester"
    monkeypatch.setattr(users, "get_default_cache", lambda: cache)
    tester_id = "JIRAUSER12345"
    summary = replace(audit(tmp_path, "EXAMPLE-E1", "EXAMPLE-C1"), tester=tester_id)
    coverage(tmp_path, [{
        **roster_row("EXAMPLE-E2", "EXAMPLE-C1"),
        "executed_by_name": None, "executed_by": tester_id,
    }])
    parsed = Capture(report.generate_html_report(tmp_path, summaries=[summary]))
    exported = [
        dict(zip(parsed.headers, json.loads(row["data-csv"]), strict=True))
        for row in parsed.rows
    ]
    assert len(exported) == 2
    assert {row["Tester"] for row in exported} == {"Example Tester"}


@pytest.mark.parametrize(("timestamp", "expected"), [
    ("2026-09-21T00:15:00+05:30", "2026-09-21"),
    ("2026-09-20T23:15:00-07:00", "2026-09-20"),
    ("2026-09-21T14:10:12.000Z", "2026-09-21"),
    ("2026-09-21", "2026-09-21"),
    (None, "_unknown"), ("", "_unknown"), ("2026-02-30", "_unknown"),
    ('<img src=x onerror="alert(1)">', "_unknown"),
])
def test_execution_day_uses_recorded_calendar_date(timestamp, expected):
    assert html_report._execution_day(timestamp) == expected


def test_tablet_separates_days_inside_each_run_and_keeps_unaudited_rows(tmp_path):
    summaries = [
        audit(tmp_path, "QA-E1", "QA-C1", execution_date="2026-09-20T23:30:00-07:00"),
        audit(tmp_path, "QA-E2", "QA-C1", execution_date="2026-09-21T00:30:00+05:30"),
        audit(tmp_path, "QA-E3", "QA-C2", execution_date="2026-09-20"),
        replace(audit(tmp_path, "QA-E4", "QA-C1"), analysis_status="incomplete"),
        audit(tmp_path, "QA-E5", "QA-C1"),
    ]
    coverage(tmp_path, [
        {**roster_row("QA-E4", "QA-C1"), "execution_date": "2026-09-21"},
        {**roster_row("QA-E6", "QA-C1"), "execution_date": "2026-09-20"},
    ])
    bundle = report.generate_html_report_bundle(
        tmp_path, summaries=summaries, ui_context=ui.from_settings(config.load(profile="tablet")),
    )
    parsed = Capture(bundle.canonical_html)
    assert parsed.headings == ["QA-C1", "QA-C2"]
    assert parsed.run_options == ["", "QA-C1", "QA-C2"]
    assert parsed.day_options == ["", "2026-09-21", "2026-09-20", "_unknown"]
    assert parsed.day_headings == [
        ("QA-C1", "2026-09-21"), ("QA-C1", "2026-09-20"),
        ("QA-C1", "_unknown"), ("QA-C2", "2026-09-20"),
    ]
    assert [(row["data-key"], row["data-day"]) for row in parsed.rows] == [
        ("QA-E2", "2026-09-21"), ("QA-E4", "2026-09-21"),
        ("QA-E1", "2026-09-20"), ("QA-E6", "2026-09-20"),
        ("QA-E5", "_unknown"), ("QA-E3", "2026-09-20"),
    ]
    for row in parsed.rows:
        exported = dict(zip(parsed.headers, json.loads(row["data-csv"]), strict=True))
        assert exported["Test run key"] == row["data-run"]
        assert html_report._execution_day(exported["Execution date"]) == row["data-day"]
    assert "Execution date not recorded" in bundle.canonical_html
    assert "2 executions · 1 completed · 1 incomplete · 0 not audited · 0 deviations" in bundle.canonical_html
    assert Capture(bundle.archive_html).day_headings == parsed.day_headings
    for row in parsed.rows:
        if row["data-key"] != "QA-E6":
            assert row["data-key"] in bundle.detail_chunks["0000.json"][row["data-idx"]]
