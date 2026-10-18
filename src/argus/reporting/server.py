"""ARGUS — local HTTP server for viewing and reviewing local audit reports."""
from __future__ import annotations

import argparse
import datetime as dt
import functools
import gzip
import hashlib
import http.server
import json
import logging
import mimetypes
import os
import re as _re
import shutil
import socket
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from argus.operations import control as argus_control
from argus import config as argus_config
from argus import runtime
from argus.reporting import static_assets
from argus.reporting.ui import (
    DEFAULT_UI_CONTEXT,
    ReportUiContext,
    TeamLink,
    from_settings as ui_context_from_settings,
)

log = logging.getLogger("argus.serve")


# The stylesheet contains ~90 literal braces: safe only as an f-string
# SUBSTITUTION VALUE — never pass it through .format()/%-formatting and
# never make the CSS itself a template.
_ASSETS_DIR = Path(__file__).parent / "assets"
_INDEX_CSS = (_ASSETS_DIR / "index.css").read_text(encoding="utf-8")
_INDEX_JS = (_ASSETS_DIR / "index.js").read_text(encoding="utf-8")


def _human_time(ts: float) -> str:
    """Format a unix timestamp as relative-then-absolute, e.g. '23 May, 13:13 (2h ago)'."""
    now = dt.datetime.now()
    when = dt.datetime.fromtimestamp(ts)
    delta = now - when
    abs_str = when.strftime("%d %b, %H:%M")
    secs = delta.total_seconds()
    if secs < 60:
        rel = "just now"
    elif secs < 3600:
        rel = f"{int(secs / 60)}m ago"
    elif secs < 86400:
        rel = f"{secs / 3600:.1f}h ago"
    else:
        rel = f"{int(secs / 86400)}d ago"
    return f"{abs_str} ({rel})"


def _scan_folders(out_dir: Path) -> list[dict]:
    """Find every folder under out_dir that has a usable _argus.html."""
    now = time.monotonic()
    with _SCAN_CACHE_LOCK:
        cached = _SCAN_CACHE.get(str(out_dir))
        if cached is not None and now - cached[0] < _SCAN_CACHE_TTL_S:
            return cached[1]
        rows = _scan_folders_uncached(out_dir)
        _SCAN_CACHE[str(out_dir)] = (time.monotonic(), rows)
        return rows


_SCAN_CACHE_LOCK = threading.Lock()
_SCAN_CACHE: dict[str, tuple[float, list[dict]]] = {}
_SCAN_CACHE_TTL_S = 15.0


def _scan_folders_uncached(out_dir: Path) -> list[dict]:
    from argus.core.coverage_scope import covers_restored_scope
    from argus.reporting import report

    rows: list[dict] = []
    if not out_dir.exists():
        return rows
    for sub in sorted(out_dir.iterdir()):
        if not sub.is_dir():
            continue
        report_path = sub / "_argus.html"
        if not report_path.exists():
            continue
        summaries = report.scan_output_dir(sub)
        audit_count = len(summaries)
        title = sub.name
        try:
            title = (
                json.loads((sub / "_folder_meta.json").read_text())
                .get("folder_name")
                or sub.name
            )
        except (OSError, ValueError):
            pass
        completion_states = [summary.analysis_status for summary in summaries]
        if report.read_report_failed_keys(sub) or "incomplete" in completion_states:
            completion = "incomplete"
        elif completion_states and all(
            state == "not_applicable" for state in completion_states
        ):
            completion = "not_applicable"
        else:
            completion = "complete"
        coverage: dict | None = None
        coverage_path = sub / "_coverage.json"
        try:
            raw_coverage = json.loads(coverage_path.read_text())
            try:
                manifest = json.loads((sub / "_latest_run.json").read_text())
            except (OSError, json.JSONDecodeError):
                manifest = None
            if isinstance(raw_coverage, dict) and covers_restored_scope(raw_coverage, manifest):
                coverage = {
                    "items_total": int(raw_coverage.get("items_total") or 0),
                    "items_executed": int(
                        raw_coverage.get("items_executed") or 0),
                }
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            coverage = None
        rows.append({
            "name": sub.name,
            "title": title,
            "rel_url": f"{sub.name}/_argus.html",
            "audits": audit_count,
            "mtime": report_path.stat().st_mtime,
            "completion_state": completion,
            "coverage": coverage,
            "prefetch": True,
        })
    rows.sort(key=lambda r: -r["mtime"])
    return rows


def _format_age(seconds: int) -> str:
    """Compact relative age for the banner. e.g. '3m', '1h 12m'."""
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    h, rem = divmod(seconds, 3600)
    return f"{h}h {rem // 60:02d}m"



_FOLDER_SECTIONS: list[tuple[str, str]] = [
    ("targeted_regression", "Targeted regression"),
    ("targeted regression", "Targeted regression"),
    ("sanity_testing",      "Sanity testing"),
    ("sanity testing",      "Sanity testing"),
    ("additional_non_automated", "Additional non-automated"),
    ("additional non automated", "Additional non-automated"),
    ("loyalty_hub",         "Loyalty Hub"),
    ("loyalty hub",         "Loyalty Hub"),
    ("lop_testing",         "LOP testing"),
    ("lop testing",         "LOP testing"),
]

_SECTION_ORDER = [
    "Targeted regression",
    "Sanity testing",
    "Additional non-automated",
    "Loyalty Hub",
    "LOP testing",
    "Other",
]


def configure_sections(labels: list[str]) -> None:
    """Configure dashboard grouping from the selected team profile."""
    global _FOLDER_SECTIONS, _SECTION_ORDER
    if not labels:
        return
    pairs: list[tuple[str, str]] = []
    for label in labels:
        normalised = label.lower().replace("-", " ")
        pairs.append((normalised, label))
        pairs.append((normalised.replace(" ", "_"), label))
    _FOLDER_SECTIONS = pairs
    _SECTION_ORDER = [*labels, "Other"]


def _classify_section(name: str) -> str:
    """Bucket a folder slug into a human section label."""
    lo = name.lower()
    for prefix, label in _FOLDER_SECTIONS:
        if lo.startswith(prefix):
            return label
    return "Other"


_DATE_IN_NAME_RX = _re.compile(
    r"\b(\d{1,2})[\s_-]+"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[\s_-]+"
    r"(20\d{2})\b",
    _re.IGNORECASE,
)
_MONTH_NUM = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"])}


def _parse_date_from_name(name: str) -> dt.date | None:
    """Extract the date embedded in a folder slug. None if not parseable."""
    m = _DATE_IN_NAME_RX.search(name or "")
    if not m:
        return None
    try:
        return dt.date(int(m.group(3)),
                       _MONTH_NUM[m.group(2).lower()[:3]],
                       int(m.group(1)))
    except (ValueError, KeyError):
        return None


def _freshness_class(secs: float) -> str:
    """Map an age in seconds to a CSS class controlling timestamp colour."""
    if secs < 600:
        return "fresh-now"
    if secs < 14400:
        return "fresh-recent"
    if secs < 172800:
        return "fresh-day"
    return "fresh-stale"


def _short_relative(secs: float) -> str:
    """Compact relative time suited for the timestamp pill: '3m', '1h', '2d'."""
    if secs < 60:
        return "now"
    if secs < 3600:
        return f"{int(secs / 60)}m"
    if secs < 86400:
        return f"{int(secs / 3600)}h"
    return f"{int(secs / 86400)}d"


def _relative_phrase(secs: float) -> str:
    """Complete compact age phrase for server fallback and live-time parity."""
    compact = _short_relative(secs)
    return compact if compact == "now" else f"{compact} ago"


def _timestamp_markup(ts: float, label: str, *, css_class: str = "") -> str:
    """Render a semantic timestamp with data consumed by the index updater."""
    timestamp_ms = max(0, round(ts * 1000))
    iso_timestamp = dt.datetime.fromtimestamp(
        ts,
        tz=dt.timezone.utc,
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    class_attr = f' class="{css_class}"' if css_class else ""
    return (
        f'<time{class_attr} data-argus-relative-time '
        f'data-timestamp-ms="{timestamp_ms}" '
        f'datetime="{iso_timestamp}">{_html(label)}</time>'
    )


def _render_in_progress_banner() -> str:
    """Render the 'AUDITING NOW' or 'stale audit state' banner."""
    try:
        record = argus_control.current_audit()
    except Exception as e:
        log.debug("in-progress banner suppressed, control state "
                  "unreadable: %s: %s", type(e).__name__, e)
        return ""
    if not record:
        return ""
    folder = _html(record.get("folder") or "?")
    n_keys = record.get("n_keys") or 0
    age = record.get("age_seconds") or 0
    age_str = _format_age(int(age))
    if record.get("stale"):
        return (
            '<div class="banner stale" role="status">'
            '<span class="icon" aria-hidden="true">⚠</span>'
            '<span class="title">Stale audit state</span>'
            f'<span class="meta">last folder <code>{folder}</code> '
            f'· started {age_str} ago, process appears dead · '
            'run <code>argus control status</code> to inspect</span>'
            '</div>'
        )
    phase = str(record.get("phase") or "auditing").lower()
    title = _ACTIVE_AUDIT_TITLES.get(phase, "Auditing now")
    keys_html = f'{n_keys} keys · ' if n_keys else ""
    requested_by = record.get("requested_by")
    requested_html = (
        f' · requested by <code>{_html(requested_by)}</code>'
        if requested_by else ""
    )
    return (
        '<div class="banner" role="status">'
        '<span class="icon" aria-hidden="true">'
        '<span class="argus-progress-spinner"></span></span>'
        f'<span class="title">{title}</span>'
        f'<span class="meta"><code>{folder}</code> · '
        f'{keys_html}started {age_str} ago{requested_html}</span>'
        '</div>'
    )


_ACTIVE_AUDIT_TITLES = {
    "launching": "Launching audit",
    "preparing": "Preparing audit",
    "auditing": "Auditing now",
    "running": "Auditing now",
    "publishing": "Publishing report",
    "failed": "Audit failed",
    "stopped": "Audit stopped",
}


def _render_live_status_banner(status_url: str) -> str:
    """Hidden banner that polls a team ``index.json`` for ``active_audit``.

    Published team dashboards are static HTML, so this is the only way the
    page can show a launch or preparation phase before the first result is
    checkpointed. Every value is written through ``textContent``; nothing
    from the payload is interpreted as markup.
    """
    titles = json.dumps(_ACTIVE_AUDIT_TITLES, separators=(",", ":"))
    return (
        '<div class="banner argus-live-status" role="status" '
        'aria-live="polite" hidden data-argus-live-status '
        f'data-status-url="{_html(status_url)}">'
        '<span class="icon" aria-hidden="true">'
        '<span class="argus-progress-spinner"></span></span>'
        '<span class="title" data-argus-live-title>Audit running</span>'
        '<span class="meta" data-argus-live-meta></span>'
        '</div>'
        '<style>'
        '.argus-live-status{display:flex;align-items:center;gap:12px;'
        'margin-bottom:24px;padding:14px 18px;border-radius:6px;'
        'border:1px solid var(--ui-color-border-divider-default);'
        'border-left:3px solid var(--ui-color-text-status-info);'
        'background:var(--ui-color-background-container-content)}'
        '.argus-live-status[hidden]{display:none}'
        '.argus-live-status.stale{'
        'border-left-color:var(--ui-color-text-status-error)}'
        '.argus-live-status .title{font-weight:700;text-transform:uppercase;'
        'letter-spacing:.8px;font-size:13px}'
        '.argus-live-status .meta{font-size:13px;'
        'color:var(--ui-color-text-body-secondary)}'
        '</style>'
        '<script>'
        '(function(){'
        'const node=document.querySelector("[data-argus-live-status]");'
        'if(!node)return;'
        f'const titles={titles};'
        'function describe(item){'
        'const parts=[];'
        'const folder=item.folder||item.target;'
        'if(folder)parts.push(String(folder));'
        'if(item.target&&item.target!==folder)parts.push(String(item.target));'
        'if(item.requested_by)parts.push("requested by "+item.requested_by);'
        'const done=Number(item.completed),total=Number(item.total);'
        'if(Number.isFinite(total)&&total>0)'
        'parts.push(`${Math.max(0,done||0)}/${total} keys`);'
        'if(item.last_key)parts.push("last "+item.last_key);'
        'if(item.diagnostic)parts.push(String(item.diagnostic));'
        'return parts.join(" · ");'
        '}'
        'async function refresh(){'
        'try{'
        'const response=await fetch(node.dataset.statusUrl,{'
        'cache:"no-store",credentials:"same-origin"});'
        'if(!response.ok)throw new Error(String(response.status));'
        'const payload=await response.json();'
        'const item=payload&&payload.active_audit;'
        'const phase=String(item&&(item.phase||item.state)||"").toLowerCase();'
        'if(!item||!Object.hasOwn(titles,phase)){node.hidden=true;return;}'
        'node.querySelector("[data-argus-live-title]").textContent=titles[phase];'
        'node.querySelector("[data-argus-live-meta]").textContent=describe(item);'
        'node.classList.toggle("stale",phase==="failed"||phase==="stopped");'
        'node.hidden=false;'
        '}catch(_error){node.hidden=true;}'
        '}'
        'refresh();setInterval(refresh,10000);'
        '})();'
        '</script>'
    )


def _render_index(
    out_dir: Path,
    *,
    rows: list[dict] | None = None,
    banner_html: str | None = None,
    live_status_url: str | None = None,
    ui_context: ReportUiContext = DEFAULT_UI_CONTEXT,
) -> str:
    """Build the HTML index page listing all folders with reports.

    ``live_status_url`` adds a self-updating status banner that polls the
    given ``index.json`` (used by published team dashboards); it never
    triggers the meta refresh that the server-rendered banner relies on.
    """
    rows = _scan_folders(out_dir) if rows is None else rows
    banner_html = (
        _render_in_progress_banner()
        if banner_html is None
        else banner_html
    )
    auto_refresh_html = (
        '<meta http-equiv="refresh" content="30">' if banner_html else ""
    )
    if live_status_url:
        banner_html += _render_live_status_banner(live_status_url)

    total_folders = len(rows)
    total_audits = sum(r["audits"] for r in rows)
    complete_folders = sum(
        row.get("completion_state") == "complete" for row in rows
    )
    incomplete_folders = sum(
        row.get("completion_state") == "incomplete" for row in rows
    )
    coverage_values = [
        progress
        for row in rows
        if (progress := _coverage_progress(row.get("coverage"))) is not None
    ]
    coverage_total = sum(progress[1] for progress in coverage_values)
    coverage_executed = sum(progress[0] for progress in coverage_values)
    coverage_value = (
        f"{round(coverage_executed / coverage_total * 100)}%"
        if coverage_total else "—"
    )
    coverage_meta = (
        f'{coverage_executed:,} of {coverage_total:,} planned items · '
        f'{len(coverage_values)} '
        f'{"report" if len(coverage_values) == 1 else "reports"}'
        if coverage_total else "No coverage data available"
    )
    completion_meta = (
        f'{incomplete_folders} '
        f'{"report" if incomplete_folders == 1 else "reports"} incomplete'
        if incomplete_folders else
        f'of {total_folders} '
        f'{"report" if total_folders == 1 else "reports"}'
    )
    if rows:
        latest_mtime = max(r["mtime"] for r in rows)
        secs_since_latest = max(0, dt.datetime.now().timestamp() - latest_mtime)
        last_refresh_label = _relative_phrase(secs_since_latest)
        last_refresh_html = _timestamp_markup(
            latest_mtime,
            last_refresh_label,
        )
    else:
        last_refresh_html = "—"

    from . import html_report as _html_report

    dashboard = _html(ui_context.dashboard_name)
    dashboard_actions = (
        '<div class="cs-actions argus-dashboard-actions">'
        '<a class="cs-btn" href="rules">'
        '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="1.7" stroke-linecap="round" '
        'stroke-linejoin="round" aria-hidden="true">'
        '<path d="M9 4H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 '
        '2-2V6a2 2 0 0 0-2-2h-4"/><rect x="9" y="2" width="6" '
        'height="4" rx="1"/><path d="m8 13 2 2 6-6"/></svg>'
        'Team rules</a></div>'
        if ui_context.dashboard_slug else ""
    )
    page_head = (
        '<nav class="cs-crumbs" aria-label="Breadcrumbs">'
        '<span>Workspace</span><span class="sep" aria-hidden="true">/</span>'
        '<span aria-current="page">Overview</span></nav>'
        '<section class="cs-header argus-dashboard-heading"><div>'
        '<span class="page-eyebrow">QUALITY AT A GLANCE</span>'
        '<h1>Audit reports</h1>'
        '<p>Review execution coverage, explore findings, and keep track '
        'of your team’s audits.</p>'
        '</div>'
        f'{dashboard_actions}</section>'
        '<dl class="argus-overview" aria-label="Audit overview">'
        '<div class="argus-overview-card">'
        '<dt class="argus-overview-label">Reports</dt>'
        f'<dd class="argus-overview-value">{total_folders:,}</dd>'
        '<dd class="argus-overview-meta">Folders and test plans</dd></div>'
        '<div class="argus-overview-card">'
        '<dt class="argus-overview-label">Audits recorded</dt>'
        f'<dd class="argus-overview-value">{total_audits:,}</dd>'
        '<dd class="argus-overview-meta">Across all reports</dd></div>'
        '<div class="argus-overview-card">'
        '<dt class="argus-overview-label">Execution coverage</dt>'
        f'<dd class="argus-overview-value">{coverage_value}</dd>'
        f'<dd class="argus-overview-meta">{coverage_meta}</dd></div>'
        '<div class="argus-overview-card">'
        '<dt class="argus-overview-label">Completed reports</dt>'
        f'<dd class="argus-overview-value">{complete_folders:,}</dd>'
        f'<dd class="argus-overview-meta">{completion_meta}</dd></div>'
        '</dl>'
    )
    from .operator_controls import render as render_operator_controls
    page_head += render_operator_controls(ui_context)

    if not rows:
        body = (
            page_head
            + f'{banner_html}'
            + '<div class="cs-container argus-empty" role="status">'
            '<div class="cs-container-header">'
            '<h2>Your reports will appear here</h2></div>'
            '<div class="cs-card-body">'
            '<p>ARGUS has no audited folders to show.</p>'
            f'<p>Looked under <code>{_html(out_dir)}</code>. Run '
            '<code>argus audit --folder ...</code> or trigger '
            '<code>/argus run</code> from Slack to generate one.</p>'
            '</div></div>'
        )
    else:
        active_name: str | None = None
        candidates = [
            (r, _parse_date_from_name(r["name"]))
            for r in rows
            if _classify_section(r["name"]) == "Targeted regression"
        ]
        dated_candidates = [(r, d) for r, d in candidates if d is not None]
        if dated_candidates:
            today = dt.date.today()
            past_or_today = [
                (r, date)
                for r, date in dated_candidates
                if date <= today
            ]
            if past_or_today:
                past_or_today.sort(
                    key=lambda rd: (rd[1], rd[0]["mtime"]), reverse=True)
                active_name = past_or_today[0][0]["name"]

        sections: dict[str, list[dict]] = {}
        for r in rows:
            sections.setdefault(_classify_section(r["name"]), []).append(r)

        section_html_parts: list[str] = []
        for section_label in _SECTION_ORDER:
            section_rows = sections.get(section_label)
            if not section_rows:
                continue
            section_html_parts.append(
                _render_folder_section(
                    (
                        "Recent reports"
                        if len(sections) == 1
                        and section_label in {"Other", ui_context.dashboard_name}
                        else section_label
                    ),
                    section_rows,
                    active_name,
                )
            )

        search_toolbar = (
            '<div class="argus-report-list-heading">'
            '<div><h2>Report library</h2>'
            '<p>All your audit results, in one place.</p></div>'
            '<span class="argus-dashboard-updated">'
            f'Latest update {last_refresh_html}</span></div>'
            '<div class="argus-report-toolbar" role="search">'
            '<div class="argus-report-search-wrap">'
            '<svg class="argus-search-icon" width="18" height="18" '
            'viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            'stroke-width="1.8" stroke-linecap="round" aria-hidden="true">'
            '<circle cx="10.5" cy="10.5" r="6.5"/>'
            '<path d="m16 16 4.5 4.5"/></svg>'
            '<input type="search" id="argus-report-search" '
            'class="argus-input argus-report-search" '
            'aria-label="Search reports" autocomplete="off" '
            'aria-keyshortcuts="/" placeholder="Search reports…">'
            '<kbd class="argus-search-shortcut" aria-hidden="true">/</kbd></div>'
            '<span class="argus-report-search-count" '
            'id="argus-report-search-count" aria-live="polite">'
            f'{total_folders} of {total_folders} reports</span></div>'
            '<div class="cs-container argus-report-no-results" '
            'id="argus-report-no-results" role="status" hidden>'
            '<div class="cs-card-body">'
            '<p>No reports match this search.</p></div></div>'
        )
        body = (
            page_head
            + f'{banner_html}'
            + search_toolbar
            + "".join(section_html_parts)
        )

    return (
        '<!DOCTYPE html>\n<html lang="en" data-theme="light"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'{auto_refresh_html}'
        f'<title>Audit reports — {dashboard} — ARGUS</title>'
        f'{_html_report._cs_head_extra(ui_context)}'
        f'{_html_report._cs_page_css(ui_context)}'
        '</head><body>'
        f'{_html_report._cs_topnav("/", ui_context)}'
        '<div class="application-layout" id="application-layout">'
        f'{_html_report._cs_sidenav("./", "reports", False, ui_context)}'
        f'<main class="argus-main main-content argus-index-page">{body}</main></div>'
        f'<script>{_INDEX_JS}</script>'
        f'<script>{_html_report._ARGUS_JS}</script>'
        '</body></html>'
    )


def _render_folder_section(
    label: str,
    rows: list[dict],
    active_name: str | None,
) -> str:
    """Render one dashboard container of report folders."""
    rows_html = "".join(
        _render_folder_row(r, r["name"] == active_name)
        for r in rows
    )
    return (
        '<section class="cs-container argus-report-section" '
        'data-argus-report-section>'
        '<div class="cs-container-header">'
        f'<h2>{_html(label)}<span class="cs-counter">'
        f'(<span data-argus-section-count>{len(rows)}</span>)'
        '</span></h2></div>'
        '<div class="argus-report-table-wrap">'
        '<table class="argus-table cs-folders"><thead><tr>'
        '<th scope="col">Report</th><th scope="col" class="argus-num">Audits</th>'
        '<th scope="col">Execution coverage</th>'
        '<th scope="col">Status</th><th scope="col">Actions</th>'
        f'</tr></thead><tbody>{rows_html}</tbody></table></div></section>'
    )


def _coverage_progress(
    coverage: object,
) -> tuple[int, int, int] | None:
    """Return bounded executed, total, and percentage coverage values."""
    if not isinstance(coverage, dict):
        return None
    try:
        total = int(coverage.get("items_total") or 0)
        executed = int(coverage.get("items_executed") or 0)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    executed = min(total, max(0, executed))
    return executed, total, round(executed / total * 100)


def _render_folder_row(
    row: dict,
    is_active: bool,
) -> str:
    """Render one folder row with truthful coverage and completion state."""
    name = str(row.get("title") or row["name"]).replace("_", " ")
    audits = row["audits"]
    mtime = row["mtime"]
    rel_url = row["rel_url"]
    secs = max(0, dt.datetime.now().timestamp() - mtime)
    fresh_cls = _freshness_class(secs)
    rel_label = _relative_phrase(secs)
    abs_label = dt.datetime.fromtimestamp(mtime).strftime("%d %b %H:%M")
    active_chip = (
        ' <span class="cs-eyebrow-badge">active sprint</span>'
        if is_active else ""
    )
    completion = row.get("completion_state")
    completion_chip = ""
    if completion in {"complete", "incomplete", "not_applicable"}:
        completion_chip = (
            f'<span class="argus-folder-completion is-{_html(completion)}">'
            f'{_html(completion.replace("_", " ").capitalize())}</span>'
        )
    else:
        completion_chip = "—"
    progress = _coverage_progress(row.get("coverage"))
    if progress is None:
        coverage_html = (
            '<span class="argus-folder-coverage-meta">'
            'Coverage unavailable</span>'
        )
    else:
        executed, total, bar_pct = progress
        progress_aria = (
            f"Folder execution progress: {executed} of {total} "
            f"planned items completed, {bar_pct} percent"
        )
        coverage_html = (
            '<div class="argus-folder-coverage-label">'
            f'<span class="argus-folder-count">{bar_pct}%</span>'
            f'<span class="argus-folder-coverage-meta">'
            f'{executed}/{total} completed</span></div>'
            '<div class="argus-folder-bar" role="progressbar" '
            f'aria-label="{_html(progress_aria)}" aria-valuemin="0" '
            f'aria-valuemax="100" aria-valuenow="{bar_pct}">'
            f'<span class="argus-folder-bar-fill" style="width:{bar_pct}%"></span>'
            '</div>'
        )
    safe_rel_url = _html(urllib.parse.quote(rel_url, safe="/"))
    row_label = (
        f"Open audit report for {name}, {audits} audits"
    )
    relative_time = _timestamp_markup(
        mtime,
        rel_label,
        css_class=f"argus-folder-time-rel {fresh_cls}",
    )
    searchable_name = " ".join(
        dict.fromkeys((str(name), str(row["name"])))
    )
    prefetch = (
        ' data-argus-prefetch="true"'
        if row.get("prefetch")
        else ""
    )
    return (
        f'<tr class="argus-folder-row{" is-active" if is_active else ""}" '
        f'data-argus-row-label="{_html(row_label)}" '
        f'data-argus-report-search="{_html(searchable_name)}" '
        f'data-argus-report-timestamp-ms="{max(0, round(mtime * 1000))}"'
        f'{prefetch}>'
        '<td><div class="argus-folder-title">'
        f'<a class="argus-folder-name" href="{safe_rel_url}">'
        f'{_html(name)}</a>{active_chip}</div>'
        f'<div class="argus-folder-description" title="{_html(abs_label)}">'
        f'Updated {relative_time}</div></td>'
        f'<td class="argus-num">{audits:d}</td>'
        f'<td><div class="argus-folder-coverage">{coverage_html}</div></td>'
        f'<td>{completion_chip}</td>'
        '<td><div class="argus-folder-actions">'
        f'<a class="cs-btn cs-btn-primary" href="{safe_rel_url}" '
        f'aria-label="Open {_html(name)} report">Open</a>'
        '<a class="cs-btn" '
        f'href="{_html(urllib.parse.quote(row["name"], safe=""))}/review" '
        f'aria-label="Review {_html(name)}">Review</a></div></td>'
        '</tr>'
    )


def _html(s: object) -> str:
    """Tiny escape — same shape as report._html_escape but kept local so this script has no transitive dependency on report.py."""
    import html as _h
    return _h.escape(str(s) if s is not None else "", quote=True)


@dataclass(frozen=True)
class Mount:
    """One team dashboard mounted beneath ``/<slug>/``."""

    slug: str
    name: str
    out_dir: Path
    sections: tuple[str, ...] = field(default_factory=tuple)


_MOUNTS: dict[str, Mount] = {}
_HUB_UI_CONTEXT = DEFAULT_UI_CONTEXT
_RENDER_LOCK = threading.Lock()
_DEFAULT_FOLDER_SECTIONS = list(_FOLDER_SECTIONS)
_DEFAULT_SECTION_ORDER = list(_SECTION_ORDER)


def configure_mounts(
    mounts: list[Mount],
    ui_context: ReportUiContext = DEFAULT_UI_CONTEXT,
) -> None:
    """Configure local hub mounts once at server startup."""
    global _MOUNTS, _HUB_UI_CONTEXT
    _MOUNTS = {mount.slug: mount for mount in mounts}
    _HUB_UI_CONTEXT = ui_context


def _apply_sections(labels: tuple[str, ...]) -> None:
    global _FOLDER_SECTIONS, _SECTION_ORDER
    _FOLDER_SECTIONS = list(_DEFAULT_FOLDER_SECTIONS)
    _SECTION_ORDER = list(_DEFAULT_SECTION_ORDER)
    configure_sections(list(labels))


def _mount_ui_context(mount: Mount) -> ReportUiContext:
    return ReportUiContext(
        dashboard_name=mount.name,
        dashboard_slug=mount.slug,
        teams=tuple(
            TeamLink(slug=item.slug, name=item.name)
            for item in _MOUNTS.values()
        ),
        docs_url=_HUB_UI_CONTEXT.docs_url,
        ticket_template_url=_HUB_UI_CONTEXT.ticket_template_url,
        asset_base_path=_HUB_UI_CONTEXT.asset_base_path,
        team_base_path=_HUB_UI_CONTEXT.team_base_path,
    )


def _render_dashboard_index(mount: Mount) -> str:
    """Render one mounted dashboard without mutating UI presentation state."""
    with _RENDER_LOCK:
        _apply_sections(mount.sections)
        return _render_index(
            mount.out_dir,
            ui_context=_mount_ui_context(mount),
        )


def _render_team_hub(
    teams: tuple[TeamLink, ...],
    ui_context: ReportUiContext,
    *,
    summaries: dict[str, dict[str, object]] | None = None,
) -> str:
    """Render the deterministic root Teams page."""
    from . import html_report as _html_report

    cards: list[str] = []
    summaries = summaries or {}
    for team in teams:
        summary = summaries.get(team.slug, {})
        folders = summary.get("folders")
        audits = summary.get("audits")
        refreshed = summary.get("refreshed")
        metrics = ""
        if isinstance(folders, int) and isinstance(audits, int):
            metrics = (
                '<div class="kv"><span>Audit folders</span>'
                f'<strong>{folders}</strong></div>'
                '<div class="kv"><span>Audits</span>'
                f'<strong>{audits:,}</strong></div>'
            )
        if isinstance(refreshed, str):
            metrics += (
                '<div class="kv"><span>Last refresh</span>'
                f'<strong>{_html(refreshed)}</strong></div>'
            )
        team_href = _html_report._team_href(ui_context, team.slug)
        cards.append(
            f'<section class="cs-container" id="{_html(team.slug)}">'
            '<div class="cs-container-header"><div>'
            f'<h2>{_html(team.name)}</h2></div></div>'
            f'<div class="cs-card-body">{metrics or "<p>Team audit dashboard</p>"}'
            '<div class="argus-team-live-status" role="status" '
            'aria-live="polite" hidden data-argus-team-status '
            f'data-status-url="{team_href}index.json">'
            '<span class="argus-team-spinner" aria-hidden="true"></span>'
            '<span data-argus-team-status-text>Audit running</span>'
            '</div></div>'
            '<div class="cs-card-foot">'
            f'<a class="cs-btn cs-btn-primary" '
            f'href="{team_href}">'
            'Open dashboard</a></div></section>'
        )
    hub_context = ReportUiContext(
        dashboard_name="All teams",
        teams=teams,
        docs_url=ui_context.docs_url,
        ticket_template_url=ui_context.ticket_template_url,
        asset_base_path=ui_context.asset_base_path,
        team_base_path=ui_context.team_base_path,
    )
    body = (
        '<nav class="cs-crumbs" aria-label="Breadcrumbs">'
        '<span>Teams</span></nav>'
        '<section class="cs-header"><div>'
        '<span class="page-eyebrow">ARGUS</span>'
        f'<h1>Teams<span class="cs-counter">({len(teams)})</span></h1>'
        '<p>Quality audit dashboards.</p></div></section>'
        f'<div class="cs-cards">{"".join(cards)}</div>'
    )
    return (
        '<!DOCTYPE html>\n<html lang="en" data-theme="light"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>Teams — ARGUS</title>'
        f'{_html_report._cs_head_extra(hub_context)}'
        f'{_html_report._cs_page_css(hub_context)}'
        '<style>'
        '.argus-team-live-status{display:flex;align-items:center;gap:8px;'
        'margin-top:8px;color:var(--ui-color-text-status-info);'
        'font-weight:700}.argus-team-live-status[hidden]{display:none}'
        '.argus-team-spinner{width:14px;height:14px;box-sizing:border-box;'
        'border:2px solid var(--ui-color-border-divider-default);'
        'border-top-color:var(--ui-color-text-status-info);'
        'border-radius:50%;animation:argus-team-spin .8s linear infinite}'
        '@keyframes argus-team-spin{to{transform:rotate(360deg)}}'
        '@media (prefers-reduced-motion:reduce){'
        '.argus-team-spinner{animation-duration:2s}}'
        '</style></head><body>'
        f'{_html_report._cs_topnav("/", hub_context)}'
        '<div class="application-layout" id="application-layout">'
        f'{_html_report._cs_sidenav("/", "teams", False, hub_context, hub_home=True)}'
        f'<main class="argus-main main-content">{body}</main></div>'
        '<script>'
        '(function(){'
        'const nodes=[...document.querySelectorAll("[data-argus-team-status]")];'
        'const active=new Set(["launching","preparing","running","auditing",'
        '"publishing"]);'
        'function label(item){'
        'const phase=String(item.phase||item.state||"running").toLowerCase();'
        'if(phase==="publishing")return "Publishing report";'
        'if(phase==="launching")return "Launching audit";'
        'if(phase==="stopped")return "Audit stopped";'
        'const done=Number(item.completed),total=Number(item.total);'
        'if(Number.isFinite(done)&&Number.isFinite(total)&&total>0)'
        'return `Audit running · ${Math.max(0,done)}/${Math.max(0,total)}`;'
        'return phase==="preparing"?"Preparing audit":"Audit running";'
        '}'
        'async function refresh(node){'
        'try{'
        'const response=await fetch(node.dataset.statusUrl,{'
        'cache:"no-store",credentials:"same-origin"});'
        'if(!response.ok)throw new Error(String(response.status));'
        'const payload=await response.json();'
        'const item=payload&&payload.active_audit;'
        'const state=String(item&&(item.state||item.phase)||"").toLowerCase();'
        'if(!item||!active.has(state)){node.hidden=true;return;}'
        'node.querySelector("[data-argus-team-status-text]").textContent=label(item);'
        'node.title=item.folder?`Folder: ${item.folder}`:"";'
        'node.hidden=false;'
        '}catch(_error){node.hidden=true;}'
        '}'
        'function refreshAll(){nodes.forEach(refresh);}'
        'refreshAll();setInterval(refreshAll,10000);'
        '})();'
        '</script>'
        f'<script>{_html_report._ARGUS_JS}</script></body></html>'
    )


def _render_local_hub() -> str:
    summaries: dict[str, dict[str, object]] = {}
    teams: list[TeamLink] = []
    now = dt.datetime.now().timestamp()
    for mount in _MOUNTS.values():
        teams.append(TeamLink(slug=mount.slug, name=mount.name))
        rows = _scan_folders(mount.out_dir)
        latest = max((row["mtime"] for row in rows), default=0)
        relative = _relative_phrase(max(0, now - latest)) if latest else "—"
        summaries[mount.slug] = {
            "folders": len(rows),
            "audits": sum(row["audits"] for row in rows),
            "refreshed": relative,
        }
    context = ReportUiContext(
        dashboard_name="All teams",
        teams=tuple(teams),
        docs_url=_HUB_UI_CONTEXT.docs_url,
        ticket_template_url=_HUB_UI_CONTEXT.ticket_template_url,
        asset_base_path=_HUB_UI_CONTEXT.asset_base_path,
        team_base_path=_HUB_UI_CONTEXT.team_base_path,
    )
    return _render_team_hub(
        tuple(teams),
        context,
        summaries=summaries,
    )


def _request_header(request_headers, name: str) -> str:
    try:
        return request_headers.get(name) or ""
    except AttributeError:
        return ""


def _accepts_gzip(request_headers) -> bool:
    """Return whether HTTP content negotiation permits gzip."""
    codings: dict[str, float] = {}
    for item in _request_header(
        request_headers,
        "Accept-Encoding",
    ).split(","):
        parts = [part.strip() for part in item.split(";")]
        coding = parts[0].lower()
        if not coding:
            continue
        quality = 1.0
        for parameter in parts[1:]:
            key, separator, value = parameter.partition("=")
            if separator and key.strip().lower() == "q":
                try:
                    quality = float(value.strip())
                except ValueError:
                    quality = 0.0
        codings[coding] = quality
    return codings.get("gzip", codings.get("*", 0.0)) > 0.0


def _etag_matches(request_headers, etag: str) -> bool:
    wanted = {
        tag.strip()
        for tag in _request_header(
            request_headers,
            "If-None-Match",
        ).split(",")
        if tag.strip()
    }
    return "*" in wanted or etag in wanted


def _send_html(
    handler,
    html: str,
    status: int = 200,
    *,
    head_only: bool = False,
) -> None:
    data = html.encode("utf-8")
    request_headers = getattr(handler, "headers", {})
    compressible = len(data) >= _GZIP_MIN_BYTES
    use_gzip = compressible and _accepts_gzip(request_headers)
    digest = hashlib.sha256(data).hexdigest()[:16]
    representation = "gzip" if use_gzip else "identity"
    etag = f'"{digest}-{representation}"'
    cache_control = "no-cache"
    if status == 200 and _etag_matches(request_headers, etag):
        handler.send_response(304)
        handler.send_header("ETag", etag)
        handler.send_header("Cache-Control", cache_control)
        handler.send_header("Vary", "Accept-Encoding")
        if use_gzip:
            handler.send_header("Content-Encoding", "gzip")
        handler.end_headers()
        return
    body = _gzipped_bytes(data, digest) if use_gzip else data
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("ETag", etag)
    handler.send_header("Cache-Control", cache_control)
    handler.send_header("Vary", "Accept-Encoding")
    if use_gzip:
        handler.send_header("Content-Encoding", "gzip")
    handler.end_headers()
    if not head_only:
        handler.wfile.write(body)


def _hub_route(
    handler,
    clean_path: str,
    *,
    head_only: bool = False,
) -> bool:
    """Handle hub routes or retarget the static handler to a mount."""
    if clean_path in ("/", "/index.html"):
        _send_html(
            handler,
            _render_local_hub(),
            head_only=head_only,
        )
        return True
    slug, separator, rest = clean_path.lstrip("/").partition("/")
    mount = _MOUNTS.get(slug)
    if mount is None:
        handler.send_error(404, "Not Found")
        return True
    if not separator:
        handler.send_response(301)
        handler.send_header("Location", f"/{slug}/")
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return True
    if rest in ("", "index.html"):
        _send_html(
            handler,
            _render_dashboard_index(mount),
            head_only=head_only,
        )
        return True
    if any(part in {"", ".", ".."} for part in Path(rest).parts):
        handler.send_error(404, "Not Found")
        return True
    query = handler.path.split("?", 1)[1] if "?" in handler.path else None
    handler.directory = str(mount.out_dir)
    handler.path = "/" + urllib.parse.quote(
        rest,
        safe="/",
    ) + (f"?{query}" if query else "")
    return False


_COMPRESSIBLE_PREFIXES = (
    "text/",
    "application/json",
    "application/javascript",
    "image/svg+xml",
)
_GZIP_MIN_BYTES = 1024
_GZIP_CACHE: dict[tuple[object, ...], bytes] = {}
_GZIP_CACHE_LOCK = threading.Lock()
_GZIP_CACHE_MAX_ENTRIES = 64
_GZIP_CACHE_MAX_BYTES = 64 * 1024 * 1024
_GZIP_CACHE_BYTES = 0


def _cached_gzip(
    key: tuple[object, ...],
    source: bytes,
    *,
    stale_prefix: tuple[object, ...] | None = None,
) -> bytes:
    """Compress once and retain a bounded least-recently-used cache."""
    global _GZIP_CACHE_BYTES
    with _GZIP_CACHE_LOCK:
        cached = _GZIP_CACHE.pop(key, None)
        if cached is not None:
            _GZIP_CACHE[key] = cached
            return cached
    body = gzip.compress(source, compresslevel=6)
    with _GZIP_CACHE_LOCK:
        cached = _GZIP_CACHE.pop(key, None)
        if cached is not None:
            _GZIP_CACHE[key] = cached
            return cached
        if stale_prefix is not None:
            for stale in [
                item
                for item in _GZIP_CACHE
                if item[:len(stale_prefix)] == stale_prefix
            ]:
                _GZIP_CACHE_BYTES -= len(_GZIP_CACHE.pop(stale))
        if len(body) > _GZIP_CACHE_MAX_BYTES:
            return body
        while _GZIP_CACHE and (
            len(_GZIP_CACHE) >= _GZIP_CACHE_MAX_ENTRIES
            or _GZIP_CACHE_BYTES + len(body) > _GZIP_CACHE_MAX_BYTES
        ):
            oldest = next(iter(_GZIP_CACHE))
            _GZIP_CACHE_BYTES -= len(_GZIP_CACHE.pop(oldest))
        _GZIP_CACHE[key] = body
        _GZIP_CACHE_BYTES += len(body)
    return body


def _gzipped_bytes(source: bytes, digest: str) -> bytes:
    return _cached_gzip(
        ("body", digest, len(source)),
        source,
    )


def _gzipped(fs_path: str, stat: os.stat_result) -> bytes:
    with open(fs_path, "rb") as handle:
        source = handle.read()
    return _cached_gzip(
        ("file", fs_path, stat.st_mtime_ns, stat.st_size),
        source,
        stale_prefix=("file", fs_path),
    )


# Apply browser protections consistently to static reports and local APIs.
_SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("Content-Security-Policy",
     "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
     "script-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; "
     "frame-ancestors 'none'"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
)


class _ArgusHandler(http.server.SimpleHTTPRequestHandler):
    """Static file handler with one extension: GET / returns the auto-built index page."""

    protocol_version = "HTTP/1.1"
    out_dir_path: Path
    ui_context: ReportUiContext = DEFAULT_UI_CONTEXT
    local_api = None

    _PRIVATE_SUFFIXES = (
        ".sqlite", ".sqlite-wal", ".sqlite-shm", ".sqlite-journal", ".jsonl",
    )

    def end_headers(self):
        for name, value in _SECURITY_HEADERS:
            self.send_header(name, value)
        super().end_headers()

    def list_directory(self, path):
        """Never list directories: they hold metadata and internal artefacts."""
        del path
        self.send_error(403, "Forbidden")
        return None

    def do_GET(self):
        self._handle_request(head_only=False)

    def do_HEAD(self):
        self._handle_request(head_only=True)

    def _valid_local_host(self) -> bool:
        port = self.server.server_port
        return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _send_json(self, status: int, value: dict, *, head_only: bool = False) -> None:
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def do_POST(self):
        if not self._valid_local_host() or self.local_api is None:
            self._send_json(403, {"error": "Use the local report server."})
            return
        origin = f"http://{self.headers['Host']}"
        if (self.headers.get("Origin") != origin
                or self.headers.get("X-Argus-Request") != "1"
                or self.headers.get_content_type() != "application/json"):
            self._send_json(403, {"error": "Submit changes from the local report page."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 131072:
                raise ValueError("Invalid request size.")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("Send a JSON object.")
        except (ValueError, TypeError):
            self._send_json(400, {"error": "Invalid request."})
            return
        self._dispatch_api("POST", body)

    def _dispatch_api(self, method: str, body: dict, *, head_only: bool = False) -> None:
        from argus.operations.review_service import ReviewError
        from argus.operations.rule_service import RuleError

        parsed = urllib.parse.urlsplit(self.path)
        try:
            result = self.local_api.handle(
                method, urllib.parse.unquote(parsed.path), body,
                {key: values[0] for key, values in urllib.parse.parse_qs(parsed.query).items()},
                self.headers.get("Idempotency-Key", ""),
            )
            self._send_json(200, result, head_only=head_only)
        except (ReviewError, RuleError) as exc:
            self._send_json(exc.status, {"error": str(exc)}, head_only=head_only)
        except (ValueError, TypeError):
            self._send_json(400, {"error": "Invalid request."}, head_only=head_only)
        except Exception:
            log.exception("Local review/rules request failed")
            self._send_json(500, {"error": "The local operation failed. Check the server log."}, head_only=head_only)

    def _local_page(self, clean_path: str, *, head_only: bool) -> bool:
        if self.local_api is None:
            return False
        parts = clean_path.strip("/").split("/")
        if self.local_api.hub:
            if not parts or parts[0] not in self.local_api.roots:
                return False
            team, parts = parts[0], parts[1:]
        else:
            team = next(iter(self.local_api.roots))
        if parts == ["rules"]:
            from argus.reporting.rules_page import render_page
            context = ReportUiContext(
                dashboard_slug=team, dashboard_name=self.local_api.settings[team].general.team_name or team,
                teams=tuple(TeamLink(slug, current.general.team_name or slug)
                            for slug, current in self.local_api.settings.items()) if self.local_api.hub else (),
                docs_url=self.local_api.settings[team].general.docs_url,
            )
            report_root = f"/{team}/" if self.local_api.hub else "/"
            _send_html(self, render_page(team, "", ui_context=context, report_root=report_root), head_only=head_only)
            return True
        if len(parts) == 2 and parts[1] in {"review", "review.html"}:
            from argus.reporting.review_data import render_page
            from argus.operations.review_service import ReviewError
            try:
                self.local_api.reviews[team].data._folder(team, parts[0])
            except ReviewError:
                self.send_error(404, "Not Found")
                return True
            _send_html(self, render_page(team, parts[0], ""), head_only=head_only)
            return True
        return False

    def _handle_request(self, *, head_only: bool) -> None:
        if not self._valid_local_host():
            self.send_error(403, "Use the loopback report address")
            return
        clean_path = urllib.parse.unquote(self.path.split("?", 1)[0])
        if self.local_api is not None and (clean_path == "/session" or clean_path.startswith("/teams/")):
            self._dispatch_api("GET", {}, head_only=head_only)
            return
        if self.local_api is not None and clean_path.startswith("/_review_assets/"):
            from argus.operations.review_service import ReviewError
            parts = clean_path.strip("/").split("/")
            try:
                if len(parts) != 3:
                    raise ReviewError(404, "Unknown review evidence.")
                path = self.local_api.asset_path(parts[1], parts[2])
            except ReviewError:
                self.send_error(404, "Not Found")
                return
            self._send_file(str(path), self.headers, head_only=head_only,
                            cache_control="private, max-age=31536000, immutable")
            return
        if self._local_page(clean_path, head_only=head_only):
            return
        if clean_path.lower().endswith(_ArgusHandler._PRIVATE_SUFFIXES):
            self.send_error(404, "Not Found")
            return
        if clean_path == "/healthz":
            data = json.dumps({"ok": True, "service": "argus"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if not head_only:
                self.wfile.write(data)
            return
        if clean_path.startswith("/_assets/"):
            asset = static_assets.resolve(clean_path)
            if asset is None:
                self.send_error(404, "Not Found")
                return
            self._send_file(
                str(asset.path),
                self.headers,
                ctype=asset.content_type,
                cache_control="public, max-age=31536000, immutable",
                head_only=head_only,
            )
            return
        if _MOUNTS:
            if _hub_route(
                self,
                clean_path,
                head_only=head_only,
            ):
                return
            return self._serve_file(head_only=head_only)
        if clean_path in ("/", "/index.html"):
            _send_html(
                self,
                _render_index(
                    self.out_dir_path,
                    ui_context=getattr(
                        self,
                        "ui_context",
                        DEFAULT_UI_CONTEXT,
                    ),
                ),
                head_only=head_only,
            )
            return
        return self._serve_file(head_only=head_only)

    def _serve_file(self, *, head_only: bool = False):
        fs_path = self.translate_path(self.path)
        # translate_path() drops `#`/`?` suffixes and percent-decodes, so the
        # deny list has to be applied to the resolved name, not the raw target.
        if os.path.basename(fs_path).lower().endswith(
                _ArgusHandler._PRIVATE_SUFFIXES):
            self.send_error(404, "Not Found")
            return
        if not os.path.isfile(fs_path):
            if head_only:
                return super().do_HEAD()
            return super().do_GET()
        self._send_file(
            fs_path,
            self.headers,
            head_only=head_only,
        )

    def _send_file(
        self,
        fs_path: str,
        request_headers,
        *,
        ctype: str | None = None,
        cache_control: str | None = None,
        head_only: bool = False,
    ) -> None:
        try:
            stat = os.stat(fs_path)
        except OSError:
            self.send_error(404, "Not Found")
            return
        if ctype is None:
            ctype = (
                mimetypes.guess_type(fs_path)[0]
                or "application/octet-stream"
            )
            if ctype.startswith("text/") and "charset" not in ctype:
                ctype += "; charset=utf-8"
        compressible = ctype.startswith(_COMPRESSIBLE_PREFIXES)
        if cache_control is None:
            cache_control = (
                "no-cache"
                if compressible
                else "public, max-age=86400"
            )
        use_gzip = (
            compressible
            and stat.st_size >= _GZIP_MIN_BYTES
            and _accepts_gzip(request_headers)
        )
        representation = "gzip" if use_gzip else "identity"
        etag = (
            f'"{stat.st_mtime_ns:x}-{stat.st_size:x}-'
            f'{representation}"'
        )
        if _etag_matches(request_headers, etag):
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", cache_control)
            self.send_header(
                "Last-Modified",
                self.date_time_string(int(stat.st_mtime)),
            )
            if compressible:
                self.send_header("Vary", "Accept-Encoding")
            if use_gzip:
                self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            return
        body = _gzipped(fs_path, stat) if use_gzip else None
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header(
            "Content-Length",
            str(len(body) if body is not None else stat.st_size),
        )
        self.send_header(
            "Last-Modified",
            self.date_time_string(int(stat.st_mtime)),
        )
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", cache_control)
        if compressible:
            self.send_header("Vary", "Accept-Encoding")
        if body is not None:
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        if head_only:
            return
        if body is not None:
            self.wfile.write(body)
        else:
            with open(fs_path, "rb") as handle:
                shutil.copyfileobj(handle, self.wfile, 256 * 1024)

    def log_request(self, code="-", size="-"):
        try:
            status = int(code)
        except (TypeError, ValueError):
            status = 0
        if 200 <= status < 400:
            return
        client = self.client_address[0] if self.client_address else "-"
        sys.stderr.write(
            f"[ARGUS] http status={code} path={self.path!r} "
            f"client={client} size={size}\n"
        )

    def log_message(self, fmt, *args):
        return


def _detect_hostname() -> str:
    """Best-effort fully-qualified hostname for display."""
    try:
        return socket.getfqdn()
    except Exception:
        return socket.gethostname()


def serve(
    out_dir: Path,
    port: int,
    host: str = "127.0.0.1",
    *,
    ui_context: ReportUiContext = DEFAULT_UI_CONTEXT,
    settings: argus_config.ArgusConfig | None = None,
) -> None:
    """Bind to 127.0.0.1:port and serve out_dir as static files plus the auto-index at /."""
    if not out_dir.exists():
        print(f"[ARGUS] error: output dir does not exist: {out_dir}",
              file=sys.stderr)
        sys.exit(1)

    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("The personal report server must bind to 127.0.0.1 or localhost.")
    from argus.operations.local_api import LocalApi
    settings = settings or argus_config.load()
    roots = {mount.slug: mount.out_dir for mount in _MOUNTS.values()} if _MOUNTS else {
        ui_context.dashboard_slug or settings.general.profile: out_dir.resolve(),
    }
    _ArgusHandler.local_api = LocalApi(settings, roots, hub=bool(_MOUNTS))
    handler = functools.partial(_ArgusHandler, directory=str(out_dir))
    _ArgusHandler.out_dir_path = out_dir
    _ArgusHandler.ui_context = ui_context

    http.server.ThreadingHTTPServer.allow_reuse_address = True

    try:
        httpd = http.server.ThreadingHTTPServer((host, port), handler)
    except OSError as e:
        print(f"[ARGUS] could not bind {host}:{port}: {e}",
              file=sys.stderr)
        print(f"[ARGUS] try a different port: --port {port + 1}",
              file=sys.stderr)
        sys.exit(2)

    banner = f"""
ARGUS — local report server
  serving: {out_dir}
  address: http://{host}:{port}/
  Stop the server with Ctrl-C.
"""
    print(banner, file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[ARGUS] server stopped.", file=sys.stderr)
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Serve ARGUS reports over HTTP for laptop access.")
    parser.add_argument(
        "--port", type=int, default=None,
        help="port to bind (default: server.port from the profile).")
    parser.add_argument(
        "--out-dir", type=Path, default=None,
        help="output dir to serve (default: extractor.out_dir from config.toml).")
    parser.add_argument(
        "--config", type=Path, default=None,
        help="path to config.toml (default: ./config.toml).")
    parser.add_argument(
        "--hub",
        action="store_true",
        help="serve every configured team dashboard on one port",
    )
    parser.add_argument(
        "--mount",
        action="append",
        default=[],
        metavar="PROFILE[=OUT_DIR]",
        help="with --hub, mount a profile and optional output directory",
    )
    args = parser.parse_args(argv)

    settings = argus_config.load(args.config)
    runtime.configure(settings)
    ui_context = ui_context_from_settings(settings)
    if args.hub:
        mounts = _build_mounts(
            args.mount or list(settings.hub.mounts),
        )
        if not mounts:
            print(
                "[ARGUS] error: no dashboard has an output dir to serve",
                file=sys.stderr,
            )
            return 1
        teams = tuple(
            TeamLink(slug=mount.slug, name=mount.name)
            for mount in mounts
        )
        configure_mounts(
            mounts,
            ReportUiContext(
                dashboard_name="All teams",
                teams=teams,
                docs_url=ui_context.docs_url,
                ticket_template_url=ui_context.ticket_template_url,
                asset_base_path=ui_context.asset_base_path,
            ),
        )
        for mount in mounts:
            print(
                f"[ARGUS] hub mount /{mount.slug}/ -> "
                f"{mount.out_dir} ({mount.name})",
                file=sys.stderr,
            )
        serve(
            mounts[0].out_dir,
            args.port if args.port is not None else settings.hub.port,
            settings.server.host,
            ui_context=ui_context,
            settings=settings,
        )
        return 0
    out_dir = args.out_dir or settings.extractor.out_dir
    out_dir = Path(out_dir).resolve()
    serve(
        out_dir,
        args.port if args.port is not None else settings.server.port,
        settings.server.host,
        ui_context=ui_context,
        settings=settings,
    )
    return 0


def _build_mounts(specs: list[str]) -> list[Mount]:
    """Resolve local hub profile declarations."""
    from argus.config.loader import available_profiles

    requested = specs or available_profiles()
    mounts: list[Mount] = []
    for spec in requested:
        slug, _separator, explicit = spec.partition("=")
        try:
            profile = argus_config.load(profile=slug)
        except Exception as exc:
            print(
                f"[ARGUS] hub: skipping {slug!r}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            continue
        out_dir = (
            Path(explicit).expanduser().resolve()
            if explicit
            else Path(profile.extractor.out_dir).resolve()
        )
        if not out_dir.exists():
            print(
                f"[ARGUS] hub: skipping {slug!r}: "
                f"{out_dir} does not exist",
                file=sys.stderr,
            )
            continue
        mounts.append(
            Mount(
                slug=slug,
                name=(
                    profile.general.team_name
                    or slug.replace("-", " ").title()
                ),
                out_dir=out_dir,
                sections=tuple(profile.folders.dashboard_sections),
            )
        )
    return mounts


if __name__ == "__main__":
    sys.exit(main())
