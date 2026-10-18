from __future__ import annotations

import threading

from argus.operations import control


def test_audit_done_only_clears_matching_owner(tmp_path):
    control.configure(tmp_path)

    assert control.mark_audit_running(
        "Sprint 47",
        2,
        pid=1234,
        owner_token="run-old",
    )
    assert control.mark_audit_running(
        "Sprint 47",
        3,
        pid=5678,
        owner_token="run-new",
    )

    assert control.mark_audit_done("run-old") is False
    current = control.current_audit()
    assert current is not None
    assert current["owner_token"] == "run-new"

    assert control.mark_audit_done("run-new") is True
    assert control.current_audit() is None


def test_audit_start_returns_generated_owner_token(tmp_path):
    control.configure(tmp_path)

    owner_token = control.mark_audit_running("Sprint 48", 1, pid=1234)

    assert isinstance(owner_token, str)
    current = control.current_audit()
    assert current is not None
    assert current["owner_token"] == owner_token
    assert control.mark_audit_done(owner_token) is True


def test_pause_cannot_overwrite_concurrent_audit_start(
    tmp_path,
    monkeypatch,
):
    control.configure(tmp_path)
    original_write = control._write
    pause_write_started = threading.Event()
    release_pause_write = threading.Event()
    errors: list[BaseException] = []

    def controlled_write(state):
        if state.get("paused") is True:
            pause_write_started.set()
            assert release_pause_write.wait(timeout=2)
        return original_write(state)

    def run(target):
        try:
            target()
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(control, "_write", controlled_write)
    pause_thread = threading.Thread(
        target=run,
        args=(lambda: control.pause("operator", "maintenance"),),
    )
    audit_thread = threading.Thread(
        target=run,
        args=(lambda: control.mark_audit_running(
            "Sprint 47",
            3,
            pid=5678,
            owner_token="run-new",
        ),),
    )

    pause_thread.start()
    assert pause_write_started.wait(timeout=2)
    audit_thread.start()
    audit_thread.join(timeout=0.1)
    assert audit_thread.is_alive()
    release_pause_write.set()
    pause_thread.join(timeout=2)
    audit_thread.join(timeout=2)

    assert errors == []
    state = control.status()
    assert state["paused"] is True
    assert state["current_audit"]["owner_token"] == "run-new"


def test_audit_phase_and_requester_are_persisted(tmp_path):
    control.configure(tmp_path)

    assert control.mark_audit_running(
        "Sprint 47",
        0,
        pid=1234,
        owner_token="run-1",
        phase="preparing",
        requested_by="alice",
        target="QA-C21462",
    )
    current = control.current_audit()
    assert current is not None
    assert current["phase"] == "preparing"
    assert current["requested_by"] == "alice"
    assert current["target"] == "QA-C21462"
    assert current["n_keys"] == 0

    assert control.mark_audit_running(
        "Sprint 47",
        12,
        pid=1234,
        owner_token="run-1",
    )
    current = control.current_audit()
    assert current is not None
    assert current["phase"] == "auditing"
    assert "requested_by" not in current
    assert control.mark_audit_done("run-1") is True
