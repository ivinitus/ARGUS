"""Unit tests for backfill_users.py — heal pre-existing USER corruption."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import backfill_users
import users


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(users, "_CACHE_PATH", tmp_path / "users.json")
    users.reset_default_cache()


def _make_metadata(executed_by="USER1", executed_by_name=None,
                   assigned_to="USER1", assigned_to_name=None,
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


def test_scan_unresolved_finds_all_trackeruser_keys_with_none_name():
    metadata = _make_metadata(
        executed_by="USER1", executed_by_name=None,
        assigned_to="USER2", assigned_to_name=None,
    )
    keys = backfill_users._scan_unresolved(metadata)
    assert keys == {"USER1", "USER2"}


def test_scan_unresolved_skips_already_resolved_pairs():
    metadata = _make_metadata(
        executed_by="USER1", executed_by_name="Alice",
        assigned_to="USER2", assigned_to_name=None,
    )
    keys = backfill_users._scan_unresolved(metadata)
    assert keys == {"USER2"}


def test_scan_unresolved_skips_non_trackeruser_keys():
    """A real username (e.g. 'alice') in executed_by isn't a backfill target —
    pre-2026 metadata sometimes has resolved usernames, not USER ids."""
    # Use bare dict to avoid _make_metadata's USER1 defaults polluting.
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
        executed_by="USER1", executed_by_name=None,
        assigned_to="USER1", assigned_to_name=None,
    )
    changed = backfill_users._apply_resolutions(
        metadata, {"USER1": "Alice"})
    assert changed is True
    tr = metadata["test_results"][0]
    assert tr["executed_by_name"] == "Alice"
    assert tr["assigned_to_name"] == "Alice"
    # Raw keys preserved:
    assert tr["executed_by"] == "USER1"
    assert tr["assigned_to"] == "USER1"


def test_apply_resolutions_returns_false_when_no_change_needed():
    """Both fields already resolved → no work to do, returns False."""
    metadata = _make_metadata(
        executed_by="USER1", executed_by_name="Alice",
        assigned_to="USER1", assigned_to_name="Alice",
    )
    changed = backfill_users._apply_resolutions(
        metadata, {"USER1": "Alice"})
    assert changed is False


def test_apply_resolutions_skips_keys_not_in_resolutions_dict():
    metadata = _make_metadata(
        executed_by="USER1", executed_by_name=None,
        assigned_to="USER2", assigned_to_name=None,
    )
    # Only resolved USER1; USER2's *_name should stay None.
    backfill_users._apply_resolutions(metadata, {"USER1": "Alice"})
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
        executed_by="USER1", executed_by_name=None,
        assigned_to="USER1", assigned_to_name=None,
    ))

    # Pre-populate the cache so the backfill doesn't make Tracker calls.
    cache = users.get_default_cache()
    cache._record_success("USER1", "Alice")
    cache.save()

    # Patch the network bits so any accidental Tracker call would fail loudly.
    monkeypatch.setattr(
        backfill_users.extractor, "build_session",
        lambda: object())

    counters = backfill_users.backfill(out_dir)
    assert counters["files_updated"] == 1
    assert counters["files_with_unresolved"] == 1
    # File was actually rewritten with the resolved name:
    healed = json.loads(meta_path.read_text())
    assert healed["test_results"][0]["executed_by_name"] == "Alice"

    # Second pass: nothing to do.
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
    cache._record_success("USER1", "Alice")
    cache.save()
    monkeypatch.setattr(
        backfill_users.extractor, "build_session",
        lambda: object())

    counters = backfill_users.backfill(out_dir, dry_run=True)
    assert counters["files_updated"] == 1   # would have updated
    # ...but the file on disk is unchanged.
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
    cache._record_success("USER1", "Alice")
    cache.save()
    monkeypatch.setattr(
        backfill_users.extractor, "build_session",
        lambda: object())

    counters = backfill_users.backfill(out_dir)
    assert counters["files_scanned"] == 2
    assert counters["files_with_unresolved"] == 1  # only the good file
