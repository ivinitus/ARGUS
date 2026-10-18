"""Run a bounded set of manual folder, plan, test-run, and execution audits."""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import replace
from types import SimpleNamespace

import requests

from argus import config, runtime
from argus.operations import stop_signal
from argus.operations.test_run_targets import MAX_MANUAL_REFERENCES
from argus.pipeline import extractor, orchestrator, test_cases, test_plans


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run multiple already-resolved manual audit targets",
    )
    parser.add_argument("--folder", action="append", default=[])
    parser.add_argument("--run", action="append", default=[])
    parser.add_argument("--key", action="append", default=[])
    parser.add_argument("--plan", action="append", default=[])
    parser.add_argument("--plan-folder", action="append", default=[])
    parser.add_argument("--case", action="append", default=[])
    args = parser.parse_args(argv)
    targets = [*args.folder, *args.run, *args.key, *args.plan, *args.plan_folder, *args.case]
    if not targets:
        parser.error("provide at least one --folder, --run, --key, --case, --plan, or --plan-folder")
    if len(targets) > MAX_MANUAL_REFERENCES:
        parser.error(
            f"at most {MAX_MANUAL_REFERENCES} targets may be launched at once"
        )

    settings = config.load()
    runtime.configure(settings)
    extractor.configure_logging(settings.logging.verbose)
    # This entry point bypasses orchestrator.main, so `/argus stop` needs
    # the SIGTERM handler installed here as well.
    stop_signal.install()
    key_pattern = re.compile(settings.extractor.execution_key_pattern)
    invalid_keys = [key for key in args.key if not key_pattern.fullmatch(key)]
    if invalid_keys:
        parser.error(f"invalid execution key: {invalid_keys[0]}")
    run_pattern = re.compile(r"^[A-Z][A-Z0-9_]*-C[0-9]+$")
    invalid_runs = [key for key in args.run if not run_pattern.fullmatch(key)]
    if invalid_runs:
        parser.error(f"invalid test-run key: {invalid_runs[0]}")
    invalid_plans = [key for key in args.plan if not test_plans.PLAN_KEY_RE.fullmatch(key)]
    if invalid_plans:
        parser.error(f"invalid test-plan key: {invalid_plans[0]}")
    invalid_cases = [key for key in args.case if not test_cases.CASE_KEY_RE.fullmatch(key)]
    if invalid_cases:
        parser.error(f"invalid test-case key: {invalid_cases[0]}")

    return run_targets(
        settings, folders=args.folder, run_keys=args.run, execution_keys=args.key,
        plan_keys=args.plan, plan_folders=args.plan_folder, case_keys=args.case,
    )


def run_targets(
    settings,
    *,
    folders: list[str] | None = None,
    run_keys: list[str] | None = None,
    execution_keys: list[str] | None = None,
    plan_keys: list[str] | None = None,
    plan_folders: list[str] | None = None,
    case_keys: list[str] | None = None,
) -> int:
    """Keep each plan in its own title-based folder; group explicit C-keys by folder."""
    selected_keys = list(run_keys or [])
    plan_targets: list[tuple[dict, list[dict]]] = []
    session = None
    execution_keys = list(execution_keys or [])
    if case_keys:
        session = extractor.build_session(settings.extractor)
        try:
            for key in dict.fromkeys(case_keys):
                with orchestrator.progress.step(f"resolving test case {key}") as step:
                    resolved = test_cases.executed_keys(settings, session, key)
                    step.finish("ok", f"{key}: {len(resolved)} completed execution(s): {', '.join(resolved)}")
                execution_keys.extend(resolved)
        except (ValueError, extractor.AuthError, requests.RequestException) as exc:
            print(f"[ARGUS] {exc}", file=sys.stderr)
            return 1
    if plan_keys or plan_folders:
        session = extractor.build_session(settings.extractor)
        base_url = settings.extractor.base_url
        plans: dict[str, dict] = {}
        try:
            for key in dict.fromkeys(plan_keys or []):
                plan = test_plans.find_plan_by_key(session, base_url, key)
                if plan is None:
                    raise ValueError(f"Jira test plan {key} was not found")
                plans[plan["key"]] = plan
            for name in dict.fromkeys(plan_folders or []):
                resolved_folder = extractor.find_folder_for_profile(
                    session, settings.extractor, name, folder_type="testplan",
                )
                if resolved_folder is None:
                    raise ValueError(f"Jira test-plan folder {name!r} was not found")
                project_id, plan_folder = resolved_folder
                found = test_plans.get_plans_in_folder(
                    session, base_url, project_id, plan_folder["id"],
                )
                if not found:
                    raise ValueError(f"Jira test-plan folder {name!r} has no active plans")
                plans.update((plan["key"], plan) for plan in found)
            for plan in plans.values():
                project_id = str(plan.get("projectId") or "")
                if project_id not in settings.extractor.project_ids:
                    raise ValueError(
                        f"{plan['key']} does not belong to selected profile projects "
                        f"{', '.join(settings.extractor.project_ids)}"
                    )
                runs = test_plans.get_runs_in_plan(session, base_url, plan["id"])
                if not runs:
                    raise ValueError(f"Jira test plan {plan['key']} has no active linked runs")
                for linked_run in runs:
                    if str(linked_run.get("projectId") or "") != project_id:
                        raise ValueError(
                            f"{linked_run['key']} linked from {plan['key']} belongs to another project"
                        )
                plan_targets.append((plan, runs))
                print(
                    f"[ARGUS] test plan {plan['key']}: {len(runs)} linked run(s)",
                    file=sys.stderr,
                )
        except (ValueError, extractor.AuthError, requests.RequestException) as exc:
            print(f"[ARGUS] {exc}", file=sys.stderr)
            return 1

    return_code = 0
    for plan, runs in plan_targets:
        return_code = max(
            return_code,
            orchestrator._run_test_plan(plan, runs, settings),
        )

    full_folders = list(dict.fromkeys(folders or []))
    for folder in full_folders:
        return_code = max(
            return_code,
            orchestrator._run_folder_list(folder, settings),
        )

    run_folders: dict[tuple[str, str], list[str]] = {}
    folder_names: dict[tuple[str, str], str | None] = {}
    if selected_keys:
        session = session or extractor.build_session(settings.extractor)
        for run_key in dict.fromkeys(selected_keys):
            run = extractor.find_test_run_by_key(
                session,
                settings.extractor.base_url,
                run_key,
            )
            if run is None:
                print(
                    f"[ARGUS] Jira test run {run_key} was not found",
                    file=sys.stderr,
                )
                return_code = max(return_code, 1)
                continue
            project_id = str(run.get("projectId") or "")
            if project_id not in settings.extractor.project_ids:
                print(
                    f"[ARGUS] {run_key} belongs to Jira project "
                    f"{project_id or 'unknown'}, not selected profile projects "
                    f"{', '.join(settings.extractor.project_ids)}",
                    file=sys.stderr,
                )
                return_code = max(return_code, 1)
                continue
            folder_id = run.get("folderId")
            if not isinstance(folder_id, (int, str)):
                print(
                    f"[ARGUS] {run_key} is missing folderId",
                    file=sys.stderr,
                )
                return_code = max(return_code, 1)
                continue
            folder_key = (project_id, str(folder_id))
            if folder_key not in folder_names:
                folder_names[folder_key] = extractor.find_folder_name_by_id(
                    session,
                    settings.extractor.base_url,
                    project_id,
                    folder_id,
                )
            folder_name = folder_names[folder_key]
            if not folder_name:
                print(
                    f"[ARGUS] could not resolve the folder for {run_key}",
                    file=sys.stderr,
                )
                return_code = max(return_code, 1)
                continue
            run_folders.setdefault((project_id, folder_name), []).append(run_key)

    for (project_id, folder), run_keys in run_folders.items():
        if folder in full_folders:
            continue
        project_settings = settings
        if len(settings.extractor.project_ids) > 1:
            project_settings = replace(
                settings,
                extractor=replace(
                    settings.extractor, project_id=project_id, additional_project_ids=[],
                ),
            )
        return_code = max(
            return_code,
            orchestrator._run_folder_list(
                folder,
                project_settings,
                selected_run_keys=set(run_keys),
            ),
        )

    for key in dict.fromkeys(execution_keys or []):
        single_args = SimpleNamespace(
            execution_key=key,
            testrun_id=None,
            item_id=None,
            skip_extract=False,
            skip_audit=False,
        )
        return_code = max(
            return_code,
            orchestrator._run_single(single_args, settings),
        )
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
