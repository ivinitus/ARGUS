"""Lightweight health monitor for the local ARGUS server."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from argus import config as argus_config
from argus.operations import notify
from argus import runtime


DEFAULT_PORT = 2405
DEFAULT_TIMEOUT_SECONDS = 3.0
DEFAULT_MAX_CRON_AGE_MINUTES = 45


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    level: str
    fail_title: str
    recover_title: str
    detail: str


def _default_cron_log() -> Path:
    return Path.home() / ".argus" / "cron.log"


def _default_state_path() -> Path:
    return Path.home() / ".argus" / "monitor_state.json"


def _load_state(path: Path) -> dict[str, Any]:
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(path: Path, checks: list[CheckResult]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        data = {
            "updated_at": now,
            "checks": {
                c.name: {
                    "ok": c.ok,
                    "detail": c.detail,
                    "updated_at": now,
                }
                for c in checks
            },
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        tmp.replace(path)
    except OSError as e:
        sys.stderr.write(f"[monitor_argus] could not write {path}: {e}\n")


def _health_url_ok(url: str, timeout_seconds: float) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as resp:
            status = getattr(resp, "status", resp.getcode())
            body = resp.read(4096)
    except urllib.error.HTTPError as e:
        return False, f"{url} returned HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, f"{url} failed: {e.reason}"
    except TimeoutError:
        return False, f"{url} timed out after {timeout_seconds:.1f}s"
    except Exception as e:
        return False, f"{url} raised {type(e).__name__}: {e}"

    if status != 200:
        return False, f"{url} returned HTTP {status}"
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False, f"{url} returned non-JSON health response"
    if payload.get("ok") is not True:
        return False, f"{url} returned unhealthy payload: {payload!r}"
    return True, f"{url} healthy"


def _cron_freshness_ok(
    cron_log: Path,
    max_age_seconds: int,
    *,
    now: float | None = None,
) -> tuple[bool, str]:
    try:
        stat = cron_log.stat()
    except OSError as e:
        return False, f"{cron_log} unavailable: {type(e).__name__}: {e}"

    now_ts = dt.datetime.now().timestamp() if now is None else now
    age_seconds = max(0, int(now_ts - stat.st_mtime))
    if age_seconds > max_age_seconds:
        age_min = age_seconds // 60
        max_min = max_age_seconds // 60
        return False, f"{cron_log} is stale: {age_min}m old (threshold {max_min}m)"
    return True, f"{cron_log} fresh: {age_seconds // 60}m old"


def collect_checks(
    settings: argus_config.ArgusConfig,
    *,
    port: int = DEFAULT_PORT,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    cron_log: Path | None = None,
    max_cron_age_seconds: int = DEFAULT_MAX_CRON_AGE_MINUTES * 60,
) -> list[CheckResult]:
    checks: list[CheckResult] = []

    local_url = f"http://127.0.0.1:{port}/healthz"
    local_ok, local_detail = _health_url_ok(local_url, timeout_seconds)
    checks.append(CheckResult(
        name="local_argus",
        ok=local_ok,
        level="error",
        fail_title="argus local health failed",
        recover_title="argus local health recovered",
        detail=local_detail,
    ))

    cron_ok, cron_detail = _cron_freshness_ok(
        cron_log or _default_cron_log(), max_cron_age_seconds)
    checks.append(CheckResult(
        name="cron_freshness",
        ok=cron_ok,
        level="warn",
        fail_title="argus cron stale",
        recover_title="argus cron recovered",
        detail=cron_detail,
    ))

    report_url = (settings.notify.report_url or "").strip()
    if report_url:
        health_url = report_url.rstrip("/") + "/healthz"
        report_ok, report_detail = _health_url_ok(health_url, timeout_seconds)
        checks.append(CheckResult(
            name="report_argus",
            ok=report_ok,
            level="warn",
            fail_title="argus report health failed",
            recover_title="argus report health recovered",
            detail=report_detail,
        ))

    return checks


def post_state_changes(
    checks: list[CheckResult],
    *,
    settings: argus_config.ArgusConfig,
    state_path: Path | None = None,
) -> None:
    path = state_path or _default_state_path()
    prior = _load_state(path).get("checks", {})
    if not isinstance(prior, dict):
        prior = {}

    for check in checks:
        old = prior.get(check.name, {})
        old_ok = old.get("ok") if isinstance(old, dict) else None
        if not check.ok and old_ok is not False:
            notify.post(check.level, check.fail_title, check.detail,
                        settings=settings)
        elif check.ok and old_ok is False:
            notify.post("info", check.recover_title, check.detail,
                        settings=settings)

    _save_state(path, checks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check local ARGUS health and post state-change alerts.")
    parser.add_argument("--config", type=Path, default=None,
                        help="path to config.toml (default: ./config.toml)")
    parser.add_argument("--port", type=int, default=None,
                        help="local ARGUS port (default: profile server.port)")
    parser.add_argument("--timeout-seconds", type=float,
                        default=DEFAULT_TIMEOUT_SECONDS,
                        help="health check timeout (default 3.0)")
    parser.add_argument("--cron-log", type=Path, default=None,
                        help="cron log path (default ~/.argus/cron.log)")
    parser.add_argument("--max-cron-age-minutes", type=int,
                        default=DEFAULT_MAX_CRON_AGE_MINUTES,
                        help="cron stale threshold (default 45)")
    parser.add_argument("--state-path", type=Path, default=None,
                        help="state file path (default ~/.argus/monitor_state.json)")
    args = parser.parse_args(argv)

    settings = argus_config.load(args.config)
    runtime.configure(settings)
    checks = collect_checks(
        settings,
        port=args.port if args.port is not None else settings.server.port,
        timeout_seconds=args.timeout_seconds,
        cron_log=args.cron_log,
        max_cron_age_seconds=args.max_cron_age_minutes * 60,
    )
    post_state_changes(checks, settings=settings, state_path=args.state_path)
    return 0 if all(c.ok for c in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
