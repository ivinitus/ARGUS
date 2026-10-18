import json

import pytest

from argus.pipeline import orchestrator
from argus.reporting import report, server


@pytest.mark.parametrize(("covered_keys", "schema"), [
    (["QA-E1"], 1),
    (["QA-E1", "QA-E99"], 1),
    (None, 1),
    (["QA-E1", "QA-E2"], None),
    (["QA-E1", "QA-E2"], 0),
    (["QA-E1", "QA-E2"], 2),
])
def test_restored_history_does_not_reuse_partial_coverage(tmp_path, covered_keys, schema):
    folder = tmp_path / "TV_Test_cycles"
    originals = {}
    for key, status in (("QA-E1", "complete"), ("QA-E2", "incomplete")):
        path = folder / "run" / key / "audit.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({
            "execution_key": key, "analysis_status": status, "findings": [],
        }))
        originals[path] = path.read_bytes()
    manifest = {
        "version": 1, "folder_name": folder.name,
        "started_at": "before", "completed_at": "after",
        "scope_keys": ["QA-E1"], "selected_keys": ["QA-E1"],
        "audit_keys": ["QA-E1"], "failed_keys": [], "statuses": {},
        "execution_key": "QA-E1",
    }
    (folder / "_latest_run.json").write_text(json.dumps(manifest))
    coverage = {"schema": schema, "items_total": 56, "items_executed": 55, "findings": []}
    if covered_keys is not None:
        coverage["executions"] = [{"e_key": key} for key in covered_keys]
    path = folder / "_coverage.json"
    path.write_text(json.dumps(coverage))
    saved_coverage = path.read_bytes()

    assert orchestrator._restore_single_execution_scope(folder)
    assert {row.key for row in report.scan_output_dir(folder)} == {
        "QA-E1", "QA-E2",
    }
    assert report._load_coverage_json(folder) is None
    assert report._render_coverage(folder) == ""
    assert report._render_coverage_html(folder) == ""
    (folder / "_argus.html").write_text(report.generate_html_report(folder))
    local = server._scan_folders_uncached(tmp_path)[0]
    assert local["audits"] == 2 and local["coverage"] is None
    assert path.read_bytes() == saved_coverage
    assert all(path.read_bytes() == body for path, body in originals.items())

    # A new full-folder snapshot restores coverage, including planned items
    # that do not yet have an execution identity.
    coverage.update({
        "schema": 1, "items_total": 3, "items_executed": 2,
        "executions": [{"e_key": "QA-E1"}, {"e_key": "QA-E2"}, {"e_key": None}],
    })
    path.write_text(json.dumps(coverage))
    assert report._load_coverage_json(folder)["items_total"] == 3
    assert server._scan_folders_uncached(tmp_path)[0]["coverage"]["items_total"] == 3


def test_unrestored_legacy_coverage_keeps_its_existing_scope(tmp_path):
    coverage = {"schema": 1, "items_total": 56, "items_executed": 55, "findings": []}
    (tmp_path / "_coverage.json").write_text(json.dumps(coverage))
    assert report._load_coverage_json(tmp_path) == coverage
