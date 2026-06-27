"""Unit + light-integration tests for report.py.

No network, no boto3 — report.py reads local JSON files and emits
markdown. Tests populate a temporary output/ tree that matches the
layout produced by extractor + auditor (<testrun_slug>/<tester_slug>/
<E-KEY>/{audit.json,metadata.json}) and assert on the rendered report.
"""
from __future__ import annotations

import json
from pathlib import Path

import report


# ---------------------------------------------------------------------------
# Tree builders — compact helpers that mirror the real on-disk layout
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Scanning + summary shape
# ---------------------------------------------------------------------------
def test_scan_returns_empty_list_for_missing_dir(tmp_path: Path):
    assert report._scan_output_dir(tmp_path / "does-not-exist") == []


def test_scan_skips_unparseable_audit(tmp_path: Path):
    # Good audit + one broken audit.json. Broken one should be skipped
    # silently so it doesn't kill the whole report.
    _write_audit(tmp_path, "run1", "alice", "E-1")
    bad_dir = tmp_path / "run1" / "alice" / "E-BAD"
    bad_dir.mkdir(parents=True)
    (bad_dir / "audit.json").write_text("{not json")
    summaries = report._scan_output_dir(tmp_path)
    assert len(summaries) == 1
    assert summaries[0].key == "E-1"


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
    # audit.json present, metadata.json missing — tester info becomes None
    # without blowing up the scan.
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


# ---------------------------------------------------------------------------
# Analysis predicates
# ---------------------------------------------------------------------------
def _stub_summary(**overrides) -> report.AuditSummary:
    """Minimal AuditSummary for direct-predicate tests. Fields can be
    overridden via kwargs so tests read as 'what's specific to this case'."""
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
    # R3 is deprecated and was never policy-correct — it should NOT
    # force-flag on its own even if somehow carried in stale audit
    # data. R8 is the new structural rule (Blocked overall without a
    # Blocked step) and MUST flag.
    for rule in ("R0", "R2", "R4", "R8"):
        s = _stub_summary(rules_fired=[rule])
        assert report._is_flagged(s) is True, (
            f"{rule} is structural and should flag"
        )


def test_is_flagged_silent_on_deprecated_r3_alone():
    # Lock-in: R3 is no longer a structural rule. A stale audit that
    # contains only an R3 finding must not be dragged into the flagged
    # list.
    assert report._is_flagged(_stub_summary(rules_fired=["R3"])) is False


def test_is_flagged_silent_on_mere_concerns_without_structural_rule():
    # R5 is heuristic-only; alone it shouldn't force-flag. R7 is a
    # policy finding (PWI without Fail step) — medium severity and
    # surfaces via findings, but not structural, so not flag-worthy
    # on its own.
    for rule in ("R3", "R5", "R7"):
        assert report._is_flagged(_stub_summary(rules_fired=[rule])) is False, (
            f"{rule} should not force-flag on its own"
        )


def test_is_flagged_returns_false_when_dismissed():
    """Layer 3: a 'dismissed' acknowledgment short-circuits flagging.
    Even an audit with verdict=fail AND high findings AND structural
    rules drops out of the flagged list once an operator has dismissed
    it via /argus dismiss — the dashboard should reflect that
    operator-driven decision."""
    s = _stub_summary(
        verdict="fail",
        severity_counts={"high": 2}, has_high_finding=True,
        rules_fired=["R0", "R4"],
        acknowledgments=[{
            "type": "dismissed",
            "by": "vinitus",
            "at": "2026-05-26T12:00:00Z",
            "reason": "false positive",
        }],
    )
    assert report._is_flagged(s) is False


def test_is_flagged_unaffected_by_unknown_ack_type():
    """Only `type == 'dismissed'` suppresses. Other ack types
    (e.g. 'acknowledged' if introduced later) leave flagging intact."""
    s = _stub_summary(
        verdict="fail",
        acknowledgments=[{"type": "other-type", "by": "vinitus"}],
    )
    assert report._is_flagged(s) is True


def test_is_divergent_only_when_tester_pass_and_auditor_not_pass():
    assert report._is_divergent(_stub_summary(
        tester_status="Pass", verdict="concerns")) is True
    assert report._is_divergent(_stub_summary(
        tester_status="Pass", verdict="fail")) is True
    assert report._is_divergent(_stub_summary(
        tester_status="Pass", verdict="pass")) is False
    # Tester claimed Fail and auditor agreed => no divergence.
    assert report._is_divergent(_stub_summary(
        tester_status="Fail", verdict="fail")) is False
    # Auditor upgraded from tester Fail to concerns => technically tester
    # was *more* cautious; we don't flag this as a compliance issue.
    assert report._is_divergent(_stub_summary(
        tester_status="Fail", verdict="concerns")) is False
    assert report._is_divergent(_stub_summary(
        tester_status=None, verdict="concerns")) is False


def test_is_stale_handles_missing_and_older_schema():
    assert report._is_stale(_stub_summary(schema_version=None)) is True
    assert report._is_stale(_stub_summary(schema_version=1)) is True
    assert report._is_stale(_stub_summary(
        schema_version=report._CURRENT_SCHEMA_VERSION)) is False


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------
def test_pct_handles_zero_total():
    assert report._pct(0, 0) == 0
    assert report._pct(5, 0) == 0


def test_pct_rounds_to_int():
    assert report._pct(1, 3) == 33
    assert report._pct(2, 3) == 67
    assert report._pct(1, 2) == 50


# ---------------------------------------------------------------------------
# End-to-end markdown rendering
# ---------------------------------------------------------------------------
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
    assert "pass=1" in md
    assert "concerns=2" in md


def test_divergence_sorts_worst_tester_first(tmp_path: Path):
    # alice: 3 audits, 3 tester-Pass, 2 divergent => 67%
    _write_audit(tmp_path, "run1", "alice", "A-1", verdict="concerns",
                 tester_status="Pass")
    _write_audit(tmp_path, "run1", "alice", "A-2", verdict="concerns",
                 tester_status="Pass")
    _write_audit(tmp_path, "run1", "alice", "A-3", verdict="pass",
                 tester_status="Pass")
    # bob: 3 audits, 3 tester-Pass, 3 divergent => 100%
    for k in ("B-1", "B-2", "B-3"):
        _write_audit(tmp_path, "run2", "bob", k, verdict="concerns",
                     tester_status="Pass")
    md = report.generate_report(tmp_path)
    divergence_section = md.split("## Tester-Pass vs Auditor-Concerns")[1] \
                           .split("\n## ")[0]
    # bob (100%) must appear before alice (67%) in the table rows.
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
    # R4 fires but no high finding — must still flag.
    _write_audit(tmp_path, "run1", "alice", "STRUCT", verdict="concerns",
                 findings=[_rule_finding("R4", "high")])
    md = report.generate_report(tmp_path)
    flagged = md.split("## Flagged for review")[1].split("\n## ")[0]
    assert "STRUCT" in flagged
    # The reason string should mention the structural rule.
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
    # Rules that never fired shouldn't appear in the table.
    assert "| R1 |" not in rule_section
    assert "| R2 |" not in rule_section


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
    # One successful audit — so the scan isn't empty.
    _write_audit(tmp_path, "run1", "alice", "OK-1")
    # One failed key with _last_error.json present.
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
    # Ghost key (no error file anywhere) shows the "no _last_error.json" note.
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
    # Alphabetical order.
    assert per_tester.index("### alice") < per_tester.index("### bob")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def test_main_writes_report_to_out_dir(tmp_path: Path, capsys):
    _write_audit(tmp_path, "run1", "alice", "K")
    rc = report.main(["--out-dir", str(tmp_path)])
    assert rc == 0
    # Filename is now timestamped (_report_YYYY-MM-DD_HHMMSS.md) so
    # each run writes a fresh file rather than clobbering the last.
    # Check via glob instead of hardcoded name.
    reports = list(tmp_path.glob("_report_*.md"))
    assert len(reports) == 1
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
    # File written with timestamped name…
    reports = list(tmp_path.glob("_report_*.md"))
    assert len(reports) == 1
    # …and same content echoed to stdout.
    assert "Tester-compliance report" in captured.out



# ---------------------------------------------------------------------------
# Image resolvers (for HTML report thumbnails)
# ---------------------------------------------------------------------------

def test_resolve_page_images_flat_and_sorted(tmp_path):
    """Pages across multiple PDF subdirs flatten to one sorted list —
    matches the auditor's flattening so finding.page lines up."""
    exec_dir = tmp_path / "exec"
    shots = exec_dir / "screenshots"
    (shots / "Session_A").mkdir(parents=True)
    (shots / "Session_B").mkdir(parents=True)
    (shots / "Session_A" / "page_002.jpg").write_bytes(b"")
    (shots / "Session_A" / "page_001.jpg").write_bytes(b"")
    (shots / "Session_B" / "page_001.jpg").write_bytes(b"")

    pages = report._resolve_page_images(exec_dir)
    # Subdir-name order, then page stem order within each subdir.
    assert [p.name for p in pages] == [
        "page_001.jpg", "page_002.jpg",  # from Session_A
        "page_001.jpg",                    # from Session_B
    ]
    assert pages[0].parent.name == "Session_A"
    assert pages[2].parent.name == "Session_B"


def test_resolve_page_images_skips_step_attachments(tmp_path):
    """The step_attachments subdir is for tester-attached step images,
    not PDF pages. It must NOT contribute to the page-number index."""
    exec_dir = tmp_path / "exec"
    shots = exec_dir / "screenshots"
    (shots / "Session_A").mkdir(parents=True)
    (shots / "step_attachments").mkdir(parents=True)
    (shots / "Session_A" / "page_001.jpg").write_bytes(b"")
    (shots / "step_attachments" / "step_003_77_shot.jpg").write_bytes(b"")
    (shots / "step_attachments" / "page_999.jpg").write_bytes(b"")  # wrong dir

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
    # Non-step prefix and unsupported ext — must be skipped.
    (step_dir / "top_99_shot.jpg").write_bytes(b"")
    (step_dir / "step_003_99_notes.pdf").write_bytes(b"")

    m = report._resolve_step_attachments(exec_dir)
    assert set(m) == {3, 17}
    assert len(m[3]) == 2
    assert len(m[17]) == 1
    # All survivors are image extensions.
    for imgs in m.values():
        for p in imgs:
            assert p.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def test_resolve_step_attachments_empty_when_dir_missing(tmp_path):
    exec_dir = tmp_path / "exec"
    (exec_dir / "screenshots").mkdir(parents=True)
    assert report._resolve_step_attachments(exec_dir) == {}


def test_image_thumb_html_emits_relative_src(tmp_path):
    """Thumbnails use a path relative to out_dir so the HTML file can
    sit alongside the output tree and the <img> just works."""
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
    """If the image is outside out_dir, we can't build a safe relative
    path. Return empty string so caller treats it as no-thumbnail."""
    out_dir = tmp_path / "output"
    out_dir.mkdir()
    elsewhere = tmp_path / "unrelated" / "page.jpg"
    elsewhere.parent.mkdir()
    elsewhere.write_bytes(b"")
    assert report._image_thumb_html(elsewhere, out_dir, alt="x") == ""


def test_html_report_embeds_thumbnail_for_finding_with_page(tmp_path):
    """End-to-end: a finding with page=2 shows a thumbnail pointing
    at the 2nd image (global-flat order) in the HTML output."""
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
    # The second page (page_002.jpg) should be rendered as a thumbnail.
    assert 'src="fol/tester/KEY/screenshots/Session/page_002.jpg"' in html
    # The first and third pages should NOT appear as thumbnails here
    # (no findings reference them).
    assert 'src="fol/tester/KEY/screenshots/Session/page_001.jpg"' not in html
    assert 'src="fol/tester/KEY/screenshots/Session/page_003.jpg"' not in html


def test_html_report_handles_out_of_range_page(tmp_path):
    """A finding claiming a page that doesn't exist must render a
    graceful placeholder, not a broken <img> link."""
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
    assert "Step 2" in html
    assert "No inbox screenshot." in html


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
    assert "Action queue" in html
    assert "Legacy audits detected" in html
    assert "data-actions=\"add_evidence\"" in html
    assert "data-categories=\"insufficient_evidence\"" in html
    assert "argus-action" in html
    assert "argus-category" in html
    assert "add evidence" in html
