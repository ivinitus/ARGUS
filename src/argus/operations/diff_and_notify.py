"""Diff a folder's audit state against the previous cron run."""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path
from typing import Any

from argus.core import atomic_io
from argus import config as argus_config
from argus.operations import notify
from argus.reporting import report as argus_report
from argus import runtime

_STATE_DIR = Path.home() / ".argus" / "last_seen"
_STATE_SCHEMA = 1


def configure(state_dir: Path) -> None:
    global _STATE_DIR
    _STATE_DIR = state_dir.expanduser() / "last_seen"


def _state_path_for(out_dir: Path) -> Path:
    """Map an absolute out_dir to its state file."""
    slug = out_dir.name or "_root"
    return _STATE_DIR / f"{slug}.json"


def _load_state(path: Path) -> dict[str, dict[str, Any]]:
    """Load prior state."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if raw.get("schema") != _STATE_SCHEMA:
        return {}
    by_key = raw.get("by_key")
    return by_key if isinstance(by_key, dict) else {}


def _write_state_atomic(path: Path,
                        by_key: dict[str, dict[str, Any]]) -> None:
    """Write state atomically (tempfile + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": _STATE_SCHEMA,
        "updated_at": _dt.datetime.now(_dt.timezone.utc)
                                  .isoformat(timespec="seconds"),
        "by_key": by_key,
    }
    try:
        atomic_io.atomic_write_json(path, payload)
    except OSError as e:
        sys.stderr.write(f"[diff_and_notify] state write failed: "
                         f"{type(e).__name__}: {e}\n")


def _build_current(summaries: list) -> dict[str, dict[str, Any]]:
    """Build the {key: {verdict, flagged}} snapshot from current scan."""
    out: dict[str, dict[str, Any]] = {}
    for s in summaries:
        out[s.key] = {
            "verdict": s.verdict,
            "flagged": argus_report._is_flagged(s),
        }
    return out


_SEV_RANK = {"high": 0, "medium": 1, "low": 2}
_FINDING_TEXT_CAP = 120


def _finding_texts(summary) -> tuple[str, ...]:
    """Digest all findings into short, single-line Slack descriptions."""
    findings = summary.findings or []
    ordered = sorted(
        findings,
        key=lambda finding: _SEV_RANK.get(
            (finding.get("severity") or "").lower(), 9),
    )
    result: list[str] = []
    for finding in ordered:
        raw = (
            finding.get("description")
            or finding.get("rule")
            or finding.get("category")
            or "unnamed finding"
        )
        text = " ".join(str(raw).split())
        if len(text) > _FINDING_TEXT_CAP:
            text = text[:_FINDING_TEXT_CAP] + "..."
        result.append(text)
    return tuple(result)


def _newly_flagged_keys(
    prior: dict[str, dict[str, Any]],
    current: dict[str, dict[str, Any]],
) -> list[str]:
    """Keys that are flagged NOW and weren't flagged before (or didn't exist)."""
    out: list[str] = []
    for key, cur in current.items():
        if not cur.get("flagged"):
            continue
        prev = prior.get(key)
        if prev is None or not prev.get("flagged"):
            out.append(key)
    return sorted(out)


def _coverage_state_path_for(out_dir: Path) -> Path:
    """Map an absolute out_dir to its R15 coverage state file."""
    slug = out_dir.name or "_root"
    return _STATE_DIR / f"{slug}_coverage.json"


def _load_prior_coverage(path: Path) -> dict | None:
    """Read prior coverage snapshot."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _coverage_finding_count(data: dict | None) -> int:
    """Total findings count in a coverage payload. 0 on missing/None."""
    if not isinstance(data, dict):
        return 0
    findings = data.get("findings")
    return len(findings) if isinstance(findings, list) else 0


def _coverage_breakdown(data: dict | None) -> dict[str, int]:
    """{rule -> count} breakdown for the Slack body. Empty on no data."""
    out: dict[str, int] = {}
    if not isinstance(data, dict):
        return out
    for f in (data.get("findings") or []):
        rule = f.get("rule") or "?"
        out[rule] = out.get(rule, 0) + 1
    return out


def run(out_dir: Path,
        settings: argus_config.ArgusConfig | None = None) -> int:
    """Compute the diff and post a consolidated Slack message."""
    if settings is None:
        settings = argus_config.load()
    if not out_dir.exists():
        sys.stderr.write(f"[diff_and_notify] out_dir does not exist: "
                         f"{out_dir}\n")
        return 1

    summaries = argus_report._scan_output_dir(out_dir)
    current = _build_current(summaries)
    state_path = _state_path_for(out_dir)
    prior = _load_state(state_path)
    new_flagged = _newly_flagged_keys(prior, current)

    if new_flagged:
        by_key = {s.key: s for s in summaries}
        deviations = [
            notify.Deviation(
                key=key,
                verdict=current[key].get("verdict"),
                tester=by_key[key].tester,
                tester_status=by_key[key].tester_status,
                testrun_name=by_key[key].testrun_name,
                testrun_key=by_key[key].testrun_key,
                findings=_finding_texts(by_key[key]),
            )
            for key in new_flagged
        ]
        notify.post_deviations(out_dir.name, deviations, settings=settings)

    _write_state_atomic(state_path, current)

    coverage_path = out_dir / "_coverage.json"
    current_coverage = None
    if coverage_path.exists():
        try:
            current_coverage = json.loads(coverage_path.read_text())
        except (OSError, json.JSONDecodeError):
            current_coverage = None

    coverage_state_path = _coverage_state_path_for(out_dir)
    prior_coverage = _load_prior_coverage(coverage_state_path)
    prior_count = _coverage_finding_count(prior_coverage)
    current_count = _coverage_finding_count(current_coverage)
    delta = current_count - prior_count

    if delta > 0:
        breakdown = _coverage_breakdown(current_coverage)
        breakdown_str = ", ".join(
            f"{rule}={n}" for rule, n in sorted(breakdown.items())
        ) or "(no breakdown)"
        title = (
            f":warning: ARGUS — {delta} new coverage gap"
            f"{'s' if delta != 1 else ''} in {out_dir.name}"
        )
        body = (
            f"folder: {out_dir.name}\n"
            f"prior gaps: {prior_count}\n"
            f"current gaps: {current_count}\n"
            f"breakdown: {breakdown_str}"
        )
        notify.post("error", title, body, settings=settings)

    if current_coverage is not None:
        try:
            coverage_state_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_io.atomic_write_json(coverage_state_path,
                                        current_coverage)
        except OSError as e:
            sys.stderr.write(
                f"[diff_and_notify] coverage state write failed: "
                f"{type(e).__name__}: {e}\n")

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Diff this folder's audit state against the last run "
                    "and post any newly-flagged keys to Slack.")
    parser.add_argument(
        "--out-dir", type=Path, default=None,
        help="folder dir to diff (default: extractor.out_dir from config.toml). "
             "Usually the per-folder scoped subdir, e.g. output/Sprint_47_Regression.")
    parser.add_argument(
        "--config", type=Path, default=None,
        help="path to config.toml (default: ./config.toml).")
    args = parser.parse_args(argv)

    settings = argus_config.load(args.config)
    runtime.configure(settings)
    out_dir = args.out_dir or settings.extractor.out_dir
    out_dir = Path(out_dir).resolve()
    return run(out_dir, settings=settings)


if __name__ == "__main__":
    sys.exit(main())
