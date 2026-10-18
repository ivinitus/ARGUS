"""Run one local audit cycle with report publication and retry tracking."""
from __future__ import annotations

import argparse
import datetime as _dt
import logging
import sys
from pathlib import Path

from argus import config as argus_config
from argus import runtime
from argus.operations import control, costs, diff_and_notify, prune, stop_signal
from argus.pipeline import extractor, orchestrator, r12_heal

log = logging.getLogger("argus.cycle")


def _today_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


def _stamp_path(state_dir: Path, name: str) -> Path:
    return state_dir / name


def _stamp_is_today(stamp: Path) -> bool:
    try:
        return stamp.read_text().strip() == _today_utc()
    except OSError:
        return False


def _write_stamp(stamp: Path) -> None:
    stamp.write_text(_today_utc() + "\n")


def _resolve_scoped_out_dir(settings: argus_config.ArgusConfig) -> Path | None:
    """The folder cron just audited, same resolution order as cron_wrapper.sh."""
    state_file = settings.general.state_dir / "last_folder_out_dir"
    try:
        recorded = Path(state_file.read_text().splitlines()[0])
        if recorded.is_dir():
            return recorded
    except (OSError, IndexError):
        pass
    out_dir = settings.extractor.out_dir
    if not out_dir.exists():
        return None
    candidates = sorted(
        (p for p in out_dir.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    return candidates[0] if candidates else None


def run_cycle(settings: argus_config.ArgusConfig, *, config_args: list[str]) -> int:
    if control.is_paused():
        log.info("argus paused — skipping cycle")
        return 0

    log.info("starting r12 self-heal pass")
    try:
        rc = r12_heal.main(["--scope", "active", *config_args])
        if rc != 0:
            log.warning("r12_heal returned rc=%d (non-fatal)", rc)
    except Exception as e:
        log.warning("r12_heal raised %s: %s (non-fatal)", type(e).__name__, e)

    log.info("starting auto-folder audit")
    audit_rc = orchestrator.main(["--auto-folder", *config_args])
    if audit_rc == 2:
        log.error("auto-folder audit failed authentication (rc=2)")
        return 2
    if audit_rc != 0:
        log.error("auto-folder audit failed (rc=%d)", audit_rc)
        return 1

    scoped_out_dir = _resolve_scoped_out_dir(settings)
    if scoped_out_dir is not None:
        log.info("diff against %s", scoped_out_dir)
        try:
            diff_rc = diff_and_notify.main(
                ["--out-dir", str(scoped_out_dir), *config_args])
            if diff_rc != 0:
                log.warning("diff_and_notify returned rc=%d (non-fatal)", diff_rc)
        except Exception as e:
            log.warning("diff_and_notify raised %s: %s (non-fatal)",
                       type(e).__name__, e)
    else:
        log.info("no recent output subfolder found — skipping diff")

    state_dir = settings.general.state_dir
    state_dir.mkdir(parents=True, exist_ok=True)

    cost_stamp = _stamp_path(state_dir, "last_cost_check_day")
    if not _stamp_is_today(cost_stamp):
        log.info("daily cost-explorer cross-check")
        try:
            if costs.main(config_args) == 0:
                _write_stamp(cost_stamp)
            else:
                log.warning("argus costs failed (non-fatal; retries next cycle)")
        except Exception as e:
            log.warning("costs.main raised %s: %s (non-fatal)",
                       type(e).__name__, e)

    prune_stamp = _stamp_path(state_dir, "last_prune_day")
    if not _stamp_is_today(prune_stamp):
        log.info("daily evidence prune")
        try:
            if prune.main(["--apply", "--if-enabled"]) == 0:
                _write_stamp(prune_stamp)
            else:
                log.warning("argus prune failed (non-fatal; retries next cycle)")
        except Exception as e:
            log.warning("prune.main raised %s: %s (non-fatal)",
                       type(e).__name__, e)

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One unattended ARGUS pass: heal, auto-folder audit, "
                    "diff, daily cost reconciliation, daily prune.")
    parser.add_argument("--config", type=Path, default=None,
                        help="path to config.toml (default: ./config.toml)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    settings = argus_config.load(args.config)
    runtime.configure(settings)
    verbose = bool(args.verbose or settings.logging.verbose)
    # Install the handler here so in-process steps (r12_heal) cannot later
    # install one at a quieter level and silence the rest of the cycle.
    extractor.configure_logging(verbose)
    logging.getLogger("argus").setLevel(
        logging.DEBUG if verbose else logging.INFO)

    # Installed before the heal step so a process stop during any phase of the
    # cycle exits 143 promptly instead of waiting for the SIGKILL.
    stop_signal.install()
    config_args = ["--config", str(args.config)] if args.config else []
    return run_cycle(settings, config_args=config_args)


if __name__ == "__main__":
    sys.exit(main())
