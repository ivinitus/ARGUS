"""ARGUS — end-to-end runner.

Two modes:

Single-execution (extract + audit one):
    python argus.py --key QA-E171453
    python argus.py --testrun-id 23507 --item-id 1403823

Folder mode (enumerate executed, not-yet-audited keys in a test-management folder
and run the full extract+audit pipeline on each in parallel):
    python argus.py --folder "Sprint 47 Regression"

Folder mode runs the batch runner on the resolved key list. Already-audited
keys (where `audit.json` exists anywhere under the configured out_dir) are
skipped. Concurrency is controlled by `[batch]` in config.toml.

Other flags:
    --skip-extract    reuse existing output dir, audit only
    --skip-audit      extract only, don't audit
    --config PATH     alternate config.toml
    -v                verbose logging
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import requests

import auditor
import batch
import config as argus_config
import extractor
import notify
import progress


# We treat these i18n status names as "audit-worthy" — they carry an actual
# execution verdict with screenshots to audit. Blocked/NotExecuted/InProgress
# have no evidence (tracked as R15 coverage gaps instead), and Retest is an
# intermediate "failed once, awaiting re-run" state we track but don't audit.
# "Passed With Issue" IS audited — it's a real executed verdict, and omitting
# it silently dropped 34% of executed work in some folders. Both the space-
# and underscore-delimited i18n shortnames are listed because test-management returns
# them inconsistently across installs; the `if n in status_map` guard below
# makes any unmatched name harmless. Actual numeric IDs are project-scoped,
# so we fetch and map them at runtime.
EXECUTED_STATUS_NAMES = {
    "PASS",
    "FAIL",
    "PASSED WITH ISSUE",
    "PASSED_WITH_ISSUE",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ARGUS end-to-end runner")
    parser.add_argument("--key", dest="execution_key",
                        help="execution key, e.g. QA-E171453")
    parser.add_argument("--testrun-id", type=int)
    parser.add_argument("--item-id", type=int)
    parser.add_argument("--folder",
                        help="folder name — enumerate executed, not-yet-audited "
                             "keys and run the full batch (extract + audit) in "
                             "parallel per [batch] in config.toml")
    parser.add_argument("--auto-folder", action="store_true",
                        help="auto-pick the latest 'Targeted regression' folder "
                             "(within 7 days). Cron-friendly: silently exits 0 "
                             "when nothing matches. Mutually exclusive with "
                             "--folder / --key / --testrun-id.")
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
                                  args.auto_folder))
    if modes == 0:
        parser.error("provide --key, --testrun-id + --item-id, --folder, "
                     "or --auto-folder")
    if modes > 1:
        parser.error("pick exactly one of --key / --testrun-id+--item-id / "
                     "--folder / --auto-folder")

    settings = argus_config.load(args.config)
    extractor.configure_logging(args.verbose or settings.logging.verbose)

    # CLI --debug wins over config.toml's audit.debug_output. Mutate
    # settings so both code paths (_run_single and the folder/batch path)
    # see the flag without extra plumbing.
    if args.debug:
        settings.auditor.debug_output = True

    if args.auto_folder:
        return _run_auto_folder(settings)
    if args.folder:
        return _run_folder_list(args.folder, settings)
    return _run_single(args, settings)


def _run_auto_folder(settings) -> int:
    """Auto-resolve the latest active 'Targeted regression' folder, then
    delegate to _run_folder_list.

    Cron-friendly: silently returns 0 when the picker finds nothing
    within the configured age window. The whole point is that cron can
    run unattended across sprints — a quiet weekend with no fresh
    targeted-regression folder is the expected steady state, not an
    error.

    The single auth/network failure mode (Tracker down, PAT expired) is
    surfaced as rc=2 so the cron wrapper logs it and a single Chat
    warn fires. Same exit-code convention as the rest of the pipeline.
    """
    session = extractor.build_session()
    try:
        with progress.step("auto-picking latest targeted-regression folder") as s:
            folder = extractor.find_latest_active_folder(
                session,
                settings.extractor.base_url,
                settings.extractor.project_id,
                name_prefix="Targeted regression",
                max_age_days=7,
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
        print("[argus] auto-folder match has no 'name' field — bailing",
              file=sys.stderr)
        return 1
    return _run_folder_list(name, settings)


def _scoped_folder_out_dir(base_out_dir: Path, folder_name: str) -> Path:
    """Return the per-folder-scoped output dir for a `--folder` run.

    Takes the configured root (e.g. ./output) and a test-management folder name
    (e.g. 'Sprint 47 Regression') and returns the subdir where fetches
    for that folder should live (./output/Sprint_47_Regression). Pure
    so it's unit-testable without touching filesystem or network.

    Rationale: running `argus.py --folder A` then `argus.py --folder B`
    without scoping would mix executions from A and B under the same
    output/ root, which breaks batch retry files and makes aggregate
    reports nonsensical. Each folder gets its own sandbox.
    """
    return base_out_dir / extractor.slugify(folder_name)


def _run_single(args, settings) -> int:
    out_dir = settings.extractor.out_dir
    label = args.execution_key or f"{args.testrun_id}_{args.item_id}"

    # Resolve the test-management parent folder for this E-key so we can scope
    # the output dir under output/<folder_slug>/... — same shape that
    # folder mode produces. Without this, single-key runs land at
    # output/<testrun_slug>/... and create orphan dirs that don't
    # merge with the folder's _argus.html dashboard.
    #
    # Cheap: one folder-tree fetch per --key invocation, cached
    # implicitly because it's a one-shot run. On enumerate failure
    # (auth, network, --testrun-id mode where we don't have a key)
    # we fall back to the un-scoped out_dir — operator still gets
    # an audit, just at the legacy flat location.
    if args.execution_key and not args.skip_extract:
        try:
            session = extractor.build_session()
            tr_url = (f"{settings.extractor.base_url}"
                      f"/rest/tests/1.0/testresult/{args.execution_key}")
            tr_resp = session.get(
                tr_url,
                params={"fields": "testRun(folderId)"},
                timeout=15,
            )
            if tr_resp.status_code == 200:
                folder_id = (tr_resp.json().get("testRun") or {}).get("folderId")
                if folder_id:
                    folder_name = extractor.find_folder_name_by_id(
                        session, settings.extractor.base_url,
                        settings.extractor.project_id, folder_id,
                    )
                    if folder_name:
                        scoped = _scoped_folder_out_dir(out_dir, folder_name)
                        scoped.mkdir(parents=True, exist_ok=True)
                        out_dir = scoped
                        # Also mutate settings so report-refresh + any
                        # downstream callers see the scoped dir.
                        settings.extractor.out_dir = scoped
                        print(f"[argus] {args.execution_key} → "
                              f"scoped to {scoped.name}/",
                              file=sys.stderr)
        except (extractor.AuthError, Exception) as e:
            # Best-effort scope. Falling back to flat layout is safer
            # than failing the whole run on a folder-tree hiccup.
            print(f"[argus] folder scope skipped "
                  f"({type(e).__name__}: {e}); using {out_dir}",
                  file=sys.stderr)

    # work_dir is computed by extractor.run() from the fetched testrun name.
    work_dir: Path | None = None

    if not args.skip_extract:
        ex_cfg = extractor.ExtractorConfig(
            execution_key=args.execution_key,
            testrun_id=args.testrun_id,
            item_id=args.item_id,
            out_dir=out_dir,
            base_url=settings.extractor.base_url,
        )
        try:
            with progress.step(f"extracting {label}") as s:
                rc, work_dir = extractor.run(ex_cfg)
                if rc != 0:
                    s.finish("x", f"extracting {label} — failed (rc={rc})")
                    return rc
                pages = _count_pages(work_dir)
                s.finish("ok", f"extracted {label} ({pages} pages)")
        except extractor.AuthError as e:
            print(f"  reason: {e}", file=sys.stderr)
            return 2
        except Exception as e:
            print(f"  reason: {e}", file=sys.stderr)
            return 1
    else:
        # --skip-extract: need to locate an existing work_dir.
        work_dir = _find_existing_workdir(out_dir, args.execution_key
                                          or f"{args.testrun_id}_{args.item_id}")
        if work_dir is None:
            print(f"  reason: no existing extraction found for {label} under {out_dir}",
                  file=sys.stderr)
            return 1

    if args.skip_audit:
        return 0

    assert work_dir is not None
    au_cfg = auditor.AuditConfig(
        execution_dir=work_dir,
        model_provider=settings.auditor.model_provider,
        model_id=settings.auditor.model_id,
        region=settings.auditor.region,
        cloud_profile=settings.auditor.cloud_profile,
        api_base_url=settings.auditor.api_base_url,
        api_key_env=settings.auditor.api_key_env,
        max_pages=settings.auditor.max_pages,
        temperature=settings.auditor.temperature,
        consensus_enabled=settings.auditor.consensus_enabled,
        debug_output=settings.auditor.debug_output,
        chunk_max_parallel=settings.auditor.chunk_max_parallel,
        env_check_inline=settings.auditor.env_check_inline,
        env_check_sample_stride=settings.auditor.env_check_sample_stride,
        env_check_region_crop_height=settings.auditor.env_check_region_crop_height,
        env_check_region_crop_bottom_height=settings.auditor.env_check_region_crop_bottom_height,
        env_check_engine=settings.auditor.env_check_engine,
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

    # Refresh the parent folder's _argus.html so the dashboard reflects
    # the new audit immediately. Single-key mode (argus.py --key,
    # /argus reaudit, /argus run QA-EXXXXX) historically wrote
    # audit.json + audit.md but left the aggregate folder report
    # stale — operators saw the verdict in Chat but the dashboard
    # still showed yesterday's state until the next folder run.
    #
    # Walk up from work_dir to find the per-folder dir (the immediate
    # child of `output/`). Layout: output/<folder>/<testrun>/<tester>/<E-key>/
    # so the folder is `out_dir.parent / out_dir.parent.name` two-or-three
    # levels above audit.md. Bounded by the configured out_dir to
    # avoid walking into surprising locations on a misconfigured run.
    try:
        _refresh_folder_report(work_dir, settings.extractor.out_dir)
    except Exception as e:
        # Non-fatal: audit.json is the canonical artefact. A failed
        # report regen is logged but doesn't block the success path.
        print(f"[argus] report refresh skipped "
              f"({type(e).__name__}: {e})", file=sys.stderr)

    _print_summary(work_dir, label)
    return 0


def _refresh_folder_report(work_dir: Path, out_dir: Path) -> None:
    """Re-render the per-folder _argus.html + _report.md that contains `work_dir`.

    `work_dir` is the per-execution dir (where audit.json lives).

    The "folder" we want to refresh is the per-test-management-folder dashboard
    level — typically `output/<folder_slug>/_argus.html`. Walking up
    from work_dir, that's the directory whose own `_argus.html` exists
    (or, if no _argus.html exists yet, the top-most ancestor that
    sits directly under the BASE `output/` root — never under a
    scoped subdir).

    Detail that matters: `_run_single` mutates `settings.extractor.out_dir`
    to the scoped folder dir (e.g. `output/Targeted_regression_..._WEB`)
    so the audit lands in the right place. Naively comparing the
    parent against `out_dir` then resolves to the immediate child of
    THAT scoped dir — `E2E_Flow_..._Samsung/` (the testrun slug) —
    which has no `_argus.html`. We instead use a robust criterion:
    walk up and pick the first ancestor that has `_argus.html` OR is
    directly under the project's `output/` root.
    """
    out_dir_resolved = out_dir.resolve()
    project_output_root = (
        Path(__file__).resolve().parent / "output").resolve()
    cur = work_dir.resolve()
    folder_dir: Path | None = None
    while cur.parent != cur:  # walk until filesystem root
        # Preferred: the ancestor that ALREADY hosts a folder report.
        # That's unambiguous — the dashboard already exists there, so
        # we know it's the right level to refresh.
        if (cur / "_argus.html").exists():
            folder_dir = cur
            break
        # Fallback: the ancestor sitting directly under either the
        # scoped out_dir OR the unscoped project output/ root. The
        # second check is what fixes the scoped-mode bug — the
        # scoped out_dir's immediate child is one level too deep.
        if cur.parent == project_output_root:
            folder_dir = cur
            break
        if cur.parent == out_dir_resolved and out_dir_resolved == project_output_root:
            folder_dir = cur
            break
        cur = cur.parent
    if folder_dir is None or not folder_dir.exists():
        return
    # Lazy import: report.py pulls in coverage.py, history_index, etc.
    # Single-key path doesn't otherwise touch them — keep startup fast.
    import report as _argus_report
    folder_dir.joinpath("_argus.html").write_text(
        _argus_report.generate_html_report(folder_dir))
    folder_dir.joinpath("_report.md").write_text(
        _argus_report.generate_report(folder_dir))
    print(f"[argus] refreshed report: {folder_dir.name}/_argus.html",
          file=sys.stderr)


def _run_folder_list(folder_name: str, settings) -> int:
    """Resolve folder → runs → items, emit executed not-yet-audited E-keys."""
    # /argus pause kill-switch. Honoured here too (not just in
    # cron_wrapper.sh) so a manual `python argus.py --folder ...` while
    # paused exits without burning Tracker API budget. Chat-controlled
    # via notification command bot; CLI override via `python argus_control.py resume`.
    import argus_control as _argus_control
    if _argus_control.is_paused():
        st = _argus_control.status()
        print(f"[argus] argus paused (by {st.get('paused_by')!r} at "
              f"{st.get('paused_at')!r}, reason="
              f"{st.get('reason')!r}). Skipping. "
              f"Resume with: /argus resume  OR  "
              f"python argus_control.py resume",
              file=sys.stderr)
        return 0
    session = extractor.build_session()
    base_url = settings.extractor.base_url
    project_id = settings.extractor.project_id
    out_dir = settings.extractor.out_dir

    try:
        with progress.step(f"resolving folder '{folder_name}'") as s:
            folder = extractor.find_folder_by_name(
                session, base_url, folder_name, project_id)
            if not folder:
                s.finish("x", f"folder '{folder_name}' not found")
                return 1
            s.finish("ok", f"resolved '{folder_name}' -> id={folder.get('id')}")
    except extractor.AuthError as e:
        print(f"  reason: {e}", file=sys.stderr)
        return 2

    # Scope the output tree to a per-folder subdir so fetching folder B
    # after folder A doesn't interleave their executions under the
    # same output/ root. Every subsequent file write (extractor
    # workdirs, batch.write_failed_keys, etc.) reads from
    # settings.extractor.out_dir, so this one-line override propagates.
    # Reports and replay can scope to the same subdir via --out-dir.
    scoped_out_dir = _scoped_folder_out_dir(out_dir, folder_name)
    settings.extractor.out_dir = scoped_out_dir
    out_dir = scoped_out_dir
    scoped_out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[argus] folder '{folder_name}' -> output scoped to {scoped_out_dir}",
          file=sys.stderr)

    folder_id = folder["id"]

    try:
        with progress.step(f"listing test runs in folder {folder_id}") as s:
            runs = extractor.get_test_runs_in_folder(
                session, base_url, folder_id, project_id)
            s.finish("ok", f"{len(runs)} test run(s) in folder")
    except extractor.AuthError as e:
        print(f"  reason: {e}", file=sys.stderr)
        return 2

    if not runs:
        print("  reason: no test runs in folder", file=sys.stderr)
        return 0

    try:
        with progress.step(f"fetching status ids for project {project_id}") as s:
            status_map = extractor.get_project_status_ids(
                session, base_url, project_id)
            executed_ids = {status_map[n] for n in EXECUTED_STATUS_NAMES
                            if n in status_map}
            if not executed_ids:
                s.finish("x", f"no PASS/FAIL status ids found for project {project_id}")
                return 1
            # N/A status IDs — test-management uses both underscore ('NOT_APPLICABLE')
            # and space ('NOT APPLICABLE') forms across installs. Catch
            # either to drive R18 (synthetic N/A audits in the main table).
            na_ids = {sid for short, sid in status_map.items()
                      if short.upper().replace("_", " ") == "NOT APPLICABLE"}
            # Inverse map (numeric_id → display name). Used by R15
            # coverage computation below — the rule logic compares
            # against display names ("Pass" / "Blocked" / ...) rather
            # than numeric ids so it isn't QA-specific. Derived
            # from the already-fetched `status_map` (i18n_short -> id)
            # by mapping the i18n shortnames to their canonical display
            # forms — no new Tracker call. Unknown shortnames pass through
            # so future statuses don't silently disappear from R15.
            # test-management returns shortnames inconsistently — most use
            # underscores (`PASS`, `IN_PROGRESS`) but some use spaces
            # (`NOT APPLICABLE`, `PASSED WITH ISSUE`). Map both
            # conventions to the canonical display form so the
            # downstream code (R15 coverage filter, R18 N/A detection,
            # status-name comparisons) sees a stable string regardless
            # of which test-management install we're talking to.
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
                sid: _I18N_TO_DISPLAY.get(short, short.title())
                for short, sid in status_map.items()
            }
            s.finish("ok", f"PASS/FAIL -> {sorted(executed_ids)}")
    except extractor.AuthError as e:
        print(f"  reason: {e}", file=sys.stderr)
        return 2

    # Layer 1 (credibility fix): timestamp-aware cache. Carries the cached
    # execution_date alongside set membership so we can detect when a tester
    # re-submitted a previously-audited key and trigger a fresh audit.
    audited = _audited_keys_with_timestamps(out_dir)

    total_executed = 0
    total_skipped_status = 0
    total_skipped_dup = 0
    total_unresolved = 0
    total_reaudits = 0
    emitted_keys: list[str] = []
    # Parallel list: user_key (executed_by) for each emitted E-key, same order.
    emitted_user_keys: list[str | None] = []
    # N/A items get split off into their own list so they bypass the
    # LLM Runtime audit pipeline (no screenshots to look at) but still
    # produce a synthetic audit.json — R18 surfaces them in the main
    # table. Same shape: parallel list of user_keys for tester
    # bucketing later.
    na_keys: list[str] = []
    na_user_keys: list[str | None] = []

    total_runs_failed = 0
    # R15: capture every run's items as we enumerate so coverage.py
    # can compute gaps without re-calling get_testrun_items. Maps
    # `run_id -> list[item-dict]` (the same shape get_testrun_items
    # returns). Runs that fail enumeration are simply absent here —
    # R15 will silently miss those, which matches the existing
    # behaviour of "best-effort, skip on failure".
    items_by_run: dict[Any, list[dict[str, Any]]] = {}
    for i, run in enumerate(runs):
        run_id = run.get("id")
        if not run_id:
            continue
        # Throttle enumeration to avoid Tracker 429s on large folders.
        if i > 0 and i % 10 == 0:
            time.sleep(1.0)
        try:
            with progress.step(f"enumerating items in run {run.get('key') or run_id}") as s:
                items = extractor.get_testrun_items(session, base_url, run_id)
                s.finish("ok", f"{len(items)} item(s)")
        except extractor.AuthError as e:
            print(f"  reason: {e}", file=sys.stderr)
            return 2
        except requests.HTTPError as e:
            # Tracker hiccup on a single run — skip and keep going. The already-
            # emitted keys above us are still valid output for the parallelizer.
            total_runs_failed += 1
            print(f"  skipping run {run.get('key') or run_id}: {e}", file=sys.stderr)
            continue
        # Stash items for R15. Cheap (already in memory).
        items_by_run[run_id] = items

        for item in items:
            last = item.get("$lastTestResult") or {}
            status = last.get("testResultStatusId")
            result_id = last.get("id")
            # N/A items branch off into their own pipeline (synthetic
            # audit, no LLM Runtime). Status check happens BEFORE the
            # Pass/Fail filter so an N/A item that happens to have
            # the same status_id space (it doesn't, but defensively)
            # can't fall through to "skipped_status".
            if status in na_ids:
                if not result_id:
                    total_unresolved += 1
                    continue
                try:
                    summary = extractor.get_testresult_summary(
                        session, base_url, result_id)
                except extractor.AuthError as e:
                    print(f"  reason: {e}", file=sys.stderr)
                    return 2
                except requests.HTTPError:
                    total_unresolved += 1
                    continue
                if not summary or not summary.get("key"):
                    total_unresolved += 1
                    continue
                na_e_key = summary["key"]
                # Re-audit dedup matches the Pass/Fail gate: same
                # executionDate = same audit, skip.
                cached_date = audited.get(na_e_key)
                live_date = summary.get("execution_date")
                if (cached_date is not None
                        and live_date is not None
                        and cached_date == live_date):
                    total_skipped_dup += 1
                    continue
                na_keys.append(na_e_key)
                na_user_keys.append(summary.get("user_key"))
                continue
            if status not in executed_ids:
                total_skipped_status += 1
                continue
            total_executed += 1
            if not result_id:
                total_unresolved += 1
                continue
            try:
                summary = extractor.get_testresult_summary(session, base_url, result_id)
            except extractor.AuthError as e:
                print(f"  reason: {e}", file=sys.stderr)
                return 2
            except requests.HTTPError:
                total_unresolved += 1
                continue
            if not summary or not summary.get("key"):
                total_unresolved += 1
                continue
            e_key = summary["key"]
            # Re-audit gate. cached_date == live_date AND both are real
            # strings -> the artefact on disk was produced from this exact
            # execution; safe to skip. Any other combination (key never
            # audited, dates differ, or live date missing) falls through
            # to emission — re-auditing is the safe direction because
            # the cost is bounded (one LLM Runtime call) but the cost of a
            # silent stale FAIL on the dashboard is operator trust.
            cached_date = audited.get(e_key)
            live_date = summary.get("execution_date")
            if (cached_date is not None
                    and live_date is not None
                    and cached_date == live_date):
                total_skipped_dup += 1
                continue
            if cached_date is not None and cached_date != live_date:
                # Already had an audit, but the executionDate moved —
                # tester re-submitted. Track separately so the folder
                # summary tells operators "we're re-auditing N keys"
                # instead of pretending these are first-time audits.
                total_reaudits += 1
            emitted_keys.append(e_key)
            emitted_user_keys.append(summary.get("user_key"))

    # stderr: folder-level summary before kicking off the batch.
    unresolved_note = (
        f" / {total_unresolved} unresolved" if total_unresolved else ""
    )
    runs_failed_note = (
        f" / {total_runs_failed} run(s) skipped" if total_runs_failed else ""
    )
    # Surface re-audit count only when nonzero. Adding "0 re-audit(s)" to
    # every cron tick's summary line is noise; appearing only on the runs
    # that actually re-audit makes the signal stand out for an operator
    # scrolling stderr / log files.
    reaudits_note = (
        f" / {total_reaudits} re-audit(s)" if total_reaudits else ""
    )
    progress.done(
        f"folder '{folder_name}'",
        f"[{len(emitted_keys)} new / {total_executed} executed / "
        f"{total_skipped_dup} already-audited / "
        f"{total_skipped_status} unexecuted"
        f"{unresolved_note}{runs_failed_note}{reaudits_note}]",
    )

    # stderr: group emitted keys by executor (displayName).
    if emitted_keys:
        _print_by_tester(session, base_url, emitted_keys, emitted_user_keys)

    # R15: per-folder coverage gaps. Operates on the runs we already
    # listed above — no new Tracker calls beyond what build_coverage_items
    # makes, and that walks items via the same get_testrun_items
    # endpoint already used in the enumeration loop. Output is a
    # sibling artefact `_coverage.json` per folder; it never modifies
    # individual audit.json files (R15 is folder-level by design).
    #
    # Skip cleanly when there are no runs (early-return path above
    # already returned 0; this branch only catches the defensive case
    # where runs is somehow empty here).
    if runs:
        try:
            import coverage as _coverage
            import history_index as _history_index

            coverage_items = extractor.build_coverage_items(
                session, base_url, runs, status_id_to_name,
                # Reuse items already fetched during enumeration above
                # — R15 itself makes zero new Tracker calls. resolve_e_keys
                # adds per-executed-item E-key lookups (cached in
                # ~/.argus/ekeys.json) so the roster can link execution
                # keys; cold cache pays once, warm cache is free.
                items_by_run=items_by_run,
                resolve_e_keys=True,
            )
            snapshot = _coverage.CoverageSnapshot(
                folder_name=folder_name,
                items=coverage_items,
            )
            # History index is best-effort: R15a only uses it to
            # escalate Blocked → high, never to block firing. A failed
            # load just leaves severity at medium.
            try:
                hidx = _history_index.HistoryIndex.load_or_build(
                    scoped_out_dir)
            except Exception:
                hidx = None
            findings = _coverage.compute_gaps(snapshot, history_idx=hidx)
            coverage_path = scoped_out_dir / "_coverage.json"
            coverage_path.write_text(json.dumps(
                _coverage.to_coverage_json(snapshot, findings),
                indent=2))
            print(
                f"[argus] coverage: {len(findings)} gap(s) "
                f"-> {coverage_path.name}",
                file=sys.stderr,
            )
        except Exception as _e:
            # R15 is additive — if it crashes, the audit pipeline
            # below still runs. Log loudly so we notice during dev.
            print(f"[argus] coverage computation failed: "
                  f"{type(_e).__name__}: {_e}",
                  file=sys.stderr)

    # Persist the user cache after enumeration. Resolutions accumulated
    # during _print_by_tester (and any prior fetch_user_display_name
    # calls) are now in the in-memory cache — flush to ~/.argus/users.json
    # so the next process starts with them already resolved. Saves Tracker
    # API budget across cron ticks and prevents the USER-key
    # poisoning bug where a transient failure stuck around forever.
    try:
        import users as _users
        _users.get_default_cache().save()
    except Exception as _e:
        # Cache flush failures are non-fatal — we still have an in-memory
        # cache for the rest of this run.
        print(f"[argus] user cache save failed: "
              f"{type(_e).__name__}: {_e}", file=sys.stderr)

    # Process N/A items (synthetic audits, no LLM Runtime). Done here —
    # before the batch hand-off — so they appear in the audit table on
    # the same dashboard refresh as any Pass/Fail audits from this run.
    # Each N/A item: run extractor to fetch metadata.json (no
    # screenshots needed), then write a synthetic audit.json with R18
    # + the rest of the rule engine.
    if na_keys:
        print(f"\n[argus] processing {len(na_keys)} N/A item(s) "
              f"(synthetic audits, no LLM Runtime)", file=sys.stderr)
        import batch as _argus_batch
        n_na_ok = 0
        n_na_fail = 0
        for na_key in na_keys:
            try:
                ex_cfg = extractor.ExtractorConfig(
                    execution_key=na_key,
                    out_dir=settings.extractor.out_dir,
                    base_url=settings.extractor.base_url,
                )
                rc, work_dir = extractor.run(ex_cfg)
                if rc != 0 or work_dir is None:
                    n_na_fail += 1
                    continue
                _argus_batch._write_not_applicable_audit(work_dir, na_key)
                n_na_ok += 1
            except Exception as _e:
                # N/A processing is best-effort; if a single key
                # fails the rest of the run continues.
                print(f"[argus] N/A audit failed for {na_key}: "
                      f"{type(_e).__name__}: {_e}", file=sys.stderr)
                n_na_fail += 1
        print(f"[argus] N/A audits: {n_na_ok} written, "
              f"{n_na_fail} failed", file=sys.stderr)

    if not emitted_keys:
        return 0

    # Hand off to the batch runner.
    concurrency = settings.batch.concurrency
    extractor_concurrency = settings.batch.extractor_concurrency
    print(
        f"\nbatch: {len(emitted_keys)} keys, concurrency={concurrency} "
        f"(extractor={extractor_concurrency})",
        file=sys.stderr,
    )

    # env_check fires automatically inside run_batch as each audit
    # lands (run_batch owns the side pool + drain). No manual
    # back-fill phase needed.
    #
    # NOTE: Chat notifications for individual flagged audits used to
    # fire here via on_audit_complete. Removed 2026-05-26 — Chat is for
    # OPERATIONAL signals (audit started/done, system errors), not for
    # findings. Findings live in the ARGUS dashboard. The verbose
    # per-key pings (often 30+ per folder) trained operators to ignore
    # Chat, which defeated the alerts that DO matter (auth failures,
    # crashes). on_audit_complete is still available in batch.run_batch
    # if a future use case wants it.

    # Operational milestone: audit started. Only fires when there's
    # actual work to do (work-to-do gate). We already returned 0 above
    # if emitted_keys was empty, so reaching this line means we're
    # about to run `len(emitted_keys)` audits.
    import time as _time
    import argus_control as _argus_control_mark
    notify.post_audit_started(folder_name, len(emitted_keys),
                              n_reaudits=total_reaudits,
                              settings=settings)
    _start_ts = _time.monotonic()

    # Stamp the in-flight state so the ARGUS dashboard can render an
    # "AUDITING NOW" banner. Cleared in the finally block so the flag
    # is always released — on success, on raised exceptions, on
    # KeyboardInterrupt. Only SIGKILL leaves it stale, which
    # argus_control.current_audit() detects via PID liveness.
    _argus_control_mark.mark_audit_running(folder_name, len(emitted_keys))
    try:
        results = batch.run_batch(
            emitted_keys, settings,
            concurrency=concurrency,
            extractor_concurrency=extractor_concurrency,
            out=sys.stderr,
        )
    finally:
        _argus_control_mark.mark_audit_done()
        # Flush the user cache once more — workers resolved more user
        # keys during the audit phase (assignedTo / executedBy on each
        # E-key's metadata fetch). Persist now so the next folder run
        # benefits, even if this run is interrupted.
        try:
            import users as _users_after_batch
            _users_after_batch.get_default_cache().save()
        except Exception:
            pass
    failed_path = batch.write_failed_keys(results, settings.extractor.out_dir)
    summary_line = batch.summarize(results)
    print(f"batch: {summary_line}", file=sys.stderr)
    skipped_block = batch.format_skipped_by_tester(results)
    if skipped_block:
        print(skipped_block, file=sys.stderr)
    if failed_path:
        print(
            f"batch: failed keys -> {failed_path} "
            f"(retry: python batch.py --keys-file {failed_path})",
            file=sys.stderr,
        )

    # Operational milestone: audit done. Single roll-up for the cron
    # operator: how many ok / flagged / failed, and how long it took.
    # `flagged` here matches report._is_flagged criteria (verdict=fail
    # OR any high-severity finding) so what Chat reports as "flagged"
    # equals what the dashboard surfaces as "needs human eyeball".
    # Compute by re-reading audit.json — already on local disk, cheap.
    #
    # _is_flagged is in report.py (already imported lazily during the
    # audit batch via on_audit_complete previously). Re-do that import
    # here so the function is in scope.
    import report as _argus_report
    n_ok = sum(1 for r in results if r.status == "ok")
    n_failed = sum(1 for r in results
                   if r.status in ("failed", "auth_failed"))
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

    # env_check was run inline-during-batch via the on_audit_complete
    # callback above (overlaps with the rest of the audit batch instead
    # of running serially afterwards). Standalone env_check_backfill is
    # still available as a manual fallback (see env_check_backfill.py)
    # if a folder needs to be re-scored without re-running audits.

    # Generate the aggregate reports. Markdown (_report.md) keeps the
    # CLI-grep workflow; HTML (_report.html) is the explorable view.
    # Scoped to the folder dir and timestamped so each run writes a
    # fresh pair — avoids silently overwriting the previous day's
    # report, which matters for cron jobs and comparing runs over time.
    # Best-effort: a report-render failure is logged but must NOT mask
    # a successful batch. Operators can always re-run `report.py`
    # manually against the same out_dir if this path fails.
    try:
        import datetime as _dt
        import report as argus_report
        scoped = settings.extractor.out_dir
        stamp = _dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        # Markdown: timestamped, archive-style. Each folder run keeps
        # its history of run-summary markdowns — useful for diffing.
        md_path = scoped / f"_report_{stamp}.md"
        # HTML: ARGUS — the auditor-facing UI. Single canonical
        # `_argus.html` per folder (overwritten on each run) so
        # bookmarks / shared links keep working. Plus a timestamped
        # historical copy for traceability.
        html_canonical = scoped / "_argus.html"
        html_archive = scoped / f"_argus_{stamp}.html"
        md_path.write_text(argus_report.generate_report(scoped))
        html_text = argus_report.generate_html_report(scoped)
        html_canonical.write_text(html_text)
        html_archive.write_text(html_text)
        print(f"[ARGUS] open: file://{html_canonical.resolve()}",
              file=sys.stderr)
        print(f"[ARGUS] archive: {html_archive.name}", file=sys.stderr)
        print(f"[argus] markdown report: {md_path.name}", file=sys.stderr)

        # Auto-start the ARGUS HTTP server if it isn't already up. Idempotent:
        # we probe 127.0.0.1:2405 first, only spawn if no listener. The server
        # serves the whole `output/` tree (read-only) so the user can pull up
        # this folder + any historical folder via the same URL.
        # Detached double-fork so it survives the shell that ran argus.py
        # exiting / SSH dropping. stdout/stderr go to a stable log path so
        # the user can grep for the SSH-tunnel command later.
        try:
            import socket as _socket
            with _socket.socket(_socket.AF_INET,
                                _socket.SOCK_STREAM) as _s:
                _s.settimeout(0.3)
                listener_up = (_s.connect_ex(("127.0.0.1", 2405)) == 0)
        except Exception:
            listener_up = False
        if not listener_up:
            try:
                import subprocess as _sp
                project_root = Path(__file__).resolve().parent
                serve_path = project_root / "argus_serve.py"
                log_path = project_root / "_argus_serve.log"
                # `start_new_session` puts the child in its own session →
                # parent shell exit / SIGHUP doesn't kill it. Output is
                # redirected; nothing inherits the user's TTY.
                _sp.Popen(
                    [sys.executable, "-u", str(serve_path)],
                    stdout=open(log_path, "ab"),
                    stderr=_sp.STDOUT,
                    stdin=_sp.DEVNULL,
                    cwd=str(project_root),
                    start_new_session=True,
                )
                print(f"[ARGUS] server: started in background "
                      f"(log: {log_path.name})", file=sys.stderr)
                # Prefer the operator's stable secure tunnel URL when configured
                # (SSO-protected, works from any laptop with no SSH).
                # Fall back to the SSH-tunnel hint for fresh checkouts that
                # haven't created a tunnel yet.
                if settings.notify.tunnel_url:
                    print(f"[ARGUS] view: {settings.notify.tunnel_url}",
                          file=sys.stderr)
                else:
                    print(f"[ARGUS] view from your laptop:  "
                          f"ssh -L 2405:localhost:2405 dev  →  "
                          f"http://localhost:2405/  "
                          f"(tip: set [notify].tunnel_url in config.toml "
                          f"after `tunnel create 2405 --name argus`)",
                          file=sys.stderr)
            except Exception as _e:
                print(f"[ARGUS] auto-start skipped: "
                      f"{type(_e).__name__}: {_e}",
                      file=sys.stderr)
        else:
            print("[ARGUS] server: already running on :2405",
                  file=sys.stderr)
    except Exception as e:
        print(f"report generation raised {type(e).__name__}: {e} "
              "(non-fatal; rerun manually via report.py)",
              file=sys.stderr)

    if any(r.status == "auth_failed" for r in results):
        return 2
    if any(r.status == "failed" for r in results):
        return 1
    return 0


def _print_by_tester(session, base_url: str,
                     keys: list[str], user_keys: list[str | None]) -> None:
    """Group emitted E-keys by executor display name, print to stderr.

    Two-pass resolution so rate-limited user lookups don't leave raw
    USER… keys in the final output:
      1. First pass: resolve during grouping (normal path, uses the shared
         cache).
      2. Second pass: for any unique userKey that cache[uk] is still None,
         retry once more with the rate budget now largely recovered (the
         heavy listing loop is done by this point).
    """
    cache: dict[str, str | None] = {}
    unique_user_keys = {u for u in user_keys if u}

    # Pass 1: best-effort resolve.
    for u_key in unique_user_keys:
        extractor.fetch_user_display_name(session, base_url, u_key, cache)

    # Pass 2: retry any that slipped through. fetch_user_display_name caches
    # failures (None), so we clear only the failed entries before retrying.
    missing = [u for u in unique_user_keys if cache.get(u) is None]
    if missing:
        for u_key in missing:
            cache.pop(u_key, None)
            extractor.fetch_user_display_name(session, base_url, u_key, cache)

    groups: dict[str, list[str]] = {}
    for e_key, u_key in zip(keys, user_keys):
        if u_key:
            name = cache.get(u_key) or u_key
        else:
            name = "unknown"
        groups.setdefault(name, []).append(e_key)

    print("by tester:", file=sys.stderr)
    # Largest group first, then alphabetical.
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


def _already_audited_keys(out_dir: Path) -> set[str]:
    """All execution keys under out_dir that already have an audit.json.

    Walks both <out>/<E-KEY>/audit.json (legacy flat) and
    <out>/<testrun_slug>/<E-KEY>/audit.json (current).
    """
    if not out_dir.exists():
        return set()
    keys: set[str] = set()
    for audit in out_dir.rglob("audit.json"):
        if "_replay" in audit.parts:
            # Replay variants (V3/V4 experiments) live alongside the
            # canonical artefact and MUST NOT be treated as a "real"
            # audit for enumeration purposes — otherwise an experiment
            # run would silently mask a fresh tester execution.
            continue
        keys.add(audit.parent.name)
    return keys


def _audited_keys_with_timestamps(out_dir: Path) -> dict[str, str]:
    """Map of E-key -> cached execution_date for everything already audited.

    Layer 1 of the credibility fix: tester corrections.

    The old _already_audited_keys() returned pure set membership, which made
    re-execution invisible — once a key had any audit.json on disk, it was
    skipped forever, even if the tester corrected the steps and re-submitted.
    This function reads the sibling metadata.json for each audit.json and
    returns the executionDate that was current when the audit ran. The caller
    compares it to the live executionDate from Tracker: if they differ, the
    tester re-executed, and the key is re-emitted for a fresh audit.

    Why empty-string sentinel (not None) for missing/unreadable metadata:
    the comparison logic at the call site is `cached_date == live_date`. An
    empty string compares unequal to any real ISO timestamp, which triggers
    re-audit. That is the SAFE direction — if we can't tell whether the
    execution has changed, re-running the audit is cheap (LLM Runtime + Tracker
    calls, idempotent on disk) and produces no false negatives. Treating it
    as "still audited" would silently keep a flagged status even after the
    tester corrected, which is exactly the bug we're fixing.

    Skips paths under `_replay/` to mirror report._scan_output_dir's
    convention — those are prompt-variant experiments, not canonical audits,
    and using their dates would corrupt the cache.
    """
    if not out_dir.exists():
        return {}
    out: dict[str, str] = {}
    for audit in out_dir.rglob("audit.json"):
        if "_replay" in audit.parts:
            continue
        e_key = audit.parent.name
        meta_path = audit.parent / "metadata.json"
        cached_date = ""
        try:
            metadata = json.loads(meta_path.read_text())
            results = metadata.get("test_results") or []
            if results and isinstance(results[0], dict):
                # build_metadata stamps execution_date on test_results[0];
                # see extractor.build_metadata. Anything else is degraded
                # data — treat as unknown and let the sentinel trigger
                # re-audit.
                date_val = results[0].get("execution_date")
                if isinstance(date_val, str):
                    cached_date = date_val
        except (OSError, json.JSONDecodeError):
            # metadata.json missing, partial, or unreadable — leave the
            # sentinel as "" so any real date triggers re-audit.
            pass
        # If the same E-key shows up under multiple paths (e.g. legacy flat
        # AND current testrun-slug layout), the last one wins. That's fine
        # — a stale empty-string entry being clobbered by a real date is
        # the right direction; the reverse is also harmless because both
        # would trigger re-audit if they disagree with the live date.
        out[e_key] = cached_date
    return out


def _find_existing_workdir(out_dir: Path, key: str) -> Path | None:
    """Locate any existing <out>/.../<key>/ dir across all historical layouts.

    Tolerates:
      - flat:            <out>/<key>/
      - testrun-slug:    <out>/<testrun_slug>/<key>/
      - per-tester:      <out>/<testrun_slug>/<tester_slug>/<key>/
    """
    if not out_dir.exists():
        return None
    flat = out_dir / key
    if flat.is_dir():
        return flat
    # rglob catches both the 2-level and 3-level layouts in one pass.
    for candidate in out_dir.rglob(key):
        if candidate.is_dir():
            return candidate
    return None


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
