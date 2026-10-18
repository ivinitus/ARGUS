"""Read Zephyr test cases and resolve their executed evidence."""
from __future__ import annotations

import re
from typing import Any

from argus.pipeline import extractor

CASE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-T[0-9]+$")
_EXECUTION_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-E[0-9]+$")


def find_case_by_key(session, base_url: str, key: str) -> dict[str, Any] | None:
    key = key.strip().upper()
    if not CASE_KEY_RE.fullmatch(key):
        raise ValueError("invalid Jira test-case key")
    response = extractor._get_with_retry(
        session, f"{base_url}/rest/tests/1.0/testcase/{key}",
        params={"fields": "id,key,name,projectId,folderId"},
        context=f"resolving test case {key}",
    )
    if response.status_code == 404:
        return None
    extractor._raise_friendly(response, f"resolving test case {key}")
    case = response.json()
    if (
        not isinstance(case, dict) or case.get("key") != key
        or not str(case.get("id", "")).isdigit() or int(case["id"]) <= 0
    ):
        raise ValueError(f"Jira returned invalid test-case metadata for {key}")
    return case


def get_case_results(session, base_url: str, case_id: int | str) -> list[dict]:
    """Read the testcase history's data/totalCount envelope without truncation."""
    if not str(case_id).isdigit() or int(case_id) <= 0:
        raise ValueError("invalid Jira test-case ID")
    results: dict[str, dict] = {}
    start = 0
    while True:
        response = extractor._get_with_retry(
            session, f"{base_url}/rest/tests/1.0/testcase/{case_id}/testresults",
            params={
                "fields": "id,key,testResultStatusId,executionDate,testRun(id,key,projectId),testCase(id)",
                "maxResults": 100, "startAt": start,
            },
            context=f"fetching executions for test case {case_id}",
        )
        extractor._raise_friendly(response, f"fetching executions for test case {case_id}")
        payload = response.json()
        page = payload.get("data") if isinstance(payload, dict) else None
        total = payload.get("totalCount") if isinstance(payload, dict) else None
        if not isinstance(page, list) or not isinstance(total, int) or total < 0:
            raise ValueError("Jira returned malformed test-case execution history")
        previous = len(results)
        for item in page:
            if (
                not isinstance(item, dict)
                or not _EXECUTION_KEY_RE.fullmatch(str(item.get("key") or ""))
                or not isinstance(item.get("testCase"), dict)
                or str(item["testCase"].get("id")) != str(case_id)
            ):
                raise ValueError("Jira returned an execution for an unexpected test case")
            results[item["key"]] = item
        if len(results) >= total:
            return list(results.values())
        if len(results) == previous:
            raise ValueError("Jira returned incomplete test-case execution pagination")
        start += len(page)


def executed_keys(settings, session, key: str) -> list[str]:
    """Select completed executions only, within the explicitly selected team."""
    from argus.operations.teams import resolve_jira_team
    from argus.pipeline.orchestrator import (
        EXECUTED_STATUS_NAMES, _normalize_execution_status_name,
    )

    base = settings.extractor.base_url
    case = find_case_by_key(session, base, key)
    if case is None:
        raise ValueError(f"Jira test case {key} was not found")
    project_id = str(case.get("projectId") or "")
    if project_id not in settings.extractor.project_ids:
        raise ValueError(
            f"{key} does not belong to selected profile projects "
            f"{', '.join(settings.extractor.project_ids)}"
        )
    status_map = extractor.get_project_status_ids(session, base, project_id)
    roles = {"pass", "fail", "passed_with_issue"}
    executed_names = {
        _normalize_execution_status_name(name) for name in (
            *EXECUTED_STATUS_NAMES,
            *(name for role, name in settings.policy.statuses.items() if role in roles),
            *(name for name, role in settings.policy.status_aliases.items() if role in roles),
        )
    }
    ignored = {
        _normalize_execution_status_name(name)
        for name in settings.extractor.ignored_execution_statuses
    }
    executed_ids = {
        str(sid) for name, sid in status_map.items()
        if _normalize_execution_status_name(name) in executed_names - ignored
    }
    selected = []
    run_teams: dict[str, str | None] = {}
    for result in get_case_results(session, base, case["id"]):
        if str(result.get("testResultStatusId")) not in executed_ids:
            continue
        context = result.get("testRun") or {}
        run_key = str(context.get("key") or "")
        if run_key not in run_teams:
            run = extractor.find_test_run_by_key(session, base, run_key)
            if run is None or str(run.get("projectId")) != project_id:
                raise ValueError(f"Could not resolve {key}'s execution run {run_key}")
            folder_id = run.get("folderId")
            if not isinstance(folder_id, (int, str)):
                raise ValueError(f"Could not resolve the folder for {run_key}")
            folder = extractor.find_folder_name_by_id(
                session, base, project_id, folder_id,
            )
            team = resolve_jira_team(project_id, folder or "")
            if team is None:
                raise ValueError(f"Could not resolve the ARGUS team for {run_key}")
            run_teams[run_key] = team.slug
        if run_teams[run_key] == settings.general.team_slug:
            selected.append(result["key"])
    if not selected:
        raise ValueError(f"{key} has no completed executions in team {settings.general.team_slug}")
    return list(dict.fromkeys(selected))
