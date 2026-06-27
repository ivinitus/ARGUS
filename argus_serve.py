"""ARGUS — local HTTP server for viewing audit reports from a Cloud Desktop.

Static `_argus.html` files live under `output/<folder>/_argus.html`. On a
Cloud Desktop those files aren't reachable from your laptop browser — you
need an HTTP server bound to a port + an SSH tunnel from your laptop.

This script does that:

  1. Picks a port (default 2405) on 127.0.0.1.
  2. Builds an index page at `/` that lists every folder under `output/`
     with its `_argus.html` link, audit count, last-modified time.
  3. Starts a stdlib `http.server` rooted at `output/` so the existing
     `_argus.html` + screenshots all work without copying anything.
  4. Prints the exact SSH-tunnel command to paste on your laptop.

Usage:

    .venv/bin/python argus_serve.py
    # then on your laptop:
    #   ssh -L 2405:localhost:2405 <dev-desk-hostname>
    # then open http://localhost:2405 in your laptop browser

Flags:
    --port N        bind to port N (default 2405)
    --out-dir PATH  serve a different output dir (default: from config.toml)

Stops on Ctrl-C. No background process, no daemonisation, no install
required. Designed to be the lowest-friction path from "audits exist on
the dev desk" to "I can click through them on my laptop".
"""
from __future__ import annotations

import argparse
import datetime as dt
import functools
import http.server
import json
import socket
import socketserver
import sys
from pathlib import Path

import argus_control
import config as argus_config


_INDEX_CSS = """
/* ARGUS — index page styles.
   Aesthetic direction: a "watchful eye" interpretation of the
   compliance dashboard. Mythological ARGUS had 100 eyes that never
   all closed at once; we lean into that with a single live indicator
   in the wordmark, monospace numerals throughout (Bloomberg-terminal
   reading), and a density bar that shows audit volume at a glance.
   Cloudscape palette preserved; we extend it with semantic freshness
   tokens for timestamps. No external dependencies. */
:root {
  --argus-navy: #232f3e;
  --argus-navy-deep: #16202c;
  --argus-bg: #fafafa;
  --argus-surface: #ffffff;
  --argus-border: #e9ebed;
  --argus-border-strong: #d5dbe0;
  --argus-text: #16191f;
  --argus-text-2: #5f6b7a;
  --argus-text-3: #8b96a3;
  --argus-blue: #0972d3;
  --argus-blue-soft: #e7f1fb;
  --argus-green: #037f0c;
  --argus-amber: #b25309;
  --argus-amber-soft: #fff4dc;
  --argus-red: #d13212;
  --argus-row-hover: #f7f9fb;
  --argus-shadow-card: 0 1px 0 rgba(35,47,62,.04), 0 0 0 1px var(--argus-border);
  --argus-mono: ui-monospace, "SFMono-Regular", "Menlo", "Consolas",
                "Liberation Mono", monospace;
  --argus-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                "Helvetica Neue", Arial, sans-serif;
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body {
  margin: 0;
  font-family: var(--argus-sans);
  font-size: 14px;
  line-height: 1.45;
  background: var(--argus-bg);
  color: var(--argus-text);
  /* Subtle grid background — a watcher's grid, very low contrast.
     Renders fast (single repeating-linear-gradient pair) and gives
     the page a faint "monitored" texture without screaming for
     attention. */
  background-image:
    linear-gradient(var(--argus-border) 1px, transparent 1px),
    linear-gradient(90deg, var(--argus-border) 1px, transparent 1px);
  background-size: 32px 32px;
  background-position: -1px -1px;
  background-attachment: fixed;
}
body::before {
  /* Soften the grid so it whispers rather than shouts. */
  content: "";
  position: fixed; inset: 0;
  background: linear-gradient(180deg,
                              rgba(250,250,250,.92) 0%,
                              rgba(250,250,250,.97) 100%);
  pointer-events: none;
  z-index: 0;
}
body > * { position: relative; z-index: 1; }

/* ============================================================== */
/* Header strip                                                    */
/* ============================================================== */
.argus-header {
  background: var(--argus-navy);
  background-image:
    radial-gradient(ellipse 600px 200px at 0% 0%,
                    rgba(9,114,211,.18) 0%, transparent 60%),
    linear-gradient(180deg, var(--argus-navy) 0%, var(--argus-navy-deep) 100%);
  color: #fff;
  padding: 18px 32px;
  border-bottom: 1px solid #000;
}
.argus-header-inner {
  max-width: 1180px;
  margin: 0 auto;
  display: flex;
  align-items: center;
  gap: 24px;
}
.argus-brand {
  display: flex;
  align-items: baseline;
  gap: 14px;
  text-decoration: none;
  color: inherit;
  outline: none;
}
.argus-brand:focus-visible {
  outline: 2px solid var(--argus-blue);
  outline-offset: 4px;
  border-radius: 2px;
}
.argus-logo {
  font-family: var(--argus-mono);
  font-weight: 700;
  font-size: 22px;
  letter-spacing: 3px;
  display: inline-flex;
  align-items: center;
  gap: 10px;
}
.argus-logo::before {
  /* The "open eye" — a live indicator that pulses gently. ARGUS
     watches; the dot reminds you the system is watching back. */
  content: "";
  display: inline-block;
  width: 8px; height: 8px;
  border-radius: 50%;
  background: #4ade80;
  box-shadow: 0 0 0 0 rgba(74,222,128,.7);
  animation: argus-watch 2.4s ease-out infinite;
}
@keyframes argus-watch {
  0%   { box-shadow: 0 0 0 0 rgba(74,222,128,.7); }
  60%  { box-shadow: 0 0 0 7px rgba(74,222,128,0); }
  100% { box-shadow: 0 0 0 0 rgba(74,222,128,0); }
}
.argus-brand:hover .argus-logo { opacity: .9; }
.argus-tagline {
  color: rgba(255,255,255,.55);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 1.5px;
  font-weight: 500;
  border-left: 1px solid rgba(255,255,255,.18);
  padding-left: 14px;
}
.argus-stats {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 28px;
}
.argus-stat {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 2px;
  min-width: 60px;
}
.argus-stat-value {
  font-family: var(--argus-mono);
  font-size: 20px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  color: #fff;
  line-height: 1;
}
.argus-stat-label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 1.2px;
  color: rgba(255,255,255,.55);
}
.argus-stat-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  font-family: var(--argus-mono);
  font-size: 11px;
  color: rgba(255,255,255,.65);
  border-left: 1px solid rgba(255,255,255,.18);
  padding-left: 16px;
  height: 32px;
}
.argus-stat-meta-icon {
  width: 6px; height: 6px;
  border-radius: 50%;
  background: rgba(255,255,255,.55);
}

/* ============================================================== */
/* Main column                                                     */
/* ============================================================== */
main {
  max-width: 1180px;
  margin: 28px auto 80px;
  padding: 0 32px;
}

/* ============================================================== */
/* Banner — auditing-now / stale                                   */
/* ============================================================== */
.banner {
  position: relative;
  background: var(--argus-surface);
  border: 1px solid var(--argus-border);
  border-left: 3px solid var(--argus-blue);
  border-radius: 6px;
  padding: 14px 18px;
  margin-bottom: 24px;
  display: flex;
  align-items: center;
  gap: 12px;
  box-shadow: 0 1px 0 rgba(35,47,62,.03);
  overflow: hidden;
}
.banner::before {
  /* A faint horizontal "scan" line — gives an active feel without
     animation jank. Pure CSS gradient; no repaint cost. */
  content: "";
  position: absolute;
  inset: 0;
  background: linear-gradient(90deg,
                              rgba(9,114,211,.04) 0%,
                              transparent 40%);
  pointer-events: none;
}
.banner.stale {
  border-left-color: var(--argus-amber);
  background: var(--argus-amber-soft);
}
.banner.stale::before {
  background: linear-gradient(90deg,
                              rgba(178,83,9,.05) 0%,
                              transparent 40%);
}
.banner .icon {
  font-size: 16px;
  line-height: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px; height: 28px;
  border-radius: 50%;
  background: var(--argus-blue-soft);
  flex-shrink: 0;
}
.banner.stale .icon {
  background: rgba(178,83,9,.12);
}
.banner .title {
  font-weight: 600;
  font-size: 13px;
  text-transform: uppercase;
  letter-spacing: .8px;
}
.banner .meta {
  color: var(--argus-text-2);
  font-size: 13px;
  font-family: var(--argus-mono);
}
.banner code {
  font-family: var(--argus-mono);
  background: rgba(35,47,62,.06);
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 12px;
}

/* ============================================================== */
/* Folder section + cards                                          */
/* ============================================================== */
.argus-section {
  margin-bottom: 32px;
}
.argus-section-head {
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 12px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--argus-border);
}
.argus-section-title {
  font-family: var(--argus-mono);
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 1.6px;
  color: var(--argus-text-2);
}
.argus-section-count {
  font-family: var(--argus-mono);
  font-size: 11px;
  color: var(--argus-text-3);
  font-variant-numeric: tabular-nums;
}
.argus-section-rule {
  flex: 1;
  height: 1px;
  background: var(--argus-border);
  align-self: center;
  margin-left: 4px;
}

.argus-folder-list {
  display: flex;
  flex-direction: column;
  gap: 0;
  background: var(--argus-surface);
  border: 1px solid var(--argus-border);
  border-radius: 8px;
  overflow: hidden;
  box-shadow: 0 1px 0 rgba(35,47,62,.04);
}
.argus-folder-row {
  display: grid;
  grid-template-columns: 4px 1fr 220px 140px;
  align-items: center;
  gap: 0;
  padding: 0;
  text-decoration: none;
  color: inherit;
  border-bottom: 1px solid var(--argus-border);
  position: relative;
  transition: background-color 80ms ease;
}
.argus-folder-row:last-child { border-bottom: none; }
.argus-folder-row:hover { background: var(--argus-row-hover); }
.argus-folder-row:focus-visible {
  outline: 2px solid var(--argus-blue);
  outline-offset: -2px;
  background: var(--argus-row-hover);
}

.argus-folder-rail {
  align-self: stretch;
  background: transparent;
}
.argus-folder-row.is-active .argus-folder-rail {
  background: var(--argus-blue);
  box-shadow: inset 0 0 0 1px rgba(9,114,211,.4);
}

.argus-folder-name-cell {
  padding: 14px 16px 14px 18px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.argus-folder-name {
  font-weight: 500;
  font-size: 14px;
  color: var(--argus-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  letter-spacing: -.1px;
}
.argus-folder-row:hover .argus-folder-name { color: var(--argus-blue); }
.argus-folder-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  color: var(--argus-text-3);
  font-family: var(--argus-mono);
}
.argus-folder-meta-active {
  color: var(--argus-blue);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 1px;
  font-size: 10px;
  background: var(--argus-blue-soft);
  padding: 2px 6px;
  border-radius: 3px;
  border: 1px solid rgba(9,114,211,.2);
}

.argus-folder-volume {
  padding: 14px 16px;
  display: flex;
  align-items: center;
  gap: 12px;
}
.argus-folder-count {
  font-family: var(--argus-mono);
  font-variant-numeric: tabular-nums;
  font-size: 14px;
  font-weight: 600;
  color: var(--argus-text);
  min-width: 44px;
  text-align: right;
}
.argus-folder-count-label {
  font-family: var(--argus-mono);
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 1px;
  color: var(--argus-text-3);
}
.argus-folder-bar {
  flex: 1;
  height: 4px;
  background: var(--argus-border);
  border-radius: 2px;
  overflow: hidden;
  position: relative;
}
.argus-folder-bar-fill {
  position: absolute;
  inset: 0 auto 0 0;
  background: linear-gradient(90deg,
                              var(--argus-blue) 0%,
                              #4d9ff0 100%);
  border-radius: 2px;
}
.argus-folder-row:hover .argus-folder-bar-fill {
  background: linear-gradient(90deg,
                              var(--argus-blue) 0%,
                              #1f8de8 100%);
}

.argus-folder-time {
  padding: 14px 18px 14px 12px;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 3px;
  font-family: var(--argus-mono);
  font-size: 12px;
}
.argus-folder-time-rel {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 8px;
  border-radius: 3px;
  font-size: 11px;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
}
.argus-folder-time-rel::before {
  content: "";
  width: 5px; height: 5px;
  border-radius: 50%;
  background: currentColor;
}
.argus-folder-time-rel.fresh-now    { color: var(--argus-green); background: #e8f5e9; }
.argus-folder-time-rel.fresh-recent { color: var(--argus-blue);  background: var(--argus-blue-soft); }
.argus-folder-time-rel.fresh-day    { color: var(--argus-text-2);background: #f1f3f5; }
.argus-folder-time-rel.fresh-stale  { color: var(--argus-text-3);background: #f7f8fa; }
.argus-folder-time-abs {
  font-size: 10px;
  color: var(--argus-text-3);
  font-variant-numeric: tabular-nums;
}

/* ============================================================== */
/* Empty state — stylized eyes                                     */
/* ============================================================== */
.argus-empty {
  text-align: center;
  background: var(--argus-surface);
  border: 1px solid var(--argus-border);
  border-radius: 12px;
  padding: 64px 32px 56px;
  box-shadow: 0 1px 0 rgba(35,47,62,.04);
}
.argus-empty-eyes {
  display: flex;
  justify-content: center;
  gap: 20px;
  margin-bottom: 28px;
}
.argus-empty-eye {
  width: 56px;
  height: 56px;
  border-radius: 50%;
  background: var(--argus-bg);
  border: 2px solid var(--argus-border-strong);
  display: flex;
  align-items: center;
  justify-content: center;
  position: relative;
}
.argus-empty-eye::after {
  content: "";
  width: 18px; height: 18px;
  border-radius: 50%;
  background: var(--argus-navy);
  position: relative;
}
.argus-empty-eye:nth-child(2)::after { transform: translateX(2px) translateY(1px); }
.argus-empty-eye:nth-child(3)::after { transform: translateX(-2px); }
.argus-empty-eye::before {
  content: "";
  position: absolute;
  inset: 14px 14px auto auto;
  width: 6px; height: 6px;
  border-radius: 50%;
  background: rgba(255,255,255,.95);
  z-index: 1;
}
.argus-empty-eye:nth-child(2)::before { transform: translateX(2px) translateY(1px); }
.argus-empty-eye:nth-child(3)::before { transform: translateX(-2px); }
.argus-empty h2 {
  font-family: var(--argus-mono);
  font-size: 13px;
  text-transform: uppercase;
  letter-spacing: 2px;
  font-weight: 600;
  margin: 0 0 12px;
  color: var(--argus-text);
}
.argus-empty p {
  margin: 0;
  color: var(--argus-text-2);
  font-size: 14px;
  max-width: 460px;
  margin: 0 auto 8px;
}
.argus-empty code {
  font-family: var(--argus-mono);
  background: var(--argus-bg);
  border: 1px solid var(--argus-border);
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 12px;
  color: var(--argus-text);
}

/* ============================================================== */
/* Mobile tolerance                                                */
/* ============================================================== */
@media (max-width: 720px) {
  .argus-header { padding: 16px 20px; }
  .argus-header-inner { gap: 16px; flex-wrap: wrap; }
  .argus-stats { width: 100%; gap: 20px; justify-content: flex-start;
                 padding-top: 12px; border-top: 1px solid rgba(255,255,255,.12); }
  .argus-stat-meta { display: none; }
  main { padding: 0 16px; margin-top: 16px; }
  .argus-folder-row {
    grid-template-columns: 4px 1fr 100px;
  }
  .argus-folder-volume { padding: 12px 12px; gap: 8px; }
  .argus-folder-bar { display: none; }
  .argus-folder-time { display: none; }
  .argus-folder-meta { flex-wrap: wrap; }
}
"""


def _human_time(ts: float) -> str:
    """Format a unix timestamp as relative-then-absolute, e.g.
    '23 May, 13:13 (2h ago)'. Renders cleanly in the index table."""
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
    rows: list[dict] = []
    if not out_dir.exists():
        return rows
    # Each immediate subfolder of out_dir is a "folder" in argus's
    # terminology (one test-management folder slug). _argus.html lives at the
    # root of that folder.
    for sub in sorted(out_dir.iterdir()):
        if not sub.is_dir():
            continue
        report_path = sub / "_argus.html"
        if not report_path.exists():
            continue
        # Count audits — recursive search for audit.json under sub.
        audit_count = sum(
            1 for p in sub.rglob("audit.json")
            if "_replay" not in p.parts
        )
        rows.append({
            "name": sub.name,
            "rel_url": f"{sub.name}/_argus.html",
            "audits": audit_count,
            "mtime": report_path.stat().st_mtime,
        })
    rows.sort(key=lambda r: -r["mtime"])  # newest first
    return rows


def _format_age(seconds: int) -> str:
    """Compact relative age for the banner. e.g. '3m', '1h 12m'."""
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    h, rem = divmod(seconds, 3600)
    return f"{h}h {rem // 60:02d}m"


# ---------------------------------------------------------------------------
# Index page helpers
# ---------------------------------------------------------------------------
import re as _re

# Map known folder-name prefixes to a section title. Order matters —
# the first matching prefix wins. "Other" is the fall-through bucket
# at the bottom for folders that don't match any prefix.
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

# Section render order. "Other" always sinks to the bottom. Targeted
# regression rises to the top because that's the active-sprint case
# operators look at most.
_SECTION_ORDER = [
    "Targeted regression",
    "Sanity testing",
    "Additional non-automated",
    "Loyalty Hub",
    "LOP testing",
    "Other",
]


def _classify_section(name: str) -> str:
    """Bucket a folder slug into a human section label."""
    lo = name.lower()
    for prefix, label in _FOLDER_SECTIONS:
        if lo.startswith(prefix):
            return label
    return "Other"


# Match DD-Month-YYYY embedded in folder slugs like
# "Targeted_regression_-_5_June_2026_WEB" so we can pick "the active
# sprint folder" — the newest dated entry within Targeted regression.
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
    """Map an age in seconds to a CSS class controlling timestamp colour.

    Tight buckets so a glance at the timestamps tells operators
    'this folder was audited in the last hour' (green) vs
    'this is from yesterday' (muted blue) vs 'over a week old'
    (greyed out).
    """
    if secs < 600:        # <10m
        return "fresh-now"
    if secs < 14400:      # <4h
        return "fresh-recent"
    if secs < 172800:     # <2d
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


def _render_in_progress_banner() -> str:
    """Render the 'AUDITING NOW' or 'stale audit state' banner.

    Pulls from argus_control.current_audit() — same state file the
    bot/cron agree on. Returns an empty string when no audit is
    running, so the index page is clean in steady state. Failures
    (state file unreadable, parse error) silently render nothing —
    the dashboard must never break because of a control-plane glitch.

    Trigger contract preserved: returns "" iff no banner should show.
    _render_index() reads the truthiness of this return to decide
    whether to emit the auto-refresh meta tag.
    """
    try:
        record = argus_control.current_audit()
    except Exception:
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
            'run <code>python argus_control.py</code> to inspect</span>'
            '</div>'
        )
    return (
        '<div class="banner" role="status">'
        '<span class="icon" aria-hidden="true">⏳</span>'
        '<span class="title">Auditing now</span>'
        f'<span class="meta"><code>{folder}</code> · '
        f'{n_keys} keys · started {age_str} ago</span>'
        '</div>'
    )


def _render_index(out_dir: Path) -> str:
    """Build the HTML index page listing all folders with reports.

    Layout (top → bottom):
      1. Header strip — wordmark + live-eye indicator + summary stats.
      2. In-progress banner — only when an audit is active.
      3. Folder list, grouped by section (Targeted regression first),
         with a per-row density bar and a freshness-coloured timestamp.
      4. Empty state — illustrated with stylised eyes if no folders
         have been audited yet.
    """
    rows = _scan_folders(out_dir)
    banner_html = _render_in_progress_banner()
    # Auto-refresh ONLY while an audit is in progress, so the banner
    # appears/disappears + the table mtime updates without an operator
    # reload. In steady state (no audit running, no stale state), the
    # page is static — no scroll-jump every 30s while reading findings.
    auto_refresh_html = (
        '<meta http-equiv="refresh" content="30">' if banner_html else ""
    )

    # ---- Header summary stats (computed once, rendered in <header>) ----
    total_folders = len(rows)
    total_audits = sum(r["audits"] for r in rows)
    if rows:
        latest_mtime = max(r["mtime"] for r in rows)
        secs_since_latest = max(0, dt.datetime.now().timestamp() - latest_mtime)
        last_refresh_label = _short_relative(secs_since_latest)
    else:
        last_refresh_label = "—"

    header_html = (
        '<header class="argus-header">'
        '<div class="argus-header-inner">'
        '<a href="/" class="argus-brand">'
        '<span class="argus-logo">ARGUS</span>'
        '<span class="argus-tagline">QS execution auditor</span>'
        '</a>'
        '<div class="argus-stats" aria-label="Index summary">'
        '<div class="argus-stat">'
        f'<span class="argus-stat-value">{total_folders:d}</span>'
        '<span class="argus-stat-label">Folders</span>'
        '</div>'
        '<div class="argus-stat">'
        f'<span class="argus-stat-value">{total_audits:,}</span>'
        '<span class="argus-stat-label">Audits</span>'
        '</div>'
        '<div class="argus-stat-meta" title="Most-recently regenerated folder report">'
        '<span class="argus-stat-meta-icon" aria-hidden="true"></span>'
        f'<span>last refresh {_html(last_refresh_label)}</span>'
        '</div>'
        '</div>'
        '</div>'
        '</header>'
    )

    # ---- Body: empty-state OR sectioned folder list -----------------
    if not rows:
        body = (
            '<main>'
            f'{banner_html}'
            '<div class="argus-empty" role="status">'
            '<div class="argus-empty-eyes" aria-hidden="true">'
            '<div class="argus-empty-eye"></div>'
            '<div class="argus-empty-eye"></div>'
            '<div class="argus-empty-eye"></div>'
            '</div>'
            '<h2>Nothing to watch yet</h2>'
            '<p>ARGUS has no audited folders to show.</p>'
            f'<p>Looked under <code>{_html(out_dir)}</code>. Run '
            '<code>argus.py --folder ...</code> or trigger '
            '<code>/argus run</code> from Chat to generate one.</p>'
            '</div>'
            '</main>'
        )
    else:
        # Find the "active sprint" — the newest dated Targeted
        # regression folder. Used to give exactly ONE row a left
        # rail accent so the operator's eye lands on the right
        # folder immediately. Tie-breaker on equal dates: the
        # most-recently-regenerated mtime wins (probably the one
        # cron just touched).
        active_name: str | None = None
        candidates = [
            (r, _parse_date_from_name(r["name"]))
            for r in rows
            if _classify_section(r["name"]) == "Targeted regression"
        ]
        candidates = [(r, d) for r, d in candidates if d is not None]
        if candidates:
            today = dt.date.today()
            past_or_today = [(r, d) for r, d in candidates if d <= today]
            if past_or_today:
                past_or_today.sort(
                    key=lambda rd: (rd[1], rd[0]["mtime"]), reverse=True)
                active_name = past_or_today[0][0]["name"]

        # Group rows by section. Within each section, keep the
        # incoming order (newest mtime first, set by _scan_folders).
        sections: dict[str, list[dict]] = {}
        for r in rows:
            sections.setdefault(_classify_section(r["name"]), []).append(r)

        # Largest audit count anywhere — used to scale the
        # density-bar widths so the busiest folder fills the bar.
        max_audits = max((r["audits"] for r in rows), default=1) or 1

        section_html_parts: list[str] = []
        for section_label in _SECTION_ORDER:
            section_rows = sections.get(section_label)
            if not section_rows:
                continue
            section_html_parts.append(
                _render_folder_section(
                    section_label, section_rows, max_audits, active_name))

        body = (
            '<main>'
            f'{banner_html}'
            + "".join(section_html_parts)
            + '</main>'
        )

    return (
        '<!DOCTYPE html>\n<html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'{auto_refresh_html}'
        '<title>ARGUS — index</title>'
        f'<style>{_INDEX_CSS}</style>'
        '</head><body>'
        + header_html
        + body
        + '</body></html>'
    )


def _render_folder_section(
    label: str,
    rows: list[dict],
    max_audits: int,
    active_name: str | None,
) -> str:
    """Render one section of the folder list (e.g. 'Targeted regression')
    with its sub-header and a card-row list."""
    rows_html = "".join(
        _render_folder_row(r, max_audits, r["name"] == active_name)
        for r in rows
    )
    return (
        '<section class="argus-section">'
        '<div class="argus-section-head">'
        f'<h2 class="argus-section-title">{_html(label)}</h2>'
        f'<span class="argus-section-count">{len(rows):d} folder'
        f'{"s" if len(rows) != 1 else ""}</span>'
        '<span class="argus-section-rule" aria-hidden="true"></span>'
        '</div>'
        '<div class="argus-folder-list" role="list">'
        f'{rows_html}'
        '</div>'
        '</section>'
    )


def _render_folder_row(
    row: dict,
    max_audits: int,
    is_active: bool,
) -> str:
    """Render one folder card-row: name + meta · density bar · timestamp."""
    name = row["name"]
    audits = row["audits"]
    mtime = row["mtime"]
    rel_url = row["rel_url"]
    bar_pct = max(2, int(audits / max_audits * 100)) if audits else 0
    secs = max(0, dt.datetime.now().timestamp() - mtime)
    fresh_cls = _freshness_class(secs)
    rel_label = _short_relative(secs)
    abs_label = dt.datetime.fromtimestamp(mtime).strftime("%d %b %H:%M")
    active_cls = " is-active" if is_active else ""
    active_chip = (
        '<span class="argus-folder-meta-active">active sprint</span>'
        if is_active else ""
    )
    return (
        f'<a class="argus-folder-row{active_cls}" href="/{rel_url}" role="listitem"'
        f' aria-label="Open audit report for {_html(name)}, {audits} audits, '
        f'updated {rel_label} ago">'
        '<span class="argus-folder-rail" aria-hidden="true"></span>'
        '<div class="argus-folder-name-cell">'
        f'<span class="argus-folder-name">{_html(name)}</span>'
        '<div class="argus-folder-meta">'
        f'{active_chip}'
        f'<span>{audits} audit{"s" if audits != 1 else ""}</span>'
        '</div>'
        '</div>'
        '<div class="argus-folder-volume" aria-hidden="true">'
        f'<span class="argus-folder-count">{audits:d}</span>'
        '<div class="argus-folder-bar">'
        f'<span class="argus-folder-bar-fill" style="width:{bar_pct}%"></span>'
        '</div>'
        '</div>'
        '<div class="argus-folder-time">'
        f'<span class="argus-folder-time-rel {fresh_cls}">{_html(rel_label)} ago</span>'
        f'<span class="argus-folder-time-abs">{_html(abs_label)}</span>'
        '</div>'
        '</a>'
    )


def _html(s: object) -> str:
    """Tiny escape — same shape as report._html_escape but kept local
    so this script has no transitive dependency on report.py."""
    import html as _h
    return _h.escape(str(s) if s is not None else "", quote=True)


class _ArgusHandler(http.server.SimpleHTTPRequestHandler):
    """Static file handler with one extension: GET / returns the
    auto-built index page (instead of the default directory listing).
    Everything else is the stdlib SimpleHTTPRequestHandler so paths
    inside `_argus.html` (relative to the folder dir) and screenshot
    `<img src>` references resolve correctly with no special-casing.
    """

    out_dir_path: Path  # set per-request via functools.partial

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            html = _render_index(self.out_dir_path)
            data = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        # Layer 2 audit history: <exec_dir>/audit.json.history/ is browsable
        # via the stdlib SimpleHTTPRequestHandler's auto directory listing,
        # and individual audit_<TS>.json files render as text/JSON in
        # browsers. Functional but ugly. TODO: render a small HTML index
        # for audit.json.history/ paths so reviewers see verdict + finding
        # counts without opening each JSON.
        return super().do_GET()

    def log_message(self, fmt, *args):
        # Suppress per-request noise; keep startup banner clean.
        return


def _detect_hostname() -> str:
    """Best-effort fully-qualified hostname for the SSH tunnel hint.
    Falls back to socket.gethostname() if FQDN resolution doesn't work."""
    try:
        return socket.getfqdn()
    except Exception:
        return socket.gethostname()


def serve(out_dir: Path, port: int) -> None:
    """Bind to 127.0.0.1:port and serve out_dir as static files plus
    the auto-index at /. Loops until Ctrl-C."""
    if not out_dir.exists():
        print(f"[ARGUS] error: output dir does not exist: {out_dir}",
              file=sys.stderr)
        sys.exit(1)

    handler = functools.partial(_ArgusHandler, directory=str(out_dir))
    # Inject out_dir_path onto the class so the / handler can read it.
    _ArgusHandler.out_dir_path = out_dir

    # Allow rapid restarts (TIME_WAIT after Ctrl-C otherwise blocks the
    # port for ~60s). ThreadingHTTPServer defaults to allow_reuse=False
    # so we override here rather than subclass.
    #
    # ThreadingHTTPServer (instead of TCPServer): each request handled in
    # its own thread. Matters for multi-viewer access — without it, a
    # large screenshot download by one viewer blocks every other viewer
    # until it finishes. Single-threaded was fine when the dashboard
    # was for one operator; with `tunnel create --allow posix:exampleappQSWeb`
    # opening it to a team, concurrent viewers hit the queue at every
    # PDF / image load. Stdlib supports this with one class swap; no new
    # dep, no framework change.
    http.server.ThreadingHTTPServer.allow_reuse_address = True

    try:
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError as e:
        print(f"[ARGUS] could not bind 127.0.0.1:{port}: {e}",
              file=sys.stderr)
        print(f"[ARGUS] try a different port: --port {port + 1}",
              file=sys.stderr)
        sys.exit(2)

    host = _detect_hostname()
    # Prefer the operator's stable secure tunnel URL when one is configured —
    # works from any laptop on SSO, no SSH needed. Falls back to the
    # SSH-tunnel hint when [notify].tunnel_url is empty (fresh setups).
    settings = argus_config.load()
    tunnel_url = (settings.notify.tunnel_url or "").strip()
    if tunnel_url:
        access_block = (
            f"  view from anywhere on SSO:\n\n"
            f"      {tunnel_url}\n"
        )
    else:
        access_block = (
            f"  To view from your laptop, open a NEW terminal on your laptop\n"
            f"  and run:\n\n"
            f"      ssh -L {port}:localhost:{port} {host}\n\n"
            f"  Then in your laptop browser:\n\n"
            f"      http://localhost:{port}/\n\n"
            f"  Tip: for a stable URL with no SSH, run\n"
            f"      `tunnel create {port} --name argus`\n"
            f"  and put the resulting URL in config.toml's [notify].tunnel_url.\n"
        )
    banner = f"""
================================================================
  ARGUS — local report server
================================================================
  serving:   {out_dir}
  bind:      http://127.0.0.1:{port}/   (this dev desk only)

{access_block}
  Stop the server with Ctrl-C.
================================================================
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
        "--port", type=int, default=2405,
        help="port to bind on 127.0.0.1 (default 2405).")
    parser.add_argument(
        "--out-dir", type=Path, default=None,
        help="output dir to serve (default: extractor.out_dir from config.toml).")
    parser.add_argument(
        "--config", type=Path, default=None,
        help="path to config.toml (default: ./config.toml).")
    args = parser.parse_args(argv)

    settings = argus_config.load(args.config)
    out_dir = args.out_dir or settings.extractor.out_dir
    out_dir = Path(out_dir).resolve()
    serve(out_dir, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
