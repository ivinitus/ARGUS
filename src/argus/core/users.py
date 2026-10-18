"""Persistent Jira user-key resolution."""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("argus.users")

_CACHE_PATH = Path.home() / ".argus" / "users.json"
_CACHE_SCHEMA = 1

_RETRY_BACKOFF_SECONDS = [60, 300, 1800, 7200, 86400]


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def _parse_iso(s: str) -> _dt.datetime | None:
    if not s:
        return None
    try:
        s = s[:-1] + "+00:00" if s.endswith("Z") else s
        return _dt.datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


@dataclass
class UserCache:
    """In-memory + on-disk cache of Jira user-key → displayName resolutions."""
    users: dict[str, dict[str, Any]] = field(default_factory=dict)
    failures: dict[str, dict[str, Any]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock,
                                  init=False, repr=False)

    @classmethod
    def load(cls) -> "UserCache":
        """Load from disk; return empty cache on any failure."""
        if not _CACHE_PATH.exists():
            return cls()
        try:
            raw = json.loads(_CACHE_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            return cls()
        if raw.get("schema") != _CACHE_SCHEMA:
            return cls()
        return cls(
            users=dict(raw.get("users") or {}),
            failures=dict(raw.get("failures") or {}),
        )

    def save(self) -> bool:
        """Atomic write to ~/.argus/users.json. Never raises."""
        try:
            _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                payload = {
                    "schema": _CACHE_SCHEMA,
                    "saved_at": _now_iso(),
                    "users": dict(self.users),
                    "failures": dict(self.failures),
                }
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8",
                dir=str(_CACHE_PATH.parent), delete=False,
                prefix=f".{_CACHE_PATH.name}.", suffix=".tmp",
            ) as tmp:
                json.dump(payload, tmp, indent=2, sort_keys=True)
                tmp_path = Path(tmp.name)
            os.replace(tmp_path, _CACHE_PATH)
            return True
        except Exception as e:
            sys.stderr.write(f"[users] save failed: "
                             f"{type(e).__name__}: {e}\n")
            return False

    def is_resolved(self, user_key: str) -> bool:
        with self._lock:
            return user_key in self.users

    def get_resolved(self, user_key: str) -> str | None:
        """Return the cached displayName if resolved, else None."""
        with self._lock:
            entry = self.users.get(user_key)
            if entry:
                return entry.get("displayName")
            return None

    def _should_retry(self, user_key: str) -> bool:
        """Decide whether enough time has passed since the last failure to warrant another Jira call."""
        with self._lock:
            entry = self.failures.get(user_key)
        if not entry:
            return True
        attempts = int(entry.get("attempts", 0))
        last = _parse_iso(entry.get("last_attempt", ""))
        if last is None:
            return True
        idx = min(attempts - 1, len(_RETRY_BACKOFF_SECONDS) - 1)
        gap = _RETRY_BACKOFF_SECONDS[max(0, idx)]
        elapsed = (_dt.datetime.now(_dt.timezone.utc) - last).total_seconds()
        return elapsed >= gap

    def _record_success(self, user_key: str, display_name: str) -> None:
        with self._lock:
            self.users[user_key] = {
                "displayName": display_name,
                "resolved_at": _now_iso(),
                "source": "rest/api/2/user?key",
            }
            self.failures.pop(user_key, None)

    def _record_failure(self, user_key: str, reason: str) -> None:
        with self._lock:
            entry = self.failures.get(user_key) or {"attempts": 0}
            entry["attempts"] = int(entry.get("attempts", 0)) + 1
            entry["last_attempt"] = _now_iso()
            entry["last_error"] = reason
            self.failures[user_key] = entry

    def resolve(self, session, base_url: str,
                user_key: str) -> str | None:
        """Return the displayName for user_key, hitting Jira only if we don't already have it cached AND the backoff allows."""
        if not user_key:
            return None

        cached = self.get_resolved(user_key)
        if cached is not None:
            return cached

        if not self._should_retry(user_key):
            return None

        from argus.pipeline.extractor import _get_with_retry
        try:
            url = f"{base_url}/rest/api/2/user"
            resp = _get_with_retry(
                session, url,
                params={"key": user_key},
                timeout=15,
                max_attempts=6,
                context=f"user lookup {user_key}",
            )
        except Exception as e:
            self._record_failure(user_key,
                                 f"{type(e).__name__}: {e}")
            log.warning("user lookup %s raised %s: %s",
                        user_key, type(e).__name__, e)
            return None

        if resp.status_code == 200:
            try:
                payload = resp.json()
            except ValueError as e:
                self._record_failure(user_key, f"non-JSON 200: {e}")
                log.warning("user lookup %s returned 200 with non-JSON",
                            user_key)
                return None
            display_name = payload.get("displayName")
            if not display_name or not isinstance(display_name, str):
                self._record_failure(
                    user_key, "200 OK but displayName missing")
                log.warning("user lookup %s returned 200 but no "
                            "displayName field", user_key)
                return None
            self._record_success(user_key, display_name)
            log.debug("resolved %s -> %s", user_key, display_name)
            return display_name

        self._record_failure(user_key, f"HTTP {resp.status_code}")
        log.warning("user lookup %s returned HTTP %s",
                    user_key, resp.status_code)
        return None

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "resolved": len(self.users),
                "unresolved": len(self.failures),
                "total_seen": len(self.users) + len(self.failures),
            }


_DEFAULT_CACHE: UserCache | None = None
_DEFAULT_LOCK = threading.Lock()


def configure(state_dir: Path) -> None:
    """Relocate the persistent user cache for the selected profile."""
    global _CACHE_PATH, _DEFAULT_CACHE
    new_path = state_dir.expanduser() / "users.json"
    with _DEFAULT_LOCK:
        if new_path != _CACHE_PATH:
            _CACHE_PATH = new_path
            _DEFAULT_CACHE = None


def get_default_cache() -> UserCache:
    """Return the process-wide default UserCache, loading it lazily."""
    global _DEFAULT_CACHE
    with _DEFAULT_LOCK:
        if _DEFAULT_CACHE is None:
            _DEFAULT_CACHE = UserCache.load()
        return _DEFAULT_CACHE


def reset_default_cache() -> None:
    """Force the next get_default_cache() call to re-load from disk."""
    global _DEFAULT_CACHE
    with _DEFAULT_LOCK:
        _DEFAULT_CACHE = None


def main(argv: list[str] | None = None) -> int:
    import argparse
    from argus import config as argus_config, runtime
    parser = argparse.ArgumentParser(
        description="Inspect the persistent Jira user cache.")
    parser.add_argument("--config", type=Path, default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stats", help="Show cache size + counts.")
    p_show = sub.add_parser("show", help="Print a user_key's cached entry.")
    p_show.add_argument("user_key")
    p_clear = sub.add_parser("clear-failures",
                             help="Drop all cached failures (forces retry "
                                  "next time each key is seen).")
    p_clear.set_defaults(cmd="clear-failures")
    args = parser.parse_args(argv)
    runtime.configure(argus_config.load(args.config))

    cache = UserCache.load()
    if args.cmd == "stats":
        s = cache.stats()
        print(f"resolved:    {s['resolved']}")
        print(f"unresolved:  {s['unresolved']}")
        print(f"total seen:  {s['total_seen']}")
        return 0
    if args.cmd == "show":
        if args.user_key in cache.users:
            print("status: resolved")
            print(json.dumps(cache.users[args.user_key], indent=2))
        elif args.user_key in cache.failures:
            print("status: failed")
            print(json.dumps(cache.failures[args.user_key], indent=2))
        else:
            print(f"{args.user_key}: not in cache")
        return 0
    if args.cmd == "clear-failures":
        n = len(cache.failures)
        cache.failures.clear()
        cache.save()
        print(f"cleared {n} failure entries")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
