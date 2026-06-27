"""test execution screenshot + metadata extractor.

"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF
import requests

import config as argus_config

log = logging.getLogger("argus.extract")

JIRA_BASE_URL = "https://tracker.exampleapp.com"

# Persistent testresult-id -> E-key cache. The mapping is immutable (a
# given testresult id always resolves to the same QA-E... key), so
# this never needs invalidation — we only ever add. Lets the roster show
# execution keys for unaudited rows without re-paying the per-id lookup
# on every cron tick. Stored next to the users cache.
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
    "assignedTo,userKey,comment,traceLinks,attachments,testRun(projectId),"
    "testScriptResults(id,testResultStatusId,executionDate,index,description,"
    "expectedResult,comment,traceLinks,attachments,stepAttachmentsMapping)"
)
# For E-key (QA-E171453) direct lookup — same shape as testrunitems path but single-shot.
EKEY_FIELDS = (
    "id,testResultStatusId,automated,executionDate,actualStartDate,actualEndDate,"
    "assignedTo,userKey,comment,traceLinks,attachments,"
    # `folderId` on testRun lets _run_single resolve the parent test-management
    # folder and scope the output dir the same way folder mode does —
    # without it, single-key audits land at output/<testrun_slug>/...
    # instead of output/<folder_slug>/<testrun_slug>/..., creating
    # orphan top-level dirs that don't merge with the folder report.
    "testRun(id,key,name,projectId,folderId),testCase(key,name),"
    "testScriptResults(id,testResultStatusId,executionDate,index,description,"
    "expectedResult,comment,traceLinks,attachments,stepAttachmentsMapping)"
)


_SLUG_RE = re.compile(r"[^\w.-]+")
# test-management appends "(cloned)" or "cloned" to names when a testrun is duplicated
# via the UI. It's noise in the folder path — strip it before slugging.
_CLONED_SUFFIX_RE = re.compile(r"\s*\(?\s*cloned\s*\)?\s*$", re.IGNORECASE)


def slugify(name: str, fallback: str = "unnamed") -> str:
    """Filesystem-safe slug: replace runs of non-[\\w.-] with `_`, trim edges."""
    if not name:
        return fallback
    slug = _SLUG_RE.sub("_", name).strip("_.")
    return slug or fallback


def _strip_cloned_suffix(name: str | None) -> str | None:
    """Strip a trailing '(cloned)' / 'cloned' from a test-management testrun name."""
    if not name:
        return name
    return _CLONED_SUFFIX_RE.sub("", name).rstrip()


@dataclass
class ExtractorConfig:
    out_dir: Path
    # Either (testrun_id, item_id) or execution_key must be set.
    testrun_id: int | None = None
    item_id: int | None = None
    execution_key: str | None = None  # e.g. "QA-E171453"
    base_url: str = JIRA_BASE_URL


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader — KEY=VALUE per line, # comments, optional quotes.
    Does NOT override vars already in the environment."""
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
    """Warn the user if a secret-bearing file is group/world readable."""
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH):
        log.warning(
            "%s is group/world accessible (mode=%o) — run `chmod 600 %s`",
            path, stat.S_IMODE(mode), path,
        )


def _load_tracker_pat() -> str | None:
    """Load a Tracker Personal Access Token.

    Lookup order:
      1. env var ARGUS_TRACKER_PAT (also populated from ./.env if present)
      2. ~/.argus/tracker.pat
    """
    dotenv_path = Path(__file__).parent / ".env"
    _load_dotenv(dotenv_path)
    if dotenv_path.exists():
        _warn_if_world_readable(dotenv_path)

    env = os.environ.get("ARGUS_TRACKER_PAT")
    if env:
        return env.strip()
    pat_file = Path.home() / ".exampleapp-mcp" / "tracker.pat"
    if pat_file.exists():
        _warn_if_world_readable(pat_file)
        return pat_file.read_text().strip() or None
    return None


def build_session() -> requests.Session:
    """Build an authenticated session for tracker.exampleapp.com.

    Auth is the Bearer PAT alone. Verified 2026-06-13: a PAT-only request
    (no SSO cookie) returns 200 across every test-management Scale endpoint we
    use (status map, folder tree, testrun search, items), while a
    cookie-only request returns 401. The SSO cookie this used to attach
    was dead weight — the PAT is the sole credential — so ARGUS has no
    SSO dependency for Tracker/test-management (matches the CTT-TrackerCommonLibrary
    PERSONAL_ACCESS_TOKEN pattern). For deployment the PAT moves to
    Secrets Manager; nothing else changes.
    """
    session = requests.Session()
    # Default to JSON Accept for the metadata call; binary downloads override per-request.
    # Note: no default Content-Type — we don't send bodies on GET, and setting it on GET
    # causes the test-management backend to 500 on attachment downloads.
    session.headers.update({
        "Accept": "application/json, text/plain, */*",
    })

    pat = _load_tracker_pat()
    if pat:
        session.headers["Authorization"] = f"Bearer {pat}"
    else:
        log.warning(
            "no Tracker PAT found — set ARGUS_TRACKER_PAT (or .env), "
            "or put token in ~/.argus/tracker.pat"
        )

    return session


class AuthError(Exception):
    """Raised when test-management auth clearly fails (401/403)."""


def _alert_operator(message: str) -> None:
    """Emit an operator-visible alert to stderr.

    The `argus` logger runs at ERROR level by default (see
    configure_logging) to keep INFO/WARNING chatter out of demos. That
    makes log.warning calls invisible for anything short of -v, which is
    the wrong channel for alerts an operator MUST see (auth-adjacent
    failures, silent degradations like the status-map fetch returning
    empty). This helper writes directly to stderr — consistent with how
    argus.py and batch.py surface user-facing messages — and also emits
    a log.warning for verbose-mode consumers so a single source of intent
    lands in both streams.

    Leading newline so the output lands on a fresh line when a spinner
    (progress.step in argus.py) is active and rewriting the same line
    with \\r. The spinner's next frame redrcloud cleanly below the alert.
    """
    sys.stderr.write(f"\n[argus] ALERT: {message}\n")
    sys.stderr.flush()
    log.warning(message)


def _raise_friendly(resp: requests.Response, context: str) -> None:
    """Turn auth-ish HTTP errors into a clean AuthError with a hint."""
    if resp.status_code in (401, 403):
        hint = (
            "auth rejected — check your Tracker PAT is current and not expired "
            "(https://tracker.exampleapp.com/secure/ViewProfile.jspa → Personal "
            "Access Tokens). Set it via ARGUS_TRACKER_PAT / .env / "
            "~/.argus/tracker.pat"
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
    """GET with backoff on 429 / 5xx. Honors Retry-After when present.

    Use this for listing endpoints (folder tree, testrun search, testrunitems,
    testresult lookups) where Tracker rate-limits aggressive callers.
    """
    import time
    delay = 1.0
    for attempt in range(1, max_attempts + 1):
        resp = session.get(url, params=params, timeout=timeout)
        if resp.status_code not in (429, 500, 502, 503, 504):
            return resp
        if attempt == max_attempts:
            return resp
        retry_after = resp.headers.get("Retry-After")
        wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
        log.warning("%s: HTTP %s — retrying in %.1fs (attempt %d/%d)",
                    context, resp.status_code, wait, attempt, max_attempts)
        time.sleep(wait)
        delay = min(delay * 2, 16.0)
    return resp  # unreachable; satisfies type checker


def fetch_test_results(session: requests.Session, cfg: ExtractorConfig) -> list[dict[str, Any]]:
    """Fetch test results. Prefers E-key direct lookup; falls back to testrun+item.

    Both paths route through `_get_with_retry` so a Tracker 429 burst
    during parallel batch extraction doesn't kill the key on a single
    throttled response. Observed in the Targeted_regression_-08_May_2026
    run: two keys failed in the first three of 74 because this function
    was using plain session.get() without backoff.
    """
    if cfg.execution_key:
        url = f"{cfg.base_url}/rest/tests/1.0/testresult/{cfg.execution_key}"
        resp = _get_with_retry(
            session, url,
            params={"fields": EKEY_FIELDS},
            context=f"fetching testresult {cfg.execution_key}",
        )
        _raise_friendly(resp, f"fetching testresult {cfg.execution_key}")
        # E-key endpoint returns a single object, not a list. Normalize to list shape.
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
) -> dict[str, Any] | None:
    """Walk the project's test-run folder tree to find a folder by exact name."""
    url = f"{base_url}/rest/tests/1.0/project/{project_id}/foldertree/testrun"
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


# Targeted-regression folders embed a date in their name. Observed variants
# in the corpus (output/ dir reflects the slugified forms; raw test-management names
# use spaces / dashes / parens):
#   "Targeted regression -19 May 2026(WEB)"          ← active sample
#   "Targeted regression -08 May 2026 (WEB)"
#   "Targeted regression - 14 May 2026 Web"
#   "Targeted regression - 22 May 2026 (WEB)"
#   "Targeted regression 06 May 2026 (WEB)"
#
# The DD-Mon-YYYY pattern always appears as three whitespace-separated
# tokens somewhere in the name. The regex is intentionally loose about
# what comes BEFORE/AFTER (dashes, parens, "WEB" / "Web") so future
# naming nudges don't silently break the auto-picker. Month names are
# matched case-insensitively against the english abbreviations test-management's
# UI emits.
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
    """Extract DD-Mon-YYYY from a folder name. None on no match / invalid date.

    Used by find_latest_active_folder to rank candidates. Pure function
    so it's unit-testable without touching network.
    """
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
) -> str | None:
    """Resolve a numeric test-management folderId to its display name.

    Walks the project's folder tree (same endpoint find_folder_by_name
    uses). Returns None when no folder with that id exists.

    Used by argus._run_single to scope single-key audits under the
    same per-folder out_dir that folder mode uses. Without this,
    `argus.py --key QA-E1` lands the output at
    output/<testrun_slug>/... instead of
    output/<folder_slug>/<testrun_slug>/..., creating duplicate
    directory trees that don't merge with the folder's _argus.html.
    """
    url = f"{base_url}/rest/tests/1.0/project/{project_id}/foldertree/testrun"
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
    """Walk the folder tree, return the most recent folder matching the prefix.

    Filtering rules:
      1. Folder name must START with `name_prefix` (case-insensitive). The
         match is a prefix, not a substring, so 'Targeted regression
         retro' would qualify but 'Old Targeted regression' would not.
      2. Folder name must contain a parseable DD-Mon-YYYY date.
      3. The parsed date must be no older than `max_age_days` from today.

    The folder with the LATEST date wins. Ties (same date in two folders,
    e.g. a misnamed duplicate) break alphabetically — deterministic but
    arbitrary; an operator who hits this case should rename one folder.

    Returns the same dict shape as find_folder_by_name (has 'id' and
    'name') so callers can pass the result straight into
    get_test_runs_in_folder. Returns None when:
      - The folder tree fetch fails (logged via _raise_friendly).
      - No folder matches the prefix.
      - All matches have stale dates (> max_age_days old).

    None is the cron-friendly answer: argus._run_auto_folder skips
    silently rather than auditing a stale folder. Operator can always
    fall back to `--folder "<exact name>"` if the auto-picker misses
    something legitimate.
    """
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
    # Sort by (date desc, name asc) so the newest date wins, with a
    # deterministic tiebreak.
    candidates.sort(key=lambda t: (-t[0].toordinal(), t[1]))
    return candidates[0][2]


def get_test_runs_in_folder(
    session: requests.Session,
    base_url: str,
    folder_id: str | int,
    project_id: str,
) -> list[dict[str, Any]]:
    """All non-archived test runs directly inside this folder."""
    url = f"{base_url}/rest/tests/1.0/testrun/search"
    params = {
        "fields": "id,key,name,folderId",
        "query": (
            f"testRun.projectId IN ({project_id}) AND "
            f"testRun.folderTreeId IN ({folder_id}) "
            "ORDER BY testRun.createdOn DESC"
        ),
        "maxResults": 100,
        "startAt": 0,
        "archived": "false",
    }
    resp = _get_with_retry(session, url, params=params,
                           context=f"searching test runs in folder {folder_id}")
    _raise_friendly(resp, f"searching test runs in folder {folder_id}")
    payload = resp.json()
    if isinstance(payload, dict) and "results" in payload:
        return payload["results"]
    return payload if isinstance(payload, list) else []


def get_testrun_items(
    session: requests.Session,
    base_url: str,
    testrun_id: str | int,
) -> list[dict[str, Any]]:
    """Items in a test run with their last result.

    Notes on quirks of this endpoint (tracker.exampleapp.com/rest/tests/1.0):
      - `fields` is REQUIRED; omitting it returns HTTP 400.
      - Sub-field selection like `$lastTestResult(id,testResultStatusId)`
        silently returns zero items. Ask for the plain field.
      - Response shape is `{"total": N, "testRunItems": [...]}`.
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
) -> dict[str, Any] | None:
    """Resolve a numeric testresult id -> {key, user_key, execution_date}.

    userKey here = the person who actually executed/submitted the last result
    (the 'executed by' field), not the assignee.

    `executionDate` (returned as `execution_date` in snake_case to match the
    rest of metadata.json) is the wall-clock timestamp test-management stamps when the
    tester clicks PASS/FAIL. Layer 1 of the credibility fix uses this to detect
    re-executions: if a tester corrects a previously-flagged result, the new
    executionDate differs from the one cached in metadata.json, and the
    enumerator (argus._run_folder_list) re-emits the key for fresh audit
    instead of skipping it as already-audited. Free to fetch — same endpoint,
    same response, just one extra field requested.
    """
    url = f"{base_url}/rest/tests/1.0/testresult/{testresult_id}"
    resp = _get_with_retry(
        session, url,
        params={"fields": "id,key,userKey,executionDate"},
        timeout=15,
        context=f"resolving testresult {testresult_id}",
    )
    if resp.status_code != 200:
        return None
    data = resp.json()
    return {
        "key": data.get("key"),
        "user_key": data.get("userKey"),
        "execution_date": data.get("executionDate"),
    }


def get_project_status_ids(
    session: requests.Session,
    base_url: str,
    project_id: str,
) -> dict[str, int]:
    """Map i18n short name (e.g. 'PASS', 'FAIL') -> numeric status id for this project.

    Status IDs are project-scoped on tracker.exampleapp.com — QA uses
    PASS=39 / FAIL=40 / BLOCKED=41 / IN_PROGRESS=38 / NOT_EXECUTED=37,
    which are not universal.
    """
    url = f"{base_url}/rest/tests/1.0/project/{project_id}/testresultstatus"
    resp = session.get(url, timeout=15)
    _raise_friendly(resp, f"fetching status ids for project {project_id}")
    out: dict[str, int] = {}
    for s in resp.json() or []:
        i18n = s.get("i18nKey", "")
        # e.g. "TEST_RESULT.STATUS.PASS" -> "PASS"
        short = i18n.rsplit(".", 1)[-1] if i18n else (s.get("name") or "").upper()
        if short and "id" in s:
            out[short] = s["id"]
    return out


def get_project_status_map(
    session: requests.Session,
    base_url: str,
    project_id: str | int,
) -> dict[int, str]:
    """Return {status_id: display_name} for a project, e.g. {39: 'Pass', 40: 'Fail'}.

    Used to resolve numeric testResultStatusId into human-readable names for
    metadata.json and the audit prompt. Best-effort: returns an empty dict on
    any error so callers can continue with raw numeric ids.
    """
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


def _strip_html(text: str | None) -> str:
    """Minimal HTML->text for tester comments.

    Testers submit `comment` via a rich-text editor; the field can contain
    <img>, <br>, <p>, inline styling. For the model we only want the prose,
    so we strip tags and collapse whitespace. Returns empty string if input
    is None or the stripped result is blank (e.g. comment was image-only).

    Order matters: real tags first, entity decode LAST. Decoding first would
    turn &lt;fake&gt; into <fake> which the tag stripper would then eat.
    """
    if not text:
        return ""
    s = text
    # 1. <br> variants become newlines so the text stays readable.
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    # 2. Drop inline images entirely — we don't ship them through the prompt.
    s = re.sub(r"<img[^>]*>", "", s, flags=re.I)
    # 3. Strip any remaining tags.
    s = re.sub(r"<[^>]+>", "", s)
    # 4. NOW decode common entities — any angle brackets that appear are
    #    real data, not markup.
    s = (s.replace("&nbsp;", " ")
         .replace("&amp;", "&")
         .replace("&lt;", "<")
         .replace("&gt;", ">")
         .replace("&quot;", '"'))
    # 5. Collapse runs of whitespace.
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _normalize_trace_links(raw: Any) -> list[str]:
    """Extract human-readable link strings from a traceLinks field.

    test-management returns traceLinks as None | [] | list of dicts with varying
    shapes (sometimes {'link': 'QA-BUG-123'}, sometimes
    {'key': 'QA-BUG-123', 'type': 'issue'}, etc.). We defensively
    pull whichever 'link'/'key'/'url'/'id' field exists and return a flat
    list of strings. Empty list if nothing extractable.
    """
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
        # Prefer keys most likely to contain a usable identifier.
        for key in ("link", "key", "url", "issueKey", "id"):
            val = item.get(key)
            if val:
                out.append(str(val))
                break
    return out


def fetch_user_display_name(
    session: requests.Session,
    base_url: str,
    user_key: str,
    cache: dict[str, str | None] | None = None,
) -> str | None:
    """Resolve a userKey (e.g. USER12345) to a displayName.

    Delegates to users.UserCache (~/.argus/users.json) — successful
    resolutions persist forever, failures retry with backoff
    (1m -> 5m -> 30m -> 2h -> 24h). Replaces the old per-session-dict
    cache that poisoned keys on transient 429/503.

    `cache` kept for back-compat: it's read first and mirrored on
    return for legacy callers, but the persistent cache is the
    source of truth.
    """
    if not user_key:
        return None

    # Honour the in-memory dict if the caller already populated it
    # (avoids re-loading the persistent cache for already-known keys).
    if cache is not None and user_key in cache:
        return cache[user_key]

    # Delegate to the persistent cache. Avoids module-load circular import
    # by importing here.
    import users
    persistent = users.get_default_cache()
    name = persistent.resolve(session, base_url, user_key)

    # Mirror the resolution into the caller's in-memory dict so any
    # legacy code relying on direct dict access still works.
    if cache is not None:
        cache[user_key] = name

    return name


def download_attachment(session: requests.Session, base_url: str, attachment_id: int, dest: Path) -> None:
    """Stream-download an attachment with retry on 429 / 5xx.

    `_get_with_retry` can't be reused directly because streaming downloads
    need the response object held open for `iter_content`, and the retry
    helper returns a buffered response. Instead we inline a minimal
    backoff loop that closes failed responses before retrying — cheap
    and keeps the streaming contract.

    Matches the retry envelope used by other Tracker endpoints: 4 attempts,
    1s → 16s exponential backoff, honours Retry-After when Tracker sends it.
    """
    import time
    url = f"{base_url}/rest/tests/1.0/attachment/{attachment_id}"
    max_attempts = 4
    delay = 1.0
    last_resp: requests.Response | None = None
    for attempt in range(1, max_attempts + 1):
        resp = session.get(url, stream=True, timeout=120)
        last_resp = resp
        # Success (or any non-retryable status) → break out and stream.
        if resp.status_code not in (429, 500, 502, 503, 504):
            break
        # Retryable failure: close the response, sleep, try again. On
        # the last attempt fall through with this response so
        # _raise_friendly sees the actual status code.
        if attempt == max_attempts:
            break
        retry_after = resp.headers.get("Retry-After")
        wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
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
        with dest.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if chunk:
                    f.write(chunk)


def split_pdf_to_images(
    pdf_path: Path,
    out_dir: Path,
    max_edge_px: int = 1568,
    fmt: str = "jpg",
    jpeg_quality: int = 95,
) -> list[Path]:
    """Render each PDF page to a raster image, capped at max_edge_px on the longer edge.

    Default output is JPEG — Claude accepts both PNG and JPEG, and for screenshots
    JPEG is ~5-10x smaller at quality 95 with minimal quality loss. The 1568px
    cap matches Anthropic's private image resize so we don't waste tokens.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    ext = "jpg" if fmt.lower() in {"jpg", "jpeg"} else "png"
    with fitz.open(pdf_path) as doc:
        for i, page in enumerate(doc, start=1):
            rect = page.rect
            long_edge = max(rect.width, rect.height)
            # PDF default is 72 DPI; compute zoom so long edge hits max_edge_px.
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


# --- Marketplace parser ----------------------------------------------------
# The test-management testrun name reliably encodes the marketplace as an
# all-caps token surrounded by separators (`_`, `-`, or whitespace).
# Examples seen in the May 15 corpus:
#   E2E_Flow_-_GMA_-_AU_Desktop_-_Chrome   -> AU
#   Additional_Non_Automated_cases_-_GMB_-_BR -> BR
#   Webview_E2E_-_AU                       -> AU
#   Arya_Regression_-_US_Mobile_-_Playback_-_Android -> US
#
# Why deterministic ground-truth on this matters: the model previously
# had to read URL bars in screenshots and guess the marketplace. That
# was unreliable (truncation, misreads) AND vulnerable to confusing
# parenthetical hints in step text ("(For BR)"). The testrun name has
# the answer, no ambiguity, no LLM call needed. R13 (a workflow rule)
# uses this stamp to fire HIGH when the URL TLDs visible in env_check
# don't match the assigned marketplace.

# ExampleApp storefronts. Order: longer TLDs first so a bare ".com" doesn't
# eat ".com.au" / ".com.br" matches when reverse-mapping later.
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
# Two-letter token boundary regex. The token must be flanked by a
# non-word boundary on each side so we don't match the "US" inside
# "Russia" (lol, defensive). GMA / GMB / E2E etc are stripped first
# because they collide with single-letter content.
_MP_TOKEN_RX = re.compile(
    r"(?:^|[\s_\-])("
    + "|".join(_MARKETPLACE_TLD.keys())
    + r")(?=[\s_\-]|$)"
)
# Group / surface markers we must NOT confuse with marketplaces. These
# get stripped before MP token search so "GMA" / "GMB" / "E2E" / etc
# are not treated as candidate MPs by accident.
_NOISE_TOKENS_RX = re.compile(
    r"(?:^|[\s_\-])(GMA|GMB|GMC|E2E|RNB|ALC|AYCL|RCDC|TYP|MP|QA|UI|UX)(?=[\s_\-]|$)",
    re.IGNORECASE,
)


def parse_marketplace_from_testrun_name(name: str | None) -> str | None:
    """Extract the marketplace token (e.g. "AU", "BR") from a test-management
    testrun name. Returns None when no recognised MP token is present
    (very rare on the QA corpus, but we tolerate it).

    Strategy: first scrub group/surface markers (GMA, GMB, E2E, ...)
    that share the two-letter shape, then look for a remaining MP
    token bounded by separators. Returns the FIRST match — testrun
    names always have the MP exactly once.

    Idempotent / pure / safe — no I/O, no API calls. Tested via
    `test_extractor_units::test_parse_marketplace_*`.
    """
    if not name:
        return None
    # Strip noise tokens by replacing with a space so position is
    # preserved (helps regex flanking checks downstream).
    cleaned = _NOISE_TOKENS_RX.sub(" ", name)
    m = _MP_TOKEN_RX.search(cleaned)
    if not m:
        return None
    return m.group(1).upper()


# Surface / browser / device tokens parsed out of testrun names.
# Examples that this parser handles:
#   "E2E Flow - GMA - AU Desktop - (Chrome)"            -> Desktop / Chrome / None
#   "E2E Flow - GMA - AU Mobile - (Edge - Samsung)"     -> Mobile  / Edge / Samsung (Android-flavoured)
#   "E2E Flow - GMB - IT Mobile - (Firefox - iphone)"   -> Mobile  / Firefox / iPhone (iOS)
#   "Arya Regression - US Mobile - Playback - Android"  -> Mobile  / None / Android
#   "Arya Regression - US Mobile - Playback - iOS"      -> Mobile  / None / iOS
#   "Webview E2E - JP (cloned) (cloned)"                -> Webview / None / None
#   "Additional Non Automated cases - GMA - AU"         -> None    / None / None
#
# Returned values are normalised constants, not free text. A None means
# we couldn't determine the value from the testrun name — callers must
# handle that case (do nothing rather than guess).
_SURFACE_RX = re.compile(
    r"(?:^|[\s_\-])(Desktop|Mobile|Webview|Tablet)(?=[\s_\-(]|$)",
    re.IGNORECASE,
)
_BROWSER_RX = re.compile(
    r"(?:^|[\s_\-(])(Chrome|Firefox|Edge|Safari|Samsung\s+Internet)"
    r"(?=[\s_\-)]|$)",
    re.IGNORECASE,
)
# Device tokens always live INSIDE the trailing "(... - <device>)" group.
# Match "Samsung", "iphone"/"iPhone", "ipad"/"iPad", "Pixel", "Android"
# (a generic device class, not a specific phone), "iOS" (same).
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
    """Return canonical browser name or None when unspecified.

    Note: browser is reported even on non-Desktop runs because mobile
    web tests run on a specific browser too (e.g. mobile Chrome).
    """
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
    """Look up the .exampleapp.<tld> suffix for a marketplace code.

    Returns None if the MP isn't recognised. Pure helper, no I/O.
    """
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
) -> list["coverage.CoverageItem"]:
    """Flatten every testrun's items (executed AND unexecuted) into
    coverage.CoverageItem records for R15.

    items_by_run = pre-fetched {run_id -> [items]} from the caller's
    enumeration; absent => fresh per-run fetch (tests/ad-hoc scripts).
    Zero new Tracker calls when items_by_run is supplied beyond optional
    user-display-name lookups (cached after first tick).

    status_map: {numeric_id -> display_name} — R15 compares names so
    rule logic isn't QA-id specific.
    """
    # Late import: coverage imports nothing from extractor, so this
    # one-way dep stays clean. Imported here (not at module top) to
    # avoid import-cycle risk when extractor is loaded as part of the
    # cron pipeline before coverage.py is needed.
    import coverage  # noqa: WPS433 — intentional local import

    # E-key cache: resolve each executed testresult's id -> QA-E...
    # key once and persist it (mapping is immutable). Lets the roster
    # show/link execution keys for unaudited rows without re-paying the
    # lookup every cron tick.
    ekey_cache = _EKeyCache()
    _ekey_dirty = False

    out: list[coverage.CoverageItem] = []
    for run in runs:
        run_id = run.get("id")
        if not run_id:
            continue
        run_key = run.get("key") or ""
        run_name = run.get("name")
        marketplace = parse_marketplace_from_testrun_name(run_name)

        # Prefer the pre-fetched items list when available — that's
        # how argus.py threads in the data already pulled during
        # enumeration. Fall back to a fresh fetch only when the
        # caller didn't supply one (tests, ad-hoc scripts).
        if items_by_run is not None and run_id in items_by_run:
            items = items_by_run[run_id]
        else:
            try:
                items = get_testrun_items(session, base_url, run_id)
            except (requests.HTTPError, requests.RequestException) as e:
                # Best-effort: log and continue. R15 with partial data
                # is better than R15 silently absent.
                log.warning(
                    "build_coverage_items: skipping run %s (%s)",
                    run_key or run_id, e,
                )
                continue

        for item in items:
            last = item.get("$lastTestResult") or {}
            status_id = last.get("testResultStatusId")
            status_name = (status_map.get(status_id)
                           if isinstance(status_id, int)
                           else None) or "Unknown"
            test_case = (last.get("testCase") or {})
            tc_key = test_case.get("key") or ""
            tc_name = test_case.get("name") or None
            # `$lastTestResult` carries `userKey` (executed_by /
            # current owner). Prefer `assignedTo` when present (some
            # test-management versions include it); fall back to `userKey`. For
            # unexecuted items the userKey is typically the assignee
            # so R15c still attributes correctly.
            assigned_to = last.get("assignedTo") or last.get("userKey")
            # Reuse the persistent user cache so re-runs don't re-pay
            # the resolution cost. fetch_user_display_name already
            # tolerates None / empty userKey.
            assigned_to_name = (
                fetch_user_display_name(
                    session, base_url, assigned_to, user_cache)
                if assigned_to else None
            )
            # executed_by = who ACTUALLY ran it. Only an executed result
            # carries a userKey; Not-Executed items have none. This is
            # the source of truth for the per-tester roster, kept
            # SEPARATE from assigned_to (the planned owner) — a case can
            # be assigned to X and executed by Y, and the roster must
            # credit Y. Never fall back to assignedTo here.
            executed_by = last.get("userKey")
            executed_by_name = (
                fetch_user_display_name(
                    session, base_url, executed_by, user_cache)
                if executed_by else None
            )
            # E-key resolution for EXECUTED items (those with a userKey).
            # Every executed testresult — including Blocked / In Progress
            # — has a resolvable QA-E... key; only truly Not-Executed
            # items (no userKey) lack one. Cache-first so the per-id lookup
            # is paid once, then persisted. OPT-IN (resolve_e_keys): this is
            # the only path that makes per-item HTTP calls, so it's off by
            # default — keeps build_coverage_items HTTP-free when items are
            # pre-fed (R15 coverage doesn't need E-keys; only the roster
            # renderer does). argus.py enables it for the folder run.
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
            out.append(coverage.CoverageItem(
                test_case_key=tc_key,
                testrun_key=run_key,
                marketplace=marketplace,
                status=status_name,
                assigned_to=assigned_to,
                assigned_to_name=assigned_to_name,
                executed_by=executed_by,
                executed_by_name=executed_by_name,
                testrun_name=run_name,
                test_case_name=tc_name,
                e_key=e_key,
            ))
    # Persist any newly resolved E-keys for the next run.
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
        # Returns None when the id is absent or isn't in the map. Callers
        # must tolerate None — downstream prompt/render skips missing values.
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
        top_comment = _strip_html(tr.get("comment"))
        top_trace_links = _normalize_trace_links(tr.get("traceLinks"))
        # Marketplace ground-truth from the testrun name. Recorded so
        # workflow_rules.R13 can deterministically check that the URLs
        # in screenshots match the assigned marketplace, instead of
        # making the LLM guess. Source of truth is the testrun name
        # because that's what test-management / QA-lead actually controls; the
        # parenthetical hints inside step description ("(For BR)") are
        # title-selection annotations and do NOT define the test's
        # scope.
        testrun = tr.get("testRun") or {}
        testrun_name = testrun.get("name")
        marketplace = parse_marketplace_from_testrun_name(testrun_name)
        cleaned.append({
            "id": tr.get("id"),
            "status_id": top_status_id,
            "status": resolve_status(top_status_id),
            "test_case_key": testcase.get("key"),
            "test_case_name": testcase.get("name"),
            "testrun_key": testrun.get("key"),  # e.g. "QA-C17608"
            "testrun_name": testrun_name,
            "marketplace": marketplace,
            "marketplace_tld": marketplace_tld(marketplace),
            # Browser / device / surface stamps (parsed from testrun_name).
            # Null when the testrun-name format doesn't carry the info
            # (Webview runs, Additional-Non-Automated bundles). The
            # auditor uses these as ground truth so the model can flag
            # screenshots that show a different browser / device than
            # what the test was scoped to.
            "expected_surface": parse_surface_from_testrun_name(testrun_name),
            "expected_browser": parse_browser_from_testrun_name(testrun_name),
            "expected_device": parse_device_from_testrun_name(testrun_name),
            "executed_by": executed_by_key,
            "executed_by_name": resolve(executed_by_key),
            "assigned_to": assigned_to_key,
            "assigned_to_name": resolve(assigned_to_key),
            "execution_date": tr.get("executionDate"),
            "actual_start": tr.get("actualStartDate"),
            "actual_end": tr.get("actualEndDate"),
            # Empty string when the comment was image-only or absent.
            "comment": top_comment,
            "trace_links": top_trace_links,
            "attachments": [
                {"id": a.get("id"), "name": a.get("name"), "size": a.get("fileSize")}
                for a in (tr.get("attachments") or [])
            ],
            "steps": [
                {
                    "index": s.get("index"),
                    "description": s.get("description"),
                    "expected_result": s.get("expectedResult"),
                    "status_id": s.get("testResultStatusId"),
                    "status": resolve_status(s.get("testResultStatusId")),
                    "execution_date": s.get("executionDate"),
                    "comment": _strip_html(s.get("comment")),
                    "trace_links": _normalize_trace_links(s.get("traceLinks")),
                    "attachments": [
                        {"id": a.get("id"), "name": a.get("name")}
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
    """Gather PDF attachments from both places testers put them:
      1. testResult.attachments           — the 'right' spot
      2. testResult.testScriptResults[*].attachments — some testers attach
         the PDF to a step (usually step 0) instead. Same PDF, different
         field; extraction works identically.
    Dedupes by attachment id.
    """
    out: list[tuple[int, str]] = []
    seen: set[int] = set()

    def _add(attachments: list[dict[str, Any]] | None) -> None:
        for a in attachments or []:
            att_id = a.get("id")
            if att_id is None or att_id in seen:
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


# Image file extensions we accept as audit input. Keep the list tight — we
# don't want to ship SVGs, TIFFs, etc. that Claude may not handle.
_IMAGE_EXTS: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".gif", ".webp")

# Matches inline image references inside HTML comment fields. test-management's
# rich-text editor produces tags like:
#   <img src="../rest/tests/1.0/attachment/image/920428" style="..." class="...">
# The attachment id is what we want — the src itself is a relative path that
# we translate into the full attachment-download URL elsewhere.
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


def collect_image_attachments(
    test_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Gather every image attachment testers provided, with step attribution.

    Four sources, each dedupes into the same id-space:

      1. Top-level `attachments` that are images (not PDFs).
      2. Images referenced inline inside the top-level `comment` HTML
         (tester pasted screenshots into the overall remarks field).
      3. Per-step `attachments` that are images.
      4. Images referenced inline inside each step's `comment` HTML.

    Returns a list of dicts:
      {
        "id": <attachment id>,
        "name": <filename or synthetic>,
        "step_index": <int or None>,  # None for top-level
        "source": "top_attachment" | "top_comment_inline"
                 | "step_attachment" | "step_comment_inline",
      }

    Dedup key: id + step_index. A top-level and a step inline referencing the
    same id produce two records (different step attribution); a step's
    attachment listed twice in the API response produces one.
    """
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
        # (1) top-level image attachments
        for a in tr.get("attachments") or []:
            att_id = a.get("id")
            nm = a.get("name")
            if isinstance(att_id, int) and _is_image_name(nm):
                _add(att_id, nm, None, "top_attachment")

        # (2) top-level inline comment images (no original filename available;
        # we fabricate one so downloads write to a sensible path).
        for img_id in _extract_inline_image_ids(tr.get("comment")):
            _add(img_id, f"inline_{img_id}.png", None, "top_comment_inline")

        # (3) + (4) per-step
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


# Cap for step/comment images. Larger files (typically 4K screenshots
# saved as PNG) get skipped with a warning rather than risking LLM Runtime
# payload-size errors that would abort the whole audit.
_MAX_STEP_IMAGE_BYTES = 5_000_000  # 5 MB

# Maximum pixel dimension (long edge) we ship to LLM Runtime per image.
# Claude rejects anything > 2000px in multi-image requests with a
# ValidationException:
#   "messages.0.content.N.image.source.base64.data: At least one of the
#    image dimensions exceed max allowed size for many-image requests:
#    2000 pixels"
# 1568 matches the PDF splitter's max_edge_px and leaves head-room under
# the 2000px ceiling. Pasted 4K phone screenshots (3840×2160) get
# resized to 1568×882 before we ship them to the model.
_MAX_STEP_IMAGE_EDGE_PX = 1568


def _resize_if_oversized(
    path: Path,
    max_edge: int = _MAX_STEP_IMAGE_EDGE_PX,
) -> bool:
    """Resize an image file in-place if either dimension > `max_edge`.

    Returns True if a resize was performed, False otherwise (already
    within bounds, unsupported format, corrupt file, or any other
    failure). Fails open: on exception we log and leave the file
    untouched — the dimension cap is a guard, not a gate, and a
    later LLM Runtime call will surface the real issue if the file
    can't be saved.

    Two fitz APIs, one for each job:
      - `fitz.Pixmap(path)` reports actual PIXEL dimensions. Used for
        the "is this oversized?" check so Retina screenshots with
        144 DPI metadata can't sneak through by having fitz.open().rect
        report half-size POINTS.
      - `fitz.open(path)[0].get_pixmap(matrix=Matrix(zoom, zoom))`
        renders a scaled version. The zoom multiplies the page's
        point dimensions, and fitz's point<->pixel conversion honours
        the embedded DPI, so `zoom = max_edge / point_long` produces
        a pixmap with pixel long-edge == max_edge regardless of source DPI.

    The two APIs have to coexist because fitz doesn't expose a
    direct Pixmap-level resize.
    """
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
            # Zoom is point-based so it accounts for any embedded DPI:
            # rendered_pixels = zoom * page_points = max_edge.
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
    """Build the on-disk path for a step/comment image download.

    Encodes the step attribution in the filename so the auditor can recover
    which step an image belongs to without a metadata sidecar. Shape:
      step_03_<id>_<original-name>         (per-step image)
      top_<id>_<original-name>             (top-level image)
    The id is in the name so collisions on original-name don't clobber.
    """
    step_idx = rec.get("step_index")
    prefix = "top" if step_idx is None else f"step_{int(step_idx):03d}"
    # Sanitize the original name — strip path separators and spaces.
    raw_name = rec.get("name") or f"attachment_{rec['id']}.png"
    safe = _SLUG_RE.sub("_", raw_name).strip("_.") or f"image_{rec['id']}.png"
    return step_dir / f"{prefix}_{rec['id']}_{safe}"


def _derive_workdir(
    cfg: ExtractorConfig,
    test_results: list[dict[str, Any]],
    tester_name: str | None = None,
) -> Path:
    """Pick output path from the test run name when we can see it.

    Layout: <out_dir>/<testrun_slug>/<tester_slug>/<execution_key_or_fallback>
    Tester defaults to 'unknown' when unresolved. Falls back to flatter layouts
    when testrun name isn't in the response (old testrun+item path).
    """
    tr0 = test_results[0] if test_results else {}
    testrun = tr0.get("testRun") or {}
    testrun_name = _strip_cloned_suffix(testrun.get("name"))
    leaf = cfg.execution_key or f"{cfg.testrun_id}_{cfg.item_id}"
    tester_slug = slugify(tester_name, fallback="unknown") if tester_name else "unknown"
    if testrun_name:
        return cfg.out_dir / slugify(testrun_name) / tester_slug / leaf
    return cfg.out_dir / tester_slug / leaf


def run(cfg: ExtractorConfig) -> tuple[int, Path | None]:
    """Returns (exit_code, work_dir). work_dir is None only on fetch failure."""
    session = build_session()

    label = cfg.execution_key or f"testrun={cfg.testrun_id} item={cfg.item_id}"
    log.info("fetching test results for %s", label)
    try:
        test_results = fetch_test_results(session, cfg)
    except AuthError as e:
        log.error("%s", e)
        return 2, None

    # Resolve the tester display name *before* computing work_dir so the path
    # can include a per-tester folder. Reuses the same cache as build_metadata
    # below so we don't pay twice.
    user_cache: dict[str, str | None] = {}
    tr0_raw = test_results[0] if test_results else {}
    executor_key = tr0_raw.get("userKey")
    tester_name: str | None = None
    if executor_key:
        tester_name = fetch_user_display_name(
            session, cfg.base_url, executor_key, user_cache)

    work_dir = _derive_workdir(cfg, test_results, tester_name=tester_name)
    work_dir.mkdir(parents=True, exist_ok=True)

    # Fetch the project's status id -> name map (best-effort). Used below
    # to resolve testResultStatusId into human-readable names in metadata.
    # We look up projectId from the response itself (E-key path sets it
    # under testRun.projectId; testrun+item path now includes testRun too).
    # Alert the operator on each failure path — without the status map,
    # workflow_rules.R1-R6 short-circuit silently (they check resolved
    # names like "Pass"/"Fail") and the audit looks falsely clean. The R0
    # meta-rule will also flag this post-hoc in audit.json, but an
    # operator watching the extractor run should see it immediately so
    # they can refresh auth (mwinit -f) and retry before the audit stage
    # burns LLM Runtime calls on partial metadata.
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
                "Refresh auth (mwinit -f) and retry if this is transient."
            )

    metadata = build_metadata(test_results, session=session, base_url=cfg.base_url,
                              user_cache=user_cache, status_map=status_map)
    (work_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))
    tr0 = (metadata.get("test_results") or [{}])[0]
    tester = tr0.get("executed_by_name") or tr0.get("executed_by") or "?"
    log.info("wrote metadata.json (%d test result(s), tester=%s)",
             len(metadata["test_results"]), tester)

    pdf_attachments = collect_pdf_attachments(test_results)
    image_attachments = collect_image_attachments(test_results)
    screenshots_root = work_dir / "screenshots"

    if not pdf_attachments and not image_attachments:
        log.info("no PDF or image attachments — nothing to download")
        return 0, work_dir

    # --- PDF attachments (session exports) ----------------------------------
    # When a tester attaches multiple PDFs (split a long execution across
    # 2+ exports, common for >50-step tests), the test-management exporter often
    # gives them IDENTICAL filenames (e.g. both called
    # `Session_20260608_111147_Export.pdf`). Without disambiguation,
    # the second download overwrites the first on disk AND overwrites
    # the page images in the shared screenshots/<stem>/ subdir — the
    # auditor then sees half the evidence and flags a fully-evidenced
    # test as under-evidenced. Observed on QA-E185649 (S Swetha,
    # 2026-06-08): 2 PDFs (ids 973907, 973909) collapsed to 1 with 4
    # pages, audit returned a false-positive "fail / under-evidenced"
    # verdict.
    #
    # Fix: when names collide, suffix later occurrences with the
    # attachment id. First occurrence keeps the original name to
    # preserve the legacy on-disk shape for audits that have only one
    # PDF (the common case). Each PDF gets its own screenshots
    # sub-dir so the page images don't fight for filenames either.
    seen_names: set[str] = set()
    for att_id, name in pdf_attachments:
        if name in seen_names:
            stem = Path(name).stem
            ext = Path(name).suffix
            disambiguated = f"{stem}__{att_id}{ext}"
        else:
            disambiguated = name
            seen_names.add(name)
        pdf_path = work_dir / disambiguated
        log.info("downloading attachment %s -> %s", att_id, pdf_path.name)
        try:
            download_attachment(session, cfg.base_url, att_id, pdf_path)
        except AuthError as e:
            log.error("%s", e)
            return 2, work_dir

        subdir = screenshots_root / Path(disambiguated).stem
        log.info("splitting %s into %s/", pdf_path.name, subdir)
        pages = split_pdf_to_images(pdf_path, subdir)
        log.info("  wrote %d page images", len(pages))

    # --- Step / comment image attachments (opportunistic accuracy-boost) ----
    # Whatever testers attached per-step or pasted into comments, pulled in
    # alongside the session PDF. Saved under screenshots/step_attachments/
    # with a filename prefix encoding the step index (or "top_") so the
    # auditor can recover step attribution at audit time.
    if image_attachments:
        log.info("downloading %d image attachment(s) from steps/comments",
                 len(image_attachments))
        step_dir = screenshots_root / "step_attachments"
        step_dir.mkdir(parents=True, exist_ok=True)
        saved = 0
        for rec in image_attachments:
            path = _step_attachment_path(step_dir, rec)
            try:
                download_attachment(session, cfg.base_url, rec["id"], path)
            except AuthError as e:
                log.error("%s", e)
                return 2, work_dir
            except requests.RequestException as e:
                log.warning("failed to download image %s (%s): %s",
                            rec["id"], rec.get("source"), e)
                continue
            # Dimension cap: LLM Runtime multi-image requests reject images
            # with either dimension > 2000px. Pasted 4K screenshots blow
            # past this — resize in place before the byte-cap check so
            # the resized version (~300 KB JPEG) is what we keep.
            if _resize_if_oversized(path):
                log.info("resized oversized step attachment %s (>%dpx)",
                         path.name, _MAX_STEP_IMAGE_EDGE_PX)
            # Size cap: 4K PNGs can be 5-10 MB each and blow LLM Runtime payloads.
            # Skip oversize files with a warning rather than silently sending
            # something that'll fail downstream.
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

    log.info("done. output: %s", work_dir)
    return 0, work_dir


class _ARGUSFormatter(logging.Formatter):
    """Clean output for demos: `[argus] msg` for INFO, level-tagged for others.

    In verbose mode (DEBUG enabled), fall back to the detailed module-qualified
    format so we still know which module logged what.
    """

    def __init__(self, verbose: bool):
        super().__init__()
        self.verbose = verbose

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        if self.verbose:
            return f"[{record.name}] {record.levelname} {msg}"
        if record.levelno <= logging.INFO:
            return f"[argus] {msg}"
        return f"[argus] {record.levelname}: {msg}"


def configure_logging(verbose: bool = False) -> None:
    """Idempotent logger setup so the CLI and library use both work."""
    root = logging.getLogger("argus")
    if root.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_ARGUSFormatter(verbose))
    root.addHandler(handler)
    # Quiet by default — audit.md is the real output. -v brings logs back.
    root.setLevel(logging.DEBUG if verbose else logging.ERROR)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ARGUS extractor")
    parser.add_argument("--key", dest="execution_key",
                        help="execution key, e.g. QA-E171453 (preferred)")
    parser.add_argument("--testrun-id", type=int, help="numeric test run ID (with --item-id)")
    parser.add_argument("--item-id", type=int, help="numeric test run item ID (with --testrun-id)")
    parser.add_argument("--config", type=Path, default=None,
                        help="path to config.toml (default: ./config.toml)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    if not args.execution_key and not (args.testrun_id and args.item_id):
        parser.error("provide --key OR both --testrun-id and --item-id")

    settings = argus_config.load(args.config)
    configure_logging(args.verbose or settings.logging.verbose)

    cfg = ExtractorConfig(
        execution_key=args.execution_key,
        testrun_id=args.testrun_id,
        item_id=args.item_id,
        out_dir=settings.extractor.out_dir,
        base_url=settings.extractor.base_url,
    )
    rc, _ = run(cfg)
    return rc


if __name__ == "__main__":
    sys.exit(main())
