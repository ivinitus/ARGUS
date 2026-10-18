"""Tests for `argus prune` — the retention job that reclaims output/ disk."""
from __future__ import annotations

import json
import os
import time

import pytest

from argus.operations import prune

_DAY = 86400


def _make_execution(
    folder,
    key,
    *,
    age_days=0.0,
    screenshots=2,
    pdf=True,
    video=False,
):
    """Create one execution dir with evidence and audit artefacts."""
    exec_dir = folder / key
    exec_dir.mkdir(parents=True, exist_ok=True)
    audit = exec_dir / "audit.json"
    audit.write_text(json.dumps({"key": key, "verdict": "pass"}))
    (exec_dir / "audit.md").write_text("# audit\n")
    (exec_dir / "metadata.json").write_text(json.dumps({"key": key}))
    shots = exec_dir / "screenshots"
    shots.mkdir(exist_ok=True)
    for i in range(screenshots):
        (shots / f"page_{i}.jpg").write_bytes(b"x" * 1024)
    if pdf:
        (exec_dir / f"Session_{key}.pdf").write_bytes(b"y" * 2048)
    if video:
        video_dir = exec_dir / "video_evidence"
        video_dir.mkdir(exist_ok=True)
        (video_dir / f"Recording_{key}.mp4").write_bytes(b"v" * 4096)
        (exec_dir / "video_evidence.json").write_text(json.dumps({
            "videos": [{
                "path": f"video_evidence/Recording_{key}.mp4",
            }],
        }))
    replay = exec_dir / "_replay" / "variant_a"
    replay.mkdir(parents=True, exist_ok=True)
    (replay / "audit.json").write_text("{}")
    (replay / "screenshots").mkdir(exist_ok=True)
    (replay / "screenshots" / "page_0.jpg").write_bytes(b"z" * 512)

    mtime = time.time() - age_days * _DAY
    os.utime(audit, (mtime, mtime))
    return exec_dir


def _folder(
    root,
    name,
    *,
    age_days=0.0,
    keys=("QA-E1",),
    video=False,
):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "_folder_meta.json").write_text(json.dumps({"name": name}))
    for key in keys:
        _make_execution(folder, key, age_days=age_days, video=video)
    return folder


@pytest.fixture
def output_root(tmp_path):
    root = tmp_path / "output"
    root.mkdir()
    return root


def test_old_folder_is_planned_and_recent_one_is_kept(output_root):
    _folder(output_root, "old_folder", age_days=90)
    _folder(output_root, "new_folder", age_days=3)

    result = prune.plan(output_root, older_than_days=60,
                        output_root=output_root)
    actions = {f.name: f.action for f in result.folders}
    assert actions == {"old_folder": "prune", "new_folder": "keep"}
    assert result.total_bytes > 0


def test_apply_removes_evidence_and_keeps_every_artefact(output_root):
    folder = _folder(output_root, "old_folder", age_days=90, video=True)
    exec_dir = folder / "QA-E1"

    prune.apply(prune.plan(output_root, older_than_days=60,
                           output_root=output_root))

    assert not (exec_dir / "screenshots").exists()
    assert not (exec_dir / "video_evidence").exists()
    assert list(exec_dir.glob("*.pdf")) == []
    assert (exec_dir / "audit.json").exists()
    assert (exec_dir / "audit.md").exists()
    assert (exec_dir / "metadata.json").exists()
    assert (exec_dir / "video_evidence.json").exists()
    assert (folder / "_folder_meta.json").exists()
    assert (exec_dir / "_replay" / "variant_a" / "screenshots"
            / "page_0.jpg").exists()


def test_dry_run_deletes_nothing(output_root):
    folder = _folder(output_root, "old_folder", age_days=90)
    result = prune.plan(output_root, older_than_days=60,
                        output_root=output_root)

    assert result.applied is False
    assert result.total_bytes > 0
    assert (folder / "QA-E1" / "screenshots").exists()


def test_folder_with_a_recent_audit_is_never_pruned(output_root):
    folder = _folder(output_root, "active", age_days=400)
    _make_execution(folder, "QA-E999", age_days=1)

    result = prune.plan(output_root, older_than_days=60,
                        output_root=output_root)
    assert [f.action for f in result.folders] == ["keep"]
    prune.apply(result)
    assert (folder / "QA-E1" / "screenshots").exists()


def test_folder_without_any_audit_json_is_skipped(output_root):
    stray = output_root / "not_ours"
    stray.mkdir()
    (stray / "screenshots").mkdir()
    (stray / "screenshots" / "page_0.jpg").write_bytes(b"x" * 4096)

    result = prune.plan(output_root, older_than_days=60,
                        output_root=output_root)
    assert [f.action for f in result.folders] == ["skipped"]
    prune.apply(result)
    assert (stray / "screenshots" / "page_0.jpg").exists()


def test_already_pruned_folder_reports_clean(output_root):
    folder = _folder(output_root, "old_folder", age_days=90)
    prune.apply(prune.plan(output_root, older_than_days=60,
                           output_root=output_root))

    second = prune.plan(output_root, older_than_days=60,
                        output_root=output_root)
    assert [f.action for f in second.folders] == ["clean"]
    assert second.total_bytes == 0
    assert (folder / "QA-E1" / "audit.json").exists()


def test_root_pointing_at_one_folder_scopes_to_it(output_root):
    old = _folder(output_root, "old_folder", age_days=90)
    other = _folder(output_root, "other_old", age_days=90)

    result = prune.plan(old, older_than_days=60, output_root=output_root)
    assert [f.name for f in result.pruned] == ["old_folder"]

    prune.apply(result)
    assert not (old / "QA-E1" / "screenshots").exists()
    assert (other / "QA-E1" / "screenshots").exists()


def test_missing_root_is_a_no_op(tmp_path):
    result = prune.plan(tmp_path / "absent", older_than_days=60)
    assert result.folders == []
    assert result.total_bytes == 0


def test_multiple_executions_all_pruned(output_root):
    folder = _folder(output_root, "old_folder", age_days=90,
                     keys=("QA-E1", "QA-E2", "QA-E3"))
    result = prune.plan(output_root, older_than_days=60,
                        output_root=output_root)
    assert result.total_targets == 6
    prune.apply(result)
    for key in ("QA-E1", "QA-E2", "QA-E3"):
        assert not (folder / key / "screenshots").exists()
        assert (folder / key / "audit.json").exists()


def test_apply_records_failures_without_raising(output_root, monkeypatch):
    """One unreadable target must not abandon the rest of the reclaim."""
    _folder(output_root, "old_folder", age_days=90,
            keys=("QA-E1", "QA-E2"))
    result = prune.plan(output_root, older_than_days=60,
                        output_root=output_root)

    real_rmtree = prune.shutil.rmtree
    calls: list = []

    def flaky_rmtree(path, *a, **kw):
        calls.append(path)
        if len(calls) == 1:
            raise OSError("permission denied")
        return real_rmtree(path, *a, **kw)

    monkeypatch.setattr(prune.shutil, "rmtree", flaky_rmtree)
    prune.apply(result)

    assert len(result.errors) == 1
    assert "permission denied" in result.errors[0]
    assert len(calls) == 2


def test_to_dict_is_json_serialisable(output_root):
    _folder(output_root, "old_folder", age_days=90)
    result = prune.plan(output_root, older_than_days=60,
                        output_root=output_root)
    payload = json.loads(json.dumps(result.to_dict()))
    assert payload["total_targets"] == 2
    assert payload["applied"] is False
    assert payload["folders"][0]["action"] == "prune"




def _settings(output_root, older_than_days=60.0):
    from argus import config as argus_config
    settings = argus_config.ArgusConfig()
    settings.extractor.out_dir = output_root
    settings.prune.older_than_days = older_than_days
    return settings


def test_main_defaults_to_dry_run(output_root, monkeypatch, capsys):
    folder = _folder(output_root, "old_folder", age_days=90)
    monkeypatch.setattr(prune.argus_config, "load",
                        lambda: _settings(output_root))

    assert prune.main([]) == 0
    assert "would prune" in capsys.readouterr().out
    assert (folder / "QA-E1" / "screenshots").exists()


def test_main_apply_deletes_and_notifies(output_root, monkeypatch, capsys):
    folder = _folder(output_root, "old_folder", age_days=90)
    monkeypatch.setattr(prune.argus_config, "load",
                        lambda: _settings(output_root))
    posted: list[tuple] = []
    monkeypatch.setattr(prune.notify, "post",
                        lambda level, title, body="", **kw:
                        posted.append((level, title, body)))

    assert prune.main(["--apply"]) == 0
    assert not (folder / "QA-E1" / "screenshots").exists()
    assert len(posted) == 1
    level, title, body = posted[0]
    assert level == "info"
    assert "reclaimed" in title
    assert "old_folder" in body


def test_main_apply_notifies_even_when_nothing_eligible(
    output_root, monkeypatch,
):
    """"Ran and found nothing" must be distinguishable from "never ran"."""
    _folder(output_root, "new_folder", age_days=2)
    monkeypatch.setattr(prune.argus_config, "load",
                        lambda: _settings(output_root))
    posted: list[tuple] = []
    monkeypatch.setattr(prune.notify, "post",
                        lambda level, title, body="", **kw:
                        posted.append((level, title, body)))

    assert prune.main(["--apply"]) == 0
    assert len(posted) == 1
    assert posted[0][0] == "info"
    assert "found nothing" in posted[0][1]


def test_main_no_notify_suppresses_the_post(output_root, monkeypatch):
    _folder(output_root, "old_folder", age_days=90)
    monkeypatch.setattr(prune.argus_config, "load",
                        lambda: _settings(output_root))
    posted: list = []
    monkeypatch.setattr(prune.notify, "post",
                        lambda *a, **kw: posted.append(a))

    assert prune.main(["--apply", "--no-notify"]) == 0
    assert posted == []


def test_main_json_output(output_root, monkeypatch, capsys):
    _folder(output_root, "old_folder", age_days=90)
    monkeypatch.setattr(prune.argus_config, "load",
                        lambda: _settings(output_root))

    assert prune.main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_targets"] == 2
    assert payload["applied"] is False


def test_main_older_than_days_flag_overrides_config(
    output_root, monkeypatch, capsys,
):
    _folder(output_root, "folder", age_days=10)
    monkeypatch.setattr(prune.argus_config, "load",
                        lambda: _settings(output_root, older_than_days=60))

    prune.main(["--json"])
    assert json.loads(capsys.readouterr().out)["total_targets"] == 0

    prune.main(["--older-than-days", "5", "--json"])
    assert json.loads(capsys.readouterr().out)["total_targets"] == 2


def test_main_returns_nonzero_when_a_delete_fails(
    output_root, monkeypatch, capsys,
):
    """cron needs a non-zero exit to route this through notify_warn."""
    _folder(output_root, "old_folder", age_days=90)
    monkeypatch.setattr(prune.argus_config, "load",
                        lambda: _settings(output_root))
    monkeypatch.setattr(prune.notify, "post", lambda *a, **kw: None)
    monkeypatch.setattr(
        prune.shutil, "rmtree",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("nope")))

    assert prune.main(["--apply"]) == 1


def test_main_survives_unloadable_config(output_root, monkeypatch, capsys):
    """cron must still get a prune when config.toml is broken."""
    def boom():
        raise RuntimeError("bad config")

    monkeypatch.setattr(prune.argus_config, "load", boom)
    monkeypatch.setattr(prune.paths, "output_root",
                        lambda settings=None: output_root)
    _folder(output_root, "old_folder", age_days=90)

    assert prune.main(["--json"]) == 0
    err = capsys.readouterr().err
    assert "config unavailable" in err


def test_if_enabled_respects_the_config_gate(output_root, monkeypatch, capsys):
    """The cron step must be switchable off without editing crontab."""
    folder = _folder(output_root, "old_folder", age_days=90)
    settings = _settings(output_root)
    settings.prune.enabled = False
    monkeypatch.setattr(prune.argus_config, "load", lambda: settings)
    posted: list = []
    monkeypatch.setattr(prune.notify, "post",
                        lambda *a, **kw: posted.append(a))

    assert prune.main(["--apply", "--if-enabled"]) == 0
    assert "disabled" in capsys.readouterr().out
    assert (folder / "QA-E1" / "screenshots").exists()
    assert posted == []


def test_manual_run_ignores_the_enabled_gate(output_root, monkeypatch):
    """Without --if-enabled the operator's explicit invocation wins."""
    folder = _folder(output_root, "old_folder", age_days=90)
    settings = _settings(output_root)
    settings.prune.enabled = False
    monkeypatch.setattr(prune.argus_config, "load", lambda: settings)
    monkeypatch.setattr(prune.notify, "post", lambda *a, **kw: None)

    assert prune.main(["--apply"]) == 0
    assert not (folder / "QA-E1" / "screenshots").exists()


def test_replay_only_audits_do_not_reset_folder_age(output_root):
    folder = _folder(output_root, "old_folder", age_days=90)
    replay_audit = (folder / "QA-E1" / "_replay" / "variant_a"
                    / "audit.json")
    now = time.time()
    os.utime(replay_audit, (now, now))

    result = prune.plan(output_root, older_than_days=60,
                        output_root=output_root)
    assert [f.action for f in result.folders] == ["prune"]
