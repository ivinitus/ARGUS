"""`argus remove-folder`: filesystem guards, index/pointer cleanup, notices."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from argus import config as argus_config
from argus.operations import audit_index, remove_folder


def _settings(tmp_path: Path) -> argus_config.ArgusConfig:
    settings = argus_config.ArgusConfig()
    settings.extractor.out_dir = tmp_path / "output"
    settings.general.state_dir = tmp_path / "state"
    settings.extractor.out_dir.mkdir(parents=True)
    settings.general.state_dir.mkdir(parents=True)
    return settings


def _make_folder(output_root: Path, slug: str, *, meta: bool = True) -> Path:
    folder = output_root / slug
    exec_dir = folder / "QA-E1"
    exec_dir.mkdir(parents=True)
    (exec_dir / "audit.json").write_text(json.dumps({
        "execution_key": "QA-E1",
        "verdict": "PASS",
        "findings": [],
    }))
    if meta:
        (folder / "_folder_meta.json").write_text(
            json.dumps({"folder_name": slug}))
    return folder


@pytest.fixture(autouse=True)
def _no_live_audit(monkeypatch):
    monkeypatch.setattr(remove_folder.control, "current_audit", lambda: None)


def test_plan_refuses_missing_symlink_and_non_argus_dirs(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    root = settings.extractor.out_dir

    with pytest.raises(remove_folder.RemovalRefused, match="no folder named"):
        remove_folder.plan(settings, "Sprint-99")

    real = _make_folder(root, "Sprint-47")
    (root / "Sprint-link").symlink_to(real, target_is_directory=True)
    with pytest.raises(remove_folder.RemovalRefused, match="symlink"):
        remove_folder.plan(settings, "Sprint-link")

    _make_folder(root, "No-Meta", meta=False)
    with pytest.raises(remove_folder.RemovalRefused, match="_folder_meta.json"):
        remove_folder.plan(settings, "No-Meta")

    (root / "a-file").write_text("x")
    with pytest.raises(remove_folder.RemovalRefused, match="not a directory"):
        remove_folder.plan(settings, "a-file")

    for bad in ("..", "Sprint 47", "../etc", "teams/x"):
        with pytest.raises(remove_folder.RemovalRefused, match="exact on-disk slug"):
            remove_folder.plan(settings, bad)

    # The symlink still exists and the real folder was never touched.
    assert (root / "Sprint-link").is_symlink()
    assert (real / "_folder_meta.json").is_file()


def test_plan_refuses_the_folder_being_audited(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _make_folder(settings.extractor.out_dir, "Sprint-47")
    monkeypatch.setattr(
        remove_folder.control, "current_audit",
        lambda: {"folder": "Sprint-47", "phase": "auditing", "stale": False,
                 "requested_by": "alice"},
    )
    with pytest.raises(remove_folder.RemovalRefused, match="being audited"):
        remove_folder.plan(settings, "Sprint-47")

    # A stale or terminal record no longer blocks.
    monkeypatch.setattr(
        remove_folder.control, "current_audit",
        lambda: {"folder": "Sprint-47", "phase": "stopped", "stale": False},
    )
    assert remove_folder.plan(settings, "Sprint-47").folder_dir is not None


def test_apply_removes_exactly_the_folder_and_cleans_index_and_pointer(tmp_path):
    settings = _settings(tmp_path)
    root = settings.extractor.out_dir
    doomed = _make_folder(root, "Sprint-47")
    kept = _make_folder(root, "Sprint-46")
    audit_index.sync(root)
    pointer = settings.general.state_dir / "last_folder_out_dir"
    pointer.write_text(str(doomed.resolve()) + "\n")

    removal = remove_folder.plan(settings, "Sprint-47")
    assert removal.pointer_file == pointer

    summary = remove_folder.apply(removal)

    assert not doomed.exists()
    assert (kept / "QA-E1" / "audit.json").is_file()
    assert summary["removed_dir"] == str(doomed)
    assert summary["index_synced"] is True
    assert summary["pointer_cleared"] is True
    assert not pointer.exists()
    conn = audit_index._connect(root)
    try:
        folders = {row[0] for row in conn.execute("SELECT folder FROM audits")}
    finally:
        conn.close()
    assert folders == {"Sprint-46"}


def test_pointer_to_another_folder_is_left_alone(tmp_path):
    settings = _settings(tmp_path)
    root = settings.extractor.out_dir
    _make_folder(root, "Sprint-47")
    other = _make_folder(root, "Sprint-46")
    pointer = settings.general.state_dir / "last_folder_out_dir"
    pointer.write_text(str(other.resolve()) + "\n")

    summary = remove_folder.apply(remove_folder.plan(settings, "Sprint-47"))

    assert summary["pointer_cleared"] is False
    assert pointer.read_text().strip() == str(other.resolve())




def test_main_requires_yes_and_posts_a_notice(tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path)
    root = settings.extractor.out_dir
    folder = _make_folder(root, "Sprint-47")
    monkeypatch.setattr(remove_folder.argus_config, "load", lambda *a, **k: settings)
    monkeypatch.setattr(remove_folder.runtime, "configure", lambda s: None)
    posts: list[tuple] = []
    monkeypatch.setattr(
        remove_folder.notify, "post",
        lambda level, title, body="", **kw: posts.append((level, title, body)),
    )
    monkeypatch.setenv("ARGUS_REQUESTED_BY", "alice")

    assert remove_folder.main(["--folder", "Sprint-47"]) == 1
    assert folder.exists()
    assert "dry run" in capsys.readouterr().err
    assert posts == []

    assert remove_folder.main(["--folder", "Sprint-47", "--yes"]) == 0
    assert not folder.exists()
    summary = json.loads(capsys.readouterr().out)
    assert summary["folder"] == "Sprint-47"
    assert len(posts) == 1
    level, title, body = posts[0]
    assert level == "info"
    assert "Sprint-47 removed" in title
    assert "requested by alice" in body

    # Refusals exit 1 and, when --yes was given, warn the channel.
    assert remove_folder.main(["--folder", "Sprint-47", "--yes"]) == 1
    assert posts[-1][0] == "warn"
    assert "refused" in posts[-1][1]
    assert remove_folder.main(["--folder", "Sprint-47", "--yes", "--no-notify"]) == 1
    assert len(posts) == 2


def test_cli_dispatch_table_registers_remove_folder():
    from argus.cli import main as cli_main

    assert cli_main._commands()["remove-folder"] == "argus.operations.remove_folder"
