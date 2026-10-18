"""Desktop evidence limits and submitted Weblinks, without fetching external files."""
from __future__ import annotations

import html
import re
from typing import Any, Iterator
from urllib.parse import urlsplit

from argus.core.redaction import redact_credentials
from argus.domain.finding_inference import (
    PRODUCT_ISSUE_TERMS,
    STRONG_CONTRADICTION_TERMS,
    reconcile_verdict,
    require_review,
)

_WEBLINK_FIELDS = ("weblinks", "web_links", "webLinks", "links", "test_case_weblinks")
_URL = re.compile(r"https?://[^\s<>\"']+", re.I)


def is_desktop(variant: str) -> bool:
    return variant.rsplit("_v", 1)[0] == "desktop"


def is_ios_shopping_app(metadata: dict[str, Any]) -> bool:
    tr = (metadata.get("test_results") or [{}])[0]
    run = str(tr.get("testrun_name") or "")
    platform = str(tr.get("expected_device") or run)
    return bool(
        re.search(r"\bshopping[ _-]+app\b", run, re.I)
        and re.search(r"\b(?:ios|iphone|ipad)\b", platform, re.I)
        and not re.search(r"\bandroid\b", platform, re.I)
    )


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for key in ("url", "webLink", "link", "urlDescription", "description"):
            yield from _strings(value.get(key))


def weblinks(record: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(
        text for field in _WEBLINK_FIELDS for text in _strings(record.get(field))
        if text.strip()
    ))


def drive_urls(text: str) -> list[str]:
    """Recognize submitted Drive links, never hostnames embedded in another URL."""
    result = []
    for match in _URL.finditer(html.unescape(text)):
        url = match.group().rstrip(".,;)]}")
        try:
            parsed = urlsplit(url)
            host = (parsed.hostname or "").lower()
        except ValueError:
            continue
        if parsed.username or parsed.password or not parsed.path.strip("/"):
            continue
        if host in {"drive.google.com", "docs.google.com"}:
            result.append(redact_credentials(url))
    return list(dict.fromkeys(result))


def external_evidence(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """Execution-submitted references only; test instructions do not prove submission."""
    refs: list[dict[str, Any]] = []
    for tr in metadata.get("test_results") or []:
        for record in [tr, *(tr.get("steps") or [])]:
            step = record.get("index") if record is not tr else None
            for text in [str(record.get("comment") or ""), *weblinks(record)]:
                for url in drive_urls(text):
                    ref = {"url": url, "step_index": step, "source": "submitted_link"}
                    if ref not in refs:
                        refs.append(ref)
    for record in metadata.get("external_evidence_links") or []:
        if not isinstance(record, dict):
            continue
        for url in drive_urls(str(record.get("url") or "")):
            ref = {**record, "url": url}
            if ref not in refs:
                refs.append(ref)
    return refs


def apply_evidence_review(
    audit: dict[str, Any], metadata: dict[str, Any],
) -> dict[str, Any]:
    """Keep desktop inspection limits separate from confirmed execution deviations."""
    if not is_desktop(str(audit.get("variant_name") or "")):
        return audit
    refs = external_evidence(metadata)
    scopes = {ref.get("step_index") for ref in refs}
    findings = []
    for finding in audit.get("findings") or []:
        text = str(finding.get("description") or "").lower()
        if finding.get("rule") in {"R9-amb", "R13-amb"}:
            finding = require_review(
                finding,
                "Desktop environment/marketplace could not be established from the attached evidence.",
            )
        elif (
            refs
            and finding.get("source") in {None, "model", "video"}
            and finding.get("category") == "insufficient_evidence"
            and (None in scopes or finding.get("step_index") is None
                 or finding.get("step_index") in scopes)
            and not any(term in text for term in PRODUCT_ISSUE_TERMS + STRONG_CONTRADICTION_TERMS)
        ):
            finding = require_review(
                finding, "The missing proof may be in linked Drive evidence ARGUS has not inspected.",
            )
        findings.append(finding)
    if refs:
        for step in sorted(scopes, key=lambda item: -1 if item is None else item):
            if any(
                f.get("_external_evidence_review") and f.get("step_index") == step
                for f in findings
            ):
                continue
            findings.append(require_review({
                "_external_evidence_review": True,
                "source": "evidence",
                "title": "Linked Drive evidence needs human review",
                "severity": "medium",
                "category": "insufficient_evidence",
                "description": (
                    "The tester supplied a Drive evidence link that ARGUS has not inspected. "
                    "Review the linked video or document before deciding whether the "
                    "required outcome is supported."
                ),
                "page": None,
                "step_index": step,
            }, "External evidence is available by reference but has not been audited."))
    out = {**audit, "findings": findings}
    if refs:
        out["external_evidence_links"] = refs
        note = "Linked Drive evidence requires human review."
        if note not in str(out.get("summary") or ""):
            out["summary"] = f"{out.get('summary') or ''} {note}".strip()
    return reconcile_verdict(out)
