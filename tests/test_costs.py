"""Model API usage pricing and read-only Cost Explorer monitoring tests."""
from __future__ import annotations

import datetime as dt
import json
import os
from decimal import Decimal
from pathlib import Path

import pytest

from argus.core import costs as cost_core
from argus.operations import costs


def _rates():
    return {"test-model": {"input_per_million": 2.0, "output_per_million": 10.0,
                           "cache_write_per_million": 2.5, "cache_read_per_million": 0.2}}


def test_unknown_pricing_keeps_usage_unpriced():
    assert cost_core.default_model_rates() == {}
    result = cost_core.summarise_usage(
        [{"model_id": "custom-model", "operation": "audit", "calls": 1, "input_tokens": 123}], {})
    assert result["unpriced_calls"] == 1
    assert result["usage"][0]["estimated_cost_usd"] is None


def test_usage_tracker_prices_cache_and_standard_tokens():
    tracker = cost_core.UsageTracker()
    tracker.record_response({
        "usage": {
            "input_tokens": 10_000,
            "output_tokens": 1_000,
            "cache_creation_input_tokens": 2_000,
            "cache_read_input_tokens": 20_000,
        }
    }, model_id="test-model", operation="audit")

    summary = tracker.summary(_rates())

    assert summary is not None
    assert summary["calls"] == 1
    assert summary["estimated_cost_usd"] == pytest.approx(0.039)
    assert summary["usage"][0]["cache_read_input_tokens"] == 20_000


def test_usage_tracker_aggregates_threads_and_marks_unknown_models():
    tracker = cost_core.UsageTracker()
    for _ in range(2):
        tracker.record_response(
            {"usage": {"input_tokens": 100, "output_tokens": 10}},
            model_id="unknown.model",
            operation="audit",
        )

    summary = tracker.summary(_rates())

    assert summary is not None
    assert summary["calls"] == 2
    assert summary["unpriced_calls"] == 2
    assert summary["estimated_cost_usd"] == 0


def test_unavailable_video_usage_stays_unpriced_after_merge_and_repricing():
    tracker = cost_core.UsageTracker()
    model = "test-video-model"
    tracker.record_unpriced_call(model_id=model, operation="video_observations")
    rates = {model: {field: 1.0 for field in cost_core.RATE_FIELDS}}
    summary = tracker.summary(rates)
    merged = cost_core.merge_summaries(summary, summary, rates)
    repriced = cost_core.reprice_summary(merged, rates)
    assert repriced["calls"] == 2
    assert repriced["unpriced_calls"] == 2
    assert repriced["usage"][0]["estimated_cost_usd"] is None
    assert repriced["usage"][0]["usage_unavailable"] is True


@pytest.mark.parametrize("unpriced_first", [False, True])
def test_missing_chunk_usage_does_not_discard_other_chunks_known_cost(unpriced_first):
    tracker = cost_core.UsageTracker()
    model = "test-audio-model"
    recorders = [
        lambda: tracker.record_response(
            {"usage": {"input_tokens": 100, "output_tokens": 20}},
            model_id=model, operation="audio_observations",
        ),
        lambda: tracker.record_unpriced_call(model_id=model, operation="audio_observations"),
    ]
    for record in reversed(recorders) if unpriced_first else recorders:
        record()
    rates = {model: {field: 1.0 for field in cost_core.RATE_FIELDS}}
    summary = tracker.summary(rates)
    assert summary["calls"] == 2
    assert summary["unpriced_calls"] == 1
    assert summary["estimated_cost_usd"] == pytest.approx(0.00012)
    assert sum(row["input_tokens"] for row in summary["usage"]) == 100
    merged = cost_core.merge_summaries(summary, summary, rates)
    repriced = cost_core.reprice_summary(
        merged, {model: {field: 2.0 for field in cost_core.RATE_FIELDS}},
    )
    assert repriced["calls"] == 4
    assert repriced["unpriced_calls"] == 2
    assert repriced["estimated_cost_usd"] == pytest.approx(0.00048)


def _write_audit(
    root: Path,
    name: str,
    *,
    audited_at: str,
    cost: dict | None,
) -> None:
    path = root / name / "audit.json"
    path.parent.mkdir(parents=True)
    payload = {"audited_at": audited_at}
    if cost is not None:
        payload["cost"] = cost
    path.write_text(json.dumps(payload))


def test_scan_local_costs_counts_tracked_and_legacy_audits(tmp_path: Path):
    tracker = cost_core.UsageTracker()
    tracker.record_response(
        {"usage": {"input_tokens": 1_000, "output_tokens": 100}},
        model_id="test-model",
        operation="audit",
    )
    summary = tracker.summary(_rates())
    assert summary is not None
    _write_audit(
        tmp_path, "tracked",
        audited_at="2026-07-10T00:00:00Z",
        cost=summary,
    )
    _write_audit(
        tmp_path, "legacy",
        audited_at="2026-07-11T00:00:00Z",
        cost=None,
    )
    _write_audit(
        tmp_path, "old",
        audited_at="2026-06-30T00:00:00Z",
        cost=summary,
    )

    snapshot = costs.scan_local_costs(
        tmp_path,
        model_rates=_rates(),
        now=dt.datetime(2026, 7, 14, tzinfo=dt.timezone.utc),
    )

    assert snapshot.audits == 2
    assert snapshot.tracked_audits == 1
    assert snapshot.untracked_audits == 1
    assert snapshot.tracked_calls == 1
    assert snapshot.estimated_cost_usd == Decimal("0.003")
    assert snapshot.average_cost_usd == Decimal("0.003")
    assert len(snapshot.recent_audits) == 1
    assert snapshot.recent_audits[0].key == "tracked"
    assert snapshot.recent_audits[0].estimated_cost_usd == Decimal("0.003")


def test_scan_local_costs_tracks_utc_today_separately(tmp_path: Path):
    tracker = cost_core.UsageTracker()
    tracker.record_response(
        {"usage": {"input_tokens": 1_000, "output_tokens": 100}},
        model_id="test-model",
        operation="audit",
    )
    summary = tracker.summary(_rates())
    assert summary is not None
    _write_audit(
        tmp_path, "today-tracked",
        audited_at="2026-07-14T01:00:00Z",
        cost=summary,
    )
    _write_audit(
        tmp_path, "today-legacy",
        audited_at="2026-07-14T02:00:00Z",
        cost=None,
    )
    _write_audit(
        tmp_path, "yesterday",
        audited_at="2026-07-13T23:59:59Z",
        cost=summary,
    )

    snapshot = costs.scan_local_costs(
        tmp_path,
        model_rates=_rates(),
        now=dt.datetime(2026, 7, 14, 12, tzinfo=dt.timezone.utc),
    )

    assert snapshot.today_audits == 2
    assert snapshot.today_tracked_audits == 1
    assert snapshot.today_calls == 1
    assert snapshot.today_unpriced_calls == 0
    assert snapshot.today_cost_usd == Decimal("0.003")


def test_record_run_cost_uses_exact_keys_and_excludes_stale_audits(
        tmp_path: Path):
    tracker = cost_core.UsageTracker()
    tracker.record_response(
        {"usage": {"input_tokens": 1_000, "output_tokens": 100}},
        model_id="test-model",
        operation="audit",
    )
    summary = tracker.summary(_rates())
    assert summary is not None
    _write_audit(
        tmp_path, "current",
        audited_at="2026-07-14T12:01:00Z",
        cost=summary,
    )
    _write_audit(
        tmp_path, "stale",
        audited_at="2026-07-14T11:00:00Z",
        cost=summary,
    )
    _write_audit(
        tmp_path, "not-queued",
        audited_at="2026-07-14T12:02:00Z",
        cost=summary,
    )

    snapshot = costs.record_run_cost(
        tmp_path,
        label="Folder A",
        kind="folder",
        keys=["current", "stale"],
        started_at=dt.datetime(
            2026, 7, 14, 12, tzinfo=dt.timezone.utc),
        completed_at=dt.datetime(
            2026, 7, 14, 12, 5, tzinfo=dt.timezone.utc),
        model_rates=_rates(),
    )

    assert snapshot.queued_audits == 2
    assert snapshot.recorded_audits == 1
    assert snapshot.model_audits == 1
    assert snapshot.calculated_cost_usd == Decimal("0.003")
    persisted = costs.scan_latest_run_cost(
        tmp_path,
        model_rates=_rates(),
    )
    assert persisted == snapshot


def test_latest_report_run_excludes_audits_from_previous_report(
        tmp_path: Path):
    folder = tmp_path / "folder"
    tracker = cost_core.UsageTracker()
    tracker.record_response(
        {"usage": {"input_tokens": 1_000, "output_tokens": 100}},
        model_id="test-model",
        operation="audit",
    )
    summary = tracker.summary(_rates())
    assert summary is not None
    _write_audit(
        folder, "old",
        audited_at="2026-07-14T10:00:00Z",
        cost=summary,
    )
    _write_audit(
        folder, "current",
        audited_at="2026-07-14T12:00:00Z",
        cost=summary,
    )
    previous = folder / "_report_2026-07-14_110000.md"
    latest = folder / "_report_2026-07-14_130000.md"
    previous.write_text("previous")
    latest.write_text("latest")
    os.utime(previous, (1784026800, 1784026800))
    os.utime(latest, (1784034000, 1784034000))

    snapshot = costs.scan_latest_run_cost(
        tmp_path,
        model_rates=_rates(),
    )

    assert snapshot is not None
    assert snapshot.label == "folder"
    assert snapshot.recorded_audits == 1
    assert snapshot.calculated_cost_usd == Decimal("0.003")


def _alert_settings(tmp_path: Path):
    from argus import config as argus_config

    settings = argus_config.ArgusConfig()
    settings.general.state_dir = tmp_path
    settings.costs.monthly_budget_usd = 100.0
    settings.costs.warning_percent = 80.0
    settings.costs.critical_percent = 100.0
    settings.costs.refresh_seconds = 300
    return settings




def test_local_budget_alerts_are_deduplicated_and_reset_each_month(tmp_path, monkeypatch):
    from dataclasses import replace

    settings = _alert_settings(tmp_path)
    snapshot = replace(costs.scan_local_costs(tmp_path, model_rates={}), estimated_cost_usd=Decimal("110"))
    posts = []
    monkeypatch.setattr(costs.notify, "post", lambda level, title, body, **kwargs: posts.append((level, title)))
    now = dt.datetime(2026, 7, 14, tzinfo=dt.timezone.utc)
    costs.evaluate_alerts(settings, snapshot, now=now)
    costs.evaluate_alerts(settings, snapshot, now=now)
    assert len(posts) == 2
    costs.evaluate_alerts(settings, snapshot, now=now.replace(month=8))
    assert len(posts) == 4


def test_local_render_identifies_estimates_and_unpriced_calls(tmp_path):
    text = costs.render(costs.scan_local_costs(tmp_path, model_rates={}), monthly_budget_usd=100)
    assert "ARGUS MODEL COST ESTIMATES" in text
    assert "Unpriced calls:" in text
    assert "not invoices" in text
