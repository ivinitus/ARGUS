from __future__ import annotations

import sys
from pathlib import Path

import pytest

from argus import config as argus_config
from argus.core import exec_paths
from argus.core import paths


@pytest.fixture(autouse=True)
def _clear_root_cache():
    """paths caches the resolved root process-wide; isolate every test."""
    paths.reset_cache()
    yield
    paths.reset_cache()


def _settings(out_dir):
    settings = argus_config.ArgusConfig()
    settings.extractor.out_dir = out_dir
    return settings


def test_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_PROJECT_ROOT", str(tmp_path))
    assert paths.project_root() == tmp_path.resolve()


def test_env_override_expands_user(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ARGUS_PROJECT_ROOT", "~/checkout")
    assert paths.project_root() == (tmp_path / "checkout").resolve()


def test_marker_walk_finds_checkout_root(monkeypatch):
    monkeypatch.delenv("ARGUS_PROJECT_ROOT", raising=False)
    root = paths.project_root()
    assert (root / "pyproject.toml").exists()


def test_result_is_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_PROJECT_ROOT", str(tmp_path))
    first = paths.project_root()
    monkeypatch.setenv("ARGUS_PROJECT_ROOT", str(tmp_path / "elsewhere"))
    assert paths.project_root() == first


def test_relative_out_dir_anchors_to_project_root(tmp_path, monkeypatch):
    """The bug this fixes: './output' must not follow the process cwd."""
    monkeypatch.setenv("ARGUS_PROJECT_ROOT", str(tmp_path))
    from pathlib import Path
    settings = _settings(Path("./output"))

    monkeypatch.chdir(tmp_path)
    from_root = paths.output_root(settings)
    elsewhere = tmp_path / "somewhere_else"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    from_elsewhere = paths.output_root(settings)

    assert from_root == from_elsewhere == (tmp_path / "output").resolve()


def test_absolute_out_dir_passes_through(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_PROJECT_ROOT", str(tmp_path))
    absolute = tmp_path / "elsewhere" / "out"
    assert paths.output_root(_settings(absolute)) == absolute.resolve()


def test_output_root_without_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_PROJECT_ROOT", str(tmp_path))
    assert paths.output_root() == tmp_path / "output"


def test_argus_executable_prefers_project_venv(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_PROJECT_ROOT", str(tmp_path))
    script = tmp_path / ".venv" / "bin" / "argus"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n")
    assert paths.argus_executable() == str(script)
    assert paths.argus_argv() == [str(script)]


def test_argus_executable_falls_back_to_module(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(paths.shutil, "which", lambda _: None)
    assert paths.argus_executable() == f"{sys.executable} -m argus"
    assert paths.argus_argv() == [sys.executable, "-m", "argus"]


def test_argus_executable_uses_path_when_no_venv(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(paths.shutil, "which", lambda _: "/usr/bin/argus")
    assert paths.argus_executable() == "/usr/bin/argus"


def test_cron_wrapper_script_is_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_PROJECT_ROOT", str(tmp_path))
    assert paths.cron_wrapper_script() is None
    (tmp_path / "cron_wrapper.sh").write_text("#!/bin/bash\n")
    assert paths.cron_wrapper_script() == tmp_path / "cron_wrapper.sh"


def _plant_execution(out_dir, *parts, key="QA-E1"):
    import json
    exec_dir = out_dir.joinpath(*parts, key)
    exec_dir.mkdir(parents=True)
    (exec_dir / "audit.json").write_text(json.dumps({
        "overall_verdict": "fail", "summary": "s", "findings": [],
    }))
    (exec_dir / "metadata.json").write_text(json.dumps({
        "test_results": [{"test_case_key": "QA-T1", "marketplace": "US",
                          "status": "Pass",
                          "execution_date": "2026-07-24T10:00:00Z"}],
    }))
    return exec_dir


def test_stale_backups_excluded_from_all_walks(tmp_path):
    out_dir = tmp_path / "output"
    canonical = _plant_execution(out_dir, "Sprint-47")
    _plant_execution(out_dir, "_stale_backups",
                     key="QA-E1_stale_r21_stub_20260724")

    found = list(exec_paths.iter_audit_jsons(out_dir))
    assert found == [canonical / "audit.json"]
    assert exec_paths.find_audit_json(out_dir, "QA-E1") == \
        canonical / "audit.json"
    assert exec_paths.find_execution_dir(out_dir, "QA-E1") == canonical
    assert exec_paths.find_execution_dir(
        out_dir, "QA-E1_stale_r21_stub_20260724") is None
    assert exec_paths.find_audit_json(
        out_dir, "QA-E1_stale_r21_stub_20260724") is None


def test_replay_excluded_from_all_walks(tmp_path):
    out_dir = tmp_path / "output"
    canonical = _plant_execution(out_dir, "Sprint-47")
    _plant_execution(out_dir, "_replay", "v5_tools")

    assert list(exec_paths.iter_audit_jsons(out_dir)) == \
        [canonical / "audit.json"]
    assert exec_paths.find_audit_json(out_dir, "QA-E1") == \
        canonical / "audit.json"
    assert exec_paths.find_execution_dir(out_dir, "QA-E1") == canonical


def test_is_excluded_matches_any_path_component(tmp_path):
    assert exec_paths.is_excluded(Path("output/_stale_backups/x/audit.json"))
    assert exec_paths.is_excluded(Path("output/_replay/v5/x/audit.json"))
    assert not exec_paths.is_excluded(Path("output/Sprint-47/x/audit.json"))


def test_find_execution_dir_treats_key_as_literal_not_glob(tmp_path):
    """Verify execution keys are treated as literals, not globs."""
    out_dir = tmp_path / "output"
    _plant_execution(out_dir, "Sprint-47", key="QA-E123")
    _plant_execution(out_dir, "Sprint-47", key="QA-E999")

    assert exec_paths.find_execution_dir(out_dir, "QA-E*") is None
    assert exec_paths.find_audit_json(out_dir, "QA-E?23") is None
    assert exec_paths.find_execution_dir(
        out_dir, "QA-E123").name == "QA-E123"
