"""ARGUS — end-to-end runner."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any

import requests

from argus.operations import audit_lock
from argus.core import atomic_io
from argus.pipeline import auditor
from argus.pipeline import batch
from argus import config as argus_config
from argus.core import exec_paths
from argus.core import paths
from argus.pipeline import analysis_status
from argus.pipeline import extractor
from argus.operations import events
from argus.operations import notify
from argus.operations import stop_signal
from argus.core import progress_line as progress
from argus import runtime

log = logging.getLogger("argus.orchestrator")


EXECUTED_STATUS_NAMES = {
    "PASS",
    "FAIL",
    "PASSED WITH ISSUE",
    "PASSED_WITH_ISSUE",
}
SINGLE_ENV_CHECK_TIMEOUT_S = 300


def _activity_progress_callback(
    total: int,
    settings: argus_config.ArgusConfig,
):
    """Emit one final, fully-costed Activity line per execution key."""

    def report(result: batch.KeyResult, done: int, callback_total: int) -> None:
        percent = done * 100 // callback_total if callback_total else 100
        if result.status == "ok":
            verdict = result.verdict or "completed"
            findings = result.findings if result.findings is not None else 0
            noun = "finding" if findings == 1 else "findings"
            outcome = f"{verdict}, {findings} {noun}"
        elif result.status == "skipped":
            outcome = f"skipped: {result.reason or 'no evidence'}"
        elif result.status == "auth_failed":
            outcome = f"auth failed: {result.reason or 'unknown reason'}"
        else:
            outcome = f"failed: {result.reason or 'unknown reason'}"
        cost = _key_result_cost(result)
        events.emit(
            "PROGRESS",
            f"{done}/{callback_total} ({percent}%) | {result.key} | "
            f"{outcome} | {cost} | {result.elapsed_s:.1f}s",
            settings=settings,
        )

    return report


def _activity_extraction_callback(
    total: int,
    settings: argus_config.ArgusConfig,
):
    """Emit one extraction/PDF update per execution key."""

    def report(
        result: batch.ExtractionResult,
        done: int,
        callback_total: int,
    ) -> None:
        percent = done * 100 // callback_total if callback_total else 100
        if result.status == "ok":
            detail = (
                f"PDF {result.pages} "
                f"{'page' if result.pages == 1 else 'pages'}"
            )
        else:
            detail = f"failed: {result.reason or 'unknown reason'}"
        events.emit(
            "EXTRACT",
            f"{done}/{callback_total} ({percent}%) | {result.key} | "
            f"{detail} | {result.elapsed_s:.1f}s",
            settings=settings,
        )

    return report


def _key_result_cost(result: batch.KeyResult) -> str:
    if result.audit_path is None:
        return "cost n/a"
    try:
        audit = json.loads(result.audit_path.read_text())
    except (OSError, json.JSONDecodeError):
        return "cost n/a"
    cost = audit.get("cost")
    if not isinstance(cost, dict):
        return "cost n/a"
    value = cost.get("estimated_cost_usd")
    if not isinstance(value, (int, float)):
        return "cost n/a"
    return f"${value:.4f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ARGUS end-to-end runner")
    parser.add_argument("--key", dest="execution_key",
                        help="execution key, e.g. QA-E123")
    parser.add_argument("--testrun-id", type=int)
    parser.add_argument("--item-id", type=int)
    parser.add_argument("--folder",
                        help="folder name — enumerate executed, not-yet-audited "
                             "keys and run the full batch (extract + audit) in "
                             "parallel per [batch] in config.toml")
    parser.add_argument("--test-plan", help="test-plan key, e.g. MOBILE-P123; audit its linked runs")
    parser.add_argument("--test-case", help="test-case key, e.g. MOBILE-T123; audit its completed executions")
    parser.add_argument("--test-plan-folder", help="exact test-plan folder name; audit its plans' linked runs")
    parser.add_argument("--auto-folder", action="store_true",
                        help="auto-pick the latest 'Targeted regression' folder "
                             "(within 7 days). Cron-friendly: silently exits 0 "
                             "when nothing matches. Mutually exclusive with "
                             "--folder / --key / --testrun-id.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-audit every currently executable item in the selected "
             "folder even when cached evidence is unchanged",
    )
    parser.add_argument("--config", type=Path, default=None,
                        help="path to config.toml (default: ./config.toml)")
    parser.add_argument("--skip-extract", action="store_true",
                        help="reuse existing output dir, audit only")
    parser.add_argument("--skip-audit", action="store_true",
                        help="extract only, don't audit")
    parser.add_argument("--debug", action="store_true",
                        help="include per-chunk breakdown and raw diagnostics "
                             "in audit.md (audit.json always carries full data)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    modes = sum(bool(x) for x in (args.execution_key,
                                  args.testrun_id and args.item_id,
                                  args.folder,
                                  args.test_plan,
                                  args.test_case,
                                  args.test_plan_folder,
                                  args.auto_folder))
    if modes == 0:
        parser.error("provide --key, --testrun-id + --item-id, --folder, "
                     "--test-case, --test-plan, --test-plan-folder, or --auto-folder")
    if modes > 1:
        parser.error("pick exactly one of --key / --testrun-id+--item-id / "
                     "--folder / --test-case / --test-plan / --test-plan-folder / --auto-folder")
    if args.force and not (args.folder or args.auto_folder):
        parser.error("--force is only valid with --folder or --auto-folder")

    settings = argus_config.load(args.config)
    runtime.configure(settings)
    extractor.configure_logging(args.verbose or settings.logging.verbose)

    if args.debug:
        settings.auditor.debug_output = True

    # `/argus stop` delivers SIGTERM; announce "stopped" and
    # exit 143 instead of dying silently mid-batch.
    stop_signal.install()

    if args.test_plan or args.test_plan_folder or args.test_case:
        from argus.operations.manual_batch import run_targets
        return run_targets(
            settings,
            plan_keys=[args.test_plan] if args.test_plan else [],
            plan_folders=[args.test_plan_folder] if args.test_plan_folder else [],
            case_keys=[args.test_case] if args.test_case else [],
        )
    if args.auto_folder:
        return _run_auto_folder(settings, force=args.force)
    if args.folder:
        return _run_folder_list(args.folder, settings, force=args.force)
    return _run_single(args, settings)


def _run_auto_folder(settings, *, force: bool = False) -> int:
    """Auto-resolve the latest active 'Targeted regression' folder, then delegate to _run_folder_list.

    Quiet no-match is rc=0 by design — cron runs unattended across
    sprints and a weekend with no fresh folder is steady state, not an
    error. Auth/network failure is rc=2 so the cron wrapper logs it and
    fires a single Slack warn.
    """
    if not settings.folders.auto_folder_enabled:
        print(
            f"[ARGUS] auto-folder is disabled for profile "
            f"{settings.general.profile!r}; use --folder or --key",
            file=sys.stderr,
        )
        return 0

    session = extractor.build_session(settings.extractor)
    try:
        with progress.step("auto-picking latest targeted-regression folder") as s:
            folder = extractor.find_latest_active_folder(
                session,
                settings.extractor.base_url,
                settings.extractor.project_id,
                name_prefix=settings.folders.auto_name_prefix,
                max_age_days=settings.folders.auto_max_age_days,
            )
            if not folder:
                s.finish("ok", "no folder within last 7 days — nothing to do")
                return 0
            s.finish("ok", f"selected {folder.get('name')!r}")
    except extractor.AuthError as e:
        print(f"  reason: {e}", file=sys.stderr)
        return 2

    name = folder.get("name")
    if not name:
        print("[ARGUS] auto-folder match has no 'name' field — bailing",
              file=sys.stderr)
        return 1
    return _run_folder_list(name, settings, force=force)


def _scoped_folder_out_dir(base_out_dir: Path, folder_name: str) -> Path:
    """Return the per-folder-scoped output dir for a `--folder` run."""
    return base_out_dir / extractor.slugify(folder_name)


def _audit_run_id() -> str:
    """Task-wide run id: the launcher run id when present, else fresh."""
    return os.environ.get("ARGUS_RUN_ID", "").strip() or uuid.uuid4().hex


def _audit_requested_by() -> str | None:
    """Alias of the operator who launched this task (set by the Slack bot)."""
    return os.environ.get("ARGUS_REQUESTED_BY", "").strip() or None


def _audit_target_label() -> str | None:
    """Operator-facing target label for this task (set by the Slack bot)."""
    return os.environ.get("ARGUS_TARGET_LABEL", "").strip()[:160] or None


def _announce_audit_phase(
    phase: str,
    target: str,
    *,
    run_id: str,
    total: int = 0,
) -> None:
    """Best-effort early progress announcement; never blocks the audit.

    Records ``phase`` in the control store (for ``/argus status`` and the
    local dashboard) and in the published team ``index.json`` (for the
    static dashboards), so operators see "preparing" during the slow Jira
    enumeration instead of waiting for the first checkpoint.
    """
    from argus.operations import control as _argus_control
    from argus.reporting import publisher as report_publisher

    requested_by = _audit_requested_by()
    label = _audit_target_label()
    try:
        _argus_control.mark_audit_running(
            target,
            total,
            owner_token=run_id,
            phase=phase,
            requested_by=requested_by,
            target=label,
        )
    except Exception as exc:
        print(
            f"[ARGUS] audit status '{phase}' not recorded: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
    try:
        report_publisher.announce_active_audit(
            phase=phase,
            target=target,
            run_id=run_id,
            label=label,
            requested_by=requested_by,
            total=total or None,
        )
    except Exception as exc:
        print(
            f"[ARGUS] audit status '{phase}' not published: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


def _finish_audit_announcement(
    run_id: str,
    *,
    rc: int | None = 0,
    error: BaseException | None = None,
) -> None:
    """Resolve the announcements made by ``_announce_audit_phase``.

    Both stores are owner-checked on ``run_id``, so a state already
    replaced by ``start_audit_run``/``publish`` (or by a newer run) is left
    untouched. A non-zero ``rc`` or an ``error`` leaves a ``failed`` marker
    with a diagnostic instead of silently clearing the banner.
    """
    from argus.operations import control as _argus_control
    from argus.reporting import publisher as report_publisher

    if error is not None:
        diagnostic: str | None = f"{type(error).__name__}: {error}"
    elif rc is None:
        diagnostic = "audit interrupted before completion"
    elif rc:
        diagnostic = f"audit exited with rc={rc} before publication"
    else:
        diagnostic = None
    try:
        if diagnostic is None:
            report_publisher.clear_active_audit(run_id=run_id)
        else:
            report_publisher.fail_active_audit(
                run_id=run_id,
                diagnostic=diagnostic,
            )
    except Exception as exc:
        print(
            f"[ARGUS] audit status not resolved: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
    try:
        _argus_control.mark_audit_done(run_id)
    except Exception as exc:
        print(
            f"[ARGUS] audit status not cleared: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


def _announce_audit_stopped(run_id: str, target: str) -> None:
    """SIGTERM path of ``_finish_audit_announcement``: mark ``stopped``.

    Runs inside ``stop_signal.handle_stop`` just before ``os._exit(143)``,
    so every write is single-shot and owner-checked on ``run_id``: the
    dashboard keeps a ``stopped`` marker naming who asked (``/argus stop``
    forwards the alias in ``ARGUS_REQUESTED_BY``) and ``current_audit`` is
    cleared so the next launch is not blocked for two hours.
    """
    from argus.operations import control as _argus_control
    from argus.reporting import publisher as report_publisher

    diagnostic = f"audit of {target} stopped by SIGTERM before completion"
    try:
        report_publisher.stop_active_audit(run_id=run_id, diagnostic=diagnostic)
    except Exception as exc:
        print(
            f"[ARGUS] audit status 'stopped' not published: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
    try:
        _argus_control.mark_audit_done(run_id)
    except Exception as exc:
        print(
            f"[ARGUS] audit status not cleared: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


def _run_single(args, settings) -> int:
    """Audit one execution, announcing progress before Jira is contacted."""
    label = args.execution_key or f"{args.testrun_id}_{args.item_id}"
    run_id = _audit_run_id()
    _announce_audit_phase("preparing", label, run_id=run_id, total=1)
    unregister_stop = stop_signal.register_on_stop(
        lambda: _announce_audit_stopped(run_id, label))
    rc: int | None = None
    error: BaseException | None = None
    try:
        rc = _run_single_prepared(args, settings, run_id=run_id)
        return rc
    except Exception as exc:
        error = exc
        raise
    finally:
        unregister_stop()
        _finish_audit_announcement(run_id, rc=rc, error=error)


def _run_single_prepared(args, settings, *, run_id: str) -> int:
    out_dir = settings.extractor.out_dir
    label = args.execution_key or f"{args.testrun_id}_{args.item_id}"

    if args.execution_key and not args.skip_extract:
        try:
            session = extractor.build_session(settings.extractor)
            tr_url = (f"{settings.extractor.base_url}"
                      f"/rest/tests/1.0/testresult/{args.execution_key}")
            tr_resp = session.get(
                tr_url,
                params={"fields": "testRun(folderId,projectId)"},
                timeout=15,
            )
            if tr_resp.status_code == 200:
                test_run = tr_resp.json().get("testRun") or {}
                folder_id = test_run.get("folderId")
                project_id = str(test_run.get("projectId") or settings.extractor.project_id)
                if project_id not in settings.extractor.project_ids:
                    print(
                        f"[ARGUS] {args.execution_key} belongs to unconfigured "
                        f"Jira project {project_id}", file=sys.stderr,
                    )
                    return 1
                if folder_id:
                    folder_name = extractor.find_folder_name_by_id(
                        session, settings.extractor.base_url,
                        project_id, folder_id,
                    )
                    if folder_name:
                        scoped = _scoped_folder_out_dir(out_dir, folder_name)
                        scoped.mkdir(parents=True, exist_ok=True)
                        _write_folder_meta(scoped, folder_name)
                        out_dir = scoped
                        print(f"[ARGUS] {args.execution_key} → "
                              f"scoped to {scoped.name}/",
                              file=sys.stderr)
        except Exception as e:
            print(f"[ARGUS] folder scope skipped "
                  f"({type(e).__name__}: {e}); using {out_dir}",
                  file=sys.stderr)

    lock_key = args.execution_key or f"{args.testrun_id}_{args.item_id}"
    try:
        with audit_lock.acquire_key(
            settings.extractor.out_dir,
            lock_key,
            owner="argus.single",
        ):
            started = time.monotonic()
            started_at = dt.datetime.now(dt.timezone.utc)
            events.emit(
                "AUDIT",
                f"Started {label} (single key)",
                settings=settings,
            )
            _announce_audit_phase("auditing", label, run_id=run_id, total=1)
            rc = _run_single_locked(args, settings, out_dir, label)
            elapsed = int(time.monotonic() - started)
            incomplete_detail = _single_incomplete_detail(
                out_dir,
                label,
                started_at,
            )
            if incomplete_detail is not None:
                events.emit(
                    "ERROR",
                    f"1/1 (100%) | {label} | analysis incomplete: "
                    f"{incomplete_detail} | {elapsed}s",
                    settings=settings,
                )
            elif rc == 0:
                detail = _single_audit_result(out_dir, label)
                events.emit(
                    "COMPLETE",
                    f"1/1 (100%) | {label} | {detail} | {elapsed}s",
                    settings=settings,
                )
            else:
                events.emit(
                    "ERROR",
                    f"1/1 (100%) | {label} | "
                    f"single-key audit failed (rc={rc}) | {elapsed}s",
                    settings=settings,
                )
            if (
                not args.skip_audit
                and (rc == 0 or incomplete_detail is not None)
            ):
                _record_run_cost(
                    out_dir,
                    label=label,
                    kind="single",
                    keys=[label],
                    started_at=started_at,
                    settings=settings,
                )
            return 1 if rc == 0 and incomplete_detail is not None else rc
    except audit_lock.AuditLockBusy as e:
        print(f"[ARGUS] {e}", file=sys.stderr)
        return 0


def _single_audit_result(out_dir: Path, label: str) -> str:
    """Compact verdict/finding summary for a completed single-key audit."""
    audit_path = exec_paths.find_audit_json(out_dir, label)
    if audit_path is None:
        return "completed"
    try:
        audit = json.loads(audit_path.read_text())
    except (OSError, json.JSONDecodeError):
        return "completed"
    verdict = str(audit.get("overall_verdict") or "completed")
    findings = len(audit.get("findings") or [])
    noun = "finding" if findings == 1 else "findings"
    detail = f"{verdict}, {findings} {noun}"
    cost = audit.get("cost")
    if isinstance(cost, dict):
        value = cost.get("estimated_cost_usd")
        if isinstance(value, (int, float)):
            detail += f", ${value:.4f}"
    return detail


def _single_incomplete_detail(
    out_dir: Path,
    label: str,
    started_at: dt.datetime,
) -> str | None:
    audit_path = exec_paths.find_audit_json(out_dir, label)
    if audit_path is None:
        return None
    try:
        if audit_path.stat().st_mtime + 1 < started_at.timestamp():
            return None
        audit = json.loads(audit_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if analysis_status.derive_status(audit) != "incomplete":
        return None
    return (
        analysis_status.diagnostic(audit)
        or "a required analysis module did not complete"
    )


def _run_single_env_check(
    result: batch.KeyResult,
    settings: argus_config.ArgusConfig,
) -> None:
    executor = ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="single_env_check",
    )
    future = executor.submit(
        batch._run_env_check_for_key,
        result,
        settings,
        lock_held=True,
    )
    try:
        future.result(timeout=SINGLE_ENV_CHECK_TIMEOUT_S)
    except TimeoutError as e:
        raise analysis_status.AnalysisIncompleteError(
            "environment",
            "post-check analysis timed out after "
            f"{SINGLE_ENV_CHECK_TIMEOUT_S} seconds",
        ) from e
    finally:
        executor.shutdown(wait=future.done(), cancel_futures=True)


def _record_run_cost(
    run_dir: Path,
    *,
    label: str,
    kind: str,
    keys: list[str],
    started_at: dt.datetime,
    settings,
    only_new: bool = True,
) -> None:
    """Persist run-scoped cost without making audit success depend on it."""
    try:
        from argus.operations import costs as cost_monitor
        cost_monitor.record_run_cost(
            run_dir,
            label=label,
            kind=kind,
            keys=keys,
            started_at=started_at,
            model_rates=settings.costs.model_rates,
            only_new=only_new,
        )
    except Exception as exc:
        print(
            f"[ARGUS] run cost record skipped "
            f"({type(exc).__name__}: {exc})",
            file=sys.stderr,
        )


def _run_single_locked(args, settings, out_dir: Path, label: str) -> int:
    work_dir: Path | None = None
    report_started_at = dt.datetime.now(dt.timezone.utc)

    if not args.skip_extract:
        ex_cfg = extractor.ExtractorConfig.from_settings(
            settings.extractor,
            execution_key=args.execution_key,
            testrun_id=args.testrun_id,
            item_id=args.item_id,
            out_dir=out_dir,
        )
        try:
            extract_started = time.monotonic()
            with progress.step(f"extracting {label}") as s:
                rc, work_dir = extractor.run(ex_cfg)
                if rc != 0:
                    s.finish("x", f"extracting {label} — failed (rc={rc})")
                    events.emit(
                        "EXTRACT",
                        f"1/1 (100%) | {label} | failed (rc={rc}) | "
                        f"{time.monotonic() - extract_started:.1f}s",
                        settings=settings,
                    )
                    return rc
                pages = _count_pages(work_dir)
                s.finish("ok", f"extracted {label} ({pages} pages)")
                events.emit(
                    "EXTRACT",
                    f"1/1 (100%) | {label} | PDF {pages} "
                    f"{'page' if pages == 1 else 'pages'} | "
                    f"{time.monotonic() - extract_started:.1f}s",
                    settings=settings,
                )
        except extractor.AuthError as e:
            print(f"  reason: {e}", file=sys.stderr)
            return 2
        except Exception as e:
            print(f"  reason: {e}", file=sys.stderr)
            return 1
    else:
        work_dir = _find_existing_workdir(out_dir, args.execution_key
                                          or f"{args.testrun_id}_{args.item_id}")
        if work_dir is None:
            print(f"  reason: no existing extraction found for {label} under {out_dir}",
                  file=sys.stderr)
            return 1

    if args.skip_audit:
        return 0

    assert work_dir is not None

    synthetic_status = _synthetic_status(work_dir)
    if synthetic_status and not batch._has_any_evidence(work_dir):
        with progress.step(f"auditing {label}") as s:
            if synthetic_status in ("Not Applicable", "N/A"):
                batch._write_not_applicable_audit(
                    work_dir, args.execution_key or label)
            else:
                batch._write_blocked_audit(
                    work_dir, args.execution_key or label)
            s.finish("ok", f"audited {label} (synthetic, "
                           f"{synthetic_status}, no evidence)")
        report_refresh_failed = False
        try:
            _refresh_folder_report(
                work_dir,
                out_dir,
                settings.extractor.base_url,
                settings=settings,
                report_started_at=report_started_at,
            )
        except Exception as e:
            print(f"[ARGUS] report refresh skipped "
                  f"({type(e).__name__}: {e})", file=sys.stderr)
            report_refresh_failed = True
        _print_summary(work_dir, label)
        return 1 if report_refresh_failed else 0

    au_cfg = auditor.AuditConfig.from_settings(
        settings.auditor, execution_dir=work_dir,
        cost_rates=settings.costs.model_rates,
    )
    try:
        with progress.step(f"auditing {label}") as s:
            rc = auditor.run(au_cfg)
            if rc != 0:
                s.finish("x", f"auditing {label} — failed (rc={rc})")
                return rc
            s.finish("ok", f"audited {label}")
    except Exception as e:
        print(f"  reason: {e}", file=sys.stderr)
        return 1

    if synthetic_status in ("Blocked", "In Progress"):
        try:
            _merge_blocked_base_finding(work_dir)
        except Exception as e:
            print(f"[ARGUS] R20/R21 merge skipped "
                  f"({type(e).__name__}: {e})", file=sys.stderr)

    post_check_failed = False
    if batch._post_checks_enabled(settings):
        env_result = batch.KeyResult(
            key=args.execution_key or label,
            status="ok",
            audit_path=work_dir / "audit.json",
        )
        try:
            _run_single_env_check(
                env_result,
                settings,
            )
        except Exception as e:
            diagnostic = (
                e.diagnostic
                if isinstance(e, analysis_status.AnalysisIncompleteError)
                else f"{type(e).__name__}: {e}"
            )
            batch._mark_result_incomplete(
                env_result,
                "environment",
                diagnostic,
            )
            print(
                f"[ARGUS] post-check analysis incomplete for {label}: "
                f"{diagnostic}",
                file=sys.stderr,
            )
            post_check_failed = True

    report_refresh_failed = False
    try:
        _refresh_folder_report(
            work_dir,
            out_dir,
            settings.extractor.base_url,
            settings=settings,
            report_started_at=report_started_at,
        )
    except Exception as e:
        print(f"[ARGUS] report refresh skipped "
              f"({type(e).__name__}: {e})", file=sys.stderr)
        report_refresh_failed = True

    _print_summary(work_dir, label)
    return 1 if post_check_failed or report_refresh_failed else 0


FOLDER_META_FILENAME = "_folder_meta.json"


@dataclass(frozen=True)
class _TestPlanScope:
    key: str
    plan_id: int
    runs: list[dict[str, Any]]


def _check_report_folder_scope(folder_root: Path, plan_scope: _TestPlanScope | None) -> None:
    """Do not let another plan or a run-folder request reuse a plan's output."""
    meta_path = folder_root / FOLDER_META_FILENAME
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            if not isinstance(meta, dict):
                raise ValueError("invalid report folder metadata")
        except (OSError, ValueError) as exc:
            raise ValueError(f"could not verify report scope for {folder_root.name}") from exc
    existing_key = meta.get("test_plan_key")
    expected_key = plan_scope.key if plan_scope else None
    if existing_key != expected_key:
        # A new/empty output directory can be assigned to the plan.
        if existing_key or (
            plan_scope and folder_root.exists() and any(folder_root.iterdir())
        ):
            raise ValueError(
                f"report folder {folder_root.name!r} already belongs to "
                f"{existing_key or 'a different audit scope'}"
            )


def _write_folder_meta(
    folder_root: Path, folder_name: str, *, plan_scope: _TestPlanScope | None = None,
) -> None:
    """Record the display name and optional plan ownership of an output dir.

    Per-folder scoping keeps each Zephyr folder in its own sandbox —
    mixing folders under one root breaks batch retry files and
    aggregate reports. The marker makes the folder level
    self-identifying across the three historical output layouts, so
    tooling never has to guess from path shape. Plan ownership must be
    persisted before auditing; ordinary folder metadata is best-effort.
    """
    try:
        meta_path = folder_root / FOLDER_META_FILENAME
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
            except (OSError, json.JSONDecodeError):
                meta = {}
        meta.setdefault("created_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
        meta["folder_name"] = folder_name
        meta["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if plan_scope:
            meta.update(
                source_type="testplan",
                test_plan_key=plan_scope.key,
                test_plan_id=plan_scope.plan_id,
                test_run_keys=[run["key"] for run in plan_scope.runs],
            )
        atomic_io.atomic_write_json(meta_path, meta)
    except OSError as e:
        if plan_scope:
            raise
        print(f"[ARGUS] could not write {FOLDER_META_FILENAME}: {e}",
              file=sys.stderr)


def _refresh_folder_report(
    work_dir: Path,
    out_dir: Path,
    jira_base_url: str,
    *,
    settings: argus_config.ArgusConfig | None = None,
    report_started_at: dt.datetime | None = None,
) -> None:
    """Re-render the per-folder _argus.html + _report.md that contains `work_dir`."""
    out_dir_resolved = out_dir.resolve()
    project_output_root = paths.output_root(settings)
    cur = work_dir.resolve()
    folder_dir: Path | None = None
    while cur.parent != cur:
        if (cur / FOLDER_META_FILENAME).exists() or (cur / "_argus.html").exists():
            folder_dir = cur
            break
        if cur.parent == project_output_root:
            folder_dir = cur
            break
        if cur.parent == out_dir_resolved and out_dir_resolved == project_output_root:
            folder_dir = cur
            break
        cur = cur.parent
    if folder_dir is None or not folder_dir.exists():
        return
    # A single-key retry updates the shared folder report. Replacing its
    # manifest with [work_dir.name] hides every other execution and makes a
    # comma-separated batch display only its last result.
    _restore_single_execution_scope(folder_dir)
    audit_path = work_dir / "audit.json"
    audit_status: str | None = None
    if audit_path.is_file():
        try:
            persisted_audit = json.loads(audit_path.read_text())
        except (OSError, json.JSONDecodeError):
            persisted_audit = None
        if isinstance(persisted_audit, dict):
            audit_status = analysis_status.derive_status(persisted_audit)
    recovery_token: str | None = None
    if audit_status == "incomplete":
        _record_single_key_failure(
            folder_dir,
            work_dir.name,
            recovery_token=None,
            require_recovery_match=False,
        )
    elif audit_status in {"complete", "not_applicable"}:
        recovery_token = _adopt_key_into_latest_run_manifest(
            folder_dir,
            work_dir.name,
        )
    _reconcile_failed_keys(folder_dir)
    if settings is not None:
        if not _publish_folder_reports(folder_dir, settings):
            _record_single_key_failure(
                folder_dir, work_dir.name, recovery_token=recovery_token,
                require_recovery_match=True,
            )
            raise RuntimeError("folder report publication failed")
        return
    from argus.reporting import report as _argus_report
    _argus_report.configure(jira_base_url)
    summaries = _argus_report.scan_output_dir(folder_dir)
    folder_dir.joinpath("_argus.html").write_text(
        _argus_report.generate_html_report(folder_dir, summaries=summaries))
    folder_dir.joinpath("_report.md").write_text(
        _argus_report.generate_report(folder_dir, summaries=summaries))
    print(f"[ARGUS] refreshed report: {folder_dir.name}/_argus.html",
          file=sys.stderr)


@dataclass
class _AuditState:
    execution_date: str = ""
    evidence_fingerprint: str = ""
    analysis_status: str = "complete"
    policy_fingerprint: str = ""


@dataclass
class _FolderEnumeration:
    """Result of walking every run/item in a Zephyr folder."""
    emitted_keys: list[str] = dc_field(default_factory=list)
    emitted_user_keys: list[str | None] = dc_field(default_factory=list)
    na_keys: list[str] = dc_field(default_factory=list)
    na_user_keys: list[str | None] = dc_field(default_factory=list)
    blocked_keys: list[str] = dc_field(default_factory=list)
    current_keys: list[str] = dc_field(default_factory=list)
    items_by_run: dict[Any, list[dict[str, Any]]] = dc_field(default_factory=dict)
    total_executed: int = 0
    total_ignored_status: int = 0
    total_skipped_status: int = 0
    total_skipped_dup: int = 0
    total_unresolved: int = 0
    total_reaudits: int = 0
    total_runs_failed: int = 0


def _cached_audit_matches_live(
    cached: _AuditState | None,
    summary: dict[str, Any],
) -> bool:
    if cached is None or cached.analysis_status == "incomplete":
        return False
    from argus.operations import rule_runtime
    if rule_runtime.digest() and cached.policy_fingerprint != rule_runtime.digest():
        return False
    live_date = summary.get("execution_date")
    live_fp = summary.get("evidence_fingerprint")
    cached_fp = cached.evidence_fingerprint or ""
    if cached_fp and not cached_fp.startswith(
            extractor.EVIDENCE_FINGERPRINT_PREFIX):
        if summary.get("test_case_weblinks_checked"):
            # Legacy attachment-only hashes cannot establish that TV's
            # expanded defect inputs were checked; upgrade affected cases.
            return False
        # Audits written before execution state was hashed carry the
        # attachment-only fingerprint. Compare like-for-like so they are not
        # all re-audited at once; the first re-audit for any reason upgrades
        # them to the state-aware fingerprint.
        legacy_fp = summary.get("evidence_fingerprint_legacy")
        if isinstance(legacy_fp, str):
            live_fp = legacy_fp
    if not isinstance(live_date, str) or not isinstance(live_fp, str):
        return False
    return (
        bool(cached.execution_date)
        and bool(cached_fp)
        and cached.execution_date == live_date
        and cached_fp == live_fp
    )


def _resolve_enumeration_summary(
    session,
    base_url: str,
    result_id: int | str,
    *,
    run_label: str,
    fallback_user_key: str | None,
    test_case_weblink_status_ids: set[int] | None = None,
    test_case_weblink_cache: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any] | None:
    """Resolve live state, falling back to the execution key when needed."""
    summary: dict[str, Any] | None = None
    failure = "summary lookup returned no data"
    try:
        link_options: dict[str, Any] = {}
        if test_case_weblink_status_ids is not None:
            link_options = {
                "test_case_weblink_status_ids": test_case_weblink_status_ids,
                "test_case_weblink_cache": test_case_weblink_cache,
            }
        summary = extractor.get_testresult_summary(
            session, base_url, result_id, **link_options)
    except (requests.RequestException, ValueError) as e:
        failure = type(e).__name__
    else:
        if summary and summary.get("key"):
            return summary
        if summary:
            failure = "summary response omitted the execution key"

    try:
        e_key = extractor.get_testresult_key(session, base_url, result_id)
    except requests.RequestException as e:
        log.warning(
            "unresolved testresult %s in %s: %s; key fallback failed: %s",
            result_id, run_label, failure, type(e).__name__,
        )
        return None
    if not e_key:
        log.warning(
            "unresolved testresult %s in %s: %s; key fallback returned no key",
            result_id, run_label, failure,
        )
        return None

    log.warning(
        "testresult %s in %s: %s; using key-only fallback %s",
        result_id, run_label, failure, e_key,
    )
    # Preserve live metadata when only the key was omitted. If the summary
    # lookup failed entirely, cached equivalence cannot be proven and the
    # missing fields intentionally force a fail-open re-audit.
    return {
        "key": e_key,
        "user_key": (
            summary.get("user_key") if summary else fallback_user_key
        ),
        "execution_date": (
            summary.get("execution_date") if summary else None
        ),
        "evidence_fingerprint": (
            summary.get("evidence_fingerprint") if summary else None
        ),
    }


def _enumerate_folder_items(
    session,
    base_url: str,
    runs: list[dict[str, Any]],
    executed_ids: set,
    na_ids: set,
    audited: dict[str, _AuditState],
    blocked_ids: set | None = None,
    ignored_ids: set | None = None,
    pause_seconds: float = 1.0,
    selected_run_keys: set[str] | None = None,
    test_case_weblink_status_ids: set[int] | None = None,
) -> _FolderEnumeration:
    """Enumerate only the requested runs, or the whole folder if unfiltered."""
    enum = _FolderEnumeration()
    test_case_weblink_cache: dict[str, list[dict[str, Any]]] = {}
    if selected_run_keys is not None:
        wanted = {key.strip().upper() for key in selected_run_keys}
        runs = [
            run for run in runs
            if str(run.get("key") or "").strip().upper() in wanted
        ]
    for i, run in enumerate(runs):
        run_id = run.get("id")
        if not run_id:
            continue
        if pause_seconds > 0 and i > 0 and i % 10 == 0:
            time.sleep(pause_seconds)
        try:
            with progress.step(f"enumerating items in run {run.get('key') or run_id}") as s:
                items = extractor.get_testrun_items(session, base_url, run_id)
                s.finish("ok", f"{len(items)} item(s)")
        except requests.RequestException as e:
            enum.total_runs_failed += 1
            print(f"  skipping run {run.get('key') or run_id}: {e}", file=sys.stderr)
            continue
        enum.items_by_run[run_id] = items

        for item in items:
            run_label = str(run.get("key") or run_id)
            last = item.get("$lastTestResult") or {}
            status = last.get("testResultStatusId")
            result_id = last.get("id")
            if ignored_ids and status in ignored_ids:
                enum.total_ignored_status += 1
                continue
            if status in na_ids:
                if not result_id:
                    enum.total_unresolved += 1
                    continue
                summary = _resolve_enumeration_summary(
                    session,
                    base_url,
                    result_id,
                    run_label=run_label,
                    fallback_user_key=last.get("userKey"),
                    test_case_weblink_status_ids=test_case_weblink_status_ids,
                    test_case_weblink_cache=test_case_weblink_cache,
                )
                if summary is None:
                    enum.total_unresolved += 1
                    continue
                na_e_key = summary["key"]
                enum.current_keys.append(na_e_key)
                cached = audited.get(na_e_key)
                if _cached_audit_matches_live(cached, summary):
                    enum.total_skipped_dup += 1
                    continue
                if cached is not None:
                    enum.total_reaudits += 1
                enum.na_keys.append(na_e_key)
                enum.na_user_keys.append(summary.get("user_key"))
                continue
            if blocked_ids and status in blocked_ids:
                if not result_id:
                    enum.total_unresolved += 1
                    continue
                summary = _resolve_enumeration_summary(
                    session,
                    base_url,
                    result_id,
                    run_label=run_label,
                    fallback_user_key=last.get("userKey"),
                    test_case_weblink_status_ids=test_case_weblink_status_ids,
                    test_case_weblink_cache=test_case_weblink_cache,
                )
                if summary is None:
                    enum.total_unresolved += 1
                    continue
                blk_e_key = summary["key"]
                enum.current_keys.append(blk_e_key)
                cached = audited.get(blk_e_key)
                if _cached_audit_matches_live(cached, summary):
                    enum.total_skipped_dup += 1
                    continue
                if cached is not None:
                    enum.total_reaudits += 1
                enum.blocked_keys.append(blk_e_key)
                continue
            if status not in executed_ids:
                enum.total_skipped_status += 1
                continue
            enum.total_executed += 1
            if not result_id:
                enum.total_unresolved += 1
                continue
            summary = _resolve_enumeration_summary(
                session,
                base_url,
                result_id,
                run_label=run_label,
                fallback_user_key=last.get("userKey"),
                test_case_weblink_status_ids=test_case_weblink_status_ids,
                test_case_weblink_cache=test_case_weblink_cache,
            )
            if summary is None:
                enum.total_unresolved += 1
                continue
            e_key = summary["key"]
            enum.current_keys.append(e_key)
            cached = audited.get(e_key)
            if _cached_audit_matches_live(cached, summary):
                enum.total_skipped_dup += 1
                continue
            if cached is not None:
                enum.total_reaudits += 1
            enum.emitted_keys.append(e_key)
            enum.emitted_user_keys.append(summary.get("user_key"))
    return enum


def _normalize_execution_status_name(name: str) -> str:
    """Return a stable status token across Jira display/i18n spellings."""
    return re.sub(r"[\s_-]+", " ", name.strip()).upper()


def _run_test_plan(
    plan: dict[str, Any],
    runs: list[dict[str, Any]],
    settings,
    *,
    reports_only: bool = False,
    selected_run_keys: set[str] | None = None,
) -> int:
    """Audit only this plan's linked runs under its title-based output folder."""
    from argus.pipeline.test_plans import PLAN_KEY_RE

    key = str(plan.get("key") or "")
    title = plan.get("name")
    if (
        not PLAN_KEY_RE.fullmatch(key)
        or not isinstance(title, str)
        or not title.strip()
        or not isinstance(plan.get("id"), int)
        or plan["id"] <= 0
        or str(plan.get("projectId") or "") not in settings.extractor.project_ids
    ):
        print("[ARGUS] test plan is missing a valid key, title, ID, or project", file=sys.stderr)
        return 1
    if not runs or any(
        not extractor._TESTRUN_KEY_RE.fullmatch(str(run.get("key") or ""))
        or not isinstance(run.get("id"), int)
        or run["id"] <= 0
        or str(run.get("projectId") or "") != str(plan.get("projectId"))
        for run in runs
    ):
        print(f"[ARGUS] {key} has no valid linked runs in this project", file=sys.stderr)
        return 1
    unique_runs = list({run["key"]: run for run in runs}.values())
    scope = _TestPlanScope(key, plan["id"], unique_runs)
    return _run_folder_list(
        title.strip(),
        settings,
        selected_run_keys=(
            selected_run_keys if selected_run_keys is not None
            else {run["key"] for run in unique_runs}
        ),
        reports_only=reports_only,
        plan_scope=scope,
    )


def _run_folder_list(
    folder_name: str,
    settings,
    *,
    force: bool = False,
    selected_run_keys: set[str] | None = None,
    reports_only: bool = False,
    plan_scope: _TestPlanScope | None = None,
) -> int:
    """Audit a Jira folder or a resolved plan within its own output folder.

    Announces "preparing" before the (slow) Jira enumeration so dashboards
    and ``/argus status`` show the run immediately; the batch stage later
    replaces that with "auditing" and the real key count.
    """
    from argus.operations import control as _argus_control
    from argus.pipeline.test_cases import CASE_KEY_RE

    # Older API/bot launchers classified T-keys as folders. Accept their
    # existing command shape during rolling upgrades without looking up a
    # folder named after a Jira key.
    if (
        CASE_KEY_RE.fullmatch(folder_name.strip().upper())
        and selected_run_keys is None and plan_scope is None and not reports_only
    ):
        from argus.operations.manual_batch import run_targets
        return run_targets(settings, case_keys=[folder_name.strip().upper()])
    run_id = _audit_run_id()
    if _argus_control.is_paused():
        st = _argus_control.status()
        print(f"[ARGUS] argus paused (by {st.get('paused_by')!r} at "
              f"{st.get('paused_at')!r}, reason="
              f"{st.get('reason')!r}). Skipping. "
              f"Resume with: /argus resume  OR  "
              "argus control resume",
              file=sys.stderr)
        _finish_audit_announcement(run_id)
        return 0
    _announce_audit_phase("preparing", folder_name, run_id=run_id)
    unregister_stop = stop_signal.register_on_stop(
        lambda: _announce_audit_stopped(run_id, folder_name))
    rc: int | None = None
    error: BaseException | None = None
    try:
        rc = _run_folder_list_prepared(
            folder_name,
            settings,
            force=force,
            selected_run_keys=selected_run_keys,
            run_id=run_id,
            reports_only=reports_only,
            plan_scope=plan_scope,
        )
        return rc
    except Exception as exc:
        error = exc
        raise
    finally:
        unregister_stop()
        _finish_audit_announcement(run_id, rc=rc, error=error)


def _run_folder_list_prepared(
    folder_name: str,
    settings,
    *,
    force: bool,
    selected_run_keys: set[str] | None,
    run_id: str,
    reports_only: bool = False,
    plan_scope: _TestPlanScope | None = None,
) -> int:
    session = extractor.build_session(settings.extractor)
    base_url = settings.extractor.base_url
    project_id = (
        str(plan_scope.runs[0]["projectId"])
        if plan_scope else settings.extractor.project_id
    )
    out_dir = settings.extractor.out_dir

    if plan_scope is None:
        try:
            with progress.step(f"resolving folder '{folder_name}'") as s:
                resolved_folder = extractor.find_folder_for_profile(
                    session, settings.extractor, folder_name)
                if not resolved_folder:
                    s.finish("x", f"folder '{folder_name}' not found")
                    return 1
                project_id, folder = resolved_folder
                folder_id = folder["id"]
                s.finish("ok", f"resolved '{folder_name}' -> id={folder.get('id')}")
        except extractor.AuthError as e:
            print(f"  reason: {e}", file=sys.stderr)
            return 2
        except ValueError as e:
            print(f"  reason: {e}", file=sys.stderr)
            return 1

    folder_root = _scoped_folder_out_dir(out_dir, folder_name)
    try:
        _check_report_folder_scope(folder_root, plan_scope)
    except ValueError as exc:
        print(f"[ARGUS] {exc}", file=sys.stderr)
        return 1
    folder_root.mkdir(parents=True, exist_ok=True)
    _write_folder_meta(folder_root, folder_name, plan_scope=plan_scope)
    print(f"[ARGUS] folder '{folder_name}' -> output scoped to {folder_root}",
          file=sys.stderr)
    try:
        state_dir = settings.general.state_dir
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "last_folder_out_dir").write_text(
            str(folder_root.resolve()) + "\n")
    except OSError as e:
        print(f"[ARGUS] could not record last_folder_out_dir: {e}",
              file=sys.stderr)

    if plan_scope:
        runs = plan_scope.runs
    else:
        try:
            with progress.step(f"listing test runs in folder {folder_id}") as s:
                runs = extractor.get_test_runs_in_folder(
                    session, base_url, folder_id, project_id)
                if not settings.folders.include_descendant_runs:
                    runs = [
                        run for run in runs
                        if str(run.get("folderId")) == str(folder_id)
                    ]
                s.finish("ok", f"{len(runs)} test run(s) in folder")
        except extractor.AuthError as e:
            print(f"  reason: {e}", file=sys.stderr)
            return 2

    if not runs:
        print("  reason: no test runs in folder", file=sys.stderr)
        return 1 if selected_run_keys is not None else 0
    wanted_run_keys: set[str] | None = None
    if selected_run_keys is not None:
        wanted_run_keys = {
            key.strip().upper() for key in selected_run_keys
        }
        selected = [
            run for run in runs
            if str(run.get("key") or "").strip().upper() in wanted_run_keys
        ]
        found = {
            str(run.get("key") or "").strip().upper()
            for run in selected
        }
        missing = sorted(wanted_run_keys - found)
        if missing:
            print(
                "  reason: selected test run(s) not found in folder: "
                + ", ".join(missing),
                file=sys.stderr,
            )
            return 1
        print(
            f"[ARGUS] selected {len(selected)} exact test run(s): "
            f"{', '.join(sorted(found))}",
            file=sys.stderr,
        )
        # The same boundary governs enumeration, coverage, and the report
        # manifest. A C-key request must never expand to sibling runs.
        runs = selected

    try:
        with progress.step(f"fetching status ids for project {project_id}") as s:
            status_map = extractor.get_project_status_ids(
                session, base_url, project_id)
            executed_roles = {"pass", "fail", "passed_with_issue"}
            executed_names = {
                _normalize_execution_status_name(name)
                for name in (
                    *EXECUTED_STATUS_NAMES,
                    *(name for role, name in settings.policy.statuses.items()
                      if role in executed_roles),
                    *(name for name, role in settings.policy.status_aliases.items()
                      if role in executed_roles),
                )
            }
            executed_ids = {
                sid for name, sid in status_map.items()
                if _normalize_execution_status_name(name) in executed_names
            }
            if not executed_ids:
                s.finish("x", f"no executed status ids found for project {project_id}")
                return 1
            na_ids = {sid for short, sid in status_map.items()
                      if short.upper().replace("_", " ") == "NOT APPLICABLE"}
            blocked_ids = {
                sid for short, sid in status_map.items()
                if _normalize_execution_status_name(short) in (
                    "BLOCKED",
                    "IN PROGRESS",
                )
            }
            configured_ignored_statuses = {
                _normalize_execution_status_name(name)
                for name in settings.extractor.ignored_execution_statuses
            }
            ignored_ids = {
                sid for short, sid in status_map.items()
                if _normalize_execution_status_name(short)
                in configured_ignored_statuses
            }
            unmatched_ignored_statuses = configured_ignored_statuses - {
                _normalize_execution_status_name(short)
                for short in status_map
            }
            if unmatched_ignored_statuses:
                log.warning(
                    "configured ignored execution status(es) not found for "
                    "project %s: %s",
                    project_id,
                    ", ".join(sorted(unmatched_ignored_statuses)),
                )
            _I18N_TO_DISPLAY = {
                "PASS": "Pass",
                "FAIL": "Fail",
                "BLOCKED": "Blocked",
                "IN_PROGRESS": "In Progress",
                "NOT_EXECUTED": "Not Executed",
                "NOT EXECUTED": "Not Executed",
                "NOT_APPLICABLE": "Not Applicable",
                "NOT APPLICABLE": "Not Applicable",
                "PASSED_WITH_ISSUE": "Passed With Issue",
                "PASSED WITH ISSUE": "Passed With Issue",
            }
            status_id_to_name: dict[int, str] = {
                sid: _I18N_TO_DISPLAY.get(
                    _normalize_execution_status_name(short),
                    _normalize_execution_status_name(short).title(),
                )
                for short, sid in status_map.items()
            }
            s.finish("ok", f"executed statuses -> {sorted(executed_ids)}")
    except extractor.AuthError as e:
        print(f"  reason: {e}", file=sys.stderr)
        return 2

    run_started_at = dt.datetime.now(dt.timezone.utc)
    audited = {} if force else _audited_keys_with_state(folder_root)

    try:
        enum = _enumerate_folder_items(
            session, base_url, runs, executed_ids, na_ids, audited,
            blocked_ids=blocked_ids,
            ignored_ids=ignored_ids,
            pause_seconds=settings.extractor.enumeration_pause_seconds,
            selected_run_keys=wanted_run_keys,
            test_case_weblink_status_ids=(
                (set(status_map.values()) if settings.extractor.test_case_weblinks_all_statuses else
                 extractor.issue_status_ids({sid: name for name, sid in status_map.items()}))
                if settings.extractor.include_test_case_weblinks else None
            ),
        )
    except extractor.AuthError as e:
        print(f"  reason: {e}", file=sys.stderr)
        return 2
    emitted_keys = enum.emitted_keys
    emitted_user_keys = enum.emitted_user_keys
    na_keys = enum.na_keys
    blocked_keys = enum.blocked_keys
    current_keys = list(dict.fromkeys(enum.current_keys))
    items_by_run = enum.items_by_run
    total_reaudits = enum.total_reaudits

    unresolved_note = (
        f" / {enum.total_unresolved} unresolved" if enum.total_unresolved else ""
    )
    runs_failed_note = (
        f" / {enum.total_runs_failed} run(s) skipped" if enum.total_runs_failed else ""
    )
    reaudits_note = (
        f" / {total_reaudits} re-audit(s)" if total_reaudits else ""
    )
    blocked_note = (
        f" / {len(blocked_keys)} blocked/in-progress" if blocked_keys else ""
    )
    ignored_note = (
        f" / {enum.total_ignored_status} ignored by profile"
        if enum.total_ignored_status else ""
    )
    progress.done(
        f"folder '{folder_name}'",
        f"[{len(emitted_keys)} new / {enum.total_executed} executed / "
        f"{enum.total_skipped_dup} already-audited / "
        f"{enum.total_skipped_status} unexecuted"
        f"{unresolved_note}{runs_failed_note}{reaudits_note}{blocked_note}"
        f"{ignored_note}]",
    )

    if emitted_keys and not reports_only:
        _print_by_tester(session, base_url, emitted_keys, emitted_user_keys)

    if runs:
        _write_folder_coverage(
            session, base_url, folder_name, runs, status_id_to_name,
            items_by_run, folder_root, ignored_status_ids=ignored_ids)

    try:
        from argus.core import users as _users
        _users.get_default_cache().save()
    except Exception as _e:
        print(f"[ARGUS] user cache save failed: "
              f"{type(_e).__name__}: {_e}", file=sys.stderr)

    run_keys = list(dict.fromkeys([*na_keys, *blocked_keys, *emitted_keys]))

    if na_keys and not reports_only:
        _process_na_items(na_keys, settings, folder_root)
    if blocked_keys and not reports_only:
        _process_blocked_items(blocked_keys, settings, folder_root)

    if reports_only or not emitted_keys:
        _announce_audit_phase(
            "publishing", folder_name, run_id=run_id, total=len(current_keys),
        )
        manifest_path = _write_latest_run_manifest(
            folder_root,
            folder_name=folder_name,
            scope_keys=current_keys,
            selected_keys=run_keys,
            results=[],
            synthetic_keys=[] if reports_only else [*na_keys, *blocked_keys],
            started_at=run_started_at,
        )
        if manifest_path is None:
            print(
                f"[ARGUS] folder run superseded before publication: "
                f"{folder_name}",
                file=sys.stderr,
            )
            return 0
        if run_keys and not reports_only:
            _record_run_cost(
                folder_root,
                label=folder_name,
                kind="folder",
                keys=run_keys,
                started_at=run_started_at,
                settings=settings,
            )
        _reconcile_failed_keys(folder_root)
        return 0 if _publish_folder_reports(folder_root, settings) else 1

    concurrency = settings.batch.concurrency
    extractor_concurrency = settings.batch.extractor_concurrency
    print(
        f"\nbatch: {len(emitted_keys)} keys, concurrency={concurrency} "
        f"(extractor={extractor_concurrency})",
        file=sys.stderr,
    )


    import time as _time
    from argus.operations import control as _argus_control_mark
    notify.post_audit_started(folder_name, len(emitted_keys),
                              n_reaudits=total_reaudits,
                              settings=settings)
    _start_ts = _time.monotonic()

    from argus.reporting import publisher as report_publisher
    checkpoint_failures: dict[
        str,
        tuple[batch.KeyResult, Exception],
    ] = {}
    checkpoint_available = False

    def checkpoint_progress(
        result: batch.KeyResult,
        done: int,
        total: int,
    ) -> None:
        activity_progress(result, done, total)
        if not checkpoint_available:
            return
        try:
            report_publisher.checkpoint_audit_key(
                folder_root,
                run_id=run_id,
                key=result.key,
                status=result.status,
                verdict=result.verdict,
                findings=result.findings,
                reason=result.reason,
                elapsed_s=result.elapsed_s,
                audit_path=result.audit_path,
            )
            checkpoint_failures.pop(result.key, None)
        except Exception as exc:
            checkpoint_failures[result.key] = (result, exc)
            print(
                f"[ARGUS] checkpoint failed for {result.key}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    def mark_checkpoint_run_failed(diagnostic: str) -> None:
        try:
            report_publisher.mark_audit_failed(
                folder_root,
                run_id=run_id,
                diagnostic=diagnostic,
            )
        except Exception as exc:
            print(
                f"[ARGUS] failed to publish run failure status: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    _argus_control_mark.mark_audit_running(
        folder_name,
        len(emitted_keys),
        owner_token=run_id,
        requested_by=_audit_requested_by(),
        target=_audit_target_label(),
    )
    try:
        activity_progress = _activity_progress_callback(
            len(emitted_keys), settings)
        extraction_progress = _activity_extraction_callback(
            len(emitted_keys), settings)
        try:
            report_publisher.start_audit_run(
                folder_root,
                run_id=run_id,
                expected_keys=emitted_keys,
            )
        except Exception as exc:
            print(
                "[ARGUS] failed to initialize durable checkpoints; "
                f"continuing audit: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
        else:
            checkpoint_available = True
        results = batch.run_batch(
            emitted_keys, settings,
            concurrency=concurrency,
            extractor_concurrency=extractor_concurrency,
            out=sys.stderr,
            on_extraction_complete=extraction_progress,
            on_key_finalized=checkpoint_progress,
            out_dir=folder_root,
        )
        _announce_audit_phase(
            "publishing", folder_name, run_id=run_id, total=len(current_keys),
        )
        manifest_path = _write_latest_run_manifest(
            folder_root,
            folder_name=folder_name,
            scope_keys=current_keys,
            selected_keys=run_keys,
            results=results,
            synthetic_keys=[*na_keys, *blocked_keys],
            started_at=run_started_at,
        )
        if manifest_path is None:
            print(
                json.dumps({
                    "event": "argus_run_superseded",
                    "folder": folder_root.name,
                    "run_id": run_id,
                }, sort_keys=True),
                file=sys.stderr,
            )
            return 0

        if checkpoint_available:
            for key, (result, _previous_error) in list(
                checkpoint_failures.items(),
            ):
                try:
                    report_publisher.checkpoint_audit_key(
                        folder_root,
                        run_id=run_id,
                        key=result.key,
                        status=result.status,
                        verdict=result.verdict,
                        findings=result.findings,
                        reason=result.reason,
                        elapsed_s=result.elapsed_s,
                        audit_path=result.audit_path,
                    )
                    checkpoint_failures.pop(key, None)
                except Exception as exc:
                    checkpoint_failures[key] = (result, exc)

        if not checkpoint_available:
            print(
                json.dumps({
                    "event": "argus_checkpoint_publication",
                    "folder": folder_root.name,
                    "run_id": run_id,
                    "status": "unavailable",
                }, sort_keys=True),
                file=sys.stderr,
            )
            publication_ok = _publish_folder_reports(
                folder_root,
                settings,
            )
        elif checkpoint_failures:
            failed_keys = ", ".join(sorted(checkpoint_failures))
            diagnostic = (
                "durable testcase checkpoint failed for: "
                f"{failed_keys}"
            )
            mark_checkpoint_run_failed(diagnostic)
            print(
                json.dumps({
                    "event": "argus_checkpoint_publication",
                    "folder": folder_root.name,
                    "run_id": run_id,
                    "status": "incomplete",
                    "failed_keys": sorted(checkpoint_failures),
                }, sort_keys=True),
                file=sys.stderr,
            )
            publication_ok = False
        else:
            try:
                report_publisher.mark_audit_publishing(
                    folder_root,
                    run_id=run_id,
                )
            except Exception as exc:
                publication_ok = False
                mark_checkpoint_run_failed(
                    f"{type(exc).__name__}: {exc}",
                )
                print(
                    f"[ARGUS] consolidated publication blocked: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
            else:
                publication_ok = _publish_folder_reports(
                    folder_root,
                    settings,
                )
                if not publication_ok:
                    mark_checkpoint_run_failed(
                        "folder report generation or upload failed",
                    )
    except Exception as exc:
        if checkpoint_available:
            mark_checkpoint_run_failed(
                f"{type(exc).__name__}: {exc}",
            )
        raise
    finally:
        _argus_control_mark.mark_audit_done(run_id)
        try:
            from argus.core import users as _users_after_batch
            _users_after_batch.get_default_cache().save()
        except Exception as exc:
            print(f"warning: user-cache save failed after batch: {exc}",
                  file=sys.stderr)
    if not publication_ok:
        for result in results:
            if result.audit_path is not None:
                batch._mark_result_incomplete(
                    result,
                    "publication",
                    "folder report generation or upload failed",
                )

    failed_path = _write_and_reconcile_failed_keys(
        results,
        folder_root,
        run_started_at=run_started_at,
    )
    summary_line = batch.summarize(results)
    print(f"batch: {summary_line}", file=sys.stderr)
    skipped_block = batch.format_skipped_by_tester(results)
    if skipped_block:
        print(skipped_block, file=sys.stderr)
    if failed_path:
        print(
            f"batch: failed keys -> {failed_path} "
            f"(retry: argus batch --keys-file {failed_path})",
            file=sys.stderr,
        )

    from argus.reporting import report as _argus_report
    n_ok = sum(1 for r in results if r.status == "ok")
    n_failed = sum(
        1 for r in results
        if r.status in ("failed", "auth_failed", "analysis_incomplete")
    )
    n_flagged = 0
    for r in results:
        if r.status != "ok" or r.audit_path is None:
            continue
        try:
            audit_obj = json.loads(r.audit_path.read_text())
            metadata_path = r.audit_path.parent / "metadata.json"
            try:
                metadata_obj = json.loads(metadata_path.read_text())
            except (OSError, json.JSONDecodeError):
                metadata_obj = {}
            sm = _argus_report._build_summary(
                audit_obj, metadata_obj, r.audit_path.parent)
            if _argus_report._is_flagged(sm):
                n_flagged += 1
        except (OSError, json.JSONDecodeError):
            pass
    notify.post_audit_done(
        folder_name, n_ok=n_ok, n_flagged=n_flagged, n_failed=n_failed,
        elapsed_seconds=_time.monotonic() - _start_ts,
        settings=settings,
    )

    _record_run_cost(
        folder_root,
        label=folder_name,
        kind="folder",
        keys=run_keys,
        started_at=run_started_at,
        settings=settings,
    )

    if any(r.status == "auth_failed" for r in results):
        return 2
    if any(
        r.status in ("failed", "analysis_incomplete")
        for r in results
    ):
        return 1
    return 0


def _write_folder_coverage(
    session,
    base_url: str,
    folder_name: str,
    runs: list[dict[str, Any]],
    status_id_to_name: dict[int, str],
    items_by_run: dict[Any, list[dict[str, Any]]],
    folder_root: Path,
    *,
    ignored_status_ids: set[int] | None = None,
) -> None:
    """R15: compute per-folder coverage gaps and write `_coverage.json`."""
    try:
        from argus.domain import folder_coverage as _coverage
        from argus.domain import history_index as _history_index

        coverage_items = extractor.build_coverage_items(
            session, base_url, runs, status_id_to_name,
            items_by_run=items_by_run,
            resolve_e_keys=True,
            ignored_status_ids=ignored_status_ids,
        )
        snapshot = _coverage.CoverageSnapshot(
            folder_name=folder_name,
            items=coverage_items,
        )
        try:
            hidx = _history_index.HistoryIndex.get_shared(folder_root)
        except Exception as e:
            log.warning("history index unavailable for %s (R15a Blocked "
                        "escalation disabled): %s: %s",
                        folder_root, type(e).__name__, e)
            hidx = None
        from argus.operations import rule_runtime
        findings = _coverage.compute_gaps(
            snapshot, history_idx=hidx, **rule_runtime.coverage_options(),
        )
        coverage_path = folder_root / "_coverage.json"
        coverage_path.write_text(json.dumps(
            rule_runtime.annotate(_coverage.to_coverage_json(snapshot, findings)),
            indent=2))
        print(
            f"[ARGUS] coverage: {len(findings)} gap(s) "
            f"-> {coverage_path.name}",
            file=sys.stderr,
        )
    except Exception as _e:
        print(f"[ARGUS] coverage computation failed: "
              f"{type(_e).__name__}: {_e}",
              file=sys.stderr)


def _process_na_items(na_keys: list[str], settings, folder_root: Path) -> None:
    """Write synthetic audits for N/A items (no model)."""
    print(f"\n[ARGUS] processing {len(na_keys)} N/A item(s) "
          f"(synthetic audits, no model)", file=sys.stderr)
    from argus.pipeline import batch as _argus_batch
    n_na_ok = 0
    n_na_fail = 0
    n_na_locked = 0
    for na_key in na_keys:
        try:
            with audit_lock.acquire_key(
                settings.extractor.out_dir,
                na_key,
                owner="argus.na",
            ):
                ex_cfg = extractor.ExtractorConfig.from_settings(
                    settings.extractor,
                    execution_key=na_key,
                    out_dir=folder_root,
                )
                rc, work_dir = extractor.run(ex_cfg)
                if rc != 0 or work_dir is None:
                    n_na_fail += 1
                    continue
                _argus_batch._write_not_applicable_audit(work_dir, na_key)
                n_na_ok += 1
        except audit_lock.AuditLockBusy as _e:
            print(f"[ARGUS] N/A audit skipped for {na_key}: {_e}",
                  file=sys.stderr)
            n_na_locked += 1
        except Exception as _e:
            print(f"[ARGUS] N/A audit failed for {na_key}: "
                  f"{type(_e).__name__}: {_e}", file=sys.stderr)
            n_na_fail += 1
    locked_note = f", {n_na_locked} already running" if n_na_locked else ""
    print(f"[ARGUS] N/A audits: {n_na_ok} written, "
          f"{n_na_fail} failed{locked_note}", file=sys.stderr)


def _synthetic_status(work_dir: Path) -> str | None:
    """Return a non-verdict status that may require a synthetic audit."""
    try:
        metadata = json.loads((work_dir / "metadata.json").read_text())
        tr = (metadata.get("test_results") or [{}])[0]
        status = (tr.get("status") or "").strip()
    except (OSError, json.JSONDecodeError, IndexError):
        return None
    if status in ("Not Applicable", "N/A", "Blocked", "In Progress"):
        return status
    return None


def _merge_blocked_base_finding(work_dir: Path) -> None:
    """Merge the R20/R21 base finding into an existing model audit."""
    from argus.domain import workflow_rules as _wr
    audit_path = work_dir / "audit.json"
    audit = json.loads(audit_path.read_text())
    metadata = json.loads((work_dir / "metadata.json").read_text())
    hidx = None
    try:
        from argus.domain import history_index as _hi
        hidx = _hi.HistoryIndex.get_shared(
            auditor._infer_history_root(work_dir))
    except Exception as e:
        print(f"[ARGUS] R20 history lookup skipped "
              f"({type(e).__name__}: {e})", file=sys.stderr)
    base = (_wr.build_r20_finding(metadata, hidx,
                                  current_e_key=work_dir.name)
            or _wr.build_r21_finding(metadata))
    if not base:
        return
    audit = auditor.merge_rule_findings(audit, [base])
    audit = auditor._finalize_verdict(audit)
    atomic_io.atomic_write_json(audit_path, audit)
    try:
        (work_dir / "audit.md").write_text(
            auditor.render_markdown(audit, metadata))
    except Exception as e:
        log.warning("audit.md render failed for %s: %s: %s",
                    work_dir, type(e).__name__, e)


def _process_blocked_items(blocked_keys: list[str], settings,
                           folder_root: Path) -> None:
    """Audit Blocked / In Progress items (evidence-aware)."""
    print(f"\n[ARGUS] processing {len(blocked_keys)} Blocked/In-Progress "
          f"item(s) (evidence-aware audits)", file=sys.stderr)
    from argus.pipeline import batch as _argus_batch
    n_ok = 0
    n_fail = 0
    n_locked = 0
    for blk_key in blocked_keys:
        try:
            with audit_lock.acquire_key(
                settings.extractor.out_dir,
                blk_key,
                owner="argus.blocked",
            ):
                ex_cfg = extractor.ExtractorConfig.from_settings(
                    settings.extractor,
                    execution_key=blk_key,
                    out_dir=folder_root,
                )
                rc, work_dir = extractor.run(ex_cfg)
                if rc != 0 or work_dir is None:
                    n_fail += 1
                    continue
                if not _argus_batch._has_any_evidence(work_dir):
                    _argus_batch._write_blocked_audit(work_dir, blk_key)
                    n_ok += 1
                    continue
                au_cfg = auditor.AuditConfig.from_settings(
                    settings.auditor, execution_dir=work_dir,
                    cost_rates=settings.costs.model_rates)
                rc = auditor.run(au_cfg)
                if rc != 0:
                    n_fail += 1
                    continue
                _merge_blocked_base_finding(work_dir)
                if _argus_batch._post_checks_enabled(settings):
                    env_result = _argus_batch.KeyResult(
                        key=blk_key,
                        status="ok",
                        audit_path=work_dir / "audit.json",
                    )
                    try:
                        _argus_batch._run_env_check_for_key(
                            env_result,
                            settings,
                            lock_held=True,
                        )
                    except Exception as post_exc:
                        # audit.json already exists with environment set to
                        # not_applicable pending this check; without marking
                        # the module incomplete the audit would be cached as
                        # complete and never retried.
                        diagnostic = (
                            post_exc.diagnostic
                            if isinstance(
                                post_exc, analysis_status.AnalysisIncompleteError)
                            else f"{type(post_exc).__name__}: {post_exc}"
                        )
                        _argus_batch._mark_result_incomplete(
                            env_result, "environment", diagnostic)
                        print(
                            f"[ARGUS] Blocked/In-Progress post-check incomplete "
                            f"for {blk_key}: {diagnostic}",
                            file=sys.stderr,
                        )
                        n_fail += 1
                        continue
                n_ok += 1
        except audit_lock.AuditLockBusy as _e:
            print(f"[ARGUS] Blocked/In-Progress audit skipped for {blk_key}: "
                  f"{_e}", file=sys.stderr)
            n_locked += 1
        except Exception as _e:
            print(f"[ARGUS] Blocked/In-Progress audit failed for {blk_key}: "
                  f"{type(_e).__name__}: {_e}", file=sys.stderr)
            n_fail += 1
    locked_note = f", {n_locked} already running" if n_locked else ""
    print(f"[ARGUS] Blocked/In-Progress audits: {n_ok} written, "
          f"{n_fail} failed{locked_note}", file=sys.stderr)


_REPORT_ARCHIVE_KEEP = 10
_LATEST_RUN_MANIFEST = "_latest_run.json"
_LATEST_RUN_TRANSITIONS = "_latest_run_transitions.json"
_LATEST_RUN_MANIFEST_LEASE_SECONDS = 60
_LATEST_RUN_MANIFEST_LOCK_WAIT_SECONDS = (
    _LATEST_RUN_MANIFEST_LEASE_SECONDS + 5.0
)
_LATEST_RUN_MANIFEST_LOCK_POLL_SECONDS = 0.05
_FAILED_KEY_STATUSES = frozenset({
    "failed",
    "auth_failed",
    "analysis_incomplete",
})


def _parse_manifest_timestamp(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _read_latest_run_manifest(folder_root: Path) -> dict[str, Any] | None:
    try:
        manifest = json.loads(
            (folder_root / _LATEST_RUN_MANIFEST).read_text()
        )
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return manifest if isinstance(manifest, dict) else None


def _read_transition_journal(folder_root: Path) -> dict[str, Any]:
    try:
        journal = json.loads(
            (folder_root / _LATEST_RUN_TRANSITIONS).read_text()
        )
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return journal if isinstance(journal, dict) else {}


def _write_transition_journal(
    folder_root: Path,
    recovered_at: dict[str, str],
    failed_at: dict[str, str],
) -> None:
    atomic_io.atomic_write_json(
        folder_root / _LATEST_RUN_TRANSITIONS,
        {
            "recovered_at": recovered_at,
            "failed_at": failed_at,
        },
    )


def _transitions_since(
    manifest: dict[str, Any] | None,
    started_at: dt.datetime,
) -> tuple[dict[str, str], dict[str, str]]:
    if manifest is None:
        return {}, {}
    threshold = started_at.astimezone(dt.timezone.utc)

    def _events(field: str) -> dict[str, str]:
        raw = manifest.get(field)
        if not isinstance(raw, dict):
            return {}
        return {
            key: value
            for key, value in raw.items()
            if (
                isinstance(key, str)
                and (event_at := _parse_manifest_timestamp(value)) is not None
                and event_at >= threshold
            )
        }

    recovered = _events("recovered_at")
    failed = _events("failed_at")
    for key in set(recovered) & set(failed):
        recovered_time = _parse_manifest_timestamp(recovered[key])
        failed_time = _parse_manifest_timestamp(failed[key])
        if (
            recovered_time is not None
            and failed_time is not None
            and recovered_time >= failed_time
        ):
            failed.pop(key, None)
        else:
            recovered.pop(key, None)
    return recovered, failed


def _acquire_latest_run_manifest_lock(
    folder_root: Path,
    *,
    owner: str,
) -> audit_lock.AuditLock:
    """Serialize canonical manifest mutations across tasks for one folder."""
    lock_key = f"report-manifest:{folder_root.name}"
    deadline = (
        time.monotonic() + _LATEST_RUN_MANIFEST_LOCK_WAIT_SECONDS
    )
    while True:
        try:
            return audit_lock.acquire_key(
                folder_root.parent,
                lock_key,
                owner=owner,
                stale_after_seconds=_LATEST_RUN_MANIFEST_LEASE_SECONDS,
                lease_duration_seconds=_LATEST_RUN_MANIFEST_LEASE_SECONDS,
            )
        except audit_lock.AuditLockBusy:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_LATEST_RUN_MANIFEST_LOCK_POLL_SECONDS)


def _write_latest_run_manifest(
    folder_root: Path,
    *,
    folder_name: str,
    scope_keys: list[str],
    selected_keys: list[str],
    results: list[batch.KeyResult],
    synthetic_keys: list[str],
    started_at: dt.datetime,
    execution_key: str | None = None,
) -> Path | None:
    """Persist the exact key boundary used by canonical folder reports."""
    with _acquire_latest_run_manifest_lock(
        folder_root,
        owner="argus.folder_manifest",
    ):
        path = folder_root / _LATEST_RUN_MANIFEST
        existing = _read_latest_run_manifest(folder_root)
        existing_started_at = (
            _parse_manifest_timestamp(existing.get("started_at"))
            if existing is not None
            else None
        )
        requested_started_at = started_at.astimezone(dt.timezone.utc)
        if (
            existing_started_at is not None
            and existing_started_at > requested_started_at
        ):
            return None
        recovered_at, failed_at = _transitions_since(
            _read_transition_journal(folder_root),
            started_at,
        )
        scope = set(scope_keys)
        recovered_at = {key:stamp for key, stamp in recovered_at.items() if key in scope}
        failed_at = {key:stamp for key, stamp in failed_at.items() if key in scope}
        failed_keys = {
            result.key
            for result in results
            if (
                result.status in _FAILED_KEY_STATUSES
                and result.key not in recovered_at
            )
        }
        failed_keys.update(failed_at)
        # A shared folder can contain hundreds of cached executions. Walk
        # it once instead of recursively searching it again for every key.
        allowed = set(scope_keys) - failed_keys
        audit_keys = {
            audit.parent.name
            for audit in exec_paths.iter_audit_jsons(folder_root)
            if audit.parent.name in allowed
        }

        statuses = {result.key: result.status for result in results}
        for key in recovered_at:
            statuses[key] = "ok"
        for key in failed_at:
            statuses[key] = "analysis_incomplete"
        for key in synthetic_keys:
            statuses.setdefault(key, "synthetic")

        payload = {
            "version": 1,
            "folder_name": folder_name,
            "started_at": started_at.isoformat(timespec="microseconds"),
            "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(
                timespec="seconds"
            ),
            "scope_keys": scope_keys,
            "selected_keys": selected_keys,
            "audit_keys": sorted(audit_keys),
            "failed_keys": sorted(failed_keys),
            "statuses": statuses,
            "recovered_at": recovered_at,
            "failed_at": failed_at,
        }
        if execution_key is not None:
            payload["execution_key"] = execution_key
        atomic_io.atomic_write_json(path, payload)
        return path


def _adopt_key_into_latest_run_manifest(
    folder_dir: Path,
    key: str,
) -> str | None:
    """Add a successful single-key retry to the folder's canonical run."""
    with _acquire_latest_run_manifest_lock(
        folder_dir,
        owner="argus.single_manifest",
    ):
        recovery_token = dt.datetime.now(
            dt.timezone.utc
        ).isoformat(timespec="microseconds")
        journal = _read_transition_journal(folder_dir)
        recovered_at = journal.get("recovered_at")
        if not isinstance(recovered_at, dict):
            recovered_at = {}
        failed_at = journal.get("failed_at")
        if not isinstance(failed_at, dict):
            failed_at = {}
        recovered_at[key] = recovery_token
        failed_at.pop(key, None)
        _write_transition_journal(
            folder_dir,
            recovered_at,
            failed_at,
        )

        manifest_path = folder_dir / _LATEST_RUN_MANIFEST
        manifest = _read_latest_run_manifest(folder_dir)
        if isinstance(manifest, dict) and isinstance(
            manifest.get("audit_keys"), list
        ):
            manifest_recovered = manifest.get("recovered_at")
            if not isinstance(manifest_recovered, dict):
                manifest_recovered = {}
                manifest["recovered_at"] = manifest_recovered
            manifest_recovered[key] = recovery_token
            manifest_failed_at = manifest.get("failed_at")
            if isinstance(manifest_failed_at, dict):
                manifest_failed_at.pop(key, None)
            if key not in manifest["audit_keys"]:
                manifest["audit_keys"] = sorted({
                    *manifest["audit_keys"],
                    key,
                })
            if isinstance(manifest.get("scope_keys"), list):
                manifest["scope_keys"] = sorted({*manifest["scope_keys"], key})
            failed_keys = manifest.get("failed_keys")
            if isinstance(failed_keys, list) and key in failed_keys:
                manifest["failed_keys"] = [
                    failed for failed in failed_keys if failed != key
                ]
            statuses = manifest.get("statuses")
            if isinstance(statuses, dict):
                statuses[key] = "ok"
            atomic_io.atomic_write_json(manifest_path, manifest)
        return recovery_token


def _record_single_key_failure(
    folder_dir: Path,
    key: str,
    *,
    recovery_token: str | None,
    require_recovery_match: bool,
) -> None:
    """Persist one failed retry without clobbering a newer recovery."""
    with _acquire_latest_run_manifest_lock(
        folder_dir,
        owner="argus.single_publication_failure",
    ):
        manifest_path = folder_dir / _LATEST_RUN_MANIFEST
        manifest = _read_latest_run_manifest(folder_dir)
        journal = _read_transition_journal(folder_dir)
        recovered_at = journal.get("recovered_at")
        if not isinstance(recovered_at, dict):
            recovered_at = {}
        failed_at = journal.get("failed_at")
        if not isinstance(failed_at, dict):
            failed_at = {}
        current_recovery = recovered_at.get(key)
        if current_recovery is None and manifest is not None:
            manifest_recovered = manifest.get("recovered_at")
            if isinstance(manifest_recovered, dict):
                current_recovery = manifest_recovered.get(key)
        if require_recovery_match:
            if (
                recovery_token is not None
                and current_recovery != recovery_token
            ):
                return
            if recovery_token is None and current_recovery is not None:
                return
        failure_token = dt.datetime.now(
            dt.timezone.utc
        ).isoformat(timespec="microseconds")
        recovered_at.pop(key, None)
        failed_at[key] = failure_token
        _write_transition_journal(
            folder_dir,
            recovered_at,
            failed_at,
        )

        if manifest is not None:
            if isinstance(manifest.get("scope_keys"), list):
                manifest["scope_keys"] = sorted({*manifest["scope_keys"], key})
            audit_keys = manifest.get("audit_keys")
            if isinstance(audit_keys, list):
                manifest["audit_keys"] = [
                    audit_key for audit_key in audit_keys
                    if audit_key != key
                ]
            failed_keys = manifest.get("failed_keys")
            if not isinstance(failed_keys, list):
                failed_keys = []
            manifest["failed_keys"] = sorted({
                *failed_keys,
                key,
            })
            statuses = manifest.get("statuses")
            if not isinstance(statuses, dict):
                statuses = {}
                manifest["statuses"] = statuses
            statuses[key] = "analysis_incomplete"
            recovered_at = manifest.get("recovered_at")
            if isinstance(recovered_at, dict):
                recovered_at.pop(key, None)
            failed_at = manifest.get("failed_at")
            if not isinstance(failed_at, dict):
                failed_at = {}
                manifest["failed_at"] = failed_at
            failed_at[key] = failure_token
            atomic_io.atomic_write_json(manifest_path, manifest)

        failed_path = folder_dir / "_failed_keys.txt"
        try:
            failed_keys = [
                failed_key.strip()
                for failed_key in failed_path.read_text().splitlines()
                if failed_key.strip()
            ]
        except FileNotFoundError:
            failed_keys = []
        except OSError:
            return
        atomic_io.atomic_write_text(
            failed_path,
            "".join(
                f"{failed_key}\n"
                for failed_key in dict.fromkeys([*failed_keys, key])
            ),
        )


def _restore_single_execution_scope(folder_root: Path) -> bool:
    """Recover cached folder audits hidden by a legacy single-key manifest.

    Ordinary folder/run/plan manifests remain scoped. Only the manifest
    written by the former single-key refresh path is expanded, and its
    original contents are retained for inspection.
    """
    with _acquire_latest_run_manifest_lock(
        folder_root, owner="argus.restore_single_scope",
    ):
        manifest = _read_latest_run_manifest(folder_root)
        if manifest is None or not isinstance(manifest.get("execution_key"), str):
            return False
        cached = {
            path.parent.name for path in exec_paths.iter_audit_jsons(folder_root)
        }
        failed = {
            key for key in manifest.get("failed_keys", [])
            if isinstance(key, str) and key
        }
        try:
            failed.update(
                key.strip()
                for key in (folder_root / "_failed_keys.txt").read_text().splitlines()
                if key.strip()
            )
        except FileNotFoundError:
            pass
        scope = cached | failed | {
            key for key in manifest.get("scope_keys", [])
            if isinstance(key, str) and key
        }
        backup = folder_root / "_single_execution_scope_backup.json"
        if not backup.exists():
            atomic_io.atomic_write_json(backup, manifest)
        manifest.pop("execution_key")
        manifest["scope_keys"] = sorted(scope)
        manifest["audit_keys"] = sorted(cached - failed)
        manifest["failed_keys"] = sorted(failed)
        manifest["scope_restored_at"] = dt.datetime.now(
            dt.timezone.utc,
        ).isoformat(timespec="seconds")
        atomic_io.atomic_write_json(
            folder_root / _LATEST_RUN_MANIFEST, manifest,
        )
        print(
            f"[ARGUS] restored folder scope: {folder_root.name} "
            f"({len(cached)} cached executions)",
            file=sys.stderr,
        )
        return True


def _cap_report_archives(folder_root: Path,
                         keep: int = _REPORT_ARCHIVE_KEEP) -> None:
    """Delete all but the newest `keep` timestamped report archives."""
    for pattern in ("_argus_*.html", "_report_*.md"):
        # Lexical reverse sort — archive names embed zero-padded ISO
        # timestamps; keep that format or newest-first breaks.
        archives = sorted(folder_root.glob(pattern), reverse=True)
        for stale in archives[keep:]:
            try:
                stale.unlink()
            except OSError as e:
                print(f"[ARGUS] archive prune skipped {stale.name}: {e}",
                      file=sys.stderr)


def _set_publication_outcome(
    folder_root: Path,
    status: str,
    diagnostic: str | None = None,
) -> None:
    """Persist publication state before rendering or after a failure."""
    from argus.core import exec_paths as _exec_paths
    from argus.pipeline import analysis_status as _analysis_status
    from argus.pipeline import env_check_merge as _env_check_merge
    from argus.reporting.report import is_complete_latest_run_manifest

    manifest = _read_latest_run_manifest(folder_root)
    allowed = (
        {key for key in manifest["audit_keys"] if isinstance(key, str)}
        if manifest is not None
        and is_complete_latest_run_manifest(manifest)
        else None
    )
    for audit_path in _exec_paths.iter_latest_audit_jsons(folder_root):
        if allowed is not None and audit_path.parent.name not in allowed:
            continue
        try:
            audit = json.loads(audit_path.read_text())
            _analysis_status.set_module(
                audit,
                "publication",
                status,
                diagnostic,
            )
            _env_check_merge.write_merged_audit(audit_path, audit)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"cannot update publication state for {audit_path}: {exc}"
            ) from exc


def _publish_folder_reports(
    folder_root: Path,
    settings,
    *,
    completion_state: str | None = None,
) -> bool:
    """Generate the aggregate folder reports + ensure ARGUS server is up."""
    import datetime as _dt

    stamp = _dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    try:
        from argus.reporting import report as argus_report
        _restore_single_execution_scope(folder_root)
        _set_publication_outcome(folder_root, "complete")
        argus_report.configure(settings.extractor.base_url)
        summaries = argus_report.scan_output_dir(folder_root)
        markdown_text = argus_report.generate_report(
            folder_root, summaries=summaries)
        from argus.reporting.ui import from_settings as ui_context_from_settings
        html_bundle = argus_report.generate_html_report_bundle(
            folder_root,
            summaries=summaries,
            ui_context=ui_context_from_settings(settings),
        )
        from argus.reporting import publisher as report_publisher
        published = report_publisher.publish(
            folder_root,
            stamp=stamp,
            html=html_bundle.canonical_html,
            markdown=markdown_text,
            completion_state=completion_state,
            archive_html=html_bundle.archive_html,
            detail_chunks=html_bundle.detail_chunks,
        )
        _cap_report_archives(folder_root)
        print(f"[ARGUS] open: {published.public_url}",
              file=sys.stderr)
        print(f"[ARGUS] archive: {published.archived_html.name}",
              file=sys.stderr)
        print(f"[ARGUS] markdown report: "
              f"{published.archived_markdown.name}", file=sys.stderr)
        try:
            from argus.operations import audit_index as _audit_index
            counts = _audit_index.sync(folder_root.parent
                                       if folder_root.parent.name == "output"
                                       else folder_root)
            print(f"[ARGUS] index: +{counts['added']} "
                  f"~{counts['updated']} -{counts['removed']}",
                  file=sys.stderr)
        except Exception as _ie:
            print(f"[ARGUS] index sync skipped "
                  f"({type(_ie).__name__}: {_ie})", file=sys.stderr)
        _ensure_argus_server(settings)
        print(
            json.dumps({
                "event": "argus_report_publication",
                "folder": folder_root.name,
                "status": "complete",
                "audits": len(summaries),
                "url": published.public_url,
            }, sort_keys=True),
            file=sys.stderr,
        )
        return True
    except Exception as e:
        diagnostic = f"{type(e).__name__}: {e}"
        try:
            _set_publication_outcome(
                folder_root,
                "incomplete",
                diagnostic,
            )
        except Exception as state_error:
            diagnostic += (
                "; publication-state write failed: "
                f"{type(state_error).__name__}: {state_error}"
            )
        try:
            from argus.reporting import publisher as report_publisher
            from argus.reporting import report as argus_report

            summaries = argus_report.scan_output_dir(folder_root)
            from argus.reporting.ui import (
                from_settings as ui_context_from_settings,
            )
            html_bundle = argus_report.generate_html_report_bundle(
                folder_root,
                summaries=summaries,
                ui_context=ui_context_from_settings(settings),
            )
            report_publisher.LocalReportPublisher().publish(
                folder_root,
                stamp=stamp,
                html=html_bundle.canonical_html,
                markdown=argus_report.generate_report(
                    folder_root,
                    summaries=summaries,
                ),
                completion_state="incomplete",
                archive_html=html_bundle.archive_html,
                detail_chunks=html_bundle.detail_chunks,
            )
        except Exception as fallback_error:
            diagnostic += (
                "; local diagnostic report failed: "
                f"{type(fallback_error).__name__}: {fallback_error}"
            )
        print(
            json.dumps({
                "event": "argus_report_publication",
                "folder": folder_root.name,
                "status": "incomplete",
                "diagnostic": diagnostic,
            }, sort_keys=True),
            file=sys.stderr,
        )
        return False


def _ensure_argus_server(settings) -> None:
    """Auto-start the ARGUS HTTP server if it isn't already up."""
    try:
        import socket as _socket
        with _socket.socket(_socket.AF_INET,
                            _socket.SOCK_STREAM) as _s:
            _s.settimeout(0.3)
            listener_up = (
                _s.connect_ex((settings.server.host, settings.server.port)) == 0
            )
    except Exception as e:
        log.debug("serve port probe failed: %s: %s", type(e).__name__, e)
        listener_up = False
    if listener_up:
        print(f"[ARGUS] server: already running on :{settings.server.port}",
              file=sys.stderr)
        return
    try:
        import subprocess as _sp
        project_root = paths.project_root()
        log_path = project_root / "_argus_serve.log"
        with open(log_path, "ab") as log_file:
            _sp.Popen(
                [
                    sys.executable,
                    "-u",
                    "-m",
                    "argus",
                    "serve",
                    "--port",
                    str(settings.server.port),
                ],
                stdout=log_file,
                stderr=_sp.STDOUT,
                stdin=_sp.DEVNULL,
                cwd=str(project_root),
                start_new_session=True,
            )
        print(f"[ARGUS] server: started in background "
              f"(log: {log_path.name})", file=sys.stderr)
        if settings.notify.tunnel_url:
            print(f"[ARGUS] view: {settings.notify.tunnel_url}",
                  file=sys.stderr)
        else:
            print("[ARGUS] view from your laptop:  "
                  f"ssh -L {settings.server.port}:localhost:"
                  f"{settings.server.port} dev  ->  "
                  f"http://localhost:{settings.server.port}/  "
                  "(tip: set [notify].tunnel_url in config.toml "
                  f"after `tunnel create {settings.server.port} "
                  "--name argus`)",
                  file=sys.stderr)
    except Exception as _e:
        print(f"[ARGUS] auto-start skipped: "
              f"{type(_e).__name__}: {_e}",
              file=sys.stderr)


def _print_by_tester(session, base_url: str,
                     keys: list[str], user_keys: list[str | None]) -> None:
    """Group emitted E-keys by executor display name, print to stderr."""
    cache: dict[str, str | None] = {}
    unique_user_keys = {u for u in user_keys if u}

    for u_key in unique_user_keys:
        extractor.fetch_user_display_name(session, base_url, u_key, cache)

    missing = [u for u in unique_user_keys if cache.get(u) is None]
    if missing:
        for u_key in missing:
            cache.pop(u_key, None)
            extractor.fetch_user_display_name(session, base_url, u_key, cache)

    groups: dict[str, list[str]] = {}
    for e_key, maybe_user_key in zip(keys, user_keys, strict=False):
        if maybe_user_key:
            name = cache.get(maybe_user_key) or maybe_user_key
        else:
            name = "unknown"
        groups.setdefault(name, []).append(e_key)

    print("by tester:", file=sys.stderr)
    for name in sorted(groups, key=lambda n: (-len(groups[n]), n.lower())):
        ks = groups[name]
        print(f"  {name} ({len(ks)}): {', '.join(ks)}", file=sys.stderr)


def _count_pages(work_dir: Path | None) -> int:
    if work_dir is None:
        return 0
    shots = work_dir / "screenshots"
    if not shots.exists():
        return 0
    return sum(1 for _ in shots.rglob("page_*.jpg")) + \
        sum(1 for _ in shots.rglob("page_*.png"))


def _audited_keys_with_state(out_dir: Path) -> dict[str, _AuditState]:
    """Map of E-key -> cached audit state for everything already audited."""
    if not out_dir.exists():
        return {}
    out: dict[str, _AuditState] = {}
    for audit in exec_paths.iter_latest_audit_jsons(out_dir):
        e_key = audit.parent.name
        meta_path = audit.parent / "metadata.json"
        cached_date = ""
        cached_fp = ""
        cached_status = "incomplete"
        cached_policy = ""
        try:
            from argus.pipeline import analysis_status as _analysis_status
            cached_audit = json.loads(audit.read_text())
            cached_status = _analysis_status.derive_status(cached_audit)
            cached_policy = (cached_audit.get("provenance") or {}).get("team_rules_sha256", "")
        except (OSError, json.JSONDecodeError):
            pass
        try:
            metadata = json.loads(meta_path.read_text())
            results = metadata.get("test_results") or []
            if results and isinstance(results[0], dict):
                date_val = results[0].get("execution_date")
                if isinstance(date_val, str):
                    cached_date = date_val
                fp_val = results[0].get("evidence_fingerprint")
                if isinstance(fp_val, str):
                    cached_fp = fp_val
                if not cached_fp:
                    cached_fp = extractor.build_evidence_fingerprint(
                        [results[0]])
        except (OSError, json.JSONDecodeError):
            pass
        out[e_key] = _AuditState(
            execution_date=cached_date,
            evidence_fingerprint=cached_fp,
            analysis_status=cached_status,
            policy_fingerprint=cached_policy,
        )
    return out


def _reconcile_failed_keys_unlocked(out_dir: Path) -> Path | None:
    """Reconcile the retry marker while the report-manifest lock is held."""
    failed_path = out_dir / "_failed_keys.txt"
    try:
        keys = [
            key.strip()
            for key in failed_path.read_text().splitlines()
            if key.strip() and not key.strip().startswith("#")
        ]
    except FileNotFoundError:
        return None
    except OSError:
        return failed_path

    from argus.pipeline import analysis_status as _analysis_status

    manifest = _read_latest_run_manifest(out_dir)
    raw_statuses = manifest.get("statuses") if manifest is not None else None
    statuses: dict[str, Any] = (
        raw_statuses if isinstance(raw_statuses, dict) else {}
    )
    manifest_failed = {
        key
        for key in (
            manifest.get("failed_keys", [])
            if isinstance(manifest, dict)
            and isinstance(manifest.get("failed_keys"), list)
            else []
        )
        if isinstance(key, str)
    }
    raw_scope = manifest.get("scope_keys") if manifest is not None else None
    scope = (
        {key for key in raw_scope if isinstance(key, str)}
        if isinstance(raw_scope, list) else None
    )
    audit_paths = {
        path.parent.name: path for path in exec_paths.iter_audit_jsons(out_dir)
        if scope is None or path.parent.name in scope
    }

    remaining: list[str] = []
    for key in dict.fromkeys(keys):
        if scope is not None and key not in scope:
            remaining.append(key)
            continue
        status = statuses.get(key)
        if key in manifest_failed or status in _FAILED_KEY_STATUSES:
            remaining.append(key)
            continue
        if status in {"ok", "synthetic"}:
            continue
        audit_path = audit_paths.get(key)
        if audit_path is None:
            remaining.append(key)
            continue
        try:
            audit = json.loads(audit_path.read_text())
        except (OSError, json.JSONDecodeError):
            remaining.append(key)
            continue
        if _analysis_status.derive_status(audit) == "incomplete":
            remaining.append(key)

    try:
        if remaining:
            failed_path.write_text("\n".join(remaining) + "\n")
            return failed_path
        failed_path.unlink(missing_ok=True)
    except OSError:
        return failed_path
    return None


def _reconcile_failed_keys(out_dir: Path) -> Path | None:
    """Keep retry markers aligned with persisted completion state."""
    with _acquire_latest_run_manifest_lock(
        out_dir,
        owner="argus.failed_keys",
    ):
        return _reconcile_failed_keys_unlocked(out_dir)


def _write_and_reconcile_failed_keys(
    results: list[batch.KeyResult],
    out_dir: Path,
    *,
    run_started_at: dt.datetime,
) -> Path | None:
    """Write batch failures and discard keys completed by another task."""
    with _acquire_latest_run_manifest_lock(
        out_dir,
        owner="argus.batch_failed_keys",
    ):
        manifest = _read_latest_run_manifest(out_dir)
        if (
            manifest is not None
            and manifest.get("started_at")
            == run_started_at.isoformat(timespec="microseconds")
        ):
            recovered_at, failed_at = _transitions_since(
                _read_transition_journal(out_dir),
                run_started_at,
            )
            recovered_keys = set(recovered_at)
            newer_failed_keys = set(failed_at)
            statuses = manifest.get("statuses")
            if not isinstance(statuses, dict):
                statuses = {}
                manifest["statuses"] = statuses
            final_failed: set[str] = set(newer_failed_keys)
            for result in results:
                if result.key in newer_failed_keys:
                    statuses[result.key] = "analysis_incomplete"
                    final_failed.add(result.key)
                    continue
                if (
                    result.key in recovered_keys
                    and result.status in _FAILED_KEY_STATUSES
                ):
                    statuses[result.key] = "ok"
                    continue
                statuses[result.key] = result.status
                if result.status in _FAILED_KEY_STATUSES:
                    final_failed.add(result.key)
            manifest["failed_keys"] = sorted(final_failed)
            audit_keys = manifest.get("audit_keys")
            if isinstance(audit_keys, list):
                manifest["audit_keys"] = sorted({
                    key
                    for key in audit_keys
                    if key not in final_failed
                })
            atomic_io.atomic_write_json(
                out_dir / _LATEST_RUN_MANIFEST,
                manifest,
            )
        manifest = _read_latest_run_manifest(out_dir)
        manifest_failed = (
            {
                key
                for key in manifest.get("failed_keys", [])
                if isinstance(key, str)
            }
            if manifest is not None
            and isinstance(manifest.get("failed_keys"), list)
            else {
                result.key
                for result in results
                if result.status in _FAILED_KEY_STATUSES
            }
        )
        # A selected-run request must retain retry markers for sibling runs.
        raw_scope = manifest.get("scope_keys") if manifest is not None else None
        if isinstance(raw_scope, list):
            scope = {key for key in raw_scope if isinstance(key, str)}
            try:
                previous = (out_dir / "_failed_keys.txt").read_text().splitlines()
            except FileNotFoundError:
                previous = []
            manifest_failed.update(
                key.strip() for key in previous
                if key.strip()
                and not key.strip().startswith("#")
                and key.strip() not in scope
            )
        batch.write_failed_keys(
            [
                batch.KeyResult(key=key, status="analysis_incomplete")
                for key in sorted(manifest_failed)
            ],
            out_dir,
        )
        return _reconcile_failed_keys_unlocked(out_dir)


def _audited_keys_with_timestamps(out_dir: Path) -> dict[str, str]:
    """Backward-compatible timestamp-only view for older pure tests/tools."""
    return {
        key: state.execution_date
        for key, state in _audited_keys_with_state(out_dir).items()
    }


def _find_existing_workdir(out_dir: Path, key: str) -> Path | None:
    """Locate any existing <out>/.../<key>/ dir across all historical layouts."""
    return exec_paths.find_execution_dir(out_dir, key)


def _print_summary(work_dir: Path, label: str) -> None:
    """Single ASCII-safe summary line after the run completes."""
    audit_path = work_dir / "audit.json"
    md_path = work_dir / "audit.md"
    try:
        audit = json.loads(audit_path.read_text())
    except (OSError, json.JSONDecodeError):
        progress.done(label, f"-> {md_path}")
        return
    verdict = audit.get("overall_verdict", "?")
    n = len(audit.get("findings") or [])
    progress.done(
        label,
        f"[{verdict}, {n} finding{'s' if n != 1 else ''}]  {md_path}",
    )


if __name__ == "__main__":
    sys.exit(main())
