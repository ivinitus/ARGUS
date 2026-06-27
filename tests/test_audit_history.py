"""Layer-2 audit-history tests.

Covers the `_archive_previous_audit` helper plus the integration with
`auditor.run()`: when a tester re-executes a test case after an audit,
Layer 1 re-emits the E-key and Layer 2 must move the prior `audit.json`
+ `audit.md` into `audit.json.history/` BEFORE the new audit overwrites.

Mirrors the style of test_audit_integration.py — same `FakeLLMRuntimeClient`
import path, same tiny-PNG fixture pattern, same `monkeypatch` of
`auditor.boto3.client`.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import pytest

import auditor

# Reuse the test-fixture helpers from the existing integration test —
# duplicating them here would drift over time.
from tests.test_audit_integration import FakeLLMRuntimeClient, _make_tiny_png


# ---------------------------------------------------------------------------
# _archive_previous_audit unit tests (no auditor.run, no LLM Runtime)
# ---------------------------------------------------------------------------

def test_archive_previous_audit_creates_history_dir(tmp_path: Path):
    """Existing audit.json + audit.md → both moved into history dir under
    timestamped names; originals gone afterwards."""
    exec_dir = tmp_path / "TC-E1"
    exec_dir.mkdir()
    original = {
        "overall_verdict": "fail",
        "summary": "old audit",
        "findings": [],
        "audited_at": "2026-05-26T08:00:00Z",
    }
    (exec_dir / "audit.json").write_text(json.dumps(original))
    (exec_dir / "audit.md").write_text("# old audit md\n")

    history_dir = auditor._archive_previous_audit(exec_dir)
    assert history_dir == exec_dir / "audit.json.history"
    assert history_dir.is_dir()

    # Old files no longer at canonical paths.
    assert not (exec_dir / "audit.json").exists()
    assert not (exec_dir / "audit.md").exists()

    # Archived files exist with timestamped names; JSON content preserved.
    archived_jsons = list(history_dir.glob("audit_*.json"))
    archived_mds = list(history_dir.glob("audit_*.md"))
    assert len(archived_jsons) == 1
    assert len(archived_mds) == 1
    assert json.loads(archived_jsons[0].read_text()) == original
    assert "old audit md" in archived_mds[0].read_text()


def test_archive_previous_audit_first_run_is_noop(tmp_path: Path):
    """No audit.json present → returns None, history dir not created."""
    exec_dir = tmp_path / "TC-E2"
    exec_dir.mkdir()
    result = auditor._archive_previous_audit(exec_dir)
    assert result is None
    assert not (exec_dir / "audit.json.history").exists()


def test_archive_previous_audit_handles_missing_md(tmp_path: Path):
    """audit.json present but audit.md missing → JSON archived, no crash."""
    exec_dir = tmp_path / "TC-E3"
    exec_dir.mkdir()
    (exec_dir / "audit.json").write_text(json.dumps({
        "overall_verdict": "pass",
        "summary": "x",
        "findings": [],
        "audited_at": "2026-05-26T09:30:00Z",
    }))
    # No audit.md.
    history_dir = auditor._archive_previous_audit(exec_dir)
    assert history_dir is not None
    archived_jsons = list(history_dir.glob("audit_*.json"))
    assert len(archived_jsons) == 1
    # Confirm no md was magically created.
    assert list(history_dir.glob("audit_*.md")) == []


def test_archive_previous_audit_uses_audited_at_field(tmp_path: Path):
    """Pre-existing audit.json with audited_at='2026-05-26T08:00:00Z' →
    archive filename is audit_2026-05-26T08-00-00.json (colons replaced)."""
    exec_dir = tmp_path / "TC-E4"
    exec_dir.mkdir()
    (exec_dir / "audit.json").write_text(json.dumps({
        "overall_verdict": "concerns",
        "summary": "x",
        "findings": [],
        "audited_at": "2026-05-26T08:00:00Z",
    }))
    history_dir = auditor._archive_previous_audit(exec_dir)
    assert history_dir is not None
    archived = list(history_dir.glob("audit_*.json"))
    assert len(archived) == 1
    assert archived[0].name == "audit_2026-05-26T08-00-00.json"


def test_archive_previous_audit_falls_back_to_mtime(tmp_path: Path):
    """Pre-existing audit.json with NO audited_at → filename derives from
    file mtime. Just check the regex shape — we don't pin the actual
    timestamp because that depends on test-machine time."""
    exec_dir = tmp_path / "TC-E5"
    exec_dir.mkdir()
    # Legacy audit (pre-Layer-2 schema): no audited_at field.
    (exec_dir / "audit.json").write_text(json.dumps({
        "overall_verdict": "pass",
        "summary": "x",
        "findings": [],
    }))
    # Force a stable mtime so the test is deterministic; the actual value
    # doesn't matter, only the resulting filename pattern.
    fixed_mtime = time.mktime((2026, 5, 1, 14, 23, 5, 0, 0, 0))
    os.utime(exec_dir / "audit.json", (fixed_mtime, fixed_mtime))

    history_dir = auditor._archive_previous_audit(exec_dir)
    assert history_dir is not None
    archived = list(history_dir.glob("audit_*.json"))
    assert len(archived) == 1
    assert re.match(
        r"^audit_\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}\.json$",
        archived[0].name,
    ), f"unexpected filename: {archived[0].name}"


# ---------------------------------------------------------------------------
# Integration: auditor.run() archives then writes new
# ---------------------------------------------------------------------------

def test_audit_run_archives_then_writes_new(tmp_path: Path, monkeypatch):
    """Run-twice scenario: the second auditor.run() must move the first
    audit into history/ before writing the new one."""
    exec_dir = tmp_path / "TC-E6"
    shots = exec_dir / "screenshots" / "sess1"
    shots.mkdir(parents=True)
    _make_tiny_png(shots / "page_001.png")

    metadata = {
        "test_results": [{
            "id": 42,
            "executed_by_name": "Tester",
            "execution_date": "2026-05-26T12:00:00Z",
            "steps": [
                {"index": 1, "description": "Open homepage",
                 "expected_result": "Page loads"},
            ],
        }]
    }
    (exec_dir / "metadata.json").write_text(json.dumps(metadata))

    # Pre-create a "previous" audit. Use a clearly different verdict so
    # the assertion below catches a clobber rather than a no-op write.
    prior = {
        "overall_verdict": "fail",
        "summary": "the old verdict",
        "findings": [{"severity": "high", "description": "old issue"}],
        "audited_at": "2026-05-26T08:00:00Z",
    }
    (exec_dir / "audit.json").write_text(json.dumps(prior))
    (exec_dir / "audit.md").write_text("# prior audit\n")

    # Mocked LLM Runtime returns a clean PASS audit.
    new_audit = {
        "overall_verdict": "pass",
        "summary": "looks clean now",
        "findings": [],
    }
    fake_client = FakeLLMRuntimeClient(json.dumps(new_audit))
    monkeypatch.setattr(auditor.boto3, "client",
                        lambda *a, **kw: fake_client)

    rc = auditor.run(auditor.AuditConfig(execution_dir=exec_dir))
    assert rc == 0

    # New audit at canonical path.
    new_on_disk = json.loads((exec_dir / "audit.json").read_text())
    assert new_on_disk["overall_verdict"] == "pass"

    # Old audit archived. Filename should derive from prior audited_at.
    history_dir = exec_dir / "audit.json.history"
    assert history_dir.is_dir()
    archived = list(history_dir.glob("audit_*.json"))
    assert len(archived) == 1
    archived_data = json.loads(archived[0].read_text())
    assert archived_data["overall_verdict"] == "fail"
    assert archived_data["summary"] == "the old verdict"


def test_audited_at_stamped_on_audit(tmp_path: Path, monkeypatch):
    """Every fresh audit.json carries an audited_at field in ISO-Z form."""
    exec_dir = tmp_path / "TC-E7"
    shots = exec_dir / "screenshots" / "sess1"
    shots.mkdir(parents=True)
    _make_tiny_png(shots / "page_001.png")

    (exec_dir / "metadata.json").write_text(json.dumps({
        "test_results": [{"steps": []}],
    }))

    fake_audit = {
        "overall_verdict": "pass",
        "summary": "x",
        "findings": [],
    }
    monkeypatch.setattr(auditor.boto3, "client",
                        lambda *a, **kw: FakeLLMRuntimeClient(json.dumps(fake_audit)))
    rc = auditor.run(auditor.AuditConfig(execution_dir=exec_dir))
    assert rc == 0

    out = json.loads((exec_dir / "audit.json").read_text())
    audited_at = out.get("audited_at")
    assert isinstance(audited_at, str), f"audited_at missing: {out!r}"
    # Shape: YYYY-MM-DDTHH:MM:SSZ
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", audited_at,
    ), f"unexpected audited_at: {audited_at!r}"
