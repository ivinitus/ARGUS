"""ARGUS Slack bot — Socket Mode daemon for /argus slash commands."""
from __future__ import annotations
import datetime as _dt
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path
from typing import Any, TYPE_CHECKING, cast
if TYPE_CHECKING:
    from slack_sdk.socket_mode.client import BaseSocketModeClient
from argus.operations import control as argus_control
from argus.operations import audit_lock
from argus.operations.teams import resolve_team
from argus.operations.test_run_targets import JiraAuditTargetResolver, JiraReferenceError, parse_jira_reference, split_manual_references
from argus.core import atomic_io
from argus import config as argus_config
from argus.core import exec_paths
from argus.core import paths
from argus.core import secret_files
from argus import runtime
_E_KEY_RE = re.compile('^[A-Z][A-Z0-9_]*-E\\d+$')
try:
    from slack_sdk import WebClient
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.socket_mode.response import SocketModeResponse
except ImportError as e:
    sys.stderr.write(f'[argus_bot] slack-sdk not installed: {e}\n  install: uv sync --extra bot\n')
    sys.exit(1)
log = logging.getLogger('argus.argus_bot')
_HEARTBEAT_INTERVAL_S = 60
_JIRA_TARGET_RESOLVER: JiraAuditTargetResolver | None = None

def _load_token(path: Path, label: str) -> str | None:
    """Read a token file."""
    try:
        if not path.exists():
            return None
        if secret_files.refuse_if_world_accessible(path, f'{label} token file'):
            return None
        token = path.read_text().strip()
        return token or None
    except OSError as e:
        log.error('could not read %s token from %s: %s', label, path, e)
        return None

def _format_status() -> str:
    """Build the multi-line text body for /argus status."""
    s = argus_control.status()
    paused = s.get('paused')
    lines = []
    if paused:
        lines.append(':octagonal_sign: *PAUSED*')
        lines.append(f"  paused by: {s.get('paused_by') or 'unknown'}")
        lines.append(f"  paused at: {s.get('paused_at') or 'unknown'}")
        if s.get('reason'):
            lines.append(f"  reason: {s.get('reason')}")
    else:
        lines.append(':green_circle: *RUNNING*')
        if s.get('paused_by'):
            lines.append(f"  last toucher: {s.get('paused_by')}")
    hb_age = argus_control.bot_heartbeat_age_seconds()
    if hb_age is None:
        lines.append('  bot heartbeat: never')
    elif hb_age < 120:
        lines.append(f'  bot heartbeat: {int(hb_age)}s ago :white_check_mark:')
    elif hb_age < 600:
        lines.append(f'  bot heartbeat: {int(hb_age)}s ago :warning:')
    else:
        lines.append(f'  bot heartbeat: {int(hb_age)}s ago :rotating_light:')
    current = argus_control.current_audit()
    if current:
        marker = ':warning:' if current.get('stale') else ':hourglass_flowing_sand:'
        lines.append(f'  current audit: {marker} {_describe_current_audit(current)}')
    return '\n'.join(lines)

def _describe_current_audit(record: dict[str, Any]) -> str:
    """One-line Slack summary shared by `/argus status` and the pre-flight."""
    phase = str(record.get('phase') or 'auditing')
    folder = record.get('folder') or 'unknown'
    details: list[str] = []
    target = record.get('target')
    if target and target != folder:
        details.append(f'for `{target}`')
    if record.get('requested_by'):
        details.append(f"requested by {record['requested_by']}")
    n_keys = int(record.get('n_keys') or 0)
    if n_keys:
        details.append(f'{n_keys} keys')
    details.append(f"started {int(record.get('age_seconds') or 0)}s ago")
    if record.get('stale'):
        details.append('stale')
    return f"{phase} `{folder}` ({', '.join(details)})"

def _ack(client: SocketModeClient, req: SocketModeRequest, text: str) -> None:
    """Send the 3-second ack response with a body."""
    response = SocketModeResponse(envelope_id=req.envelope_id, payload={'text': text, 'response_type': 'ephemeral'})
    client.send_socket_mode_response(response)
_PROJECT_ROOT = paths.project_root()
_CRON_LOG = Path.home() / '.argus' / 'cron.log'

def _refuse_if_unavailable(client: SocketModeClient, req: SocketModeRequest, action: str, *, check_in_flight: bool=True) -> bool:
    """Shared pre-flight for the spawn commands (`run`, `reaudit`)."""
    if argus_control.is_paused():
        _ack(client, req, f':octagonal_sign: ARGUS is *paused* — `/argus resume` before triggering a {action}.')
        return True
    in_flight = argus_control.current_audit() if check_in_flight else None
    if in_flight and (not in_flight.get('stale')):
        _ack(client, req, f':hourglass_flowing_sand: an audit is already running: {_describe_current_audit(in_flight)}. Wait for it to finish or check the dashboard.')
        return True
    return False

def _refuse_if_key_locked(client: SocketModeClient, req: SocketModeRequest, key: str, action: str) -> bool:
    if _OUTPUT_ROOT is None:
        return False
    lock = audit_lock.active_key_lock(_OUTPUT_ROOT, key)
    if lock is None:
        return False
    _ack(client, req, f":hourglass_flowing_sand: `{key}` is already being audited ({lock.get('owner') or 'unknown owner'}, started {lock.get('started_at') or 'unknown time'}). Wait for it to finish before starting another {action}.")
    return True

def _spawn_detached(cmd: list[str]) -> subprocess.Popen:
    """Launch a detached audit subprocess, logging to ~/.argus/cron.log."""
    _CRON_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(_CRON_LOG, 'ab') as logf:
        return subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=str(_PROJECT_ROOT), start_new_session=True)

def _argus_argv() -> list[str]:
    """ARGUS entry point as an argv PREFIX, not a single token."""
    return paths.argus_argv()

def _post_response(response_url: str, text: str) -> None:
    """POST a delayed ephemeral reply to a slash command's response_url."""
    try:
        body = json.dumps({'text': text, 'response_type': 'ephemeral'}).encode('utf-8')
        rq = urllib.request.Request(response_url, data=body, headers={'Content-Type': 'application/json'}, method='POST')
        urllib.request.urlopen(rq, timeout=10).read()
    except Exception as e:
        log.warning('failed to post command follow-up: %s: %s', type(e).__name__, e)

def _launch_manual_references(references: list[str], *, explicit_team_slug: str | None, user: str) -> str:
    """Resolve, deduplicate, group, and launch manual audit references."""
    if argus_control.is_paused():
        raise RuntimeError('ARGUS is paused; resume it before triggering a manual audit')
    grouped: dict[str, dict[str, Any]] = {}
    for reference in references:
        parsed = parse_jira_reference(reference)
        if parsed is None:
            team = resolve_team(explicit_team_slug or 'web')
            if team is None:
                raise JiraReferenceError('unknown ARGUS team')
            kind = 'folder'
            value = reference
            label = f'folder=`{reference}`'
        else:
            if _JIRA_TARGET_RESOLVER is None:
                raise RuntimeError('Jira reference resolution is not configured')
            target = _JIRA_TARGET_RESOLVER.resolve(parsed.key)
            team = target.team
            if explicit_team_slug and explicit_team_slug != team.slug:
                raise JiraReferenceError(f'{target.key} belongs to team {team.name} (`{team.slug}`), not `{explicit_team_slug}`')
            kind = target.audit_kind
            value = target.audit_value
            if target.reference_kind == 'plan':
                label = f'test plan=`{target.key}` ({target.source_name})'
            elif target.reference_kind == 'case':
                label = f'test case=`{target.key}` ({target.source_name})'
            elif target.reference_kind == 'run':
                label = f'run=`{target.key}` → folder=`{target.folder_name}`'
            else:
                label = f'execution=`{target.key}` (folder=`{target.folder_name}`)'
        group = grouped.setdefault(team.slug, {'team': team, 'targets': [], 'labels': []})
        target_pair = (kind, value)
        if target_pair not in group['targets']:
            group['targets'].append(target_pair)
        group['labels'].append(label)
    if len(grouped) > 1:
        raise RuntimeError('The local bot launches one team at a time; launch one team at a time on a local bot host')
    in_flight = argus_control.current_audit()
    if in_flight and (not in_flight.get('stale')):
        raise RuntimeError(f"an audit is already running for {in_flight.get('folder') or 'an unknown folder'}")
    launches: list[tuple[str, str]] = []
    descriptions: list[str] = []
    failures: list[tuple[str, str]] = []
    for group in grouped.values():
        team = group['team']
        targets = group['targets']
        labels = group['labels']
        description = f'team=`{team.slug}` · ' + '; '.join(labels)
        try:
            if len(targets) == 1:
                (kind, value) = targets[0]
                if kind in {'run', 'plan', 'case'}:
                    command = [*_argus_argv(), '--profile', team.profile, 'manual-batch', f'--{kind}', value]
                else:
                    option = '--folder' if kind == 'folder' else '--key'
                    command = [*_argus_argv(), '--profile', team.profile, 'audit', option, value]
            else:
                command = [*_argus_argv(), '--profile', team.profile, 'manual-batch']
                for (kind, value) in targets:
                    option = {'folder': '--folder', 'run': '--run', 'plan': '--plan', 'case': '--case', 'key': '--key'}[kind]
                    command.extend([option, value])
            launch_id = str(_spawn_detached(command).pid)
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'
            failures.append((team.slug, error))
            log.exception('/argus run by %s failed to launch team %s', user, team.slug)
            continue
        launches.append((team.slug, launch_id))
        descriptions.append(description)
    if not launches:
        details = '; '.join((f'{team_slug}: {error}' for (team_slug, error) in failures))
        raise RuntimeError(f'no audits started ({details})')
    log.info('/argus run by %s started %s (%s); failures=%s', user, ', '.join((launch_id for (_, launch_id) in launches)), ' | '.join(descriptions), failures)
    if failures:
        lines = [f':warning: audit partially started by {user}; some team launches failed']
    else:
        lines = [f':hourglass_flowing_sand: audit started by {user}']
    lines.extend((f'  • {description}' for description in descriptions))
    lines.extend((f'  • launch `{team_slug}`: `{launch_id}`' for (team_slug, launch_id) in launches))
    lines.extend((f'  • failed `{team_slug}`: {error}' for (team_slug, error) in failures))
    lines.append('  • watch the ARGUS Activity pane for progress')
    return '\n'.join(lines)

def _run_manual_references_worker(references: list[str], explicit_team_slug: str | None, user: str, response_url: str) -> None:
    """Resolve and launch outside Slack's three-second acknowledgment window."""
    try:
        text = _launch_manual_references(references, explicit_team_slug=explicit_team_slug, user=user)
    except Exception as exc:
        log.exception('failed to resolve or launch manual audit references')
        text = f':rotating_light: failed to start manual audit: {type(exc).__name__}: {exc}'
    _post_response(response_url, text)

def _handle_run(client: SocketModeClient, req: SocketModeRequest, user: str, args: str) -> None:
    """Spawn a folder audit on demand."""
    arg = args.strip()
    explicit_team_slug: str | None = None
    for prefix in ('key ', '--key ', '--folder ', 'folder '):
        if arg.lower().startswith(prefix):
            arg = arg[len(prefix):].strip()
            break
    if arg:
        try:
            references = split_manual_references(arg)
            parsed_references = [parse_jira_reference(reference) for reference in references]
        except JiraReferenceError as exc:
            _ack(client, req, f':question: {exc}')
            return
    else:
        references = []
        parsed_references = []
    if len(references) > 1 or any(parsed_references):
        if argus_control.is_paused():
            _ack(client, req, ':octagonal_sign: ARGUS is *paused* — `/argus resume` before triggering a run.')
            return
        response_url = str((req.payload or {}).get('response_url') or '')
        if response_url:
            jira_count = sum((item is not None for item in parsed_references))
            _ack(client, req, f':mag: resolving {jira_count} Jira reference(s) and grouping {len(references)} audit target(s) by team…')
            threading.Thread(target=_run_manual_references_worker, args=(references, explicit_team_slug, user, response_url), name='argus_bot.manual_refs', daemon=True).start()
        else:
            try:
                text = _launch_manual_references(references, explicit_team_slug=explicit_team_slug, user=user)
            except Exception as exc:
                log.exception('failed to resolve or launch manual audit references')
                text = f':rotating_light: failed to start manual audit: {type(exc).__name__}: {exc}'
            _ack(client, req, text)
        return
    if _refuse_if_unavailable(client, req, 'run', check_in_flight=True):
        return
    if arg and _E_KEY_RE.match(arg):
        if _refuse_if_key_locked(client, req, arg, 'run'):
            return
        cmd = [*_argus_argv(), 'audit', '--key', arg]
        descr = f'single key=`{arg}`'
    elif arg:
        cmd = [*_argus_argv(), 'audit', '--folder', arg]
        descr = f'folder=`{arg}` (manual)'
    else:
        wrapper = paths.cron_wrapper_script()
        if wrapper is None:
            _ack(client, req, f':rotating_light: `cron_wrapper.sh` not found under `{_PROJECT_ROOT}` — this looks like an installed package rather than a checkout. Use `/argus run <folder>` to name a folder explicitly, or set `ARGUS_PROJECT_ROOT` to the checkout path.')
            return
        cmd = ['/bin/bash', '-lc', str(wrapper)]
        descr = 'auto-folder (same as cron)'
    try:
        proc = _spawn_detached(cmd)
        launch_id = str(proc.pid)
    except Exception as e:
        log.exception('failed to spawn audit run')
        _ack(client, req, f':rotating_light: failed to start audit: {type(e).__name__}: {e}')
        return
    log.info('/argus run by %s started %s (%s)', user, launch_id, descr)
    _ack(client, req, f':hourglass_flowing_sand: audit started by {user}\n  • {descr}\n  • launch: `{launch_id}`\n  • watch the ARGUS Activity pane for progress')

def _now_iso() -> str:
    """UTC ISO-8601 timestamp with 'Z' suffix."""
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')

def _find_audit_json(execution_key: str) -> Path | None:
    """Locate the audit.json for an E-key under any folder layout."""
    root = _OUTPUT_ROOT if _OUTPUT_ROOT is not None else paths.output_root()
    return exec_paths.find_audit_json(root, execution_key)

def _atomic_write_json(path: Path, data: dict) -> None:
    """Atomic JSON write — same pattern as argus_control._write."""
    atomic_io.atomic_write_json(path, data)

def _handle_reaudit(client: SocketModeClient, req: SocketModeRequest, user: str, args: str) -> None:
    """Force a re-audit of a single E-key, bypassing Layer 1's cache."""
    parts = args.split(maxsplit=1)
    key = parts[0].strip() if parts else ''
    if not key or not _E_KEY_RE.match(key):
        _ack(client, req, ':question: usage: `/argus reaudit PROJECT-E12345 [reason]`')
        return
    if _refuse_if_unavailable(client, req, 're-audit'):
        return
    if _refuse_if_key_locked(client, req, key, 're-audit'):
        return
    cmd = [*_argus_argv(), 'audit', '--key', key]
    try:
        proc = _spawn_detached(cmd)
        launch_id = str(proc.pid)
    except Exception as e:
        log.exception('failed to spawn re-audit')
        _ack(client, req, f':rotating_light: failed to start re-audit: {type(e).__name__}: {e}')
        return
    log.info('/argus reaudit by %s started %s (key=%s)', user, launch_id, key)
    _ack(client, req, f':repeat: re-audit started by {user} · key=`{key}` · launch=`{launch_id}`')

def _handle_dismiss(client: SocketModeClient, req: SocketModeRequest, user: str, args: str) -> None:
    """Annotate an audit.json with a `dismissed` acknowledgment."""
    parts = args.split(maxsplit=1)
    key = parts[0].strip() if parts else ''
    reason = parts[1].strip() if len(parts) > 1 else ''
    if not key or not _E_KEY_RE.match(key):
        _ack(client, req, ':question: usage: `/argus dismiss PROJECT-E12345 [reason]`')
        return
    audit_path = _find_audit_json(key)
    if audit_path is None:
        _ack(client, req, f':question: no audit found for `{key}`. The E-key may never have been audited, or the artefact was archived.')
        return
    try:
        audit = json.loads(audit_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        _ack(client, req, f':rotating_light: could not read `{audit_path}`: {type(e).__name__}: {e}')
        return
    audit.setdefault('acknowledgments', []).append({'type': 'dismissed', 'by': user, 'at': _now_iso(), 'reason': reason or None})
    try:
        _atomic_write_json(audit_path, audit)
    except OSError as e:
        log.exception('failed to write dismissal to %s', audit_path)
        _ack(client, req, f':rotating_light: could not write dismissal: {type(e).__name__}: {e}')
        return
    log.info('/argus dismiss by %s key=%s reason=%r', user, key, reason)
    reason_text = f' — {reason}' if reason else ''
    _ack(client, req, f':mute: `{key}` dismissed by {user}{reason_text}')

def _handle_pause(client: SocketModeClient, req: SocketModeRequest, user: str, args: str) -> None:
    """Pause cron + folder runs until `/argus resume`."""
    if argus_control.pause(by=user, reason=args or None):
        reason_text = f' (reason: {args})' if args else ''
        _ack(client, req, f':octagonal_sign: ARGUS paused by {user}{reason_text}.\nCron + folder runs will skip until `/argus resume`.')
    else:
        _ack(client, req, ":rotating_light: failed to write pause state — check the bot host's filesystem.")

def _handle_resume(client: SocketModeClient, req: SocketModeRequest, user: str, args: str) -> None:
    """Resume audits paused by `/argus pause`."""
    if argus_control.resume(by=user):
        _ack(client, req, f':green_circle: ARGUS resumed by {user}. Next cron tick proceeds.')
    else:
        _ack(client, req, ":rotating_light: failed to write resume state — check the bot host's filesystem.")

def _handle_status(client: SocketModeClient, req: SocketModeRequest, user: str, args: str) -> None:
    """Show current pause/resume state + bot heartbeat freshness."""
    _ack(client, req, _format_status())


_HELP_TEXT = '`/argus run [folder or Jira key]` — audit a folder or saved test reference\n`/argus reaudit <execution-key>` — re-audit one execution\n`/argus dismiss <execution-key> <reason>` — dismiss a finding\n`/argus pause [reason]` — pause audits\n`/argus resume` — resume audits\n`/argus status` — show current activity\n`/argus help` — this message'

def _handle_help(client: SocketModeClient, req: SocketModeRequest, user: str, args: str) -> None:
    """Print the command reference."""
    _ack(client, req, _HELP_TEXT)
_SUBCOMMANDS = {'pause': _handle_pause, 'resume': _handle_resume, 'status': _handle_status, '': _handle_status, 'run': _handle_run, 'reaudit': _handle_reaudit, 'dismiss': _handle_dismiss, 'help': _handle_help, '?': _handle_help}
_MUTATING_SUBCOMMANDS = frozenset({'pause', 'resume', 'run', 'reaudit', 'dismiss'})
_BOT_ADMINS: frozenset[str] = frozenset()
_OUTPUT_ROOT: Path | None = None
_SLACK_USER_ID_RE = re.compile('^[UW][A-Z0-9]{6,}$')

def _is_admin(payload: dict[str, Any]) -> bool:
    user_id = payload.get('user_id')
    return bool(user_id) and user_id in _BOT_ADMINS

def _validate_admin_ids(configured: list[str]) -> frozenset[str]:
    valid = {u for u in configured if _SLACK_USER_ID_RE.match(u or '')}
    rejected = sorted(set(configured) - valid)
    if rejected:
        log.warning("bot_admin_users contains %d entr%s that %s not Slack member ID(s) and %s IGNORED: %s. Display names are user-editable in Slack, so they are no longer accepted. Get the ID from the member's Slack profile -> 'Copy member ID' (looks like U01ABC2DEF3) and put that in [notify] bot_admin_users.", len(rejected), 'y' if len(rejected) == 1 else 'ies', 'is' if len(rejected) == 1 else 'are', 'is' if len(rejected) == 1 else 'are', ', '.join(rejected))
    return frozenset(valid)

def _refuse_unauthorized(client: SocketModeClient, req: SocketModeRequest, user: str, sub: str) -> None:
    log.warning('unauthorized /argus %s attempt by %s', sub, user)
    if _BOT_ADMINS:
        _ack(client, req, f':no_entry: `{sub}` changes ARGUS state and is limited to the operators in `bot_admin_users` (config.toml). `/argus status` and `/argus help` are open to everyone.')
    else:
        _ack(client, req, f":no_entry: `{sub}` is disabled: no valid `bot_admin_users` configured. The operator must add their Slack member ID (profile → 'Copy member ID') to `[notify] bot_admin_users` in config.toml and restart the bot.")

def _handle_command(client: SocketModeClient, req: SocketModeRequest) -> None:
    """Route a slash-command payload to the right action."""
    payload: dict[str, Any] = req.payload or {}
    cmd = (payload.get('command') or '').strip()
    text = (payload.get('text') or '').strip()
    user = payload.get('user_name') or payload.get('user_id') or 'unknown'
    if cmd != '/argus':
        return
    parts = text.split(maxsplit=1)
    sub = (parts[0].lower() if parts else '').strip()
    args = parts[1] if len(parts) > 1 else ''
    log.info('/argus %s by %s (args=%r)', sub, user, args)
    handler = _SUBCOMMANDS.get(sub)
    if handler is None:
        _ack(client, req, f':question: unknown subcommand `{sub}`. Try `/argus help`.')
        return
    if sub in _MUTATING_SUBCOMMANDS and (not _is_admin(payload)):
        _refuse_unauthorized(client, req, user, sub)
        return
    handler(client, req, user, args)

def _dispatch_socket_request(client: SocketModeClient, req: SocketModeRequest) -> None:
    """Route one Socket Mode envelope, ignoring Slack redeliveries."""
    attempt = getattr(req, 'retry_attempt', None)
    if isinstance(attempt, int) and attempt > 0:
        log.warning('ignoring Slack redelivery (attempt %s, reason %s) of %s', attempt, getattr(req, 'retry_reason', None), req.type)
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        return
    if req.type == 'slash_commands':
        _handle_command(client, req)
        return
    log.info('unhandled socket request type: %s', req.type)
    client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

def _heartbeat_loop(stop_event: threading.Event) -> None:
    """Background thread: write argus_control heartbeat every 60s."""
    pid = os.getpid()
    while not stop_event.is_set():
        argus_control.update_heartbeat(pid=pid)
        if stop_event.wait(timeout=_HEARTBEAT_INTERVAL_S):
            return

def main(argv: list[str] | None=None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s %(message)s')
    settings = argus_config.load()
    runtime.configure(settings, load_team_rules=False)
    global _BOT_ADMINS, _OUTPUT_ROOT, _E_KEY_RE, _CRON_LOG
    global _JIRA_TARGET_RESOLVER
    _JIRA_TARGET_RESOLVER = JiraAuditTargetResolver(settings.extractor)
    _BOT_ADMINS = _validate_admin_ids(list(settings.notify.bot_admin_users))
    _OUTPUT_ROOT = paths.output_root(settings)
    _E_KEY_RE = re.compile(settings.extractor.execution_key_pattern)
    _CRON_LOG = settings.general.state_dir / 'cron.log'
    if not _BOT_ADMINS:
        log.warning("no valid bot_admin_users — pause/resume/run/reaudit/dismiss/are DISABLED. Add your Slack member ID (profile -> 'Copy member ID', e.g. U01ABC2DEF3) to [notify] bot_admin_users in config.toml.")
    app_token: str | None
    bot_token: str | None
    app_token = _load_token(settings.notify.app_token_path, 'app')
    bot_token = _load_token(settings.notify.bot_token_path, 'bot')
    if not app_token or not bot_token:
        sys.stderr.write(f'[argus_bot] missing slack tokens. Setup:\n  1. api.slack.com/apps → your app → Socket Mode → generate App-Level Token (xapp-...).\n  2. nano {settings.notify.app_token_path}  (paste the xapp-..., chmod 600).\n  3. OAuth & Permissions → install / reinstall to workspace → copy Bot User OAuth Token (xoxb-...).\n  4. nano {settings.notify.bot_token_path}  (paste the xoxb-..., chmod 600).\n  5. Slash Commands → create `/argus`.\n')
        return 1
    if not app_token.startswith('xapp-'):
        sys.stderr.write(f"[argus_bot] app token at {settings.notify.app_token_path} does not start with 'xapp-' — it's the wrong token. The app-level token comes from Basic Information → App-Level Tokens, NOT OAuth & Permissions.\n")
        return 1
    if not bot_token.startswith('xoxb-'):
        sys.stderr.write(f"[argus_bot] bot token at {settings.notify.bot_token_path} does not start with 'xoxb-' — it's the wrong token. The bot token comes from OAuth & Permissions, NOT Basic Information.\n")
        return 1
    web = WebClient(token=bot_token)
    client = SocketModeClient(app_token=app_token, web_client=web)

    def listener(base_client: BaseSocketModeClient, req: SocketModeRequest) -> None:
        client_arg = cast(SocketModeClient, base_client)
        try:
            _dispatch_socket_request(client_arg, req)
        except Exception as e:
            log.exception('listener error: %s', e)
            try:
                _ack(client_arg, req, f':rotating_light: bot error: {type(e).__name__}: {e}')
            except Exception as ack_err:
                log.warning('failed to deliver error ack for %s: %s: %s', req.type, type(ack_err).__name__, ack_err)
    client.socket_mode_request_listeners.append(listener)
    stop_event = threading.Event()
    hb_thread = threading.Thread(target=_heartbeat_loop, args=(stop_event,), name='argus_bot.heartbeat', daemon=True)
    hb_thread.start()

    def _shutdown(signum: int, _frame: Any) -> None:
        log.info('received signal %d, shutting down', signum)
        stop_event.set()
        try:
            client.disconnect()
        except Exception as e:
            log.warning('disconnect raised %s: %s', type(e).__name__, e)
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    log.info('connecting to Slack via Socket Mode...')
    client.connect()
    log.info('connected. listening for /argus commands. Ctrl-C to exit.')
    try:
        while not stop_event.is_set():
            stop_event.wait(timeout=3600)
    finally:
        log.info('argus_bot exited')
    return 0
if __name__ == '__main__':
    sys.exit(main())
