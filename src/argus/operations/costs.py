"""Local model usage estimates and per-audit cost monitoring."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from argus import config as argus_config
from argus.core import atomic_io
from argus.core import costs as cost_core
from argus.core import exec_paths
from argus.operations import notify






@dataclass(frozen=True)
class AuditCostRow:
    key: str
    estimated_cost_usd: Decimal
    calls: int
    audited_at: str


@dataclass(frozen=True)
class LocalCostSnapshot:
    audits: int
    tracked_audits: int
    untracked_audits: int
    tracked_calls: int
    unpriced_calls: int
    estimated_cost_usd: Decimal
    average_cost_usd: Decimal | None
    start: str
    end: str
    today_audits: int = 0
    today_tracked_audits: int = 0
    today_calls: int = 0
    today_unpriced_calls: int = 0
    today_cost_usd: Decimal = Decimal("0")
    recent_audits: tuple[AuditCostRow, ...] = ()


@dataclass(frozen=True)
class RunCostSnapshot:
    label: str
    kind: str
    started_at: str
    completed_at: str
    queued_audits: int
    recorded_audits: int
    tracked_audits: int
    model_audits: int
    calls: int
    unpriced_calls: int
    calculated_cost_usd: Decimal
    invoice_exact: bool = False
    pricing_basis: str = "model_response_tokens"


_ALERT_STATE_FILE = "cost_alert_state.json"
_RUN_COST_FILE = "_latest_run_cost.json"
_ARCHIVED_REPORT_RE = re.compile(
    r"^_report_\d{4}-\d{2}-\d{2}_\d{6}\.md$")


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _month_period(now: dt.datetime) -> tuple[dt.date, dt.date]:
    today = now.astimezone(dt.timezone.utc).date()
    return today.replace(day=1), today + dt.timedelta(days=1)












def _audit_date(path: Path, audit: dict[str, Any]) -> dt.date | None:
    raw = audit.get("audited_at")
    if isinstance(raw, str):
        try:
            parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.astimezone(dt.timezone.utc).date()
        except ValueError:
            pass
    try:
        return dt.datetime.fromtimestamp(
            path.stat().st_mtime, tz=dt.timezone.utc).date()
    except OSError:
        return None


def _audit_timestamp(path: Path, audit: dict[str, Any]) -> str:
    raw = audit.get("audited_at")
    if isinstance(raw, str):
        try:
            parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed.astimezone(dt.timezone.utc).isoformat()
        except ValueError:
            pass
    try:
        return dt.datetime.fromtimestamp(
            path.stat().st_mtime, tz=dt.timezone.utc).isoformat()
    except OSError:
        return ""


def _parse_timestamp(value: str) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _path_timestamp(path: Path) -> dt.datetime | None:
    try:
        return dt.datetime.fromtimestamp(
            path.stat().st_mtime, tz=dt.timezone.utc)
    except OSError:
        return None


def _summarise_run_audits(
    paths: list[Path],
    *,
    label: str,
    kind: str,
    started_at: dt.datetime,
    completed_at: dt.datetime,
    queued_audits: int,
    model_rates: dict[str, dict[str, float]],
) -> RunCostSnapshot:
    recorded = tracked = model_audits = calls = unpriced_calls = 0
    calculated = Decimal("0")
    for path in paths:
        try:
            audit = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        recorded += 1
        cost = audit.get("cost")
        if not isinstance(cost, dict):
            continue
        repriced = cost_core.reprice_summary(cost, model_rates)
        if repriced is None:
            continue
        tracked += 1
        audit_calls = int(repriced.get("calls") or 0)
        calls += audit_calls
        if audit_calls:
            model_audits += 1
        unpriced_calls += int(repriced.get("unpriced_calls") or 0)
        calculated += _decimal(repriced.get("estimated_cost_usd"))
    return RunCostSnapshot(
        label=label,
        kind=kind,
        started_at=started_at.isoformat(),
        completed_at=completed_at.isoformat(),
        queued_audits=queued_audits,
        recorded_audits=recorded,
        tracked_audits=tracked,
        model_audits=model_audits,
        calls=calls,
        unpriced_calls=unpriced_calls,
        calculated_cost_usd=calculated,
    )


def record_run_cost(
    run_dir: Path,
    *,
    label: str,
    kind: str,
    keys: list[str],
    started_at: dt.datetime,
    model_rates: dict[str, dict[str, float]],
    only_new: bool = True,
    completed_at: dt.datetime | None = None,
) -> RunCostSnapshot:
    """Persist the latest run's exact key boundary and token calculation."""
    completed_at = completed_at or dt.datetime.now(dt.timezone.utc)
    started_at = started_at.astimezone(
        dt.timezone.utc).replace(microsecond=0)
    wanted = set(keys)
    paths: list[Path] = []
    for path in exec_paths.iter_audit_jsons(run_dir):
        if path.parent.name not in wanted:
            continue
        if only_new:
            try:
                audit = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            audited_at = _parse_timestamp(_audit_timestamp(path, audit))
            if audited_at is None or audited_at < started_at:
                continue
        paths.append(path)
    snapshot = _summarise_run_audits(
        paths,
        label=label,
        kind=kind,
        started_at=started_at,
        completed_at=completed_at,
        queued_audits=len(wanted),
        model_rates=model_rates,
    )
    atomic_io.atomic_write_json(
        run_dir / _RUN_COST_FILE,
        _json_value(asdict(snapshot)),
    )
    return snapshot


def _load_run_cost(path: Path) -> RunCostSnapshot | None:
    try:
        raw = json.loads(path.read_text())
        return RunCostSnapshot(
            label=str(raw["label"]),
            kind=str(raw["kind"]),
            started_at=str(raw["started_at"]),
            completed_at=str(raw["completed_at"]),
            queued_audits=int(raw["queued_audits"]),
            recorded_audits=int(raw["recorded_audits"]),
            tracked_audits=int(raw["tracked_audits"]),
            model_audits=int(raw["model_audits"]),
            calls=int(raw["calls"]),
            unpriced_calls=int(raw["unpriced_calls"]),
            calculated_cost_usd=_decimal(raw["calculated_cost_usd"]),
            invoice_exact=bool(raw.get("invoice_exact", False)),
            pricing_basis=str(
                raw.get("pricing_basis") or "model_response_tokens"),
        )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _latest_recorded_run(out_dir: Path) -> RunCostSnapshot | None:
    candidates: list[tuple[dt.datetime, RunCostSnapshot]] = []
    for path in out_dir.rglob(_RUN_COST_FILE):
        snapshot = _load_run_cost(path)
        if snapshot is None:
            continue
        completed = _parse_timestamp(snapshot.completed_at)
        if completed is not None:
            candidates.append((completed, snapshot))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _report_run_snapshot(
    report: Path,
    *,
    model_rates: dict[str, dict[str, float]],
) -> RunCostSnapshot | None:
    completed = _path_timestamp(report)
    if completed is None:
        return None
    sibling_reports = sorted(
        (
            path for path in report.parent.iterdir()
            if path.is_file() and _ARCHIVED_REPORT_RE.match(path.name)
        ),
        key=lambda path: path.stat().st_mtime,
    )
    previous_times = [
        timestamp
        for path in sibling_reports
        if path != report
        and (timestamp := _path_timestamp(path)) is not None
        and timestamp < completed
    ]
    lower_bound = max(previous_times) if previous_times else None
    paths: list[Path] = []
    audit_times: list[dt.datetime] = []
    for path in exec_paths.iter_audit_jsons(report.parent):
        try:
            audit = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        audited_at = _parse_timestamp(_audit_timestamp(path, audit))
        if audited_at is None or audited_at > completed:
            continue
        if lower_bound is not None and audited_at <= lower_bound:
            continue
        paths.append(path)
        audit_times.append(audited_at)
    if not paths:
        return None
    started = min(audit_times)
    return _summarise_run_audits(
        paths,
        label=report.parent.name,
        kind="folder",
        started_at=started,
        completed_at=completed,
        queued_audits=len(paths),
        model_rates=model_rates,
    )


def scan_latest_run_cost(
    out_dir: Path,
    *,
    model_rates: dict[str, dict[str, float]],
) -> RunCostSnapshot | None:
    """Return the latest persisted run, with report-boundary fallback."""
    candidates: list[RunCostSnapshot] = []
    recorded = _latest_recorded_run(out_dir)
    if recorded is not None:
        candidates.append(recorded)
    reports = sorted(
        (
            path for path in out_dir.rglob("_report_*.md")
            if _ARCHIVED_REPORT_RE.match(path.name)
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for report in reports:
        snapshot = _report_run_snapshot(
            report, model_rates=model_rates)
        if snapshot is not None:
            candidates.append(snapshot)
            break
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda snapshot: _parse_timestamp(snapshot.completed_at)
        or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
    )


def scan_local_costs(
    out_dir: Path,
    *,
    model_rates: dict[str, dict[str, float]],
    now: dt.datetime | None = None,
) -> LocalCostSnapshot:
    now = now or dt.datetime.now(dt.timezone.utc)
    start, end = _month_period(now)
    today = now.astimezone(dt.timezone.utc).date()
    audits = tracked = calls = unpriced_calls = 0
    today_audits = today_tracked = today_calls = today_unpriced = 0
    estimated = Decimal("0")
    today_cost = Decimal("0")
    recent: list[tuple[str, AuditCostRow]] = []
    for path in exec_paths.iter_audit_jsons(out_dir):
        try:
            audit = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        audit_date = _audit_date(path, audit)
        if audit_date is None or not start <= audit_date < end:
            continue
        audits += 1
        is_today = audit_date == today
        if is_today:
            today_audits += 1
        cost = audit.get("cost")
        if not isinstance(cost, dict):
            continue
        repriced = cost_core.reprice_summary(cost, model_rates)
        if repriced is None:
            continue
        tracked += 1
        audit_calls = int(repriced.get("calls") or 0)
        audit_unpriced = int(repriced.get("unpriced_calls") or 0)
        calls += audit_calls
        unpriced_calls += audit_unpriced
        audit_cost = _decimal(repriced.get("estimated_cost_usd"))
        estimated += audit_cost
        if is_today:
            today_tracked += 1
            today_calls += audit_calls
            today_unpriced += audit_unpriced
            today_cost += audit_cost
        audited_at = _audit_timestamp(path, audit)
        recent.append((
            audited_at,
            AuditCostRow(
                key=path.parent.name,
                estimated_cost_usd=audit_cost,
                calls=int(repriced.get("calls") or 0),
                audited_at=audited_at,
            ),
        ))
    average = estimated / tracked if tracked else None
    recent_rows = tuple(
        row for _, row in sorted(
            recent, key=lambda item: item[0], reverse=True
        )[:10]
    )
    return LocalCostSnapshot(
        audits=audits,
        tracked_audits=tracked,
        untracked_audits=audits - tracked,
        tracked_calls=calls,
        unpriced_calls=unpriced_calls,
        estimated_cost_usd=estimated,
        average_cost_usd=average,
        start=start.isoformat(),
        end=end.isoformat(),
        today_audits=today_audits,
        today_tracked_audits=today_tracked,
        today_calls=today_calls,
        today_unpriced_calls=today_unpriced,
        today_cost_usd=today_cost,
        recent_audits=recent_rows,
    )


def _money(
    value: Decimal | None,
    currency: str = "USD",
    *,
    places: int = 2,
) -> str:
    if value is None:
        return "n/a"
    symbol = "$" if currency == "USD" else f"{currency} "
    return f"{symbol}{value:,.{places}f}"




def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value




def _load_alert_state(path: Path, month: str) -> dict[str, Any]:
    default = {
        "month": month,
        "triggered": [],
    }
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default
    if not isinstance(raw, dict):
        return default
    if raw.get("month") != month:
        return default
    triggered = raw.get("triggered")
    if not isinstance(triggered, list):
        triggered = []
    return {
        "month": month,
        "triggered": [
            item for item in triggered
            if item in {"warning", "critical"}
        ],
    }


def _save_alert_state(path: Path, state: dict[str, Any]) -> None:
    try:
        atomic_io.atomic_write_json(path, state)
    except OSError as exc:
        sys.stderr.write(
            f"[costs] could not write alert state {path}: {exc}\n")






def render(
    local: LocalCostSnapshot,
    monthly_budget_usd: float | None = None,
    warning_percent: float = 80.0,
    critical_percent: float = 100.0,
    latest_run: RunCostSnapshot | None = None,
) -> str:
    """Show estimates derived from saved usage and configured model rates."""
    lines = ["ARGUS MODEL COST ESTIMATES", "", "LAST RUN"]
    if latest_run is None:
        lines.append("  No completed audit run found.")
    else:
        lines.extend([
            f"  Folder:               {latest_run.label}",
            f"  Completed:            {latest_run.completed_at}",
            f"  Test cases audited:   {latest_run.recorded_audits}",
            f"  Model-audited:        {latest_run.model_audits}",
            f"  Estimated cost:       {_money(latest_run.calculated_cost_usd)}",
        ])
    lines.extend(["", "MONTH TO DATE",
                  f"  Estimated cost:       {_money(local.estimated_cost_usd)}",
                  f"  Tracked audits:       {local.tracked_audits} / {local.audits}",
                  f"  Unpriced calls:       {local.unpriced_calls}"])
    if monthly_budget_usd is not None and monthly_budget_usd > 0:
        used = local.estimated_cost_usd / Decimal(str(monthly_budget_usd)) * 100
        status = "CRITICAL" if used >= critical_percent else "WARNING" if used >= warning_percent else "OK"
        lines.append(f"  Budget:               {used:.1f}% - {status}")
    lines.extend(["", "Estimates use saved token usage and configured rates; they are not invoices."])
    return "\n".join(lines)


def evaluate_alerts(
    settings: argus_config.ArgusConfig,
    local: LocalCostSnapshot,
    *,
    state_path: Path | None = None,
    now: dt.datetime | None = None,
) -> None:
    """Emit each budget threshold once per month based on local estimates."""
    now = now or dt.datetime.now(dt.timezone.utc)
    month = now.astimezone(dt.timezone.utc).strftime("%Y-%m")
    path = state_path or settings.general.state_dir / _ALERT_STATE_FILE
    state = _load_alert_state(path, month)
    budget_value = settings.costs.monthly_budget_usd
    if budget_value is None or budget_value <= 0:
        return
    budget = Decimal(str(budget_value))
    used = local.estimated_cost_usd / budget * 100
    for key, threshold, level, title in (
        ("warning", settings.costs.warning_percent, "warn", "cost budget warning"),
        ("critical", settings.costs.critical_percent, "error", "cost budget exceeded"),
    ):
        if used < Decimal(str(threshold)) or key in state["triggered"]:
            continue
        notify.post(level, title,
                    f"Estimated model cost is {used:.1f}% | ${local.estimated_cost_usd:,.2f} of ${budget:,.2f}",
                    settings=settings, audience="owner")
        state["triggered"].append(key)
    _save_alert_state(path, state)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show ARGUS local model usage estimates.")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--watch", action="store_true", help="refresh continuously")
    parser.add_argument("--refresh-seconds", type=int, default=None)
    args = parser.parse_args(argv)
    settings = argus_config.load(args.config)
    out_dir = args.out_dir or settings.extractor.out_dir
    refresh = args.refresh_seconds or settings.costs.refresh_seconds
    if refresh < 30:
        parser.error("--refresh-seconds must be at least 30")
    while True:
        local = scan_local_costs(out_dir, model_rates=settings.costs.model_rates)
        latest_run = scan_latest_run_cost(out_dir, model_rates=settings.costs.model_rates)
        if args.watch and not args.json_output:
            print("\033[2J\033[H", end="")
        if args.json_output:
            print(json.dumps({
                "local": _json_value(asdict(local)),
                "latest_run": _json_value(asdict(latest_run)) if latest_run else None,
            }, indent=2, sort_keys=True))
        else:
            print(render(local, settings.costs.monthly_budget_usd,
                         settings.costs.warning_percent, settings.costs.critical_percent,
                         latest_run), flush=True)
        evaluate_alerts(settings, local)
        if not args.watch:
            return 0
        try:
            time.sleep(refresh)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    sys.exit(main())
