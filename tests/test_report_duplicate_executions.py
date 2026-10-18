import json
import os

from argus.reporting import report
from argus.reporting import server


def _entry(root):
    (root / "_argus.html").write_text(report.generate_html_report(root))
    return next(row for row in server._scan_folders_uncached(root.parent) if row["name"] == root.name)


def test_report_and_dashboard_count_only_latest_copy_of_exact_execution(tmp_path):
    for relative, verdict, modified in (
        ("old-tester/MOBILE-E1", "fail", 100),
        ("renamed-tester/MOBILE-E1", "pass", 200),
        ("renamed-tester/MOBILE-E2", "concerns", 150),
        ("_stale_backups/MOBILE-E1", "fail", 300),
    ):
        folder = tmp_path / relative
        folder.mkdir(parents=True)
        path = folder / "audit.json"
        path.write_text(json.dumps({
            "execution_key": folder.name, "analysis_status": "complete",
            "overall_verdict": verdict, "findings": [],
        }))
        os.utime(path, (modified, modified))
        (folder / "metadata.json").write_text(json.dumps({
            "test_case": {"key": "MOBILE-T1", "name": "The same test case"},
        }))
    summaries = report.scan_output_dir(tmp_path)
    assert {item.key: item.verdict for item in summaries} == {
        "MOBILE-E1": "pass", "MOBILE-E2": "pass",
    }
    assert _entry(tmp_path)["audits"] == 2
    assert (tmp_path / "old-tester/MOBILE-E1/audit.json").exists()


def test_dashboard_counts_current_failure_without_counting_stale_success(tmp_path):
    for key, status in (
        ("MOBILE-E1", "complete"), ("MOBILE-E2", "incomplete"),
        ("MOBILE-E3", "complete"), ("MOBILE-E4", "complete"),
    ):
        folder = tmp_path / key
        folder.mkdir()
        (folder / "audit.json").write_text(json.dumps({
            "execution_key": key, "analysis_status": status,
            "overall_verdict": "concerns", "findings": [],
        }))
    (tmp_path / "_latest_run.json").write_text(json.dumps({
        "version": 1, "folder_name": "Android",
        "started_at": "2026-09-12T10:00:00Z",
        "completed_at": "2026-09-12T10:01:00Z",
        "scope_keys": ["MOBILE-E1", "MOBILE-E2", "MOBILE-E3"],
        "selected_keys": ["MOBILE-E2", "MOBILE-E3"],
        "audit_keys": ["MOBILE-E1"],
        "failed_keys": ["MOBILE-E2", "MOBILE-E3", "MOBILE-E4"],
        "statuses": {"MOBILE-E2": "failed", "MOBILE-E3": "failed"},
    }))
    assert {row.key for row in report.scan_output_dir(tmp_path)} == {
        "MOBILE-E1", "MOBILE-E2",
    }
    entry = _entry(tmp_path)
    assert entry["audits"] == 2
    assert entry["completion_state"] == "incomplete"
