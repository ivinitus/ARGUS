"""Extract test execution screenshots and metadata."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    import datetime
    from argus.domain import folder_coverage

import fitz
import requests

from argus import config as argus_config
from argus import runtime
from argus.core import atomic_io, redaction, secret_files
from argus.pipeline import attachment_quarantine, xml_evidence
from argus.pipeline.errors import AttachmentTooLargeError, AuthError

log = logging.getLogger("argus.extract")

JIRA_BASE_URL = "https://jira.example.com"

_EKEY_CACHE_PATH = Path.home() / ".argus" / "ekeys.json"


class _EKeyCache:
    """Tiny append-only id->E-key cache. Best-effort; never raises."""

    def __init__(self) -> None:
        self._map: dict[str, str] = {}
        try:
            raw = json.loads(_EKEY_CACHE_PATH.read_text())
            if isinstance(raw, dict):
                self._map = {str(k): str(v) for k, v in raw.items() if v}
        except (OSError, json.JSONDecodeError):
            pass

    def get(self, testresult_id: Any) -> str | None:
        return self._map.get(str(testresult_id))

    def put(self, testresult_id: Any, e_key: str) -> None:
        if testresult_id is not None and e_key:
            self._map[str(testresult_id)] = e_key

    def save(self) -> bool:
        try:
            _EKEY_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = _EKEY_CACHE_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._map, indent=2, sort_keys=True))
            tmp.replace(_EKEY_CACHE_PATH)
            return True
        except OSError:
            return False
TESTRESULTS_FIELDS = (
    "id,testResultStatusId,automated,executionDate,actualStartDate,actualEndDate,"
    "assignedTo,userKey,comment,traceLinks,webLinks,attachments,testRun(projectId),"
    "testCase(id,key,name,objective,precondition),"
    "testScriptResults(id,testResultStatusId,executionDate,index,description,"
    "expectedResult,comment,traceLinks,webLinks,attachments,stepAttachmentsMapping)"
)
EKEY_FIELDS = (
    "id,testResultStatusId,automated,executionDate,actualStartDate,actualEndDate,"
    "assignedTo,userKey,comment,traceLinks,webLinks,attachments,"
    "testRun(id,key,name,projectId,folderId),testCase(id,key,name,objective,precondition),"
    "testScriptResults(id,testResultStatusId,executionDate,index,description,"
    "expectedResult,comment,traceLinks,webLinks,attachments,stepAttachmentsMapping)"
)
SUMMARY_FIELDS = (
    "id,key,userKey,executionDate,testResultStatusId,comment,traceLinks,webLinks,"
    "attachments,testCase(id),"
    "testScriptResults(index,testResultStatusId,comment,traceLinks,webLinks,attachments)"
)

# Fingerprints written before status/comment/trace-link state was hashed
# carry no prefix; current ones are prefixed so stale checks can compare
# like-for-like until an old audit is refreshed.
EVIDENCE_FINGERPRINT_PREFIX = "v2:"


_SLUG_RE = re.compile(r"[^\w.-]+")
_CLONED_SUFFIX_RE = re.compile(r"\s*\(?\s*cloned\s*\)?\s*$", re.IGNORECASE)
_TESTRUN_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-C[0-9]+$")
_EXECUTION_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-E[0-9]+$")


def slugify(name: str, fallback: str = "unnamed") -> str:
    """Filesystem-safe slug: replace runs of non-[\\w.-] with `_`, trim edges."""
    if not name:
        return fallback
    slug = _SLUG_RE.sub("_", name).strip("_.")
    return slug or fallback


def _strip_cloned_suffix(name: str | None) -> str | None:
    """Strip a trailing '(cloned)' / 'cloned' from a Zephyr testrun name."""
    if not name:
        return name
    return _CLONED_SUFFIX_RE.sub("", name).rstrip()


@dataclass
class ExtractorConfig:
    out_dir: Path
    testrun_id: int | None = None
    item_id: int | None = None
    execution_key: str | None = None
    base_url: str = JIRA_BASE_URL
    jira_pat_env: str = "ARGUS_JIRA_PAT"
    jira_pat_path: Path = Path.home() / ".argus" / "jira.pat"
    include_test_case_weblinks: bool = False
    test_case_weblinks_all_statuses: bool = False
    # Pre-authenticated Jira session to reuse (e.g. one pooled session for a
    # whole batch). None builds a fresh session per run() call.
    session: Any = field(default=None, repr=False, compare=False)

    @classmethod
    def from_settings(cls, settings, **overrides) -> "ExtractorConfig":
        values = {
            "out_dir": settings.out_dir,
            "base_url": settings.base_url,
            "jira_pat_env": settings.jira_pat_env,
            "jira_pat_path": settings.jira_pat_path,
            "include_test_case_weblinks": getattr(settings, "include_test_case_weblinks", False),
            "test_case_weblinks_all_statuses": getattr(settings, "test_case_weblinks_all_statuses", False),
        }
        values.update(overrides)
        return cls(**values)


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader — KEY=VALUE per line, # comments, optional quotes."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def _warn_if_world_readable(path: Path) -> None:
    secret_files.warn_if_world_readable(path)


def _load_jira_pat(
    env_name: str = "ARGUS_JIRA_PAT",
    pat_file: Path | None = None,
) -> str | None:
    """Load a Jira Personal Access Token."""
    dotenv_path = Path.cwd() / ".env"
    _load_dotenv(dotenv_path)
    if dotenv_path.exists():
        _warn_if_world_readable(dotenv_path)

    for candidate in dict.fromkeys(
        (env_name, "ARGUS_JIRA_PAT")
    ):
        env = os.environ.get(candidate)
        if env:
            return env.strip()
    pat_file = (pat_file or Path.home() / ".argus" / "jira.pat").expanduser()
    if pat_file.exists():
        if secret_files.refuse_if_world_accessible(pat_file, "Jira PAT file"):
            return None
        return pat_file.read_text().strip() or None
    return None


_credential_provider = None


def configure_credentials(settings) -> None:
    """Configure the Jira token source for subsequent extraction requests."""
    global _credential_provider
    from argus.pipeline import jira_credentials
    _credential_provider = jira_credentials.build_provider(settings)


def build_session(settings=None) -> requests.Session:
    """Build a Jira/Zephyr session authenticated with a Bearer PAT.

    Requests' normal redirect handling strips credentials on cross-host
    redirects. Browser cookies and third-party authentication are not used.
    """
    from argus.pipeline import jira_credentials
    provider = _credential_provider
    if provider is None:
        env_name = getattr(settings, "jira_pat_env", "ARGUS_JIRA_PAT")
        pat_path = getattr(
            settings, "jira_pat_path", Path.home() / ".argus" / "jira.pat")
        provider = jira_credentials.LocalCredentialProvider(env_name, pat_path)
    pat = provider.get_pat()
    if not pat:
        raise jira_credentials.JiraCredentialError(
            "No Jira personal access token is configured; set ARGUS_JIRA_PAT "
            "or the selected profile's credential environment variable or PAT file."
        )
    session = requests.Session()
    # GET attachment requests must not send a default Content-Type header.
    session.headers.update({
        "Accept": "application/json, text/plain, */*",
        "Authorization": f"Bearer {pat}",
    })
    return session


# AuthError and AttachmentTooLargeError live in argus.pipeline.errors and
# stay importable as extractor.AuthError / extractor.AttachmentTooLargeError.


def _alert_operator(message: str) -> None:
    """Emit an auth or degradation warning even when logging is quiet."""
    sys.stderr.write(f"\n[ARGUS] ALERT: {message}\n")
    sys.stderr.flush()
    log.warning(message)


def _raise_friendly(resp: requests.Response, context: str) -> None:
    """Turn auth-ish HTTP errors into a clean AuthError with a hint."""
    if resp.status_code in (401, 403):
        hint = (
            "auth rejected — check that your Jira PAT is current, has the "
            "required project permissions, and is provided through "
            "ARGUS_JIRA_PAT or the selected profile's credential source"
        )
        raise AuthError(f"{context}: HTTP {resp.status_code}. {hint}")
    resp.raise_for_status()


def _get_with_retry(
    session: requests.Session,
    url: str,
    *,
    context: str,
    params: dict[str, Any] | None = None,
    timeout: float = 30,
    max_attempts: int = 6,
) -> requests.Response:
    """GET with backoff on 429 / 5xx / transient transport errors."""
    import time
    delay = 1.0
    for attempt in range(1, max_attempts + 1):
        try:
            resp = session.get(url, params=params, timeout=timeout)
        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError) as e:
            if attempt == max_attempts:
                raise
            log.warning("%s: %s — retrying in %.1fs (attempt %d/%d)",
                        context, type(e).__name__, delay, attempt,
                        max_attempts)
            time.sleep(delay)
            delay = min(delay * 2, 16.0)
            continue
        if resp.status_code not in (429, 500, 502, 503, 504):
            return resp
        if attempt == max_attempts:
            return resp
        retry_after = resp.headers.get("Retry-After")
        wait = (
            max(delay, min(float(retry_after), _RETRY_AFTER_CAP_SECONDS))
            if retry_after and retry_after.isdigit() else delay
        )
        log.warning("%s: HTTP %s — retrying in %.1fs (attempt %d/%d)",
                    context, resp.status_code, wait, attempt, max_attempts)
        time.sleep(wait)
        delay = min(delay * 2, 16.0)
    return resp


def fetch_test_results(session: requests.Session, cfg: ExtractorConfig) -> list[dict[str, Any]]:
    """Fetch test results."""
    if cfg.execution_key:
        url = f"{cfg.base_url}/rest/tests/1.0/testresult/{cfg.execution_key}"
        resp = _get_with_retry(
            session, url,
            params={"fields": EKEY_FIELDS},
            context=f"fetching testresult {cfg.execution_key}",
        )
        _raise_friendly(resp, f"fetching testresult {cfg.execution_key}")
        return [resp.json()]

    url = f"{cfg.base_url}/rest/tests/1.0/testrun/{cfg.testrun_id}/testresults"
    params = {"fields": TESTRESULTS_FIELDS, "itemId": cfg.item_id}
    resp = _get_with_retry(
        session, url,
        params=params,
        context=f"fetching testresults for testrun={cfg.testrun_id}",
    )
    _raise_friendly(resp, f"fetching testresults for testrun={cfg.testrun_id}")
    return resp.json()


def find_folder_by_name(
    session: requests.Session,
    base_url: str,
    folder_name: str,
    project_id: str,
    *,
    folder_type: str = "testrun",
) -> dict[str, Any] | None:
    """Walk a project's run or plan folder tree for an exact name."""
    if folder_type not in {"testrun", "testplan"}:
        raise ValueError("invalid Jira folder type")
    url = f"{base_url}/rest/tests/1.0/project/{project_id}/foldertree/{folder_type}"
    resp = _get_with_retry(session, url,
                           context=f"fetching folder tree for project {project_id}")
    _raise_friendly(resp, f"fetching folder tree for project {project_id}")
    tree = resp.json()

    def _walk(node: Any) -> dict[str, Any] | None:
        if isinstance(node, list):
            for n in node:
                hit = _walk(n)
                if hit:
                    return hit
            return None
        if not isinstance(node, dict):
            return None
        if node.get("name") == folder_name:
            return node
        for child in node.get("children") or []:
            hit = _walk(child)
            if hit:
                return hit
        return None

    return _walk(tree)


def find_folder_for_profile(
    session: requests.Session,
    settings: argus_config.ExtractorSettings,
    folder_name: str,
    *,
    folder_type: str = "testrun",
) -> tuple[str, dict[str, Any]] | None:
    """Resolve a folder in one allowed project; require a key on ambiguity."""
    matches = []
    options = {"folder_type": folder_type} if folder_type != "testrun" else {}
    for project_id in settings.project_ids:
        folder = find_folder_by_name(
            session, settings.base_url, folder_name, project_id, **options,
        )
        if folder:
            matches.append((project_id, folder))
    if len(matches) > 1:
        raise ValueError(
            f"Folder {folder_name!r} exists in multiple configured Jira projects; "
            "use a run or test-plan key to select the project"
        )
    return matches[0] if matches else None


_DATE_IN_FOLDER_RX = re.compile(
    r"\b(?P<day>\d{1,2})\s+"
    r"(?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+"
    r"(?P<year>20\d{2})\b",
    re.IGNORECASE,
)
_MONTH_NUM = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_date_from_folder_name(name: str) -> "datetime.date | None":
    """Extract DD-Mon-YYYY from a folder name."""
    import datetime as _dt
    m = _DATE_IN_FOLDER_RX.search(name or "")
    if not m:
        return None
    try:
        day = int(m.group("day"))
        month = _MONTH_NUM[m.group("mon").lower()[:3]]
        year = int(m.group("year"))
        return _dt.date(year, month, day)
    except (ValueError, KeyError):
        return None


def find_folder_name_by_id(
    session: requests.Session,
    base_url: str,
    project_id: str,
    folder_id: int | str,
    *,
    folder_type: str = "testrun",
) -> str | None:
    """Resolve a numeric Zephyr folderId to its display name."""
    if folder_type not in {"testrun", "testplan"}:
        raise ValueError("invalid Jira folder type")
    url = f"{base_url}/rest/tests/1.0/project/{project_id}/foldertree/{folder_type}"
    resp = _get_with_retry(
        session, url,
        context=f"fetching folder tree for project {project_id}")
    _raise_friendly(resp, f"fetching folder tree for project {project_id}")
    tree = resp.json()
    target = int(folder_id) if folder_id is not None else None

    def _walk(node: Any) -> str | None:
        if isinstance(node, list):
            for n in node:
                hit = _walk(n)
                if hit:
                    return hit
            return None
        if not isinstance(node, dict):
            return None
        if node.get("id") == target:
            return node.get("name")
        for child in node.get("children") or []:
            hit = _walk(child)
            if hit:
                return hit
        return None

    return _walk(tree)


def find_latest_active_folder(
    session: requests.Session,
    base_url: str,
    project_id: str,
    *,
    name_prefix: str = "Targeted regression",
    max_age_days: int = 7,
) -> dict[str, Any] | None:
    """Walk the folder tree, return the most recent folder matching the prefix."""
    import datetime as _dt
    url = f"{base_url}/rest/tests/1.0/project/{project_id}/foldertree/testrun"
    resp = _get_with_retry(
        session, url,
        context=f"fetching folder tree for project {project_id}")
    _raise_friendly(resp, f"fetching folder tree for project {project_id}")
    tree = resp.json()

    prefix_lower = name_prefix.lower()
    today = _dt.date.today()
    cutoff = today - _dt.timedelta(days=max_age_days)
    candidates: list[tuple[_dt.date, str, dict[str, Any]]] = []

    def _walk(node: Any) -> None:
        if isinstance(node, list):
            for n in node:
                _walk(n)
            return
        if not isinstance(node, dict):
            return
        nm = node.get("name") or ""
        if nm.lower().startswith(prefix_lower):
            d = _parse_date_from_folder_name(nm)
            if d and cutoff <= d <= today:
                candidates.append((d, nm, node))
        for child in node.get("children") or []:
            _walk(child)

    _walk(tree)
    if not candidates:
        return None
    candidates.sort(key=lambda t: (-t[0].toordinal(), t[1]))
    return candidates[0][2]


def get_test_runs_in_folder(
    session: requests.Session,
    base_url: str,
    folder_id: str | int,
    project_id: str,
) -> list[dict[str, Any]]:
    """All non-archived test runs directly inside this folder (paginated)."""
    url = f"{base_url}/rest/tests/1.0/testrun/search"
    page_size = 100
    start_at = 0
    runs: list[dict[str, Any]] = []
    seen_ids: set[Any] = set()
    while True:
        params = {
            "fields": "id,key,name,folderId",
            "query": (
                f"testRun.projectId IN ({project_id}) AND "
                f"testRun.folderTreeId IN ({folder_id}) "
                "ORDER BY testRun.createdOn DESC"
            ),
            "maxResults": page_size,
            "startAt": start_at,
            "archived": "false",
        }
        resp = _get_with_retry(
            session, url, params=params,
            context=f"searching test runs in folder {folder_id}")
        _raise_friendly(resp, f"searching test runs in folder {folder_id}")
        payload = resp.json()
        if isinstance(payload, dict) and "results" in payload:
            page = payload["results"]
        else:
            page = payload if isinstance(payload, list) else []
        if not isinstance(page, list):
            break
        fresh = [
            run for run in page
            if not (isinstance(run, dict) and run.get("id") in seen_ids)
        ]
        for run in fresh:
            if isinstance(run, dict):
                seen_ids.add(run.get("id"))
        runs.extend(fresh)
        # A short page ends the listing; a page of only repeats means the
        # server ignored startAt, so stop rather than loop forever.
        if len(page) < page_size or not fresh:
            break
        start_at += len(page)
    return runs


def find_test_run_by_key(
    session: requests.Session,
    base_url: str,
    test_run_key: str,
) -> dict[str, Any] | None:
    """Resolve one validated Zephyr test-run key to compact metadata."""
    key = test_run_key.strip().upper()
    if not _TESTRUN_KEY_RE.fullmatch(key):
        raise ValueError("invalid Jira test-run key")
    url = f"{base_url}/rest/tests/1.0/testrun/search"
    params = {
        "fields": "id,key,name,projectId,folderId",
        "query": f'testRun.key IN ("{key}")',
        "maxResults": 2,
        "startAt": 0,
        "archived": "false",
    }
    resp = _get_with_retry(
        session,
        url,
        params=params,
        context=f"resolving test run {key}",
    )
    _raise_friendly(resp, f"resolving test run {key}")
    payload = resp.json()
    results = payload.get("results", []) if isinstance(payload, dict) else payload
    exact = [
        item for item in (results if isinstance(results, list) else [])
        if isinstance(item, dict) and str(item.get("key") or "").upper() == key
    ]
    if not exact:
        return None
    if len(exact) > 1:
        raise RuntimeError(f"Jira returned duplicate test runs for {key}")
    return exact[0]


def find_test_result_context(
    session: requests.Session,
    base_url: str,
    execution_key: str,
) -> dict[str, Any] | None:
    """Resolve an E-key to compact parent test-run metadata."""
    key = execution_key.strip().upper()
    if not _EXECUTION_KEY_RE.fullmatch(key):
        raise ValueError("invalid Jira execution key")
    url = f"{base_url}/rest/tests/1.0/testresult/{key}"
    resp = _get_with_retry(
        session,
        url,
        params={
            "fields": (
                "id,key,testRun(id,key,name,projectId,folderId)"
            )
        },
        timeout=15,
        context=f"resolving execution {key}",
    )
    if resp.status_code == 404:
        return None
    _raise_friendly(resp, f"resolving execution {key}")
    payload = resp.json()
    return payload if isinstance(payload, dict) else None


def get_testrun_items(
    session: requests.Session,
    base_url: str,
    testrun_id: str | int,
) -> list[dict[str, Any]]:
    """Items in a test run with their last result.

    Endpoint quirks: `fields` is REQUIRED (omitting it returns HTTP
    400), and sub-field selection like `$lastTestResult(id)` silently
    returns zero items — ask for the plain field.
    """
    url = f"{base_url}/rest/tests/1.0/testrun/{testrun_id}/testrunitems"
    params = {"fields": "id,index,issueCount,$lastTestResult"}
    resp = _get_with_retry(session, url, params=params,
                           context=f"fetching items for testrun {testrun_id}")
    _raise_friendly(resp, f"fetching items for testrun {testrun_id}")
    payload = resp.json()
    if isinstance(payload, dict):
        return (payload.get("testRunItems")
                or payload.get("results")
                or payload.get("items")
                or [])
    return payload if isinstance(payload, list) else []


def get_testresult_key(
    session: requests.Session,
    base_url: str,
    testresult_id: int | str,
) -> str | None:
    """Resolve a numeric testresult id -> its QA-E... key."""
    url = f"{base_url}/rest/tests/1.0/testresult/{testresult_id}"
    resp = _get_with_retry(session, url, params={"fields": "id,key"},
                           timeout=15,
                           context=f"resolving testresult {testresult_id}")
    if resp.status_code != 200:
        return None
    return resp.json().get("key")


def get_testresult_summary(
    session: requests.Session,
    base_url: str,
    testresult_id: int | str,
    *,
    test_case_weblink_status_ids: set[int] | None = None,
    test_case_weblink_cache: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any] | None:
    """Resolve a numeric testresult id -> compact live audit state."""
    url = f"{base_url}/rest/tests/1.0/testresult/{testresult_id}"
    resp = _get_with_retry(
        session, url,
        params={"fields": SUMMARY_FIELDS},
        timeout=15,
        context=f"resolving testresult {testresult_id}",
    )
    if resp.status_code != 200:
        return None
    data = resp.json()
    if test_case_weblink_status_ids is not None:
        enrich_test_case_weblinks(
            session, base_url, [data], test_case_weblink_status_ids,
            cache=test_case_weblink_cache,
        )
    summary = {
        "key": data.get("key"),
        "user_key": data.get("userKey"),
        "execution_date": data.get("executionDate"),
        "evidence_fingerprint": build_evidence_fingerprint([data]),
        "evidence_fingerprint_legacy": legacy_evidence_fingerprint([data]),
    }
    if data.get("test_case_weblinks_checked"):
        summary["test_case_weblinks_checked"] = True
    return summary


def get_project_status_ids(
    session: requests.Session,
    base_url: str,
    project_id: str,
) -> dict[str, int]:
    """Map i18n short name (e.g. 'PASS', 'FAIL') -> numeric status id for this project."""
    url = f"{base_url}/rest/tests/1.0/project/{project_id}/testresultstatus"
    resp = _get_with_retry(
        session, url, timeout=15,
        context=f"fetching status ids for project {project_id}")
    _raise_friendly(resp, f"fetching status ids for project {project_id}")
    out: dict[str, int] = {}
    for s in resp.json() or []:
        i18n = s.get("i18nKey", "")
        short = i18n.rsplit(".", 1)[-1] if i18n else (s.get("name") or "").upper()
        if short and "id" in s:
            out[short] = s["id"]
    return out


# Status names are per project and effectively static; every key in a batch
# used to refetch them. Cache non-empty maps per (base_url, project) so a
# batch fetches once, under a lock so concurrent extractors share one fetch.
_STATUS_MAP_TTL_S = 900.0
_status_map_cache: dict[tuple[str, str], tuple[float, dict[int, str]]] = {}
_status_map_lock = threading.Lock()


def clear_status_map_cache() -> None:
    """Forget cached project status maps (tests and long-lived processes)."""
    with _status_map_lock:
        _status_map_cache.clear()


def get_project_status_map(
    session: requests.Session,
    base_url: str,
    project_id: str | int,
) -> dict[int, str]:
    """Return {status_id: display_name} for a project, e.g. {39: 'Pass', 40: 'Fail'}.

    Non-empty results are cached per (base_url, project) for
    `_STATUS_MAP_TTL_S`; empty results (which trigger operator alerts) are
    never cached so a transient failure is retried by the next key.
    """
    cache_key = (base_url, str(project_id))
    with _status_map_lock:
        cached = _status_map_cache.get(cache_key)
        if (
            cached is not None
            and time.monotonic() - cached[0] < _STATUS_MAP_TTL_S
        ):
            return dict(cached[1])
        status_map = _fetch_project_status_map(session, base_url, project_id)
        if status_map:
            _status_map_cache[cache_key] = (time.monotonic(), dict(status_map))
        return status_map


def _fetch_project_status_map(
    session: requests.Session,
    base_url: str,
    project_id: str | int,
) -> dict[int, str]:
    url = f"{base_url}/rest/tests/1.0/project/{project_id}/testresultstatus"
    try:
        resp = _get_with_retry(
            session, url, timeout=15,
            context=f"fetching status map for project {project_id}")
        if resp.status_code != 200:
            log.warning("status-map fetch for project %s returned HTTP %s",
                        project_id, resp.status_code)
            return {}
        out: dict[int, str] = {}
        for s in resp.json() or []:
            sid = s.get("id")
            name = s.get("name")
            if isinstance(sid, int) and name:
                out[sid] = name
        return out
    except (requests.RequestException, ValueError) as e:
        log.warning("status-map fetch for project %s failed: %s", project_id, e)
        return {}


def _strip_html(text: str | None, *, preserve_links: bool = False) -> str:
    """Minimal HTML->text for tester comments."""
    if not text:
        return ""
    s = text
    if preserve_links:
        def _anchor(match: re.Match[str]) -> str:
            label = match.group("label")
            href = re.search(
                r"""\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""",
                match.group("attrs"), flags=re.I,
            )
            if href is None:
                return label
            url = html.unescape(next(v for v in href.groups() if v is not None))
            try:
                parsed = urlparse(url)
                safe = parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)
                safe = safe and parsed.username is None and parsed.password is None
            except ValueError:
                safe = False
            if not safe or url in html.unescape(re.sub(r"<[^>]+>", "", label)):
                return label
            return f"{label} ({html.escape(url)})"
        s = re.sub(
            r"<a\b(?P<attrs>[^>]*)>(?P<label>.*?)</a\s*>",
            _anchor, s, flags=re.I | re.S,
        )
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<img[^>]*>", "", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    # One decoding pass: "&amp;lt;" is the literal text "&lt;", and numeric
    # entities such as "&#39;" must resolve too.
    s = html.unescape(s).replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _redact_optional(value: Any) -> Any:
    """Redact credentials in tester-authored text, leaving non-strings alone."""
    if isinstance(value, str) and value:
        return redaction.redact_credentials(value)
    return value


def _normalize_trace_links(raw: Any) -> list[str]:
    """Extract human-readable link strings from a traceLinks field."""
    if not raw:
        return []
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, str):
            out.append(item)
            continue
        if not isinstance(item, dict):
            continue
        issue = item.get("issue")
        if isinstance(issue, dict):
            value = issue.get("key") or issue.get("id")
            if value:
                out.append(str(value))
                continue
        for key in ("link", "key", "url", "issueKey", "issueId", "id"):
            val = item.get(key)
            if val:
                out.append(str(val))
                break
    return out


def _issue_links(record: dict[str, Any]) -> list[str]:
    """Keep actual Issues separate from web/confluence trace links."""
    raw = record.get("traceLinks", record.get("trace_links")) or []
    if not isinstance(raw, list):
        return []
    return _normalize_trace_links([
        item for item in raw
        if not isinstance(item, dict) or item.get("issueId") or item.get("issue") or not any(
            item.get(field) for field in (
                "url", "webLink", "urlDescription", "confluencePage", "confluenceAppId",
            )
        )
    ])


def _web_links(record: dict[str, Any]) -> list[str]:
    """Read Weblinks aliases and URL-bearing entries in Zephyr traceLinks."""
    items: list[Any] = []
    for field_name in ("webLinks", "weblinks", "web_links", "links"):
        raw = record.get(field_name)
        items.extend(raw if isinstance(raw, list) else [raw])
    for item in record.get("traceLinks") or []:
        if isinstance(item, dict) and any(
            item.get(field) for field in ("url", "webLink", "urlDescription")
        ):
            items.append(item)
    links: list[str] = []
    for item in items:
        if isinstance(item, str):
            if item.strip():
                links.append(redaction.redact_credentials(item.strip()))
        elif isinstance(item, dict):
            link = item.get("webLink", item)
            if isinstance(link, str):
                if link.strip():
                    links.append(redaction.redact_credentials(link.strip()))
                continue
            if not isinstance(link, dict):
                continue
            for key in ("url", "link", "urlDescription", "description"):
                if isinstance(link.get(key), str) and link[key].strip():
                    links.append(redaction.redact_credentials(link[key].strip()))
                    break
    return list(dict.fromkeys(links))


def issue_status_ids(status_map: dict[int, str]) -> set[int]:
    from argus.domain.workflow_rules import status_role
    return {
        sid for sid, name in status_map.items()
        if status_role(name.replace("_", " ")) in {"fail", "passed_with_issue"}
    }


def enrich_test_case_weblinks(
    session: requests.Session,
    base_url: str,
    test_results: list[dict[str, Any]],
    status_ids: set[int],
    *,
    cache: dict[str, list[dict[str, Any]]] | None = None,
) -> None:
    """Fetch only each affected execution's own test-case links.

    A folder enumeration may reuse successful responses for repeated test cases;
    a new enumeration or extraction starts fresh so link edits remain visible.
    """
    if cache is None:
        cache = {}
    for tr in test_results:
        if not any(
            item.get("testResultStatusId") in status_ids
            for item in [tr, *(tr.get("testScriptResults") or [])]
        ):
            continue
        case = tr.get("testCase") or {}
        case_id = str(case.get("id") or "")
        if not re.fullmatch(r"[1-9][0-9]*", case_id):
            raise ValueError("failed execution is missing its test-case ID for Weblinks")
        if case_id not in cache:
            response = _get_with_retry(
                session, f"{base_url}/rest/tests/1.0/testcase/{case_id}/tracelinks/weblink",
                params={"fields": "id,url,urlDescription"},
                context=f"fetching Weblinks for test case {case_id}",
            )
            _raise_friendly(response, f"fetching Weblinks for test case {case_id}")
            links = response.json()
            if not isinstance(links, list) or any(not isinstance(link, dict) for link in links):
                raise ValueError("Jira returned malformed test-case Weblinks")
            cache[case_id] = links
        tr["test_case_weblinks"] = _web_links({"webLinks": cache[case_id]})
        tr["test_case_weblinks_checked"] = True


def fetch_user_display_name(
    session: requests.Session,
    base_url: str,
    user_key: str,
    cache: dict[str, str | None] | None = None,
) -> str | None:
    """Resolve a userKey (e.g. JIRAUSER12345) to a displayName."""
    if not user_key:
        return None

    if cache is not None and user_key in cache:
        return cache[user_key]

    from argus.core import users
    persistent = users.get_default_cache()
    name = persistent.resolve(session, base_url, user_key)

    if cache is not None:
        cache[user_key] = name

    return name


# Jira/WAF Retry-After headers are honoured but bounded: a worker holding the
# extractor semaphore must never sleep for hours on one header value.
_RETRY_AFTER_CAP_SECONDS = 60.0


def download_attachment(
    session: requests.Session,
    base_url: str,
    attachment_id: int,
    dest: Path,
    *,
    max_bytes: int | None = None,
) -> str:
    """Stream-download an attachment with retry on 429 / 5xx."""
    import time
    url = f"{base_url}/rest/tests/1.0/attachment/{attachment_id}"
    max_attempts = 6
    delay = 1.0
    last_resp: requests.Response | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = session.get(url, stream=True, timeout=120)
        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError) as e:
            if attempt == max_attempts:
                raise
            log.warning(
                "downloading attachment %s: %s — retrying in %.1fs "
                "(attempt %d/%d)",
                attachment_id, type(e).__name__, delay, attempt,
                max_attempts,
            )
            time.sleep(delay)
            delay = min(delay * 2, 16.0)
            continue
        last_resp = resp
        if resp.status_code not in (429, 500, 502, 503, 504):
            break
        if attempt == max_attempts:
            break
        retry_after = resp.headers.get("Retry-After")
        wait = (
            max(delay, min(float(retry_after), _RETRY_AFTER_CAP_SECONDS))
            if retry_after and retry_after.isdigit() else delay
        )
        log.warning(
            "downloading attachment %s: HTTP %s — retrying in %.1fs (attempt %d/%d)",
            attachment_id, resp.status_code, wait, attempt, max_attempts,
        )
        resp.close()
        time.sleep(wait)
        delay = min(delay * 2, 16.0)

    assert last_resp is not None
    with last_resp as resp:
        _raise_friendly(resp, f"downloading attachment {attachment_id}")
        content_length = resp.headers.get("Content-Length")
        if max_bytes is not None and content_length and content_length.isdigit():
            if int(content_length) > max_bytes:
                raise AttachmentTooLargeError(
                    f"attachment {attachment_id} exceeds {max_bytes} byte limit"
                )
        downloaded = 0
        try:
            with dest.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        downloaded += len(chunk)
                        if max_bytes is not None and downloaded > max_bytes:
                            raise AttachmentTooLargeError(
                                f"attachment {attachment_id} exceeds "
                                f"{max_bytes} byte limit"
                            )
                        f.write(chunk)
        except BaseException:
            dest.unlink(missing_ok=True)
            raise
    return resp.headers.get("Content-Type", "")


def split_pdf_to_images(
    pdf_path: Path,
    out_dir: Path,
    max_edge_px: int = 1568,
    fmt: str = "jpg",
    jpeg_quality: int = 95,
) -> list[Path]:
    """Render each PDF page to a raster image, capped at max_edge_px on the longer edge."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in list(out_dir.glob("page_*.jpg")) + list(out_dir.glob("page_*.png")):
        try:
            old.unlink()
        except OSError:
            pass
    written: list[Path] = []
    ext = "jpg" if fmt.lower() in {"jpg", "jpeg"} else "png"
    with fitz.open(pdf_path) as doc:
        for i, page in enumerate(doc, start=1):
            rect = page.rect
            long_edge = max(rect.width, rect.height)
            zoom = max_edge_px / long_edge if long_edge else 1.0
            matrix = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            dest = out_dir / f"page_{i:03d}.{ext}"
            if ext == "jpg":
                pix.save(dest, jpg_quality=jpeg_quality)
            else:
                pix.save(dest)
            written.append(dest)
    return written



_MARKETPLACE_TLD = {
    "US": ".com",
    "UK": ".co.uk",
    "AU": ".com.au",
    "CA": ".ca",
    "DE": ".de",
    "FR": ".fr",
    "ES": ".es",
    "IT": ".it",
    "JP": ".co.jp",
    "BR": ".com.br",
    "IN": ".in",
}
_MP_DELIM_BEFORE = r"[\s_\-\[\](){}|/,:]"
_MP_DELIM_AFTER = r"[\s_\-\[\](){}|/,:.]"


def _compile_mp_token_rx(marketplaces) -> re.Pattern[str]:
    return re.compile(
        rf"(?:^|{_MP_DELIM_BEFORE})("
        + "|".join(map(re.escape, marketplaces))
        + rf")(?={_MP_DELIM_AFTER}|$)"
    )


_MP_TOKEN_RX = _compile_mp_token_rx(_MARKETPLACE_TLD.keys())
_NOISE_TOKENS_RX = re.compile(
    rf"(?:^|{_MP_DELIM_BEFORE})"
    r"(GMA|GMB|GMC|E2E|RNB|ALC|AYCL|RCDC|TYP|MP|QA|UI|UX)"
    rf"(?={_MP_DELIM_AFTER}|$)",
    re.IGNORECASE,
)


def configure_policy(policy) -> None:
    """Apply profile-specific marketplace mappings."""
    global _MARKETPLACE_TLD, _MP_TOKEN_RX
    if not policy.marketplace_tlds:
        return
    _MARKETPLACE_TLD = {
        marketplace.upper(): tld.lower()
        for marketplace, tld in policy.marketplace_tlds.items()
    }
    _MP_TOKEN_RX = _compile_mp_token_rx(_MARKETPLACE_TLD)


def parse_marketplace_from_testrun_name(name: str | None) -> str | None:
    """Extract the marketplace token (e.g. "AU", "BR") from a Zephyr testrun name."""
    if not name:
        return None
    cleaned = _NOISE_TOKENS_RX.sub(" ", name)
    m = _MP_TOKEN_RX.search(cleaned)
    if not m:
        return None
    return m.group(1).upper()


def _sole_marketplace_in_name(name: str | None) -> str | None:
    """Return one marketplace from a name, abstaining on ambiguous lists."""
    if not name:
        return None
    cleaned = _NOISE_TOKENS_RX.sub(" ", name)
    found = {
        match.group(1).upper()
        for match in _MP_TOKEN_RX.finditer(cleaned)
    }
    if len(found) != 1:
        return None
    return found.pop()


def resolve_marketplace(
    testrun_name: str | None,
    test_case_name: str | None = None,
) -> str | None:
    """Use the run marketplace, else an unambiguous test-case marketplace."""
    return (
        parse_marketplace_from_testrun_name(testrun_name)
        or _sole_marketplace_in_name(test_case_name)
    )


_SURFACE_RX = re.compile(
    r"(?:^|[\s_\-])(Desktop|Mobile|Webview|Tablet)(?=[\s_\-(]|$)",
    re.IGNORECASE,
)
_BROWSER_RX = re.compile(
    r"(?:^|[\s_\-(])(Chrome|Firefox|Edge|Safari|Samsung\s+Internet)"
    r"(?=[\s_\-)]|$)",
    re.IGNORECASE,
)
_DEVICE_RX = re.compile(
    r"(?:^|[\s_\-(])(Samsung|iPhone|iPad|Pixel|Android|iOS)"
    r"(?=[\s_\-)]|$)",
    re.IGNORECASE,
)


def parse_surface_from_testrun_name(name: str | None) -> str | None:
    """Return "Desktop" | "Mobile" | "Webview" | "Tablet" | None."""
    if not name:
        return None
    m = _SURFACE_RX.search(name)
    if m:
        return m.group(1).capitalize()
    return None


def parse_browser_from_testrun_name(name: str | None) -> str | None:
    """Return canonical browser name or None when unspecified."""
    if not name:
        return None
    m = _BROWSER_RX.search(name)
    if not m:
        return None
    raw = m.group(1).strip().lower()
    return {
        "chrome": "Chrome",
        "firefox": "Firefox",
        "edge": "Edge",
        "safari": "Safari",
        "samsung internet": "Samsung Internet",
    }.get(raw)


def parse_device_from_testrun_name(name: str | None) -> str | None:
    """Return canonical device-class token or None when unspecified."""
    if not name:
        return None
    m = _DEVICE_RX.search(name)
    if not m:
        return None
    raw = m.group(1).lower()
    return {
        "samsung": "Samsung",
        "iphone": "iPhone",
        "ipad": "iPad",
        "pixel": "Pixel",
        "android": "Android",
        "ios": "iOS",
    }.get(raw)


def marketplace_tld(marketplace: str | None) -> str | None:
    """Look up the storefront.<tld> suffix for a marketplace code."""
    if not marketplace:
        return None
    return _MARKETPLACE_TLD.get(marketplace.upper())


def build_coverage_items(
    session: requests.Session,
    base_url: str,
    runs: list[dict[str, Any]],
    status_map: dict[int, str],
    user_cache: dict[str, str | None] | None = None,
    *,
    items_by_run: dict[Any, list[dict[str, Any]]] | None = None,
    resolve_e_keys: bool = False,
    ignored_status_ids: set[int] | None = None,
) -> list["folder_coverage.CoverageItem"]:
    """Flatten included testrun items into coverage records for R15."""
    from argus.domain import folder_coverage  # noqa: WPS433 — intentional local import

    ekey_cache = _EKeyCache()
    _ekey_dirty = False

    out: list[folder_coverage.CoverageItem] = []
    for run in runs:
        run_id = run.get("id")
        if not run_id:
            continue
        run_key = run.get("key") or ""
        run_name = run.get("name")
        marketplace = parse_marketplace_from_testrun_name(run_name)

        if items_by_run is not None and run_id in items_by_run:
            items = items_by_run[run_id]
        else:
            try:
                items = get_testrun_items(session, base_url, run_id)
            except (requests.HTTPError, requests.RequestException) as e:
                log.warning(
                    "build_coverage_items: skipping run %s (%s)",
                    run_key or run_id, e,
                )
                continue

        for item in items:
            last = item.get("$lastTestResult") or {}
            status_id = last.get("testResultStatusId")
            if ignored_status_ids and status_id in ignored_status_ids:
                continue
            status_name = (status_map.get(status_id)
                           if isinstance(status_id, int)
                           else None) or "Unknown"
            test_case = (last.get("testCase") or {})
            tc_key = test_case.get("key") or ""
            tc_name = test_case.get("name") or None
            assigned_to = last.get("assignedTo") or last.get("userKey")
            assigned_to_name = (
                fetch_user_display_name(
                    session, base_url, assigned_to, user_cache)
                if assigned_to else None
            )
            executed_by = last.get("userKey")
            executed_by_name = (
                fetch_user_display_name(
                    session, base_url, executed_by, user_cache)
                if executed_by else None
            )
            e_key = None
            if resolve_e_keys and executed_by:
                tr_id = last.get("id")
                if tr_id is not None:
                    e_key = ekey_cache.get(tr_id)
                    if e_key is None:
                        try:
                            e_key = get_testresult_key(
                                session, base_url, tr_id)
                        except (requests.HTTPError,
                                requests.RequestException):
                            e_key = None
                        if e_key:
                            ekey_cache.put(tr_id, e_key)
                            _ekey_dirty = True
            out.append(folder_coverage.CoverageItem(
                test_case_key=tc_key,
                testrun_key=run_key,
                marketplace=marketplace or resolve_marketplace(
                    run_name, tc_name),
                status=status_name,
                assigned_to=assigned_to,
                assigned_to_name=assigned_to_name,
                executed_by=executed_by,
                executed_by_name=executed_by_name,
                testrun_name=run_name,
                test_case_name=tc_name,
                e_key=e_key,
            ))
    if _ekey_dirty:
        ekey_cache.save()
    return out


def build_metadata(
    test_results: list[dict[str, Any]],
    session: requests.Session | None = None,
    base_url: str = JIRA_BASE_URL,
    user_cache: dict[str, str | None] | None = None,
    status_map: dict[int, str] | None = None,
) -> dict[str, Any]:
    if not test_results:
        return {"test_results": []}

    if user_cache is None:
        user_cache = {}
    if status_map is None:
        status_map = {}

    def resolve(user_key: str | None) -> str | None:
        if not user_key or session is None:
            return None
        return fetch_user_display_name(session, base_url, user_key, user_cache)

    def resolve_status(status_id: Any) -> str | None:
        if not isinstance(status_id, int):
            return None
        return status_map.get(status_id)

    cleaned = []
    for tr in test_results:
        steps = tr.get("testScriptResults") or []
        steps_sorted = sorted(steps, key=lambda s: s.get("index", 0))
        executed_by_key = tr.get("userKey")
        assigned_to_key = tr.get("assignedTo")
        testcase = tr.get("testCase") or {}
        top_status_id = tr.get("testResultStatusId")
        top_comment = redaction.redact_credentials(
            _strip_html(tr.get("comment"), preserve_links=True))
        top_trace_links = _issue_links(tr)
        web_links = _web_links(tr)
        testrun = tr.get("testRun") or {}
        testrun_name = testrun.get("name")
        marketplace = resolve_marketplace(
            testrun_name, testcase.get("name"))
        cleaned.append({
            "id": tr.get("id"),
            "status_id": top_status_id,
            "status": resolve_status(top_status_id),
            "test_case_key": testcase.get("key"),
            "test_case_name": testcase.get("name"),
            "test_case_objective": redaction.redact_credentials(
                _strip_html(testcase.get("objective"))),
            "test_case_precondition": redaction.redact_credentials(
                _strip_html(testcase.get("precondition"))),
            "testrun_key": testrun.get("key"),
            "testrun_name": testrun_name,
            "marketplace": marketplace,
            "marketplace_tld": marketplace_tld(marketplace),
            "expected_surface": parse_surface_from_testrun_name(testrun_name),
            "expected_browser": parse_browser_from_testrun_name(testrun_name),
            "expected_device": parse_device_from_testrun_name(testrun_name),
            "executed_by": executed_by_key,
            "executed_by_name": resolve(executed_by_key),
            "assigned_to": assigned_to_key,
            "assigned_to_name": resolve(assigned_to_key),
            "execution_date": tr.get("executionDate"),
            "evidence_fingerprint": build_evidence_fingerprint([tr]),
            "actual_start": tr.get("actualStartDate"),
            "actual_end": tr.get("actualEndDate"),
            "comment": top_comment,
            "trace_links": top_trace_links,
            "weblinks": web_links,
            "test_case_weblinks": tr.get("test_case_weblinks") or [],
            "test_case_weblinks_checked": bool(tr.get("test_case_weblinks_checked")),
            "attachments": [
                {"id": a.get("id"), "name": a.get("name"), "size": a.get("fileSize")}
                for a in (tr.get("attachments") or [])
            ],
            "steps": [
                {
                    "index": s.get("index"),
                    "description": _redact_optional(s.get("description")),
                    "expected_result": _redact_optional(s.get("expectedResult")),
                    "status_id": s.get("testResultStatusId"),
                    "status": resolve_status(s.get("testResultStatusId")),
                    "execution_date": s.get("executionDate"),
                    "comment": redaction.redact_credentials(
                        _strip_html(s.get("comment"), preserve_links=True)),
                    "trace_links": _issue_links(s),
                    "weblinks": _web_links(s),
                    "attachments": [
                        {"id": a.get("id"), "name": a.get("name"),
                         "size": a.get("fileSize")}
                        for a in (s.get("attachments") or [])
                    ],
                }
                for s in steps_sorted
            ],
        })
    return {"test_results": cleaned}


def collect_pdf_attachments(
    test_results: list[dict[str, Any]],
) -> list[tuple[int, str]]:
    """Gather PDF attachments from both places testers put them: 1."""
    out: list[tuple[int, str]] = []
    seen: set[int] = set()

    def _add(attachments: list[dict[str, Any]] | None) -> None:
        for a in attachments or []:
            att_id = a.get("id")
            if (
                not isinstance(att_id, int)
                or isinstance(att_id, bool)
                or att_id <= 0
                or att_id in seen
            ):
                continue
            name = a.get("name") or f"attachment_{att_id}"
            if name.lower().endswith(".pdf"):
                seen.add(att_id)
                out.append((att_id, name))

    for tr in test_results:
        _add(tr.get("attachments"))
        for step in tr.get("testScriptResults") or []:
            _add(step.get("attachments"))
    return out


_IMAGE_EXTS: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".gif", ".webp")
_VIDEO_EXTS: tuple[str, ...] = (".mp4", ".webm")
SUPPORTED_EVIDENCE_EXTS = frozenset({".pdf", ".xml", *_IMAGE_EXTS, *_VIDEO_EXTS})
UNSUPPORTED_EVIDENCE_EXTS = frozenset({
    ".3gp",
    ".aac",
    ".avi",
    ".bmp",
    ".doc",
    ".docx",
    ".heic",
    ".heif",
    ".m4a",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp3",
    ".ogg",
    ".ppt",
    ".pptx",
    ".tif",
    ".tiff",
    ".wav",
})

_INLINE_IMG_RE = re.compile(
    r'<img\b[^>]*\bsrc\s*=\s*["\'][^"\']*/attachment/image/(\d+)[^"\']*["\']',
    re.IGNORECASE,
)


def _extract_inline_image_ids(html: str | None) -> list[int]:
    """Return attachment ids referenced via <img src=...attachment/image/N>."""
    if not html or not isinstance(html, str):
        return []
    return [int(m) for m in _INLINE_IMG_RE.findall(html)]


def _is_image_name(name: str | None) -> bool:
    """True when a filename looks like an image we can ship to the model."""
    if not name:
        return False
    lower = name.lower()
    return any(lower.endswith(ext) for ext in _IMAGE_EXTS)


def is_supported_evidence_name(name: str | None) -> bool:
    """True when an attachment name identifies a format ARGUS can audit."""
    if not isinstance(name, str):
        return False
    lower = name.lower()
    return any(lower.endswith(ext) for ext in SUPPORTED_EVIDENCE_EXTS)


def is_unsupported_evidence_name(name: str | None) -> bool:
    """True when a named attachment looks like evidence ARGUS cannot audit."""
    if not isinstance(name, str):
        return False
    lower = name.lower()
    return any(lower.endswith(ext) for ext in UNSUPPORTED_EVIDENCE_EXTS)


def _fingerprint_scalar(value: Any) -> Any:
    """Normalize Zephyr metadata values for deterministic JSON hashing."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _state_text_digest(text: Any) -> str | None:
    """Digest tester text the way metadata stores it (stripped, redacted)."""
    if not isinstance(text, str) or not text.strip():
        return None
    cleaned = redaction.redact_credentials(_strip_html(text, preserve_links=True))
    if not cleaned:
        return None
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:16]


def _trace_links_digest(raw: Any) -> str | None:
    links = sorted(_normalize_trace_links(raw))
    if not links:
        return None
    payload = json.dumps(links, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _evidence_fingerprint_records(
    test_results: list[dict[str, Any]],
    *,
    include_state: bool = True,
) -> list[dict[str, Any]]:
    """Return normalized records that affect audit input or classification.

    Attachment records cover the evidence itself. With ``include_state`` the
    execution's status, comment, Issues and Weblinks, and each step's status,
    comment and links are hashed too, so a re-marked status, an edited
    comment or a newly linked defect triggers a re-audit instead of being
    served from the cached verdict. Both the raw Zephyr shape and the cached
    metadata.json shape produce identical records.
    """
    records: list[dict[str, Any]] = []

    def _add_state(source: str, step_index: Any, record: dict[str, Any]) -> None:
        status_id = record.get("testResultStatusId")
        if status_id is None:
            status_id = record.get("status_id")
        state = {
            "source": source,
            "step_index": _fingerprint_scalar(step_index),
            "status_id": _fingerprint_scalar(status_id),
            "comment": _state_text_digest(record.get("comment")),
            "trace_links": _trace_links_digest(_issue_links(record)),
        }
        # Preserve existing v2 hashes when no new link inputs are present.
        for field_name, links in (
            ("weblinks", _web_links(record)),
            ("test_case_weblinks", record.get("test_case_weblinks")),
        ):
            if links:
                state[field_name] = _trace_links_digest(links)
        if record.get("test_case_weblinks_checked"):
            state["test_case_weblinks_checked"] = True
        records.append(state)

    def _add_attachment(
        source: str,
        step_index: Any,
        attachment: dict[str, Any],
    ) -> None:
        att_id = attachment.get("id")
        if att_id is None:
            return
        name = attachment.get("name")
        if not (
            is_supported_evidence_name(name)
            or is_unsupported_evidence_name(name)
        ):
            return
        size = (attachment.get("fileSize") if "fileSize" in attachment
                else attachment.get("size"))
        records.append({
            "source": source,
            "step_index": _fingerprint_scalar(step_index),
            "id": _fingerprint_scalar(att_id),
            "name": _fingerprint_scalar(name),
            "size": _fingerprint_scalar(size),
        })

    def _add_inline(source: str, step_index: Any, att_id: int) -> None:
        records.append({
            "source": source,
            "step_index": _fingerprint_scalar(step_index),
            "id": _fingerprint_scalar(att_id),
            "name": None,
            "size": None,
        })

    for tr in test_results or []:
        for attachment in tr.get("attachments") or []:
            if isinstance(attachment, dict):
                _add_attachment("top_attachment", None, attachment)
        for img_id in _extract_inline_image_ids(tr.get("comment")):
            _add_inline("top_comment_inline", None, img_id)
        if include_state:
            _add_state("execution_state", None, tr)
        steps = tr.get("testScriptResults")
        if not isinstance(steps, list):
            steps = tr.get("steps") or []
        for step in steps:
            if not isinstance(step, dict):
                continue
            idx = step.get("index")
            for attachment in step.get("attachments") or []:
                if isinstance(attachment, dict):
                    _add_attachment("step_attachment", idx, attachment)
            for img_id in _extract_inline_image_ids(step.get("comment")):
                _add_inline("step_comment_inline", idx, img_id)
            if include_state:
                _add_state("step_state", idx, step)

    unique = {
        json.dumps(r, sort_keys=True, separators=(",", ":")): r
        for r in records
    }
    return [unique[k] for k in sorted(unique)]


def _fingerprint_digest(records: list[dict[str, Any]]) -> str:
    payload = json.dumps(records, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_evidence_fingerprint(test_results: list[dict[str, Any]]) -> str:
    """Hash evidence plus execution state for re-audit detection."""
    return EVIDENCE_FINGERPRINT_PREFIX + _fingerprint_digest(
        _evidence_fingerprint_records(test_results, include_state=True))


def legacy_evidence_fingerprint(test_results: list[dict[str, Any]]) -> str:
    """Attachment-only hash used by audits written before state hashing."""
    return _fingerprint_digest(
        _evidence_fingerprint_records(test_results, include_state=False))


def collect_image_attachments(
    test_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Gather every image attachment testers provided, with step attribution."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[int, int | None]] = set()

    def _add(att_id: int, name: str | None, step_index: int | None, source: str) -> None:
        key = (att_id, step_index)
        if key in seen:
            return
        seen.add(key)
        out.append({
            "id": att_id,
            "name": name or f"attachment_{att_id}.png",
            "step_index": step_index,
            "source": source,
        })

    for tr in test_results:
        for a in tr.get("attachments") or []:
            att_id = a.get("id")
            nm = a.get("name")
            if isinstance(att_id, int) and _is_image_name(nm):
                _add(att_id, nm, None, "top_attachment")

        for img_id in _extract_inline_image_ids(tr.get("comment")):
            _add(img_id, f"inline_{img_id}.png", None, "top_comment_inline")

        for step in tr.get("testScriptResults") or []:
            idx = step.get("index")
            for a in step.get("attachments") or []:
                att_id = a.get("id")
                nm = a.get("name")
                if isinstance(att_id, int) and _is_image_name(nm):
                    _add(att_id, nm, idx, "step_attachment")
            for img_id in _extract_inline_image_ids(step.get("comment")):
                _add(img_id, f"inline_{img_id}.png", idx, "step_comment_inline")

    return out


def collect_video_attachments(
    test_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Gather MP4 and WEBM evidence with top-level or step attribution."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[int, int | None]] = set()

    def _add(
        attachment: dict[str, Any],
        step_index: int | None,
        source: str,
    ) -> None:
        att_id = attachment.get("id")
        name = attachment.get("name")
        if (
            not isinstance(att_id, int)
            or isinstance(att_id, bool)
            or att_id <= 0
            or not isinstance(name, str)
            or not name.lower().endswith(_VIDEO_EXTS)
        ):
            return
        key = (att_id, step_index)
        if key in seen:
            return
        seen.add(key)
        size = (attachment.get("fileSize") if "fileSize" in attachment
                else attachment.get("size"))
        out.append({
            "id": att_id,
            "name": name,
            "size": size,
            "step_index": step_index,
            "source": source,
        })

    for tr in test_results:
        for attachment in tr.get("attachments") or []:
            if isinstance(attachment, dict):
                _add(attachment, None, "top_attachment")
        steps = tr.get("testScriptResults")
        if not isinstance(steps, list):
            steps = tr.get("steps") or []
        for step in steps:
            if not isinstance(step, dict):
                continue
            for attachment in step.get("attachments") or []:
                if isinstance(attachment, dict):
                    _add(attachment, step.get("index"), "step_attachment")
    return out


def collect_unsupported_attachments(
    test_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Gather attached evidence candidates ARGUS cannot currently audit."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[int, int | None]] = set()

    def _add(
        attachment: dict[str, Any],
        step_index: int | None,
        source: str,
    ) -> None:
        att_id = attachment.get("id")
        if (
            not isinstance(att_id, int)
            or isinstance(att_id, bool)
            or att_id <= 0
        ):
            return
        name = attachment.get("name")
        if is_supported_evidence_name(name):
            return
        if not is_unsupported_evidence_name(name):
            return
        key = (att_id, step_index)
        if key in seen:
            return
        seen.add(key)
        size = (attachment.get("fileSize") if "fileSize" in attachment
                else attachment.get("size"))
        out.append({
            "id": att_id,
            "name": name if isinstance(name, str) and name else None,
            "size": size,
            "step_index": step_index,
            "source": source,
        })

    for tr in test_results:
        for attachment in tr.get("attachments") or []:
            if isinstance(attachment, dict):
                _add(attachment, None, "top_attachment")
        steps = tr.get("testScriptResults")
        if not isinstance(steps, list):
            steps = tr.get("steps") or []
        for step in steps:
            if not isinstance(step, dict):
                continue
            index = step.get("index")
            for attachment in step.get("attachments") or []:
                if isinstance(attachment, dict):
                    _add(attachment, index, "step_attachment")
    return out


def collect_xml_attachments(
    test_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Gather Appium/ADB XML attachments with step attribution."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[int, int | None]] = set()

    def _add(
        attachment: dict[str, Any],
        step_index: int | None,
        source: str,
    ) -> None:
        att_id = attachment.get("id")
        name = attachment.get("name")
        if (
            not isinstance(att_id, int)
            or not isinstance(name, str)
            or not name.lower().endswith(".xml")
        ):
            return
        key = (att_id, step_index)
        if key in seen:
            return
        seen.add(key)
        out.append({
            "id": att_id,
            "name": name,
            "step_index": step_index,
            "source": source,
        })

    for tr in test_results:
        for attachment in tr.get("attachments") or []:
            if isinstance(attachment, dict):
                _add(attachment, None, "top_attachment")
        for step in tr.get("testScriptResults") or []:
            index = step.get("index")
            for attachment in step.get("attachments") or []:
                if isinstance(attachment, dict):
                    _add(attachment, index, "step_attachment")
    return out


_MAX_STEP_IMAGE_BYTES = 5_000_000

_MAX_STEP_IMAGE_EDGE_PX = 1568


def _resize_if_oversized(
    path: Path,
    max_edge: int = _MAX_STEP_IMAGE_EDGE_PX,
) -> bool:
    """Resize an image file in-place if either dimension > `max_edge`."""
    try:
        src_pix = fitz.Pixmap(str(path))
    except Exception as e:
        log.warning("could not open %s for resize check: %s", path, e)
        return False

    long_edge_px = max(src_pix.width, src_pix.height)
    if long_edge_px <= max_edge:
        return False

    try:
        with fitz.open(str(path)) as doc:
            if len(doc) == 0:
                return False
            page = doc[0]
            point_long = max(page.rect.width, page.rect.height)
            if point_long <= 0:
                return False
            zoom = max_edge / point_long
            new_pix = page.get_pixmap(
                matrix=fitz.Matrix(zoom, zoom),
                alpha=False,
            )
    except Exception as e:
        log.warning(
            "could not shrink %s (%dx%d px -> target %d long edge): %s",
            path, src_pix.width, src_pix.height, max_edge, e,
        )
        return False

    ext = path.suffix.lower()
    try:
        if ext in {".jpg", ".jpeg"}:
            new_pix.save(str(path), jpg_quality=90)
        else:
            new_pix.save(str(path))
    except Exception as e:
        log.warning("could not save resized %s: %s", path, e)
        return False
    return True


def _step_attachment_path(step_dir: Path, rec: dict[str, Any]) -> Path:
    """Build an ID-based path that never contains the Jira display name."""
    step_idx = rec.get("step_index")
    prefix = "top" if step_idx is None else f"step_{int(step_idx):03d}"
    raw_name = rec.get("name") or f"attachment_{rec['id']}.png"
    suffix = next(
        (extension for extension in _IMAGE_EXTS if raw_name.lower().endswith(extension)),
        ".png",
    )
    return step_dir / f"{prefix}_{rec['id']}{suffix}"


def _xml_attachment_path(xml_dir: Path, rec: dict[str, Any]) -> Path:
    step_idx = rec.get("step_index")
    prefix = "top" if step_idx is None else f"step_{int(step_idx):03d}"
    return xml_dir / f"{prefix}_{rec['id']}.xml"


def _video_attachment_path(video_dir: Path, rec: dict[str, Any]) -> Path:
    step_idx = rec.get("step_index")
    prefix = "top" if step_idx is None else f"step_{int(step_idx):03d}"
    suffix = ".webm" if str(rec.get("name", "")).lower().endswith(".webm") else ".mp4"
    return video_dir / f"{prefix}_{rec['id']}{suffix}"


def _derive_workdir(
    cfg: ExtractorConfig,
    test_results: list[dict[str, Any]],
    tester_name: str | None = None,
) -> Path:
    """Pick output path from the test run name when we can see it."""
    tr0 = test_results[0] if test_results else {}
    testrun = tr0.get("testRun") or {}
    testrun_name = _strip_cloned_suffix(testrun.get("name"))
    leaf = cfg.execution_key or f"{cfg.testrun_id}_{cfg.item_id}"
    tester_slug = slugify(tester_name, fallback="unknown") if tester_name else "unknown"
    if testrun_name:
        return cfg.out_dir / slugify(testrun_name) / tester_slug / leaf
    return cfg.out_dir / tester_slug / leaf


def expected_pdf_names(pdf_attachments: list[tuple[Any, str]]) -> set[str]:
    """Return generated PDF names; original Jira names remain metadata only."""
    return {f"attachment_{att_id}.pdf" for att_id, _name in pdf_attachments}


def _reconcile_stale_evidence(
    work_dir: Path,
    pdf_attachments: list[tuple[Any, str]],
    xml_attachments: list[dict[str, Any]] | None = None,
) -> None:
    """Delete evidence left over from a previous submission."""
    import shutil

    expected = expected_pdf_names(pdf_attachments)
    expected_stems = {Path(n).stem for n in expected}
    screenshots_root = work_dir / "screenshots"
    xml_root = work_dir / "xml_evidence"
    normalized_xml = work_dir / "xml_evidence.json"
    video_root = work_dir / "video_evidence"
    normalized_video = work_dir / "video_evidence.json"

    for pdf in work_dir.glob("*.pdf"):
        if pdf.name in expected:
            continue
        log.info("removing stale evidence from a previous submission: %s",
                 pdf.name)
        try:
            pdf.unlink()
        except OSError as e:
            log.warning("could not remove stale PDF %s: %s", pdf.name, e)

    if screenshots_root.is_dir():
        for subdir in screenshots_root.iterdir():
            if not subdir.is_dir():
                continue
            if subdir.name == "step_attachments":
                try:
                    shutil.rmtree(subdir)
                except OSError as e:
                    log.warning("could not reset step_attachments: %s", e)
                continue
            if subdir.name not in expected_stems:
                log.info("removing stale page images: screenshots/%s/",
                         subdir.name)
                try:
                    shutil.rmtree(subdir)
                except OSError as e:
                    log.warning("could not remove stale screenshots/%s: %s",
                                subdir.name, e)
    if xml_root.exists():
        try:
            shutil.rmtree(xml_root)
        except OSError as e:
            log.warning("could not reset XML evidence: %s", e)
    normalized_xml.unlink(missing_ok=True)
    if video_root.exists():
        try:
            shutil.rmtree(video_root)
        except OSError as e:
            log.warning("could not reset video evidence: %s", e)
    normalized_video.unlink(missing_ok=True)


def run(cfg: ExtractorConfig) -> tuple[int, Path | None]:
    """Returns (exit_code, work_dir). work_dir is None only on fetch failure."""
    session = cfg.session if cfg.session is not None else build_session(cfg)

    label = cfg.execution_key or f"testrun={cfg.testrun_id} item={cfg.item_id}"
    log.info("fetching test results for %s", label)
    try:
        test_results = fetch_test_results(session, cfg)
    except AuthError as e:
        _alert_operator(str(e))
        return 2, None

    user_cache: dict[str, str | None] = {}
    tr0_raw = test_results[0] if test_results else {}
    executor_key = tr0_raw.get("userKey")
    tester_name: str | None = None
    if executor_key:
        tester_name = fetch_user_display_name(
            session, cfg.base_url, executor_key, user_cache)

    work_dir = _derive_workdir(cfg, test_results, tester_name=tester_name)
    work_dir.mkdir(parents=True, exist_ok=True)

    status_map: dict[int, str] = {}
    testrun0 = tr0_raw.get("testRun") or {}
    project_id = testrun0.get("projectId")
    if not project_id:
        _alert_operator(
            f"no projectId in testrun payload for {label} — status names "
            "will not resolve and workflow-consistency rules (R1-R6) will "
            "be bypassed. R0 will flag this in audit.json."
        )
    else:
        status_map = get_project_status_map(session, cfg.base_url, project_id)
        if status_map:
            log.debug("loaded %d status names for project %s",
                      len(status_map), project_id)
        else:
            _alert_operator(
                f"status-map fetch for project {project_id} returned "
                f"empty for {label} — workflow-consistency rules (R1-R6) "
                "will be bypassed. R0 will flag this in audit.json. "
                "Check your Jira PAT and retry if this is transient."
            )

    if cfg.include_test_case_weblinks:
        try:
            enrich_test_case_weblinks(
                session, cfg.base_url, test_results,
                set(status_map) if cfg.test_case_weblinks_all_statuses else issue_status_ids(status_map),
            )
        except (requests.RequestException, ValueError, AuthError) as exc:
            _alert_operator(f"could not retrieve test-case Weblinks: {exc}")
            return 3, work_dir

    metadata = build_metadata(test_results, session=session, base_url=cfg.base_url,
                              user_cache=user_cache, status_map=status_map)
    atomic_io.atomic_write_json(work_dir / "metadata.json", metadata)
    tr0 = (metadata.get("test_results") or [{}])[0]
    tester = tr0.get("executed_by_name") or tr0.get("executed_by") or "?"
    log.info("wrote metadata.json (%d test result(s), tester=%s)",
             len(metadata["test_results"]), tester)

    pdf_attachments = collect_pdf_attachments(test_results)
    image_attachments = collect_image_attachments(test_results)
    xml_attachments = collect_xml_attachments(test_results)
    video_attachments = collect_video_attachments(test_results)
    unsupported_attachments = collect_unsupported_attachments(test_results)
    screenshots_root = work_dir / "screenshots"

    _reconcile_stale_evidence(work_dir, pdf_attachments, xml_attachments)

    if (not pdf_attachments and not image_attachments
            and not xml_attachments and not video_attachments):
        if unsupported_attachments:
            formats = sorted({
                Path(str(item.get("name") or "")).suffix.lower()
                or "(unknown)"
                for item in unsupported_attachments
            })
            log.info(
                "%d attachment(s) exist, but none use an auditable "
                "PDF/image/XML/MP4 format (%s)",
                len(unsupported_attachments),
                ", ".join(formats),
            )
        else:
            log.info(
                "no PDF, image, XML, or MP4 attachments — nothing to download"
            )
        return 0, work_dir

    # Phase 1: download and locally validate every attachment before release.
    # The batch preserves independent results for the whole execution instead of one
    # serial upload/poll/release round trip per file. Per-attachment error
    # handling is unchanged from the one-at-a-time flow.
    quarantine = attachment_quarantine.QuarantineBatch(
        session=session,
        base_url=cfg.base_url,
        execution_key=cfg.execution_key or label,
        downloader=download_attachment,
    )
    pdf_tickets: list[
        tuple[int, str, Path, attachment_quarantine.QuarantineTicket]
    ] = []
    for att_id, name in pdf_attachments:
        storage_name = f"attachment_{att_id}.pdf"
        pdf_path = work_dir / storage_name
        log.info("downloading attachment %s -> %s", att_id, pdf_path.name)
        try:
            ticket = quarantine.add(
                attachment_id=att_id,
                attachment_name=name,
                destination=pdf_path,
            )
        except AuthError as e:
            _alert_operator(str(e))
            return 2, work_dir
        except AttachmentTooLargeError as e:
            log.warning("skipping PDF attachment %s: %s", att_id, e)
            continue
        except requests.RequestException as e:
            _alert_operator(
                f"downloading PDF attachment {att_id} failed after retries: "
                f"{type(e).__name__}: {e}")
            return 3, work_dir
        except attachment_quarantine.QuarantineError as e:
            _alert_operator(str(e))
            return 3, work_dir
        pdf_tickets.append((att_id, storage_name, pdf_path, ticket))

    step_dir = screenshots_root / "step_attachments"
    image_tickets: list[
        tuple[dict[str, Any], Path, attachment_quarantine.QuarantineTicket]
    ] = []
    if image_attachments:
        log.info("downloading %d image attachment(s) from steps/comments",
                 len(image_attachments))
        step_dir.mkdir(parents=True, exist_ok=True)
        for rec in image_attachments:
            path = _step_attachment_path(step_dir, rec)
            try:
                ticket = quarantine.add(
                    attachment_id=rec["id"],
                    attachment_name=rec.get("name") or path.name,
                    destination=path,
                )
            except AuthError as e:
                _alert_operator(str(e))
                return 2, work_dir
            except attachment_quarantine.QuarantineError as e:
                _alert_operator(str(e))
                return 3, work_dir
            except AttachmentTooLargeError as e:
                log.warning("skipping image attachment %s (%s): %s",
                            rec["id"], rec.get("source"), e)
                continue
            except requests.RequestException as e:
                log.warning("failed to download image %s (%s): %s",
                            rec["id"], rec.get("source"), e)
                continue
            image_tickets.append((rec, path, ticket))

    video_tickets: list[
        tuple[dict[str, Any], Path, attachment_quarantine.QuarantineTicket]
    ] = []
    if video_attachments:
        video_dir = work_dir / "video_evidence"
        video_dir.mkdir(parents=True, exist_ok=True)
        for rec in video_attachments:
            path = _video_attachment_path(video_dir, rec)
            try:
                ticket = quarantine.add(
                    attachment_id=rec["id"],
                    attachment_name=rec["name"],
                    destination=path,
                )
            except AuthError as e:
                _alert_operator(str(e))
                return 2, work_dir
            except AttachmentTooLargeError as e:
                log.warning("skipping video attachment %s: %s", rec["id"], e)
                continue
            except requests.RequestException as e:
                _alert_operator(
                    f"downloading video attachment {rec['id']} failed after "
                    f"retries: {type(e).__name__}: {e}")
                return 3, work_dir
            except attachment_quarantine.QuarantineError as e:
                _alert_operator(str(e))
                return 3, work_dir
            video_tickets.append((rec, path, ticket))

    xml_tickets: list[
        tuple[dict[str, Any], Path, attachment_quarantine.QuarantineTicket]
    ] = []
    if xml_attachments:
        xml_dir = work_dir / "xml_evidence"
        xml_dir.mkdir(parents=True, exist_ok=True)
        for rec in xml_attachments:
            path = _xml_attachment_path(xml_dir, rec)
            try:
                ticket = quarantine.add(
                    attachment_id=rec["id"],
                    attachment_name=rec["name"],
                    destination=path,
                )
            except AuthError as e:
                _alert_operator(str(e))
                return 2, work_dir
            except AttachmentTooLargeError as e:
                log.warning("skipping XML attachment %s: %s", rec["id"], e)
                continue
            except requests.RequestException as e:
                _alert_operator(
                    f"downloading XML attachment {rec['id']} failed after "
                    f"retries: {type(e).__name__}: {e}")
                return 3, work_dir
            except attachment_quarantine.QuarantineError as e:
                _alert_operator(str(e))
                return 3, work_dir
            xml_tickets.append((rec, path, ticket))

    # Phase 2: one poll loop over every pending upload. Any malware verdict
    # or scan timeout fails the whole execution closed (rc 3), as before.
    try:
        quarantine.release()
    except attachment_quarantine.QuarantineError as e:
        _alert_operator(str(e))
        return 3, work_dir

    # Phase 3: post-process the released evidence in the original order.
    for att_id, storage_name, pdf_path, _ticket in pdf_tickets:
        subdir = screenshots_root / Path(storage_name).stem
        log.info("splitting %s into %s/", pdf_path.name, subdir)
        try:
            pages = split_pdf_to_images(pdf_path, subdir)
        except (fitz.EmptyFileError, fitz.FileDataError) as exc:
            log.warning(
                "attachment %s is an empty or invalid PDF (%s); treating it "
                "as unavailable evidence so R12 can report the submission",
                att_id,
                exc,
            )
            continue
        log.info("  wrote %d page images", len(pages))

    if image_attachments:
        saved = 0
        for rec, path, ticket in image_tickets:
            released = ticket.result()
            if released.path != path:
                # The quarantine relabelled the file to its real image type;
                # keep the record consistent so later path derivation matches.
                rec.setdefault("original_name", rec.get("name"))
                rec["name"] = Path(
                    rec.get("name") or path.name
                ).with_suffix(released.path.suffix).name
                path = released.path
            if _resize_if_oversized(path):
                log.info("resized oversized step attachment %s (>%dpx)",
                         path.name, _MAX_STEP_IMAGE_EDGE_PX)
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            if size > _MAX_STEP_IMAGE_BYTES:
                log.warning(
                    "image %s (%.1f MB) exceeds %d MB cap — dropping",
                    path.name, size / 1_000_000, _MAX_STEP_IMAGE_BYTES // 1_000_000,
                )
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            saved += 1
        log.info("  saved %d image attachment(s) under %s",
                 saved, step_dir)

    if video_attachments:
        downloaded_videos: list[dict[str, Any]] = [
            {
                **rec,
                "format": path.suffix.lstrip(".").lower(),
                "path": path.relative_to(work_dir).as_posix(),
            }
            for rec, path, ticket in video_tickets
        ]
        atomic_io.atomic_write_json(
            work_dir / "video_evidence.json",
            {"videos": downloaded_videos},
        )
        log.info(
            "  prepared %d video attachment(s) for visual and audio analysis",
            len(downloaded_videos),
        )

    if xml_attachments:
        downloaded_xml: list[dict[str, Any]] = [
            {**rec, "path": str(path)}
            for rec, path, _ticket in xml_tickets
        ]

        screenshot_records = [
            {
                **rec,
                "original_name": rec.get("original_name") or rec.get("name"),
                "path": _step_attachment_path(
                    screenshots_root / "step_attachments", rec
                ).relative_to(work_dir).as_posix(),
            }
            for rec in image_attachments
            if _step_attachment_path(
                screenshots_root / "step_attachments", rec
            ).exists()
        ]
        try:
            xml_evidence.write_normalized_evidence(
                work_dir,
                downloaded_xml,
                screenshot_records,
            )
        except xml_evidence.XmlEvidenceError as e:
            _alert_operator(str(e))
            return 3, work_dir
        log.info("  normalized %d XML attachment(s)", len(downloaded_xml))

    log.info("done. output: %s", work_dir)
    return 0, work_dir


class _ArgusFormatter(logging.Formatter):
    """Clean output for demos: `[ARGUS] msg` for INFO, level-tagged for others."""

    def __init__(self, verbose: bool):
        super().__init__()
        self.verbose = verbose

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        if self.verbose:
            return f"[{record.name}] {record.levelname} {msg}"
        if record.levelno <= logging.INFO:
            return f"[ARGUS] {msg}"
        return f"[ARGUS] {record.levelname}: {msg}"


def configure_logging(verbose: bool = False) -> None:
    """Idempotent logger setup so the CLI and library use both work."""
    root = logging.getLogger("argus")
    if root.handlers:
        return
    handler: logging.Handler
    try:
        from rich.console import Console
        from rich.logging import RichHandler

        handler = RichHandler(
            console=Console(stderr=True),
            show_time=verbose,
            show_path=verbose,
            rich_tracebacks=True,
            markup=False,
        )
        handler.setFormatter(logging.Formatter(
            "[%(name)s] %(message)s" if verbose else "%(message)s"))
    except ImportError:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_ArgusFormatter(verbose))
    root.addHandler(handler)
    # Warnings describe degraded artefacts (docs/architecture.md) and must
    # reach the operator without -v; DEBUG/INFO stay opt-in.
    root.setLevel(logging.DEBUG if verbose else logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ARGUS extractor")
    parser.add_argument("--key", dest="execution_key",
                        help="execution key, e.g. QA-E123 (preferred)")
    parser.add_argument("--testrun-id", type=int, help="numeric test run ID (with --item-id)")
    parser.add_argument("--item-id", type=int, help="numeric test run item ID (with --testrun-id)")
    parser.add_argument("--config", type=Path, default=None,
                        help="path to config.toml (default: ./config.toml)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    if not args.execution_key and not (args.testrun_id and args.item_id):
        parser.error("provide --key OR both --testrun-id and --item-id")

    settings = argus_config.load(args.config)
    runtime.configure(settings)
    configure_logging(args.verbose or settings.logging.verbose)

    cfg = ExtractorConfig.from_settings(
        settings.extractor,
        execution_key=args.execution_key,
        testrun_id=args.testrun_id,
        item_id=args.item_id,
    )
    rc, _ = run(cfg)
    return rc


if __name__ == "__main__":
    sys.exit(main())
