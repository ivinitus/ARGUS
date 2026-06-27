"""Tests for the parallel batch runner.

extractor.run and auditor.run are monkeypatched to canned behavior so no
network / boto3 / PyMuPDF is involved.
"""
from __future__ import annotations

import io
import json
import threading
from pathlib import Path

import pytest

import auditor
import batch
import config as argus_config
import extractor


def _settings(tmp_path: Path) -> argus_config.ARGUSConfig:
    s = argus_config.ARGUSConfig()
    s.extractor.out_dir = tmp_path / "out"
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
    # Duplicates deduped, comments and blanks dropped, stripped.
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
    """Write a fake step-attachment image. Simulates the mobile-playback
    tester pattern where the tester pastes phone screenshots instead
    of attaching a Session PDF — regression fixture for the case that
    was silently being skipped by the old `_has_page_images`."""
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
    results = batch.run_batch(keys, settings, concurrency=2, extractor_concurrency=2, out=buf)

    assert len(results) == 3
    assert all(r.status == "ok" for r in results)
    assert {r.key for r in results} == set(keys)
    assert all(r.verdict == "pass" for r in results)
    assert all(r.findings == 0 for r in results)


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
    assert by_key["BAD-EXTRACT"].status == "failed"
    assert "extractor rc=1" in by_key["BAD-EXTRACT"].reason
    assert by_key["BAD-AUDIT"].status == "failed"
    assert "auditor rc=1" in by_key["BAD-AUDIT"].reason
    assert by_key["BOOM"].status == "failed"
    assert "RuntimeError" in by_key["BOOM"].reason


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
    # Other keys still complete (skip-and-continue, not global abort).
    assert by_key["OK-1"].status == "ok"
    assert by_key["OK-2"].status == "ok"


def test_extractor_concurrency_is_throttled(tmp_path: Path, monkeypatch):
    """Tracker-side semaphore should cap simultaneous extract calls."""
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
        # Hold inside the extractor until the test releases the gate.
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
        # Give workers time to pile up against the semaphore, then release.
        import time
        time.sleep(0.2)
        gate.set()

    t = threading.Thread(target=release_soon, daemon=True)
    t.start()

    results = batch.run_batch(keys, settings, concurrency=8, extractor_concurrency=3, out=buf)
    t.join()

    assert len(results) == 8
    assert peak <= 3, f"extractor peak concurrency was {peak}, expected <= 3"


def test_extractor_concurrency_clamp_is_reported(tmp_path: Path, monkeypatch):
    """When the caller requests extractor_concurrency > concurrency, the
    effective value gets clamped and a notice is printed to the progress
    stream so the operator doesn't see a different effective value than
    what was logged upstream."""
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
    # Requested extractor=10, concurrency=2 — must clamp to 2.
    batch.run_batch(["K-1"], settings, concurrency=2, extractor_concurrency=10, out=buf)
    output = buf.getvalue()
    assert "clamped" in output
    assert "extractor_concurrency=10" in output
    assert " to 2 " in output


def test_extractor_concurrency_no_log_when_already_valid(tmp_path: Path, monkeypatch):
    """No clamp notice when the requested value is already in range — we
    only add output when there's something worth surfacing."""
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
        # skipped must NOT land in the retry file.
        batch.KeyResult(key="D", status="skipped", reason="no PDF or image attachments"),
    ]
    path = batch.write_failed_keys(results, tmp_path / "out")
    assert path is not None
    lines = path.read_text().splitlines()
    assert lines == ["B", "C"]


def test_write_failed_keys_none(tmp_path: Path):
    # Only ok + skipped = no retry file.
    results = [
        batch.KeyResult(key="A", status="ok"),
        batch.KeyResult(key="B", status="skipped", reason="no PDF or image attachments"),
    ]
    assert batch.write_failed_keys(results, tmp_path / "out") is None


def test_summarize():
    results = [
        batch.KeyResult(key="A", status="ok"),
        batch.KeyResult(key="B", status="ok"),
        batch.KeyResult(key="C", status="failed"),
        batch.KeyResult(key="D", status="auth_failed"),
        batch.KeyResult(key="E", status="skipped"),
        batch.KeyResult(key="F", status="skipped"),
    ]
    assert batch.summarize(results) == "2 succeeded, 2 skipped, 1 failed, 1 auth-failed"


def test_summarize_hides_skipped_when_zero():
    results = [
        batch.KeyResult(key="A", status="ok"),
        batch.KeyResult(key="B", status="failed"),
    ]
    # No "0 skipped" noise in the summary when nothing was skipped.
    assert batch.summarize(results) == "1 succeeded, 1 failed, 0 auth-failed"


def _write_metadata(work_dir: Path, tester_name: str | None) -> None:
    """Fake what extractor.run() writes to metadata.json, just enough for
    `_read_tester` to pull the tester name out."""
    tr: dict = {"executed_by": "USER00001"}
    if tester_name is not None:
        tr["executed_by_name"] = tester_name
    (work_dir / "metadata.json").write_text(json.dumps({"test_results": [tr]}))


def test_run_batch_skips_when_no_screenshots(tmp_path: Path, monkeypatch):
    """Extractor succeeds but writes no page images (tester didn't attach a
    PDF). Those keys should be `skipped`, not `failed`, the auditor should
    NOT be called, and the tester name should be read from metadata.json."""
    settings = _settings(tmp_path)

    audit_called_for: list[str] = []

    def fake_extract(cfg):
        wd = settings.extractor.out_dir / cfg.execution_key
        wd.mkdir(parents=True, exist_ok=True)
        _write_metadata(wd, "Prathap B")
        if cfg.execution_key == "WITH-SHOTS":
            _seed_screenshot(wd)
        # NO-SHOTS: create work dir but no screenshots/ (common real-world case).
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
    assert by_key["NO-SHOTS"].status == "skipped"
    assert by_key["NO-SHOTS"].reason == "no PDF or image attachments"
    assert by_key["NO-SHOTS"].tester == "Prathap B"
    assert by_key["WITH-SHOTS"].status == "ok"
    assert by_key["WITH-SHOTS"].tester == "Prathap B"
    # Short-circuit: auditor was never invoked for the skipped key.
    assert audit_called_for == ["WITH-SHOTS"]


def test_run_batch_audits_step_attachments_without_pdf(tmp_path: Path, monkeypatch):
    """Regression: mobile / webview testers often paste phone screenshots
    as step attachments rather than exporting a Session PDF. Those show
    up under screenshots/step_attachments/<file>.{png,jpg,...} and the
    auditor handles them cleanly (via load_step_attachment_images),
    but the old `_has_page_images` pre-filter only looked for
    page_*.jpg / page_*.png and silently skipped the key with 'no
    screenshots'. Observed in Targeted_regression_-08_May_2026(WEB)
    on 6 mobile playback keys from Rishekesavan K and Yugesh L.

    The fix renames the filter to `_has_any_evidence` and widens it
    to count step_attachments images as valid audit input.
    """
    settings = _settings(tmp_path)
    audit_called_for: list[str] = []

    def fake_extract(cfg):
        wd = settings.extractor.out_dir / cfg.execution_key
        wd.mkdir(parents=True, exist_ok=True)
        _write_metadata(wd, "Rishekesavan K")
        # STEP-ATTACH: no PDF, but step_attachments has real images —
        # should audit, not skip.
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


def test_has_any_evidence_direct_cases(tmp_path: Path):
    """Direct unit test for the predicate so the widening isn't only
    locked in by the run_batch integration test."""
    wd = tmp_path / "exec"
    wd.mkdir()
    # No screenshots/ at all.
    assert batch._has_any_evidence(wd) is False

    # Empty screenshots/ dir.
    (wd / "screenshots").mkdir()
    assert batch._has_any_evidence(wd) is False

    # PDF-split page image — the historical True case.
    _seed_screenshot(wd)
    assert batch._has_any_evidence(wd) is True

    # Fresh dir, step attachment only — the regression case.
    wd2 = tmp_path / "exec-step-only"
    wd2.mkdir()
    _seed_step_attachment(wd2)
    assert batch._has_any_evidence(wd2) is True

    # Non-image in step_attachments does NOT count (e.g. a stray .txt).
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
        batch.KeyResult(key="E5", status="skipped", tester=None),  # unknown
        batch.KeyResult(key="E6", status="ok", tester="Carol"),    # excluded
        batch.KeyResult(key="E7", status="failed", tester="Dave"), # excluded
    ]
    out = batch.format_skipped_by_tester(results)
    assert out is not None
    lines = out.splitlines()
    assert lines[0] == "needs evidence (tester didn't attach any PDF or image):"
    # Alice (3) first, then Bob (1) and unknown (1) alphabetical by name.
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
    cfg = argus_config.ARGUSConfig()
    assert cfg.batch.concurrency == 8
    assert cfg.batch.extractor_concurrency == 4


def test_config_batch_from_file(tmp_path: Path):
    p = tmp_path / "c.toml"
    p.write_text('[batch]\nconcurrency = 12\nextractor_concurrency = 6\n')
    cfg = argus_config.load(p)
    assert cfg.batch.concurrency == 12
    assert cfg.batch.extractor_concurrency == 6
