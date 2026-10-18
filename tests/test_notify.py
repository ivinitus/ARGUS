from __future__ import annotations

import json
import urllib.error

from argus import config as argus_config
from argus.core import secret_files
from argus.operations import daily_summary
from argus.operations import notify


def _settings(tmp_path):
    settings = argus_config.ArgusConfig()
    settings.general.state_dir = tmp_path
    settings.notify.mode = "verbose"
    settings.notify.webhook_path = tmp_path / "webhook"
    settings.notify.webhook_path.write_text("https://hooks.example.test/x")
    settings.notify.webhook_path.chmod(0o600)
    settings.notify.log_path = tmp_path / "notify.log"
    return settings


def _capture_slack(monkeypatch):
    """Monkeypatch urlopen; return the list of posted Slack `text` bodies."""
    posts = []

    class _Resp:
        def read(self):
            return b"ok"

    def fake_urlopen(req, timeout):
        posts.append(json.loads(req.data.decode("utf-8"))["text"])
        return _Resp()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    return posts


def _dev(key: str = "QA-E183671", **kw) -> notify.Deviation:
    fields = dict(
        verdict="fail",
        tester="Annamalai V",
        tester_status="Pass",
        testrun_name="E2E Flow GMA UK Desktop Firefox",
        testrun_key="QA-C17820",
        finding="Device family mismatch — iOS test on Android",
    )
    fields.update(kw)
    return notify.Deviation(key=key, **fields)


def test_slack_delivery_failure_is_logged_and_swallowed(
        tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path)

    def fail_urlopen(req, timeout):
        raise urllib.error.URLError("slack down")

    monkeypatch.setattr(notify.urllib.request, "urlopen", fail_urlopen)

    notify.post("error", "audit done with failures", "details",
                settings=settings)

    err = capsys.readouterr().err
    assert "slack POST failed" in err
    text = settings.notify.log_path.read_text()
    assert "[error] audit done with failures" in text
    assert "[warn] slack delivery failed" in text
    assert "slack down" in text


def test_successful_slack_delivery_does_not_add_failure_event(
        tmp_path, monkeypatch):
    settings = _settings(tmp_path)

    class _Resp:
        def read(self):
            return b"ok"

    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda req, timeout: _Resp())

    notify.post("warn", "credential unavailable", "details", settings=settings)

    text = settings.notify.log_path.read_text()
    assert "[warn] credential unavailable" in text
    assert "slack delivery failed" not in text


def test_post_deviations_renders_auditor_facing_message(
        tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.notify.report_url = "https://tun.example.test"
    posts = _capture_slack(monkeypatch)

    notify.post_deviations(
        "Targeted_regression_June",
        [_dev(
            findings=(
                "Device family mismatch — iOS test on Android",
                "Production URL detected",
            ),
        ),
         _dev("QA-E183702", tester="J. Doe",
              testrun_name="E2E Flow GMA DE Mobile Chrome",
              testrun_key="QA-C17831",
              finding="Screenshot evidence missing for checkout step")],
        settings=settings)

    assert len(posts) == 1
    text = posts[0]
    assert text.startswith(
        ":warning: *ARGUS* — 2 new deviations in Targeted regression June\n")
    first_entry = next(
        line for line in text.splitlines()
        if "QA-E183671" in line
    )
    assert (
        "- <https://tun.example.test?q=QA-E183671"
        "|QA-E183671> | *Annamalai V* | "
        "E2E Flow GMA UK Desktop Firefox (QA-C17820) | "
        "marked *Pass* -> Device family mismatch — iOS test on Android; "
        "Production URL detected"
    ) == first_entry
    assert sum(line.startswith("- ") for line in text.splitlines()) == 2
    assert "```" not in text
    assert text.endswith("<https://tun.example.test|open ARGUS>")

    log = settings.notify.log_path.read_text()
    assert "[warn] 2 new deviations in Targeted regression June" in log
    assert "- QA-E183671 [Deviation] — Annamalai V" in log
    assert (
        "marked Pass; Device family mismatch — iOS test on Android; "
        "Production URL detected"
    ) in log
    assert "<https://tun.example.test" not in log


def test_post_deviations_fallbacks_for_missing_fields(
        tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    posts = _capture_slack(monkeypatch)

    notify.post_deviations(
        "F",
        [_dev(tester=None, tester_status=None, testrun_name=None,
              testrun_key=None, finding=None)],
        settings=settings)

    text = posts[0]
    assert "- QA-E183671 | *?* | ? (?) | audit verdict: Deviation" in text
    assert "marked" not in text
    assert "<" not in text


def test_post_deviations_includes_every_entry(
        tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    posts = _capture_slack(monkeypatch)

    devs = [_dev(f"E{i}", tester="T", tester_status=None,
                 testrun_name="R", testrun_key="K", finding="f")
            for i in range(12)]
    notify.post_deviations("F", devs, settings=settings)

    text = posts[0]
    assert sum(line.startswith("- ") for line in text.splitlines()) == 12
    for index in range(12):
        assert f"- E{index} |" in text
    assert "more" not in text
    log = settings.notify.log_path.read_text()
    for index in range(12):
        assert f"- E{index} [Deviation]" in log


def test_post_deviations_chunks_without_omitting_entries(
        tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    posts = _capture_slack(monkeypatch)
    monkeypatch.setattr(notify, "_DEVIATION_CHAR_BUDGET", 90)
    devs = [
        _dev(
            f"E{index}",
            tester="T",
            tester_status=None,
            testrun_name="R",
            testrun_key="K",
            finding=f"finding {index}",
        )
        for index in range(6)
    ]

    notify.post_deviations("F", devs, settings=settings)

    assert len(posts) > 1
    combined = "\n".join(posts)
    for index in range(6):
        assert combined.count(f"- E{index} |") == 1
    assert "6 new deviations in F (1/" in posts[0]
    assert "deviations in F (2/" in posts[1]


def test_post_deviations_warn_delivers_in_failures_mode(
        tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.notify.mode = "failures"
    posts = _capture_slack(monkeypatch)

    notify.post_deviations("F", [_dev()], settings=settings)

    assert len(posts) == 1
    assert "1 new deviation in F" in posts[0]


def test_post_deviations_silent_mode_logs_only(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.notify.mode = "silent"
    posts = _capture_slack(monkeypatch)

    notify.post_deviations("F", [_dev()], settings=settings)

    assert posts == []
    assert "1 new deviation in F" in settings.notify.log_path.read_text()


def test_post_deviations_empty_list_is_a_no_op(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    posts = _capture_slack(monkeypatch)

    notify.post_deviations("F", [], settings=settings)

    assert posts == []
    assert not settings.notify.log_path.exists()


def _capture_slack_with_urls(monkeypatch):
    """Like _capture_slack but keeps (webhook_url, text) pairs."""
    posts = []

    class _Resp:
        def read(self):
            return b"ok"

    def fake_urlopen(req, timeout):
        posts.append((req.full_url, json.loads(req.data.decode())["text"]))
        return _Resp()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    return posts


def test_owner_audience_routes_to_owner_webhook(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.notify.owner_webhook_path = tmp_path / "webhook_owner"
    settings.notify.owner_webhook_path.write_text(
        "https://hooks.example.test/owner")
    settings.notify.owner_webhook_path.chmod(0o600)
    posts = _capture_slack_with_urls(monkeypatch)

    notify.post("error", "credential unavailable", "details", settings=settings)

    assert posts == [("https://hooks.example.test/owner",
                      posts[0][1])]
    assert "credential unavailable" in posts[0][1]


def test_owner_audience_falls_back_to_channel_webhook(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.notify.owner_webhook_path = tmp_path / "webhook_owner_missing"
    posts = _capture_slack_with_urls(monkeypatch)

    notify.post("error", "credential unavailable", "details", settings=settings)

    assert [url for url, _ in posts] == ["https://hooks.example.test/x"]


def _http_error(url: str, status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, status, "rejected", {}, None)


def _routing_settings(tmp_path):
    """Settings with BOTH webhooks present and distinguishable."""
    settings = _settings(tmp_path)
    settings.notify.owner_webhook_path = tmp_path / "webhook_owner"
    settings.notify.owner_webhook_path.write_text(
        "https://hooks.example.test/owner")
    settings.notify.owner_webhook_path.chmod(0o600)
    return settings


def _capture_with_statuses(monkeypatch, statuses: dict[str, int]):
    """urlopen fake that rejects specific URLs with a given HTTP status."""
    posts = []

    class _Resp:
        def read(self):
            return b"ok"

    def fake_urlopen(req, timeout):
        posts.append((req.full_url, json.loads(req.data.decode())["text"]))
        status = statuses.get(req.full_url)
        if status is not None:
            raise _http_error(req.full_url, status)
        return _Resp()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    return posts


def test_revoked_owner_webhook_falls_back_and_escalates(
        tmp_path, monkeypatch):
    settings = _routing_settings(tmp_path)
    posts = _capture_with_statuses(
        monkeypatch, {"https://hooks.example.test/owner": 404})

    notify.post("error", "credential unavailable", "details", settings=settings)

    assert [url for url, _ in posts] == [
        "https://hooks.example.test/owner",
        "https://hooks.example.test/x",
    ]
    assert "could not reach the owner webhook" in posts[1][1]
    assert "HTTP 404" in posts[1][1]
    assert "credential unavailable" in posts[1][1]
    log = settings.notify.log_path.read_text()
    assert "slack webhook rejected (HTTP 404)" in log


def test_transient_owner_failure_does_not_fall_back(tmp_path, monkeypatch):
    """A 5xx leaves delivery ambiguous — retrying elsewhere could double-page."""
    settings = _routing_settings(tmp_path)
    posts = _capture_with_statuses(
        monkeypatch, {"https://hooks.example.test/owner": 503})

    notify.post("error", "credential unavailable", "details", settings=settings)

    assert [url for url, _ in posts] == ["https://hooks.example.test/owner"]
    log = settings.notify.log_path.read_text()
    assert "slack delivery failed" in log
    assert "slack webhook rejected" not in log


def test_all_webhooks_rejected_is_loud(tmp_path, monkeypatch, capsys):
    """Nothing reached Slack: log + stderr + events must all say so."""
    settings = _routing_settings(tmp_path)
    posts = _capture_with_statuses(monkeypatch, {
        "https://hooks.example.test/owner": 404,
        "https://hooks.example.test/x": 404,
    })
    alerts = []
    monkeypatch.setattr(
        notify.events, "emit_notification",
        lambda level, title, body, *, settings: alerts.append((level, title)))

    notify.post("error", "credential unavailable", "details", settings=settings)

    assert len(posts) == 2
    log = settings.notify.log_path.read_text()
    assert "slack delivery failed on all webhooks" in log
    assert "slack delivery failed on all webhooks" in capsys.readouterr().err
    assert ("error", "slack delivery failed on all webhooks") in alerts


def test_identical_webhooks_post_once(tmp_path, monkeypatch):
    """Both files pointing at one hook must not double-post."""
    settings = _settings(tmp_path)
    settings.notify.owner_webhook_path = tmp_path / "webhook_owner"
    settings.notify.owner_webhook_path.write_text(
        "https://hooks.example.test/x")
    posts = _capture_slack_with_urls(monkeypatch)

    notify.post("error", "credential unavailable", "details", settings=settings)

    assert [url for url, _ in posts] == ["https://hooks.example.test/x"]


def test_channel_audience_never_falls_back_to_owner_dm(
        tmp_path, monkeypatch):
    """Auditor content must not be silently re-routed into the owner's DM."""
    settings = _routing_settings(tmp_path)
    posts = _capture_with_statuses(
        monkeypatch, {"https://hooks.example.test/x": 404})

    notify.post_deviations("F", [_dev()], settings=settings)

    assert [url for url, _ in posts] == ["https://hooks.example.test/x"]


def test_deviations_route_to_channel_webhook(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.notify.owner_webhook_path = tmp_path / "webhook_owner"
    settings.notify.owner_webhook_path.write_text(
        "https://hooks.example.test/owner")
    settings.notify.owner_webhook_path.chmod(0o600)
    posts = _capture_slack_with_urls(monkeypatch)

    notify.post_deviations("F", [_dev()], settings=settings)

    assert [url for url, _ in posts] == ["https://hooks.example.test/x"]






def test_load_webhook_refuses_world_readable_file(tmp_path, capsys):
    secret_files.reset_warned_cache()
    path = tmp_path / "webhook"
    path.write_text("https://hooks.example.test/x")
    path.chmod(0o644)

    assert notify._load_webhook(path) is None
    err = capsys.readouterr().err
    # The configured logger can also write the warning to stderr.
    assert err.count("[ARGUS] ALERT:") == 1
    assert "chmod 600" in err
    assert str(path) in err
    assert "mode=644" in err
    assert "ignoring" in err

    assert notify._load_webhook(path) is None
    assert "chmod 600" not in capsys.readouterr().err


def test_load_webhook_silent_for_mode_600(tmp_path, capsys):
    secret_files.reset_warned_cache()
    path = tmp_path / "webhook"
    path.write_text("https://hooks.example.test/x")
    path.chmod(0o600)

    assert notify._load_webhook(path) == "https://hooks.example.test/x"
    assert "chmod 600" not in capsys.readouterr().err


def test_warn_if_world_readable_return_value_and_missing_file(tmp_path):
    secret_files.reset_warned_cache()
    open_file = tmp_path / "open"
    open_file.write_text("x")
    open_file.chmod(0o644)
    tight = tmp_path / "tight"
    tight.write_text("x")
    tight.chmod(0o600)

    assert secret_files.warn_if_world_readable(open_file) is True
    assert secret_files.warn_if_world_readable(open_file) is True
    assert secret_files.warn_if_world_readable(tight) is False
    assert secret_files.warn_if_world_readable(tmp_path / "absent") is False


def test_group_writable_only_file_still_warns(tmp_path, capsys):
    secret_files.reset_warned_cache()
    path = tmp_path / "webhook"
    path.write_text("https://hooks.example.test/x")
    path.chmod(0o620)

    assert secret_files.warn_if_world_readable(path) is True
    assert "chmod 600" in capsys.readouterr().err


def test_daily_summary_counts_new_and_legacy_deviation_titles():
    stats = daily_summary._summarise([
        {"level": "warn", "title": "2 new deviations in F"},
        {"level": "info", "title": "new flagged keys — F"},
        {"level": "info", "title": "audit done"},
    ])
    assert stats["new_flagged_batches"] == 2


def test_post_deviations_escapes_slack_control_sequences(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    posts = _capture_slack(monkeypatch)

    notify.post_deviations(
        "Folder",
        [_dev(tester="<!channel> Eve",
              testrun_name="Run <https://evil.example|click me>",
              finding="see <@U0123456> & friends")],
        settings=settings)

    text = posts[0]
    assert "<!channel>" not in text
    assert "&lt;!channel&gt; Eve" in text
    assert "<https://evil.example|click me>" not in text
    assert "<@U0123456>" not in text
    assert "&amp; friends" in text
