"""Read-only projection of a report into versioned human-review documents."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
from pathlib import Path
from typing import Any

from argus.core import exec_paths
from argus.domain.finding_identity import finding_identity
from argus.reporting import report


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str,
    ).encode()).hexdigest()


def review_revision(detail: dict) -> str:
    """Bind decisions to reviewed content, independent of publication metadata.

    This also works with immutable documents published before review revisions
    were introduced, so their saved decisions can be compared without rewriting
    history. Document revisions remain separate for loading and stale saves.
    """
    content = {
        key: detail.get(key)
        for key in (
            "key", "executor", "run", "verdict", "analysis", "ready",
            "steps", "automated_review_status",
        )
    }
    # Finding order and transient model fields are not review inputs. Include
    # the saved decision ID as well as the rule (not part of finding_identity).
    content["findings"] = sorted({
        (str(f.get("id") or finding_identity(f)),
         str(f.get("rule") or ""), finding_identity(f))
        for f in detail.get("findings", [])
    })
    content["evidence"] = sorted({
        (str(e.get("name") or ""), str(e.get("sha256") or e.get("url") or ""),
         str(e.get("type") or ""))
        for e in detail.get("evidence", [])
    })
    return digest(content)


def enabled(team: str) -> bool:
    # Available to any registered team explicitly enabled via ARGUS_REVIEW_TEAMS.
    from argus.operations.teams import resolve_team

    return (
        resolve_team(team) is not None
        and team in os.environ.get("ARGUS_REVIEW_TEAMS", "").split(",")
    )


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def with_review_link(html: str) -> str:
    """Place the entry point in the report sidebar after successful publication."""
    html = re.sub(r'<p\b[^>]*\bdata-argus-review-link\b[^>]*>.*?</p>', "", html, flags=re.S)
    html = re.sub(r'<a\b[^>]*\bdata-argus-review-link\b[^>]*>.*?</a>', "", html, flags=re.S)
    link = (
        '<a class="nav-link" href="review" data-argus-review-link>'
        '<span class="nav-icon" aria-hidden="true">☑</span>Review cases</a>'
    )
    pattern = r'(<aside\b[^>]*class="[^"]*\bside-navigation\b[^"]*"[^>]*>.*?<nav\b[^>]*>)(.*?)(</nav>)'

    def insert(match: re.Match[str]) -> str:
        items = match[2]
        divider = items.find('<div class="nav-divider"')
        position = divider if divider >= 0 else len(items)
        return match[1] + items[:position] + link + items[position:] + match[3]

    result, count = re.subn(pattern, insert, html, count=1, flags=re.S)
    if not count:
        raise ValueError("Report sidebar is unavailable.")
    return result


def build_documents(folder: Path, team: str) -> tuple[dict, dict, list]:
    """Return manifest, details, and content-addressed evidence assets.

    Unknown coverage rows remain explicit blockers. This projection never
    enriches or rewrites the source audit and makes no network/model calls.
    """
    from argus.reporting.publisher import ReportAsset

    latest = read_json(folder / "_latest_run.json")
    scoped = report.is_complete_latest_run_manifest(latest)
    scope = set(latest.get("scope_keys", [])) if scoped else None
    paths = {p.parent.name: p for p in exec_paths.iter_latest_audit_jsons(folder)}
    if scope is not None:
        paths = {key: path for key, path in paths.items() if key in scope}
    coverage = read_json(folder / "_coverage.json")
    roster = {
        row["e_key"]: row
        for row in coverage.get("executions", [])
        if row.get("e_key") and (scope is None or row["e_key"] in scope)
    }
    keys = sorted(set(paths) | set(roster) | (scope or set()))
    assets = []
    details = {}
    items = []
    allowed_media = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".pdf",
                     ".mp4", ".webm", ".mov", ".wav", ".mp3", ".m4a", ".ogg"}
    for key in keys:
        audit_path = paths.get(key)
        execution = audit_path.parent if audit_path else exec_paths.find_execution_dir(
            folder, key, skip_replay=True,
        )
        audit = read_json(audit_path) if audit_path else {}
        metadata = read_json(execution / "metadata.json") if execution else {}
        results = metadata.get("test_results") or []
        tr = results[0] if results else roster.get(key, {})
        from argus.pipeline.analysis_status import derive_status

        analysis = derive_status(audit) if audit else "incomplete"
        if key in latest.get("failed_keys", []):
            analysis = "incomplete"
        evidence = []
        if execution:
            for path in sorted(execution.rglob("*")):
                if (
                    not path.is_file() or path.suffix.lower() not in allowed_media
                    or exec_paths.is_excluded(path)
                    or "quarantine" in str(path.relative_to(execution)).lower()
                    or not path.resolve().is_relative_to(folder.resolve())
                ):
                    continue
                with path.open("rb") as handle:
                    sha = hashlib.file_digest(handle, "sha256").hexdigest()
                relative = path.relative_to(folder).as_posix()
                content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                assets.append(ReportAsset(path, relative, content_type, sha))
                evidence.append({
                    "name": path.relative_to(execution).as_posix(),
                    "url": f"assets/content/{sha}{path.suffix.lower()}",
                    "sha256": sha, "type": content_type,
                })
        findings = [
            {**f, "id": finding_identity(f)}
            for f in audit.get("findings", []) if isinstance(f, dict)
        ]
        # Exact duplicates share a decision; distinct findings stay distinct.
        findings = list({f["id"]: f for f in findings}.values())
        steps = [{
            "index": step.get("index"),
            "description": report._plain_step_description(step.get("description")),
            "expected": report._plain_step_description(
                step.get("expected_result") or step.get("expectedResult")
                or step.get("expected") or step.get("expectedResults"),
            ),
            "status": step.get("status"),
        } for step in tr.get("steps", []) if isinstance(step, dict)]
        item = {
            "key": key, "executor": tr.get("executed_by"),
            "executor_name": tr.get("executed_by_name"),
            "test_case": tr.get("test_case_name") or tr.get("test_case_key") or key,
            "run": tr.get("testrun_key"), "run_name": tr.get("testrun_name"),
            "verdict": audit.get("overall_verdict"),
            "analysis": analysis, "finding_count": len(findings),
            "ready": bool(audit and analysis == "complete" and evidence and steps),
        }
        detail = {
            **item, "steps": steps, "findings": findings, "evidence": evidence,
            "analysis_diagnostic": audit.get("analysis_diagnostic"),
            "automated_review_status": audit.get("review_status", "none"),
        }
        revision = digest({"audit": audit, "metadata": metadata, "detail": detail})
        item["revision"] = detail["revision"] = revision
        item["review_revision"] = detail["review_revision"] = review_revision(detail)
        details[key] = detail
        items.append(item)
    # Legacy coverage omitted never-attempted executions. Never imply these
    # have been reviewed, invent identities, or drop them from the denominator.
    is_execution_only = isinstance(latest.get("execution_key"), str)
    total = len(items) if is_execution_only else max(len(items), int(coverage.get("items_total") or 0))
    manifest = {
        "schema": 1, "team": team, "folder": folder.name,
        "items": items, "coverage_total": total, "coverage_missing": total - len(items),
    }
    manifest["revision"] = digest(manifest)
    return manifest, details, assets


def render_page(team: str, folder: str, api_url: str) -> str:
    import html

    from argus.reporting.html_report import _ARGUS_RULE_HEADLINES

    headlines = {
        **_ARGUS_RULE_HEADLINES,
        "R3": "Overall pass conflicts with a blocked step",
        "R10": "Defect reference is in a comment instead of a linked ticket",
        "R11": "Step with an issue has no defect reference",
        "R13-amb": "Test marketplace could not be verified",
        "R14a": "Test passed after recent blocked or not applicable results",
        "R14b": "Test is blocked or not applicable after recent passes",
        "R15a": "Planned test was not executed",
        "R15b": "Required marketplace execution is missing",
        "R15c": "Tester completion is below the required target",
        "R16": "Failed or blocked execution has no explanation or defect link",
        "R17": "Previously executed test is now marked as not executed",
        "R18": "Not applicable result has no explanation",
        "R20": "Test execution is blocked",
        "R21": "Test execution is unfinished",
    }
    base = Path(__file__).parent / "assets"
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>ARGUS · Human review</title><style>'
        + (base / "review.css").read_text() + '</style><body data-team="'
        + html.escape(team, quote=True) + '" data-folder="'
        + html.escape(folder, quote=True) + '" data-api="'
        + html.escape(api_url, quote=True) + '">'
        + (base / "review.html").read_text()
        + '<script type="application/json" id="review-rule-headlines">'
        + json.dumps(headlines).replace("<", "\\u003c") + "</script><script>"
        + (base / "review.js").read_text() + "</script></body></html>"
    )
