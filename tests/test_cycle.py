"""`argus cycle` — the in-process equivalent of cron_wrapper.sh's sequence."""
from __future__ import annotations

from unittest import mock

import pytest

from argus import config as argus_config
from argus.pipeline import cycle


def _settings(tmp_path):
    cfg = argus_config.ArgusConfig()
    cfg.general.state_dir = tmp_path / "state"
    cfg.extractor.out_dir = tmp_path / "output"
    return cfg


def _stub_all(monkeypatch, *, heal_rc=0, audit_rc=0, diff_rc=0,
              costs_rc=0, prune_rc=0):
    calls: dict[str, list] = {
        "heal": [], "audit": [], "diff": [], "costs": [], "prune": [],
    }

    def _record(name, rc):
        def _fn(argv):
            calls[name].append(argv)
            return rc
        return _fn

    monkeypatch.setattr(cycle.control, "is_paused", lambda: False)
    monkeypatch.setattr(cycle.r12_heal, "main", _record("heal", heal_rc))
    monkeypatch.setattr(cycle.orchestrator, "main", _record("audit", audit_rc))
    monkeypatch.setattr(cycle.diff_and_notify, "main", _record("diff", diff_rc))
    monkeypatch.setattr(cycle.costs, "main", _record("costs", costs_rc))
    monkeypatch.setattr(cycle.prune, "main", _record("prune", prune_rc))
    return calls


def test_paused_short_circuits_before_any_step(tmp_path, monkeypatch):
    monkeypatch.setattr(cycle.control, "is_paused", lambda: True)
    heal = mock.MagicMock()
    monkeypatch.setattr(cycle.r12_heal, "main", heal)

    rc = cycle.run_cycle(_settings(tmp_path), config_args=[])

    assert rc == 0
    heal.assert_not_called()


def test_heal_failure_is_non_fatal(tmp_path, monkeypatch):
    calls = _stub_all(monkeypatch, heal_rc=1)

    rc = cycle.run_cycle(_settings(tmp_path), config_args=[])

    assert rc == 0
    assert calls["heal"] == [["--scope", "active"]]
    assert calls["audit"]  # pipeline still ran


def test_heal_exception_is_non_fatal(tmp_path, monkeypatch):
    calls = _stub_all(monkeypatch)
    monkeypatch.setattr(
        cycle.r12_heal, "main", mock.MagicMock(side_effect=RuntimeError("boom")))

    rc = cycle.run_cycle(_settings(tmp_path), config_args=[])

    assert rc == 0
    assert calls["audit"]


def test_auth_failure_propagates_rc_2(tmp_path, monkeypatch):
    _stub_all(monkeypatch, audit_rc=2)

    rc = cycle.run_cycle(_settings(tmp_path), config_args=[])

    assert rc == 2


def test_audit_failure_propagates_rc_1(tmp_path, monkeypatch):
    _stub_all(monkeypatch, audit_rc=1)

    rc = cycle.run_cycle(_settings(tmp_path), config_args=[])

    assert rc == 1


def test_clean_run_chains_all_five_steps(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.general.state_dir.mkdir(parents=True)
    scoped = settings.extractor.out_dir / "Sprint_47"
    scoped.mkdir(parents=True)
    (settings.general.state_dir / "last_folder_out_dir").write_text(
        str(scoped) + "\n")
    calls = _stub_all(monkeypatch)

    rc = cycle.run_cycle(settings, config_args=["--config", "/tmp/c.toml"])

    assert rc == 0
    assert calls["heal"] == [["--scope", "active", "--config", "/tmp/c.toml"]]
    assert calls["audit"] == [["--auto-folder", "--config", "/tmp/c.toml"]]
    assert calls["diff"] == [
        ["--out-dir", str(scoped), "--config", "/tmp/c.toml"]]
    assert calls["costs"] == [["--config", "/tmp/c.toml"]]
    assert calls["prune"] == [["--apply", "--if-enabled"]]


def test_diff_falls_back_to_newest_output_subdir_when_state_file_missing(
        tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.extractor.out_dir.mkdir(parents=True)
    older = settings.extractor.out_dir / "older"
    older.mkdir()
    newer = settings.extractor.out_dir / "newer"
    newer.mkdir()
    import os
    import time
    now = time.time()
    os.utime(older, (now - 100, now - 100))
    os.utime(newer, (now, now))
    calls = _stub_all(monkeypatch)

    cycle.run_cycle(settings, config_args=[])

    assert calls["diff"] == [["--out-dir", str(newer)]]


def test_diff_skipped_when_no_output_dir_exists(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    calls = _stub_all(monkeypatch)

    rc = cycle.run_cycle(settings, config_args=[])

    assert rc == 0
    assert calls["diff"] == []


def test_diff_failure_is_non_fatal(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.extractor.out_dir.mkdir(parents=True)
    (settings.extractor.out_dir / "Sprint_1").mkdir()
    calls = _stub_all(monkeypatch, diff_rc=1)

    rc = cycle.run_cycle(settings, config_args=[])

    assert rc == 0
    assert calls["diff"]


def test_costs_and_prune_run_when_stamps_are_stale(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    calls = _stub_all(monkeypatch)

    cycle.run_cycle(settings, config_args=[])

    assert calls["costs"] == [[]]
    assert calls["prune"] == [["--apply", "--if-enabled"]]
    assert (settings.general.state_dir / "last_cost_check_day").exists()
    assert (settings.general.state_dir / "last_prune_day").exists()


def test_costs_and_prune_skipped_when_stamp_is_today(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.general.state_dir.mkdir(parents=True)
    today = cycle._today_utc()
    (settings.general.state_dir / "last_cost_check_day").write_text(today + "\n")
    (settings.general.state_dir / "last_prune_day").write_text(today + "\n")
    calls = _stub_all(monkeypatch)

    cycle.run_cycle(settings, config_args=[])

    assert calls["costs"] == []
    assert calls["prune"] == []


def test_costs_failure_leaves_stamp_unwritten_for_retry(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _stub_all(monkeypatch, costs_rc=1)

    cycle.run_cycle(settings, config_args=[])

    assert not (settings.general.state_dir / "last_cost_check_day").exists()


def test_prune_failure_leaves_stamp_unwritten_for_retry(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _stub_all(monkeypatch, prune_rc=1)

    cycle.run_cycle(settings, config_args=[])

    assert not (settings.general.state_dir / "last_prune_day").exists()


def test_cli_help_does_not_require_network_or_aws(capsys):
    with pytest.raises(SystemExit) as exc:
        cycle.main(["--help"])
    assert exc.value.code == 0
    assert "unattended" in capsys.readouterr().out.lower()
