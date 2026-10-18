"""Pure helper tests for the ARGUS orchestrator."""
from __future__ import annotations

import datetime as dt
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from argus import config as argus_config
from argus.pipeline import analysis_status
from argus.pipeline import batch
from argus.pipeline import orchestrator


def test_scoped_folder_out_dir_basic():
    base = Path("/tmp/out")
    scoped = orchestrator._scoped_folder_out_dir(base, "Sprint 47 Regression")
    assert scoped == Path("/tmp/out/Sprint_47_Regression")


def test_scoped_folder_out_dir_strips_dangerous_chars():
    base = Path("/tmp/out")
    scoped = orchestrator._scoped_folder_out_dir(base, "Folder / with \\ bad: chars")
    assert "/" not in scoped.name
    assert ":" not in scoped.name
    assert " " not in scoped.name
    assert scoped.parent == base


def test_scoped_folder_out_dir_handles_empty_and_weird_names():
    base = Path("/tmp/out")
    assert orchestrator._scoped_folder_out_dir(base, "") == base / "unnamed"
    assert orchestrator._scoped_folder_out_dir(base, "!!! ??? ///") == base / "unnamed"


def test_scoped_folder_out_dir_is_idempotent_on_already_slugged_input():
    base = Path("/tmp/out")
    scoped = orchestrator._scoped_folder_out_dir(base, "Sprint_47_Regression")
    assert scoped == Path("/tmp/out/Sprint_47_Regression")


def test_scoped_folder_out_dir_preserves_nested_base_paths(tmp_path: Path):
    base = tmp_path / "data" / "qa-output"
    scoped = orchestrator._scoped_folder_out_dir(base, "E2E Flow")
    assert scoped == tmp_path / "data" / "qa-output" / "E2E_Flow"


def test_disabled_auto_folder_never_contacts_jira(monkeypatch, capsys):
    settings = argus_config.ArgusConfig()
    settings.general.profile = "manual-team"
    settings.folders.auto_folder_enabled = False

    monkeypatch.setattr(
        orchestrator.extractor,
        "build_session",
        lambda unused: pytest.fail("disabled auto-folder must not contact Jira"),
    )

    assert orchestrator._run_auto_folder(settings) == 0
    assert "auto-folder is disabled" in capsys.readouterr().err


@pytest.mark.parametrize("include_descendants", [True, False])
def test_folder_descendant_policy_scopes_enumeration_coverage_and_manifest(
    tmp_path, monkeypatch, include_descendants,
):
    settings = argus_config.ArgusConfig()
    settings.extractor.out_dir = tmp_path / "output"
    settings.general.state_dir = tmp_path / "state"
    settings.folders.include_descendant_runs = include_descendants
    parent = {"id": 1, "key": "QA-C1", "folderId": 47}
    child = {"id": 2, "key": "QA-C2", "folderId": 48}
    monkeypatch.setattr(orchestrator.extractor, "build_session", lambda _: object())
    monkeypatch.setattr(orchestrator.extractor, "find_folder_by_name", lambda *a: {"id": 47})
    monkeypatch.setattr(orchestrator.extractor, "get_test_runs_in_folder", lambda *a: [parent, child])
    monkeypatch.setattr(orchestrator.extractor, "get_project_status_ids", lambda *a: {"PASS": 39})
    seen = []

    def enumerate_runs(session, base_url, runs, *args, **kwargs):
        seen.extend(runs)
        return orchestrator._FolderEnumeration(
            current_keys=[f"QA-E{run['id']}" for run in runs],
        )

    monkeypatch.setattr(orchestrator, "_enumerate_folder_items", enumerate_runs)
    coverage = mock.MagicMock()
    monkeypatch.setattr(orchestrator, "_write_folder_coverage", coverage)
    monkeypatch.setattr(orchestrator, "_publish_folder_reports", lambda *a: True)
    monkeypatch.setattr(orchestrator, "_announce_audit_phase", lambda *a, **kw: None)
    monkeypatch.setattr(
        "argus.core.users.get_default_cache",
        lambda: SimpleNamespace(save=lambda: None),
    )
    assert orchestrator._run_folder_list_prepared(
        "Parent", settings, force=False, selected_run_keys=None,
        run_id="test", reports_only=True,
    ) == 0
    expected = [parent, child] if include_descendants else [parent]
    assert seen == expected
    assert coverage.call_args.args[3] == expected
    manifest = json.loads(
        (settings.extractor.out_dir / "Parent" / "_latest_run.json").read_text(),
    )
    assert manifest["scope_keys"] == [f"QA-E{run['id']}" for run in expected]


def test_force_is_rejected_for_single_key():
    with pytest.raises(SystemExit):
        orchestrator.main(["--key", "QA-E1", "--force"])


def test_activity_progress_emits_every_key_with_cost_and_time(
        tmp_path, monkeypatch):
    settings = argus_config.ArgusConfig()
    emit = mock.MagicMock()
    monkeypatch.setattr(orchestrator.events, "emit", emit)
    audit_path = tmp_path / "E-1" / "audit.json"
    audit_path.parent.mkdir()
    audit_path.write_text(json.dumps({
        "cost": {"estimated_cost_usd": 0.1234},
    }))
    callback = orchestrator._activity_progress_callback(3, settings)
    callback(
        batch.KeyResult(
            key="E-1",
            status="ok",
            verdict="concerns",
            findings=2,
            elapsed_s=12.5,
            audit_path=audit_path,
        ),
        1,
        3,
    )
    callback(
        batch.KeyResult(
            key="E-2",
            status="skipped",
            reason="no evidence",
            elapsed_s=2.0,
        ),
        2,
        3,
    )

    messages = [call.args[1] for call in emit.call_args_list]
    assert messages == [
        "1/3 (33%) | E-1 | concerns, 2 findings | $0.1234 | 12.5s",
        "2/3 (66%) | E-2 | skipped: no evidence | cost n/a | 2.0s",
    ]


def test_activity_extraction_emits_key_pdf_pages_and_time(monkeypatch):
    settings = argus_config.ArgusConfig()
    emit = mock.MagicMock()
    monkeypatch.setattr(orchestrator.events, "emit", emit)
    callback = orchestrator._activity_extraction_callback(4, settings)

    callback(
        batch.ExtractionResult(
            key="QA-E1",
            status="ok",
            pages=12,
            elapsed_s=3.25,
        ),
        1,
        4,
    )

    emit.assert_called_once_with(
        "EXTRACT",
        "1/4 (25%) | QA-E1 | PDF 12 pages | 3.2s",
        settings=settings,
    )


def test_single_key_audit_emits_activity_lifecycle(tmp_path, monkeypatch):
    settings = argus_config.ArgusConfig()
    settings.extractor.out_dir = tmp_path
    args = SimpleNamespace(
        execution_key="QA-E1",
        testrun_id=None,
        item_id=None,
        skip_extract=True,
        skip_audit=False,
    )
    audit_dir = tmp_path / "run" / "QA-E1"
    audit_dir.mkdir(parents=True)
    (audit_dir / "audit.json").write_text(json.dumps({
        "overall_verdict": "concerns",
        "findings": [{"severity": "medium"}],
        "cost": {"estimated_cost_usd": 0.1234},
    }))
    monkeypatch.setattr(orchestrator, "_run_single_locked",
                        lambda *unused: 0)
    emit = mock.MagicMock()
    monkeypatch.setattr(orchestrator.events, "emit", emit)

    assert orchestrator._run_single(args, settings) == 0

    assert [call.args[0] for call in emit.call_args_list] == [
        "AUDIT", "COMPLETE",
    ]
    assert emit.call_args_list[0].args[1] == (
        "Started QA-E1 (single key)"
    )
    assert (
        "QA-E1 | concerns, 1 finding, $0.1234"
        in emit.call_args_list[1].args[1]
    )


def test_single_key_failure_emits_alert(tmp_path, monkeypatch):
    settings = argus_config.ArgusConfig()
    settings.extractor.out_dir = tmp_path
    args = SimpleNamespace(
        execution_key="QA-E2",
        testrun_id=None,
        item_id=None,
        skip_extract=True,
        skip_audit=False,
    )
    monkeypatch.setattr(orchestrator, "_run_single_locked",
                        lambda *unused: 2)
    emit = mock.MagicMock()
    monkeypatch.setattr(orchestrator.events, "emit", emit)

    assert orchestrator._run_single(args, settings) == 2

    assert [call.args[0] for call in emit.call_args_list] == [
        "AUDIT", "ERROR",
    ]
    assert "rc=2" in emit.call_args_list[1].args[1]


def test_single_key_incomplete_emits_precise_alert_and_records_cost(
        tmp_path, monkeypatch):
    settings = argus_config.ArgusConfig()
    settings.extractor.out_dir = tmp_path
    args = SimpleNamespace(
        execution_key="QA-E3",
        testrun_id=None,
        item_id=None,
        skip_extract=True,
        skip_audit=False,
    )
    audit_path = tmp_path / "run" / "QA-E3" / "audit.json"

    def incomplete_run(*unused):
        audit_path.parent.mkdir(parents=True)
        audit_path.write_text(json.dumps({
            "overall_verdict": "concerns",
            "findings": [],
            "analysis_modules": {
                "environment": {
                    "status": "incomplete",
                    "diagnostic": "response contained no complete JSON object",
                },
            },
        }))
        return 0

    monkeypatch.setattr(
        orchestrator,
        "_run_single_locked",
        incomplete_run,
    )
    emit = mock.MagicMock()
    record_cost = mock.MagicMock()
    monkeypatch.setattr(orchestrator.events, "emit", emit)
    monkeypatch.setattr(orchestrator, "_record_run_cost", record_cost)

    assert orchestrator._run_single(args, settings) == 1

    assert [call.args[0] for call in emit.call_args_list] == [
        "AUDIT", "ERROR",
    ]
    assert "analysis incomplete: environment: response contained no complete" \
        in emit.call_args_list[1].args[1]
    assert "single-key audit failed" not in emit.call_args_list[1].args[1]
    record_cost.assert_called_once()


def test_single_key_runs_deferred_environment_check_while_locked(
        tmp_path, monkeypatch):
    settings = argus_config.ArgusConfig()
    settings.extractor.out_dir = tmp_path
    work_dir = tmp_path / "Sprint_47" / "QA-E1"
    work_dir.mkdir(parents=True)
    args = SimpleNamespace(
        execution_key="QA-E1",
        testrun_id=None,
        item_id=None,
        skip_extract=True,
        skip_audit=False,
    )
    monkeypatch.setattr(
        orchestrator,
        "_find_existing_workdir",
        lambda *unused: work_dir,
    )
    monkeypatch.setattr(orchestrator, "_synthetic_status", lambda *unused: None)

    def fake_audit(cfg):
        (cfg.execution_dir / "audit.json").write_text(json.dumps({
            "overall_verdict": "pass",
            "findings": [],
            "analysis_modules": {
                "environment": {"status": "not_applicable"},
            },
        }))
        return 0

    monkeypatch.setattr(orchestrator.auditor, "run", fake_audit)
    env_check = mock.MagicMock()
    monkeypatch.setattr(batch, "_run_env_check_for_key", env_check)
    monkeypatch.setattr(orchestrator, "_refresh_folder_report",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "_print_summary", lambda *unused: None)

    assert orchestrator._run_single_locked(
        args, settings, tmp_path, "QA-E1") == 0
    env_check.assert_called_once()
    assert env_check.call_args.kwargs == {"lock_held": True}
    assert env_check.call_args.args[0].audit_path == work_dir / "audit.json"


def test_single_key_report_refresh_failure_returns_nonzero(
    tmp_path,
    monkeypatch,
):
    settings = argus_config.ArgusConfig()
    settings.extractor.out_dir = tmp_path
    work_dir = tmp_path / "Sprint_47" / "QA-E1"
    work_dir.mkdir(parents=True)
    args = SimpleNamespace(
        execution_key="QA-E1",
        testrun_id=None,
        item_id=None,
        skip_extract=True,
        skip_audit=False,
    )
    monkeypatch.setattr(
        orchestrator,
        "_find_existing_workdir",
        lambda *unused: work_dir,
    )
    monkeypatch.setattr(
        orchestrator,
        "_synthetic_status",
        lambda *unused: None,
    )

    def fake_audit(cfg):
        (cfg.execution_dir / "audit.json").write_text(json.dumps({
            "overall_verdict": "pass",
            "findings": [],
            "analysis_status": "complete",
        }))
        return 0

    monkeypatch.setattr(orchestrator.auditor, "run", fake_audit)
    monkeypatch.setattr(
        batch,
        "_run_env_check_for_key",
        mock.MagicMock(),
    )
    monkeypatch.setattr(
        orchestrator,
        "_refresh_folder_report",
        mock.MagicMock(side_effect=RuntimeError("publication failed")),
    )
    monkeypatch.setattr(
        orchestrator,
        "_print_summary",
        lambda *unused: None,
    )

    assert orchestrator._run_single_locked(
        args,
        settings,
        tmp_path,
        "QA-E1",
    ) == 1


def test_single_key_environment_check_has_bounded_wait(monkeypatch):
    future = mock.MagicMock()
    future.result.side_effect = TimeoutError
    future.done.return_value = False
    executor = mock.MagicMock()
    executor.submit.return_value = future
    executor_factory = mock.MagicMock(return_value=executor)
    monkeypatch.setattr(orchestrator, "ThreadPoolExecutor", executor_factory)
    settings = argus_config.ArgusConfig()
    result = batch.KeyResult(key="QA-E1", status="ok")

    with pytest.raises(
        analysis_status.AnalysisIncompleteError,
        match="post-check analysis timed out after 300 seconds",
    ):
        orchestrator._run_single_env_check(result, settings)

    future.result.assert_called_once_with(timeout=300)
    executor.shutdown.assert_called_once_with(
        wait=False,
        cancel_futures=True,
    )


@pytest.mark.parametrize("synthetic_status", [None, "Not Applicable", "Blocked"])
def test_single_execution_refresh_preserves_other_folder_audits(
        tmp_path, monkeypatch, synthetic_status):
    from argus.reporting import report

    settings = argus_config.ArgusConfig()
    settings.extractor.out_dir = tmp_path
    folder = tmp_path / "Sprint_47"
    selected = folder / "run" / "QA-E2"
    sibling = folder / "run" / "QA-E1" / "audit.json"
    selected.mkdir(parents=True)
    sibling.parent.mkdir(parents=True)
    sibling.write_text(json.dumps({"analysis_status": "complete", "findings": []}))
    original = sibling.read_bytes()
    (folder / orchestrator.FOLDER_META_FILENAME).write_text("{}")
    (folder / "_coverage.json").write_text(json.dumps({
        "schema": 1, "items_total": 500, "items_executed": 400, "findings": [],
    }))
    (folder / "_failed_keys.txt").write_text("QA-E3\n")
    args = SimpleNamespace(
        execution_key="QA-E2", testrun_id=None, item_id=None,
        skip_extract=True, skip_audit=False,
    )
    monkeypatch.setattr(orchestrator, "_find_existing_workdir", lambda *a: selected)
    monkeypatch.setattr(orchestrator, "_synthetic_status", lambda *a: synthetic_status)
    monkeypatch.setattr(batch, "_has_any_evidence", lambda *a: False)
    monkeypatch.setattr(batch, "_post_checks_enabled", lambda *a: False)
    monkeypatch.setattr(orchestrator, "_print_summary", lambda *a: None)

    def audit(config):
        (config.execution_dir / "audit.json").write_text(json.dumps({
            "execution_key": "QA-E2", "analysis_status": "complete",
            "findings": [],
        }))
        return 0

    monkeypatch.setattr(orchestrator.auditor, "run", audit)
    published = []

    def publish(root, unused_settings):
        summaries = report.scan_output_dir(root)
        assert {summary.key for summary in summaries} == {"QA-E1", "QA-E2"}
        html = report.generate_html_report(root, summaries=summaries)
        markdown = report.generate_report(root, summaries=summaries)
        assert "QA-E2" in html
        assert "QA-E1" in html and "QA-E1" in markdown
        assert report._load_coverage_json(root)["items_total"] == 500
        assert len(summaries) == 2
        published.append(root)
        return True

    monkeypatch.setattr(orchestrator, "_publish_folder_reports", publish)
    assert orchestrator._run_single_locked(args, settings, tmp_path, args.execution_key) == 0
    assert published == [folder]
    assert not (folder / "_latest_run.json").exists()
    assert sibling.read_bytes() == original
    assert (folder / "_failed_keys.txt").read_text() == "QA-E3\n"


def test_restore_148_audits_hidden_by_single_execution_manifest(tmp_path):
    from argus.reporting import report

    keys = [f"MOBILE-E{index}" for index in range(1, 149)]
    for key in keys:
        folder = tmp_path / "run" / key
        folder.mkdir(parents=True)
        (folder / "audit.json").write_text(json.dumps({
            "execution_key": key, "analysis_status": "complete", "findings": [],
        }))
    original = {
        "version": 1, "folder_name": "Android", "started_at": "before",
        "completed_at": "after", "scope_keys": ["MOBILE-E1"],
        "selected_keys": ["MOBILE-E1"], "audit_keys": ["MOBILE-E1"],
        "failed_keys": [], "statuses": {"MOBILE-E1": "ok"},
        "execution_key": "MOBILE-E1",
    }
    (tmp_path / "_latest_run.json").write_text(json.dumps(original))
    assert len(report.scan_output_dir(tmp_path)) == 1
    assert orchestrator._restore_single_execution_scope(tmp_path)
    assert {summary.key for summary in report.scan_output_dir(tmp_path)} == set(keys)
    restored = json.loads((tmp_path / "_latest_run.json").read_text())
    assert "execution_key" not in restored
    assert set(restored["scope_keys"]) == set(keys)
    assert json.loads(
        (tmp_path / "_single_execution_scope_backup.json").read_text(),
    ) == original
    assert not orchestrator._restore_single_execution_scope(tmp_path)


def test_scope_restore_keeps_failed_keys_out_of_completed_audits(tmp_path):
    from argus.reporting import report

    for key in ("MOBILE-E1", "MOBILE-E2", "MOBILE-E3"):
        folder = tmp_path / "run" / key
        folder.mkdir(parents=True)
        (folder / "audit.json").write_text(json.dumps({
            "execution_key": key,
            "analysis_status": "incomplete" if key == "MOBILE-E3" else "complete",
            "findings": [],
        }))
    (tmp_path / "_latest_run.json").write_text(json.dumps({
        "version": 1, "folder_name": "Android", "started_at": "before",
        "completed_at": "after", "scope_keys": ["MOBILE-E1"],
        "selected_keys": ["MOBILE-E1"], "audit_keys": ["MOBILE-E1"],
        "failed_keys": [], "statuses": {}, "execution_key": "MOBILE-E1",
    }))
    (tmp_path / "_failed_keys.txt").write_text("MOBILE-E2\nMOBILE-E3\n")
    assert orchestrator._restore_single_execution_scope(tmp_path)
    # E2 has a failed retry marker and only an older complete result.
    assert {summary.key for summary in report.scan_output_dir(tmp_path)} == {
        "MOBILE-E1", "MOBILE-E3",
    }


def test_single_refresh_accumulates_comma_batch_keys_without_losing_history(
        tmp_path, monkeypatch):
    from argus.reporting import report

    settings = argus_config.ArgusConfig()
    settings.extractor.out_dir = tmp_path
    folder = tmp_path / "Android"
    folder.mkdir()
    (folder / orchestrator.FOLDER_META_FILENAME).write_text("{}")
    existing = folder / "run" / "MOBILE-E1"
    existing.mkdir(parents=True)
    (existing / "audit.json").write_text(json.dumps({
        "execution_key": "MOBILE-E1", "analysis_status": "complete", "findings": [],
    }))
    (folder / "_latest_run.json").write_text(json.dumps({
        "version": 1, "folder_name": "Android", "started_at": "before",
        "completed_at": "after", "scope_keys": ["MOBILE-E1"],
        "selected_keys": ["MOBILE-E1"], "audit_keys": ["MOBILE-E1"],
        "failed_keys": [], "statuses": {},
    }))
    publications = []
    monkeypatch.setattr(orchestrator, "_publish_folder_reports", lambda root, _: (
        publications.append({s.key for s in report.scan_output_dir(root)}) or True
    ))
    for key in ("MOBILE-E2", "MOBILE-E3", "MOBILE-E4"):
        execution = folder / "run" / key
        execution.mkdir()
        (execution / "audit.json").write_text(json.dumps({
            "execution_key": key, "analysis_status": "complete", "findings": [],
        }))
        orchestrator._refresh_folder_report(
            execution, tmp_path, "https://jira.test", settings=settings,
            report_started_at=dt.datetime.now(dt.timezone.utc),
        )
    assert publications == [
        {"MOBILE-E1", "MOBILE-E2"},
        {"MOBILE-E1", "MOBILE-E2", "MOBILE-E3"},
        {"MOBILE-E1", "MOBILE-E2", "MOBILE-E3", "MOBILE-E4"},
    ]
    assert set(json.loads((folder / "_latest_run.json").read_text())["scope_keys"]) == publications[-1]


def test_single_key_environment_failure_is_persisted_and_returns_nonzero(
        tmp_path, monkeypatch):
    settings = argus_config.ArgusConfig()
    settings.extractor.out_dir = tmp_path
    work_dir = tmp_path / "Sprint_47" / "QA-E2"
    work_dir.mkdir(parents=True)
    args = SimpleNamespace(
        execution_key="QA-E2",
        testrun_id=None,
        item_id=None,
        skip_extract=True,
        skip_audit=False,
    )
    monkeypatch.setattr(
        orchestrator,
        "_find_existing_workdir",
        lambda *unused: work_dir,
    )
    monkeypatch.setattr(orchestrator, "_synthetic_status", lambda *unused: None)

    def fake_audit(cfg):
        (cfg.execution_dir / "audit.json").write_text(json.dumps({
            "overall_verdict": "pass",
            "findings": [],
            "analysis_modules": {
                "extraction": {"status": "complete"},
                "screenshots": {"status": "complete"},
                "environment": {"status": "not_applicable"},
            },
        }))
        return 0

    monkeypatch.setattr(orchestrator.auditor, "run", fake_audit)
    monkeypatch.setattr(
        batch,
        "_run_env_check_for_key",
        mock.MagicMock(side_effect=analysis_status.AnalysisIncompleteError(
            "environment",
            "response contained no complete JSON object",
        )),
    )
    refresh = mock.MagicMock()
    monkeypatch.setattr(orchestrator, "_refresh_folder_report", refresh)
    monkeypatch.setattr(orchestrator, "_print_summary", lambda *unused: None)

    assert orchestrator._run_single_locked(
        args, settings, tmp_path, "QA-E2") == 1
    audit = json.loads((work_dir / "audit.json").read_text())
    assert audit["analysis_status"] == "incomplete"
    assert audit["analysis_modules"]["environment"] == {
        "status": "incomplete",
        "diagnostic": "response contained no complete JSON object",
    }
    refresh.assert_called_once()


def _make_audited_pair(
    out_dir: Path,
    folder: str,
    e_key: str,
    execution_date: str | None,
    *,
    evidence_fingerprint: str | None = None,
    write_metadata: bool = True,
) -> Path:
    """Lay down <out>/<folder>/<E-KEY>/{audit.json, metadata.json}."""
    exec_dir = out_dir / folder / e_key
    exec_dir.mkdir(parents=True, exist_ok=True)
    (exec_dir / "audit.json").write_text(json.dumps({"overall_verdict": "pass"}))
    if write_metadata:
        tr = {"execution_date": execution_date}
        if evidence_fingerprint is not None:
            tr["evidence_fingerprint"] = evidence_fingerprint
        meta = {"test_results": [tr]}
        (exec_dir / "metadata.json").write_text(json.dumps(meta))
    return exec_dir


def test_audited_keys_with_timestamps_returns_dates_per_key(tmp_path: Path):
    out = tmp_path / "out"
    _make_audited_pair(out, "Sprint_47", "QA-E1", "2026-05-26T10:00:00Z")
    _make_audited_pair(out, "Sprint_47", "QA-E2", "2026-05-27T11:00:00Z")

    result = orchestrator._audited_keys_with_timestamps(out)
    assert result == {
        "QA-E1": "2026-05-26T10:00:00Z",
        "QA-E2": "2026-05-27T11:00:00Z",
    }


def test_audited_keys_with_state_returns_dates_and_fingerprints(
        tmp_path: Path):
    out = tmp_path / "out"
    _make_audited_pair(
        out, "Sprint_47", "QA-E1", "2026-05-26T10:00:00Z",
        evidence_fingerprint="fp-1")

    result = orchestrator._audited_keys_with_state(out)

    assert result == {
        "QA-E1": orchestrator._AuditState(
            execution_date="2026-05-26T10:00:00Z",
            evidence_fingerprint="fp-1",
        )
    }


def test_audited_keys_with_state_reads_incomplete_analysis_status(
        tmp_path: Path):
    out = tmp_path / "out"
    exec_dir = _make_audited_pair(
        out, "Sprint_47", "QA-E1", "2026-05-26T10:00:00Z",
        evidence_fingerprint="fp-1")
    (exec_dir / "audit.json").write_text(json.dumps({
        "analysis_modules": {
            "model": {
                "status": "incomplete",
                "diagnostic": "model response could not be parsed",
            },
        },
    }))

    result = orchestrator._audited_keys_with_state(out)

    assert result["QA-E1"].analysis_status == "incomplete"


def test_audited_keys_with_state_missing_fingerprint_derives_from_metadata(
        tmp_path: Path):
    out = tmp_path / "out"
    exec_dir = _make_audited_pair(
        out, "Sprint_47", "QA-E1", "2026-05-26T10:00:00Z")
    metadata = json.loads((exec_dir / "metadata.json").read_text())
    metadata["test_results"][0]["attachments"] = [
        {"id": 77, "name": "proof.pdf", "size": 123}
    ]
    (exec_dir / "metadata.json").write_text(json.dumps(metadata))

    result = orchestrator._audited_keys_with_state(out)

    assert result == {
        "QA-E1": orchestrator._AuditState(
            execution_date="2026-05-26T10:00:00Z",
            evidence_fingerprint=orchestrator.extractor.build_evidence_fingerprint(
                metadata["test_results"]),
        )
    }


def test_audited_keys_with_timestamps_missing_metadata_yields_sentinel(
        tmp_path: Path):
    out = tmp_path / "out"
    _make_audited_pair(
        out, "Sprint_47", "QA-E1", None, write_metadata=False)

    result = orchestrator._audited_keys_with_timestamps(out)
    assert result == {"QA-E1": ""}


def test_audited_keys_with_timestamps_unreadable_metadata_yields_sentinel(
        tmp_path: Path):
    out = tmp_path / "out"
    exec_dir = out / "Sprint_47" / "QA-E1"
    exec_dir.mkdir(parents=True)
    (exec_dir / "audit.json").write_text("{}")
    (exec_dir / "metadata.json").write_text("{not-json")

    result = orchestrator._audited_keys_with_timestamps(out)
    assert result == {"QA-E1": ""}


def test_audited_keys_with_timestamps_excludes_replay_paths(tmp_path: Path):
    out = tmp_path / "out"
    _make_audited_pair(out, "Sprint_47", "QA-E1", "2026-05-26T10:00:00Z")
    replay_dir = out / "Sprint_47" / "QA-E2" / "_replay" / "v4"
    replay_dir.mkdir(parents=True)
    (replay_dir / "audit.json").write_text("{}")
    (replay_dir / "metadata.json").write_text(json.dumps(
        {"test_results": [{"execution_date": "2026-05-99T00:00:00Z"}]}))

    result = orchestrator._audited_keys_with_timestamps(out)
    assert result == {"QA-E1": "2026-05-26T10:00:00Z"}
    assert "QA-E2" not in result
    assert "v4" not in result


def test_audited_keys_with_timestamps_handles_missing_out_dir(tmp_path: Path):
    nonexistent = tmp_path / "never-created"
    assert orchestrator._audited_keys_with_timestamps(nonexistent) == {}


def test_audited_keys_with_timestamps_handles_missing_test_results(
        tmp_path: Path):
    out = tmp_path / "out"
    exec_dir = out / "Sprint_47" / "QA-E1"
    exec_dir.mkdir(parents=True)
    (exec_dir / "audit.json").write_text("{}")
    (exec_dir / "metadata.json").write_text(json.dumps({"unrelated": "shape"}))

    result = orchestrator._audited_keys_with_timestamps(out)
    assert result == {"QA-E1": ""}


def _enumerate_one_executed_item(monkeypatch, summary):
    monkeypatch.setattr(
        orchestrator.extractor,
        "get_testrun_items",
        lambda session, base_url, run_id: [{
            "$lastTestResult": {"id": 7, "testResultStatusId": 39}
        }],
    )
    monkeypatch.setattr(
        orchestrator.extractor,
        "get_testresult_summary",
        lambda session, base_url, result_id: summary,
    )
    return orchestrator._enumerate_folder_items(
        object(),
        "https://jira.test",
        [{"id": 1, "key": "QA-C1"}],
        executed_ids={39},
        na_ids=set(),
        audited={"QA-E1": orchestrator._AuditState(
            execution_date="2026-05-26T10:00:00Z",
            evidence_fingerprint="cached-fp",
        )},
    )


def test_enumerate_folder_items_skips_when_date_and_evidence_match(
        monkeypatch):
    enum = _enumerate_one_executed_item(monkeypatch, {
        "key": "QA-E1",
        "user_key": "USER1",
        "execution_date": "2026-05-26T10:00:00Z",
        "evidence_fingerprint": "cached-fp",
    })

    assert enum.emitted_keys == []
    assert enum.total_skipped_dup == 1
    assert enum.total_reaudits == 0


def test_enumerate_folder_items_reaudits_incomplete_cached_audit(monkeypatch):
    monkeypatch.setattr(
        orchestrator.extractor,
        "get_testrun_items",
        lambda session, base_url, run_id: [{
            "$lastTestResult": {"id": 7, "testResultStatusId": 39}
        }],
    )
    monkeypatch.setattr(
        orchestrator.extractor,
        "get_testresult_summary",
        lambda session, base_url, result_id: {
            "key": "QA-E1",
            "user_key": "USER1",
            "execution_date": "2026-05-26T10:00:00Z",
            "evidence_fingerprint": "cached-fp",
        },
    )

    enum = orchestrator._enumerate_folder_items(
        object(),
        "https://jira.test",
        [{"id": 1, "key": "QA-C1"}],
        executed_ids={39},
        na_ids=set(),
        audited={"QA-E1": orchestrator._AuditState(
            execution_date="2026-05-26T10:00:00Z",
            evidence_fingerprint="cached-fp",
            analysis_status="incomplete",
        )},
    )

    assert enum.emitted_keys == ["QA-E1"]
    assert enum.total_reaudits == 1


def test_enumerate_folder_items_skips_ignored_status_before_key_resolution(
        monkeypatch):
    monkeypatch.setattr(
        orchestrator.extractor,
        "get_testrun_items",
        lambda *args: [
            {"$lastTestResult": {"id": 7, "testResultStatusId": 51}},
            {"$lastTestResult": {"id": 8, "testResultStatusId": 39}},
        ],
    )
    resolve_summary = mock.MagicMock(return_value={
        "key": "MOBILE-E8",
        "user_key": "USER1",
        "execution_date": "2026-09-06T00:00:00Z",
        "evidence_fingerprint": "fp",
    })
    monkeypatch.setattr(
        orchestrator.extractor,
        "get_testresult_summary",
        resolve_summary,
    )

    enum = orchestrator._enumerate_folder_items(
        object(),
        "https://jira.test",
        [{"id": 1, "key": "MOBILE-C1"}],
        executed_ids={39, 51},
        na_ids=set(),
        audited={},
        ignored_ids={51},
    )

    assert enum.emitted_keys == ["MOBILE-E8"]
    assert enum.total_executed == 1
    assert enum.total_ignored_status == 1
    resolve_summary.assert_called_once_with(
        mock.ANY,
        "https://jira.test",
        8,
    )


@pytest.mark.parametrize("selected, expected_ids", [
    (None, [1, 2, 3]),
    ({"QA-C2"}, [2]),
    ({"qa-c1", "QA-C3"}, [1, 3]),
    (set(), []),
])
def test_enumerate_folder_items_never_fetches_unselected_runs(
    monkeypatch, selected, expected_ids,
):
    get_items = mock.MagicMock(side_effect=lambda session, base_url, run_id: [{
        "$lastTestResult": {"id": run_id, "testResultStatusId": 39},
    }])
    monkeypatch.setattr(
        orchestrator.extractor,
        "get_testrun_items",
        get_items,
    )
    monkeypatch.setattr(
        orchestrator.extractor,
        "get_testresult_summary",
        lambda session, base_url, result_id: {
            "key": f"QA-E{result_id}",
            "user_key": "USER1",
            "execution_date": "2026-09-09T00:00:00Z",
            "evidence_fingerprint": f"fp-{result_id}",
        },
    )

    enum = orchestrator._enumerate_folder_items(
        object(),
        "https://jira.test",
        [
            {"id": 1, "key": "QA-C1"},
            {"id": 2, "key": "QA-C2"},
            {"id": 3, "key": "QA-C3"},
        ],
        executed_ids={39},
        na_ids=set(),
        audited={},
        selected_run_keys=selected,
    )

    assert [call.args[2] for call in get_items.call_args_list] == expected_ids
    expected_keys = [f"QA-E{run_id}" for run_id in expected_ids]
    assert enum.current_keys == expected_keys
    assert enum.emitted_keys == expected_keys
    assert enum.total_executed == len(expected_ids)
    assert sorted(enum.items_by_run) == expected_ids


def test_enumerate_folder_items_reaudits_same_date_changed_evidence(
        monkeypatch):
    enum = _enumerate_one_executed_item(monkeypatch, {
        "key": "QA-E1",
        "user_key": "USER1",
        "execution_date": "2026-05-26T10:00:00Z",
        "evidence_fingerprint": "new-fp",
    })

    assert enum.emitted_keys == ["QA-E1"]
    assert enum.emitted_user_keys == ["USER1"]
    assert enum.total_skipped_dup == 0
    assert enum.total_reaudits == 1


def test_enumerate_folder_items_reaudits_missing_cached_fingerprint(
        monkeypatch):
    monkeypatch.setattr(
        orchestrator.extractor,
        "get_testrun_items",
        lambda session, base_url, run_id: [{
            "$lastTestResult": {"id": 7, "testResultStatusId": 39}
        }],
    )
    monkeypatch.setattr(
        orchestrator.extractor,
        "get_testresult_summary",
        lambda session, base_url, result_id: {
            "key": "QA-E1",
            "user_key": "USER1",
            "execution_date": "2026-05-26T10:00:00Z",
            "evidence_fingerprint": "live-fp",
        },
    )

    enum = orchestrator._enumerate_folder_items(
        object(),
        "https://jira.test",
        [{"id": 1, "key": "QA-C1"}],
        executed_ids={39},
        na_ids=set(),
        audited={"QA-E1": orchestrator._AuditState(
            execution_date="2026-05-26T10:00:00Z",
            evidence_fingerprint="",
        )},
    )

    assert enum.emitted_keys == ["QA-E1"]
    assert enum.total_reaudits == 1


def test_enumerate_folder_items_still_reaudits_changed_execution_date(
        monkeypatch):
    enum = _enumerate_one_executed_item(monkeypatch, {
        "key": "QA-E1",
        "user_key": "USER1",
        "execution_date": "2026-05-27T10:00:00Z",
        "evidence_fingerprint": "cached-fp",
    })

    assert enum.emitted_keys == ["QA-E1"]
    assert enum.total_reaudits == 1


def test_enumerate_folder_items_survives_timeout_on_summary(monkeypatch):
    import requests as _requests

    monkeypatch.setattr(
        orchestrator.extractor,
        "get_testrun_items",
        lambda session, base_url, run_id: [
            {"$lastTestResult": {"id": 7, "testResultStatusId": 39}},
            {"$lastTestResult": {"id": 8, "testResultStatusId": 39}},
        ],
    )

    def flaky_summary(session, base_url, result_id):
        if result_id == 7:
            raise _requests.exceptions.ReadTimeout("read timed out")
        return {"key": "QA-E2", "user_key": "USER2",
                "execution_date": "2026-05-27T10:00:00Z",
                "evidence_fingerprint": "fp"}

    monkeypatch.setattr(orchestrator.extractor,
                        "get_testresult_summary", flaky_summary)
    monkeypatch.setattr(orchestrator.extractor,
                        "get_testresult_key", lambda *args: None)

    enum = orchestrator._enumerate_folder_items(
        object(), "https://jira.test",
        [{"id": 1, "key": "QA-C1"}],
        executed_ids={39}, na_ids=set(), audited={},
    )

    assert enum.total_unresolved == 1
    assert enum.emitted_keys == ["QA-E2"]


def test_enumerate_folder_items_uses_key_fallback_on_summary_timeout(
        monkeypatch):
    import requests as _requests

    monkeypatch.setattr(
        orchestrator.extractor,
        "get_testrun_items",
        lambda session, base_url, run_id: [{
            "$lastTestResult": {
                "id": 7,
                "testResultStatusId": 39,
                "userKey": "USER1",
            },
        }],
    )
    monkeypatch.setattr(
        orchestrator.extractor,
        "get_testresult_summary",
        lambda *args: (_ for _ in ()).throw(
            _requests.exceptions.ReadTimeout("read timed out")),
    )
    monkeypatch.setattr(
        orchestrator.extractor,
        "get_testresult_key",
        lambda session, base_url, result_id: "QA-E1",
    )

    enum = orchestrator._enumerate_folder_items(
        object(), "https://jira.test",
        [{"id": 1, "key": "QA-C1"}],
        executed_ids={39}, na_ids=set(), audited={},
    )

    assert enum.total_unresolved == 0
    assert enum.emitted_keys == ["QA-E1"]
    assert enum.emitted_user_keys == ["USER1"]


def test_enumerate_folder_items_keeps_live_cache_fields_when_key_omitted(
        monkeypatch):
    monkeypatch.setattr(
        orchestrator.extractor,
        "get_testrun_items",
        lambda session, base_url, run_id: [{
            "$lastTestResult": {
                "id": 7,
                "testResultStatusId": 39,
                "userKey": "FALLBACK",
            },
        }],
    )
    monkeypatch.setattr(
        orchestrator.extractor,
        "get_testresult_summary",
        lambda *args: {
            "user_key": "USER1",
            "execution_date": "2026-05-26T10:00:00Z",
            "evidence_fingerprint": "cached-fp",
        },
    )
    monkeypatch.setattr(
        orchestrator.extractor,
        "get_testresult_key",
        lambda session, base_url, result_id: "QA-E1",
    )

    enum = orchestrator._enumerate_folder_items(
        object(),
        "https://jira.test",
        [{"id": 1, "key": "QA-C1"}],
        executed_ids={39},
        na_ids=set(),
        audited={"QA-E1": orchestrator._AuditState(
            execution_date="2026-05-26T10:00:00Z",
            evidence_fingerprint="cached-fp",
        )},
    )

    assert enum.emitted_keys == []
    assert enum.total_skipped_dup == 1
    assert enum.total_reaudits == 0


def test_enumerate_folder_items_survives_connection_error_on_run(
        monkeypatch, capsys):
    import requests as _requests

    def flaky_items(session, base_url, run_id):
        if run_id == 1:
            raise _requests.exceptions.ConnectionError("reset by peer")
        return [{"$lastTestResult": {"id": 9, "testResultStatusId": 39}}]

    monkeypatch.setattr(orchestrator.extractor,
                        "get_testrun_items", flaky_items)
    monkeypatch.setattr(
        orchestrator.extractor,
        "get_testresult_summary",
        lambda session, base_url, result_id: {
            "key": "QA-E3", "user_key": "USER3",
            "execution_date": "2026-05-27T10:00:00Z",
            "evidence_fingerprint": "fp"},
    )

    enum = orchestrator._enumerate_folder_items(
        object(), "https://jira.test",
        [{"id": 1, "key": "QA-C1"}, {"id": 2, "key": "QA-C2"}],
        executed_ids={39}, na_ids=set(), audited={},
    )

    assert enum.total_runs_failed == 1
    assert enum.emitted_keys == ["QA-E3"]
    assert "skipping run QA-C1" in capsys.readouterr().err


def test_reconcile_failed_keys_removes_completed_retries(tmp_path: Path):
    folder = tmp_path / "Sprint_47"
    complete = _make_audited_pair(
        folder, "run", "QA-E1", "2026-05-26T10:00:00Z")
    incomplete = _make_audited_pair(
        folder, "run", "QA-E2", "2026-05-26T10:00:00Z")
    (complete / "audit.json").write_text(json.dumps({
        "analysis_status": "complete",
    }))
    (incomplete / "audit.json").write_text(json.dumps({
        "analysis_status": "incomplete",
    }))
    failed = folder / "_failed_keys.txt"
    failed.write_text("# Retry keys\nQA-E1\nQA-E2\nQA-E3\nQA-E1\n")

    result = orchestrator._reconcile_failed_keys(folder)

    assert result == failed
    assert failed.read_text() == "QA-E2\nQA-E3\n"

    (incomplete / "audit.json").write_text(json.dumps({
        "analysis_status": "complete",
    }))
    failed.write_text("QA-E1\nQA-E2\n")
    assert orchestrator._reconcile_failed_keys(folder) is None
    assert not failed.exists()


@pytest.mark.parametrize("issue_status", [
    "PASS WITH ISSUE", "PASS_WITH_ISSUE", "pass-with-issue",
    "PASSED_WITH_ISSUE", "COMPLETED WITH NOTES",
])
def test_folder_with_no_new_keys_republishes(
        tmp_path: Path, monkeypatch, issue_status):
    settings = argus_config.ArgusConfig()
    settings.policy = argus_config.load(profile="portal").policy
    settings.policy.status_aliases["Completed with notes"] = "passed_with_issue"
    settings.extractor.out_dir = tmp_path / "output"
    settings.extractor.ignored_execution_statuses = [
        "automation pass",
        "automation-fail",
    ]
    settings.general.state_dir = tmp_path / "state"
    monkeypatch.setattr(
        "argus.operations.control.is_paused", lambda: False)
    monkeypatch.setattr(
        orchestrator.extractor, "build_session", lambda config: object())
    monkeypatch.setattr(
        orchestrator.extractor,
        "find_folder_by_name",
        lambda *args: {"id": 47},
    )
    monkeypatch.setattr(
        orchestrator.extractor,
        "get_test_runs_in_folder",
        lambda *args: [{"id": 1, "key": "QA-C1"}],
    )
    monkeypatch.setattr(
        orchestrator.extractor,
        "get_project_status_ids",
        lambda *args: {
            "PASS": 39,
            "FAIL": 40,
            issue_status: 41,
            "NOT_EXECUTED": 42,
            "AUTOMATION_PASS": 51,
            "AUTOMATION_FAIL": 52,
        },
    )
    enumerate_items = mock.MagicMock(
        return_value=orchestrator._FolderEnumeration(
            total_executed=1,
            total_skipped_dup=1,
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_enumerate_folder_items",
        enumerate_items,
    )
    write_coverage = mock.MagicMock()
    monkeypatch.setattr(
        orchestrator,
        "_write_folder_coverage",
        write_coverage,
    )
    reconcile = mock.MagicMock()
    publish = mock.MagicMock(return_value=True)
    monkeypatch.setattr(orchestrator, "_reconcile_failed_keys", reconcile)
    monkeypatch.setattr(orchestrator, "_publish_folder_reports", publish)
    monkeypatch.setattr(
        "argus.core.users.get_default_cache",
        lambda: SimpleNamespace(save=lambda: None),
    )

    assert orchestrator._run_folder_list("Sprint 47", settings) == 0

    folder_root = settings.extractor.out_dir / "Sprint_47"
    assert enumerate_items.call_args.args[3] == {39, 40, 41}
    assert enumerate_items.call_args.kwargs["ignored_ids"] == {51, 52}
    assert write_coverage.call_args.kwargs["ignored_status_ids"] == {51, 52}
    reconcile.assert_called_once_with(folder_root)
    publish.assert_called_once_with(folder_root, settings)


@pytest.mark.parametrize("reports_only", [False, True])
def test_folder_run_can_be_scoped_to_exact_selected_test_runs(
    tmp_path: Path,
    monkeypatch,
    reports_only,
):
    settings = argus_config.ArgusConfig()
    settings.extractor.out_dir = tmp_path / "output"
    settings.general.state_dir = tmp_path / "state"
    folder_root = settings.extractor.out_dir / "Sprint_47"
    for key in ("QA-E1", "QA-E2"):
        execution_dir = folder_root / "run" / key
        execution_dir.mkdir(parents=True)
        (execution_dir / "audit.json").write_text(json.dumps({
            "execution_key": key, "findings": [],
        }))
    monkeypatch.setattr(
        "argus.operations.control.is_paused",
        lambda: False,
    )
    monkeypatch.setattr(
        orchestrator.extractor,
        "build_session",
        lambda config: object(),
    )
    monkeypatch.setattr(
        orchestrator.extractor,
        "find_folder_by_name",
        lambda *args: {"id": 47},
    )
    monkeypatch.setattr(
        orchestrator.extractor,
        "get_test_runs_in_folder",
        lambda *args: [
            {"id": 1, "key": "QA-C1"},
            {"id": 2, "key": "QA-C2"},
        ],
    )
    monkeypatch.setattr(
        orchestrator.extractor,
        "get_project_status_ids",
        lambda *args: {"PASS": 39, "FAIL": 40},
    )
    enumerate_items = mock.MagicMock(
        return_value=orchestrator._FolderEnumeration(
            current_keys=["QA-E2"],
            emitted_keys=["QA-E2"] if reports_only else [],
            total_executed=1,
            total_skipped_dup=1,
        )
    )
    monkeypatch.setattr(
        orchestrator,
        "_enumerate_folder_items",
        enumerate_items,
    )
    write_coverage = mock.MagicMock()
    monkeypatch.setattr(
        orchestrator,
        "_write_folder_coverage",
        write_coverage,
    )
    monkeypatch.setattr(
        orchestrator,
        "_reconcile_failed_keys",
        lambda *args: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_publish_folder_reports",
        lambda *args: True,
    )
    monkeypatch.setattr(
        orchestrator.batch, "run_batch",
        lambda *a, **kw: pytest.fail("report-only refresh must not start audits"),
    )
    monkeypatch.setattr(
        "argus.core.users.get_default_cache",
        lambda: SimpleNamespace(save=lambda: None),
    )

    assert orchestrator._run_folder_list(
        "Sprint 47",
        settings,
        selected_run_keys={"QA-C2"},
        reports_only=reports_only,
    ) == 0

    expected_runs = [{"id": 2, "key": "QA-C2"}]
    assert enumerate_items.call_args.args[2] == expected_runs
    assert enumerate_items.call_args.kwargs["selected_run_keys"] == {
        "QA-C2"
    }
    assert write_coverage.call_args.args[3] == expected_runs
    manifest = json.loads((folder_root / "_latest_run.json").read_text())
    assert manifest["scope_keys"] == ["QA-E2"]
    assert manifest["audit_keys"] == ["QA-E2"]
    (folder_root / "_failed_keys.txt").write_text("QA-E1\n")

    # Publication must update and render only the selected execution even
    # though sibling-run results share the same historical cache directory.
    sibling = folder_root / "run" / "QA-E1" / "audit.json"
    sibling_before = sibling.read_bytes()
    orchestrator._set_publication_outcome(folder_root, "complete")
    assert sibling.read_bytes() == sibling_before
    selected = json.loads(
        (folder_root / "run" / "QA-E2" / "audit.json").read_text()
    )
    assert selected["analysis_modules"]["publication"]["status"] == "complete"
    from argus.reporting import report
    assert report.read_report_failed_keys(folder_root) == []
    assert [summary.key for summary in report.scan_output_dir(folder_root)] == [
        "QA-E2",
    ]


def test_publication_failure_records_successful_key_for_retry(
        tmp_path: Path, monkeypatch):
    settings = argus_config.ArgusConfig()
    settings.extractor.out_dir = tmp_path / "output"
    settings.general.state_dir = tmp_path / "state"
    folder_root = settings.extractor.out_dir / "Sprint_47"
    audit_path = folder_root / "run" / "QA-E1" / "audit.json"
    audit_path.parent.mkdir(parents=True)
    audit_path.write_text(json.dumps({
        "analysis_status": "complete",
        "analysis_modules": {
            "model": {"status": "complete"},
        },
        "findings": [],
        "overall_verdict": "pass",
    }))
    (audit_path.parent / "metadata.json").write_text("{}")
    monkeypatch.setattr(
        "argus.operations.control.is_paused", lambda: False)
    monkeypatch.setattr(
        orchestrator.extractor, "build_session", lambda config: object())
    monkeypatch.setattr(
        orchestrator.extractor,
        "find_folder_by_name",
        lambda *args: {"id": 47},
    )
    monkeypatch.setattr(
        orchestrator.extractor,
        "get_test_runs_in_folder",
        lambda *args: [{"id": 1, "key": "QA-C1"}],
    )
    monkeypatch.setattr(
        orchestrator.extractor,
        "get_project_status_ids",
        lambda *args: {"PASS": 39, "FAIL": 40},
    )
    monkeypatch.setattr(
        orchestrator,
        "_enumerate_folder_items",
        lambda *args, **kwargs: orchestrator._FolderEnumeration(
            emitted_keys=["QA-E1"],
            emitted_user_keys=[None],
            total_executed=1,
        ),
    )
    monkeypatch.setattr(orchestrator, "_print_by_tester", lambda *args: None)
    monkeypatch.setattr(orchestrator, "_write_folder_coverage",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(
        orchestrator.batch,
        "run_batch",
        lambda *args, **kwargs: [
            batch.KeyResult(
                key="QA-E1",
                status="ok",
                verdict="pass",
                findings=0,
                audit_path=audit_path,
            ),
        ],
    )
    monkeypatch.setattr(
        orchestrator, "_publish_folder_reports", lambda *args: False)
    monkeypatch.setattr(orchestrator.notify, "post_audit_started",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator.notify, "post_audit_done",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "_record_run_cost",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "argus.operations.control.mark_audit_running",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "argus.operations.control.mark_audit_done", lambda *args: None)
    monkeypatch.setattr(
        "argus.core.users.get_default_cache",
        lambda: SimpleNamespace(save=lambda: None),
    )

    assert orchestrator._run_folder_list("Sprint 47", settings) == 1

    assert (folder_root / "_failed_keys.txt").read_text() == "QA-E1\n"
    audit = json.loads(audit_path.read_text())
    assert audit["analysis_status"] == "incomplete"
    assert audit["analysis_modules"]["publication"]["status"] == "incomplete"


def test_checkpoint_initialization_failure_does_not_block_audit(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    settings = argus_config.ArgusConfig()
    settings.extractor.out_dir = tmp_path / "output"
    settings.general.state_dir = tmp_path / "state"
    monkeypatch.setattr(
        "argus.operations.control.is_paused",
        lambda: False,
    )
    monkeypatch.setattr(
        orchestrator.extractor,
        "build_session",
        lambda config: object(),
    )
    monkeypatch.setattr(
        orchestrator.extractor,
        "find_folder_by_name",
        lambda *args: {"id": 47},
    )
    monkeypatch.setattr(
        orchestrator.extractor,
        "get_test_runs_in_folder",
        lambda *args: [{"id": 1, "key": "QA-C1"}],
    )
    monkeypatch.setattr(
        orchestrator.extractor,
        "get_project_status_ids",
        lambda *args: {"PASS": 39, "FAIL": 40},
    )
    monkeypatch.setattr(
        orchestrator,
        "_enumerate_folder_items",
        lambda *args, **kwargs: orchestrator._FolderEnumeration(
            emitted_keys=["QA-E1"],
            emitted_user_keys=[None],
            total_executed=1,
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_print_by_tester",
        lambda *args: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_write_folder_coverage",
        lambda *args, **kwargs: None,
    )
    run_batch = mock.MagicMock(return_value=[
        batch.KeyResult(
            key="QA-E1",
            status="ok",
            verdict="pass",
            findings=0,
        ),
    ])
    monkeypatch.setattr(orchestrator.batch, "run_batch", run_batch)
    publish = mock.MagicMock(return_value=True)
    monkeypatch.setattr(
        orchestrator,
        "_publish_folder_reports",
        publish,
    )
    monkeypatch.setattr(
        orchestrator.notify,
        "post_audit_started",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator.notify,
        "post_audit_done",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_record_run_cost",
        lambda *args, **kwargs: None,
    )
    mark_audit_running = mock.MagicMock()
    monkeypatch.setattr(
        "argus.operations.control.mark_audit_running",
        mark_audit_running,
    )
    mark_audit_done = mock.MagicMock()
    monkeypatch.setattr(
        "argus.operations.control.mark_audit_done",
        mark_audit_done,
    )
    monkeypatch.setattr(
        "argus.core.users.get_default_cache",
        lambda: SimpleNamespace(save=lambda: None),
    )
    start_checkpoint = mock.MagicMock(
        side_effect=RuntimeError("Report storage temporarily unavailable"),
    )
    write_checkpoint = mock.MagicMock()
    mark_publishing = mock.MagicMock()
    monkeypatch.setattr(
        "argus.reporting.publisher.start_audit_run",
        start_checkpoint,
    )
    monkeypatch.setattr(
        "argus.reporting.publisher.checkpoint_audit_key",
        write_checkpoint,
    )
    monkeypatch.setattr(
        "argus.reporting.publisher.mark_audit_publishing",
        mark_publishing,
    )

    assert orchestrator._run_folder_list("Sprint 47", settings) == 0

    run_batch.assert_called_once()
    publish.assert_called_once_with(
        settings.extractor.out_dir / "Sprint_47",
        settings,
    )
    write_checkpoint.assert_not_called()
    mark_publishing.assert_not_called()
    assert "continuing audit: RuntimeError: Report storage temporarily unavailable" in (
        capsys.readouterr().err
    )


def test_superseded_folder_run_skips_publication_and_finalization(
    tmp_path,
    monkeypatch,
    capsys,
):
    settings = argus_config.ArgusConfig()
    settings.extractor.out_dir = tmp_path / "output"
    settings.general.state_dir = tmp_path / "state"
    monkeypatch.setattr(
        "argus.operations.control.is_paused",
        lambda: False,
    )
    monkeypatch.setattr(
        orchestrator.extractor,
        "build_session",
        lambda config: object(),
    )
    monkeypatch.setattr(
        orchestrator.extractor,
        "find_folder_by_name",
        lambda *args: {"id": 47},
    )
    monkeypatch.setattr(
        orchestrator.extractor,
        "get_test_runs_in_folder",
        lambda *args: [{"id": 1, "key": "QA-C1"}],
    )
    monkeypatch.setattr(
        orchestrator.extractor,
        "get_project_status_ids",
        lambda *args: {"PASS": 39, "FAIL": 40},
    )
    monkeypatch.setattr(
        orchestrator,
        "_enumerate_folder_items",
        lambda *args, **kwargs: orchestrator._FolderEnumeration(
            emitted_keys=["QA-E1"],
            emitted_user_keys=[None],
            total_executed=1,
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_print_by_tester",
        lambda *args: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_write_folder_coverage",
        lambda *args, **kwargs: None,
    )
    run_batch = mock.MagicMock(return_value=[
        batch.KeyResult(key="QA-E1", status="ok"),
    ])
    monkeypatch.setattr(orchestrator.batch, "run_batch", run_batch)
    write_manifest = mock.MagicMock(return_value=None)
    monkeypatch.setattr(
        orchestrator,
        "_write_latest_run_manifest",
        write_manifest,
    )
    publish = mock.MagicMock(return_value=True)
    finalize = mock.MagicMock()
    monkeypatch.setattr(
        orchestrator,
        "_publish_folder_reports",
        publish,
    )
    monkeypatch.setattr(
        orchestrator,
        "_write_and_reconcile_failed_keys",
        finalize,
    )
    monkeypatch.setattr(
        orchestrator.notify,
        "post_audit_started",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator.notify,
        "post_audit_done",
        lambda *args, **kwargs: None,
    )
    mark_audit_running = mock.MagicMock()
    monkeypatch.setattr(
        "argus.operations.control.mark_audit_running",
        mark_audit_running,
    )
    mark_audit_done = mock.MagicMock()
    monkeypatch.setattr(
        "argus.operations.control.mark_audit_done",
        mark_audit_done,
    )
    monkeypatch.setattr(
        "argus.core.users.get_default_cache",
        lambda: SimpleNamespace(save=lambda: None),
    )
    monkeypatch.setattr(
        "argus.reporting.publisher.start_audit_run",
        lambda *args, **kwargs: None,
    )

    assert orchestrator._run_folder_list("Sprint 47", settings) == 0

    run_batch.assert_called_once()
    write_manifest.assert_called_once()
    publish.assert_not_called()
    finalize.assert_not_called()
    owner_token = mark_audit_running.call_args.kwargs["owner_token"]
    # Preparation, auditing, and report preparation share one owner token.
    assert [
        call.kwargs["owner_token"] for call in mark_audit_running.call_args_list
    ] == [owner_token, owner_token, owner_token]
    assert mark_audit_done.call_args_list == [
        mock.call(owner_token),
        mock.call(owner_token),
    ]
    assert '"event": "argus_run_superseded"' in capsys.readouterr().err


def test_single_key_local_refresh_republishes_folder(tmp_path, monkeypatch):
    settings = argus_config.ArgusConfig()
    settings.extractor.out_dir = tmp_path
    folder = tmp_path / "Sprint_47"
    work_dir = folder / "run" / "QA-E1"
    work_dir.mkdir(parents=True)
    (folder / orchestrator.FOLDER_META_FILENAME).write_text("{}")
    reconcile = mock.MagicMock()
    publish = mock.MagicMock(return_value=True)
    monkeypatch.setattr(orchestrator, "_reconcile_failed_keys", reconcile)
    monkeypatch.setattr(orchestrator, "_publish_folder_reports", publish)

    orchestrator._refresh_folder_report(
        work_dir,
        tmp_path,
        "https://jira.test",
        settings=settings,
    )

    reconcile.assert_called_once_with(folder)
    publish.assert_called_once_with(folder, settings)


def test_write_latest_run_manifest_limits_report_to_current_successes(
        tmp_path, monkeypatch):
    for key in ("QA-E1", "QA-E2", "QA-E4"):
        execution_dir = tmp_path / "run" / key
        execution_dir.mkdir(parents=True)
        (execution_dir / "audit.json").write_text("{}")
    scans = []
    original_scan = orchestrator.exec_paths.iter_audit_jsons

    def scan_once(root):
        scans.append(root)
        yield from original_scan(root)

    monkeypatch.setattr(orchestrator.exec_paths, "iter_audit_jsons", scan_once)
    monkeypatch.setattr(
        orchestrator.exec_paths, "find_execution_dir",
        lambda *a, **kw: pytest.fail("manifest must not walk once per key"),
    )

    manifest_path = orchestrator._write_latest_run_manifest(
        tmp_path,
        folder_name="Sprint 47",
        scope_keys=["QA-E1", "QA-E2", "QA-E3"],
        selected_keys=["QA-E1", "QA-E2"],
        results=[
            batch.KeyResult(key="QA-E1", status="ok"),
            batch.KeyResult(key="QA-E2", status="analysis_incomplete"),
        ],
        synthetic_keys=[],
        started_at=dt.datetime(
            2026, 9, 8, 12, 0, tzinfo=dt.timezone.utc
        ),
    )

    assert manifest_path is not None
    manifest = json.loads(manifest_path.read_text())
    assert manifest["scope_keys"] == [
        "QA-E1", "QA-E2", "QA-E3"
    ]
    assert manifest["audit_keys"] == ["QA-E1"]
    assert scans == [tmp_path]
    assert manifest["failed_keys"] == ["QA-E2"]
    assert manifest["recovered_at"] == {}
    assert manifest["failed_at"] == {}
    assert "QA-E4" not in manifest["audit_keys"]


def test_single_key_refresh_adopts_retry_into_latest_manifest(
    tmp_path,
    monkeypatch,
):
    settings = argus_config.ArgusConfig()
    settings.extractor.out_dir = tmp_path
    folder = tmp_path / "Sprint_47"
    work_dir = folder / "run" / "QA-E1"
    work_dir.mkdir(parents=True)
    (work_dir / "audit.json").write_text("{}")
    (folder / orchestrator.FOLDER_META_FILENAME).write_text("{}")
    (folder / orchestrator._LATEST_RUN_MANIFEST).write_text(json.dumps({
        "audit_keys": [],
        "failed_keys": ["QA-E1"],
        "statuses": {"QA-E1": "analysis_incomplete"},
    }))
    (folder / "_failed_keys.txt").write_text(
        "QA-E1\nQA-E2\n"
    )
    monkeypatch.setattr(
        orchestrator,
        "_publish_folder_reports",
        lambda *a: True,
    )

    orchestrator._refresh_folder_report(
        work_dir,
        tmp_path,
        "https://jira.test",
        settings=settings,
    )

    manifest = json.loads(
        (folder / orchestrator._LATEST_RUN_MANIFEST).read_text()
    )
    assert manifest["audit_keys"] == ["QA-E1"]
    assert manifest["failed_keys"] == []
    assert manifest["statuses"] == {"QA-E1": "ok"}
    assert set(manifest["recovered_at"]) == {"QA-E1"}
    assert orchestrator._parse_manifest_timestamp(
        manifest["recovered_at"]["QA-E1"]
    ) is not None
    assert (folder / "_failed_keys.txt").read_text() == "QA-E2\n"


def test_single_key_refresh_does_not_adopt_incomplete_audit(
    tmp_path,
    monkeypatch,
):
    settings = argus_config.ArgusConfig()
    settings.extractor.out_dir = tmp_path
    folder = tmp_path / "Sprint_47"
    work_dir = folder / "run" / "QA-E1"
    work_dir.mkdir(parents=True)
    (work_dir / "audit.json").write_text(json.dumps({
        "overall_verdict": "concerns",
        "analysis_status": "incomplete",
    }))
    (folder / orchestrator.FOLDER_META_FILENAME).write_text("{}")
    manifest = {
        "audit_keys": ["QA-E1"],
        "failed_keys": [],
        "statuses": {"QA-E1": "ok"},
        "recovered_at": {
            "QA-E1": "2026-09-08T12:00:00+00:00",
        },
    }
    (folder / orchestrator._LATEST_RUN_MANIFEST).write_text(
        json.dumps(manifest)
    )
    monkeypatch.setattr(
        orchestrator,
        "_publish_folder_reports",
        lambda *a: True,
    )

    orchestrator._refresh_folder_report(
        work_dir,
        tmp_path,
        "https://jira.test",
        settings=settings,
    )

    updated = json.loads(
        (folder / orchestrator._LATEST_RUN_MANIFEST).read_text()
    )
    assert updated["audit_keys"] == []
    assert updated["failed_keys"] == ["QA-E1"]
    assert updated["statuses"] == {
        "QA-E1": "analysis_incomplete",
    }
    assert updated["recovered_at"] == {}
    assert set(updated["failed_at"]) == {"QA-E1"}
    assert orchestrator._parse_manifest_timestamp(
        updated["failed_at"]["QA-E1"]
    ) is not None
    assert (folder / "_failed_keys.txt").read_text() == "QA-E1\n"


def test_single_key_refresh_rolls_back_failed_local_publication(
    tmp_path,
    monkeypatch,
):
    settings = argus_config.ArgusConfig()
    settings.extractor.out_dir = tmp_path
    folder = tmp_path / "Sprint_47"
    work_dir = folder / "run" / "QA-E1"
    work_dir.mkdir(parents=True)
    (work_dir / "audit.json").write_text(json.dumps({
        "overall_verdict": "pass",
        "analysis_status": "complete",
    }))
    (folder / orchestrator.FOLDER_META_FILENAME).write_text("{}")
    (folder / orchestrator._LATEST_RUN_MANIFEST).write_text(json.dumps({
        "audit_keys": [],
        "failed_keys": ["QA-E1"],
        "statuses": {"QA-E1": "analysis_incomplete"},
        "recovered_at": {},
    }))
    (folder / "_failed_keys.txt").write_text("QA-E1\n")
    monkeypatch.setattr(
        orchestrator,
        "_publish_folder_reports",
        lambda *a: False,
    )

    with pytest.raises(
        RuntimeError,
        match="folder report publication failed",
    ):
        orchestrator._refresh_folder_report(
            work_dir,
            tmp_path,
            "https://jira.test",
            settings=settings,
        )

    manifest = json.loads(
        (folder / orchestrator._LATEST_RUN_MANIFEST).read_text()
    )
    assert manifest["audit_keys"] == []
    assert manifest["failed_keys"] == ["QA-E1"]
    assert manifest["statuses"] == {
        "QA-E1": "analysis_incomplete",
    }
    assert manifest["recovered_at"] == {}
    assert set(manifest["failed_at"]) == {"QA-E1"}
    assert (folder / "_failed_keys.txt").read_text() == "QA-E1\n"


def test_latest_run_manifest_mutations_are_serialized(
    tmp_path,
    monkeypatch,
):
    folder = tmp_path / "Sprint_47"
    execution_dir = folder / "run" / "QA-E1"
    execution_dir.mkdir(parents=True)
    (execution_dir / "audit.json").write_text("{}")

    original_acquire = orchestrator.audit_lock.acquire_key
    original_write = orchestrator.atomic_io.atomic_write_json
    first_write_started = threading.Event()
    release_first_write = threading.Event()
    single_update_attempted = threading.Event()
    second_write_started = threading.Event()
    write_guard = threading.Lock()
    write_count = 0
    lock_options: list[dict] = []
    errors: list[BaseException] = []

    def observed_acquire(*args, **kwargs):
        lock_options.append(dict(kwargs))
        if kwargs.get("owner") == "argus.single_manifest":
            single_update_attempted.set()
        return original_acquire(*args, **kwargs)

    def controlled_write(path, payload):
        nonlocal write_count
        with write_guard:
            write_count += 1
            current_write = write_count
        if current_write == 1:
            first_write_started.set()
            assert release_first_write.wait(timeout=2)
        else:
            second_write_started.set()
        original_write(path, payload)

    def run(target):
        try:
            target()
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(
        orchestrator.audit_lock,
        "acquire_key",
        observed_acquire,
    )
    monkeypatch.setattr(
        orchestrator.atomic_io,
        "atomic_write_json",
        controlled_write,
    )

    folder_run = threading.Thread(
        target=run,
        args=(lambda: orchestrator._write_latest_run_manifest(
            folder,
            folder_name="Sprint 47",
            scope_keys=["QA-E1"],
            selected_keys=["QA-E1"],
            results=[batch.KeyResult(key="QA-E1", status="ok")],
            synthetic_keys=[],
            started_at=dt.datetime(
                2026,
                9,
                8,
                12,
                0,
                tzinfo=dt.timezone.utc,
            ),
        ),),
    )
    single_retry = threading.Thread(
        target=run,
        args=(lambda: orchestrator._adopt_key_into_latest_run_manifest(
            folder,
            "QA-E2",
        ),),
    )

    folder_run.start()
    assert first_write_started.wait(timeout=2)
    single_retry.start()
    assert single_update_attempted.wait(timeout=2)
    assert not second_write_started.is_set()
    release_first_write.set()
    folder_run.join(timeout=2)
    single_retry.join(timeout=2)

    assert not folder_run.is_alive()
    assert not single_retry.is_alive()
    assert errors == []
    assert write_count == 3
    assert lock_options
    assert all(
        options["stale_after_seconds"]
        == orchestrator._LATEST_RUN_MANIFEST_LEASE_SECONDS
        and options["lease_duration_seconds"]
        == orchestrator._LATEST_RUN_MANIFEST_LEASE_SECONDS
        for options in lock_options
    )
    manifest = json.loads(
        (folder / orchestrator._LATEST_RUN_MANIFEST).read_text()
    )
    assert manifest["audit_keys"] == ["QA-E1", "QA-E2"]
    assert manifest["statuses"] == {
        "QA-E1": "ok",
        "QA-E2": "ok",
    }
    assert set(manifest["recovered_at"]) == {"QA-E2"}


def test_failed_key_writes_and_reconciliation_are_serialized(
    tmp_path,
    monkeypatch,
):
    folder = tmp_path / "Sprint_47"
    run_started_at = dt.datetime.now(dt.timezone.utc)
    _make_audited_pair(
        folder,
        "run",
        "QA-E1",
        "2026-09-08T12:00:00Z",
    )
    stale_result = orchestrator._write_latest_run_manifest(
        folder,
        folder_name="Sprint 47",
        scope_keys=["QA-E1"],
        selected_keys=["QA-E1"],
        results=[batch.KeyResult(key="QA-E1", status="failed")],
        synthetic_keys=[],
        started_at=run_started_at,
    )
    orchestrator._adopt_key_into_latest_run_manifest(
        folder,
        "QA-E1",
    )
    write_started = threading.Event()
    release_write = threading.Event()
    errors: list[BaseException] = []
    original_write = batch.write_failed_keys

    def controlled_write(results, out_dir):
        result = original_write(results, out_dir)
        write_started.set()
        assert release_write.wait(timeout=2)
        return result

    def run(target):
        try:
            target()
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(batch, "write_failed_keys", controlled_write)
    stale_results = [
        batch.KeyResult(key="QA-E1", status="failed"),
    ]
    batch_finish = threading.Thread(
        target=run,
        args=(lambda: orchestrator._write_and_reconcile_failed_keys(
            stale_results,
            folder,
            run_started_at=run_started_at,
        ),),
    )
    retry_finish = threading.Thread(
        target=run,
        args=(lambda: orchestrator._reconcile_failed_keys(folder),),
    )

    batch_finish.start()
    assert write_started.wait(timeout=2)
    retry_finish.start()
    retry_finish.join(timeout=0.1)
    assert retry_finish.is_alive()
    release_write.set()
    batch_finish.join(timeout=2)
    retry_finish.join(timeout=2)

    assert not batch_finish.is_alive()
    assert not retry_finish.is_alive()
    assert errors == []
    assert not (folder / "_failed_keys.txt").exists()


def test_batch_failure_is_retained_when_only_old_complete_audit_exists(
    tmp_path,
):
    folder = tmp_path / "Sprint_47"
    audit_dir = _make_audited_pair(
        folder,
        "run",
        "QA-E1",
        "2026-09-08T12:00:00Z",
    )
    run_started_at = dt.datetime.now(dt.timezone.utc)
    stale_result = orchestrator._write_latest_run_manifest(
        folder,
        folder_name="Sprint 47",
        scope_keys=["QA-E1"],
        selected_keys=["QA-E1"],
        results=[batch.KeyResult(key="QA-E1", status="failed")],
        synthetic_keys=[],
        started_at=run_started_at,
    )
    (audit_dir / "audit.json").write_text(json.dumps({
        "overall_verdict": "pass",
        "analysis_status": "complete",
        "publication_rewrite": True,
    }))

    failed_path = orchestrator._write_and_reconcile_failed_keys(
        [batch.KeyResult(key="QA-E1", status="failed")],
        folder,
        run_started_at=run_started_at,
    )

    assert failed_path == folder / "_failed_keys.txt"
    assert failed_path.read_text() == "QA-E1\n"


def test_newer_single_key_failure_overrides_stale_batch_success(tmp_path):
    folder = tmp_path / "Sprint_47"
    _make_audited_pair(
        folder,
        "run",
        "QA-E1",
        "2026-09-08T12:00:00Z",
    )
    run_started_at = dt.datetime.now(dt.timezone.utc)
    orchestrator._write_latest_run_manifest(
        folder,
        folder_name="Sprint 47",
        scope_keys=["QA-E1"],
        selected_keys=["QA-E1"],
        results=[batch.KeyResult(key="QA-E1", status="ok")],
        synthetic_keys=[],
        started_at=run_started_at,
    )
    orchestrator._record_single_key_failure(
        folder,
        "QA-E1",
        recovery_token=None,
        require_recovery_match=False,
    )

    failed_path = orchestrator._write_and_reconcile_failed_keys(
        [batch.KeyResult(key="QA-E1", status="ok")],
        folder,
        run_started_at=run_started_at,
    )

    manifest = json.loads(
        (folder / orchestrator._LATEST_RUN_MANIFEST).read_text()
    )
    assert manifest["audit_keys"] == []
    assert manifest["failed_keys"] == ["QA-E1"]
    assert manifest["statuses"]["QA-E1"] == "analysis_incomplete"
    assert failed_path == folder / "_failed_keys.txt"
    assert failed_path.read_text() == "QA-E1\n"


def test_single_key_transitions_are_journaled_before_first_manifest(tmp_path):
    folder = tmp_path / "Sprint_47"
    folder.mkdir()
    run_started_at = dt.datetime.now(dt.timezone.utc)

    recovery_token = orchestrator._adopt_key_into_latest_run_manifest(
        folder,
        "QA-E1",
    )

    assert recovery_token is not None
    assert not (folder / orchestrator._LATEST_RUN_MANIFEST).exists()
    journal = json.loads(
        (folder / orchestrator._LATEST_RUN_TRANSITIONS).read_text()
    )
    assert journal["recovered_at"] == {
        "QA-E1": recovery_token,
    }
    assert journal["failed_at"] == {}

    work_dir = folder / "run" / "QA-E1"
    work_dir.mkdir(parents=True)
    (work_dir / "audit.json").write_text("{}")
    manifest_path = orchestrator._write_latest_run_manifest(
        folder,
        folder_name="Sprint 47",
        scope_keys=["QA-E1"],
        selected_keys=["QA-E1"],
        results=[batch.KeyResult(key="QA-E1", status="failed")],
        synthetic_keys=[],
        started_at=run_started_at,
    )

    assert manifest_path is not None
    manifest = json.loads(manifest_path.read_text())
    assert manifest["audit_keys"] == ["QA-E1"]
    assert manifest["failed_keys"] == []
    assert manifest["statuses"]["QA-E1"] == "ok"


def test_manifest_creation_does_not_prune_active_publication_token(tmp_path):
    folder = tmp_path / "Sprint_47"
    folder.mkdir()
    recovery_token = orchestrator._adopt_key_into_latest_run_manifest(
        folder,
        "QA-E1",
    )
    assert recovery_token is not None
    run_started_at = dt.datetime.now(dt.timezone.utc)

    stale_result = orchestrator._write_latest_run_manifest(
        folder,
        folder_name="Sprint 47",
        scope_keys=["QA-E1"],
        selected_keys=["QA-E1"],
        results=[batch.KeyResult(key="QA-E1", status="ok")],
        synthetic_keys=[],
        started_at=run_started_at,
    )
    orchestrator._record_single_key_failure(
        folder,
        "QA-E1",
        recovery_token=recovery_token,
        require_recovery_match=True,
    )

    journal = json.loads(
        (folder / orchestrator._LATEST_RUN_TRANSITIONS).read_text()
    )
    assert journal["recovered_at"] == {}
    assert set(journal["failed_at"]) == {"QA-E1"}
    assert (folder / "_failed_keys.txt").read_text() == "QA-E1\n"


def test_batch_identity_uses_microsecond_precision(tmp_path):
    folder = tmp_path / "Sprint_47"
    work_dir = folder / "run" / "QA-E1"
    work_dir.mkdir(parents=True)
    (work_dir / "audit.json").write_text("{}")
    first_start = dt.datetime(
        2026,
        9,
        8,
        12,
        0,
        0,
        100,
        tzinfo=dt.timezone.utc,
    )
    second_start = first_start.replace(microsecond=200)
    orchestrator._write_latest_run_manifest(
        folder,
        folder_name="Sprint 47",
        scope_keys=["QA-E1"],
        selected_keys=["QA-E1"],
        results=[batch.KeyResult(key="QA-E1", status="failed")],
        synthetic_keys=[],
        started_at=first_start,
    )

    failed_path = orchestrator._write_and_reconcile_failed_keys(
        [batch.KeyResult(key="QA-E1", status="ok")],
        folder,
        run_started_at=second_start,
    )

    manifest = json.loads(
        (folder / orchestrator._LATEST_RUN_MANIFEST).read_text()
    )
    assert manifest["started_at"] == first_start.isoformat(
        timespec="microseconds"
    )
    assert manifest["statuses"]["QA-E1"] == "failed"
    assert failed_path == folder / "_failed_keys.txt"


def test_older_folder_run_cannot_overwrite_newer_manifest(tmp_path):
    folder = tmp_path / "Sprint_47"
    work_dir = folder / "run" / "QA-E1"
    work_dir.mkdir(parents=True)
    (work_dir / "audit.json").write_text("{}")
    older_start = dt.datetime(
        2026,
        9,
        8,
        12,
        0,
        tzinfo=dt.timezone.utc,
    )
    newer_start = older_start + dt.timedelta(seconds=1)
    orchestrator._write_latest_run_manifest(
        folder,
        folder_name="Sprint 47",
        scope_keys=["QA-E1"],
        selected_keys=["QA-E1"],
        results=[batch.KeyResult(key="QA-E1", status="ok")],
        synthetic_keys=[],
        started_at=newer_start,
    )

    stale_result = orchestrator._write_latest_run_manifest(
        folder,
        folder_name="Sprint 47",
        scope_keys=["QA-E1"],
        selected_keys=["QA-E1"],
        results=[batch.KeyResult(key="QA-E1", status="failed")],
        synthetic_keys=[],
        started_at=older_start,
    )

    assert stale_result is None
    manifest = json.loads(
        (folder / orchestrator._LATEST_RUN_MANIFEST).read_text()
    )
    assert manifest["started_at"] == newer_start.isoformat(
        timespec="microseconds"
    )
    assert manifest["statuses"]["QA-E1"] == "ok"
    assert manifest["failed_keys"] == []


def test_cap_report_archives_keeps_newest(tmp_path):
    """Verify report archive pruning keeps the newest files."""
    (tmp_path / "_argus.html").write_text("canonical")
    for i in range(15):
        stamp = f"2026-07-{i + 1:02d}_120000"
        (tmp_path / f"_argus_{stamp}.html").write_text("archive")
        (tmp_path / f"_report_{stamp}.md").write_text("report")

    orchestrator._cap_report_archives(tmp_path, keep=10)

    html = sorted(p.name for p in tmp_path.glob("_argus_*.html"))
    md = sorted(p.name for p in tmp_path.glob("_report_*.md"))
    assert len(html) == 10 and len(md) == 10
    assert html[0] == "_argus_2026-07-06_120000.html"
    assert (tmp_path / "_argus.html").exists()


def test_publication_failure_marks_audits_incomplete_and_refreshes_local_report(
    tmp_path,
    monkeypatch,
):
    folder_root = tmp_path / "Sprint_47"
    exec_dir = folder_root / "QA-E1"
    exec_dir.mkdir(parents=True)
    (exec_dir / "audit.json").write_text(json.dumps({
        "overall_verdict": "pass",
        "summary": "complete before publication",
        "findings": [],
        "schema_version": 7,
        "analysis_status": "complete",
        "analysis_modules": {
            "model": {"status": "complete"},
        },
    }))
    (exec_dir / "metadata.json").write_text(json.dumps({
        "test_results": [{
            "executed_by_name": "Tester",
            "status": "Pass",
            "steps": [],
        }],
    }))
    settings = argus_config.ArgusConfig()
    settings.extractor.out_dir = tmp_path

    from argus.reporting import publisher
    monkeypatch.setattr(
        publisher,
        "publish",
        mock.MagicMock(side_effect=RuntimeError("Report publication failed")),
    )

    assert (
        orchestrator._publish_folder_reports(folder_root, settings)
        is False
    )

    audit = json.loads((exec_dir / "audit.json").read_text())
    assert audit["analysis_status"] == "incomplete"
    assert audit["analysis_modules"]["publication"] == {
        "status": "incomplete",
        "diagnostic": "RuntimeError: Report publication failed",
    }
    local_html = (folder_root / "_argus.html").read_text()
    assert "argus-detail-slot" in local_html
    details = json.loads(
        (folder_root / "_argus_details" / "0000.json").read_text()
    )["0"]
    assert "Required analysis incomplete." in details
    assert "Completed audit results, findings, and evidence are shown below" in details
    assert "publication" in details
    assert "Report publication failed" in details


def test_merge_blocked_base_finding_is_history_aware(tmp_path, monkeypatch):
    work_dir = tmp_path / "F" / "run" / "tester" / "QA-E1"
    work_dir.mkdir(parents=True)
    (work_dir / "audit.json").write_text(json.dumps({
        "overall_verdict": "pass", "summary": "s", "findings": [],
    }))
    (work_dir / "metadata.json").write_text(json.dumps({
        "test_results": [{"test_case_key": "QA-T1",
                          "marketplace": "US", "status": "Blocked",
                          "comment": "env down",
                          "trace_links": [], "steps": []}],
    }))

    captured = {}

    def spy_r20(metadata, history_idx=None, current_e_key=None):
        captured["history_idx"] = history_idx
        captured["current_e_key"] = current_e_key
        return None

    from argus.domain import workflow_rules as wr
    monkeypatch.setattr(wr, "build_r20_finding", spy_r20)
    monkeypatch.setattr(wr, "build_r21_finding", lambda metadata: None)

    orchestrator._merge_blocked_base_finding(work_dir)

    assert captured["history_idx"] is not None
    assert captured["current_e_key"] == "QA-E1"


def test_enumerate_folder_items_auth_error_still_fatal(monkeypatch):
    def auth_fail(session, base_url, run_id):
        raise orchestrator.extractor.AuthError("HTTP 401")

    monkeypatch.setattr(orchestrator.extractor,
                        "get_testrun_items", auth_fail)

    with pytest.raises(orchestrator.extractor.AuthError):
        orchestrator._enumerate_folder_items(
            object(), "https://jira.test",
            [{"id": 1, "key": "QA-C1"}],
            executed_ids={39}, na_ids=set(), audited={},
        )


def test_folder_run_announces_preparing_before_enumeration(
    tmp_path: Path,
    monkeypatch,
):
    settings = argus_config.ArgusConfig()
    settings.extractor.out_dir = tmp_path / "output"
    settings.general.state_dir = tmp_path / "state"
    monkeypatch.setenv("ARGUS_RUN_ID", "run-folder-1")
    monkeypatch.setenv("ARGUS_REQUESTED_BY", "alice")
    monkeypatch.setenv("ARGUS_TARGET_LABEL", "QA-C21462")
    monkeypatch.setattr("argus.operations.control.is_paused", lambda: False)
    events_log: list[tuple] = []
    monkeypatch.setattr(
        "argus.operations.control.mark_audit_running",
        lambda *args, **kwargs: events_log.append(("mark", args, kwargs)),
    )
    monkeypatch.setattr(
        "argus.operations.control.mark_audit_done",
        lambda token: events_log.append(("done", token)),
    )
    monkeypatch.setattr(
        "argus.reporting.publisher.announce_active_audit",
        lambda **kwargs: events_log.append(("announce", kwargs)),
    )
    monkeypatch.setattr(
        "argus.reporting.publisher.clear_active_audit",
        lambda **kwargs: events_log.append(("clear", kwargs)),
    )
    monkeypatch.setattr(
        "argus.reporting.publisher.fail_active_audit",
        lambda **kwargs: events_log.append(("fail", kwargs)),
    )
    monkeypatch.setattr(
        orchestrator.extractor,
        "build_session",
        lambda config: events_log.append(("jira-session",)) or object(),
    )
    monkeypatch.setattr(
        orchestrator.extractor, "find_folder_by_name", lambda *args: {"id": 47},
    )
    monkeypatch.setattr(
        orchestrator.extractor,
        "get_test_runs_in_folder",
        lambda *args: [{"id": 1, "key": "QA-C1"}],
    )
    monkeypatch.setattr(
        orchestrator.extractor,
        "get_project_status_ids",
        lambda *args: {"PASS": 39, "FAIL": 40},
    )
    monkeypatch.setattr(
        orchestrator,
        "_enumerate_folder_items",
        lambda *args, **kwargs: events_log.append(("enumerate",))
        or orchestrator._FolderEnumeration(total_executed=1, total_skipped_dup=1),
    )
    monkeypatch.setattr(orchestrator, "_write_folder_coverage",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "_reconcile_failed_keys", lambda *a: None)
    monkeypatch.setattr(orchestrator, "_publish_folder_reports", lambda *a: True)
    monkeypatch.setattr(
        "argus.core.users.get_default_cache",
        lambda: SimpleNamespace(save=lambda: None),
    )

    assert orchestrator._run_folder_list("Sprint 47", settings) == 0

    kinds = [entry[0] for entry in events_log]
    assert kinds.index("mark") < kinds.index("jira-session")
    assert kinds.index("announce") < kinds.index("enumerate")
    _kind, args, kwargs = events_log[kinds.index("mark")]
    assert args == ("Sprint 47", 0)
    assert kwargs == {
        "owner_token": "run-folder-1",
        "phase": "preparing",
        "requested_by": "alice",
        "target": "QA-C21462",
    }
    announce = events_log[kinds.index("announce")][1]
    assert announce["phase"] == "preparing"
    assert announce["target"] == "Sprint 47"
    assert announce["label"] == "QA-C21462"
    assert announce["requested_by"] == "alice"
    assert announce["run_id"] == "run-folder-1"
    assert announce["total"] is None
    assert [
        entry[1]["phase"] for entry in events_log if entry[0] == "announce"
    ] == ["preparing", "publishing"]
    # No new keys: the run is cleared, never marked failed.
    assert ("clear", {"run_id": "run-folder-1"}) in events_log
    assert ("done", "run-folder-1") in events_log
    assert "fail" not in kinds
    assert kinds[-1] == "done"


def test_folder_run_failure_during_preparation_is_marked_failed(
    tmp_path: Path,
    monkeypatch,
):
    settings = argus_config.ArgusConfig()
    settings.extractor.out_dir = tmp_path / "output"
    monkeypatch.setenv("ARGUS_RUN_ID", "run-folder-2")
    monkeypatch.setattr("argus.operations.control.is_paused", lambda: False)
    mark_running = mock.MagicMock()
    mark_done = mock.MagicMock()
    monkeypatch.setattr(
        "argus.operations.control.mark_audit_running", mark_running,
    )
    monkeypatch.setattr("argus.operations.control.mark_audit_done", mark_done)
    announce = mock.MagicMock()
    fail = mock.MagicMock()
    clear = mock.MagicMock()
    monkeypatch.setattr(
        "argus.reporting.publisher.announce_active_audit", announce,
    )
    monkeypatch.setattr("argus.reporting.publisher.fail_active_audit", fail)
    monkeypatch.setattr("argus.reporting.publisher.clear_active_audit", clear)
    monkeypatch.setattr(
        orchestrator.extractor, "build_session", lambda config: object(),
    )
    monkeypatch.setattr(
        orchestrator.extractor, "find_folder_by_name", lambda *args: None,
    )

    assert orchestrator._run_folder_list("Missing", settings) == 1

    announce.assert_called_once()
    fail.assert_called_once_with(
        run_id="run-folder-2",
        diagnostic="audit exited with rc=1 before publication",
    )
    clear.assert_not_called()
    mark_done.assert_called_once_with("run-folder-2")

    # An exception is reported with its type and still re-raised.
    fail.reset_mock()
    mark_done.reset_mock()
    monkeypatch.setattr(
        orchestrator.extractor,
        "build_session",
        mock.MagicMock(side_effect=RuntimeError("Jira unreachable")),
    )
    with pytest.raises(RuntimeError, match="Jira unreachable"):
        orchestrator._run_folder_list("Missing", settings)
    fail.assert_called_once_with(
        run_id="run-folder-2",
        diagnostic="RuntimeError: Jira unreachable",
    )
    mark_done.assert_called_once_with("run-folder-2")


def test_paused_folder_run_clears_bot_launch_record(tmp_path, monkeypatch):
    settings = argus_config.ArgusConfig()
    monkeypatch.setenv("ARGUS_RUN_ID", "run-paused")
    monkeypatch.setattr("argus.operations.control.is_paused", lambda: True)
    monkeypatch.setattr(
        "argus.operations.control.status",
        lambda: {"paused_by": "alice", "paused_at": "now", "reason": None},
    )
    mark_done = mock.MagicMock()
    monkeypatch.setattr("argus.operations.control.mark_audit_done", mark_done)
    announce = mock.MagicMock()
    clear = mock.MagicMock()
    monkeypatch.setattr(
        "argus.reporting.publisher.announce_active_audit", announce,
    )
    monkeypatch.setattr("argus.reporting.publisher.clear_active_audit", clear)
    monkeypatch.setattr(
        orchestrator.extractor,
        "build_session",
        lambda config: pytest.fail("paused run must not contact Jira"),
    )

    assert orchestrator._run_folder_list("Sprint 47", settings) == 0

    announce.assert_not_called()
    clear.assert_called_once_with(run_id="run-paused")
    mark_done.assert_called_once_with("run-paused")


def test_single_key_announces_preparing_then_auditing_and_clears(
    tmp_path, monkeypatch,
):
    settings = argus_config.ArgusConfig()
    settings.extractor.out_dir = tmp_path
    args = SimpleNamespace(
        execution_key="QA-E1",
        testrun_id=None,
        item_id=None,
        skip_extract=True,
        skip_audit=False,
    )
    monkeypatch.setenv("ARGUS_RUN_ID", "run-single-1")
    monkeypatch.setenv("ARGUS_REQUESTED_BY", "alice")
    monkeypatch.delenv("ARGUS_TARGET_LABEL", raising=False)
    phases: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "argus.operations.control.mark_audit_running",
        lambda folder, total, **kwargs: phases.append(
            ("control", kwargs["phase"]),
        ),
    )
    mark_done = mock.MagicMock()
    monkeypatch.setattr("argus.operations.control.mark_audit_done", mark_done)
    monkeypatch.setattr(
        "argus.reporting.publisher.announce_active_audit",
        lambda **kwargs: phases.append(("publisher", kwargs["phase"])),
    )
    clear = mock.MagicMock()
    fail = mock.MagicMock()
    monkeypatch.setattr("argus.reporting.publisher.clear_active_audit", clear)
    monkeypatch.setattr("argus.reporting.publisher.fail_active_audit", fail)
    monkeypatch.setattr(
        orchestrator,
        "_run_single_locked",
        lambda *unused: phases.append(("locked", "run")) or 0,
    )
    monkeypatch.setattr(orchestrator.events, "emit", lambda *a, **k: None)

    assert orchestrator._run_single(args, settings) == 0

    assert phases == [
        ("control", "preparing"),
        ("publisher", "preparing"),
        ("control", "auditing"),
        ("publisher", "auditing"),
        ("locked", "run"),
    ]
    clear.assert_called_once_with(run_id="run-single-1")
    fail.assert_not_called()
    mark_done.assert_called_once_with("run-single-1")


def test_single_key_failure_marks_active_audit_failed(tmp_path, monkeypatch):
    settings = argus_config.ArgusConfig()
    settings.extractor.out_dir = tmp_path
    args = SimpleNamespace(
        execution_key="QA-E2",
        testrun_id=None,
        item_id=None,
        skip_extract=True,
        skip_audit=False,
    )
    monkeypatch.setenv("ARGUS_RUN_ID", "run-single-2")
    announce = mock.MagicMock()
    monkeypatch.setattr(
        "argus.reporting.publisher.announce_active_audit", announce,
    )
    clear = mock.MagicMock()
    fail = mock.MagicMock()
    monkeypatch.setattr("argus.reporting.publisher.clear_active_audit", clear)
    monkeypatch.setattr("argus.reporting.publisher.fail_active_audit", fail)
    mark_done = mock.MagicMock()
    monkeypatch.setattr("argus.operations.control.mark_audit_done", mark_done)
    monkeypatch.setattr(orchestrator, "_run_single_locked", lambda *unused: 2)
    monkeypatch.setattr(orchestrator.events, "emit", lambda *a, **k: None)

    assert orchestrator._run_single(args, settings) == 2

    assert announce.call_args.kwargs["target"] == "QA-E2"
    assert announce.call_args.kwargs["total"] == 1
    fail.assert_called_once_with(
        run_id="run-single-2",
        diagnostic="audit exited with rc=2 before publication",
    )
    clear.assert_not_called()
    mark_done.assert_called_once_with("run-single-2")


def test_announcement_failures_never_break_the_audit(tmp_path, monkeypatch, capsys):
    settings = argus_config.ArgusConfig()
    settings.extractor.out_dir = tmp_path
    args = SimpleNamespace(
        execution_key="QA-E3",
        testrun_id=None,
        item_id=None,
        skip_extract=True,
        skip_audit=False,
    )
    boom = mock.MagicMock(side_effect=RuntimeError("report storage unavailable"))
    monkeypatch.setattr("argus.reporting.publisher.announce_active_audit", boom)
    monkeypatch.setattr("argus.reporting.publisher.clear_active_audit", boom)
    monkeypatch.setattr(
        "argus.operations.control.mark_audit_running",
        mock.MagicMock(side_effect=OSError("disk full")),
    )
    monkeypatch.setattr(
        "argus.operations.control.mark_audit_done",
        mock.MagicMock(side_effect=OSError("disk full")),
    )
    monkeypatch.setattr(orchestrator, "_run_single_locked", lambda *unused: 0)
    monkeypatch.setattr(orchestrator.events, "emit", lambda *a, **k: None)

    assert orchestrator._run_single(args, settings) == 0

    err = capsys.readouterr().err
    assert "audit status 'preparing' not published: RuntimeError: report storage unavailable" in err
    assert "audit status 'preparing' not recorded: OSError: disk full" in err
    assert "audit status not resolved" in err
    assert "audit status not cleared" in err
