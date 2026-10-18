"""Unit + light-integration tests for report.py."""
from __future__ import annotations

import json
from pathlib import Path

from argus.reporting import html_report, report


def test_csv_export_embeds_full_values_without_html_injection(tmp_path):
    from html.parser import HTMLParser

    description = 'Missing "evidence", café\n</script><script>alert(1)</script>'
    summary = report.AuditSummary(
        key="MOBILE-E1", path=tmp_path, verdict="fail", findings_count=1,
        severity_counts={"high": 1}, rules_fired=["R12"], has_high_finding=True,
        schema_version=5, tester="Zoë", tester_status="Pass",
        test_case="MOBILE-T1 — Example", testrun_key="MOBILE-C1",
        testrun_name="A full run name", findings=[{"description": description}],
    )

    class Capture(HTMLParser):
        values = None
        headers = None

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if "data-csv" in attrs:
                self.values = json.loads(attrs["data-csv"])
            if attrs.get("id") == "argus-export-csv":
                self.headers = json.loads(attrs["data-csv-headers"])

    page = html_report._render_argus_html_report(tmp_path, [summary], detail_sink={})
    parser = Capture()
    parser.feed(page)
    assert len(parser.headers) == len(parser.values)
    record = dict(zip(parser.headers, parser.values, strict=True))
    assert record["Test run key"] == "MOBILE-C1"
    assert record["Findings"] == description
    assert record["High"] == 1
    assert record["Verdict"] == "Deviation"
    assert "</script><script>alert(1)</script>" not in page


def test_issue_label_prioritizes_review_test_data_over_diagnostic_tool():
    finding = {
        "description": (
            "The review contains copied Diagnostic tool instructions and is a "
            "test-data hygiene issue."
        ),
    }

    assert (
        html_report._argus_issue_label(finding)
        == "Inappropriate review test data"
    )


def test_issue_label_does_not_treat_marketplace_metadata_as_mismatch():
    finding = {
        "description": (
            "Test case QA-T44877 (marketplace=DE) was left "
            "'In Progress' by S Swetha."
        ),
        "category": "tester_error",
    }

    label = html_report._argus_issue_label(finding)

    assert label != "Marketplace / locale mismatch"
    assert label.startswith("Test case QA-T44877")


def test_issue_label_does_not_flag_valid_feature_preprod_host():
    finding = {
        "description": (
            "The feature-preprod.example.de host is visible, but the "
            "required final state is not evidenced."
        ),
        "category": "insufficient_evidence",
    }

    assert (
        html_report._argus_issue_label(finding)
        == "Expected verification not evidenced"
    )


def test_issue_label_keeps_environment_mismatch_for_environment_findings():
    finding = {
        "description": (
            "The screenshot shows the wrong marketplace storefront."
        ),
        "category": "environment_issue",
    }

    assert (
        html_report._argus_issue_label(finding)
        == "Marketplace / locale mismatch"
    )


def _write_audit(
    out_dir: Path,
    testrun: str,
    tester: str,
    key: str,
    *,
    verdict: str = "pass",
    findings: list[dict] | None = None,
    schema_version: int | None = report._CURRENT_SCHEMA_VERSION,
    tester_status: str | None = "Pass",
    test_case_key: str = "QA-T1",
    test_case_name: str = "Canned test",
) -> Path:
    """Seed one <out_dir>/<testrun>/<tester>/<key>/ with audit + metadata."""
    exec_dir = out_dir / testrun / tester / key
    exec_dir.mkdir(parents=True, exist_ok=True)
    audit: dict = {
        "overall_verdict": verdict,
        "summary": "test",
        "findings": findings or [],
    }
    if schema_version is not None:
        audit["schema_version"] = schema_version
    (exec_dir / "audit.json").write_text(json.dumps(audit))
    (exec_dir / "metadata.json").write_text(json.dumps({
        "test_results": [{
            "status": tester_status,
            "test_case_key": test_case_key,
            "test_case_name": test_case_name,
            "executed_by_name": tester.replace("_", " "),
            "execution_date": "2026-05-06T00:00:00Z",
            "steps": [],
        }]
    }))
    return exec_dir


def _rule_finding(rule: str, severity: str = "medium") -> dict:
    return {
        "severity": severity,
        "page": None,
        "step_index": None,
        "description": f"{rule} firing",
        "source": "rule",
        "rule": rule,
    }


def _model_finding(severity: str, page: int = 1) -> dict:
    return {
        "severity": severity,
        "page": page,
        "step_index": 1,
        "description": "model finding",
    }


def _write_coverage(out_dir: Path, *, findings: list[dict], total: int,
                    completed: int,
                    tester_completion: list[dict] | None = None,
                    executions: list[dict] | None = None) -> None:
    payload = {
        "schema": 1,
        "folder": out_dir.name,
        "computed_at": "2026-05-06T00:00:00Z",
        "findings": findings,
        "items_total": total,
        "items_executed": completed,
        "items_unexecuted": total - completed,
        "executions": executions or [],
    }
    if tester_completion is not None:
        payload["tester_completion"] = tester_completion
    (out_dir / "_coverage.json").write_text(json.dumps(payload))


def _latest_run_manifest(audit_keys: list[str]) -> dict:
    return {
        "version": 1,
        "folder_name": "Sprint 47",
        "started_at": "2026-09-08T12:00:00+00:00",
        "completed_at": "2026-09-08T12:05:00+00:00",
        "scope_keys": list(audit_keys),
        "selected_keys": list(audit_keys),
        "audit_keys": list(audit_keys),
        "failed_keys": [],
        "statuses": {key: "ok" for key in audit_keys},
    }


def test_scan_returns_empty_list_for_missing_dir(tmp_path: Path):
    assert report._scan_output_dir(tmp_path / "does-not-exist") == []


def test_scan_skips_unparseable_audit(tmp_path: Path):
    _write_audit(tmp_path, "run1", "alice", "E-1")
    bad_dir = tmp_path / "run1" / "alice" / "E-BAD"
    bad_dir.mkdir(parents=True)
    (bad_dir / "audit.json").write_text("{not json")
    summaries = report._scan_output_dir(tmp_path)
    assert len(summaries) == 1
    assert summaries[0].key == "E-1"


def test_scan_uses_latest_run_manifest_as_canonical_scope(tmp_path: Path):
    _write_audit(tmp_path, "run1", "alice", "E-1")
    _write_audit(tmp_path, "run1", "alice", "E-STALE")
    (tmp_path / "_latest_run.json").write_text(json.dumps(
        _latest_run_manifest(["E-1"])
    ))

    summaries = report._scan_output_dir(tmp_path)

    assert [summary.key for summary in summaries] == ["E-1"]


def test_scan_honors_valid_empty_latest_run_manifest(tmp_path: Path):
    _write_audit(tmp_path, "run1", "alice", "E-STALE")
    (tmp_path / "_latest_run.json").write_text(json.dumps(
        _latest_run_manifest([])
    ))

    assert report._scan_output_dir(tmp_path) == []


def test_manifest_keeps_scoped_failure_diagnostics_without_stale_results(
        tmp_path: Path):
    _write_audit(tmp_path, "run1", "alice", "OK-1")
    _write_audit(tmp_path, "run1", "alice", "STALE-1")
    for key in ("BAD-1", "OUTSIDE-1"):
        execution = _write_audit(tmp_path, "run1", "alice", key)
        (execution / "audit.json").write_text(json.dumps({
            "overall_verdict": "unknown",
            "findings": [],
            "analysis_modules": {
                "extraction": {
                    "status": "incomplete",
                    "diagnostic": "attachment download failed",
                },
            },
        }))
    manifest = _latest_run_manifest(["OK-1"])
    manifest["scope_keys"] += ["BAD-1", "STALE-1"]
    manifest["failed_keys"] = ["BAD-1", "STALE-1", "OUTSIDE-1"]
    (tmp_path / "_latest_run.json").write_text(json.dumps(manifest))

    summaries = report._scan_output_dir(tmp_path)
    assert {s.key for s in summaries} == {"OK-1", "BAD-1"}
    md = report.generate_report(tmp_path)
    html = report.generate_html_report(tmp_path)
    assert "**Analysis complete:** 1" in md
    assert "**Analysis incomplete (excluded from compliance totals):** 1" in md
    assert "**Verdicts:** Compliant=1" in md
    assert "attachment download failed" in html
    assert 'data-key="BAD-1"' in html
    assert 'data-analysis-status="incomplete"' in html
    assert 'data-key="STALE-1"' not in html
    assert 'data-key="OUTSIDE-1"' not in html


def test_scan_ignores_partial_latest_run_manifest(tmp_path: Path):
    _write_audit(tmp_path, "run1", "alice", "E-1")
    (tmp_path / "_latest_run.json").write_text(json.dumps({
        "version": 1,
        "audit_keys": [],
    }))

    summaries = report._scan_output_dir(tmp_path)

    assert [summary.key for summary in summaries] == ["E-1"]


def test_build_summary_pulls_both_auditor_and_tester_fields(tmp_path: Path):
    _write_audit(tmp_path, "run1", "alice", "E-1",
                 verdict="concerns",
                 findings=[_model_finding("medium"),
                           _rule_finding("R3")],
                 tester_status="Pass",
                 test_case_key="QA-T10",
                 test_case_name="homepage smoke")
    [s] = report._scan_output_dir(tmp_path)
    assert s.key == "E-1"
    assert s.verdict == "concerns"
    assert s.findings_count == 2
    assert s.severity_counts == {"medium": 2}
    assert s.rules_fired == ["R3"]
    assert s.has_high_finding is False
    assert s.tester == "alice"
    assert s.tester_status == "Pass"
    assert s.test_case == "QA-T10 — homepage smoke"
    assert s.schema_version == report._CURRENT_SCHEMA_VERSION


def test_build_summary_missing_metadata_leaves_tester_none(tmp_path: Path):
    exec_dir = tmp_path / "run1" / "alice" / "E-1"
    exec_dir.mkdir(parents=True)
    (exec_dir / "audit.json").write_text(json.dumps({
        "overall_verdict": "pass", "summary": "x", "findings": [],
        "schema_version": report._CURRENT_SCHEMA_VERSION,
    }))
    [s] = report._scan_output_dir(tmp_path)
    assert s.tester is None
    assert s.tester_status is None
    assert s.test_case is None


def test_build_summary_schema_version_missing_preserved_as_none(tmp_path: Path):
    _write_audit(tmp_path, "run1", "alice", "E-1", schema_version=None)
    [s] = report._scan_output_dir(tmp_path)
    assert s.schema_version is None


def test_build_summary_demotes_fail_when_enrichment_demotes_every_high(
    tmp_path: Path,
):
    _write_audit(
        tmp_path, "run1", "alice", "E-1",
        verdict="fail",
        findings=[{
            "severity": "high",
            "page": None,
            "step_index": 3,
            "description": (
                "The order summary screenshot shows a Visa card, not the "
                "cash payment the tester claims was used."
            ),
        }],
    )
    [s] = report._scan_output_dir(tmp_path)
    assert [f["severity"] for f in s.findings] == ["medium"]
    assert s.has_high_finding is False
    assert s.verdict == "concerns"
    assert report._is_flagged(s) is False


def test_build_summary_escalates_concerns_when_enrichment_escalates_a_medium(
    tmp_path: Path,
):
    _write_audit(
        tmp_path, "run1", "alice", "E-1",
        verdict="concerns",
        findings=[{
            "severity": "medium",
            "page": 4,
            "step_index": 2,
            "description": (
                "The screenshot contradicts the tester's claim that the "
                "membership was cancelled."
            ),
        }],
    )
    [s] = report._scan_output_dir(tmp_path)
    assert [f["severity"] for f in s.findings] == ["high"]
    assert s.has_high_finding is True
    assert s.verdict == "fail"
    assert report._is_flagged(s) is True


def test_build_summary_verdict_matches_write_path_verdict(tmp_path: Path):
    from argus.pipeline import auditor

    findings = [
        {
            "severity": "high",
            "page": None,
            "step_index": 3,
            "description": (
                "The library page screenshot shows no such title, "
                "so the purchase is not evidenced as claimed."
            ),
        },
        {
            "severity": "medium",
            "page": 2,
            "step_index": 1,
            "description": "Expected banner is not visible in the capture.",
        },
    ]
    exec_dir = _write_audit(tmp_path, "run1", "alice", "E-1",
                            verdict="fail", findings=findings)
    metadata = json.loads((exec_dir / "metadata.json").read_text())

    stored = json.loads((exec_dir / "audit.json").read_text())
    write_path = auditor._finalize_verdict(
        auditor.enrich_audit(stored, metadata))

    [s] = report._scan_output_dir(tmp_path)
    assert s.verdict == write_path["overall_verdict"]


def test_build_summary_leaves_already_consistent_verdict_untouched(
    tmp_path: Path,
):
    _write_audit(
        tmp_path, "run1", "alice", "E-1",
        verdict="fail",
        findings=[_model_finding("high", page=7)],
    )
    [s] = report._scan_output_dir(tmp_path)
    assert s.verdict == "fail"
    assert s.has_high_finding is True


def test_read_path_verdict_matches_write_path_across_severity_shapes(
    tmp_path: Path,
):
    from argus.pipeline import auditor

    cases = [
        (
            "un-anchored visual high demotes to medium",
            "fail",
            [{"severity": "high", "page": None, "step_index": 2,
              "description": "The library screen shows no such title."}],
        ),
        (
            "contradiction-worded medium escalates to high",
            "concerns",
            [{"severity": "medium", "page": 3, "step_index": 1,
              "description": "Page 3 contradicts the tester's claim."}],
        ),
        (
            "page-anchored high survives",
            "fail",
            [_model_finding("high", page=5)],
        ),
        (
            "evidence-gap medium stays medium",
            "concerns",
            [{"severity": "medium", "page": None, "step_index": 4,
              "description": "No screenshot was provided for this step."}],
        ),
        (
            "R20 status-rule high caps at concerns",
            "concerns",
            [_rule_finding("R20", severity="high")],
        ),
        (
            "R22 nudge alone leaves a clean pass",
            "pass",
            [_rule_finding("R22", severity="low")],
        ),
        (
            "no findings at all",
            "pass",
            [],
        ),
    ]

    for i, (label, verdict, findings) in enumerate(cases):
        exec_dir = _write_audit(
            tmp_path, "run1", "alice", f"E-{i}",
            verdict=verdict, findings=findings,
        )
        metadata = json.loads((exec_dir / "metadata.json").read_text())
        stored = json.loads((exec_dir / "audit.json").read_text())
        expected = auditor._finalize_verdict(
            auditor.enrich_audit(stored, metadata))["overall_verdict"]

        [s] = [x for x in report._scan_output_dir(tmp_path)
               if x.key == f"E-{i}"]
        assert s.verdict == expected, (
            f"{label}: read path gave {s.verdict!r}, "
            f"write path gives {expected!r}"
        )


def _stub_summary(**overrides) -> report.AuditSummary:
    """Minimal AuditSummary for direct-predicate tests."""
    defaults = dict(
        key="K", path=Path("/x"), verdict="concerns", findings_count=0,
        severity_counts={}, rules_fired=[], has_high_finding=False,
        schema_version=report._CURRENT_SCHEMA_VERSION,
        tester="a", tester_status="Pass", test_case=None, execution_date=None,
    )
    defaults.update(overrides)
    return report.AuditSummary(**defaults)


def test_is_flagged_fires_on_fail_verdict():
    assert report._is_flagged(_stub_summary(verdict="fail")) is True


def test_is_flagged_fires_on_high_finding():
    assert report._is_flagged(_stub_summary(
        severity_counts={"high": 1}, has_high_finding=True
    )) is True


def test_is_flagged_fires_on_structural_rule():
    for rule in ("R0", "R2", "R4", "R8", "R9", "R19"):
        s = _stub_summary(rules_fired=[rule])
        assert report._is_flagged(s) is True, (
            f"{rule} is structural and should flag"
        )


def test_is_flagged_silent_on_deprecated_r3_alone():
    assert report._is_flagged(_stub_summary(rules_fired=["R3"])) is False


def test_is_flagged_silent_on_mere_concerns_without_structural_rule():
    for rule in ("R3", "R5", "R7"):
        assert report._is_flagged(_stub_summary(rules_fired=[rule])) is False, (
            f"{rule} should not force-flag on its own"
        )


def test_is_flagged_silent_on_r9_susp():
    assert report._is_flagged(_stub_summary(rules_fired=["R9-susp"])) is False


def test_is_flagged_silent_on_retired_r9_hover():
    assert report._is_flagged(
        _stub_summary(rules_fired=["R9-hover"])
    ) is False


def test_is_flagged_returns_false_when_dismissed():
    s = _stub_summary(
        verdict="fail",
        severity_counts={"high": 2}, has_high_finding=True,
        rules_fired=["R0", "R4"],
        acknowledgments=[{
            "type": "dismissed",
            "by": "argus-test-user",
            "at": "2026-05-26T12:00:00Z",
            "reason": "false positive",
        }],
    )
    assert report._is_flagged(s) is False


def test_is_flagged_unaffected_by_unknown_ack_type():
    s = _stub_summary(
        verdict="fail",
        acknowledgments=[{"type": "other-type", "by": "argus-test-user"}],
    )
    assert report._is_flagged(s) is True


def test_is_divergent_only_when_tester_pass_and_auditor_not_pass():
    assert report._is_divergent(_stub_summary(
        tester_status="Pass", verdict="concerns")) is True
    assert report._is_divergent(_stub_summary(
        tester_status="Pass", verdict="fail")) is True
    assert report._is_divergent(_stub_summary(
        tester_status="Pass", verdict="pass")) is False
    assert report._is_divergent(_stub_summary(
        tester_status="Fail", verdict="fail")) is False
    assert report._is_divergent(_stub_summary(
        tester_status="Fail", verdict="concerns")) is False
    assert report._is_divergent(_stub_summary(
        tester_status=None, verdict="concerns")) is False


def test_is_stale_handles_missing_and_older_schema():
    assert report._is_stale(_stub_summary(schema_version=None)) is True
    assert report._is_stale(_stub_summary(schema_version=1)) is True
    assert report._is_stale(_stub_summary(
        schema_version=report._CURRENT_SCHEMA_VERSION)) is False


def test_pct_handles_zero_total():
    assert report._pct(0, 0) == 0
    assert report._pct(5, 0) == 0


def test_pct_rounds_to_int():
    assert report._pct(1, 3) == 33
    assert report._pct(2, 3) == 67
    assert report._pct(1, 2) == 50


def test_coverage_report_uses_structured_completion_copy(tmp_path: Path):
    _write_coverage(
        tmp_path,
        total=10,
        completed=4,
        findings=[{
            "severity": "medium",
            "page": None,
            "step_index": None,
            "description": "legacy text should not be parsed",
            "source": "rule",
            "rule": "R15c",
            "tester": "Alice",
            "assigned_count": 10,
            "executed_count": 4,
            "completion_pct": 40,
            "threshold_pct": 100,
        }],
    )

    md = report._render_coverage(tmp_path)

    assert "## Coverage" in md
    assert "### Tester completion" in md
    assert "R15c" not in md
    assert "| Alice | 4/10 completed (40%; target 100%) |" in md
    assert "Tester abandonment" not in md


def test_coverage_report_lists_all_testers_when_rollup_present(
        tmp_path: Path):
    _write_coverage(
        tmp_path,
        total=10,
        completed=9,
        findings=[{
            "severity": "medium",
            "page": None,
            "step_index": None,
            "description": "x",
            "source": "rule",
            "rule": "R15c",
            "tester": "Bob",
            "assigned_count": 5,
            "executed_count": 4,
            "completion_pct": 80,
            "threshold_pct": 100,
        }],
        tester_completion=[
            {"tester": "Ada", "assigned_count": 5,
             "executed_count": 5, "completion_pct": 100},
            {"tester": "Bob", "assigned_count": 5,
             "executed_count": 4, "completion_pct": 80},
        ],
    )

    md = report._render_coverage(tmp_path)

    assert "| Ada | 5/5 completed (100%) |" in md
    assert "| Bob | 4/5 completed (80%; target 100%) |" in md


def test_coverage_report_lists_attempted_but_not_completed(tmp_path: Path):
    _write_coverage(
        tmp_path,
        total=4,
        completed=2,
        findings=[{
            "severity": "medium", "page": None, "step_index": None,
            "description": "R15a: T9 in US is still Not Executed in C1; "
                           "owner: Zed.",
            "source": "rule", "rule": "R15a",
            "test_case_key": "T9", "marketplace": "US",
            "tester": "Zed", "testrun_key": "C1",
        }],
        executions=[
            {"test_case_key": "T1", "marketplace": "US", "status": "Pass",
             "executed_by": "u1", "executed_by_name": "Ada"},
            {"test_case_key": "T2", "marketplace": "US", "status": "Blocked",
             "executed_by": "u2", "executed_by_name": "Gaya"},
            {"test_case_key": "T3", "marketplace": "DE",
             "status": "In Progress",
             "executed_by": "u2", "executed_by_name": "Gaya"},
        ],
    )

    md = report._render_coverage(tmp_path)

    assert "### Attempted but not completed" in md
    assert ("- **Gaya** — 2 attempted but not completed: "
            "T2 (US: Blocked), T3 (DE: In Progress)" in md)
    assert "Ada" not in md


def test_coverage_report_normalizes_legacy_completion_copy(tmp_path: Path):
    _write_coverage(
        tmp_path,
        total=10,
        completed=4,
        findings=[{
            "severity": "medium",
            "page": None,
            "step_index": None,
            "description": (
                "R15c: Alice assigned 10 item(s) but executed only 4 "
                "(40% < 60% threshold)."
            ),
            "source": "rule",
            "rule": "R15c",
            "tester": "Alice",
        }],
    )

    md = report._render_coverage(tmp_path)

    assert "| Alice | 4/10 completed (40%; target 100%) |" in md
    assert "executed only" not in md


def test_coverage_report_groups_planned_items_by_run(tmp_path: Path):
    _write_coverage(
        tmp_path,
        total=3,
        completed=0,
        findings=[
            {
                "severity": "medium",
                "page": None,
                "step_index": None,
                "description": "new shape",
                "source": "rule",
                "rule": "R15a",
                "tester": "Alice",
                "test_case_key": "QA-T1",
                "marketplace": "US",
                "testrun_key": "QA-C9",
            },
            {
                "severity": "medium",
                "page": None,
                "step_index": None,
                "description": "same run",
                "source": "rule",
                "rule": "R15a",
                "tester": "Alice",
                "test_case_key": "QA-T3",
                "marketplace": "US",
                "testrun_key": "QA-C9",
            },
            {
                "severity": "medium",
                "page": None,
                "step_index": None,
                "description": (
                    "R15a: QA-T2 in UK (testrun QA-C10) "
                    "is Not Executed; assigned to Alice but never executed."
                ),
                "source": "rule",
                "rule": "R15a",
                "tester": "Alice",
                "test_case_key": "QA-T2",
                "marketplace": "UK",
            },
        ],
    )

    md = report._render_coverage(tmp_path)
    html = report._render_coverage_html(tmp_path)

    assert "QA-C9 (US: 2)" in md
    assert "QA-C10 (UK: 1)" in md
    assert "QA-T1" not in md
    assert "QA-T2" not in md
    assert "QA-T3" not in md
    assert "QA-C9" in html
    assert "QA-C10" in html
    assert '<span class="argus-mp-badge">US</span>' in html
    assert '<span class="argus-cov-chip-n">×2</span>' in html
    assert '<span class="argus-mp-badge">UK</span>' in html
    assert '<span class="argus-cov-chip-n">×1</span>' in html
    assert "QA-T1" not in html
    assert "QA-T2" not in html
    assert "QA-T3" not in html


def test_coverage_html_clean_state_uses_checked_copy(tmp_path: Path):
    _write_coverage(tmp_path, findings=[], total=3, completed=3)

    html = report._render_coverage_html(tmp_path)

    assert 'class="argus-coverage-trigger is-clean"' in html
    assert "Coverage" in html
    assert "Coverage gaps" not in html
    assert "Coverage checked" not in html
    assert "3 item(s) planned · 3 completed (100%)" in html


def test_coverage_trigger_is_in_report_header_actions(tmp_path: Path):
    _write_coverage(tmp_path, findings=[], total=3, completed=3)

    html = report.generate_html_report(tmp_path)

    assert (
        html.index('<main class="argus-main main-content">')
        < html.index('id="argus-coverage-open"')
        < html.index("No audits found in this folder.")
    )
    assert '<div class="cs-actions">' in html
    assert '<div class="argus-stat-label">Incomplete</div>' not in html


def test_empty_output_dir_produces_header_only_report(tmp_path: Path):
    md = report.generate_report(tmp_path)
    assert "Tester-compliance report" in md
    assert "no audits found" in md


def test_report_header_counts_match_seeded_corpus(tmp_path: Path):
    _write_audit(tmp_path, "run1", "alice", "E-1", verdict="pass")
    _write_audit(tmp_path, "run1", "alice", "E-2", verdict="concerns",
                 findings=[_model_finding("medium")])
    _write_audit(tmp_path, "run1", "bob", "E-3", verdict="concerns",
                 findings=[_model_finding("medium"), _rule_finding("R3")])
    md = report.generate_report(tmp_path)
    assert "Audits scanned:** 3" in md
    assert "Unique testers:** 2" in md
    assert "Compliant=1" in md
    assert "Concerns=2" in md


def test_divergence_sorts_worst_tester_first(tmp_path: Path):
    concern = [_model_finding("medium")]
    _write_audit(tmp_path, "run1", "alice", "A-1", verdict="concerns",
                 tester_status="Pass", findings=concern)
    _write_audit(tmp_path, "run1", "alice", "A-2", verdict="concerns",
                 tester_status="Pass", findings=concern)
    _write_audit(tmp_path, "run1", "alice", "A-3", verdict="pass",
                 tester_status="Pass")
    for k in ("B-1", "B-2", "B-3"):
        _write_audit(tmp_path, "run2", "bob", k, verdict="concerns",
                     tester_status="Pass", findings=concern)
    md = report.generate_report(tmp_path)
    divergence_section = md.split("## Tester-Pass vs Auditor-Concerns")[1] \
                           .split("\n## ")[0]
    bob_pos = divergence_section.index("bob")
    alice_pos = divergence_section.index("alice")
    assert bob_pos < alice_pos, (
        "divergence table must sort worst-rate first"
    )


def test_flagged_section_lists_high_severity_audits(tmp_path: Path):
    _write_audit(tmp_path, "run1", "alice", "CLEAN", verdict="concerns",
                 findings=[_model_finding("low")])
    _write_audit(tmp_path, "run1", "alice", "FLAGGED",
                 verdict="concerns",
                 findings=[_model_finding("high"), _model_finding("medium")])
    md = report.generate_report(tmp_path)
    flagged = md.split("## Flagged for review")[1].split("\n## ")[0]
    assert "FLAGGED" in flagged
    assert "CLEAN" not in flagged
    assert "high-severity finding" in flagged


def test_flagged_section_lists_structural_rule_fires(tmp_path: Path):
    _write_audit(tmp_path, "run1", "alice", "STRUCT", verdict="concerns",
                 findings=[_rule_finding("R4", "high")])
    md = report.generate_report(tmp_path)
    flagged = md.split("## Flagged for review")[1].split("\n## ")[0]
    assert "STRUCT" in flagged
    assert "R4" in flagged


def test_flagged_section_empty_message_when_nothing_flagged(tmp_path: Path):
    _write_audit(tmp_path, "run1", "alice", "K", verdict="pass")
    md = report.generate_report(tmp_path)
    assert "_None" in md.split("## Flagged for review")[1].split("\n## ")[0]


def test_rule_distribution_shows_only_fired_rules(tmp_path: Path):
    _write_audit(tmp_path, "run1", "alice", "K-1", verdict="concerns",
                 findings=[_rule_finding("R3"), _rule_finding("R3")])
    _write_audit(tmp_path, "run1", "alice", "K-2", verdict="concerns",
                 findings=[_rule_finding("R0")])
    md = report.generate_report(tmp_path)
    assert "## Workflow-rule firings" in md
    rule_section = md.split("## Workflow-rule firings")[1].split("\n## ")[0]
    assert "| R0 | 1 |" in rule_section
    assert "| R3 | 2 |" in rule_section
    assert "| R1 |" not in rule_section
    assert "| R2 |" not in rule_section


def test_rule_distribution_describes_every_emittable_rule(tmp_path: Path):
    """Verify every emitted rule has a report description."""
    emittable = ["R14a", "R14b", "R16", "R17", "R18", "R20", "R21"]
    _write_audit(tmp_path, "run1", "alice", "K-1", verdict="concerns",
                 findings=[_rule_finding(r) for r in emittable])
    md = report.generate_report(tmp_path)
    rule_section = md.split("## Workflow-rule firings")[1].split("\n## ")[0]
    assert "(unknown rule)" not in rule_section
    for r in emittable:
        assert f"| {r} | 1 |" in rule_section


def test_divergence_uses_status_role_not_literal():
    """Verify divergence uses the configured Pass status."""
    from argus.config.models import PolicySettings
    from argus.domain import workflow_rules as wr
    wr.configure(PolicySettings(statuses={"pass": "Passed"}))
    try:
        assert report._is_divergent(_stub_summary(
            tester_status="Passed", verdict="fail")) is True
        assert report._is_divergent(_stub_summary(
            tester_status="Passed", verdict="pass")) is False
    finally:
        wr.reset_configuration()


def test_rule_distribution_section_omitted_when_no_rules_fired(tmp_path: Path):
    _write_audit(tmp_path, "run1", "alice", "K", verdict="pass")
    md = report.generate_report(tmp_path)
    assert "## Workflow-rule firings" not in md


def test_stale_section_lists_audits_with_old_or_missing_schema(tmp_path: Path):
    _write_audit(tmp_path, "run1", "alice", "CURRENT",
                 schema_version=report._CURRENT_SCHEMA_VERSION)
    _write_audit(tmp_path, "run1", "alice", "OLD", schema_version=1)
    _write_audit(tmp_path, "run1", "alice", "MISSING", schema_version=None)
    md = report.generate_report(tmp_path)
    stale = md.split("## Stale audits")[1].split("\n## ")[0] if "## Stale" in md else ""
    assert "OLD" in stale
    assert "MISSING" in stale
    assert "CURRENT" not in stale


def test_stale_section_omitted_when_all_current(tmp_path: Path):
    _write_audit(tmp_path, "run1", "alice", "K",
                 schema_version=report._CURRENT_SCHEMA_VERSION)
    md = report.generate_report(tmp_path)
    assert "## Stale audits" not in md


def test_failed_keys_section_reads_last_error_json(tmp_path: Path):
    _write_audit(tmp_path, "run1", "alice", "OK-1")
    failed_dir = tmp_path / "run1" / "alice" / "FAIL-1"
    failed_dir.mkdir(parents=True)
    (failed_dir / "metadata.json").write_text("{}")
    (failed_dir / "_last_error.json").write_text(json.dumps({
        "exception_class": "ThrottlingException",
        "exception_message": "Rate exceeded",
        "traceback": "Traceback...",
    }))
    (tmp_path / "_failed_keys.txt").write_text("FAIL-1\nFAIL-GHOST\n")
    md = report.generate_report(tmp_path)
    failed = md.split("## Failed keys")[1] if "## Failed keys" in md else ""
    assert "FAIL-1" in failed
    assert "ThrottlingException" in failed
    assert "Rate exceeded" in failed
    assert "FAIL-GHOST" in failed
    assert "no _last_error.json found" in failed


def test_failed_keys_section_omitted_when_no_file(tmp_path: Path):
    _write_audit(tmp_path, "run1", "alice", "OK-1")
    md = report.generate_report(tmp_path)
    assert "## Failed keys" not in md


def test_per_tester_breakdown_has_one_section_per_tester(tmp_path: Path):
    _write_audit(tmp_path, "run1", "alice", "A-1", verdict="pass")
    _write_audit(tmp_path, "run1", "bob", "B-1", verdict="concerns",
                 findings=[_model_finding("medium")])
    md = report.generate_report(tmp_path)
    per_tester = md.split("## Per-tester audit summary")[1].split("\n## ")[0]
    assert "### alice" in per_tester
    assert "### bob" in per_tester
    assert per_tester.index("### alice") < per_tester.index("### bob")


def test_roster_matches_execution_despite_marketplace_difference(tmp_path: Path):
    execution = _write_audit(
        tmp_path, "run1", "alice", "PORTAL-E25643",
        test_case_key="PORTAL-T2550",
    )
    metadata_path = execution / "metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["test_results"][0]["testrun_key"] = "PORTAL-C656"
    metadata_path.write_text(json.dumps(metadata))
    roster = [{
        "e_key": "PORTAL-E25643",
        "test_case_key": "PORTAL-T2550",
        "testrun_key": "PORTAL-C656",
        "marketplace": "ES",
        "status": "Pass",
        "executed_by_name": "alice",
    }]
    _write_coverage(
        tmp_path, findings=[], total=1, completed=1, executions=roster)

    summaries = report._scan_output_dir(tmp_path)
    assert html_report._argus_unaudited_rows(summaries, roster, 1) == ([], set())
    [tester] = html_report._argus_per_tester_summary(summaries, roster)
    assert tester["audits"] == 1
    assert tester["not_audited"] == 0
    assert "audited 1, incomplete 0, not audited 0" in (
        report.generate_report(tmp_path))

    # A different execution of the same test case still needs its own audit.
    roster[0]["e_key"] = "PORTAL-E25644"
    roster[0]["marketplace"] = None
    rows, _ = html_report._argus_unaudited_rows(summaries, roster, 1)
    assert len(rows) == 1
    assert 'data-key="PORTAL-E25644"' in rows[0]
    [tester] = html_report._argus_per_tester_summary(summaries, roster)
    assert tester["not_audited"] == 1

    # Older coverage files without execution keys retain their existing join.
    roster[0].pop("e_key")
    assert html_report._argus_unaudited_rows(summaries, roster, 1) == ([], set())


def test_incomplete_audit_is_excluded_from_compliance_totals(tmp_path: Path):
    _write_audit(tmp_path, "run1", "alice", "OK-1", verdict="pass")
    incomplete_dir = _write_audit(
        tmp_path,
        "run1",
        "alice",
        "BAD-1",
        verdict="pass",
        findings=[_model_finding("high")],
    )
    audit_path = incomplete_dir / "audit.json"
    audit = json.loads(audit_path.read_text())
    audit["analysis_status"] = "incomplete"
    audit["analysis_modules"] = {
        "model": {
            "status": "incomplete",
            "diagnostic": "model response was truncated",
        },
    }
    audit_path.write_text(json.dumps(audit))

    md = report.generate_report(tmp_path)
    html = report.generate_html_report(tmp_path)

    assert "**Analysis complete:** 1" in md
    assert (
        "**Analysis incomplete (excluded from compliance totals):** 1"
        in md
    )
    assert "**Verdicts:** Compliant=1" in md
    assert "high=0" in md
    assert "- Audits complete: 1" in md
    assert "- Audits incomplete: 1" in md
    assert "model response was truncated" in html
    incomplete_row = html.split('data-key="BAD-1"', 1)[1].split(">", 1)[0]
    assert 'data-verdict="fail"' in incomplete_row
    assert 'data-analysis-status="incomplete"' in incomplete_row
    assert "IDX.analysis.get('incomplete')" in html
    assert "Completed audit results, findings, and evidence are shown below" in html
    assert "argus-verdict-fail" in html
    assert "INCOMPLETE" in html
    per_tester = html_report._argus_per_tester_summary(
        report._scan_output_dir(tmp_path)
    )
    assert per_tester[0]["audits"] == 1
    assert per_tester[0]["incomplete"] == 1
    assert per_tester[0]["verdicts"]["pass"] == 1
    assert per_tester[0]["high_finding_audits"] == 0


def test_main_writes_report_to_out_dir(tmp_path: Path, capsys):
    _write_audit(tmp_path, "run1", "alice", "K")
    rc = report.main(["--out-dir", str(tmp_path)])
    assert rc == 0
    reports = list(tmp_path.glob("_report_*.md"))
    assert len(reports) == 1
    assert (tmp_path / "_report.md").exists()
    err = capsys.readouterr().err
    assert "_report_" in err


def test_main_respects_no_report_file_flag(tmp_path: Path, capsys):
    _write_audit(tmp_path, "run1", "alice", "K")
    rc = report.main(["--out-dir", str(tmp_path), "--no-report-file",
                      "--stdout"])
    assert rc == 0
    assert list(tmp_path.glob("_report_*.md")) == []
    assert "Tester-compliance report" in capsys.readouterr().out


def test_main_stdout_and_file_both_work(tmp_path: Path, capsys):
    _write_audit(tmp_path, "run1", "alice", "K")
    rc = report.main(["--out-dir", str(tmp_path), "--stdout"])
    assert rc == 0
    captured = capsys.readouterr()
    reports = list(tmp_path.glob("_report_*.md"))
    assert len(reports) == 1
    assert (tmp_path / "_report.md").exists()
    assert "Tester-compliance report" in captured.out




def test_resolve_page_images_flat_and_sorted(tmp_path):
    exec_dir = tmp_path / "exec"
    shots = exec_dir / "screenshots"
    (shots / "Session_A").mkdir(parents=True)
    (shots / "Session_B").mkdir(parents=True)
    (shots / "Session_A" / "page_002.jpg").write_bytes(b"")
    (shots / "Session_A" / "page_001.jpg").write_bytes(b"")
    (shots / "Session_B" / "page_001.jpg").write_bytes(b"")

    pages = report._resolve_page_images(exec_dir)
    assert [p.name for p in pages] == [
        "page_001.jpg", "page_002.jpg",
        "page_001.jpg",
    ]
    assert pages[0].parent.name == "Session_A"
    assert pages[2].parent.name == "Session_B"


def test_resolve_page_images_skips_step_attachments(tmp_path):
    exec_dir = tmp_path / "exec"
    shots = exec_dir / "screenshots"
    (shots / "Session_A").mkdir(parents=True)
    (shots / "step_attachments").mkdir(parents=True)
    (shots / "Session_A" / "page_001.jpg").write_bytes(b"")
    (shots / "step_attachments" / "step_003_77_shot.jpg").write_bytes(b"")
    (shots / "step_attachments" / "page_999.jpg").write_bytes(b"")

    pages = report._resolve_page_images(exec_dir)
    assert len(pages) == 1
    assert pages[0].parent.name == "Session_A"


def test_resolve_page_images_empty_when_no_screenshots(tmp_path):
    exec_dir = tmp_path / "exec"
    exec_dir.mkdir()
    assert report._resolve_page_images(exec_dir) == []


def test_resolve_page_images_mixed_jpg_png(tmp_path):
    """Both .jpg and .png get picked up (legacy executions used PNG)."""
    exec_dir = tmp_path / "exec"
    shots = exec_dir / "screenshots" / "Session_A"
    shots.mkdir(parents=True)
    (shots / "page_001.png").write_bytes(b"")
    (shots / "page_002.jpg").write_bytes(b"")

    pages = report._resolve_page_images(exec_dir)
    assert [p.name for p in pages] == ["page_001.png", "page_002.jpg"]


def test_resolve_step_attachments_groups_by_step(tmp_path):
    exec_dir = tmp_path / "exec"
    step_dir = exec_dir / "screenshots" / "step_attachments"
    step_dir.mkdir(parents=True)
    (step_dir / "step_003_77_shot.jpg").write_bytes(b"")
    (step_dir / "step_003_78_shot.jpg").write_bytes(b"")
    (step_dir / "step_017_88_shot.png").write_bytes(b"")
    (step_dir / "top_99_shot.jpg").write_bytes(b"")
    (step_dir / "step_003_99_notes.pdf").write_bytes(b"")

    m = report._resolve_step_attachments(exec_dir)
    assert set(m) == {3, 17}
    assert len(m[3]) == 2
    assert len(m[17]) == 1
    for imgs in m.values():
        for p in imgs:
            assert p.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def test_resolve_step_attachments_empty_when_dir_missing(tmp_path):
    exec_dir = tmp_path / "exec"
    (exec_dir / "screenshots").mkdir(parents=True)
    assert report._resolve_step_attachments(exec_dir) == {}


def test_ordered_screenshot_images_includes_all_attachment_types(tmp_path):
    page = tmp_path / "page.jpg"
    top = tmp_path / "top.png"
    step_1 = tmp_path / "step_1.png"
    step_3 = tmp_path / "step_3.png"

    images = html_report._ordered_screenshot_images(
        [page],
        [top],
        {3: [step_3], 1: [step_1], 4: [page]},
    )

    assert images == [page, top, step_1, step_3]


def test_image_thumb_html_emits_relative_src(tmp_path):
    out_dir = tmp_path / "output"
    exec_dir = out_dir / "folder" / "tester" / "KEY"
    exec_dir.mkdir(parents=True)
    img = exec_dir / "screenshots" / "Session" / "page_001.jpg"
    img.parent.mkdir(parents=True)
    img.write_bytes(b"")

    html = report._image_thumb_html(img, out_dir, alt="page 1")
    assert 'src="folder/tester/KEY/screenshots/Session/page_001.jpg"' in html
    assert 'alt="page 1"' in html
    assert 'loading="lazy"' in html
    assert 'target="_blank"' in html


def test_image_thumb_html_out_of_tree_returns_empty(tmp_path):
    out_dir = tmp_path / "output"
    out_dir.mkdir()
    elsewhere = tmp_path / "unrelated" / "page.jpg"
    elsewhere.parent.mkdir()
    elsewhere.write_bytes(b"")
    assert report._image_thumb_html(elsewhere, out_dir, alt="x") == ""


def test_html_report_embeds_thumbnail_for_finding_with_page(tmp_path):
    out_dir = tmp_path / "output"
    exec_dir = out_dir / "fol" / "tester" / "KEY"
    exec_dir.mkdir(parents=True)
    shots = exec_dir / "screenshots" / "Session"
    shots.mkdir(parents=True)
    for name in ("page_001.jpg", "page_002.jpg", "page_003.jpg"):
        (shots / name).write_bytes(b"")
    audit = {
        "overall_verdict": "fail",
        "summary": "x",
        "findings": [{
            "severity": "high", "page": 2, "step_index": None,
            "description": "issue on page 2",
        }],
    }
    (exec_dir / "audit.json").write_text(json.dumps(audit))
    (exec_dir / "metadata.json").write_text(json.dumps({
        "test_results": [{"id": 1, "executed_by_name": "T", "steps": []}]
    }))

    html = report.generate_html_report(out_dir)
    page_1_src = 'src="fol/tester/KEY/screenshots/Session/page_001.jpg"'
    page_2_src = 'src="fol/tester/KEY/screenshots/Session/page_002.jpg"'
    page_3_src = 'src="fol/tester/KEY/screenshots/Session/page_003.jpg"'

    assert "<summary>View 3 screenshots</summary>" in html
    assert html.count(page_1_src) == 1
    assert html.count(page_2_src) == 2
    assert html.count(page_3_src) == 1


def test_html_report_shows_screenshot_button_for_step_only_clean_audit(tmp_path):
    out_dir = tmp_path / "output"
    exec_dir = _write_audit(out_dir, "fol", "tester", "KEY")
    step_dir = exec_dir / "screenshots" / "step_attachments"
    step_dir.mkdir(parents=True)
    (step_dir / "step_003_77_first.png").write_bytes(b"")
    (step_dir / "step_001_78_second.png").write_bytes(b"")

    html = report.generate_html_report(out_dir)

    assert "<summary>View 2 screenshots</summary>" in html
    assert (
        'src="fol/tester/KEY/screenshots/step_attachments/'
        'step_001_78_second.png"'
    ) in html
    assert (
        'src="fol/tester/KEY/screenshots/step_attachments/'
        'step_003_77_first.png"'
    ) in html


def test_html_report_links_xml_locator_screenshot_from_report_root(tmp_path):
    out_dir = tmp_path / "output"
    exec_dir = _write_audit(out_dir, "fol", "tester", "KEY")
    screenshot = (
        exec_dir / "screenshots" / "step_attachments" / "step_003_checkout.png"
    )
    screenshot.parent.mkdir(parents=True)
    screenshot.write_bytes(b"")
    (exec_dir / "xml_evidence.json").write_text(json.dumps({
        "nodes": [{
            "platform": "android",
            "step_index": 3,
            "element_type": "android.widget.Button",
            "resource_id": "checkout",
            "xpath": "//*[@resource-id='checkout']",
            "screenshot": "screenshots/step_attachments/step_003_checkout.png",
        }],
    }))

    html = report.generate_html_report(out_dir)

    assert "Locator evidence (1 nodes)" in html
    assert (
        'href="fol/tester/KEY/screenshots/step_attachments/'
        'step_003_checkout.png"'
    ) in html


def test_html_report_handles_out_of_range_page(tmp_path):
    out_dir = tmp_path / "output"
    exec_dir = out_dir / "fol" / "tester" / "KEY"
    exec_dir.mkdir(parents=True)
    (exec_dir / "screenshots" / "Session").mkdir(parents=True)
    (exec_dir / "screenshots" / "Session" / "page_001.jpg").write_bytes(b"")
    audit = {
        "overall_verdict": "fail",
        "summary": "x",
        "findings": [{
            "severity": "high", "page": 99, "step_index": None,
            "description": "bogus page",
        }],
    }
    (exec_dir / "audit.json").write_text(json.dumps(audit))
    (exec_dir / "metadata.json").write_text(json.dumps({
        "test_results": [{"id": 1, "executed_by_name": "T", "steps": []}]
    }))

    html = report.generate_html_report(out_dir)
    assert "thumb-missing" in html
    assert "image unavailable" in html
    assert "execution has 1 pages" in html


def test_plain_step_description_normalizes_zephyr_html():
    raw = (
        "<p>Click <strong>Listen in app</strong>.</p>"
        "<div>Confirm the native player opens.</div>"
        "<script>unique-script-text-must-not-render</script>"
    )

    assert report._plain_step_description(raw) == (
        "Click Listen in app. Confirm the native player opens."
    )


def test_finding_headline_prefers_meaningful_title_over_description():
    finding = {
        "title": "Example Storefront app does not show the expected resume prompt",
        "description": (
            "The step asks to play the audiobook in the Example Storefront app and "
            "verify the app shows a resume prompt with the expected title."
        ),
    }

    assert report._argus_finding_headline(finding) == (
        "Example Storefront app does not show the expected resume prompt"
    )


def test_html_report_adds_matching_test_step_to_finding_card(tmp_path):
    out_dir = tmp_path / "output"
    exec_dir = out_dir / "fol" / "tester" / "KEY"
    exec_dir.mkdir(parents=True)
    audit = {
        "overall_verdict": "concerns",
        "summary": "x",
        "findings": [{
            "severity": "medium",
            "page": None,
            "step_index": 3,
            "description": (
                "ARGUS could not find Chrome's connected-device page. "
                "Review the screenshots manually."
            ),
        }],
    }
    (exec_dir / "audit.json").write_text(json.dumps(audit))
    (exec_dir / "metadata.json").write_text(json.dumps({
        "test_results": [{
            "id": 1,
            "executed_by_name": "T",
            "steps": [{
                "index": 3,
                "description": (
                    "<p>Click <strong>Listen in app</strong> and confirm "
                    "the title opens in the Example Storefront app.</p>"
                ),
                "expected_result": "This expected result must stay hidden.",
            }],
        }]
    }))

    html = report.generate_html_report(out_dir)

    step_context = (
        '<div class="argus-finding-step">'
        '<span class="argus-finding-step-label">Test step:</span> '
        "Click Listen in app and confirm the title opens in the Example Storefront app."
        "</div>"
    )
    assert step_context in html
    step_position = html.index(step_context)
    body_position = html.index(
        "ARGUS could not find Chrome&#x27;s connected-device page. "
        "Review the screenshots manually.",
        step_position,
    )
    assert step_position < body_position
    assert "Step 4" in html
    assert "Step 3" not in html
    assert "This expected result must stay hidden." not in html


def test_html_report_omits_step_context_when_description_is_unavailable(tmp_path):
    out_dir = tmp_path / "output"
    exec_dir = out_dir / "fol" / "tester" / "KEY"
    exec_dir.mkdir(parents=True)
    (exec_dir / "audit.json").write_text(json.dumps({
        "overall_verdict": "concerns",
        "summary": "x",
        "findings": [{
            "severity": "medium",
            "step_index": 9,
            "description": "Review this finding.",
        }],
    }))
    (exec_dir / "metadata.json").write_text(json.dumps({
        "test_results": [{
            "id": 1,
            "executed_by_name": "T",
            "steps": [{"index": 9, "description": ""}],
        }]
    }))

    html = report.generate_html_report(out_dir)

    assert '<div class="argus-finding-step">' not in html
    assert "Review this finding." in html


def test_html_report_shows_related_blocked_steps_for_global_finding(tmp_path):
    out_dir = tmp_path / "output"
    exec_dir = out_dir / "fol" / "tester" / "KEY"
    exec_dir.mkdir(parents=True)
    (exec_dir / "audit.json").write_text(json.dumps({
        "overall_verdict": "concerns",
        "summary": "x",
        "findings": [{
            "severity": "medium",
            "step_index": None,
            "description": (
                "Two main objectives are marked Blocked, so the overall "
                "Pass result needs review."
            ),
        }],
    }))
    (exec_dir / "metadata.json").write_text(json.dumps({
        "test_results": [{
            "id": 1,
            "executed_by_name": "T",
            "steps": [
                {
                    "index": 0,
                    "description": "Redeem a code on the existing membership.",
                    "expected_result": "Must stay hidden.",
                    "status": "Pass",
                },
                {
                    "index": 1,
                    "description": "Switch upgrade from Monthly to Annual.",
                    "expected_result": "Must stay hidden.",
                    "status": "Blocked",
                },
                {
                    "index": 2,
                    "description": "Switch downgrade from Annual to Monthly.",
                    "expected_result": "Must stay hidden.",
                    "status": "Blocked",
                },
            ],
        }],
    }))

    html = report.generate_html_report(out_dir)

    assert "Steps 2, 3" in html
    assert "Affected test steps:" in html
    assert "Step 2 · Blocked:" in html
    assert "Switch upgrade from Monthly to Annual." in html
    assert "Step 3 · Blocked:" in html
    assert "Switch downgrade from Annual to Monthly." in html
    assert "Redeem a code on the existing membership." not in html
    assert "Must stay hidden." not in html


def test_html_report_does_not_guess_related_steps_without_status_language(
    tmp_path,
):
    out_dir = tmp_path / "output"
    exec_dir = out_dir / "fol" / "tester" / "KEY"
    exec_dir.mkdir(parents=True)
    (exec_dir / "audit.json").write_text(json.dumps({
        "overall_verdict": "concerns",
        "summary": "x",
        "findings": [{
            "severity": "medium",
            "step_index": None,
            "description": "The environment could not be confirmed.",
        }],
    }))
    (exec_dir / "metadata.json").write_text(json.dumps({
        "test_results": [{
            "id": 1,
            "steps": [{
                "index": 1,
                "description": "Unrelated blocked step.",
                "status": "Blocked",
            }],
        }],
    }))

    html = report.generate_html_report(out_dir)

    assert "Affected test steps:" not in html
    assert "Unrelated blocked step." not in html


def test_html_report_renders_v6_triage_and_evidence_coverage(tmp_path):
    out_dir = tmp_path / "output"
    exec_dir = out_dir / "fol" / "tester" / "KEY"
    exec_dir.mkdir(parents=True)
    audit = {
        "overall_verdict": "concerns",
        "summary": "x",
        "findings": [{
            "severity": "medium",
            "page": None,
            "step_index": 2,
            "category": "insufficient_evidence",
            "confidence": "medium",
            "action": "add_evidence",
            "description": "Email confirmation is not shown.",
        }],
        "evidence_by_step": [{
            "step_index": 2,
            "pages": [4],
            "status": "partial",
            "confidence": "medium",
            "missing_reason": "No inbox screenshot.",
        }],
    }
    (exec_dir / "audit.json").write_text(json.dumps(audit))
    (exec_dir / "metadata.json").write_text(json.dumps({
        "test_results": [{"id": 1, "executed_by_name": "T", "steps": []}]
    }))

    html = report.generate_html_report(out_dir)

    assert "Category:" in html
    assert "insufficient evidence" in html
    assert "Action:" in html
    assert "add evidence" in html
    assert "Evidence coverage by step" in html
    assert "Step 3" in html
    assert "No inbox screenshot." in html


def test_triage_chips_hidden_when_only_inferred(tmp_path):
    out_dir = tmp_path / "output"
    exec_dir = out_dir / "fol" / "tester" / "KEY"
    exec_dir.mkdir(parents=True)
    audit = {
        "overall_verdict": "concerns",
        "summary": "x",
        "findings": [{
            "severity": "medium",
            "page": None,
            "step_index": 2,
            "description": "Email confirmation is not shown in any screenshot.",
        }],
    }
    (exec_dir / "audit.json").write_text(json.dumps(audit))
    (exec_dir / "metadata.json").write_text(json.dumps({
        "test_results": [{"id": 1, "executed_by_name": "T", "steps": []}]
    }))

    html = report.generate_html_report(out_dir)

    assert "Email confirmation is not shown" in html
    assert "Category:" not in html
    assert "Confidence:" not in html
    assert "Action:" not in html


def test_report_infers_legacy_triage_into_action_queue(tmp_path):
    finding = {
        "severity": "medium",
        "page": None,
        "step_index": 8,
        "description": (
            "Step 8 requires verifying the title in Library. "
            "No library screenshot and no purchase history screenshot "
            "are provided."
        ),
    }
    _write_audit(
        tmp_path,
        "run1",
        "alice",
        "LEGACY-1",
        verdict="concerns",
        findings=[finding],
        schema_version=5,
    )

    md = report.generate_report(tmp_path)
    assert "## Action queue" in md
    assert "| Add evidence | 1 |" in md
    assert "| Insufficient evidence | 1 |" in md

    html = report.generate_html_report(tmp_path)
    assert "Legacy audits detected" in html
    assert "data-actions=\"add_evidence\"" in html
    assert "data-categories=\"insufficient_evidence\"" in html
    assert "argus-action-panel" not in html
    assert 'id="argus-action"' in html
    assert 'id="argus-category"' in html


def test_advisories_are_visible_but_excluded_from_action_and_divergence(
        tmp_path):
    finding = {
        "severity": "low",
        "description": "Mobile URL evidence could not be verified.",
        "source": "env_check",
        "rule": "R9-amb",
        "advisory": True,
    }
    _write_audit(
        tmp_path,
        "run1",
        "alice",
        "ADVISORY-1",
        verdict="concerns",
        findings=[finding],
        tester_status="Pass",
    )

    summaries = report._scan_output_dir(tmp_path)
    assert summaries[0].verdict == "pass"
    assert report._is_divergent(summaries[0]) is False
    md = report.generate_report(tmp_path, summaries=summaries)
    assert "**Operational findings:** 0" in md
    assert "**Advisories (excluded from compliance actions):** 1" in md
    assert "## Action queue" not in md
    html = report.generate_html_report(tmp_path, summaries=summaries)
    assert "Advisories (1)" in html
    assert "<strong>Type:</strong> Advisory" in html
    assert 'data-actions=""' in html


def test_diagnostic_low_remains_in_operational_action_queue(tmp_path):
    _write_audit(
        tmp_path,
        "run1",
        "alice",
        "DIAGNOSTIC-1",
        verdict="pass",
        findings=[{
            "severity": "low",
            "description": "Diagnostic claim-code does not match.",
        }],
    )
    [summary] = report._scan_output_dir(tmp_path)
    assert summary.verdict == "concerns"
    md = report.generate_report(tmp_path, summaries=[summary])
    assert "**Operational findings:** 1" in md
    assert "| Review manually | 1 |" in md


def test_renderers_accept_precomputed_summaries(tmp_path: Path, monkeypatch):
    _write_audit(tmp_path, "Run_A", "tester_one", "QA-E1",
                 verdict="concerns", findings=[_model_finding("medium")])
    _write_audit(tmp_path, "Run_A", "tester_two", "QA-E2")

    summaries = report.scan_output_dir(tmp_path)
    md_self = report.generate_report(tmp_path)
    html_self = report.generate_html_report(tmp_path)

    def boom(*a, **kw):
        raise AssertionError("summaries supplied — scan must not run")
    monkeypatch.setattr(report, "_scan_output_dir", boom)

    md_shared = report.generate_report(tmp_path, summaries=summaries)
    html_shared = report.generate_html_report(tmp_path, summaries=summaries)
    assert md_shared == md_self
    assert html_shared == html_self


def test_failed_keys_section_single_walk(tmp_path: Path):
    """_render_failed_keys resolves _last_error.json details in one walk."""
    exec_dir = tmp_path / "Run_A" / "tester_one" / "QA-E9"
    exec_dir.mkdir(parents=True)
    (exec_dir / "_last_error.json").write_text(json.dumps({
        "exception_class": "TimeoutError",
        "exception_message": "Model API read timed out",
    }))
    (tmp_path / "_failed_keys.txt").write_text(
        "QA-E9\nQA-E404\n")

    section = report._render_failed_keys(tmp_path)
    assert "QA-E9" in section
    assert "TimeoutError: Model API read timed out" in section
    assert "QA-E404" in section
    assert "(no _last_error.json found)" in section


def test_audit_row_badges_for_blocked_and_in_progress(tmp_path: Path):
    row_blocked = report._argus_render_audit_row(
        _stub_summary(rules_fired=["R20"]), 0, tmp_path)
    assert "argus-blocked-badge" in row_blocked
    assert ">BLOCKED<" in row_blocked
    row_inprog = report._argus_render_audit_row(
        _stub_summary(rules_fired=["R21"]), 1, tmp_path)
    assert "argus-inprog-badge" in row_inprog
    assert ">IN PROGRESS<" in row_inprog
    row_plain = report._argus_render_audit_row(
        _stub_summary(rules_fired=[]), 2, tmp_path)
    assert "argus-blocked-badge" not in row_plain
    assert "argus-inprog-badge" not in row_plain


def test_audit_row_has_status_column(tmp_path: Path):
    row = report._argus_render_audit_row(
        _stub_summary(tester_status="Blocked"), 0, tmp_path)
    assert 'data-status="Blocked"' in row
    assert "argus-status-blocked" in row
    assert 'colspan="6"' in row


def test_audit_row_status_column_handles_missing_status(tmp_path: Path):
    row = report._argus_render_audit_row(
        _stub_summary(tester_status=None), 0, tmp_path)
    assert 'data-status=""' in row


def test_filters_bar_includes_status_dropdown():
    html = report._argus_render_filters_bar(
        3, ["A"], ["Blocked", "Passed With Issue", "Pass"])
    assert 'id="argus-status"' in html
    assert '<option value="">All statuses</option>' in html
    assert html.index('value="Blocked"') < html.index(
        'value="Passed With Issue"') < html.index('value="Pass"')


def test_filters_bar_status_dropdown_empty_when_no_statuses():
    html = report._argus_render_filters_bar(0, [], [])
    assert 'id="argus-status"' in html
    assert '<option value="">All statuses</option>' in html


def test_dismissed_banner_uses_text_tag_not_shortcode(tmp_path: Path):
    row = report._argus_render_audit_row(
        _stub_summary(acknowledgments=[{
            "type": "dismissed", "by": "argus-test-user",
            "at": "2026-07-01", "reason": "false positive",
        }]), 0, tmp_path)
    assert ":mute:" not in row
    assert "argus-dismissed-tag" in row
    assert "argus-test-user" in row


def test_audit_row_carries_bare_key_for_sorting(tmp_path: Path):
    row = report._argus_render_audit_row(_stub_summary(), 0, tmp_path)
    assert row.count('data-key="K"') == 2
    assert row.startswith('<tr class="argus-row" data-idx="0" '
                          'tabindex="0" aria-expanded="false" '
                          'data-key="K"')


def test_audit_row_tc_cells_have_title_tooltips(tmp_path: Path):
    row = report._argus_render_audit_row(
        _stub_summary(testrun_name="E2E Flow - Very Long Run Name",
                      test_case="QA-T1 — homepage smoke"), 0, tmp_path)
    assert ('class="argus-tc-run" '
            'title="E2E Flow - Very Long Run Name"') in row
    assert ('class="argus-tc-name" '
            'title="QA-T1 — homepage smoke"') in row


def test_audit_row_links_bare_team_test_case_key(tmp_path: Path):
    row = report._argus_render_audit_row(
        _stub_summary(test_case="TEAM42-T17"),
        0,
        tmp_path,
    )
    assert "#/testCase/TEAM42-T17" in row


def test_unaudited_rows_data_attrs_and_tooltips(tmp_path: Path):
    rows, testers = report._argus_unaudited_rows([], [
        {
            "test_case_key": "QA-T9",
            "test_case_name": "checkout smoke",
            "testrun_key": "QA-C1",
            "testrun_name": "Run 1",
            "marketplace": "US",
            "status": "Pass",
            "executed_by_name": "bob",
            "e_key": "QA-E77",
        },
        {
            "test_case_key": "QA-T10",
            "testrun_key": "QA-C1",
            "marketplace": "US",
            "status": "Not Executed",
            "executed_by_name": "bob",
            "e_key": "QA-E78",
        },
    ], start_idx=0)
    [row] = rows
    assert 'data-key="QA-E77"' in row
    assert "QA-E78" not in row
    assert "aria-expanded" not in row
    assert 'class="argus-tc-run" title="Run 1"' in row
    assert ('class="argus-tc-name" '
            'title="QA-T9 — checkout smoke"') in row


def test_sortable_headers_are_buttons_with_aria_sort(tmp_path: Path):
    _write_audit(tmp_path, "run1", "alice", "E-1")
    html = report.generate_html_report(tmp_path)
    assert 'aria-sort="none"' in html
    assert "argus-th-btn" in html
    assert 'data-sort-key="findings" data-sort-numeric="1"' in html


def test_filters_bar_has_expand_collapse_buttons():
    html = report._argus_render_filters_bar(3, ["A"], [])
    assert 'id="argus-expand-all"' in html
    assert 'id="argus-collapse-all"' in html


def test_primary_report_ctas_use_brand_tokens():
    css = html_report._ARGUS_CS_CSS

    assert "--argus-action:var(--ui-color-background-button-primary-default)" in css
    assert "--argus-action-hover:var(--ui-color-background-button-primary-hover)" in css
    for selector in (
        "#argus-coverage-open",
        ".argus-folder-row a.cs-btn",
    ):
        assert selector in css
    orange_rules = [
        line for line in css.splitlines()
        if line.startswith("#argus-coverage-open")
    ]
    assert len(orange_rules) == 3
    for selector in (
        "#argus-clear",
        "#argus-expand-all",
        "#argus-collapse-all",
    ):
        assert all(selector not in rule for rule in orange_rules)


def test_navigation_controls_are_shared_with_other_argus_pages():
    shell = (html_report._ASSETS_DIR / "shell.js").read_text()
    assert shell in html_report._cs_head_extra()
    assert shell not in html_report._ARGUS_JS


def test_ticket_template_is_a_plain_utility_link_in_navigation():
    context = html_report.ReportUiContext(
        ticket_template_url=(
            "https://issues.example.com/create/templates/example"
        ),
    )

    html = html_report._cs_sidenav(
        "./",
        "reports",
        False,
        context,
    )

    assert '<div class="nav-divider nav-utility-divider"></div>' in html
    assert 'class="nav-link nav-utility-link"' in html
    assert 'href="https://issues.example.com/create/templates/example"' in html
    assert 'target="_blank" rel="noopener noreferrer"' in html
    assert "Report Issue" in html
    assert "Report an issue" not in html
    assert "nav-support" not in html
    assert ".nav-utility-link{gap:4px}" in html_report._ARGUS_CS_CSS


def test_ticket_template_is_omitted_when_not_configured():
    html = html_report._cs_sidenav(
        "./",
        "reports",
        False,
        html_report.ReportUiContext(ticket_template_url=""),
    )

    assert "Report Issue" not in html


def test_tester_panel_mini_bar_only_when_audited():
    audited = report._argus_render_tester_panel([{
        "tester": "alice", "executions": 3, "audits": 2, "not_audited": 1,
        "verdicts": {"pass": 1, "concerns": 1, "fail": 0},
        "top_issues": [],
    }])
    assert "argus-tester-bar" in audited
    assert "argus-mini-pill" in audited
    unaudited_only = report._argus_render_tester_panel([{
        "tester": "bob", "executions": 2, "audits": 0, "not_audited": 2,
        "verdicts": {"pass": 0, "concerns": 0, "fail": 0},
        "top_issues": [],
    }])
    assert "argus-tester-bar" not in unaudited_only


def test_tester_panel_matches_original_auto_layout_without_scroll_wrapper():
    html = report._argus_render_tester_panel([{
        "tester": "alice", "executions": 3, "audits": 2, "not_audited": 1,
        "verdicts": {"pass": 1, "concerns": 1, "fail": 0},
        "top_issues": [("Environment", 1)],
    }])

    table = '<table class="argus-tester-table">'
    assert "argus-tester-table-wrap" not in html
    assert html.index("</div>") < html.index(table)
    assert "argus-tester-col-" not in html
    assert ".argus-tester-table-wrap" not in html_report._ARGUS_CSS
    assert "table-layout: fixed" not in html_report._ARGUS_CSS
    assert "min-width: 820px" not in html_report._ARGUS_CSS
    assert "width: 140px;" in html_report._ARGUS_CSS
    assert ".argus-verdict-mini { white-space: nowrap; }" \
        in html_report._ARGUS_CSS


def test_full_report_includes_theme_toggle_and_dark_palette(
        tmp_path: Path):
    _write_audit(tmp_path, "run1", "alice", "E-1")
    html = report.generate_html_report(tmp_path)
    assert 'id="cs-theme-toggle"' in html
    assert html.index("localStorage.getItem('cs.theme')") \
        < html.index('<style id="argus-legacy">')
    assert 'html[data-theme="dark"]' in html
    assert 'html[data-cs-theme="dark"]' in html


def test_lazy_report_bundle_writes_detail_chunks_and_keeps_archive_self_contained(
        tmp_path: Path):
    _write_audit(
        tmp_path,
        "run1",
        "alice",
        "E-1",
        verdict="concerns",
        findings=[_model_finding("medium")],
    )

    bundle = report.generate_html_report_bundle(tmp_path)

    assert '__ARGUS_DETAIL_BASE__' in bundle.canonical_html
    assert "argus-detail-slot" in bundle.canonical_html
    assert list(bundle.detail_chunks) == ["0000.json"]
    assert "E-1" in bundle.detail_chunks["0000.json"]["0"]
    assert '<td colspan="6" class="argus-detail-slot"' not in bundle.archive_html
    assert "Required analysis incomplete." not in bundle.archive_html


def test_detail_chunks_are_stable_page_sized_groups():
    chunks = html_report.detail_chunk_maps({
        index: f"<div>detail {index}</div>"
        for index in range(101)
    })

    assert list(chunks) == ["0000.json", "0001.json", "0002.json"]
    assert [len(payload) for payload in chunks.values()] == [50, 50, 1]
    assert chunks["0000.json"]["49"] == "<div>detail 49</div>"
    assert chunks["0001.json"]["50"] == "<div>detail 50</div>"
    assert chunks["0002.json"]["100"] == "<div>detail 100</div>"


def test_expensive_report_subtrees_use_css_containment():
    assert (
        ".argus-detail,.argus-tester-panel,.argus-noevd-panel"
        "{contain:content}"
    ) in html_report._ARGUS_CS_CSS


def test_empty_folder_still_renders(tmp_path: Path):
    html = report.generate_html_report(tmp_path)
    assert "No audits found" in html
