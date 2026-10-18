from __future__ import annotations

import copy
import json
import os
import sqlite3
from pathlib import Path

import pytest

from argus import config
from argus.cli import main as cli
from argus.core import exec_paths
from argus.domain import workflow_rules
from argus.operations import audit_index, audit_lock
from argus.operations import reader_policy_backfill as backfill
from argus.pipeline import auditor, batch
from argus.reporting import report


def finding(rule, severity="low", **extra):
    return {
        "rule": rule, "source": "device_check" if rule.startswith("R19") else "rule",
        "severity": severity, "description": f"Historical {rule} finding", **extra,
    }


@pytest.fixture
def settings(tmp_path):
    settings = config.load(profile="reader")
    settings.extractor.out_dir = tmp_path / "reader"
    settings.extractor.out_dir.mkdir()
    settings.general.state_dir = tmp_path / "state"
    audit_index.configure(settings)
    workflow_rules.configure(settings.policy)
    yield settings
    workflow_rules.reset_configuration()


def write_audit(settings, relative="Ereader/QA-E1", findings=None):
    path = settings.extractor.out_dir / relative / "audit.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    audit = {
        "overall_verdict": "fail", "summary": "Historical summary",
        "schema_version": 8, "audited_at": "2026-10-01T12:00:00Z",
        "findings": findings if findings is not None else [finding("R19", "high"), finding("R22")],
        "device_check": {"version": 4, "observed_device": "desktop"},
    }
    path.write_text(json.dumps(audit))
    path.with_name("metadata.json").write_text(json.dumps({
        "test_results": [{"status": "Pass", "test_case_key": "QA-T1", "steps": []}],
    }))
    path.with_name("audit.md").write_text("Historical Markdown")
    return path


@pytest.mark.parametrize("status", ["Pass", "Fail", "Blocked", "Passed With Issue", "Retest"])
def test_reader_never_requests_account_or_device_identity(settings, status):
    metadata = {"test_results": [{
        "status": status, "steps": [{"description": "Play audio", "status": status}],
    }]}
    findings = workflow_rules.run_workflow_rules(metadata)
    assert all(f["rule"] != "R22" for f in findings)
    assert workflow_rules.filter_enabled([finding("R19"), finding("R19-amb"), finding("R22")]) == []
    assert not batch._post_checks_enabled(settings)
    cfg = auditor.AuditConfig.from_settings(settings.auditor, settings.extractor.out_dir)
    assert "Do not check device family" in cfg.system_prompt
    assert "does not require a test-account identifier" in cfg.system_prompt


def test_reconcile_keeps_other_findings_and_review_history():
    other = finding("R2", "high", category="tester_error", custom="keep")
    audit = {
        "findings": [finding("R22"), finding("R19", "high"), finding("R19-amb"), other],
        "overall_verdict": "fail", "summary": "Wrong device.",
        "device_check": {"version": 4}, "acknowledgments": [{"rule": "R22", "actor": "reviewer"}],
        "review_status": "required", "cost": {"usd": 1}, "evidence_by_step": [{"pages": [1]}],
    }
    original = copy.deepcopy(audit)
    updated, counts = backfill.reconcile(audit)
    assert audit == original
    assert updated["findings"] == [other]
    assert updated["overall_verdict"] == "fail"
    assert "Wrong device" not in updated["summary"]
    assert counts["R19"] == 2 and counts["R22"] == 1
    for field in ("review_status", "acknowledgments", "cost", "evidence_by_step"):
        assert updated[field] == audit[field]
    assert "device_check" not in updated
    assert backfill.reconcile(updated) == (updated, dict.fromkeys(counts, 0))


@pytest.mark.parametrize(("other", "verdict"), [
    ([], "pass"),
    ([finding("R10", "medium")], "concerns"),
    ([finding("R2", "high")], "fail"),
])
def test_retired_device_check_no_longer_escalates_verdict(other, verdict):
    updated, _ = backfill.reconcile({
        "findings": [finding("R19", "high"), *other], "overall_verdict": "fail",
    })
    assert updated["overall_verdict"] == verdict


def test_clear_only_device_failure_and_keep_other_incomplete_analysis():
    audit = {
        "findings": [], "overall_verdict": "pass", "analysis_status": "incomplete",
        "analysis_modules": {
            "environment": {"status": "incomplete", "diagnostic": "device-family analysis failed: Timeout"},
            "model": {"status": "incomplete", "diagnostic": "model failed"},
        },
    }
    updated, counts = backfill.reconcile(audit)
    assert counts["device_failures"] == 1
    assert updated["analysis_modules"]["environment"] == {"status": "not_applicable"}
    assert updated["analysis_status"] == "incomplete"
    assert updated["analysis_modules"]["model"] == audit["analysis_modules"]["model"]
    audit["analysis_modules"]["model"]["status"] = "complete"
    updated, _ = backfill.reconcile(audit)
    assert updated["analysis_status"] == "complete"
    audit["analysis_modules"]["environment"]["diagnostic"] = "host analysis failed"
    assert backfill.reconcile(audit)[0] == audit


def test_preview_then_apply_preserves_timestamps_and_rebuilds_indexes(settings):
    path = write_audit(settings)
    metadata_bytes = path.with_name("metadata.json").read_bytes()
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns)
              for p in settings.extractor.out_dir.rglob("*") if p.is_file()}
    preview = backfill.backfill(settings)
    assert preview["changed"] == 1 and preview["R22"] == preview["R19"] == 1
    assert preview["backup_root"] is None
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns)
                      for p in settings.extractor.out_dir.rglob("*") if p.is_file()}
    audit_index.sync(settings.extractor.out_dir)
    audit_index.sync(path.parents[1])
    applied = backfill.backfill(settings, apply=True)
    assert not applied["errors"]
    backup = Path(applied["backup_root"]) / path.relative_to(settings.extractor.out_dir)
    assert backup.read_bytes() == before[path][0]
    assert backup.with_name("audit.md").read_bytes() == b"Historical Markdown"
    assert path.stat().st_mtime_ns == before[path][1]
    assert path.with_name("metadata.json").read_bytes() == metadata_bytes
    assert json.loads(path.read_text())["findings"] == []
    assert "Historical R22" not in path.with_name("audit.md").read_text()
    for root in (settings.extractor.out_dir, path.parents[1]):
        with sqlite3.connect(root / audit_index.DB_FILENAME) as db:
            assert db.execute("SELECT verdict, findings, rules FROM audits").fetchall() == [("pass", 0, "")]
    summaries = report._scan_output_dir(settings.extractor.out_dir)
    assert len(summaries) == 1
    assert summaries[0].findings_count == 0 and summaries[0].rules_fired == []
    assert backfill.backfill(settings, apply=True)["changed"] == 0
    assert len(list(exec_paths.iter_audit_jsons(settings.extractor.out_dir))) == 1


def test_cleanup_does_not_promote_an_older_duplicate(settings):
    old = write_audit(settings, "Ereader/old/QA-E1")
    new = write_audit(settings, "Ereader/new/QA-E1", findings=[finding("R10", "medium")])
    os.utime(old, ns=(1_000_000_000, 1_000_000_000))
    os.utime(new, ns=(2_000_000_000, 2_000_000_000))
    backfill.backfill(settings, apply=True)
    assert list(exec_paths.iter_latest_audit_jsons(settings.extractor.out_dir)) == [new]


def test_other_team_and_enabled_policy_are_rejected(settings):
    settings.general.team_slug = "tv"
    with pytest.raises(ValueError, match="Reader profile and team"):
        backfill.backfill(settings, apply=True)
    settings.general.team_slug = "reader"
    settings.policy.enabled_rules.append("R19")
    with pytest.raises(ValueError, match="disable both"):
        backfill.backfill(settings)


def test_busy_execution_is_not_changed(settings):
    path = write_audit(settings)
    original = path.read_bytes()
    with audit_lock.acquire_key(settings.extractor.out_dir, "QA-E1", owner="audit"):
        result = backfill.backfill(settings, apply=True)
    assert result["changed"] == 0 and len(result["errors"]) == 1
    assert path.read_bytes() == original


def test_failed_write_restores_both_artifacts(settings, monkeypatch):
    path = write_audit(settings)
    originals = {p: (p.read_bytes(), p.stat().st_mtime_ns)
                 for p in (path, path.with_name("audit.md"))}
    def fail_write(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(backfill.atomic_io, "atomic_write_json", fail_write)
    result = backfill.backfill(settings, apply=True)
    assert result["changed"] == 0 and len(result["errors"]) == 1
    for p, (content, timestamp) in originals.items():
        assert p.read_bytes() == content and p.stat().st_mtime_ns == timestamp


def test_replay_and_backups_are_excluded(settings):
    for relative in ("_replay/v2/QA-E1", "_stale_backups/old/QA-E1"):
        write_audit(settings, relative)
    assert backfill.backfill(settings, apply=True)["scanned"] == 0


def test_symlink_cannot_select_other_teams_data(settings, tmp_path):
    target = tmp_path / "other.json"
    target.write_text(json.dumps({"findings": [finding("R22")]}))
    directory = settings.extractor.out_dir / "Ereader/QA-E1"
    directory.mkdir(parents=True)
    (directory / "audit.json").symlink_to(target)
    original = target.read_bytes()
    result = backfill.backfill(settings, apply=True)
    assert result["changed"] == 0 and result["errors"]
    assert target.read_bytes() == original


def test_cli_dispatch_and_preview_use_reader(settings, monkeypatch, capsys):
    write_audit(settings)
    monkeypatch.setattr(backfill.config, "load", lambda **kwargs: settings)
    monkeypatch.setattr(backfill.runtime, "configure", lambda _: pytest.fail("preview configured runtime"))
    assert cli.main(["backfill-reader-policy"]) == 0
    assert json.loads(capsys.readouterr().out)["changed"] == 1
