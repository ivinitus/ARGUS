"""Unit tests for users.py — persistent Jira user cache."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from argus.core import users


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    """Point the module's cache file at a temp location for every test."""
    monkeypatch.setattr(users, "_CACHE_PATH", tmp_path / "users.json")
    users.reset_default_cache()


def _stub_session(*, status: int = 200,
                  display_name: str | None = "Test User",
                  raise_exc: Exception | None = None) -> MagicMock:
    """Build a minimal session-like mock that returns a controlled response."""
    sess = MagicMock()
    if raise_exc is not None:
        sess.get.side_effect = raise_exc
        return sess
    resp = MagicMock()
    resp.status_code = status
    if display_name is not None:
        resp.json.return_value = {"displayName": display_name,
                                   "name": "user"}
    else:
        resp.json.return_value = {}
    sess.get.return_value = resp
    return sess


def test_empty_cache_loads_clean():
    cache = users.UserCache.load()
    assert cache.users == {}
    assert cache.failures == {}


def test_save_and_reload_round_trip():
    cache = users.UserCache()
    cache._record_success("JIRAUSER1", "Alice")
    cache._record_failure("JIRAUSER2", "HTTP 503")
    assert cache.save() is True

    reloaded = users.UserCache.load()
    assert reloaded.get_resolved("JIRAUSER1") == "Alice"
    assert reloaded.is_resolved("JIRAUSER1") is True
    assert reloaded.is_resolved("JIRAUSER2") is False
    assert "HTTP 503" in reloaded.failures["JIRAUSER2"]["last_error"]


def test_save_never_raises_under_concurrent_mutation():
    import threading
    import time

    cache = users.UserCache()
    start = threading.Barrier(2)
    errors: list[BaseException] = []

    def writer():
        start.wait()
        for i in range(1000):
            cache._record_success(f"JIRAUSER{i}", f"User {i}")
            if i % 25 == 0:
                time.sleep(0)

    def saver():
        try:
            start.wait()
            for _ in range(50):
                cache.save()
        except BaseException as e:
            errors.append(e)

    w = threading.Thread(target=writer)
    s = threading.Thread(target=saver)
    w.start(); s.start()
    s.join(timeout=10)
    w.join(timeout=10)

    assert not s.is_alive(), "save() stress thread hung"
    assert not w.is_alive(), "writer stress thread hung"
    assert errors == [], f"save() raised under concurrency: {errors}"


def test_load_returns_empty_on_corrupt_file(tmp_path, monkeypatch):
    p = tmp_path / "users.json"
    p.write_text("{not valid json")
    monkeypatch.setattr(users, "_CACHE_PATH", p)
    cache = users.UserCache.load()
    assert cache.users == {}


def test_load_returns_empty_on_wrong_schema(tmp_path, monkeypatch):
    p = tmp_path / "users.json"
    p.write_text(json.dumps({"schema": 999, "users": {"X": "Y"}}))
    monkeypatch.setattr(users, "_CACHE_PATH", p)
    cache = users.UserCache.load()
    assert cache.users == {}


def test_resolve_caches_success_and_returns_immediately_next_call():
    cache = users.UserCache()
    sess = _stub_session(display_name="Alice")
    name = cache.resolve(sess, "https://jira", "JIRAUSER1")
    assert name == "Alice"
    assert sess.get.call_count == 1

    name_again = cache.resolve(sess, "https://jira", "JIRAUSER1")
    assert name_again == "Alice"
    assert sess.get.call_count == 1


def test_resolve_returns_none_for_empty_user_key():
    cache = users.UserCache()
    sess = _stub_session()
    assert cache.resolve(sess, "https://jira", "") is None
    assert sess.get.call_count == 0


def test_resolve_records_failure_on_503():
    cache = users.UserCache()
    sess = _stub_session(status=503)
    name = cache.resolve(sess, "https://jira", "JIRAUSER1")
    assert name is None
    assert "JIRAUSER1" in cache.failures
    assert cache.failures["JIRAUSER1"]["attempts"] == 1
    assert "503" in cache.failures["JIRAUSER1"]["last_error"]


def test_resolve_records_failure_on_network_exception():
    cache = users.UserCache()
    sess = _stub_session(raise_exc=ConnectionError("boom"))
    name = cache.resolve(sess, "https://jira", "JIRAUSER1")
    assert name is None
    assert "JIRAUSER1" in cache.failures
    assert "ConnectionError" in cache.failures["JIRAUSER1"]["last_error"]


def test_resolve_records_failure_on_200_without_displayname():
    cache = users.UserCache()
    sess = _stub_session(display_name=None)
    name = cache.resolve(sess, "https://jira", "JIRAUSER1")
    assert name is None
    assert "JIRAUSER1" in cache.failures


def test_failure_then_success_promotes_to_users():
    cache = users.UserCache()
    sess_fail = _stub_session(status=503)
    cache.resolve(sess_fail, "https://jira", "JIRAUSER1")
    assert "JIRAUSER1" in cache.failures

    cache.failures["JIRAUSER1"]["last_attempt"] = "2000-01-01T00:00:00Z"

    sess_ok = _stub_session(display_name="Alice")
    name = cache.resolve(sess_ok, "https://jira", "JIRAUSER1")
    assert name == "Alice"
    assert "JIRAUSER1" in cache.users
    assert "JIRAUSER1" not in cache.failures


def test_backoff_blocks_immediate_retry():
    cache = users.UserCache()
    sess = _stub_session(status=503)
    cache.resolve(sess, "https://jira", "JIRAUSER1")
    first_call_count = sess.get.call_count
    assert first_call_count >= 1

    cache.resolve(sess, "https://jira", "JIRAUSER1")
    assert sess.get.call_count == first_call_count, (
        "second resolve hit Jira despite backoff; cache poison "
        "protection failed"
    )


def test_should_retry_after_backoff_window():
    cache = users.UserCache()
    cache._record_failure("JIRAUSER1", "HTTP 503")
    cache.failures["JIRAUSER1"]["last_attempt"] = "2000-01-01T00:00:00Z"
    assert cache._should_retry("JIRAUSER1") is True


def test_get_default_cache_is_a_singleton():
    cache1 = users.get_default_cache()
    cache2 = users.get_default_cache()
    assert cache1 is cache2


def test_reset_default_cache_forces_reload():
    cache1 = users.get_default_cache()
    cache1._record_success("JIRAUSER1", "Alice")
    cache1.save()

    users.reset_default_cache()
    cache2 = users.get_default_cache()
    assert cache2 is not cache1
    assert cache2.get_resolved("JIRAUSER1") == "Alice"


def test_stats_counts_resolved_and_unresolved():
    cache = users.UserCache()
    cache._record_success("J1", "A")
    cache._record_success("J2", "B")
    cache._record_failure("J3", "x")
    s = cache.stats()
    assert s["resolved"] == 2
    assert s["unresolved"] == 1
    assert s["total_seen"] == 3
