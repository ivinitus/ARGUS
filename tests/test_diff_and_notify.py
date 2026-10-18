from __future__ import annotations

from pathlib import Path

from argus import config as argus_config
from argus.operations import diff_and_notify
from argus.reporting import report as argus_report


def _settings():
    settings = argus_config.ArgusConfig()
    settings.notify.mode = "verbose"
    return settings


def _summary(key: str, *, verdict: str = "fail", tester: str | None = "Ana V",
             tester_status: str | None = "Pass",
             testrun_name: str | None = "E2E Flow UK Desktop",
             testrun_key: str | None = "QA-C17820",
             findings: list[dict] | None = None) -> argus_report.AuditSummary:
    return argus_report.AuditSummary(
        key=key,
        path=Path(f"/tmp/{key}"),
        verdict=verdict,
        findings_count=len(findings or []),
        severity_counts={},
        rules_fired=[],
        has_high_finding=False,
        schema_version=None,
        tester=tester,
        tester_status=tester_status,
        test_case=None,
        testrun_name=testrun_name,
        testrun_key=testrun_key,
        findings=findings or [],
    )


def _run_with(monkeypatch, tmp_path, summaries, out_dir=None):
    """Run diff_and_notify.run with fakes; return the captured deviations."""
    calls = []
    monkeypatch.setattr(diff_and_notify, "_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(diff_and_notify.argus_report, "_scan_output_dir",
                        lambda _out: summaries)
    monkeypatch.setattr(
        diff_and_notify.notify, "post_deviations",
        lambda folder, deviations, *, settings: calls.append(
            (folder, deviations)))
    out_dir = out_dir or tmp_path / "Sprint_47"
    out_dir.mkdir(exist_ok=True)
    assert diff_and_notify.run(out_dir, settings=_settings()) == 0
    return calls


def test_first_run_posts_batch_with_identity(monkeypatch, tmp_path):
    findings = [
        {"severity": "low", "description": "minor cosmetic thing"},
        {"severity": "high", "description": "Device family  mismatch:\niOS "
                                            "test on Android"},
    ]
    summaries = [
        _summary("QA-E1", findings=findings),
        _summary("QA-E2", tester="J. Doe", tester_status=None,
                 testrun_name=None, testrun_key=None, findings=[]),
    ]

    calls = _run_with(monkeypatch, tmp_path, summaries)

    assert len(calls) == 1
    folder, deviations = calls[0]
    assert folder == "Sprint_47"
    by_key = {d.key: d for d in deviations}
    assert set(by_key) == {"QA-E1", "QA-E2"}

    d1 = by_key["QA-E1"]
    assert d1.verdict == "fail"
    assert d1.tester == "Ana V"
    assert d1.tester_status == "Pass"
    assert d1.testrun_name == "E2E Flow UK Desktop"
    assert d1.testrun_key == "QA-C17820"
    assert d1.findings == (
        "Device family mismatch: iOS test on Android",
        "minor cosmetic thing",
    )

    d2 = by_key["QA-E2"]
    assert d2.tester_status is None
    assert d2.findings == ()


def test_second_run_same_state_does_not_repost(monkeypatch, tmp_path):
    summaries = [_summary("QA-E1")]

    first = _run_with(monkeypatch, tmp_path, summaries)
    second = _run_with(monkeypatch, tmp_path, summaries)

    assert len(first) == 1
    assert second == []


def test_nothing_flagged_posts_nothing(monkeypatch, tmp_path):
    summaries = [_summary("QA-E1", verdict="pass")]

    calls = _run_with(monkeypatch, tmp_path, summaries)

    assert calls == []


def test_finding_texts_truncate_long_descriptions():
    s = _summary("QA-E1", findings=[
        {"severity": "medium", "description": "x" * 300},
    ])
    texts = diff_and_notify._finding_texts(s)
    assert texts == ("x" * 120 + "...",)


def test_finding_texts_tolerate_missing_fields_without_dropping_entries():
    s = _summary("QA-E1", findings=[
        {"description": "no severity on this one"},
        {"severity": "weird", "description": ""},
    ])
    assert diff_and_notify._finding_texts(s) == (
        "no severity on this one",
        "unnamed finding",
    )
