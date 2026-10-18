"""Curated Activity and Alerts event stream tests."""
from __future__ import annotations

import json

from argus import config as argus_config
from argus.operations import events


def _settings(tmp_path):
    settings = argus_config.ArgusConfig()
    settings.general.state_dir = tmp_path
    return settings


def _records(tmp_path):
    return [
        json.loads(line)
        for line in (tmp_path / "events.log").read_text().splitlines()
    ]


def test_notification_events_route_to_activity_and_alerts(tmp_path):
    settings = _settings(tmp_path)

    events.emit_notification(
        "info",
        "audit started",
        "folder: Targeted regression\nkeys: 4\nmode: auto-folder",
        settings=settings,
    )
    events.emit_notification(
        "info",
        "audit done",
        "folder: Targeted regression\n"
        "result: 3 ok, 1 flagged, 0 failed\nelapsed: 2m 14s",
        settings=settings,
    )
    events.emit_notification(
        "warn",
        "1 new deviation in Targeted regression",
        "- QA-E1 [fail] — Tester — Run (C-1) — "
        "marked Pass; missing execution evidence",
        settings=settings,
    )

    records = _records(tmp_path)
    assert [record["label"] for record in records] == [
        "AUDIT", "COMPLETE", "FLAGGED",
    ]
    assert records[0]["message"] == (
        'Started 4 keys in "Targeted regression"'
    )
    assert records[1]["message"] == (
        "3 passed, 1 flagged, 0 failed | 2m 14s"
    )
    assert records[2]["message"] == (
        "QA-E1 | missing execution evidence"
    )


def test_idle_info_notifications_do_not_enter_curated_stream(tmp_path):
    settings = _settings(tmp_path)

    events.emit_notification(
        "info",
        "daily summary",
        "folder runs: 0",
        settings=settings,
    )

    assert not (tmp_path / "events.log").exists()


def test_progress_is_an_activity_event(tmp_path):
    settings = _settings(tmp_path)

    events.emit(
        "PROGRESS", "20/45 (44%) | 19 passed, 1 skipped",
        settings=settings,
    )

    record = _records(tmp_path)[0]
    assert record["label"] == "PROGRESS"
    assert events._matches(record, "activity")
    assert not events._matches(record, "alerts")


def test_extract_is_an_activity_event(tmp_path):
    settings = _settings(tmp_path)

    events.emit(
        "EXTRACT", "1/4 (25%) | QA-E1 | PDF 12 pages | 3.2s",
        settings=settings,
    )

    record = _records(tmp_path)[0]
    assert record["label"] == "EXTRACT"
    assert events._matches(record, "activity")
    assert not events._matches(record, "alerts")


def test_cost_notifications_receive_distinct_alert_labels(tmp_path):
    settings = _settings(tmp_path)

    events.emit_notification(
        "warn", "cost budget warning",
        "Model API spend is 82.0% | $82.0000 of $100.0000",
        settings=settings,
    )
    events.emit_notification(
        "error", "cost budget exceeded",
        "Model API spend is 101.0% | $101.0000 of $100.0000",
        settings=settings,
    )
    events.emit_notification(
        "info", "cost explorer recovered", "available",
        settings=settings,
    )

    records = _records(tmp_path)
    assert [record["label"] for record in records] == [
        "COST WARNING", "COST CRITICAL", "RECOVERED",
    ]


def test_failed_completion_also_routes_to_alerts(tmp_path):
    settings = _settings(tmp_path)

    events.emit_notification(
        "error",
        "audit done with failures",
        "folder: F\nresult: 2 ok, 0 flagged, 1 failed\nelapsed: 30s",
        settings=settings,
    )

    records = _records(tmp_path)
    assert [record["label"] for record in records] == ["COMPLETE", "ERROR"]


def test_event_log_rotates_at_size_limit(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(events, "_MAX_LOG_BYTES", 1)

    events.emit("WARNING", "first", settings=settings)
    events.emit("ERROR", "second", settings=settings)

    assert (tmp_path / "events.log.1").exists()
    assert json.loads((tmp_path / "events.log").read_text())["message"] == "second"
