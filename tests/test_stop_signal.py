"""SIGTERM handling: stop_signal module and the orchestrator's stopped path."""
from __future__ import annotations

import signal
import threading
from types import SimpleNamespace
from unittest import mock

import pytest

from argus import config as argus_config
from argus.operations import stop_signal
from argus.pipeline import orchestrator


@pytest.fixture(autouse=True)
def _reset_stop_signal(monkeypatch):
    """Never let a test leave a stop request or callback behind."""
    stop_signal._EVENT.clear()
    monkeypatch.setattr(stop_signal, "_CALLBACKS", [])
    monkeypatch.setattr(stop_signal, "_INSTALLED", False)
    yield
    stop_signal._EVENT.clear()


def test_handle_stop_runs_callbacks_flushes_and_exits_143(monkeypatch, caplog):
    calls: list[str] = []
    exits: list[int] = []
    monkeypatch.setattr(stop_signal, "_exit", exits.append)
    stop_signal.register_on_stop(lambda: calls.append("first"))
    stop_signal.register_on_stop(lambda: calls.append("second"))

    assert stop_signal.requested() is False
    with caplog.at_level("WARNING", logger="argus.stop_signal"):
        stop_signal.handle_stop(signal.SIGTERM, None)

    assert stop_signal.requested() is True
    # Most recently registered (innermost) callback runs first.
    assert calls == ["second", "first"]
    assert exits == [143]
    assert "SIGTERM received; audit stopped, exiting 143" in caplog.text


def test_handle_stop_survives_callback_errors_and_is_not_reentrant(
    monkeypatch, caplog,
):
    exits: list[int] = []
    calls: list[str] = []
    monkeypatch.setattr(stop_signal, "_exit", exits.append)

    def broken() -> None:
        calls.append("broken")
        raise RuntimeError("dynamo down")

    stop_signal.register_on_stop(broken)
    stop_signal.register_on_stop(lambda: calls.append("ok"))
    with caplog.at_level("WARNING", logger="argus.stop_signal"):
        stop_signal.handle_stop()
        stop_signal.handle_stop()

    assert calls == ["ok", "broken"]
    assert exits == [143]
    assert "stop callback" in caplog.text and "RuntimeError: dynamo down" in caplog.text


def test_unregister_removes_only_that_callback(monkeypatch):
    exits: list[int] = []
    monkeypatch.setattr(stop_signal, "_exit", exits.append)
    calls: list[str] = []
    unregister = stop_signal.register_on_stop(lambda: calls.append("gone"))
    stop_signal.register_on_stop(lambda: calls.append("kept"))
    unregister()
    unregister()  # idempotent

    stop_signal.handle_stop()

    assert calls == ["kept"]


def test_install_registers_sigterm_once_and_only_from_main_thread(monkeypatch):
    installed: list[tuple[int, object]] = []
    monkeypatch.setattr(
        stop_signal.signal, "signal",
        lambda signum, handler: installed.append((signum, handler)),
    )

    assert stop_signal.install() is True
    assert stop_signal.install() is True
    assert installed == [(signal.SIGTERM, stop_signal.handle_stop)]

    monkeypatch.setattr(stop_signal, "_INSTALLED", False)
    results: list[bool] = []
    worker = threading.Thread(target=lambda: results.append(stop_signal.install()))
    worker.start()
    worker.join()
    assert results == [False]
    assert len(installed) == 1


def test_announce_audit_stopped_writes_owner_checked_status(monkeypatch):
    events: list[tuple] = []
    monkeypatch.setattr(
        "argus.reporting.publisher.stop_active_audit",
        lambda **kwargs: events.append(("stop", kwargs)) or True,
    )
    monkeypatch.setattr(
        "argus.operations.control.mark_audit_done",
        lambda token: events.append(("done", token)) or True,
    )

    orchestrator._announce_audit_stopped("run-7", "Sprint 47")

    assert events == [
        ("stop", {
            "run_id": "run-7",
            "diagnostic": "audit of Sprint 47 stopped by SIGTERM before completion",
        }),
        ("done", "run-7"),
    ]


def test_announce_audit_stopped_reports_but_survives_store_failures(
    monkeypatch, capsys,
):
    monkeypatch.setattr(
        "argus.reporting.publisher.stop_active_audit",
        mock.MagicMock(side_effect=RuntimeError("report storage unavailable")),
    )
    monkeypatch.setattr(
        "argus.operations.control.mark_audit_done",
        mock.MagicMock(side_effect=OSError("disk full")),
    )

    orchestrator._announce_audit_stopped("run-8", "QA-E1")

    err = capsys.readouterr().err
    assert "audit status 'stopped' not published: RuntimeError: report storage unavailable" in err
    assert "audit status not cleared: OSError: disk full" in err


def test_single_run_registers_stop_callback_for_its_run_id(monkeypatch, tmp_path):
    settings = argus_config.ArgusConfig()
    settings.extractor.out_dir = tmp_path
    monkeypatch.setenv("ARGUS_RUN_ID", "run-single-9")
    events: list[tuple] = []
    exits: list[int] = []
    monkeypatch.setattr(stop_signal, "_exit", exits.append)
    monkeypatch.setattr(
        "argus.operations.control.mark_audit_running",
        lambda *args, **kwargs: events.append(("mark", kwargs.get("phase"))),
    )
    monkeypatch.setattr(
        "argus.operations.control.mark_audit_done",
        lambda token: events.append(("done", token)) or True,
    )
    monkeypatch.setattr(
        "argus.reporting.publisher.announce_active_audit",
        lambda **kwargs: events.append(("announce", kwargs["phase"])),
    )
    monkeypatch.setattr(
        "argus.reporting.publisher.stop_active_audit",
        lambda **kwargs: events.append(("stopped", kwargs["run_id"])) or True,
    )
    monkeypatch.setattr(
        "argus.reporting.publisher.clear_active_audit",
        lambda **kwargs: events.append(("clear", kwargs["run_id"])) or True,
    )

    def fake_prepared(args, settings, *, run_id):
        # SIGTERM lands while the audit is in flight: the handler must find
        # this run's callback registered.
        stop_signal.handle_stop()
        return 0

    monkeypatch.setattr(orchestrator, "_run_single_prepared", fake_prepared)
    args = SimpleNamespace(execution_key="QA-E9", testrun_id=None, item_id=None)

    assert orchestrator._run_single(args, settings) == 0

    assert ("stopped", "run-single-9") in events
    assert exits == [143]
    # The stopped marker is written before the (simulated) exit; the normal
    # completion path afterwards is irrelevant because the process is gone.
    assert events.index(("stopped", "run-single-9")) < events.index(
        ("done", "run-single-9"))
    # The callback is unregistered once the run returns.
    assert stop_signal._CALLBACKS == []


def test_cycle_and_orchestrator_main_install_the_handler(monkeypatch, tmp_path):
    installs: list[str] = []
    monkeypatch.setattr(stop_signal, "install", lambda: installs.append("install") or True)
    from argus.pipeline import cycle

    settings = argus_config.ArgusConfig()
    settings.general.state_dir = tmp_path / "state"
    settings.extractor.out_dir = tmp_path / "output"
    monkeypatch.setattr(cycle.argus_config, "load", lambda *a, **k: settings)
    monkeypatch.setattr(cycle.runtime, "configure", lambda s: None)
    monkeypatch.setattr(cycle.extractor, "configure_logging", lambda v: None)
    monkeypatch.setattr(cycle, "run_cycle", lambda s, *, config_args: 0)
    assert cycle.main([]) == 0

    monkeypatch.setattr(orchestrator.argus_config, "load", lambda *a, **k: settings)
    monkeypatch.setattr(orchestrator.runtime, "configure", lambda s: None)
    monkeypatch.setattr(orchestrator.extractor, "configure_logging", lambda v: None)
    monkeypatch.setattr(orchestrator, "_run_single", lambda a, s: 0)
    assert orchestrator.main(["--key", "QA-E1"]) == 0

    assert installs == ["install", "install"]


def test_manual_batch_installs_the_handler(monkeypatch):
    installs: list[str] = []
    monkeypatch.setattr(stop_signal, "install", lambda: installs.append("install") or True)
    from argus.operations import manual_batch

    settings = argus_config.ArgusConfig()
    monkeypatch.setattr(manual_batch.config, "load", lambda *a, **k: settings)
    monkeypatch.setattr(manual_batch.runtime, "configure", lambda s: None)
    monkeypatch.setattr(manual_batch.extractor, "configure_logging", lambda v: None)
    monkeypatch.setattr(manual_batch.orchestrator, "_run_folder_list", lambda *a, **k: 0)

    assert manual_batch.main(["--folder", "Sprint 47"]) == 0
    assert installs == ["install"]
