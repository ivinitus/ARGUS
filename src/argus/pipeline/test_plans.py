"""Read Zephyr test plans and their linked test runs."""
from __future__ import annotations

import re
from typing import Any

import requests

from argus.pipeline import extractor

PLAN_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-P[0-9]+$")
_RUN_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-C[0-9]+$")
_PLAN_FIELDS = "id,key,name,projectId,folderId"


def _positive_id(value: object) -> str:
    text = str(value)
    if not re.fullmatch(r"[0-9]+", text) or int(text) <= 0:
        raise ValueError("invalid Jira plan, project, or folder ID")
    return text


def _pages(
    session: requests.Session, url: str, params: dict[str, Any],
) -> list[dict[str, Any]]:
    """Accept Jira's unpaginated lists and paginated search envelopes."""
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    start = 0
    while True:
        response = extractor._get_with_retry(
            session, url, params={**params, "maxResults": 100, "startAt": start},
            context="fetching test-plan records",
        )
        extractor._raise_friendly(response, "fetching test-plan records")
        payload = response.json()
        page = payload.get("results") if isinstance(payload, dict) else payload
        if not isinstance(page, list) or any(
            not isinstance(item, dict) or item.get("id") is None for item in page
        ):
            raise ValueError("Jira returned malformed test-plan records")
        fresh = [item for item in page if str(item["id"]) not in seen]
        for item in fresh:
            if str(item["id"]) not in seen:
                records.append(item)
                seen.add(str(item["id"]))
        start += len(page)
        total = payload.get("total") if isinstance(payload, dict) else None
        if isinstance(total, int):
            if start >= total:
                if len(records) < total:
                    raise ValueError("Jira returned incomplete test-plan pagination")
                break
            if not fresh:
                raise ValueError("Jira returned incomplete test-plan pagination")
        elif len(page) < 100 or not fresh:
            break
    return records


def find_plan_by_key(
    session: requests.Session, base_url: str, key: str,
) -> dict[str, Any] | None:
    key = key.strip().upper()
    if not PLAN_KEY_RE.fullmatch(key):
        raise ValueError("invalid Jira test-plan key")
    records = _pages(
        session, f"{base_url}/rest/tests/1.0/testplan/search",
        {"query": f'testPlan.key IN ("{key}")', "fields": _PLAN_FIELDS, "archived": "false"},
    )
    exact = [item for item in records if str(item.get("key") or "").upper() == key]
    if len(exact) > 1:
        raise ValueError(f"Jira returned duplicate test plans for {key}")
    return exact[0] if exact else None


def get_plans_in_folder(
    session: requests.Session, base_url: str, project_id: str, folder_id: int | str,
) -> list[dict[str, Any]]:
    """List non-archived plans directly inside one test-plan folder."""
    return _pages(
        session, f"{base_url}/rest/tests/1.0/testplan/search",
        {
            "query": (
                f"testPlan.projectId IN ({_positive_id(project_id)}) AND "
                f"testPlan.folderTreeId IN ({_positive_id(folder_id)})"
            ),
            "fields": _PLAN_FIELDS,
            "archived": "false",
        },
    )


def get_runs_in_plan(
    session: requests.Session, base_url: str, plan_id: int | str,
) -> list[dict[str, Any]]:
    # Verified against the Test Plans UI: runs are attached by trace links.
    links = _pages(
        session,
        f"{base_url}/rest/tests/1.0/testplan/{_positive_id(plan_id)}/tracelinks/testrun",
        {"fields": "id,testRun(id,key,name,projectId,folderId,archived)"},
    )
    runs: dict[str, dict[str, Any]] = {}
    for link in links:
        run = link.get("testRun")
        if not isinstance(run, dict) or not _RUN_KEY_RE.fullmatch(str(run.get("key") or "")):
            raise ValueError("Jira test plan contains a link without test-run metadata")
        if not run.get("archived"):
            runs[run["key"]] = run
    return list(runs.values())
