from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
import requests
from botocore.config import Config as BotoConfig

import config as argus_config
import env_check
import workflow_rules

log = logging.getLogger("argus.audit")

DEFAULT_MODEL_ID = "example-model"
DEFAULT_REGION = "us-west-2"
DEFAULT_TEMPERATURE = 0.0

AUDIT_SCHEMA_VERSION = 6

VALID_VERDICTS = {"pass", "concerns", "fail"}
VALID_SEVERITIES = {"high", "medium", "low"}
VALID_FINDING_CATEGORIES = {
    "tester_error",
    "product_issue",
    "environment_issue",
    "insufficient_evidence",
    "policy_exception",
}
VALID_FINDING_ACTIONS = {
    "re_run",
    "attach_defect",
    "fix_status",
    "add_evidence",
    "review_manually",
    "none",
}
VALID_EVIDENCE_STATUSES = {
    "supported",
    "partial",
    "missing",
    "issue_found",
    "not_assessed",
}

from auditor_prompts import (  # noqa: E402
    SYSTEM_PROMPT_V1,
    SYSTEM_PROMPT_V2,
    SYSTEM_PROMPT_V3,
    SYSTEM_PROMPT_V4,
    SYSTEM_PROMPT_V5,
    SYSTEM_PROMPT_V5_4,
    SYSTEM_PROMPT_V5_5,
)

SYSTEM_PROMPT = SYSTEM_PROMPT_V5


@dataclass
class AuditConfig:
    execution_dir: Path
    model_provider: str = "bedrock"
    model_id: str = DEFAULT_MODEL_ID
    region: str = DEFAULT_REGION
    cloud_profile: str | None = None
    api_base_url: str | None = None
    api_key_env: str | None = None
    max_pages: int | None = None
    temperature: float = DEFAULT_TEMPERATURE
    system_prompt: str | None = None
    output_dir: Path | None = None
    variant_name: str = "v5"
    consensus_enabled: bool = True
    chunk_threshold: int = 70
    chunk_size: int = 50
    chunk_overlap: int = 5
    chunk_max_parallel: int = 4
    env_check_inline: bool = True
    env_check_sample_stride: int = 1
    env_check_region_crop_height: int = 0
    env_check_region_crop_bottom_height: int = 0
    env_check_engine: str = "off"
    debug_output: bool = False


def _html_to_text(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<img[^>]*>", "[inline image]", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return s.strip()


def build_steps_block(metadata: dict[str, Any]) -> str:
    """Render the tester-facing test-plan block for the prompt."""
    results = metadata.get("test_results") or []
    if not results:
        return "(no steps found)"
    tr = results[0]  # v1 assumes one test result per execution

    lines: list[str] = []

    # Assigned marketplace (ground truth from testrun name). R13 owns the
    # URL-vs-MP check; model uses this for content sanity-checks only.
    marketplace = tr.get("marketplace")
    if marketplace:
        mp_tld = tr.get("marketplace_tld") or ""
        lines.append("=== Assigned marketplace (ground truth) ===")
        lines.append(
            f"This test was assigned to the {marketplace} marketplace "
            f"({mp_tld}). The deterministic R13 check verifies the "
            f"exampleapp.<tld> in URL bars; you only need to use this "
            f"to sanity-check pixel-level content (currency, locale-"
            f"specific surfaces, storefront branding). Do NOT attempt "
            f"to re-derive the assigned marketplace from URL bars or "
            f"step text — R13 has ground truth from the testrun name."
        )
        lines.append("")

    # Assigned platform (ground truth from testrun name). ExampleApp policy:
    # only WEBVIEW tests run on physical devices; non-Webview Mobile uses
    # desktop DevTools emulation — emulating a Galaxy/iPhone is intended,
    # NOT a mismatch. Flag only on browser-family or device-family swaps.
    surface = tr.get("expected_surface")
    browser = tr.get("expected_browser")
    device = tr.get("expected_device")
    if surface or browser or device:
        parts: list[str] = []
        if surface:
            parts.append(f"surface={surface}")
        if browser:
            parts.append(f"browser={browser}")
        if device:
            parts.append(f"device={device}")
        lines.append("=== Assigned platform (ground truth) ===")
        lines.append(
            "This test was scoped to: "
            + ", ".join(parts) + ". "
            "IMPORTANT: only Webview runs are executed on physical "
            "devices. Non-Webview Mobile runs are expected to use "
            "desktop browser DevTools mobile-emulation — that is the "
            "intended execution mode for these. Do NOT flag DevTools "
            "emulation as a platform violation when the testrun is a "
            "non-Webview Mobile run. "
            "Flag a HIGH finding ONLY when the DEVICE FAMILY shown is "
            "unambiguously different from what was assigned (e.g. UA "
            "string contains 'Linux; Android; SM-...' or device "
            "dropdown shows 'Galaxy/Pixel' on a test scoped to iPhone). "
            "BROWSER-FAMILY DETECTION IS UNRELIABLE: do NOT claim a "
            "browser-family mismatch unless the assigned browser is "
            "explicitly named in the test and you can clearly see a "
            "DIFFERENT browser's distinctive UI signature — and even "
            "then, default to TRUST THE TESTER's assignment. Firefox "
            "with DevTools open, Chrome with DevTools open, and Edge "
            "with DevTools open all look very similar in cropped "
            "screenshots; the dominant element is the DevTools panel, "
            "not the browser chrome. Window chrome (tab shape, toolbar "
            "icons) varies slightly between browsers but the differences "
            "are subtle — do NOT use them as primary evidence. The only "
            "high-confidence browser identifications are: (a) Safari's "
            "very compact toolbar with no separate tab bar; (b) Samsung "
            "Internet's distinctive blue download arrow + tab counter "
            "in the URL bar on actual mobile; (c) the actual browser "
            "name visible in screen-mirror window titles or task-"
            "switcher chrome. Don't fire a browser-family HIGH unless "
            "you have one of those signals. "
            "Do NOT flag: different theme, dark/light mode, minor "
            "browser-version differences, presence of DevTools panel "
            "itself, or Mobile-vs-Desktop surface mismatches (those "
            "are covered separately by the WEBVIEW / DEVICE TESTING "
            "checklist for Webview tests only)."
        )
        lines.append("")

    # Top-level tester context (if present).
    top_status = tr.get("status")  # resolved name like "Pass"
    top_comment = tr.get("comment")
    top_links = tr.get("trace_links") or []
    if top_status or top_comment or top_links:
        lines.append("=== Tester overall result ===")
        if top_status:
            lines.append(f"Status: {top_status}")
        if top_comment:
            lines.append(f"Comment: {top_comment}")
        if top_links:
            lines.append(f"Linked issues: {', '.join(top_links)}")
        lines.append("")

    # Per-step block.
    lines.append("=== Test steps ===")
    steps = tr.get("steps") or []
    for s in steps:
        desc = _html_to_text(s.get("description"))
        exp = _html_to_text(s.get("expected_result"))
        lines.append(f"Step {s.get('index')}:")
        lines.append(f"  Description: {desc}")
        lines.append(f"  Expected result: {exp or '(none)'}")
        # Optional tester-provided rows. Only emit when non-empty so the
        # prompt stays compact on executions that don't use these fields.
        step_status = s.get("status")
        if step_status:
            lines.append(f"  Tester status: {step_status}")
        step_comment = s.get("comment")
        if step_comment:
            lines.append(f"  Tester comment: {step_comment}")
        step_links = s.get("trace_links") or []
        if step_links:
            lines.append(f"  Linked issues: {', '.join(step_links)}")
    return "\n".join(lines)


def load_page_images(execution_dir: Path, max_pages: int | None) -> list[Path]:
    screenshots_root = execution_dir / "screenshots"
    if not screenshots_root.exists():
        return []
    # There may be multiple PDFs split into subdirs; flatten, sorted by name.
    # Accept both .jpg (new default) and .png (legacy). Skip the
    # step_attachments/ subdir — those are handled by
    # load_step_attachment_images() below.
    pages: list[Path] = []
    for subdir in sorted(screenshots_root.iterdir()):
        if subdir.is_dir() and subdir.name != "step_attachments":
            found = sorted(subdir.glob("page_*.jpg")) + sorted(subdir.glob("page_*.png"))
            pages.extend(sorted(found, key=lambda p: p.stem))
    if max_pages is not None:
        pages = pages[:max_pages]
    return pages


# Filenames written by extractor._step_attachment_path:
#   step_NNN_<id>_<origname>    -> step index = NNN (int)
#   top_<id>_<origname>         -> no step attribution (None)
_STEP_FILENAME_RE = re.compile(r"^(?:step_(\d+)|top)_")


def load_step_attachment_images(
    execution_dir: Path,
) -> list[tuple[Path, int | None]]:
    """Load step/comment images the extractor downloaded, with their step
    attribution.

    Returns a list of (path, step_index_or_None). step_index is None for
    top-level attachments (not tied to any specific step). Ordering:
    step-attributed files first, sorted by step index then by filename;
    then top-level files in filename order. This keeps related attachments
    adjacent in the prompt.
    """
    step_dir = execution_dir / "screenshots" / "step_attachments"
    if not step_dir.exists():
        return []
    attributed: list[tuple[Path, int]] = []
    top_level: list[Path] = []
    for p in step_dir.iterdir():
        if not p.is_file():
            continue
        if p.suffix.lower() not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
            continue
        m = _STEP_FILENAME_RE.match(p.name)
        if not m:
            # Unknown naming — treat as top-level rather than drop.
            top_level.append(p)
            continue
        step_str = m.group(1)
        if step_str is None:
            top_level.append(p)
        else:
            attributed.append((p, int(step_str)))
    attributed.sort(key=lambda t: (t[1], t[0].name))
    top_level.sort(key=lambda p: p.name)
    return [(p, idx) for (p, idx) in attributed] + [(p, None) for p in top_level]


def build_message_content(
    steps_text: str,
    page_paths: list[Path],
    step_attachments: list[tuple[Path, int | None]] | None = None,
) -> list[dict[str, Any]]:
    """Build the multimodal message content sent to LLM Runtime.

    page_paths = PDF-split screenshots (numbered, primary input).
    step_attachments = tester-attached images with step attribution,
    appended unnumbered so they don't collide with page-indexed findings.
    """
    content: list[dict[str, Any]] = []
    step_attachments = step_attachments or []

    n_attachments = len(step_attachments)
    intro = (
        "Here is the list of test steps:\n\n"
        f"{steps_text}\n\n"
        f"Below are {len(page_paths)} screenshot page(s) from the execution, "
        "in order."
    )
    if n_attachments:
        intro += (
            f" After those, {n_attachments} additional tester-attached "
            "reference image(s) are provided with step attribution — these "
            "are NOT numbered pages, they're supplementary context."
        )
    intro += " Review everything and produce the audit JSON per the system prompt."
    content.append({"type": "text", "text": intro})

    # PDF-split pages, numbered so finding.page refs are meaningful.
    for i, p in enumerate(page_paths, start=1):
        img_bytes = p.read_bytes()
        b64 = base64.standard_b64encode(img_bytes).decode("ascii")
        media_type = "image/jpeg" if p.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
        content.append({
            "type": "text",
            "text": f"Page {i} ({p.name}):",
        })
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": b64,
            },
        })

    # Tester-attached step / comment images, with step attribution so the
    # model can correlate them to the appropriate test step. Deliberately
    # NOT numbered as pages to prevent confusion with page-indexed findings.
    if step_attachments:
        content.append({
            "type": "text",
            "text": (
                "\n=== Tester-attached reference images ===\n"
                "These images were attached directly by the tester to "
                "specific steps (or to the overall execution). Use them as "
                "supplementary evidence — the step attribution tells you "
                "which step each image relates to."
            ),
        })
        for p, step_idx in step_attachments:
            img_bytes = p.read_bytes()
            b64 = base64.standard_b64encode(img_bytes).decode("ascii")
            ext = p.suffix.lower()
            if ext in {".jpg", ".jpeg"}:
                media_type = "image/jpeg"
            elif ext == ".webp":
                media_type = "image/webp"
            elif ext == ".gif":
                media_type = "image/gif"
            else:
                media_type = "image/png"
            if step_idx is None:
                label = f"Top-level attachment ({p.name}):"
            else:
                label = f"Attachment for Step {step_idx} ({p.name}):"
            content.append({"type": "text", "text": label})
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": b64,
                },
            })
    return content


def _make_llm_runtime_client(cfg: AuditConfig, *, boto_cfg: BotoConfig):
    """Build the Bedrock-compatible runtime client honoring cfg.cloud_profile.

    Two paths: profile -> Session.client (production); no profile ->
    module-level boto3.client (preserves the
    monkeypatch.setattr(auditor.boto3, "client", ...) test pattern).
    """
    if cfg.cloud_profile:
        session = boto3.Session(profile_name=cfg.cloud_profile)
        return session.client("llm_runtime-runtime", region_name=cfg.region,
                              config=boto_cfg)
    return boto3.client("llm_runtime-runtime", region_name=cfg.region,
                        config=boto_cfg)


def _system_prompt_for_cfg(cfg: AuditConfig) -> str:
    # None => canonical SYSTEM_PROMPT. Empty string is a real override
    # (so an experiment can test a blank-system-prompt variant).
    return cfg.system_prompt if cfg.system_prompt is not None else SYSTEM_PROMPT


def _messages_body(cfg: AuditConfig, content: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "model": cfg.model_id,
        "max_tokens": 8192,
        "temperature": cfg.temperature,
        "system": _system_prompt_for_cfg(cfg),
        "messages": [{"role": "user", "content": content}],
    }


def _api_key_from_env(env_name: str | None, default_env: str) -> str:
    name = env_name or default_env
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing API key. Set ${name} or configure auditor.api_key_env."
        )
    return value


def _api_url(base_url: str | None, default_base: str, endpoint: str) -> str:
    base = (base_url or default_base).rstrip("/")
    if base.endswith(endpoint):
        return base
    if base.endswith("/v1"):
        return f"{base}{endpoint}"
    return f"{base}/v1{endpoint}"


def _post_json(url: str, headers: dict[str, str],
               payload: dict[str, Any]) -> dict[str, Any]:
    resp = requests.post(url, headers=headers, json=payload, timeout=300)
    resp.raise_for_status()
    return resp.json()


def _invoke_bedrock(cfg: AuditConfig,
                    content: list[dict[str, Any]]) -> dict[str, Any]:
    # Adaptive retries are required at batch concurrency >= ~4; without this,
    # provider throttling surfaces as a hard failure on busy runs.
    #
    # read_timeout=300: botocore's default is 60s, which was firing on
    # large multi-image payloads (~9 MB, 50+ images) where LLM Runtime takes
    # >60s to produce the first byte. When the timeout fires, the entire
    # adaptive-retry budget is burned at 60s per attempt, so a single
    # slow request costs ~5 min before failing. 300s covers the observed
    # p99 comfortably; adaptive retries still kick in for real throttling.
    boto_cfg = BotoConfig(
        retries={"mode": "adaptive", "max_attempts": 5},
        read_timeout=300,
        connect_timeout=30,
    )
    client = _make_llm_runtime_client(cfg, boto_cfg=boto_cfg)
    body = _messages_body(cfg, content)
    body["anthropic_version"] = "llm_runtime-2023-05-31"
    body.pop("model", None)
    resp = client.invoke_model(
        modelId=cfg.model_id,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    return json.loads(resp["body"].read())


def _invoke_anthropic(cfg: AuditConfig,
                      content: list[dict[str, Any]]) -> dict[str, Any]:
    url = _api_url(cfg.api_base_url, "https://api.anthropic.com", "/messages")
    headers = {
        "x-api-key": _api_key_from_env(cfg.api_key_env, "ANTHROPIC_API_KEY"),
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    return _post_json(url, headers, _messages_body(cfg, content))


def _openai_content(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for block in content:
        if block.get("type") == "text":
            converted.append({"type": "text", "text": block.get("text", "")})
        elif block.get("type") == "image":
            source = block.get("source") or {}
            media_type = source.get("media_type", "image/jpeg")
            data = source.get("data", "")
            converted.append({
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{data}"},
            })
    return converted


def _invoke_openai(cfg: AuditConfig,
                   content: list[dict[str, Any]]) -> dict[str, Any]:
    url = _api_url(
        cfg.api_base_url, "https://api.openai.com", "/chat/completions")
    headers = {
        "authorization": (
            "Bearer " + _api_key_from_env(cfg.api_key_env, "OPENAI_API_KEY")
        ),
        "content-type": "application/json",
    }
    payload = {
        "model": cfg.model_id,
        "temperature": cfg.temperature,
        "messages": [
            {"role": "system", "content": _system_prompt_for_cfg(cfg)},
            {"role": "user", "content": _openai_content(content)},
        ],
    }
    raw = _post_json(url, headers, payload)
    text = (
        (((raw.get("choices") or [{}])[0]).get("message") or {})
        .get("content", "")
    )
    return {"content": [{"type": "text", "text": text}]}


def _google_parts(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for block in content:
        if block.get("type") == "text":
            parts.append({"text": block.get("text", "")})
        elif block.get("type") == "image":
            source = block.get("source") or {}
            parts.append({
                "inline_data": {
                    "mime_type": source.get("media_type", "image/jpeg"),
                    "data": source.get("data", ""),
                },
            })
    return parts


def _google_url(cfg: AuditConfig) -> str:
    key = _api_key_from_env(cfg.api_key_env, "GOOGLE_API_KEY")
    base = (
        cfg.api_base_url
        or "https://generativelanguage.googleapis.com/v1beta"
    ).rstrip("/")
    if ":generateContent" in base:
        url = base
    else:
        url = f"{base}/models/{cfg.model_id}:generateContent"
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}key={key}"


def _invoke_google(cfg: AuditConfig,
                   content: list[dict[str, Any]]) -> dict[str, Any]:
    payload = {
        "system_instruction": {
            "parts": [{"text": _system_prompt_for_cfg(cfg)}],
        },
        "contents": [{
            "role": "user",
            "parts": _google_parts(content),
        }],
        "generationConfig": {
            "temperature": cfg.temperature,
            "maxOutputTokens": 8192,
        },
    }
    raw = _post_json(
        _google_url(cfg),
        {"content-type": "application/json"},
        payload,
    )
    text = ""
    candidates = raw.get("candidates") or []
    if candidates:
        parts = ((candidates[0].get("content") or {}).get("parts") or [])
        text = "".join(p.get("text", "") for p in parts)
    return {"content": [{"type": "text", "text": text}]}


def invoke_model(cfg: AuditConfig, content: list[dict[str, Any]]) -> dict[str, Any]:
    provider = (cfg.model_provider or "bedrock").lower()
    if provider in {"bedrock", "aws", "llm_runtime"}:
        return _invoke_bedrock(cfg, content)
    if provider in {"anthropic", "claude"}:
        return _invoke_anthropic(cfg, content)
    if provider == "openai":
        return _invoke_openai(cfg, content)
    if provider in {"google", "gemini"}:
        return _invoke_google(cfg, content)
    raise ValueError(
        "Unsupported model_provider "
        f"{cfg.model_provider!r}; expected openai, anthropic, google, or bedrock."
    )


def extract_audit_json(raw: dict[str, Any]) -> dict[str, Any]:
    # Claude Messages API via LLM Runtime returns content as a list of blocks.
    blocks = raw.get("content") or []
    text = ""
    for b in blocks:
        if b.get("type") == "text":
            text += b.get("text", "")
    text = text.strip()
    # Strip code fences if present.
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    # Fallback: extract first balanced {...} block if the model added preamble.
    candidates = [text]
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace >= 0 and last_brace > first_brace:
        candidates.append(text[first_brace : last_brace + 1])
    for cand in candidates:
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    return {
        "overall_verdict": "concerns",
        "summary": f"Model returned non-JSON output; could not parse. Raw: {text[:500]}",
        "findings": [],
        "_raw": text,
    }


def validate_audit(audit: dict[str, Any]) -> list[str]:
    """Return human-readable errors for a parsed audit dict (empty == valid).

    Messages are usable verbatim in a repair prompt back to the model.
    """
    errors: list[str] = []

    # Skip our own parse-failure fallback shape; it already carries a summary.
    if "_raw" in audit:
        return errors

    verdict = audit.get("overall_verdict")
    if verdict not in VALID_VERDICTS:
        errors.append(
            f"'overall_verdict' must be one of {sorted(VALID_VERDICTS)}, got {verdict!r}"
        )

    summary = audit.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        errors.append("'summary' must be a non-empty string")

    findings = audit.get("findings")
    if not isinstance(findings, list):
        errors.append("'findings' must be a list (use [] for no findings)")
        return errors  # can't validate items if it's not a list

    for i, f in enumerate(findings):
        if not isinstance(f, dict):
            errors.append(f"findings[{i}] must be an object")
            continue
        sev = f.get("severity")
        if sev not in VALID_SEVERITIES:
            errors.append(
                f"findings[{i}].severity must be one of "
                f"{sorted(VALID_SEVERITIES)}, got {sev!r}"
            )
        desc = f.get("description")
        if not isinstance(desc, str) or not desc.strip():
            errors.append(f"findings[{i}].description must be a non-empty string")
        # page and step_index are optional — must be int or null when present.
        for key in ("page", "step_index"):
            if key in f and f[key] is not None and not isinstance(f[key], int):
                errors.append(f"findings[{i}].{key} must be an integer or null")
        category = f.get("category")
        if category is not None and category not in VALID_FINDING_CATEGORIES:
            errors.append(
                f"findings[{i}].category must be one of "
                f"{sorted(VALID_FINDING_CATEGORIES)}, got {category!r}"
            )
        confidence = f.get("confidence")
        if confidence is not None and confidence not in VALID_SEVERITIES:
            errors.append(
                f"findings[{i}].confidence must be one of "
                f"{sorted(VALID_SEVERITIES)}, got {confidence!r}"
            )
        action = f.get("action")
        if action is not None and action not in VALID_FINDING_ACTIONS:
            errors.append(
                f"findings[{i}].action must be one of "
                f"{sorted(VALID_FINDING_ACTIONS)}, got {action!r}"
            )

    evidence = audit.get("evidence_by_step")
    if evidence is not None:
        if not isinstance(evidence, list):
            errors.append("'evidence_by_step' must be a list when present")
        else:
            for i, row in enumerate(evidence):
                if not isinstance(row, dict):
                    errors.append(f"evidence_by_step[{i}] must be an object")
                    continue
                if not isinstance(row.get("step_index"), int):
                    errors.append(
                        f"evidence_by_step[{i}].step_index must be an integer"
                    )
                pages = row.get("pages", [])
                if not isinstance(pages, list) or not all(
                    isinstance(p, int) for p in pages
                ):
                    errors.append(
                        f"evidence_by_step[{i}].pages must be a list of integers"
                    )
                confidence = row.get("confidence")
                if confidence is not None and confidence not in VALID_SEVERITIES:
                    errors.append(
                        f"evidence_by_step[{i}].confidence must be one of "
                        f"{sorted(VALID_SEVERITIES)}, got {confidence!r}"
                    )
                status = row.get("status")
                if status is not None and status not in VALID_EVIDENCE_STATUSES:
                    errors.append(
                        f"evidence_by_step[{i}].status must be one of "
                        f"{sorted(VALID_EVIDENCE_STATUSES)}, got {status!r}"
                    )
    return errors


def invoke_and_parse(
    cfg: AuditConfig,
    content: list[dict[str, Any]],
) -> dict[str, Any]:
    """Invoke -> parse -> validate -> (repair retry) for the full pipeline.

    On unrepairable validation failure, returns a concerns-shaped fallback
    with errors surfaced in the summary (same pattern as extract_audit_json).
    """
    raw = invoke_model(cfg, content)
    audit = extract_audit_json(raw)
    # Parse-failure fallback already carries its own summary; don't re-validate.
    if "_raw" in audit:
        return audit

    errors = validate_audit(audit)
    if not errors:
        return audit

    log.warning("audit JSON failed validation: %s — attempting repair", "; ".join(errors))

    # Repair retry: feed back the invalid response + the specific errors, ask
    # for a corrected one. We append to `content` so the model sees the full
    # original context (steps + images) plus what went wrong.
    repair_content = content + [
        {
            "type": "text",
            "text": (
                "Your previous response did not conform to the required schema. "
                "Specifically:\n"
                + "\n".join(f"  - {e}" for e in errors)
                + "\n\nReturn a corrected JSON object with the exact schema "
                "from the system prompt. ONE JSON object, no prose, no code "
                "fences. First character must be '{', last must be '}'."
            ),
        }
    ]
    raw2 = invoke_model(cfg, repair_content)
    audit2 = extract_audit_json(raw2)
    if "_raw" in audit2:
        # Repair produced un-parseable output. Keep the original parsed-but-
        # -invalid dict, wrap it in a concerns fallback so the pipeline
        # doesn't crash and the problem is visible in audit.json.
        return {
            "overall_verdict": "concerns",
            "summary": (
                "Model output failed schema validation and the repair retry "
                "returned un-parseable JSON. "
                f"Original errors: {'; '.join(errors)}"
            ),
            "findings": [],
            "_invalid_original": audit,
        }

    errors2 = validate_audit(audit2)
    if errors2:
        log.warning("repair retry still invalid: %s", "; ".join(errors2))
        return {
            "overall_verdict": "concerns",
            "summary": (
                "Model output failed schema validation twice. "
                f"Final errors: {'; '.join(errors2)}"
            ),
            "findings": [],
            "_invalid_original": audit,
            "_invalid_repair": audit2,
        }
    return audit2


# Chunking subsystem in auditor_chunking.py; re-exported so callers and
# tests reach it via auditor.plan_chunks etc. Placement matters:
# invoke_and_parse + build_message_content above are pulled back via
# deferred imports inside chunking.
from auditor_chunking import (  # noqa: E402  (must follow defs above)
    CONSENSUS_RUNS,
    CONSENSUS_THRESHOLD,
    _finding_key,
    _rewrite_page_refs,
    audit_with_consensus,
    plan_chunks,
    _combine_verdicts,
    merge_chunk_audits,
    audit_chunked,
    synthesize_summary,
)

def merge_rule_findings(
    audit: dict[str, Any],
    rule_findings: list[dict[str, Any]],
) -> dict[str, Any]:
    """Merge workflow_rules findings into a screenshot audit.

    Rule findings carry source="rule" + a rule id and bypass consensus
    (pure functions of metadata). Verdict escalates only:
    high -> fail, medium on pass -> concerns. Never demotes.
    """
    if not rule_findings:
        return audit

    merged = dict(audit)
    existing = list(audit.get("findings") or [])
    merged["findings"] = existing + rule_findings

    # Verdict escalation (never demote).
    current = merged.get("overall_verdict", "concerns")
    severity_rank = {"pass": 0, "concerns": 1, "fail": 2}
    highest_rule_sev = max(
        (f.get("severity", "low") for f in rule_findings),
        default="low",
        key=lambda s: {"low": 0, "medium": 1, "high": 2}.get(s, 0),
    )
    if highest_rule_sev == "high":
        proposed = "fail"
    elif highest_rule_sev == "medium":
        proposed = "concerns"
    else:
        proposed = current
    if severity_rank.get(proposed, 0) > severity_rank.get(current, 0):
        merged["overall_verdict"] = proposed

    # Summary note so the audit.md reader sees why the finding count jumped.
    note = (
        f"Additionally, {len(rule_findings)} workflow consistency "
        f"issue{'s' if len(rule_findings) != 1 else ''} flagged by "
        "deterministic metadata checks (see findings below)."
    )
    summary = merged.get("summary", "") or ""
    merged["summary"] = f"{summary}\n\n{note}".strip()

    return merged


_INSUFFICIENT_EVIDENCE_TERMS = (
    "no evidence",
    "missing evidence",
    "not evidenced",
    "not otherwise evidenced",
    "not shown",
    "not otherwise shown",
    "not present",
    "no screenshot",
    "no library screenshot",
    "no purchase history screenshot",
    "no inbox screenshot",
    "no email screenshot",
    "screenshot missing",
    "not provided",
    "not verified",
    "not otherwise verified",
    "unable to verify",
    "requires verifying",
    "required verification",
)
_ENVIRONMENT_TERMS = (
    "production url",
    "prod url",
    "feature-preprod",
    "marketplace",
    "currency",
    "locale",
    "storefront",
    "exampleapp.",
)
_PRODUCT_ISSUE_TERMS = (
    "crash",
    "stack trace",
    "404",
    "500",
    "5xx",
    "broken",
    "error",
    "failed to load",
    "something went wrong",
)
_VISUAL_CLAIM_TERMS = (
    "screenshot",
    "page",
    "visible",
    "shown",
    "screen",
    "image",
    "ui",
    "button",
    "banner",
    "modal",
    "order summary",
)


def _infer_finding_category(finding: dict[str, Any]) -> str:
    """Best-effort category for old model/rule findings."""
    desc = (finding.get("description") or "").lower()
    if any(t in desc for t in _INSUFFICIENT_EVIDENCE_TERMS):
        return "insufficient_evidence"
    category = finding.get("category")
    if category in VALID_FINDING_CATEGORIES:
        return category
    rule = finding.get("rule")
    source = finding.get("source")
    if source == "env_check" or rule in {"R9", "R13"}:
        return "environment_issue"
    if rule in {"R12", "R15a", "R15b", "R15c"}:
        return "insufficient_evidence"
    if rule in {
        "R0", "R1", "R2", "R4", "R5", "R6", "R7", "R8", "R10",
        "R11", "R14", "R16", "R17", "R18",
    }:
        return "tester_error"
    if any(t in desc for t in _ENVIRONMENT_TERMS):
        return "environment_issue"
    if any(t in desc for t in _PRODUCT_ISSUE_TERMS):
        return "product_issue"
    return "tester_error"


def _infer_finding_action(finding: dict[str, Any], category: str) -> str:
    action = finding.get("action")
    if action in VALID_FINDING_ACTIONS:
        return action
    rule = finding.get("rule")
    if category == "insufficient_evidence":
        return "add_evidence"
    if category == "environment_issue":
        return "re_run"
    if category == "product_issue":
        return "attach_defect"
    if category == "policy_exception":
        return "review_manually"
    if rule in {"R2", "R4", "R5", "R6", "R7", "R8", "R16", "R18"}:
        return "fix_status"
    if rule in {"R10", "R11"}:
        return "attach_defect"
    return "review_manually"


def _infer_finding_confidence(finding: dict[str, Any], category: str) -> str:
    confidence = finding.get("confidence")
    if confidence in VALID_SEVERITIES:
        return confidence
    if finding.get("source") in {"rule", "env_check"}:
        return "high"
    if isinstance(finding.get("page"), int):
        return "high"
    if category == "insufficient_evidence" and isinstance(
        finding.get("step_index"), int
    ):
        return "medium"
    return "low"


def _looks_like_unpage_grounded_visual_claim(finding: dict[str, Any]) -> bool:
    if finding.get("source") in {"rule", "env_check"}:
        return False
    if isinstance(finding.get("page"), int):
        return False
    desc = (finding.get("description") or "").lower()
    if any(t in desc for t in _INSUFFICIENT_EVIDENCE_TERMS):
        return False
    return any(t in desc for t in _VISUAL_CLAIM_TERMS + _PRODUCT_ISSUE_TERMS)


def _normalise_findings_for_schema(
    findings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in findings:
        if not isinstance(raw, dict):
            continue
        f = dict(raw)
        category = _infer_finding_category(f)
        f["category"] = category
        f["action"] = _infer_finding_action(f, category)
        f["confidence"] = _infer_finding_confidence(f, category)

        if (
            f.get("severity") == "high"
            and category != "insufficient_evidence"
            and _looks_like_unpage_grounded_visual_claim(f)
        ):
            f["_severity_adjusted_from"] = "high"
            f["severity"] = "medium"
            f["confidence"] = "low"
            f["action"] = "review_manually"
        out.append(f)
    return out


def _normalise_evidence_by_step(audit: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = audit.get("evidence_by_step")
    if not isinstance(evidence, list):
        return []
    out: list[dict[str, Any]] = []
    for row in evidence:
        if not isinstance(row, dict) or not isinstance(row.get("step_index"), int):
            continue
        pages = row.get("pages") or []
        if not isinstance(pages, list):
            pages = []
        clean_pages = sorted({p for p in pages if isinstance(p, int) and p >= 1})
        confidence = row.get("confidence")
        if confidence not in VALID_SEVERITIES:
            confidence = "medium" if clean_pages else "low"
        status = row.get("status")
        if status not in VALID_EVIDENCE_STATUSES:
            status = "supported" if clean_pages else "not_assessed"
        missing_reason = row.get("missing_reason")
        if missing_reason is not None and not isinstance(missing_reason, str):
            missing_reason = str(missing_reason)
        out.append({
            "step_index": row["step_index"],
            "pages": clean_pages,
            "confidence": confidence,
            "status": status,
            "missing_reason": missing_reason,
        })
    return out


def enrich_audit(
    audit: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Back-fill v6 audit fields and normalize model/rule output.

    V6 adds optional human-triage fields to findings plus
    evidence_by_step. The model is prompted to emit them, but deterministic
    rules and older replay outputs do not. This function keeps the rest of
    the pipeline schema-stable without making legacy findings invalid.
    """
    enriched = dict(audit)
    enriched["findings"] = _normalise_findings_for_schema(
        list(audit.get("findings") or [])
    )
    evidence = _normalise_evidence_by_step(audit)
    if evidence:
        enriched["evidence_by_step"] = evidence
    elif "evidence_by_step" in enriched:
        enriched.pop("evidence_by_step", None)
    return enriched


def _severity_counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    """Count findings by severity. Unknown severities bucket under 'other'."""
    counts = {"high": 0, "medium": 0, "low": 0}
    for f in findings:
        sev = f.get("severity")
        if sev in counts:
            counts[sev] += 1
        else:
            counts["other"] = counts.get("other", 0) + 1
    return counts


# Phrases the model uses when re-implementing the deprecated R3 rule
# (Pass-overall + Blocked step → "no bug link" finding). Per V4 policy
# Pass + non-real-blocker Blocked step is acceptable, so we strip these.
_BLOCKED_NO_BUG_PHRASES = (
    "blocked step",
    "step is blocked",
    "blocked but no",
    "no bug link",
    "no linked defect",
)


def _drop_deprecated_blocked_findings(
    audit: dict[str, Any],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Strip model findings that re-implement deprecated R3.

    V4 policy: Pass overall + non-real-blocker Blocked step is fine.
    Drops the model's low/medium re-implementations only on Pass overall,
    only on Blocked-step-anchored findings, only when wording matches
    _BLOCKED_NO_BUG_PHRASES, and never on rule-sourced findings. Records
    the dropped count under `_dropped_blocked_buglink_fp`.
    """
    tr = (metadata.get("test_results") or [{}])[0]
    if tr.get("status") != "Pass":
        return audit
    blocked_step_indices = {
        s.get("index") for s in (tr.get("steps") or [])
        if s.get("status") == "Blocked"
    }
    if not blocked_step_indices:
        return audit
    findings = audit.get("findings") or []
    kept: list[dict[str, Any]] = []
    dropped = 0
    for f in findings:
        sev = f.get("severity")
        si = f.get("step_index")
        desc_l = (f.get("description") or "").lower()
        is_blocked_buglink_fp = (
            sev in ("low", "medium")
            and si in blocked_step_indices
            and any(p in desc_l for p in _BLOCKED_NO_BUG_PHRASES)
            and f.get("source") != "rule"
        )
        if is_blocked_buglink_fp:
            dropped += 1
            continue
        kept.append(f)
    if dropped:
        audit = dict(audit)
        audit["findings"] = kept
        audit["_dropped_blocked_buglink_fp"] = dropped
    return audit


def _finalize_verdict(audit: dict[str, Any]) -> dict[str, Any]:
    """Reconcile overall_verdict with final findings.

    Invariant: any high => fail; any findings => >=concerns. Never demotes.
    Closes the E179041-class gap (model returned high findings but kept
    verdict=concerns). Records `_verdict_escalated_from` on escalation.
    """
    findings = audit.get("findings") or []
    severity_rank = {"pass": 0, "concerns": 1, "fail": 2}
    has_high = any(f.get("severity") == "high" for f in findings)
    has_any = bool(findings)
    if has_high:
        proposed = "fail"
    elif has_any:
        proposed = "concerns"
    else:
        proposed = audit.get("overall_verdict", "pass")
    current = audit.get("overall_verdict", "concerns")
    if severity_rank.get(proposed, 0) > severity_rank.get(current, 0):
        audit = dict(audit)
        audit["overall_verdict"] = proposed
        audit["_verdict_escalated_from"] = current
    return audit


def _is_webview_path(p) -> bool:
    """True for actual WEBVIEW testing folders (gates R9-amb).

    Tightened 2026-05-23 from also matching "mobile" — Mobile_Playback /
    E2E_Flow_*Mobile are mobile-browser tests, not webview, and were
    triggering false R9-amb findings.
    """
    s = str(p).lower()
    return "webview" in s or "in_app" in s or "in-app" in s


# Markdown rendering in auditor_render.py; re-exported for existing callers.
from auditor_render import (  # noqa: E402
    render_markdown,
    _format_finding,
    _render_debug_section,
)

def _write_last_error(
    execution_dir: Path,
    cfg: AuditConfig,
    *,
    exception: BaseException | None = None,
    rc: int | None = None,
    failure_reason: str | None = None,
) -> None:
    """Persist failure details to <execution_dir>/_last_error.json.

    So operators inspecting _failed_keys.txt can see why a key failed
    without re-running. Overwrites stale errors; OSError on write is
    swallowed (don't mask the original failure).
    """
    import datetime
    import traceback as tb_module
    err: dict[str, Any] = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc)
                             .isoformat(timespec="seconds"),
        "model_provider": cfg.model_provider,
        "model_id": cfg.model_id,
        "region": cfg.region,
        "temperature": cfg.temperature,
        "max_pages": cfg.max_pages,
        "execution_dir": str(execution_dir),
    }
    if exception is not None:
        err["exception_class"] = type(exception).__name__
        err["exception_message"] = str(exception)
        err["traceback"] = "".join(tb_module.format_exception(
            type(exception), exception, exception.__traceback__
        ))
    if rc is not None:
        err["rc"] = rc
    if failure_reason:
        err["failure_reason"] = failure_reason
    try:
        (execution_dir / "_last_error.json").write_text(
            json.dumps(err, indent=2))
    except OSError as write_err:
        # Last-ditch: if the execution_dir itself isn't writeable, log
        # and move on. The original exception will still propagate.
        log.warning("could not write _last_error.json to %s: %s",
                    execution_dir, write_err)


def _clear_last_error(execution_dir: Path) -> None:
    """Remove stale _last_error.json after a successful audit."""
    p = execution_dir / "_last_error.json"
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass  # non-fatal — file lingers but audit still succeeded


def _archive_previous_audit(execution_dir: Path) -> Path | None:
    """Move existing audit.json (+ audit.md) into audit.json.history/ before
    a fresh audit overwrites them.

    Preserves the forensic chain when an E-key gets re-audited (Layer 1
    credibility fix). Filename: audit_<ISO>.json where ISO is colon-free
    YYYY-MM-DDTHH-MM-SS, sourced from `audited_at` field then mtime.
    Uses os.replace (atomic on POSIX/NTFS within a filesystem). Returns
    the history dir on success, None when nothing to archive.
    """
    import datetime as _dt
    import os as _os
    src_json = execution_dir / "audit.json"
    if not src_json.exists():
        return None  # first-time audit — nothing to preserve

    # Determine timestamp for the archive filename. Prefer the audit's
    # own `audited_at` so the filename matches the audit's self-reported
    # write time (works even if files have been copied around, losing
    # mtime). Fall back to mtime for legacy audits.
    ts_iso: str | None = None
    try:
        existing = json.loads(src_json.read_text())
        raw = existing.get("audited_at")
        if isinstance(raw, str) and raw:
            # Strip trailing Z (we write it as ...:SSZ); replace colons in
            # the time portion only. Date dashes stay intact.
            ts_iso = raw.rstrip("Z").replace(":", "-")
    except (OSError, json.JSONDecodeError, ValueError):
        ts_iso = None
    if not ts_iso:
        mtime = _dt.datetime.fromtimestamp(
            src_json.stat().st_mtime, tz=_dt.timezone.utc,
        )
        ts_iso = mtime.isoformat(timespec="seconds").rstrip("Z").replace(":", "-")
        # mtime ISO carries a +00:00 offset; strip it for filename hygiene.
        if "+" in ts_iso:
            ts_iso = ts_iso.split("+", 1)[0]

    history_dir = execution_dir / "audit.json.history"
    history_dir.mkdir(parents=True, exist_ok=True)

    dest_json = history_dir / f"audit_{ts_iso}.json"
    _os.replace(src_json, dest_json)

    src_md = execution_dir / "audit.md"
    if src_md.exists():
        # Markdown is regenerable from JSON, so missing-md is harmless;
        # we only archive it for human convenience when it does exist.
        dest_md = history_dir / f"audit_{ts_iso}.md"
        try:
            _os.replace(src_md, dest_md)
        except OSError as e:
            # Non-fatal: JSON archived OK, md couldn't be moved. Log and
            # continue — the canonical record is preserved.
            log.warning("could not archive audit.md to %s: %s", dest_md, e)

    return history_dir


def run(cfg: AuditConfig) -> int:
    metadata_path = cfg.execution_dir / "metadata.json"
    # cfg.output_dir is set by replay.py to <exec>/_replay/<variant>/ so
    # experiments don't touch canonical audit.json.
    output_dir = cfg.output_dir or cfg.execution_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    if not metadata_path.exists():
        log.error("metadata.json not found in %s", cfg.execution_dir)
        _write_last_error(
            output_dir, cfg,
            rc=1,
            failure_reason=(
                f"metadata.json not found in {cfg.execution_dir} — the "
                "extractor step either failed or was run against a "
                "different out_dir."
            ),
        )
        return 1

    # Wrap full audit flow so any failure is captured in _last_error.json
    # before propagating. Batch mode catches the re-raise; CLI gets the
    # traceback on stderr as before.
    try:
        metadata = json.loads(metadata_path.read_text())

        pages = load_page_images(cfg.execution_dir, cfg.max_pages)
        step_attachments = load_step_attachment_images(cfg.execution_dir)
        if not pages and not step_attachments:
            log.error(
                "no page images or step attachments found under %s/screenshots",
                cfg.execution_dir,
            )
            _write_last_error(
                output_dir, cfg,
                rc=1,
                failure_reason=(
                    f"no page images or step attachments under "
                    f"{cfg.execution_dir}/screenshots — the tester did "
                    "not attach a session PDF or per-step images."
                ),
            )
            return 1

        log.info(
            "auditing %d page(s) + %d step attachment(s) with %s (temp=%s)",
            len(pages), len(step_attachments), cfg.model_id, cfg.temperature,
        )
        steps_text = build_steps_block(metadata)

        if len(pages) >= cfg.chunk_threshold:
            log.info("page count %d > threshold %d — using chunked audit",
                     len(pages), cfg.chunk_threshold)
            audit = audit_chunked(cfg, steps_text, pages,
                                  step_attachments=step_attachments)
        else:
            content = build_message_content(steps_text, pages,
                                            step_attachments=step_attachments)
            audit = audit_with_consensus(cfg, content)

        # Deterministic workflow rules on metadata (no LLM Runtime cost).
        rule_findings = workflow_rules.run_workflow_rules(metadata)
        if rule_findings:
            log.info("workflow rules flagged %d issue(s)", len(rule_findings))
            audit = merge_rule_findings(audit, rule_findings)

        # Deterministic env-compliance via OCR — every exampleapp.<tld>
        # classified preprod/prod/ignore. R9 high-severity on any prod URL.
        # Persisted as audit["env_check"] for back-fill tooling.
        # env_check_inline=False defers to env_check_backfill (~doubles
        # folder throughput by unblocking workers on LLM Runtime response).
        if cfg.env_check_inline and cfg.env_check_engine != "off":
            is_webview = _is_webview_path(cfg.execution_dir)
            # Tesseract retired 2026-05-23; logs warn + falls through to haiku.
            if cfg.env_check_engine and cfg.env_check_engine not in ("haiku", "off"):
                log.info("env_check_engine=%r requested but tesseract "
                         "is retired; using haiku", cfg.env_check_engine)
            import env_check_haiku
            ec_result = env_check_haiku.check_env_compliance(
                cfg.execution_dir,
                sample_stride=cfg.env_check_sample_stride,
                region_crop_height=cfg.env_check_region_crop_height,
                region_crop_bottom_height=cfg.env_check_region_crop_bottom_height,
                region=cfg.region,
                profile=cfg.cloud_profile,
                is_webview=is_webview,
            )
            audit["env_check"] = {
                "version": ec_result["version"],
                "verdict": ec_result["verdict"],
                "preprod_urls": ec_result["preprod_urls"],
                "prod_urls": ec_result["prod_urls"],
                "images_with_preprod": ec_result["images_with_preprod"],
                "images_with_prod": ec_result["images_with_prod"],
                "images_scanned": ec_result["images_scanned"],
                "images_total": ec_result.get("images_total",
                                              ec_result["images_scanned"]),
                "sample_stride": ec_result.get("sample_stride", 1),
                "region_crop_height": ec_result.get("region_crop_height", 0),
                "region_crop_bottom_height": ec_result.get(
                    "region_crop_bottom_height", 0),
                "per_image": ec_result["per_image"],
            }
            if ec_result["findings"]:
                log.info(
                    "env_check verdict=%s — emitting %d finding(s)",
                    ec_result["verdict"], len(ec_result["findings"]),
                )
                audit = merge_rule_findings(audit, ec_result["findings"])
            else:
                log.info(
                    "env_check verdict=%s (scanned=%d/%d stride=%d, "
                    "preprod=%d, prod=%d)",
                    ec_result["verdict"],
                    ec_result["images_scanned"],
                    ec_result.get("images_total", ec_result["images_scanned"]),
                    ec_result.get("sample_stride", 1),
                    ec_result["images_with_preprod"],
                    ec_result["images_with_prod"],
                )
            # R13 needs env_check.preprod_urls. Deterministic
            # testrun-name-MP vs URL-MP check; bypasses the LLM.
            r13 = workflow_rules.check_marketplace_match(metadata, audit)
            if r13:
                log.info("R13 marketplace mismatch — emitting %d finding(s)",
                         len(r13))
                audit = merge_rule_findings(audit, r13)
        else:
            log.info("env_check deferred — caller will run env_check_backfill")

        # R14/R17 are pure history checks (no env_check dependency), so they
        # run UNCONDITIONALLY — not gated behind env_check_inline. Gating them
        # silently dropped these HIGH findings on the env_check_inline=False
        # ("doubles throughput") path unless backfill re-ran them. R13 stays
        # inside the block above because it genuinely needs env_check URLs.
        # Index load failure is non-fatal.
        try:
            import history_index as _history_index
            hidx = _history_index.HistoryIndex.load_or_build(
                cfg.execution_dir.parent.parent
                if cfg.execution_dir.parent.parent.name not in
                ("output", "")
                else cfg.execution_dir.parent
            )
            hidx.save()
            r14 = workflow_rules.check_streak_break(metadata, hidx)
            if r14:
                log.info("R14 status-streak break — emitting %d finding(s)",
                         len(r14))
                audit = merge_rule_findings(audit, r14)
            r17 = workflow_rules.check_immediate_prior_executed(
                metadata, hidx)
            if r17:
                log.info("R17 prior-executed-now-NA — emitting %d finding(s)",
                         len(r17))
                audit = merge_rule_findings(audit, r17)
        except Exception as e:
            log.warning("R14/R17 history-aware check failed: %s: %s",
                        type(e).__name__, e)

        # Stamp schema_version + variant_name + audited_at last so they
        # survive every code path above. UTC ISO + Z for round-trip parity.
        audit["schema_version"] = AUDIT_SCHEMA_VERSION
        audit["variant_name"] = cfg.variant_name
        import datetime as _dt
        audit["audited_at"] = (
            _dt.datetime.now(_dt.timezone.utc)
               .isoformat(timespec="seconds")
               .replace("+00:00", "Z")
        )

        # Drop deprecated-R3 FPs, enrich v6 triage fields, then reconcile
        # verdict with the final finding severities.
        audit = _drop_deprecated_blocked_findings(audit, metadata)
        audit = enrich_audit(audit, metadata)
        audit = _finalize_verdict(audit)

        # Archive any prior audit before overwriting. Layer 2 forensic
        # trail is desirable, not safety-critical — never blocks a write.
        try:
            _archive_previous_audit(output_dir)
        except Exception as arc_err:
            log.warning(
                "could not archive previous audit in %s: %s: %s — "
                "writing fresh audit anyway",
                output_dir, type(arc_err).__name__, arc_err,
            )

        out_json = output_dir / "audit.json"
        out_md = output_dir / "audit.md"
        out_json.write_text(json.dumps(audit, indent=2))
        out_md.write_text(render_markdown(audit, metadata, debug=cfg.debug_output))

        log.info("verdict: %s", audit.get("overall_verdict"))
        log.info("findings: %d", len(audit.get("findings") or []))
        log.info("wrote %s", out_json)
        log.info("wrote %s", out_md)
    except Exception as e:
        log.error("audit failed for %s: %s: %s",
                  cfg.execution_dir, type(e).__name__, e)
        _write_last_error(output_dir, cfg, exception=e)
        raise  # propagate so batch.py / CLI treat it as failed

    _clear_last_error(output_dir)  # success — clear stale error on retry
    return 0


def configure_logging(verbose: bool = False) -> None:
    """Delegate to extractor's formatter (single source of truth)."""
    import extractor  # local: avoid module-load cycle
    extractor.configure_logging(verbose)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ARGUS auditor")
    parser.add_argument("execution_dir", type=Path,
                        help="Output dir from extractor, e.g. output/23507_1403823")
    parser.add_argument("--config", type=Path, default=None,
                        help="path to config.toml (default: ./config.toml)")
    parser.add_argument("--debug", action="store_true",
                        help="include per-chunk breakdown and raw diagnostics "
                             "in audit.md (audit.json always carries full data)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    settings = argus_config.load(args.config)
    configure_logging(args.verbose or settings.logging.verbose)

    cfg = AuditConfig(
        execution_dir=args.execution_dir,
        model_provider=settings.auditor.model_provider,
        model_id=settings.auditor.model_id,
        region=settings.auditor.region,
        cloud_profile=settings.auditor.cloud_profile,
        api_base_url=settings.auditor.api_base_url,
        api_key_env=settings.auditor.api_key_env,
        max_pages=settings.auditor.max_pages,
        temperature=settings.auditor.temperature,
        debug_output=args.debug or settings.auditor.debug_output,
        chunk_max_parallel=settings.auditor.chunk_max_parallel,
        env_check_inline=settings.auditor.env_check_inline,
        env_check_sample_stride=settings.auditor.env_check_sample_stride,
        env_check_region_crop_height=settings.auditor.env_check_region_crop_height,
        env_check_region_crop_bottom_height=settings.auditor.env_check_region_crop_bottom_height,
        env_check_engine=settings.auditor.env_check_engine,
    )
    return run(cfg)


if __name__ == "__main__":
    sys.exit(main())
