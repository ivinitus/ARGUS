"""Tests for audit_index.py — the SQLite mirror of output/ artifacts."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from argus.operations import audit_index


def _write_audit(exec_dir: Path, *, verdict="concerns", findings=None,
                 tester="Jane Q", status="Pass", acks=None) -> None:
    exec_dir.mkdir(parents=True, exist_ok=True)
    audit = {
        "overall_verdict": verdict,
        "schema_version": 6,
        "findings": findings or [],
    }
    if acks:
        audit["acknowledgments"] = acks
    (exec_dir / "audit.json").write_text(json.dumps(audit))
    (exec_dir / "metadata.json").write_text(json.dumps({
        "test_results": [{
            "executed_by_name": tester,
            "status": status,
            "test_case_key": "QA-T1",
            "test_case_name": "Buy a book",
            "marketplace": "DE",
            "execution_date": "2026-07-01T00:00:00Z",
        }],
    }))


def test_sync_indexes_and_is_incremental(tmp_path: Path):
    exec_dir = tmp_path / "Folder_A" / "run" / "tester" / "QA-E1"
    _write_audit(exec_dir, verdict="fail", findings=[
        {"severity": "high", "source": "rule", "rule": "R12",
         "description": "no evidence"},
        {"severity": "low", "description": "model note"},
    ])

    counts = audit_index.sync(tmp_path)
    assert counts == {"added": 1, "updated": 0, "removed": 0, "unchanged": 0}

    counts = audit_index.sync(tmp_path)
    assert counts["unchanged"] == 1 and counts["added"] == 0

    conn = sqlite3.connect(tmp_path / audit_index.DB_FILENAME)
    row = conn.execute(
        "SELECT key, folder, verdict, tester, findings, high, low, rules, "
        "marketplace, dismissed FROM audits").fetchone()
    conn.close()
    assert row == ("QA-E1", "Folder_A", "fail", "Jane Q",
                   2, 1, 1, "R12", "DE", 0)


def test_index_verdict_matches_report_reconciliation(tmp_path: Path):
    e1 = tmp_path / "F" / "QA-E1"
    _write_audit(e1, verdict="fail", findings=[
        {"severity": "medium", "page": 1, "description": "evidence gap"},
    ])
    e2 = tmp_path / "F" / "QA-E2"
    _write_audit(e2, verdict="concerns", findings=[
        {"severity": "medium", "page": 4, "step_index": 1,
         "description": "page 4 shows Visa payment, which directly "
                        "contradicts the expected result of cash"},
    ])
    e3 = tmp_path / "F" / "QA-E3"
    _write_audit(e3, verdict="fail", findings=[
        {"severity": "high", "page": 2, "description": "real defect"},
    ])

    audit_index.sync(tmp_path)
    conn = sqlite3.connect(tmp_path / audit_index.DB_FILENAME)
    rows = dict(conn.execute("SELECT key, verdict FROM audits").fetchall())
    highs = dict(conn.execute("SELECT key, high FROM audits").fetchall())
    conn.close()

    assert rows == {"QA-E1": "concerns", "QA-E2": "fail",
                    "QA-E3": "fail"}
    assert highs["QA-E2"] == 1
    assert json.loads((e1 / "audit.json").read_text())[
        "overall_verdict"] == "fail"


def test_sync_detects_update_and_removal(tmp_path: Path):
    e1 = tmp_path / "F" / "QA-E1"
    e2 = tmp_path / "F" / "QA-E2"
    _write_audit(e1)
    _write_audit(e2)
    assert audit_index.sync(tmp_path)["added"] == 2

    time.sleep(0.01)
    _write_audit(e1, verdict="pass")
    (e1 / "audit.json").touch()
    (e2 / "audit.json").unlink()
    counts = audit_index.sync(tmp_path)
    assert counts["updated"] == 1 and counts["removed"] == 1

    conn = sqlite3.connect(tmp_path / audit_index.DB_FILENAME)
    rows = conn.execute("SELECT key, verdict FROM audits").fetchall()
    conn.close()
    assert rows == [("QA-E1", "pass")]


def test_sync_skips_replay_variants(tmp_path: Path):
    _write_audit(tmp_path / "F" / "QA-E1")
    _write_audit(tmp_path / "F" / "QA-E1" / "_replay" / "v4")
    assert audit_index.sync(tmp_path)["added"] == 1


def test_dismissed_flag_indexed(tmp_path: Path):
    _write_audit(tmp_path / "F" / "QA-E1",
                 acks=[{"type": "dismissed", "by": "op"}])
    audit_index.sync(tmp_path)
    conn = sqlite3.connect(tmp_path / audit_index.DB_FILENAME)
    (dismissed,) = conn.execute("SELECT dismissed FROM audits").fetchone()
    conn.close()
    assert dismissed == 1


def test_device_check_rules_are_indexed(tmp_path: Path):
    _write_audit(
        tmp_path / "F" / "QA-E19",
        findings=[{
            "severity": "high",
            "description": "proven device mismatch",
            "source": "device_check",
            "rule": "R19",
        }],
    )
    audit_index.sync(tmp_path)
    conn = sqlite3.connect(tmp_path / audit_index.DB_FILENAME)
    (rules,) = conn.execute(
        "SELECT rules FROM audits WHERE key='QA-E19'"
    ).fetchone()
    conn.close()
    assert rules == "R19"
