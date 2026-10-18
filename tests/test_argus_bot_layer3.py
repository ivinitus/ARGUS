"""Unit tests for Layer 3 escape hatches in argus_bot."""
from __future__ import annotations

import json
import sys
import types
from unittest import mock

import pytest


class _StubSocketModeResponse:
    """Captures envelope_id + payload kwargs the way the real class does."""

    def __init__(self, envelope_id=None, payload=None):
        self.envelope_id = envelope_id
        self.payload = payload or {}


def _install_slack_sdk_stubs() -> None:
    if "slack_sdk" in sys.modules:
        return
    slack_sdk = types.ModuleType("slack_sdk")
    slack_sdk.WebClient = type("WebClient", (), {})
    socket_mode = types.ModuleType("slack_sdk.socket_mode")
    socket_mode.SocketModeClient = type("SocketModeClient", (), {})
    request_mod = types.ModuleType("slack_sdk.socket_mode.request")
    request_mod.SocketModeRequest = type("SocketModeRequest", (), {})
    response_mod = types.ModuleType("slack_sdk.socket_mode.response")
    response_mod.SocketModeResponse = _StubSocketModeResponse
    sys.modules["slack_sdk"] = slack_sdk
    sys.modules["slack_sdk.socket_mode"] = socket_mode
    sys.modules["slack_sdk.socket_mode.request"] = request_mod
    sys.modules["slack_sdk.socket_mode.response"] = response_mod


_install_slack_sdk_stubs()
from argus.operations import bot as argus_bot  # noqa: E402 — stubs must precede
from argus.operations.teams import resolve_team  # noqa: E402
from argus.operations.test_run_targets import JiraAuditTarget  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_log_path(tmp_path, monkeypatch):
    """Keep subprocess-spawn tests from touching the operator's home dir."""
    monkeypatch.setattr(argus_bot, "_CRON_LOG", tmp_path / "cron.log")


def _mock_req(envelope_id: str = "env-1") -> mock.MagicMock:
    """Minimal SocketModeRequest stand-in with the only field _ack reads."""
    req = mock.MagicMock()
    req.envelope_id = envelope_id
    return req


def _mock_client() -> mock.MagicMock:
    """SocketModeClient stand-in — we only assert on send_socket_mode_response being called via the handlers' internal `_ack`."""
    return mock.MagicMock()


def _last_ack_text(client: mock.MagicMock) -> str:
    """Pull the text body from the most recent _ack(...) call."""
    assert client.send_socket_mode_response.called, "no ack was sent"
    call = client.send_socket_mode_response.call_args
    response = call.args[0] if call.args else call.kwargs.get("response")
    payload = getattr(response, "payload", {}) or {}
    return payload.get("text", "")


def test_reaudit_rejects_non_e_key_format(monkeypatch):
    client = _mock_client()
    req = _mock_req()
    popen = mock.MagicMock()
    monkeypatch.setattr("subprocess.Popen", popen)
    monkeypatch.setattr(argus_bot.argus_control, "is_paused", lambda: False)
    monkeypatch.setattr(argus_bot.argus_control, "current_audit",
                        lambda: None)

    argus_bot._handle_reaudit(client, req, "argus-test-user", "not-a-key")

    text = _last_ack_text(client)
    assert "usage" in text.lower()
    assert "/argus reaudit" in text
    popen.assert_not_called()


def test_reaudit_rejects_empty_args(monkeypatch):
    """No args at all: usage hint, no subprocess."""
    client = _mock_client()
    req = _mock_req()
    popen = mock.MagicMock()
    monkeypatch.setattr("subprocess.Popen", popen)
    monkeypatch.setattr(argus_bot.argus_control, "is_paused", lambda: False)
    monkeypatch.setattr(argus_bot.argus_control, "current_audit",
                        lambda: None)

    argus_bot._handle_reaudit(client, req, "argus-test-user", "")

    assert "usage" in _last_ack_text(client).lower()
    popen.assert_not_called()


def test_reaudit_rejects_when_paused(monkeypatch):
    client = _mock_client()
    req = _mock_req()
    popen = mock.MagicMock()
    monkeypatch.setattr("subprocess.Popen", popen)
    monkeypatch.setattr(argus_bot.argus_control, "is_paused", lambda: True)
    monkeypatch.setattr(argus_bot.argus_control, "current_audit",
                        lambda: None)

    argus_bot._handle_reaudit(client, req, "argus-test-user", "QA-E12345")

    assert "paused" in _last_ack_text(client).lower()
    popen.assert_not_called()


def test_reaudit_rejects_when_audit_in_flight(monkeypatch):
    client = _mock_client()
    req = _mock_req()
    popen = mock.MagicMock()
    monkeypatch.setattr("subprocess.Popen", popen)
    monkeypatch.setattr(argus_bot.argus_control, "is_paused", lambda: False)
    monkeypatch.setattr(argus_bot.argus_control, "current_audit", lambda: {
        "folder": "Sprint-47", "n_keys": 10, "stale": False,
        "age_seconds": 12, "pid": 9999,
    })

    argus_bot._handle_reaudit(client, req, "argus-test-user", "QA-E12345")

    text = _last_ack_text(client).lower()
    assert "in progress" in text or "in flight" in text or "wait" in text
    popen.assert_not_called()


def test_reaudit_proceeds_when_audit_in_flight_is_stale(monkeypatch):
    client = _mock_client()
    req = _mock_req()
    popen = mock.MagicMock()
    popen.return_value.pid = 4242
    monkeypatch.setattr("subprocess.Popen", popen)
    monkeypatch.setattr(argus_bot.argus_control, "is_paused", lambda: False)
    monkeypatch.setattr(argus_bot.argus_control, "current_audit", lambda: {
        "folder": "Sprint-47", "n_keys": 10, "stale": True,
        "age_seconds": 99999, "pid": 1,
    })

    argus_bot._handle_reaudit(client, req, "argus-test-user", "QA-E12345")

    popen.assert_called_once()
    assert ":repeat:" in _last_ack_text(client)














def test_local_comma_separated_refs_refuse_multi_team_fanout(monkeypatch):
    client = _mock_client()
    req = _mock_req()
    req.payload = {}
    resolver = mock.MagicMock()
    resolver.resolve.side_effect = [
        JiraAuditTarget(
            reference_kind="run",
            key="QA-C1",
            project_id="10000",
            folder_id=10,
            folder_name="Targeted regression - 03 sep 2026(WEB)",
            team=resolve_team("web"),
            audit_kind="run",
            audit_value="QA-C1",
            source_name="US Chrome",
        ),
        JiraAuditTarget(
            reference_kind="run",
            key="PORTALQA-C2",
            project_id="10002",
            folder_id=20,
            folder_name="PORTAL regression - Sep 2026",
            team=resolve_team("portal"),
            audit_kind="run",
            audit_value="PORTALQA-C2",
            source_name="Earnings statements",
        ),
    ]
    spawn = mock.MagicMock()
    monkeypatch.setattr(argus_bot, "_JIRA_TARGET_RESOLVER", resolver)
    monkeypatch.setattr(argus_bot, "_spawn_detached", spawn)
    monkeypatch.setattr(argus_bot.argus_control, "is_paused", lambda: False)

    argus_bot._handle_run(
        client,
        req,
        "argus-test-user",
        "QA-C1, PORTALQA-C2",
    )

    assert "The local bot launches one team at a time" in (
        _last_ack_text(client)
    )
    spawn.assert_not_called()


def test_reaudit_spawns_subprocess(monkeypatch):
    client = _mock_client()
    req = _mock_req()
    popen = mock.MagicMock()
    popen.return_value.pid = 12345
    monkeypatch.setattr("subprocess.Popen", popen)
    monkeypatch.setattr(argus_bot.argus_control, "is_paused", lambda: False)
    monkeypatch.setattr(argus_bot.argus_control, "current_audit",
                        lambda: None)

    argus_bot._handle_reaudit(client, req, "argus-test-user",
                              "QA-E12345 because tester fixed it")

    popen.assert_called_once()
    cmd = popen.call_args.args[0]
    assert cmd[0].endswith("/argus")
    assert cmd[1] == "audit"
    assert "--key" in cmd
    key_idx = cmd.index("--key")
    assert cmd[key_idx + 1] == "QA-E12345"

    text = _last_ack_text(client)
    assert ":repeat:" in text
    assert "QA-E12345" in text
    assert "12345" in text


def test_dismiss_rejects_invalid_key(monkeypatch):
    """Bad regex → usage hint, no filesystem mutation."""
    client = _mock_client()
    req = _mock_req()
    find = mock.MagicMock()
    monkeypatch.setattr(argus_bot, "_find_audit_json", find)

    argus_bot._handle_dismiss(client, req, "argus-test-user", "garbage-key")

    assert "usage" in _last_ack_text(client).lower()
    find.assert_not_called()


def test_dismiss_writes_acknowledgment(tmp_path, monkeypatch):
    audit_path = tmp_path / "QA-E1" / "audit.json"
    audit_path.parent.mkdir(parents=True)
    audit_path.write_text(json.dumps({
        "overall_verdict": "fail",
        "summary": "x",
        "findings": [],
    }))
    monkeypatch.setattr(argus_bot, "_find_audit_json",
                        lambda key: audit_path)

    client = _mock_client()
    req = _mock_req()
    argus_bot._handle_dismiss(client, req, "argus-test-user",
                              "QA-E1 false positive — env_check noise")

    after = json.loads(audit_path.read_text())
    assert "acknowledgments" in after
    acks = after["acknowledgments"]
    assert len(acks) == 1
    assert acks[0]["type"] == "dismissed"
    assert acks[0]["by"] == "argus-test-user"
    assert acks[0]["reason"] == "false positive — env_check noise"
    assert "at" in acks[0] and acks[0]["at"]

    text = _last_ack_text(client)
    assert ":mute:" in text
    assert "QA-E1" in text


def test_dismiss_appends_to_existing_list(tmp_path, monkeypatch):
    audit_path = tmp_path / "QA-E1" / "audit.json"
    audit_path.parent.mkdir(parents=True)
    audit_path.write_text(json.dumps({
        "overall_verdict": "fail",
        "findings": [],
        "acknowledgments": [{"type": "old-thing", "by": "someone-else"}],
    }))
    monkeypatch.setattr(argus_bot, "_find_audit_json",
                        lambda key: audit_path)

    client = _mock_client()
    req = _mock_req()
    argus_bot._handle_dismiss(client, req, "argus-test-user", "QA-E1")

    after = json.loads(audit_path.read_text())
    acks = after["acknowledgments"]
    assert len(acks) == 2
    assert acks[0]["type"] == "old-thing"
    assert acks[0]["by"] == "someone-else"
    assert acks[1]["type"] == "dismissed"
    assert acks[1]["by"] == "argus-test-user"
    assert acks[1]["reason"] is None


def test_dismiss_no_audit_found(monkeypatch):
    """E-key never audited → :question: ack, no write attempted."""
    monkeypatch.setattr(argus_bot, "_find_audit_json", lambda key: None)
    client = _mock_client()
    req = _mock_req()

    argus_bot._handle_dismiss(client, req, "argus-test-user", "QA-E99999")

    text = _last_ack_text(client)
    assert ":question:" in text
    assert "QA-E99999" in text


def test_find_audit_json_returns_none_when_output_dir_missing(monkeypatch,
                                                              tmp_path):
    monkeypatch.chdir(tmp_path)
    result = argus_bot._find_audit_json("QA-E000000-fake-not-real")
    assert result is None


def _slash_req(command: str, text: str, user_name: str = "mallory",
               user_id: str = "U0MALLORY") -> mock.MagicMock:
    req = _mock_req()
    req.payload = {"command": command, "text": text,
                   "user_name": user_name, "user_id": user_id}
    return req


def test_mutating_command_refused_for_non_admin(monkeypatch):
    """A user off the allowlist must not reach the dismiss handler."""
    monkeypatch.setattr(argus_bot, "_BOT_ADMINS", frozenset({"U0ADMIN1"}))
    handler = mock.MagicMock()
    monkeypatch.setitem(argus_bot._SUBCOMMANDS, "dismiss", handler)
    client = _mock_client()

    argus_bot._handle_command(client, _slash_req("/argus", "dismiss QA-E1"))

    handler.assert_not_called()
    assert ":no_entry:" in _last_ack_text(client)


def test_display_name_match_is_not_admin(monkeypatch):
    monkeypatch.setattr(argus_bot, "_BOT_ADMINS", frozenset({"display-admin"}))
    handler = mock.MagicMock()
    monkeypatch.setitem(argus_bot._SUBCOMMANDS, "pause", handler)
    client = _mock_client()

    argus_bot._handle_command(
        client, _slash_req("/argus", "pause lunch",
                           user_name="display-admin", user_id="U0OUTSIDER"))

    handler.assert_not_called()
    assert ":no_entry:" in _last_ack_text(client)


def test_mutating_command_allowed_for_admin_by_user_id(monkeypatch):
    monkeypatch.setattr(argus_bot, "_BOT_ADMINS", frozenset({"U0ADMIN1"}))
    handler = mock.MagicMock()
    monkeypatch.setitem(argus_bot._SUBCOMMANDS, "run", handler)
    client = _mock_client()

    argus_bot._handle_command(
        client, _slash_req("/argus", "run Folder",
                           user_name="renamed-user", user_id="U0ADMIN1"))

    handler.assert_called_once()




def test_validate_admin_ids_drops_display_names(caplog):
    valid = argus_bot._validate_admin_ids(
        ["U01ABC2DEF3", "WTBEDCBA987", "display-admin", "other-admin", ""])

    assert valid == frozenset({"U01ABC2DEF3", "WTBEDCBA987"})
    assert "display-admin" in caplog.text and "IGNORED" in caplog.text


def test_validate_admin_ids_empty_when_all_names():
    assert argus_bot._validate_admin_ids(
        ["display-admin", "other-admin"]) == frozenset()


def test_status_and_help_open_to_everyone(monkeypatch):
    monkeypatch.setattr(argus_bot, "_BOT_ADMINS", frozenset({"U0ADMIN1"}))
    for sub in ("status", "help"):
        handler = mock.MagicMock()
        monkeypatch.setitem(argus_bot._SUBCOMMANDS, sub, handler)
        client = _mock_client()
        argus_bot._handle_command(client, _slash_req("/argus", sub))
        handler.assert_called_once()




def test_empty_allowlist_fails_closed(monkeypatch):
    """No admins configured => mutating commands refused with setup hint."""
    monkeypatch.setattr(argus_bot, "_BOT_ADMINS", frozenset())
    handler = mock.MagicMock()
    monkeypatch.setitem(argus_bot._SUBCOMMANDS, "run", handler)
    client = _mock_client()

    argus_bot._handle_command(client, _slash_req("/argus", "run example-folder"))

    handler.assert_not_called()
    assert "bot_admin_users" in _last_ack_text(client)


def test_slack_redelivery_is_acknowledged_but_not_executed(monkeypatch):
    client = _mock_client()
    req = _mock_req("env-9")
    req.type = "slash_commands"
    req.retry_attempt = 1
    req.retry_reason = "timeout"
    handled: list = []
    monkeypatch.setattr(argus_bot, "_handle_command",
                        lambda c, r: handled.append(r))

    argus_bot._dispatch_socket_request(client, req)

    assert handled == []
    client.send_socket_mode_response.assert_called_once()
    assert client.send_socket_mode_response.call_args.args[0].envelope_id == "env-9"


def test_first_delivery_of_a_slash_command_is_dispatched(monkeypatch):
    client = _mock_client()
    req = _mock_req("env-1")
    req.type = "slash_commands"
    req.retry_attempt = None
    handled: list = []
    monkeypatch.setattr(argus_bot, "_handle_command",
                        lambda c, r: handled.append(r))

    argus_bot._dispatch_socket_request(client, req)

    assert handled == [req]








def _stale_launch_record(phase: str = "launching") -> dict:
    return {
        "folder": "HS Reg Sept 1",
        "phase": phase,
        "n_keys": 0,
        "stale": False,
        "age_seconds": 900,
        "owner_token": "run-orphan",
        "requested_by": "alice",
    }








def test_status_shows_current_audit_phase_and_requester(monkeypatch):
    monkeypatch.setattr(argus_bot.argus_control, "status", lambda: {
        "paused": False,
        "paused_by": None,
    })
    monkeypatch.setattr(
        argus_bot.argus_control,
        "bot_heartbeat_age_seconds",
        lambda: 5.0,
    )
    monkeypatch.setattr(argus_bot.argus_control, "current_audit", lambda: {
        "folder": "Sprint 47",
        "target": "QA-C21462",
        "phase": "preparing",
        "requested_by": "alice",
        "n_keys": 0,
        "age_seconds": 42,
        "stale": False,
    })

    text = argus_bot._format_status()

    assert "current audit:" in text
    assert "preparing `Sprint 47`" in text
    assert "for `QA-C21462`" in text
    assert "requested by alice" in text
    assert "started 42s ago" in text

    monkeypatch.setattr(argus_bot.argus_control, "current_audit",
                        lambda: None)
    assert "current audit" not in argus_bot._format_status()
