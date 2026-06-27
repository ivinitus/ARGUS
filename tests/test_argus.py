"""Tests for argus.py — pure helpers only (no network, no subprocess).

Network-dependent code paths in argus.py (build_session, folder
resolution, batch fan-out) are exercised via the extractor and batch
test suites, so this file only covers the filesystem-path helpers that
exist at the orchestration layer.
"""
from __future__ import annotations

import json
from pathlib import Path

import argus


def test_scoped_folder_out_dir_basic():
    base = Path("/tmp/out")
    scoped = argus._scoped_folder_out_dir(base, "Sprint 47 Regression")
    assert scoped == Path("/tmp/out/Sprint_47_Regression")


def test_scoped_folder_out_dir_strips_dangerous_chars():
    # Extractor's slugify collapses anything non-[\w.-] to underscore.
    # Tests for slug rules live in test_extractor_units; here we just
    # confirm the helper delegates to it correctly.
    base = Path("/tmp/out")
    scoped = argus._scoped_folder_out_dir(base, "Folder / with \\ bad: chars")
    # No slashes, colons, or spaces in the final path component.
    assert "/" not in scoped.name
    assert ":" not in scoped.name
    assert " " not in scoped.name
    # Still under the base dir as a single subdirectory.
    assert scoped.parent == base


def test_scoped_folder_out_dir_handles_empty_and_weird_names():
    base = Path("/tmp/out")
    # Empty name -> slug falls back to "unnamed" so we always have a
    # valid directory to write into.
    assert argus._scoped_folder_out_dir(base, "") == base / "unnamed"
    # Pure-punctuation name collapses to "unnamed" rather than an
    # empty segment.
    assert argus._scoped_folder_out_dir(base, "!!! ??? ///") == base / "unnamed"


def test_scoped_folder_out_dir_is_idempotent_on_already_slugged_input():
    # If a caller passes an already-slugged name (e.g. from a previous
    # run's path), the helper shouldn't double-mangle it.
    base = Path("/tmp/out")
    scoped = argus._scoped_folder_out_dir(base, "Sprint_47_Regression")
    assert scoped == Path("/tmp/out/Sprint_47_Regression")


def test_scoped_folder_out_dir_preserves_nested_base_paths(tmp_path: Path):
    # Users may configure `out_dir = ./data/qa-output` or similar. The
    # helper appends the folder slug to whatever the base is without
    # flattening intermediate components.
    base = tmp_path / "data" / "qa-output"
    scoped = argus._scoped_folder_out_dir(base, "E2E Flow")
    assert scoped == tmp_path / "data" / "qa-output" / "E2E_Flow"


# ---------------------------------------------------------------------------
# _audited_keys_with_timestamps — Layer 1 re-audit detection
# ---------------------------------------------------------------------------
def _make_audited_pair(
    out_dir: Path,
    folder: str,
    e_key: str,
    execution_date: str | None,
    *,
    write_metadata: bool = True,
) -> Path:
    """Lay down <out>/<folder>/<E-KEY>/{audit.json, metadata.json}.

    Mirrors the shape extractor.build_metadata produces — execution_date
    on test_results[0]. Used by the timestamp-cache tests below.
    """
    exec_dir = out_dir / folder / e_key
    exec_dir.mkdir(parents=True, exist_ok=True)
    (exec_dir / "audit.json").write_text(json.dumps({"overall_verdict": "pass"}))
    if write_metadata:
        meta = {"test_results": [{"execution_date": execution_date}]}
        (exec_dir / "metadata.json").write_text(json.dumps(meta))
    return exec_dir


def test_audited_keys_with_timestamps_returns_dates_per_key(tmp_path: Path):
    # Two distinct executions in the same folder, each with their own
    # cached executionDate. The function should return both, keyed by
    # E-key.
    out = tmp_path / "out"
    _make_audited_pair(out, "Sprint_47", "QA-E1", "2026-05-26T10:00:00Z")
    _make_audited_pair(out, "Sprint_47", "QA-E2", "2026-05-27T11:00:00Z")

    result = argus._audited_keys_with_timestamps(out)
    assert result == {
        "QA-E1": "2026-05-26T10:00:00Z",
        "QA-E2": "2026-05-27T11:00:00Z",
    }


def test_audited_keys_with_timestamps_missing_metadata_yields_sentinel(
        tmp_path: Path):
    # An audit.json without a sibling metadata.json should still be
    # tracked — but with the empty-string sentinel so the comparison
    # at the call site triggers re-audit (the safe direction).
    out = tmp_path / "out"
    _make_audited_pair(
        out, "Sprint_47", "QA-E1", None, write_metadata=False)

    result = argus._audited_keys_with_timestamps(out)
    assert result == {"QA-E1": ""}


def test_audited_keys_with_timestamps_unreadable_metadata_yields_sentinel(
        tmp_path: Path):
    # Corrupt JSON in metadata.json must NOT crash the enumerator — fall
    # back to the empty-string sentinel and let the caller re-audit.
    out = tmp_path / "out"
    exec_dir = out / "Sprint_47" / "QA-E1"
    exec_dir.mkdir(parents=True)
    (exec_dir / "audit.json").write_text("{}")
    (exec_dir / "metadata.json").write_text("{not-json")

    result = argus._audited_keys_with_timestamps(out)
    assert result == {"QA-E1": ""}


def test_audited_keys_with_timestamps_excludes_replay_paths(tmp_path: Path):
    # _replay/ subdirs hold prompt-variant experiments (V3, V4, etc.).
    # Treating them as canonical audits would mean a single prompt
    # experiment would suppress every legitimate re-audit afterwards.
    out = tmp_path / "out"
    _make_audited_pair(out, "Sprint_47", "QA-E1", "2026-05-26T10:00:00Z")
    # Drop a replay artefact for a different key to ensure rglob would
    # otherwise pick it up.
    replay_dir = out / "Sprint_47" / "QA-E2" / "_replay" / "v4"
    replay_dir.mkdir(parents=True)
    (replay_dir / "audit.json").write_text("{}")
    (replay_dir / "metadata.json").write_text(json.dumps(
        {"test_results": [{"execution_date": "2026-05-99T00:00:00Z"}]}))

    result = argus._audited_keys_with_timestamps(out)
    # Only the canonical pair shows up; the replay variant is filtered.
    assert result == {"QA-E1": "2026-05-26T10:00:00Z"}
    assert "QA-E2" not in result
    assert "v4" not in result


def test_audited_keys_with_timestamps_handles_missing_out_dir(tmp_path: Path):
    # First-time run: the scoped out_dir doesn't exist yet. Empty dict
    # rather than a crash — every E-key downstream then falls into the
    # "never audited" branch and gets emitted normally.
    nonexistent = tmp_path / "never-created"
    assert argus._audited_keys_with_timestamps(nonexistent) == {}


def test_audited_keys_with_timestamps_handles_missing_test_results(
        tmp_path: Path):
    # metadata.json exists but lacks the test_results array entirely
    # (degraded shape from a partial extractor crash). Sentinel applies.
    out = tmp_path / "out"
    exec_dir = out / "Sprint_47" / "QA-E1"
    exec_dir.mkdir(parents=True)
    (exec_dir / "audit.json").write_text("{}")
    (exec_dir / "metadata.json").write_text(json.dumps({"unrelated": "shape"}))

    result = argus._audited_keys_with_timestamps(out)
    assert result == {"QA-E1": ""}
