from __future__ import annotations

import os
import time

from argus import config as argus_config
from argus.operations import monitor as monitor_argus


def _settings():
    settings = argus_config.ArgusConfig()
    settings.notify.mode = "verbose"
    return settings


def _check(ok: bool) -> monitor_argus.CheckResult:
    return monitor_argus.CheckResult(
        name="local_argus",
        ok=ok,
        level="error",
        fail_title="argus local health failed",
        recover_title="argus local health recovered",
        detail="local detail",
    )


def test_failure_posts_once_and_state_suppresses_repeats(
        tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(monitor_argus.notify, "post",
                        lambda level, title, body, *, settings: calls.append(
                            (level, title, body)))
    state_path = tmp_path / "monitor_state.json"
    settings = _settings()

    monitor_argus.post_state_changes([_check(False)], settings=settings,
                                     state_path=state_path)
    monitor_argus.post_state_changes([_check(False)], settings=settings,
                                     state_path=state_path)

    assert calls == [("error", "argus local health failed", "local detail")]


def test_recovery_posts_after_prior_failure(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(monitor_argus.notify, "post",
                        lambda level, title, body, *, settings: calls.append(
                            (level, title, body)))
    state_path = tmp_path / "monitor_state.json"
    settings = _settings()

    monitor_argus.post_state_changes([_check(False)], settings=settings,
                                     state_path=state_path)
    monitor_argus.post_state_changes([_check(True)], settings=settings,
                                     state_path=state_path)

    assert calls == [
        ("error", "argus local health failed", "local detail"),
        ("info", "argus local health recovered", "local detail"),
    ]


def test_healthy_first_run_does_not_post(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(monitor_argus.notify, "post",
                        lambda level, title, body, *, settings: calls.append(
                            (level, title, body)))

    monitor_argus.post_state_changes([_check(True)], settings=_settings(),
                                     state_path=tmp_path / "state.json")

    assert calls == []


def test_collect_checks_includes_tunnel_when_configured(tmp_path, monkeypatch):
    cron_log = tmp_path / "cron.log"
    cron_log.write_text("fresh\n")
    settings = _settings()
    settings.notify.report_url = "https://argus.example.test/base/"

    def fake_health(url, timeout_seconds):
        if url.startswith("http://127.0.0.1:2405"):
            return True, "local ok"
        if url == "https://argus.example.test/base/healthz":
            return False, "tunnel failed"
        raise AssertionError(url)

    monkeypatch.setattr(monitor_argus, "_health_url_ok", fake_health)

    checks = monitor_argus.collect_checks(settings, cron_log=cron_log)

    by_name = {c.name: c for c in checks}
    assert by_name["local_argus"].ok is True
    assert by_name["cron_freshness"].ok is True
    assert by_name["report_argus"].ok is False
    assert by_name["report_argus"].level == "warn"
    assert by_name["report_argus"].fail_title == "argus report health failed"


def test_cron_freshness_flags_stale_log(tmp_path):
    cron_log = tmp_path / "cron.log"
    cron_log.write_text("old\n")
    now = time.time()
    os.utime(cron_log, (now - 3600, now - 3600))

    ok, detail = monitor_argus._cron_freshness_ok(
        cron_log, max_age_seconds=45 * 60, now=now)

    assert ok is False
    assert "stale" in detail


def test_cron_freshness_accepts_fresh_log(tmp_path):
    cron_log = tmp_path / "cron.log"
    cron_log.write_text("fresh\n")
    now = time.time()
    os.utime(cron_log, (now - 60, now - 60))

    ok, detail = monitor_argus._cron_freshness_ok(
        cron_log, max_age_seconds=45 * 60, now=now)

    assert ok is True
    assert "fresh" in detail
