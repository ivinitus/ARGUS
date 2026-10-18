"""Unit tests for backfill_users.py — heal pre-existing JIRAUSER corruption."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from argus.pipeline import backfill_users
from argus.core import users


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(users, "_CACHE_PATH", tmp_path / "users.json")
    users.reset_default_cache()


def _make_metadata(executed_by="JIRAUSER1", executed_by_name=None,
                   assigned_to="JIRAUSER1", assigned_to_name=None,
                   **extra) -> dict:
    return {
        "test_results": [{
            "executed_by": executed_by,
            "executed_by_name": executed_by_name,
            "assigned_to": assigned_to,
            "assigned_to_name": assigned_to_name,
            **extra,
        }]
    }


def _write_metadata(path: Path, metadata: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata, indent=2))


def test_scan_unresolved_finds_all_jirauser_keys_with_none_name():
    metadata = _make_metadata(
        executed_by="JIRAUSER1", executed_by_name=None,
        assigned_to="JIRAUSER2", assigned_to_name=None,
    )
    keys = backfill_users._scan_unresolved(metadata)
    assert keys == {"JIRAUSER1", "JIRAUSER2"}


def test_scan_unresolved_skips_already_resolved_pairs():
    metadata = _make_metadata(
        executed_by="JIRAUSER1", executed_by_name="Alice",
        assigned_to="JIRAUSER2", assigned_to_name=None,
    )
    keys = backfill_users._scan_unresolved(metadata)
    assert keys == {"JIRAUSER2"}


def test_scan_unresolved_skips_non_jirauser_keys():
    metadata = {
        "test_results": [{
            "executed_by": "alice",
            "executed_by_name": None,
            "assigned_to": "alice",
            "assigned_to_name": None,
        }]
    }
    assert backfill_users._scan_unresolved(metadata) == set()


def test_apply_resolutions_updates_in_place_and_reports_change():
    metadata = _make_metadata(
        executed_by="JIRAUSER1", executed_by_name=None,
        assigned_to="JIRAUSER1", assigned_to_name=None,
    )
    changed = backfill_users._apply_resolutions(
        metadata, {"JIRAUSER1": "Alice"})
    assert changed is True
    tr = metadata["test_results"][0]
    assert tr["executed_by_name"] == "Alice"
    assert tr["assigned_to_name"] == "Alice"
    assert tr["executed_by"] == "JIRAUSER1"
    assert tr["assigned_to"] == "JIRAUSER1"


def test_apply_resolutions_returns_false_when_no_change_needed():
    """Both fields already resolved → no work to do, returns False."""
    metadata = _make_metadata(
        executed_by="JIRAUSER1", executed_by_name="Alice",
        assigned_to="JIRAUSER1", assigned_to_name="Alice",
    )
    changed = backfill_users._apply_resolutions(
        metadata, {"JIRAUSER1": "Alice"})
    assert changed is False


def test_apply_resolutions_skips_keys_not_in_resolutions_dict():
    metadata = _make_metadata(
        executed_by="JIRAUSER1", executed_by_name=None,
        assigned_to="JIRAUSER2", assigned_to_name=None,
    )
    backfill_users._apply_resolutions(metadata, {"JIRAUSER1": "Alice"})
    tr = metadata["test_results"][0]
    assert tr["executed_by_name"] == "Alice"
    assert tr["assigned_to_name"] is None


def test_atomic_write_round_trips(tmp_path):
    path = tmp_path / "metadata.json"
    payload = {"key": "value", "nested": {"x": 1}}
    backfill_users._atomic_write_json(path, payload)
    assert json.loads(path.read_text()) == payload


def test_backfill_idempotent_no_changes_on_second_pass(tmp_path, monkeypatch):
    """First run heals the file; second run finds nothing to fix."""
    out_dir = tmp_path / "output"
    meta_path = out_dir / "Folder" / "QA-E1" / "metadata.json"
    _write_metadata(meta_path, _make_metadata(
        executed_by="JIRAUSER1", executed_by_name=None,
        assigned_to="JIRAUSER1", assigned_to_name=None,
    ))

    cache = users.get_default_cache()
    cache._record_success("JIRAUSER1", "Alice")
    cache.save()

    monkeypatch.setattr(
        backfill_users.extractor, "build_session",
        lambda: object())

    counters = backfill_users.backfill(out_dir)
    assert counters["files_updated"] == 1
    assert counters["files_with_unresolved"] == 1
    healed = json.loads(meta_path.read_text())
    assert healed["test_results"][0]["executed_by_name"] == "Alice"

    counters2 = backfill_users.backfill(out_dir)
    assert counters2["files_updated"] == 0
    assert counters2["files_with_unresolved"] == 0


def test_backfill_dry_run_does_not_write(tmp_path, monkeypatch):
    out_dir = tmp_path / "output"
    meta_path = out_dir / "F" / "E1" / "metadata.json"
    original = _make_metadata(executed_by_name=None, assigned_to_name=None)
    _write_metadata(meta_path, original)
    raw_before = meta_path.read_text()

    cache = users.get_default_cache()
    cache._record_success("JIRAUSER1", "Alice")
    cache.save()
    monkeypatch.setattr(
        backfill_users.extractor, "build_session",
        lambda: object())

    counters = backfill_users.backfill(out_dir, dry_run=True)
    assert counters["files_updated"] == 1
    assert meta_path.read_text() == raw_before


def test_backfill_skips_files_with_no_test_results(tmp_path, monkeypatch):
    out_dir = tmp_path / "output"
    bad = out_dir / "F" / "E1" / "metadata.json"
    bad.parent.mkdir(parents=True)
    bad.write_text(json.dumps({"test_results": []}))
    good = out_dir / "F" / "E2" / "metadata.json"
    _write_metadata(good, _make_metadata(executed_by_name=None,
                                          assigned_to_name=None))

    cache = users.get_default_cache()
    cache._record_success("JIRAUSER1", "Alice")
    cache.save()
    monkeypatch.setattr(
        backfill_users.extractor, "build_session",
        lambda: object())

    counters = backfill_users.backfill(out_dir)
    assert counters["files_scanned"] == 2
    assert counters["files_with_unresolved"] == 1
