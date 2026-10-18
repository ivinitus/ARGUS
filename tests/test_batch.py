"""Tests for the parallel batch runner."""
from __future__ import annotations

import io
import json
import threading
from pathlib import Path
from unittest import mock

import pytest

from argus.domain import workflow_rules
from argus.pipeline import auditor
from argus.pipeline import batch
from argus.pipeline import env_check_merge
from argus import config as argus_config
from argus.pipeline import extractor


def _settings(tmp_path: Path) -> argus_config.ArgusConfig:
    s = argus_config.ArgusConfig()
    s.extractor.out_dir = tmp_path / "out"
    s.auditor.env_check_engine = "off"
    return s


def _write_audit(work_dir: Path, verdict: str, n_findings: int) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "audit.json").write_text(json.dumps({
        "overall_verdict": verdict,
        "summary": "fake",
        "findings": [{"severity": "low"} for _ in range(n_findings)],
    }))


def test_read_keys_skips_blanks_and_comments(tmp_path: Path):
    p = tmp_path / "keys.txt"
    p.write_text("QA-E1\n\n# a comment\nQA-E2\n  QA-E1  \n")
    keys = batch.read_keys(None, p)
    assert keys == ["QA-E1", "QA-E2"]


def test_read_keys_from_stdin_iterable():
    lines = iter(["QA-E1\n", "\n", "# skip\n", "QA-E2\n"])
    assert batch.read_keys(lines, None) == ["QA-E1", "QA-E2"]


def _seed_screenshot(work_dir: Path) -> None:
    """Write a fake page image so `_has_any_evidence` sees the dir as populated."""
    shots = work_dir / "screenshots" / "sess1"
    shots.mkdir(parents=True, exist_ok=True)
    (shots / "page_001.jpg").write_bytes(b"fake-jpeg-bytes")


def _seed_step_attachment(work_dir: Path) -> None:
    """Write a fake step-attachment image."""
    step_dir = work_dir / "screenshots" / "step_attachments"
    step_dir.mkdir(parents=True, exist_ok=True)
    (step_dir / "step_003_42_screenshot.png").write_bytes(b"fake-png-bytes")


def test_run_batch_all_ok(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path)

    def fake_extract(cfg):
        wd = settings.extractor.out_dir / cfg.execution_key
        wd.mkdir(parents=True, exist_ok=True)
        _seed_screenshot(wd)
        return (0, wd)

    def fake_audit(cfg):
        _write_audit(cfg.execution_dir, "pass", 0)
        return 0

    monkeypatch.setattr(extractor, "run", fake_extract)
    monkeypatch.setattr(auditor, "run", fake_audit)

    keys = ["QA-E1", "QA-E2", "QA-E3"]
    buf = io.StringIO()
    progress = []
    extractions = []
    finalized = []
    results = batch.run_batch(
        keys,
        settings,
        concurrency=2,
        extractor_concurrency=2,
        out=buf,
        on_progress=lambda result, done, total:
        progress.append((result.key, done, total)),
        on_extraction_complete=lambda result, done, total:
        extractions.append((result.key, result.pages, done, total)),
        on_key_finalized=lambda result, done, total:
        finalized.append((result.key, done, total)),
    )

    assert len(results) == 3
    assert all(r.status == "ok" for r in results)
    assert {r.key for r in results} == set(keys)
    assert all(r.verdict == "pass" for r in results)
    assert all(r.findings == 0 for r in results)
    assert [done for _, done, _ in progress] == [1, 2, 3]
    assert all(total == 3 for _, _, total in progress)
    assert sorted((key, pages) for key, pages, _, _ in extractions) == [
        ("QA-E1", 1),
        ("QA-E2", 1),
        ("QA-E3", 1),
    ]
    assert sorted(done for _, done, _ in finalized) == [1, 2, 3]
    assert all(total == 3 for _, _, total in finalized)


def test_key_finalized_callback_sees_env_merged_cost(
        tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path)
    settings.auditor.env_check_engine = "haiku"

    def fake_extract(cfg):
        work_dir = settings.extractor.out_dir / cfg.execution_key
        _seed_screenshot(work_dir)
        return (0, work_dir)

    def fake_audit(cfg):
        _write_audit(cfg.execution_dir, "pass", 0)
        audit_path = cfg.execution_dir / "audit.json"
        audit = json.loads(audit_path.read_text())
        audit["cost"] = {"estimated_cost_usd": 0.1}
        audit_path.write_text(json.dumps(audit))
        return 0

    def fake_env_check(result, unused_settings):
        assert result.audit_path is not None
        audit = json.loads(result.audit_path.read_text())
        audit["cost"]["estimated_cost_usd"] = 0.25
        result.audit_path.write_text(json.dumps(audit))

    monkeypatch.setattr(extractor, "run", fake_extract)
    monkeypatch.setattr(auditor, "run", fake_audit)
    monkeypatch.setattr(batch, "_run_env_check_for_key", fake_env_check)
    finalized_costs = []

    batch.run_batch(
        ["QA-E1"],
        settings,
        concurrency=1,
        extractor_concurrency=1,
        out=io.StringIO(),
        on_key_finalized=lambda result, done, total:
        finalized_costs.append(json.loads(
            result.audit_path.read_text()
        )["cost"]["estimated_cost_usd"]),
    )

    assert finalized_costs == [0.25]


def test_run_batch_mixed_failures(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path)

    def fake_extract(cfg):
        wd = settings.extractor.out_dir / cfg.execution_key
        wd.mkdir(parents=True, exist_ok=True)
        if cfg.execution_key == "BAD-EXTRACT":
            return (1, None)
        _seed_screenshot(wd)
        return (0, wd)

    def fake_audit(cfg):
        if cfg.execution_dir.name == "BAD-AUDIT":
            return 1
        if cfg.execution_dir.name == "BOOM":
            raise RuntimeError("model exploded")
        _write_audit(cfg.execution_dir, "concerns", 2)
        return 0

    monkeypatch.setattr(extractor, "run", fake_extract)
    monkeypatch.setattr(auditor, "run", fake_audit)

    keys = ["OK-1", "BAD-EXTRACT", "BAD-AUDIT", "BOOM", "OK-2"]
    buf = io.StringIO()
    results = batch.run_batch(keys, settings, concurrency=3, extractor_concurrency=2, out=buf)

    by_key = {r.key: r for r in results}
    assert by_key["OK-1"].status == "ok"
    assert by_key["OK-2"].status == "ok"
    assert by_key["BAD-EXTRACT"].status == "analysis_incomplete"
    assert "extractor rc=1" in by_key["BAD-EXTRACT"].reason
    assert by_key["BAD-AUDIT"].status == "analysis_incomplete"
    assert "auditor rc=1" in by_key["BAD-AUDIT"].reason
    assert by_key["BOOM"].status == "analysis_incomplete"
    assert "RuntimeError" in by_key["BOOM"].reason
    for key in ("BAD-AUDIT", "BOOM"):
        audit = json.loads(by_key[key].audit_path.read_text())
        assert audit["analysis_status"] == "incomplete"


@pytest.mark.parametrize(
    ("error", "module"),
    [
        (
            RuntimeError("model API request rejected"),
            "model",
        ),
        (
            batch.model_client.ModelInterventionError("blocked"),
            "guardrail",
        ),
    ],
)
def test_run_batch_model_failures_are_incomplete(
    tmp_path: Path,
    monkeypatch,
    error,
    module: str,
):
    settings = _settings(tmp_path)

    def fake_extract(cfg):
        work_dir = settings.extractor.out_dir / cfg.execution_key
        _seed_screenshot(work_dir)
        return (0, work_dir)

    def fake_audit(cfg):
        raise error

    monkeypatch.setattr(extractor, "run", fake_extract)
    monkeypatch.setattr(auditor, "run", fake_audit)

    result = batch.run_batch(
        ["FAIL-1"],
        settings,
        concurrency=1,
        extractor_concurrency=1,
        out=io.StringIO(),
    )[0]

    assert result.status == "analysis_incomplete"
    assert result.reason.startswith(f"{module}:")
    audit = json.loads(result.audit_path.read_text())
    assert audit["analysis_status"] == "incomplete"
    assert audit["analysis_modules"][module]["status"] == "incomplete"
    failed_path = batch.write_failed_keys([result], settings.extractor.out_dir)
    assert failed_path.read_text() == "FAIL-1\n"


@pytest.mark.parametrize(
    "error",
    [
        batch.analysis_status.AnalysisIncompleteError(
            "environment",
            "environment evidence was ambiguous",
        ),
        RuntimeError("environment model unavailable"),
    ],
)
def test_run_batch_environment_failure_is_incomplete(
    tmp_path: Path,
    monkeypatch,
    error,
):
    settings = _settings(tmp_path)
    settings.auditor.env_check_engine = "haiku"

    def fake_extract(cfg):
        work_dir = settings.extractor.out_dir / cfg.execution_key
        _seed_screenshot(work_dir)
        return (0, work_dir)

    def fake_audit(cfg):
        _write_audit(cfg.execution_dir, "pass", 0)
        return 0

    def fake_env_check(result, unused_settings):
        raise error

    monkeypatch.setattr(extractor, "run", fake_extract)
    monkeypatch.setattr(auditor, "run", fake_audit)
    monkeypatch.setattr(batch, "_run_env_check_for_key", fake_env_check)

    result = batch.run_batch(
        ["ENV-1"],
        settings,
        concurrency=1,
        extractor_concurrency=1,
        out=io.StringIO(),
    )[0]

    assert result.status == "analysis_incomplete"
    assert result.reason.startswith("environment:")
    audit = json.loads(result.audit_path.read_text())
    assert audit["analysis_status"] == "incomplete"
    assert audit["analysis_modules"]["environment"]["status"] == "incomplete"


def test_existing_ambiguous_environment_result_is_completed_analysis(
        tmp_path: Path, monkeypatch):
    from argus.pipeline import env_check_model

    settings = _settings(tmp_path)
    settings.auditor.env_check_engine = "haiku"
    audit_path = tmp_path / "out" / "QA-E1" / "audit.json"
    audit_path.parent.mkdir(parents=True)
    audit_path.write_text(json.dumps({
        "overall_verdict": "concerns",
        "findings": [{
            "severity": "medium",
            "source": "env_check",
            "rule": "R9-amb",
        }],
        "env_check": {
            "version": env_check_model.ENV_CHECK_VERSION,
            "verdict": "ambiguous",
        },
        "device_check": {"version": 4},
    }))
    monkeypatch.setattr(
        env_check_merge,
        "write_merged_audit",
        lambda path, audit: path.write_text(json.dumps(audit)),
    )

    batch._run_env_check_for_key(
        batch.KeyResult(
            key="QA-E1",
            status="ok",
            audit_path=audit_path,
        ),
        settings,
        lock_held=True,
    )

    audit = json.loads(audit_path.read_text())
    assert audit["analysis_status"] == "complete"
    assert audit["analysis_modules"]["environment"] == {
        "status": "complete",
    }


def test_run_batch_auth_error(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path)

    def fake_extract(cfg):
        if cfg.execution_key == "AUTH-BAD":
            raise extractor.AuthError("PAT rejected")
        wd = settings.extractor.out_dir / cfg.execution_key
        wd.mkdir(parents=True, exist_ok=True)
        _seed_screenshot(wd)
        return (0, wd)

    def fake_audit(cfg):
        _write_audit(cfg.execution_dir, "pass", 0)
        return 0

    monkeypatch.setattr(extractor, "run", fake_extract)
    monkeypatch.setattr(auditor, "run", fake_audit)

    keys = ["OK-1", "AUTH-BAD", "OK-2"]
    buf = io.StringIO()
    results = batch.run_batch(keys, settings, concurrency=2, extractor_concurrency=2, out=buf)

    by_key = {r.key: r for r in results}
    assert by_key["AUTH-BAD"].status == "auth_failed"
    assert "PAT rejected" in by_key["AUTH-BAD"].reason
    assert by_key["OK-1"].status == "ok"
    assert by_key["OK-2"].status == "ok"


def test_extractor_concurrency_is_throttled(tmp_path: Path, monkeypatch):
    """Jira-side semaphore should cap simultaneous extract calls."""
    settings = _settings(tmp_path)
    in_flight = 0
    peak = 0
    lock = threading.Lock()
    gate = threading.Event()

    def fake_extract(cfg):
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        gate.wait(timeout=2.0)
        with lock:
            in_flight -= 1
        wd = settings.extractor.out_dir / cfg.execution_key
        wd.mkdir(parents=True, exist_ok=True)
        _seed_screenshot(wd)
        return (0, wd)

    def fake_audit(cfg):
        _write_audit(cfg.execution_dir, "pass", 0)
        return 0

    monkeypatch.setattr(extractor, "run", fake_extract)
    monkeypatch.setattr(auditor, "run", fake_audit)

    keys = [f"K-{i}" for i in range(8)]
    buf = io.StringIO()

    def release_soon():
        import time
        time.sleep(0.2)
        gate.set()

    t = threading.Thread(target=release_soon, daemon=True)
    t.start()

    results = batch.run_batch(keys, settings, concurrency=8, extractor_concurrency=3, out=buf)
    t.join()

    assert len(results) == 8
    assert peak <= 3, f"extractor peak concurrency was {peak}, expected <= 3"


def test_run_batch_duplicate_key_is_locked_and_skipped(
        tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path)
    calls = 0
    gate = threading.Event()

    def fake_extract(cfg):
        nonlocal calls
        calls += 1
        wd = settings.extractor.out_dir / cfg.execution_key
        wd.mkdir(parents=True, exist_ok=True)
        _seed_screenshot(wd)
        gate.wait(timeout=2.0)
        return (0, wd)

    def fake_audit(cfg):
        _write_audit(cfg.execution_dir, "pass", 0)
        return 0

    monkeypatch.setattr(extractor, "run", fake_extract)
    monkeypatch.setattr(auditor, "run", fake_audit)

    def release_soon():
        import time
        time.sleep(0.2)
        gate.set()

    t = threading.Thread(target=release_soon, daemon=True)
    t.start()
    results = batch.run_batch(
        ["QA-E1", "QA-E1"],
        settings,
        concurrency=2,
        extractor_concurrency=2,
        out=io.StringIO(),
    )
    t.join()

    assert calls == 1
    statuses = sorted(r.status for r in results)
    assert statuses == ["ok", "skipped"]
    locked = [r for r in results if r.status == "skipped"][0]
    assert "already auditing QA-E1" in (locked.reason or "")


def test_extractor_concurrency_clamp_is_reported(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path)

    def fake_extract(cfg):
        wd = settings.extractor.out_dir / cfg.execution_key
        wd.mkdir(parents=True, exist_ok=True)
        (wd / "screenshots").mkdir(parents=True, exist_ok=True)
        (wd / "screenshots" / "x").mkdir(parents=True, exist_ok=True)
        (wd / "screenshots" / "x" / "page_001.jpg").write_bytes(b"x")
        (wd / "metadata.json").write_text("{}")
        return (0, wd)

    def fake_audit(cfg):
        _write_audit(cfg.execution_dir, "pass", 0)
        return 0

    monkeypatch.setattr(extractor, "run", fake_extract)
    monkeypatch.setattr(auditor, "run", fake_audit)

    buf = io.StringIO()
    batch.run_batch(["K-1"], settings, concurrency=2, extractor_concurrency=10, out=buf)
    output = buf.getvalue()
    assert "clamped" in output
    assert "extractor_concurrency=10" in output
    assert " to 2 " in output


def test_extractor_concurrency_no_log_when_already_valid(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path)

    def fake_extract(cfg):
        wd = settings.extractor.out_dir / cfg.execution_key
        wd.mkdir(parents=True, exist_ok=True)
        (wd / "screenshots").mkdir(parents=True, exist_ok=True)
        (wd / "screenshots" / "x").mkdir(parents=True, exist_ok=True)
        (wd / "screenshots" / "x" / "page_001.jpg").write_bytes(b"x")
        (wd / "metadata.json").write_text("{}")
        return (0, wd)

    def fake_audit(cfg):
        _write_audit(cfg.execution_dir, "pass", 0)
        return 0

    monkeypatch.setattr(extractor, "run", fake_extract)
    monkeypatch.setattr(auditor, "run", fake_audit)

    buf = io.StringIO()
    batch.run_batch(["K-1"], settings, concurrency=4, extractor_concurrency=2, out=buf)
    assert "clamped" not in buf.getvalue()


def test_write_failed_keys(tmp_path: Path):
    results = [
        batch.KeyResult(key="A", status="ok", verdict="pass", findings=0),
        batch.KeyResult(key="B", status="failed", reason="x"),
        batch.KeyResult(key="C", status="auth_failed", reason="y"),
        batch.KeyResult(key="D", status="skipped", reason="no PDF or image attachments"),
        batch.KeyResult(key="E", status="analysis_incomplete", reason="environment"),
    ]
    path = batch.write_failed_keys(results, tmp_path / "out")
    assert path is not None
    lines = path.read_text().splitlines()
    assert lines == ["B", "C", "E"]


def test_write_failed_keys_none(tmp_path: Path):
    results = [
        batch.KeyResult(key="A", status="ok"),
        batch.KeyResult(key="B", status="skipped", reason="no PDF or image attachments"),
    ]
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    stale_marker = out_dir / "_failed_keys.txt"
    stale_marker.write_text("OLD-FAILURE\n")

    assert batch.write_failed_keys(results, out_dir) is None
    assert not stale_marker.exists()


def test_summarize():
    results = [
        batch.KeyResult(key="A", status="ok"),
        batch.KeyResult(key="B", status="ok"),
        batch.KeyResult(key="C", status="failed"),
        batch.KeyResult(key="D", status="auth_failed"),
        batch.KeyResult(key="E", status="skipped"),
        batch.KeyResult(key="F", status="skipped"),
    ]
    assert batch.summarize(results) == (
        "2 succeeded, 2 skipped, 1 failed, 1 auth-failed"
    )


def test_summarize_hides_skipped_when_zero():
    results = [
        batch.KeyResult(key="A", status="ok"),
        batch.KeyResult(key="B", status="failed"),
    ]
    assert batch.summarize(results) == "1 succeeded, 1 failed, 0 auth-failed"


def _write_metadata(work_dir: Path, tester_name: str | None) -> None:
    """Fake what extractor.run() writes to metadata.json, just enough for `_read_tester` to pull the tester name out."""
    tr: dict = {"executed_by": "JIRAUSER00001"}
    if tester_name is not None:
        tr["executed_by_name"] = tester_name
    (work_dir / "metadata.json").write_text(json.dumps({"test_results": [tr]}))


def test_run_batch_marks_incomplete_when_no_screenshots(
        tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path)

    audit_called_for: list[str] = []

    def fake_extract(cfg):
        wd = settings.extractor.out_dir / cfg.execution_key
        wd.mkdir(parents=True, exist_ok=True)
        _write_metadata(wd, "Prathap B")
        if cfg.execution_key == "WITH-SHOTS":
            _seed_screenshot(wd)
        return (0, wd)

    def fake_audit(cfg):
        audit_called_for.append(cfg.execution_dir.name)
        _write_audit(cfg.execution_dir, "pass", 0)
        return 0

    monkeypatch.setattr(extractor, "run", fake_extract)
    monkeypatch.setattr(auditor, "run", fake_audit)

    keys = ["NO-SHOTS", "WITH-SHOTS"]
    buf = io.StringIO()
    results = batch.run_batch(keys, settings, concurrency=2, extractor_concurrency=2, out=buf)

    by_key = {r.key: r for r in results}
    assert by_key["NO-SHOTS"].status == "analysis_incomplete"
    assert by_key["NO-SHOTS"].reason == (
        "no PDF, image, or usable video evidence"
    )
    assert by_key["NO-SHOTS"].tester == "Prathap B"
    assert by_key["WITH-SHOTS"].status == "ok"
    assert by_key["WITH-SHOTS"].tester == "Prathap B"
    assert audit_called_for == ["WITH-SHOTS"]
    incomplete = json.loads(by_key["NO-SHOTS"].audit_path.read_text())
    assert incomplete["analysis_status"] == "incomplete"
    assert incomplete["analysis_modules"]["screenshots"]["status"] == "incomplete"


def test_run_batch_audits_step_attachments_without_pdf(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path)
    audit_called_for: list[str] = []

    def fake_extract(cfg):
        wd = settings.extractor.out_dir / cfg.execution_key
        wd.mkdir(parents=True, exist_ok=True)
        _write_metadata(wd, "Rishekesavan K")
        _seed_step_attachment(wd)
        return (0, wd)

    def fake_audit(cfg):
        audit_called_for.append(cfg.execution_dir.name)
        _write_audit(cfg.execution_dir, "pass", 0)
        return 0

    monkeypatch.setattr(extractor, "run", fake_extract)
    monkeypatch.setattr(auditor, "run", fake_audit)

    buf = io.StringIO()
    results = batch.run_batch(["STEP-ATTACH-ONLY"], settings,
                              concurrency=1, extractor_concurrency=1,
                              out=buf)

    r = results[0]
    assert r.status == "ok", (
        f"key with step attachments must audit, not skip as "
        f"no-screenshots. Got status={r.status!r} reason={r.reason!r}"
    )
    assert audit_called_for == ["STEP-ATTACH-ONLY"]


def test_run_batch_audits_mp4_evidence(
    tmp_path: Path,
    monkeypatch,
):
    settings = _settings(tmp_path)
    audit_called_for: list[str] = []

    def fake_extract(cfg):
        wd = settings.extractor.out_dir / cfg.execution_key
        wd.mkdir(parents=True, exist_ok=True)
        (wd / "metadata.json").write_text(json.dumps({
            "test_results": [{
                "executed_by_name": "Harini M",
                "attachments": [{
                    "id": 1109069,
                    "name": "video-1788506690548.mp4",
                    "size": 39838260,
                }],
                "steps": [],
            }],
        }))
        video_dir = wd / "video_evidence"
        video_dir.mkdir()
        (video_dir / "top_1109069.mp4").write_bytes(
            b"\x00\x00\x00\x18ftypmp42"
        )
        (wd / "video_evidence.json").write_text(json.dumps({
            "videos": [{
                "id": 1109069,
                "name": "video-1788506690548.mp4",
                "format": "mp4",
                "path": "video_evidence/top_1109069.mp4",
            }],
        }))
        return (0, wd)

    def fake_audit(cfg):
        audit_called_for.append(cfg.execution_dir.name)
        _write_audit(cfg.execution_dir, "pass", 0)
        return 0

    monkeypatch.setattr(extractor, "run", fake_extract)
    monkeypatch.setattr(auditor, "run", fake_audit)

    results = batch.run_batch(
        ["MOBILE-E1924716"],
        settings,
        concurrency=1,
        extractor_concurrency=1,
        out=io.StringIO(),
    )

    result = results[0]
    assert result.status == "ok"
    assert audit_called_for == ["MOBILE-E1924716"]


def test_unsupported_evidence_audit_keeps_tester_without_metadata(tmp_path):
    work_dir = tmp_path / "MOBILE-E1924716"
    work_dir.mkdir()

    batch._write_unsupported_evidence_audit(
        work_dir,
        "MOBILE-E1924716",
        "Harini M",
        [{"id": 1109069, "name": "recording.mov", "size": 39838260}],
    )

    audit = json.loads((work_dir / "audit.json").read_text())
    assert audit["tester"] == "Harini M"


def test_unsupported_evidence_audit_escalates_high_rule_finding(
    tmp_path,
    monkeypatch,
):
    work_dir = tmp_path / "MOBILE-E1924716"
    work_dir.mkdir()
    (work_dir / "metadata.json").write_text(json.dumps({
        "test_results": [{}],
    }))
    monkeypatch.setattr(
        workflow_rules,
        "run_workflow_rules",
        lambda metadata: [{
            "severity": "high",
            "description": "A deterministic workflow violation was detected.",
            "source": "rule",
            "rule": "R99",
            "category": "process_issue",
            "action": "review_manually",
            "confidence": "high",
        }],
    )

    batch._write_unsupported_evidence_audit(
        work_dir,
        "MOBILE-E1924716",
        "Harini M",
        [{"id": 1109069, "name": "recording.mov", "size": 39838260}],
    )

    audit = json.loads((work_dir / "audit.json").read_text())
    assert audit["overall_verdict"] == "fail"
    assert audit["_verdict_escalated_from"] == "concerns"


def test_has_any_evidence_direct_cases(tmp_path: Path):
    wd = tmp_path / "exec"
    wd.mkdir()
    assert batch._has_any_evidence(wd) is False

    (wd / "screenshots").mkdir()
    assert batch._has_any_evidence(wd) is False

    _seed_screenshot(wd)
    assert batch._has_any_evidence(wd) is True

    wd2 = tmp_path / "exec-step-only"
    wd2.mkdir()
    _seed_step_attachment(wd2)
    assert batch._has_any_evidence(wd2) is True

    wd_video = tmp_path / "exec-video-only"
    video_dir = wd_video / "video_evidence"
    video_dir.mkdir(parents=True)
    (video_dir / "top_1109069.mp4").write_bytes(b"video")
    (wd_video / "video_evidence.json").write_text(json.dumps({
        "videos": [{
            "format": "mp4",
            "path": "video_evidence/top_1109069.mp4",
        }],
    }))
    assert batch._has_any_evidence(wd_video) is True

    wd3 = tmp_path / "exec-only-txt"
    wd3.mkdir()
    step_dir = wd3 / "screenshots" / "step_attachments"
    step_dir.mkdir(parents=True)
    (step_dir / "note.txt").write_bytes(b"readme")
    assert batch._has_any_evidence(wd3) is False


def test_format_skipped_by_tester_groups_largest_first():
    results = [
        batch.KeyResult(key="E1", status="skipped", tester="Alice"),
        batch.KeyResult(key="E2", status="skipped", tester="Alice"),
        batch.KeyResult(key="E3", status="skipped", tester="Alice"),
        batch.KeyResult(key="E4", status="skipped", tester="Bob"),
        batch.KeyResult(key="E5", status="skipped", tester=None),
        batch.KeyResult(key="E6", status="ok", tester="Carol"),
        batch.KeyResult(key="E7", status="failed", tester="Dave"),
    ]
    out = batch.format_skipped_by_tester(results)
    assert out is not None
    lines = out.splitlines()
    assert lines[0] == "needs evidence (tester didn't attach any PDF or image):"
    assert lines[1].startswith("  Alice (3):")
    assert "E1" in lines[1] and "E2" in lines[1] and "E3" in lines[1]
    assert lines[2].startswith("  Bob (1):")
    assert lines[3].startswith("  unknown (1):")


def test_format_skipped_by_tester_returns_none_when_nothing_skipped():
    results = [
        batch.KeyResult(key="A", status="ok"),
        batch.KeyResult(key="B", status="failed"),
    ]
    assert batch.format_skipped_by_tester(results) is None


def test_config_batch_defaults():
    cfg = argus_config.ArgusConfig()
    assert cfg.batch.concurrency == 16
    assert cfg.batch.extractor_concurrency == 4


def test_env_check_workers_are_bounded_below_connection_pool_capacity():
    assert batch._env_check_worker_count(1) == 1
    assert batch._env_check_worker_count(4) == 4
    assert batch._env_check_worker_count(16) == 8
    assert batch._env_check_worker_count(128) == 8


def test_haiku_device_only_post_checks_keep_bounded_worker_pool():
    settings = argus_config.ArgusConfig()
    settings.policy.environment_check_enabled = False
    settings.policy.enabled_rules = ["R19"]
    settings.auditor.env_check_engine = "haiku"

    assert batch._post_check_worker_count(settings, 16) == 8

    settings.auditor.env_check_engine = "off"
    assert batch._post_check_worker_count(settings, 16) == 8

    settings.auditor.env_check_engine = "tesseract"
    assert batch._post_check_worker_count(settings, 16) == 8


def test_device_post_check_remains_enabled_when_environment_is_disabled():
    settings = argus_config.ArgusConfig()
    settings.policy.environment_check_enabled = False
    settings.policy.enabled_rules = ["R0", "R19"]

    assert batch._post_checks_enabled(settings) is True

    settings.policy.enabled_rules = ["R0"]
    assert batch._post_checks_enabled(settings) is False


@pytest.mark.parametrize("profile", ["tablet", "tv", "reader", "mobile"])
def test_device_profiles_skip_environment_analysis(
        tmp_path: Path, monkeypatch, profile: str):
    from argus.pipeline import device_check, env_check_model

    settings = argus_config.load(profile=profile)
    assert settings.policy.environment_check_enabled is False
    assert settings.auditor.env_check_engine == "off"
    assert settings.auditor.env_check_inline is False
    assert "R9" not in settings.policy.enabled_rules

    _write_audit(tmp_path, "pass", 0)
    environment = mock.Mock(side_effect=AssertionError(
        "device profiles must not run environment analysis"))
    device = mock.Mock(return_value={
        "version": device_check.DEVICE_CHECK_VERSION,
        "findings": [],
    })
    monkeypatch.setattr(
        env_check_model, "check_env_compliance", environment)
    monkeypatch.setattr(
        device_check, "evaluate_device_compliance", device)

    batch._run_env_check_for_key_locked(settings, tmp_path / "audit.json")

    environment.assert_not_called()
    audit = json.loads((tmp_path / "audit.json").read_text())
    assert "env_check" not in audit
    if profile in {"mobile", "reader"}:
        device.assert_not_called()
    else:
        device.assert_called_once()
        assert audit["analysis_modules"]["environment"]["status"] == (
            "not_applicable")


def test_policy_rule_gate_uses_shared_parent_family_parser(tmp_path):
    settings = _settings(tmp_path)
    settings.policy.enabled_rules = ["R2"]

    assert batch._policy_rule_enabled(settings, "R2B22") is True
    assert batch._policy_rule_enabled(settings, "R14a") is False


def test_env_check_reuses_callers_existing_key_lock(tmp_path, monkeypatch):
    from argus.pipeline import env_check_model

    settings = _settings(tmp_path)
    settings.auditor.env_check_engine = "haiku"
    audit_path = tmp_path / "QA-E1" / "audit.json"
    audit_path.parent.mkdir()
    audit_path.write_text(json.dumps({
        "overall_verdict": "pass",
        "findings": [],
        "env_check": {
            "version": env_check_model.ENV_CHECK_VERSION,
            "verdict": "compliant",
        },
        "device_check": {"version": 4},
        "analysis_modules": {
            "environment": {"status": "not_applicable"},
        },
    }))
    acquire = mock.MagicMock()
    monkeypatch.setattr(batch.audit_lock, "acquire_key", acquire)

    batch._run_env_check_for_key(
        batch.KeyResult(
            key="QA-E1",
            status="ok",
            audit_path=audit_path,
        ),
        settings,
        lock_held=True,
    )

    acquire.assert_not_called()
    audit = json.loads(audit_path.read_text())
    assert audit["analysis_status"] == "complete"
    assert audit["analysis_modules"]["environment"] == {
        "status": "complete",
    }


def test_config_batch_from_file(tmp_path: Path):
    p = tmp_path / "c.toml"
    p.write_text('[batch]\nconcurrency = 12\nextractor_concurrency = 6\n')
    cfg = argus_config.load(p)
    assert cfg.batch.concurrency == 12
    assert cfg.batch.extractor_concurrency == 6


def _blocked_work_dir(tmp_path: Path, e_key: str) -> Path:
    """Create a work_dir with the production 4-level layout."""
    work_dir = tmp_path / "out" / "Folder" / "Tester" / e_key
    work_dir.mkdir(parents=True)
    return work_dir


def _write_status_metadata(work_dir: Path, status: str, *,
                           tc_key: str = "QA-T99",
                           comment: str = "",
                           trace_links: list | None = None) -> None:
    (work_dir / "metadata.json").write_text(json.dumps({
        "test_results": [{
            "status": status,
            "test_case_key": tc_key,
            "marketplace": "US",
            "executed_by": "JIRAUSER00001",
            "executed_by_name": "Prathap B",
            "comment": comment,
            "trace_links": trace_links or [],
            "steps": [],
        }]
    }))


def test_write_blocked_audit_undocumented_is_high_concerns(tmp_path: Path):
    work_dir = _blocked_work_dir(tmp_path, "QA-E1")
    _write_status_metadata(work_dir, "Blocked", tc_key="QA-T91")
    batch._write_blocked_audit(work_dir, "QA-E1")
    audit = json.loads((work_dir / "audit.json").read_text())
    assert audit["variant_name"] == "blocked"
    assert audit["overall_verdict"] == "concerns"
    assert audit["analysis_status"] == "complete"
    assert audit["analysis_modules"]["screenshots"] == {
        "status": "not_applicable",
    }
    r20 = [f for f in audit["findings"] if f.get("rule") == "R20"]
    assert len(r20) == 1 and r20[0]["severity"] == "high"
    assert not any(f.get("rule") == "R12" for f in audit["findings"])
    assert (work_dir / "audit.md").exists()


def test_write_blocked_audit_documented_is_medium_concerns(tmp_path: Path):
    work_dir = _blocked_work_dir(tmp_path, "QA-E2")
    _write_status_metadata(work_dir, "Blocked", tc_key="QA-T92",
                           comment="env down, tracked in TESLA-123")
    batch._write_blocked_audit(work_dir, "QA-E2")
    audit = json.loads((work_dir / "audit.json").read_text())
    assert audit["variant_name"] == "blocked"
    assert audit["overall_verdict"] == "concerns"
    assert audit["analysis_status"] == "complete"
    r20 = [f for f in audit["findings"] if f.get("rule") == "R20"]
    assert len(r20) == 1 and r20[0]["severity"] == "medium"


def test_write_blocked_audit_in_progress_variant(tmp_path: Path):
    work_dir = _blocked_work_dir(tmp_path, "QA-E3")
    _write_status_metadata(work_dir, "In Progress", tc_key="QA-T93")
    batch._write_blocked_audit(work_dir, "QA-E3")
    audit = json.loads((work_dir / "audit.json").read_text())
    assert audit["variant_name"] == "in-progress"
    assert audit["overall_verdict"] == "fail"
    assert audit["analysis_status"] == "complete"
    assert audit["analysis_modules"]["screenshots"] == {
        "status": "not_applicable",
    }
    r21 = [f for f in audit["findings"] if f.get("rule") == "R21"]
    assert len(r21) == 1 and r21[0]["severity"] == "high"


def test_post_checks_finalize_during_audit_phase_in_completion_order(
        tmp_path: Path, monkeypatch):
    """A slow post-check must not hold back keys that finish after it."""
    import time as _time

    settings = _settings(tmp_path)
    settings.auditor.env_check_engine = "haiku"
    extraction_end: dict[str, float] = {}

    def fake_extract(cfg):
        if cfg.execution_key == "SLOW-EXTRACT":
            _time.sleep(0.6)
        work_dir = settings.extractor.out_dir / cfg.execution_key
        _seed_screenshot(work_dir)
        extraction_end[cfg.execution_key] = _time.monotonic()
        return (0, work_dir)

    def fake_audit(cfg):
        _write_audit(cfg.execution_dir, "pass", 0)
        return 0

    def fake_env_check(result, unused_settings):
        if result.key == "SLOW-ENV":
            _time.sleep(1.0)

    monkeypatch.setattr(extractor, "run", fake_extract)
    monkeypatch.setattr(auditor, "run", fake_audit)
    monkeypatch.setattr(batch, "_run_env_check_for_key", fake_env_check)

    finalized: list[tuple[str, int, float, str]] = []

    def on_key_finalized(result, done, total):
        finalized.append((
            result.key, done, _time.monotonic(),
            threading.current_thread().name,
        ))

    buf = io.StringIO()
    results = batch.run_batch(
        ["SLOW-ENV", "FAST", "SLOW-EXTRACT"],
        settings,
        concurrency=2,
        extractor_concurrency=2,
        out=buf,
        on_key_finalized=on_key_finalized,
    )

    assert all(r.status == "ok" for r in results)
    assert [key for key, _, _, _ in finalized] == [
        "FAST", "SLOW-EXTRACT", "SLOW-ENV",
    ]
    assert [done for _, done, _, _ in finalized] == [1, 2, 3]
    # FAST was finalized while SLOW-EXTRACT was still being extracted, i.e.
    # during the audit phase rather than in a drain after the last audit.
    assert finalized[0][2] < extraction_end["SLOW-EXTRACT"]
    assert all(name.startswith("post_checks") for *_, name in finalized)
    assert "env_check completed for 3/3 keys" in buf.getvalue()


def test_keyboard_interrupt_cancels_queued_keys_promptly(
        tmp_path: Path, monkeypatch):
    import time as _time

    settings = _settings(tmp_path)
    processed: list[str] = []
    processed_lock = threading.Lock()

    def fake_extract(cfg):
        _time.sleep(0.05)
        with processed_lock:
            processed.append(cfg.execution_key)
        work_dir = settings.extractor.out_dir / cfg.execution_key
        _seed_screenshot(work_dir)
        return (0, work_dir)

    def fake_audit(cfg):
        _write_audit(cfg.execution_dir, "pass", 0)
        return 0

    def interrupt(result, done, total):
        raise KeyboardInterrupt

    monkeypatch.setattr(extractor, "run", fake_extract)
    monkeypatch.setattr(auditor, "run", fake_audit)

    keys = [f"K-{i}" for i in range(8)]
    buf = io.StringIO()
    started = _time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        batch.run_batch(
            keys, settings,
            concurrency=1, extractor_concurrency=1,
            out=buf, on_progress=interrupt,
        )
    elapsed = _time.monotonic() - started
    _time.sleep(0.3)  # let the single in-flight key drain
    # The interrupted key plus at most the one already in flight; the six
    # queued keys were cancelled instead of being processed.
    assert len(processed) <= 2
    assert elapsed < 2.0
    assert "interrupted" in buf.getvalue()


def test_batch_shares_one_pooled_jira_session_across_keys(
        tmp_path: Path, monkeypatch):
    import requests

    settings = _settings(tmp_path)
    built: list[requests.Session] = []
    seen: list[object] = []
    seen_lock = threading.Lock()

    def fake_build_session(extractor_settings):
        assert extractor_settings is settings.extractor
        session = requests.Session()
        built.append(session)
        return session

    def fake_extract(cfg):
        with seen_lock:
            seen.append(cfg.session)
        work_dir = settings.extractor.out_dir / cfg.execution_key
        _seed_screenshot(work_dir)
        return (0, work_dir)

    def fake_audit(cfg):
        _write_audit(cfg.execution_dir, "pass", 0)
        return 0

    monkeypatch.setattr(extractor, "build_session", fake_build_session)
    monkeypatch.setattr(extractor, "run", fake_extract)
    monkeypatch.setattr(auditor, "run", fake_audit)

    results = batch.run_batch(
        ["S-1", "S-2", "S-3"], settings,
        concurrency=3, extractor_concurrency=3, out=io.StringIO(),
    )

    assert all(r.status == "ok" for r in results)
    assert len(built) == 1
    assert seen == [built[0]] * 3
    adapter = built[0].get_adapter("https://jira.example/rest")
    assert adapter._pool_maxsize == 6  # extractor_concurrency * 2


def test_batch_falls_back_to_per_key_session_when_shared_build_fails(
        tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path)
    seen: list[object] = []

    def failing_build_session(extractor_settings):
        raise RuntimeError("secrets manager unavailable")

    def fake_extract(cfg):
        seen.append(cfg.session)
        work_dir = settings.extractor.out_dir / cfg.execution_key
        _seed_screenshot(work_dir)
        return (0, work_dir)

    def fake_audit(cfg):
        _write_audit(cfg.execution_dir, "pass", 0)
        return 0

    monkeypatch.setattr(extractor, "build_session", failing_build_session)
    monkeypatch.setattr(extractor, "run", fake_extract)
    monkeypatch.setattr(auditor, "run", fake_audit)

    results = batch.run_batch(
        ["F-1", "F-2"], settings,
        concurrency=2, extractor_concurrency=2, out=io.StringIO(),
    )

    assert all(r.status == "ok" for r in results)
    assert seen == [None, None]
