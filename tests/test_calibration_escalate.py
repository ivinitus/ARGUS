from __future__ import annotations

import json
import sys
from pathlib import Path

_TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(_TOOLS))
import calibration_verdicts  # noqa: E402


CONTRA_DESC = (
    "Step 1 requires cash payment but page 4 shows payment via Visa — "
    "the evidence directly contradicts the expected result."
)


def _write_audit(exec_dir: Path, verdict="concerns", findings=None,
                 key_meta=True) -> Path:
    exec_dir.mkdir(parents=True, exist_ok=True)
    audit = {
        "overall_verdict": verdict,
        "summary": "summary",
        "findings": findings if findings is not None else [
            {"severity": "medium", "page": 4, "step_index": 1,
             "description": CONTRA_DESC},
        ],
        "schema_version": 6,
        "variant_name": "v5",
    }
    (exec_dir / "audit.json").write_text(json.dumps(audit))
    if key_meta:
        (exec_dir / "metadata.json").write_text(json.dumps(
            {"test_results": [{"steps": [{"index": 1}]}]}))
    return exec_dir / "audit.json"


def _run(out_dir: Path, *args) -> int:
    return calibration_verdicts.main(
        ["--out-dir", str(out_dir), "escalate", *args])


def test_escalate_dry_run_writes_nothing(tmp_path, capsys):
    audit_path = _write_audit(tmp_path / "F" / "T" / "QA-E1")
    before = audit_path.read_text()

    assert _run(tmp_path) == 0
    out = capsys.readouterr().out
    assert "DRY-RUN QA-E1: concerns -> fail" in out
    assert "1 verdict flip(s)" in out
    assert audit_path.read_text() == before
    assert not (audit_path.parent / "audit.md").exists()


def test_escalate_apply_flips_verdict_and_rerenders_md(tmp_path):
    audit_path = _write_audit(tmp_path / "F" / "T" / "QA-E2")

    assert _run(tmp_path, "--apply") == 0
    audit = json.loads(audit_path.read_text())
    assert audit["overall_verdict"] == "fail"
    assert audit["_verdict_escalated_from"] == "concerns"
    f = audit["findings"][0]
    assert f["severity"] == "high"
    assert f["_severity_adjusted_from"] == "medium"
    assert f["action"] == "review_manually"
    md = (audit_path.parent / "audit.md").read_text()
    assert "**Auditor verdict:** Deviation" in md


def test_escalate_apply_is_idempotent(tmp_path, capsys):
    audit_path = _write_audit(tmp_path / "F" / "T" / "QA-E3")
    assert _run(tmp_path, "--apply") == 0
    after_first = audit_path.read_text()
    capsys.readouterr()

    assert _run(tmp_path, "--apply") == 0
    out = capsys.readouterr().out
    assert "0 audit(s) with escalations" in out
    assert audit_path.read_text() == after_first


def test_escalate_skips_unmatched_and_already_fail_counts(tmp_path, capsys):
    _write_audit(tmp_path / "F" / "T" / "QA-E4", findings=[
        {"severity": "medium", "page": 1,
         "description": "minor gap, nothing conclusive"},
    ])
    _write_audit(tmp_path / "F" / "T" / "QA-E5", verdict="fail",
                 findings=[
                     {"severity": "high", "page": 2, "description": "real"},
                     {"severity": "medium", "page": 4,
                      "description": CONTRA_DESC},
                 ])

    assert _run(tmp_path) == 0
    out = capsys.readouterr().out
    assert "QA-E4" not in out
    assert "QA-E5" in out and "(already fail)" in out
    assert "1 audit(s) with escalations (0 verdict flip(s)" in out


def test_escalate_skips_replay_and_dummy_runs(tmp_path, capsys):
    _write_audit(tmp_path / "F" / "T" / "QA-E6" / "_replay" / "v5")
    _write_audit(tmp_path / "Dummy_runs" / "T" / "QA-E7")

    assert _run(tmp_path) == 0
    out = capsys.readouterr().out
    assert "QA-E6" not in out
    assert "QA-E7" not in out
    assert "0 audit(s) with escalations" in out


def test_escalate_sample_prints_seeded_findings(tmp_path, capsys):
    for i in range(5):
        _write_audit(tmp_path / "F" / "T" / f"QA-E1{i}")

    assert _run(tmp_path, "--sample", "3", "--seed", "7") == 0
    out = capsys.readouterr().out
    assert "SAMPLE (3 of 5 matched findings, seed=7)" in out
    assert out.count("term=contradict") >= 3


def _run_demote(out_dir: Path, *args) -> int:
    return calibration_verdicts.main(
        ["--out-dir", str(out_dir), "demote", *args])


def _over_severe_findings() -> list[dict]:
    return [{"severity": "medium", "page": 2, "step_index": 1,
             "description": "evidence gap, nothing conclusive"}]


def test_demote_without_apply_writes_nothing(tmp_path, capsys):
    audit_path = _write_audit(tmp_path / "F" / "T" / "QA-E20",
                              verdict="fail",
                              findings=_over_severe_findings())
    before = audit_path.read_text()

    assert _run_demote(tmp_path) == 0
    out = capsys.readouterr().out
    assert "DRY-RUN QA-E20: fail -> concerns" in out
    assert "1 audit(s) would be demoted" in out
    assert audit_path.read_text() == before
    assert not (audit_path.parent / "audit.md").exists()


def test_demote_apply_updates_json_and_md(tmp_path, capsys):
    audit_path = _write_audit(tmp_path / "F" / "T" / "QA-E21",
                              verdict="fail",
                              findings=_over_severe_findings())

    assert _run_demote(tmp_path, "--apply") == 0
    audit = json.loads(audit_path.read_text())
    assert audit["overall_verdict"] == "concerns"
    assert audit["_verdict_demoted_from"] == "fail"
    md = (audit_path.parent / "audit.md").read_text()
    assert "**Auditor verdict:** Concerns" in md
    assert "1 audit(s) demoted" in capsys.readouterr().out


def test_demote_apply_is_idempotent(tmp_path, capsys):
    audit_path = _write_audit(tmp_path / "F" / "T" / "QA-E22",
                              verdict="fail",
                              findings=_over_severe_findings())
    assert _run_demote(tmp_path, "--apply") == 0
    after_first = audit_path.read_text()
    capsys.readouterr()

    assert _run_demote(tmp_path, "--apply") == 0
    out = capsys.readouterr().out
    assert "0 audit(s) demoted" in out
    assert audit_path.read_text() == after_first


def test_demote_marker_guard_skips_already_demoted_over_severe(tmp_path,
                                                               capsys):
    audit_path = _write_audit(tmp_path / "F" / "T" / "QA-E27",
                              verdict="fail",
                              findings=_over_severe_findings())
    audit = json.loads(audit_path.read_text())
    audit["_verdict_demoted_from"] = "fail"
    audit_path.write_text(json.dumps(audit))
    before = audit_path.read_text()

    assert _run_demote(tmp_path, "--apply") == 0
    out = capsys.readouterr().out
    assert "QA-E27" not in out
    assert "0 audit(s) demoted" in out
    assert audit_path.read_text() == before


def test_demote_leaves_fail_with_high_and_empty_findings_alone(tmp_path,
                                                               capsys):
    _write_audit(tmp_path / "F" / "T" / "QA-E23", verdict="fail",
                 findings=[{"severity": "high", "page": 1,
                            "description": "real defect"}])
    _write_audit(tmp_path / "F" / "T" / "QA-E24", verdict="fail",
                 findings=[])

    assert _run_demote(tmp_path) == 0
    out = capsys.readouterr().out
    assert "QA-E23" not in out
    assert "QA-E24" not in out
    assert "0 audit(s) would be demoted" in out


def test_demote_skips_stale_backups_and_dummy_runs(tmp_path, capsys):
    _write_audit(tmp_path / "_stale_backups" / "QA-E25_stale_20260724",
                 verdict="fail", findings=_over_severe_findings())
    _write_audit(tmp_path / "Dummy_runs" / "T" / "QA-E26",
                 verdict="fail", findings=_over_severe_findings())

    assert _run_demote(tmp_path) == 0
    out = capsys.readouterr().out
    assert "QA-E25" not in out
    assert "QA-E26" not in out
    assert "0 audit(s) would be demoted" in out
