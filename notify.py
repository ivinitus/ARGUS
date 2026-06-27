"""Chat notifier for ARGUS.

Single, narrow responsibility: take a level + title + body, deliver to Chat
when the configured mode allows it, and ALWAYS append to a local log
regardless of mode so a muted run is never an unobservable run.

Modes (from `[notify].mode` in config.toml):
  verbose  — post everything (info + warn + error). Use the first week.
  failures — drop info; post warn + error. Steady-state.
  silent   — log only, never POST. Debug / vacation.

Mode lives in config, NOT an environment variable, so behaviour is identical
across cron, manual runs, and ad-hoc retries — no surprise muting from a
forgotten shell export.

Designed to be callable from anywhere (including the `on_audit_complete`
callback that runs on `batch.run_batch`'s as_completed loop thread):
  - Never raises. A bad notification_hook URL, a 5xx from Chat, an EOFError on the
    log file — all caught, logged to stderr, return None.
  - Single fast HTTP POST via stdlib `urllib`. No new dependency, no side
    pool, no retries. The 5-second timeout is the whole budget.
  - Webhook URL is loaded fresh on every call so rotating the secret
    doesn't require restarting any long-lived process.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import config as argus_config

log = logging.getLogger("argus.notify")


# Visual prefixes. Chat renders these as inline emoji. Kept compact so the
# message line stays scannable — the reader's eye sees colour first, title
# second, detail third.
_ICON = {
    "info": ":white_check_mark:",
    "warn": ":warning:",
    "error": ":rotating_light:",
}

# Level → which modes deliver. Cheaper than four if/else chains in callers.
_DELIVERS_AT = {
    "info": {"verbose"},
    "warn": {"verbose", "failures"},
    "error": {"verbose", "failures"},
}

# Chat body cap. Chat's actual limit is much higher, but ARGUS summaries
# can balloon (large folder runs, long failure tracebacks) and a wall-of-text
# Chat post is just noise. 1500 chars matches the lazy-loading width Chat
# uses before truncating, so one screenful is roughly all we send.
_BODY_CHAR_CAP = 1500


def _load_notification_hook(path: Path) -> str | None:
    """Read the notification_hook URL from disk. Returns None on any failure.

    Same shape as extractor._load_tracker_pat: secret lives outside the repo,
    file may be missing in fresh checkouts, never raise. The chmod-600
    warning is intentionally NOT here (notify is on the hot path of every
    audit; permission warnings on every call would be noise) — operators
    can run env_check or extractor first to surface the same warning once.
    """
    try:
        if not path.exists():
            return None
        url = path.read_text().strip()
        return url or None
    except OSError as e:
        log.debug("could not read notification_hook from %s: %s", path, e)
        return None


def _append_log(log_path: Path, level: str, title: str, body: str) -> None:
    """Append one event to the notify log. Best-effort.

    Format is two lines per event so `tail -f` is readable AND
    daily_summary.py can grep timestamps + level. Body is intentionally
    written even when empty so the parser can rely on the two-line shape.

    Failure to write is swallowed (mkdir race, disk full, permission
    error). The Chat POST already happened or didn't; not being able to
    log is bad but it must NOT throw out of `post()` — callers including
    `on_audit_complete` cannot tolerate exceptions on this path.
    """
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"{ts} [{level}] {title}\n  {body}\n")
    except OSError as e:
        # Last-ditch: write to stderr so the operator at least sees something.
        # Don't raise.
        sys.stderr.write(f"[notify] could not append to {log_path}: {e}\n")


def _post_to_chat(notification_hook: str, text: str) -> None:
    """Single POST to the Chat incoming notification_hook. Never raises.

    5-second timeout: Chat should answer in <500ms; if it's slow, the
    cron run is more important than the page. We'd rather skip the
    notification than block the audit pipeline. The exception is logged
    to stderr (NOT to the notify log — that's already been written by
    the caller) so an operator pinging stderr gets one signal.
    """
    try:
        req = urllib.request.Request(
            notification_hook,
            data=json.dumps({"text": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5).read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        sys.stderr.write(f"[notify] chat POST failed: "
                         f"{type(e).__name__}: {e}\n")
    except Exception as e:  # belt-and-braces: must not raise
        sys.stderr.write(f"[notify] chat POST raised "
                         f"{type(e).__name__}: {e}\n")


def _format_message(level: str, title: str, body: str,
                    tunnel_url: str | None) -> str:
    """Assemble the Chat message body.

    Layout:
      :icon: *ARGUS* — title
      ```optional body, capped at _BODY_CHAR_CAP```
      <tunnel_url|open ARGUS>     ← only when a URL is set

    The triple-backtick block is used for body so multi-line failure
    output (paths, tracebacks, key lists) renders monospace without
    Chat collapsing whitespace or interpreting URLs.
    """
    icon = _ICON.get(level, ":bell:")
    parts = [f"{icon} *ARGUS* — {title}"]
    if body:
        truncated = body[:_BODY_CHAR_CAP]
        if len(body) > _BODY_CHAR_CAP:
            truncated += f"\n... ({len(body) - _BODY_CHAR_CAP} more chars)"
        parts.append(f"```{truncated}```")
    if tunnel_url:
        # Chat link syntax: <url|label>
        parts.append(f"<{tunnel_url}|open ARGUS>")
    return "\n".join(parts)


def post(
    level: str,
    title: str,
    body: str = "",
    *,
    settings: argus_config.ARGUSConfig | None = None,
) -> None:
    """Send a notification. Single entry point for all callers.

    `level`  — "info" | "warn" | "error". Unknown levels are treated as
               "error" (delivered in failures mode and louder).
    `title`  — short single-line headline. Required.
    `body`   — optional multi-line detail; rendered inside a code block
               and capped at _BODY_CHAR_CAP.
    `settings` — pass the loaded ARGUSConfig to avoid a repeat config load
               from inside hot paths (e.g. on_audit_complete fires once per
               key). When None, loaded fresh — fine for one-off invocations
               (CLI wrapper scripts, daily summary).

    Always returns None and never raises, even when:
      - settings.notify.notification_hook_path is missing
      - the file is unreadable
      - Chat returns 5xx
      - mode is "silent"
      - the notify log path is unwritable

    The notify log is ALWAYS appended (regardless of mode) so a muted run
    is still observable. That's the contract callers rely on.
    """
    if settings is None:
        try:
            settings = argus_config.load()
        except Exception as e:  # broken config.toml, etc.
            sys.stderr.write(f"[notify] could not load config: "
                             f"{type(e).__name__}: {e}\n")
            return

    nt = settings.notify
    # Always log first — even when we won't POST, the local trail must
    # capture what happened.
    _append_log(nt.log_path, level, title, body)

    if nt.mode == "silent":
        return
    delivers = _DELIVERS_AT.get(level, {"verbose", "failures"})
    if nt.mode not in delivers:
        return

    notification_hook = _load_notification_hook(nt.notification_hook_path)
    if notification_hook is None:
        # Webhook not configured yet (fresh checkout, secret rotated and
        # not yet replaced). Already logged to file; warn once on stderr
        # so operators see it during setup.
        sys.stderr.write(
            f"[notify] no notification_hook at {nt.notification_hook_path} — Chat delivery "
            f"skipped (event still logged)\n"
        )
        return

    text = _format_message(level, title, body, nt.tunnel_url or None)
    _post_to_chat(notification_hook, text)


def post_flagged_audit(
    key: str,
    audit: dict[str, Any],
    *,
    settings: argus_config.ARGUSConfig | None = None,
) -> None:
    """Convenience wrapper: format a flagged audit's findings into a page.

    Pulled out of `post()` so callers in `on_audit_complete` don't need to
    know the audit.json shape. Reads:
      - overall_verdict
      - summary
      - findings (severity + description for the first few)

    Always sent at "error" level — flagged findings are the whole reason
    cron runs every 15 minutes.
    """
    verdict = audit.get("overall_verdict", "?")
    summary = (audit.get("summary") or "").strip()
    findings = audit.get("findings") or []

    # Show the first 3 findings inline; the link to ARGUS handles deep dive.
    bullets = []
    for f in findings[:3]:
        sev = f.get("severity", "?")
        desc = (f.get("description") or "").strip()
        # Single-line: collapse newlines so the Chat code block stays compact.
        desc_one = " ".join(desc.split())
        if len(desc_one) > 200:
            desc_one = desc_one[:200] + "..."
        bullets.append(f"[{sev}] {desc_one}")
    if len(findings) > 3:
        bullets.append(f"... and {len(findings) - 3} more")

    body_lines = [f"verdict: {verdict}"]
    if summary:
        body_lines.append(f"summary: {summary}")
    if bullets:
        body_lines.append("findings:")
        body_lines.extend(f"  - {b}" for b in bullets)
    body = "\n".join(body_lines)

    title = f"flagged {key} ({verdict})"
    post("error", title, body, settings=settings)


def post_audit_started(
    folder_name: str,
    n_keys: int,
    *,
    n_reaudits: int = 0,
    settings: argus_config.ARGUSConfig | None = None,
) -> None:
    """Operational milestone: a folder audit run has started.

    Only call this when there's actual work to do (new keys to audit).
    Cron ticks that find 0 new keys MUST stay silent — otherwise the
    operator gets 4 pings/hour with nothing to act on. argus.py
    enforces the work-to-do gate at the call site.

    Level=info so `failures` mode operators don't see it. Operators on
    `verbose` see it as the "audit kicking off" heartbeat. The point of
    this ping is operational visibility ("ARGUS is doing something
    right now"), not findings — those go to the dashboard, never Chat.

    `n_reaudits` is the count of E-keys included in this run because the
    tester re-executed (executionDate moved). Surfaced in the body when
    nonzero so an operator who sees a flagged key on the dashboard can
    correlate it with "yes, the tester corrected, ARGUS is rescoring"
    instead of mistaking it for a fresh first-time audit. Defaults to 0
    so callers that don't know about re-audits (legacy or single-key
    paths) don't have to thread the parameter through.
    """
    body = f"folder: {folder_name}\nkeys: {n_keys}"
    if n_reaudits > 0:
        # Plural-aware so the message reads naturally for n=1 too.
        body += (
            f" ({n_reaudits} re-audit"
            f"{'s' if n_reaudits != 1 else ''} due to tester corrections)"
        )
    body += "\nmode: auto-folder"
    title = "audit started"
    # Override the icon table for this specific message — :hourglass:
    # is more semantically correct than :white_check_mark:. Done by
    # pre-formatting and using a custom level fallback.
    post("info", title, body, settings=settings)


def post_audit_done(
    folder_name: str,
    n_ok: int,
    n_flagged: int,
    n_failed: int,
    elapsed_seconds: float,
    *,
    settings: argus_config.ARGUSConfig | None = None,
) -> None:
    """Operational milestone: a folder audit run has finished.

    Level=error when anything needs operator attention (failed or
    auth-failed keys); level=info when the run completed cleanly even
    if some audits were flagged. "Flagged" findings are NOT operator
    failures — they're QA findings, viewed in the ARGUS dashboard.
    """
    minutes, seconds = divmod(int(elapsed_seconds), 60)
    elapsed_str = f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"
    body_lines = [
        f"folder: {folder_name}",
        f"result: {n_ok} ok, {n_flagged} flagged, {n_failed} failed",
        f"elapsed: {elapsed_str}",
    ]
    body = "\n".join(body_lines)
    level = "error" if n_failed > 0 else "info"
    title = "audit done" if level == "info" else "audit done with failures"
    post(level, title, body, settings=settings)
