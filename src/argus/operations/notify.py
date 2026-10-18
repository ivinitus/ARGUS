"""Slack notifier for ARGUS."""
from __future__ import annotations
import dataclasses
import datetime as _dt
import json
import logging
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from argus import config as argus_config
from argus.core import secret_files
from argus.operations import events
log = logging.getLogger('argus.notify')
_ICON = {'info': ':white_check_mark:', 'warn': ':warning:', 'error': ':rotating_light:'}
_DELIVERS_AT = {'info': {'verbose'}, 'warn': {'verbose', 'failures'}, 'error': {'verbose', 'failures'}}
_BODY_CHAR_CAP = 1500

def _load_webhook(path: Path) -> str | None:
    try:
        if not path.exists():
            return None
        if secret_files.refuse_if_world_accessible(path, 'Slack webhook file'):
            return None
        url = path.read_text().strip()
        return url or None
    except OSError as e:
        log.debug('could not read webhook from %s: %s', path, e)
        return None

def _append_log(log_path: Path, level: str, title: str, body: str) -> None:
    """Append one event to the notify log."""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        ts = _dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with log_path.open('a', encoding='utf-8') as f:
            f.write(f'{ts} [{level}] {title}\n  {body}\n')
    except OSError as e:
        sys.stderr.write(f'[notify] could not append to {log_path}: {e}\n')

def _record_notification(settings: argus_config.ArgusConfig, level: str, title: str, body: str) -> None:
    """Write one event to the local notification log."""
    _append_log(settings.notify.log_path, level, title, body)

@dataclasses.dataclass(frozen=True)
class _Delivery:
    """Outcome of one POST attempt."""
    detail: str | None = None
    status: int | None = None

    @property
    def ok(self) -> bool:
        return self.detail is None
_PERMANENT_REJECTIONS = frozenset({400, 403, 404, 410})

def _post_to_slack(webhook: str, text: str) -> _Delivery:
    """Single POST to the Slack incoming webhook."""
    try:
        req = urllib.request.Request(webhook, data=json.dumps({'text': text}).encode('utf-8'), headers={'Content-Type': 'application/json'}, method='POST')
        urllib.request.urlopen(req, timeout=5).read()
        return _Delivery()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        detail = f'{type(e).__name__}: {e}'
        status = e.code if isinstance(e, urllib.error.HTTPError) else None
        sys.stderr.write(f'[notify] slack POST failed: {detail}\n')
        return _Delivery(detail, status)
    except Exception as e:
        detail = f'{type(e).__name__}: {e}'
        sys.stderr.write(f'[notify] slack POST raised {detail}\n')
        return _Delivery(detail)

def _format_message(level: str, title: str, body: str, report_url: str | None, *, code_block: bool=True, body_char_cap: int=_BODY_CHAR_CAP) -> str:
    """Assemble the Slack message body."""
    icon = _ICON.get(level, ':bell:')
    parts = [f'{icon} *ARGUS* — {title}']
    if body:
        truncated = body[:body_char_cap]
        if len(body) > body_char_cap:
            truncated += f'\n... ({len(body) - body_char_cap} more chars)'
        if code_block:
            fence_safe = truncated.replace('```', "'''")
            parts.append(f'```{fence_safe}```')
        else:
            parts.append(truncated)
    if report_url:
        parts.append(f'<{report_url}|open ARGUS>')
    return '\n'.join(parts)

def _webhook_candidates(settings: argus_config.ArgusConfig, audience: str) -> list[tuple[str, str]]:
    """Ordered (label, url) webhooks to try for `audience`."""
    nt = settings.notify
    paths = [('owner', nt.owner_webhook_path), ('channel', nt.webhook_path)]
    if audience != 'owner':
        paths = [('channel', nt.webhook_path)]
    candidates = [(label, _load_webhook(path)) for (label, path) in paths]
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for (label, url) in candidates:
        if url is None or url in seen:
            continue
        seen.add(url)
        out.append((label, url))
    return out
_REVOKED_BANNER = ':rotating_light: *ARGUS could not reach the {label} webhook (HTTP {status} — revoked or invalid). Re-create it and write the new URL to {location}.* This message was re-routed here.'

def _deliver(nt, candidates: list[tuple[str, str]], text: str, *, settings: argus_config.ArgusConfig) -> None:
    """POST `text` to the first webhook that accepts it."""
    banner: str | None = None
    for (label, url) in candidates:
        body_text = f'{banner}\n{text}' if banner else text
        result = _post_to_slack(url, body_text)
        if result.ok:
            return
        if result.status in _PERMANENT_REJECTIONS:
            location = str(nt.owner_webhook_path if label == 'owner' else nt.webhook_path)
            _record_notification(settings, 'error', f'slack webhook rejected (HTTP {result.status})', f'audience={label} location={location} — treating as revoked; trying the next webhook')
            banner = _REVOKED_BANNER.format(label=label, status=result.status, location=location)
            continue
        _record_notification(settings, 'warn', 'slack delivery failed', result.detail or 'unknown error')
        return
    detail = f"tried {len(candidates)} webhook(s): {', '.join((label for (label, _) in candidates))}"
    title = 'slack delivery failed on all webhooks'
    _record_notification(settings, 'error', title, detail)
    sys.stderr.write(f'[notify] {title} — {detail}\n')
    try:
        events.emit_notification('error', title, detail, settings=settings)
    except Exception as e:
        sys.stderr.write(f'[notify] could not emit alert event: {type(e).__name__}: {e}\n')

def post(level: str, title: str, body: str='', *, settings: argus_config.ArgusConfig | None=None, slack_body: str | None=None, audience: str='owner', slack_body_char_cap: int=_BODY_CHAR_CAP) -> None:
    """Send a notification."""
    if settings is None:
        try:
            settings = argus_config.load()
        except Exception as e:
            sys.stderr.write(f'[notify] could not load config: {type(e).__name__}: {e}\n')
            return
    nt = settings.notify
    _record_notification(settings, level, title, body)
    events.emit_notification(level, title, body, settings=settings)
    if nt.mode == 'silent':
        return
    delivers = _DELIVERS_AT.get(level, {'verbose', 'failures'})
    if nt.mode not in delivers:
        return
    candidates = _webhook_candidates(settings, audience)
    if not candidates:
        location = str(nt.webhook_path)
        sys.stderr.write(f'[notify] no webhook in {location} — Slack delivery skipped (event still logged)\n')
        return
    text = _format_message(level, title, slack_body if slack_body is not None else body, nt.report_url or None, code_block=slack_body is None, body_char_cap=slack_body_char_cap)
    _deliver(nt, candidates, text, settings=settings)
_DEVIATION_CHAR_BUDGET = 35000

@dataclasses.dataclass
class Deviation:
    """One flagged execution, pre-digested for the auditor-facing post."""
    key: str
    verdict: str | None = None
    tester: str | None = None
    tester_status: str | None = None
    testrun_name: str | None = None
    testrun_key: str | None = None
    finding: str | None = None
    findings: tuple[str, ...] = ()

def _deviation_findings(d: Deviation) -> tuple[str, ...]:
    from argus.reporting.verdicts import verdict_label
    if d.findings:
        return d.findings
    if d.finding:
        return (d.finding,)
    return (f'audit verdict: {verdict_label(d.verdict)}',)

def _mrkdwn_escape(text: object) -> str:
    """Escape Slack mrkdwn control characters in Jira/model-derived text.

    Without this, `<!channel>`, `<@U...>` or `<url|label>` typed into a
    tester comment (or emitted by the model) would render live in Slack.
    """
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def _deviation_entry(d: Deviation, report_url: str) -> tuple[str, str]:
    """Render one deviation as (log_line, slack_entry)."""
    from argus.reporting.verdicts import verdict_label
    tester = d.tester or '?'
    run_name = d.testrun_name or '?'
    run_key = d.testrun_key or '?'
    findings = '; '.join(_deviation_findings(d))
    esc = _mrkdwn_escape
    marked = f'marked *{esc(d.tester_status)}* -> ' if d.tester_status else ''
    if report_url:
        sep = '&' if '?' in report_url else '?'
        key = f'<{report_url}{sep}q={urllib.parse.quote(d.key)}|{d.key}>'
    else:
        key = d.key
    slack_entry = f'- {key} | *{esc(tester)}* | {esc(run_name)} ({esc(run_key)}) | {marked}{esc(findings)}'
    marked_log = f'marked {d.tester_status}; ' if d.tester_status else ''
    log_line = f'- {d.key} [{verdict_label(d.verdict)}] — {tester} — {run_name} ({run_key}) — {marked_log}{findings}'
    return (log_line, slack_entry)

def _deviation_chunks(rendered: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    chunks: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    current_size = 0
    for entry in rendered:
        entry_size = len(entry[1]) + (1 if current else 0)
        if current and current_size + entry_size > _DEVIATION_CHAR_BUDGET:
            chunks.append(current)
            current = []
            current_size = 0
            entry_size = len(entry[1])
        current.append(entry)
        current_size += entry_size
    if current:
        chunks.append(current)
    return chunks

def post_deviations(folder_name: str, deviations: list[Deviation], *, settings: argus_config.ArgusConfig | None=None) -> None:
    """Post one batched, auditor-facing message for a run's new deviations."""
    if not deviations:
        return
    if settings is None:
        try:
            settings = argus_config.load()
        except Exception as e:
            sys.stderr.write(f'[notify] could not load config: {type(e).__name__}: {e}\n')
            return
    report_url = settings.notify.report_url or ''
    n = len(deviations)
    rendered = [_deviation_entry(d, report_url) for d in deviations]
    chunks = _deviation_chunks(rendered)
    base_title = f"{n} new deviation{('s' if n != 1 else '')} in {folder_name.replace('_', ' ')}"
    for (index, chunk) in enumerate(chunks, start=1):
        if len(chunks) == 1:
            title = base_title
        elif index == 1:
            title = f'{base_title} (1/{len(chunks)})'
        else:
            title = f"deviations in {folder_name.replace('_', ' ')} ({index}/{len(chunks)})"
        post('warn', title, '\n'.join((entry[0] for entry in chunk)), settings=settings, slack_body='\n'.join((entry[1] for entry in chunk)), audience='channel', slack_body_char_cap=_DEVIATION_CHAR_BUDGET)

def post_audit_started(folder_name: str, n_keys: int, *, n_reaudits: int=0, settings: argus_config.ArgusConfig | None=None) -> None:
    """Operational milestone: a folder audit run has started."""
    body = f'folder: {folder_name}\nkeys: {n_keys}'
    if n_reaudits > 0:
        body += f" ({n_reaudits} re-audit{('s' if n_reaudits != 1 else '')} due to tester corrections)"
    body += '\nmode: auto-folder'
    title = 'audit started'
    post('info', title, body, settings=settings)

def post_audit_done(folder_name: str, n_ok: int, n_flagged: int, n_failed: int, elapsed_seconds: float, *, settings: argus_config.ArgusConfig | None=None) -> None:
    """Operational milestone: a folder audit run has finished."""
    (minutes, seconds) = divmod(int(elapsed_seconds), 60)
    elapsed_str = f'{minutes}m {seconds:02d}s' if minutes else f'{seconds}s'
    body_lines = [f'folder: {folder_name}', f'result: {n_ok} ok, {n_flagged} flagged, {n_failed} failed', f'elapsed: {elapsed_str}']
    body = '\n'.join(body_lines)
    level = 'error' if n_failed > 0 else 'info'
    title = 'audit done' if level == 'info' else 'audit done with failures'
    post(level, title, body, settings=settings)

def main(argv: list[str] | None=None) -> int:
    """CLI entry point: `argus notify LEVEL TITLE [BODY]`."""
    import argparse
    parser = argparse.ArgumentParser(description='Post one ARGUS notify event (Slack + local log).')
    parser.add_argument('level', choices=['info', 'warn', 'error'])
    parser.add_argument('title')
    parser.add_argument('body', nargs='?', default='')
    parser.add_argument('--audience', choices=['owner', 'channel'], default='owner', help="owner (default): operator/debug traffic, delivered via the owner webhook (your DM). channel: the auditors' channel.")
    args = parser.parse_args(argv)
    post(args.level, args.title, args.body, audience=args.audience)
    return 0
if __name__ == '__main__':
    sys.exit(main())
