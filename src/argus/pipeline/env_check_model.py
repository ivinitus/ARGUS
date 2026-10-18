"""Vision-model env-compliance check (Claude Haiku 5.5)."""
from __future__ import annotations

import base64
import copy
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Iterable


from argus.core import claude
from argus.core.costs import UsageTracker
from argus.domain import execution_policy, workflow_rules
from argus.pipeline import analysis_status, model_client, env_check

try:  # pragma: no cover
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

ENV_CHECK_VERSION = 29

DEFAULT_MODEL_ID = "claude-haiku-5-5"
DEFAULT_MALFORMED_RESPONSE_FALLBACK_MODEL_ID = (
    "claude-sonnet-5-5"
)
DEFAULT_MODEL_API_URL = model_client.DEFAULT_MODEL_API_URL

SYSTEM_PROMPT = """You are extracting URLs from a screenshot of a browser window.

You will be shown a cropped strip from a screenshot. The strip contains the browser's URL bar (top), and may also contain a status-bar hover URL (bottom-left of the original browser window) or a mobile browser's bottom URL bar.

YOUR ONLY JOB:
- List every URL that is VISIBLY DISPLAYED in the actual browser URL bar (chrome) or in a status-bar hover model_api_url.
- Do NOT include URLs that appear inside the page body (clickable links rendered as part of the web page content).
- Do NOT include URLs that appear in screenshots of other browser windows or developer tools.
- Do NOT include text from BROWSER TAB TITLES. Tab titles look like "Page Name | Example.ca" or "Account Details — Example.de" inside a browser tab — they are page-title text the site sets, NOT the active URL. The active URL only lives in the address/URL bar below the tabs. If only a tab title is visible (no address bar reading), return {"urls": []}.
- MULTIPLE TABS: the tab strip (the row of tabs above the address bar) is NEVER a source of URLs, full stop — it is not the address bar. Only the SELECTED/ACTIVE tab (visually highlighted — lighter background, connected to the address bar below it) corresponds to what's in the address bar; every other tab shown in the strip is a different, inactive page. If a tab's label happens to look like a URL or hostname (e.g. a second tab reading "example.in/checkout" or "example.in/typ"), that is tab-strip text, NOT an address-bar reading — treat it exactly like a tab title and leave it out of "urls" entirely. The address bar model_api_url (the long horizontal bar below the tab strip, usually starting with a lock/site icon) normally contains exactly ONE URL per screenshot. If you find yourself about to report two URLs from one screenshot, stop: one of them is almost certainly tab-strip text you mistook for a URL — re-examine and only keep the one actually inside the address bar.
- Do NOT synthesise a URL from a hostname-looking token. Extract only fully-formed URLs that you can see typed into the address bar (with or without scheme/path). A bare domain rendered inside text like "Privacy | Example.ca" is page-title decoration, not a URL.
- Do NOT guess or fabricate URLs that aren't clearly visible.
- EXACT TRANSCRIPTION (critical): copy the hostname CHARACTER-FOR-CHARACTER, including EVERY subdomain label. "jira.example.com/browse/X" must be reported as "jira.example.com/browse/X" — NEVER shortened to "example.com". "feature-preprod.example.co.jp/write-review" must keep its "feature-preprod." prefix — NEVER report it as "example.co.jp". Do not normalise, summarise, complete, or "clean up" the URL in any way; the leading subdomain is the single most important part of the reading. If the full hostname is too small or blurry to read every label with confidence, report exactly the characters you can see and set truncated_left=true — do NOT substitute a shorter host you infer.
- FIREFOX EMPHASIS AND TEXT SELECTION: Firefox can brighten only the registrable domain (for example "example.de") while dimming a real "feature-preprod." prefix. A selected or highlighted address-bar fragment can also hide neighboring text. Read the entire address bar, including dim text. If selection, highlighting, low contrast, or clipping prevents you from proving that the visible domain is the complete hostname, set truncated_left=true. Never report the bright fragment alone as a complete production URL.
- TRUNCATION DETECTION (critical for mobile browsers): mobile URL bars often hide the LEADING portion of a long URL behind a search/lock icon, so what's visible is just the END of the URL (e.g. "prod.example.com" when the actual URL is "feature-preprod.example.com"). When the visible text starts mid-URL with no ellipsis indicator, the URL is TRUNCATED at the start. Detect this by checking: is there a search/lock icon immediately to the LEFT of the URL text? Does the visible hostname START at the very edge of the text area, flush against that icon? Does the URL begin with a host-component that looks like the END of a longer subdomain (e.g. "prod" alone, "od" alone, "rod" alone, "preprod" alone)? Mark such URLs as truncated_left=true. When you are not sure whether the left edge of the hostname is fully visible, err on the side of truncated_left=true.
- Ellipsis-style truncation ("feature-pre…example.de") should be marked truncated_left=true as well.

OUTPUT FORMAT — respond with ONE JSON object and NOTHING ELSE. No prose, no markdown, no code fences. The first character must be `{` and the last must be `}`. Shape:

{
  "urls": [
    {"url": "https://feature-preprod.example.com/...", "truncated_left": false},
    {"url": "prod.example.com/cart", "truncated_left": true}
  ]
}

If no URL is visible in the URL bar / status-bar regions, return:
{"urls": []}

For backwards compatibility, plain string URLs are also accepted (treated as truncated_left=false), but PREFER the object form so truncation is explicit.
"""

WEBVIEW_CONTEXT_PROMPT = """You are verifying the environment used by a Webview test.

Inspect the FULL screenshot and list only Example URLs that are visibly shown by:
- chrome://inspect/#devices as the inspected WebView page URL, or
- a detached/embedded browser DevTools window's own address bar.

These URLs may appear in the middle of the screenshot. Ignore the main page body, email clients and email messages, browser tab titles, source code, cookies/storage rows, console/network rows, bookmarks, and ordinary clickable links. Do not infer or shorten a hostname. Keep every subdomain label, especially "feature-preprod".

Respond with one JSON object:
{"urls": [{"url": "https://feature-preprod.example.example/...", "truncated_left": false}]}

If no qualifying chrome://inspect or DevTools URL is clearly visible:
{"urls": []}
"""

BOTTOM_SOURCE_PROMPT = """Classify the source of a hostname shown near the bottom of this FULL screenshot.

Return "browser_status" only when the hostname is visibly in a desktop browser's bottom-left link-hover/status overlay.
Return "mobile_address_bar" only when it is visibly in a mobile browser's bottom address bar.
Return "email_client" when the screenshot is Outlook or another mail client and the hostname belongs to a sender, subject, inbox row, or email body.
Return "page_content" when the hostname is ordinary rendered content rather than browser chrome.
Return "other" when none of the above is clear.

Respond with one JSON object and nothing else:
{"source": "browser_status|mobile_address_bar|email_client|page_content|other"}
"""

NAVIGATION_NOTICE_PROMPT = """You are checking whether a production URL is visible only because a QA test intentionally opened an approved informational page.

Use the full screenshot and the supplied candidate URL/source. Return true only when:
- source=address_bar and the MAIN LOADED PAGE is clearly one of the approved page labels, or
- source=hover and the visible hovered link clearly targets one of those labels.

Do not approve a normal storefront, checkout, account, search, product, or playback page merely because it contains a privacy/legal link somewhere. When uncertain, return false.

Respond with one JSON object:
{"is_notice": true|false, "label": "<one approved label or empty>"}
"""



_MALFORMED_RESPONSE_ATTEMPTS = 3
_REPAIR_RESPONSE_CHAR_LIMIT = 4096
_URL_TOOL_NAME = "report_urls"
_SOURCE_TOOL_NAME = "report_source"
_HOSTNAME_TOOL_NAME = "report_hostname"
_FIREFOX_HOSTNAME_TOOL_NAME = "report_firefox_hostname"

_URL_TOOL = {
    "name": _URL_TOOL_NAME,
    "description": "Report URLs visibly displayed in browser chrome.",
    "input_schema": {
        "type": "object",
        "properties": {
            "urls": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "truncated_left": {"type": "boolean"},
                    },
                    "required": ["url", "truncated_left"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["urls"],
        "additionalProperties": False,
    },
}

_SOURCE_TOOL = {
    "name": _SOURCE_TOOL_NAME,
    "description": "Classify the source of a hostname near the image bottom.",
    "input_schema": {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "enum": [
                    "browser_status",
                    "mobile_address_bar",
                    "email_client",
                    "page_content",
                    "other",
                ],
            },
        },
        "required": ["source"],
        "additionalProperties": False,
    },
}

_HOSTNAME_TOOL = {
    "name": _HOSTNAME_TOOL_NAME,
    "description": "Report the exact hostname visible in browser chrome.",
    "input_schema": {
        "type": "object",
        "properties": {
            "host": {"type": ["string", "null"]},
            "complete": {"type": "boolean"},
        },
        "required": ["host", "complete"],
        "additionalProperties": False,
    },
}

_FIREFOX_HOSTNAME_TOOL = {
    "name": _FIREFOX_HOSTNAME_TOOL_NAME,
    "description": (
        "Report the complete Firefox address-bar hostname and any text "
        "shown to the left of the configured storefront domain."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "host": {"type": ["string", "null"]},
            "complete": {"type": "boolean"},
            "left_prefix": {"type": ["string", "null"]},
            "prefix_confidence": {
                "type": "string",
                "enum": ["high", "low"],
            },
        },
        "required": [
            "host",
            "complete",
            "left_prefix",
            "prefix_confidence",
        ],
        "additionalProperties": False,
    },
}


def _get_client(model_api_url: str, api_key_env: str | None):
    return model_client.create_client(
        model_api_url=model_api_url, api_key_env=api_key_env, timeout=60,
    )


def _image_to_base64_jpeg(img: "Image.Image", quality: int = 85) -> str:
    """Encode a PIL Image as base64 JPEG."""
    buf = BytesIO()
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.save(buf, format="JPEG", quality=quality)
    return base64.standard_b64encode(buf.getvalue()).decode("ascii")


def _response_text(raw: dict[str, Any]) -> str:
    blocks = raw.get("content")
    if not isinstance(blocks, list):
        raise ValueError("response content must be a list")
    text_parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            raise ValueError("response content block must be an object")
        if block.get("type") == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise ValueError("response text block must contain text")
            text_parts.append(text)
    text = "".join(text_parts).strip()
    if not text:
        raise ValueError("response contained no text")
    return text


def _response_value(
    raw: dict[str, Any],
    tool_name: str | None,
) -> tuple[Any, str]:
    """Read one forced tool input, with text JSON as a compatibility fallback."""
    blocks = raw.get("content")
    if not isinstance(blocks, list):
        raise ValueError("response content must be a list")
    if tool_name is not None:
        tool_blocks = [
            block
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ]
        if tool_blocks:
            if len(tool_blocks) != 1:
                raise ValueError("response contained multiple tool calls")
            tool_block = tool_blocks[0]
            if tool_block.get("name") != tool_name:
                raise ValueError("response called an unexpected tool")
            value = tool_block.get("input")
            if not isinstance(value, dict):
                raise ValueError("tool input must be an object")
            return value, json.dumps(value, separators=(",", ":"))

    text = _response_text(raw)
    return model_client.parse_single_json_object(text), text


def _repair_request_body(
    body: dict[str, Any],
    invalid_response: str,
    error: str,
    tool_name: str | None = None,
) -> dict[str, Any]:
    """Add one explicit repair turn while preserving the source image."""
    repaired = copy.deepcopy(body)
    messages = repaired.get("messages")
    if not isinstance(messages, list):
        messages = []
        repaired["messages"] = messages
    if invalid_response:
        messages.append({
            "role": "assistant",
            "content": [{
                "type": "text",
                "text": invalid_response[:_REPAIR_RESPONSE_CHAR_LIMIT],
            }],
        })
    messages.append({
        "role": "user",
        "content": [{
            "type": "text",
            "text": (
                (
                    f"Your previous response was invalid: {error}. "
                    f"Call the {tool_name} tool exactly once with input "
                    "matching its schema. Return no prose or additional "
                    "tool calls."
                )
                if tool_name is not None
                else (
                    f"Your previous response was invalid: {error}. "
                    "Return exactly ONE complete JSON object matching the "
                    "required output schema. Return no prose, markdown, code "
                    "fences, alternatives, or second JSON object."
                )
            ),
        }],
    })
    return repaired


def _invoke_strict_json(
    client: Any,
    *,
    model_id: str,
    body: dict[str, Any],
    operation: str,
    validator: Callable[[Any], Any],
    usage_tracker: UsageTracker | None,
    tool_name: str | None = None,
    malformed_response_fallback_model_id: str | None = None,
) -> Any:
    """Invoke with guarded bounded retries for malformed output only."""
    last_error = "model returned invalid JSON"
    request_body = body
    attempt_models = [model_id] * _MALFORMED_RESPONSE_ATTEMPTS
    if (
        malformed_response_fallback_model_id
        and malformed_response_fallback_model_id != model_id
    ):
        attempt_models.append(malformed_response_fallback_model_id)
    for attempt, attempt_model_id in enumerate(attempt_models):
        response_text = ""
        invocation_body = claude.prepare_request(
            request_body, model_id=attempt_model_id,
        )
        try:
            response = model_client.invoke_model(
                client,
                model_id=attempt_model_id,
                body=json.dumps(invocation_body),
                content_type="application/json",
                accept="application/json",
                allow_untagged_multimodal=True,
            )
        except Exception:
            # Invocation, access, throttling, and policy failures remain
            # fail-closed and are never retried through an alternate path.
            raise
        try:
            payload = response["body"].read()
            raw = json.loads(payload)
            if not isinstance(raw, dict):
                raise ValueError("model API response body must be an object")
            if usage_tracker is not None:
                usage_tracker.record_response(
                    raw,
                    model_id=attempt_model_id,
                    operation=(
                        operation
                        if attempt == 0
                        else (
                            f"{operation}_fallback"
                            if attempt_model_id != model_id
                            else f"{operation}_repair"
                        )
                    ),
                )
            try:
                response_text = _response_text(raw)
            except ValueError:
                # Forced tool responses normally contain no text. Capturing
                # text here preserves malformed legacy output for repair.
                pass
            parsed, canonical_response = _response_value(raw, tool_name)
            response_text = canonical_response
            return validator(parsed)
        except (KeyError, TypeError, UnicodeDecodeError,
                json.JSONDecodeError, ValueError) as exc:
            last_error = str(exc)
            if attempt + 1 < len(attempt_models):
                next_model_id = attempt_models[attempt + 1]
                log.warning(
                    "haiku env_check: %s response invalid; bounded %s: %s",
                    operation,
                    (
                        "Sonnet fallback"
                        if next_model_id != model_id
                        else "corrective retry"
                    ),
                    last_error,
                )
                request_body = _repair_request_body(
                    body,
                    response_text,
                    last_error,
                    tool_name,
                )
                continue
    raise analysis_status.AnalysisIncompleteError(
        "environment",
        f"{operation} response remained invalid after "
        f"{len(attempt_models)} attempts: {last_error}",
    )


def _validate_urls_response(parsed: Any) -> list[str]:
    if not isinstance(parsed, dict) or "urls" not in parsed:
        raise ValueError("URL response must contain the urls field")
    raw_urls = parsed["urls"]
    if not isinstance(raw_urls, list):
        raise ValueError("urls must be a list")
    out: list[str] = []
    for item in raw_urls:
        if (
            not isinstance(item, dict)
            or "url" not in item
            or "truncated_left" not in item
        ):
            raise ValueError(
                "each URL must contain url and truncated_left fields"
            )
        url = item["url"]
        truncated = item["truncated_left"]
        if (
            not isinstance(url, str)
            or not url.strip()
            or not isinstance(truncated, bool)
        ):
            raise ValueError("URL fields have invalid types or values")
        out.append(url.strip() + ("#TRUNC" if truncated else ""))
    return out


def _validate_source_response(parsed: Any) -> str:
    if not isinstance(parsed, dict) or "source" not in parsed:
        raise ValueError("source response must contain source")
    source = parsed["source"]
    if source not in {
        "browser_status",
        "mobile_address_bar",
        "email_client",
        "page_content",
        "other",
    }:
        raise ValueError("source response contains an unsupported value")
    return source


def _validate_navigation_response(parsed: Any) -> tuple[bool, str]:
    if not isinstance(parsed, dict):
        raise ValueError("navigation response must be an object")
    is_notice = parsed.get("is_notice")
    label = parsed.get("label")
    if not isinstance(is_notice, bool) or not isinstance(label, str):
        raise ValueError(
            "navigation response must contain boolean is_notice and string label"
        )
    return is_notice, label.strip()


def _validate_hostname_response(parsed: Any) -> tuple[str, bool] | None:
    if (
        not isinstance(parsed, dict)
        or "host" not in parsed
        or "complete" not in parsed
    ):
        raise ValueError("hostname response must contain host and complete")
    host = parsed["host"]
    complete = parsed["complete"]
    if not isinstance(complete, bool):
        raise ValueError("complete must be boolean")
    if host is None:
        if complete:
            raise ValueError("a missing host cannot be complete")
        return None
    if not isinstance(host, str) or not host.strip():
        raise ValueError("host must be a non-empty string or null")
    return host.strip(), complete


def _validate_firefox_hostname_response(
    parsed: Any,
) -> tuple[str, bool, str | None, str] | None:
    if (
        not isinstance(parsed, dict)
        or "host" not in parsed
        or "complete" not in parsed
        or "left_prefix" not in parsed
        or "prefix_confidence" not in parsed
    ):
        raise ValueError(
            "Firefox hostname response must contain host, complete, "
            "left_prefix, and prefix_confidence"
        )
    host = parsed["host"]
    complete = parsed["complete"]
    prefix = parsed["left_prefix"]
    confidence = parsed["prefix_confidence"]
    if not isinstance(complete, bool):
        raise ValueError("complete must be boolean")
    if confidence not in {"high", "low"}:
        raise ValueError("prefix_confidence must be high or low")
    if prefix is not None and not isinstance(prefix, str):
        raise ValueError("left_prefix must be a string or null")
    if host is None:
        if complete:
            raise ValueError("a missing host cannot be complete")
        return None
    if not isinstance(host, str) or not host.strip():
        raise ValueError("host must be a non-empty string or null")
    return (
        host.strip(),
        complete,
        prefix.strip().rstrip(".") if isinstance(prefix, str) else None,
        confidence,
    )


def _extract_urls_haiku(
    img: "Image.Image",
    model_id: str,
    model_api_url: str,
    api_key_env: str | None,
    usage_tracker: UsageTracker | None = None,
    *,
    system_prompt: str = SYSTEM_PROMPT,
    instruction: str = "Extract URLs visible in the URL bar / status bar regions.",
) -> list[str]:
    """Send one cropped image to Haiku, parse the URL list back."""
    if Image is None:  # pragma: no cover
        raise analysis_status.AnalysisIncompleteError(
            "environment", "Pillow is unavailable")
    try:
        b64 = _image_to_base64_jpeg(img)
    except Exception as exc:  # pragma: no cover
        raise analysis_status.AnalysisIncompleteError(
            "environment",
            f"image encoding failed: {type(exc).__name__}: {exc}",
        ) from exc
    client = _get_client(model_api_url, api_key_env)
    body = {

        "max_tokens": 1024,
        "temperature": 0.0,
        "system": [{
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }],
        "messages": [{
            "role": "user",
            "content": [{
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": b64,
                },
            }, {
                "type": "text",
                "text": instruction,
            }],
        }],
        "tools": [_URL_TOOL],
        "tool_choice": {"type": "tool", "name": _URL_TOOL_NAME},
    }
    return _invoke_strict_json(
        client,
        model_id=model_id,
        body=body,
        operation="env_check",
        validator=_validate_urls_response,
        usage_tracker=usage_tracker,
        tool_name=_URL_TOOL_NAME,
        malformed_response_fallback_model_id=(
            DEFAULT_MALFORMED_RESPONSE_FALLBACK_MODEL_ID
        ),
    )


def _classify_bottom_source(
    img: "Image.Image",
    model_id: str,
    model_api_url: str,
    api_key_env: str | None,
    usage_tracker: UsageTracker | None = None,
) -> str | None:
    """Classify whether a bottom-strip hostname belongs to browser chrome."""
    if Image is None:  # pragma: no cover
        raise analysis_status.AnalysisIncompleteError(
            "environment", "Pillow is unavailable")
    try:
        b64 = _image_to_base64_jpeg(img)
    except Exception as exc:  # pragma: no cover
        raise analysis_status.AnalysisIncompleteError(
            "environment",
            f"bottom-source image encoding failed: "
            f"{type(exc).__name__}: {exc}",
        ) from exc
    body = {

        "max_tokens": 128,
        "temperature": 0.0,
        "system": BOTTOM_SOURCE_PROMPT,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64",
                 "media_type": "image/jpeg", "data": b64}},
                {"type": "text",
                 "text": "Classify the source of the bottom hostname."},
            ],
        }],
        "tools": [_SOURCE_TOOL],
        "tool_choice": {"type": "tool", "name": _SOURCE_TOOL_NAME},
    }
    client = _get_client(model_api_url, api_key_env)
    return _invoke_strict_json(
        client,
        model_id=model_id,
        body=body,
        operation="env_bottom_context",
        validator=_validate_source_response,
        usage_tracker=usage_tracker,
        tool_name=_SOURCE_TOOL_NAME,
        malformed_response_fallback_model_id=(
            DEFAULT_MALFORMED_RESPONSE_FALLBACK_MODEL_ID
        ),
    )


def _normalise_notice_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _classify_navigation_notice(
    img: "Image.Image",
    urls: list[str],
    source: str,
    model_id: str,
    model_api_url: str,
    api_key_env: str | None,
    usage_tracker: UsageTracker | None = None,
) -> str | None:
    """Return an approved informational-page label when visually proved."""
    labels = env_check._NAVIGATION_NOTICE_LABELS
    if not labels or not urls:
        return None
    normalised_urls = " ".join(
        _normalise_notice_text(url)
        for url in urls
    )
    for label in labels:
        if _normalise_notice_text(label) in normalised_urls:
            return label
    try:
        b64 = _image_to_base64_jpeg(img)
    except Exception as exc:  # pragma: no cover
        raise analysis_status.AnalysisIncompleteError(
            "environment",
            "navigation notice image encoding failed: "
            f"{type(exc).__name__}: {exc}",
        ) from exc
    body = {

        "max_tokens": 128,
        "temperature": 0.0,
        "system": NAVIGATION_NOTICE_PROMPT,
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": b64,
                    },
                },
                {
                    "type": "text",
                    "text": (
                        f"Source: {source}\n"
                        f"Candidate URLs: {', '.join(urls)}\n"
                        f"Approved labels: {', '.join(labels)}"
                    ),
                },
            ],
        }],
    }
    client = _get_client(model_api_url, api_key_env)
    is_notice, returned_label = _invoke_strict_json(
        client,
        model_id=model_id,
        body=body,
        operation="env_navigation_notice",
        validator=_validate_navigation_response,
        usage_tracker=usage_tracker,
        malformed_response_fallback_model_id=(
            DEFAULT_MALFORMED_RESPONSE_FALLBACK_MODEL_ID
        ),
    )
    if not is_notice:
        return None
    returned = _normalise_notice_text(returned_label)
    for label in labels:
        normalised = _normalise_notice_text(label)
        if returned == normalised or returned.startswith(normalised):
            return label
    return None


def _process_webview_context_image(
    path: Path,
    model_id: str,
    model_api_url: str,
    api_key_env: str | None,
    usage_tracker: UsageTracker | None = None,
) -> dict[str, Any]:
    """Recover preprod URLs shown mid-frame in chrome://inspect/DevTools."""
    urls: list[str] = []
    if Image is not None:
        try:
            with Image.open(path) as raw_img:
                image = (
                    raw_img.convert("RGB")
                    if raw_img.mode not in ("RGB", "L")
                    else raw_img
                )
                urls = _extract_urls_haiku(
                    image, model_id, model_api_url, api_key_env, usage_tracker,
                    system_prompt=WEBVIEW_CONTEXT_PROMPT,
                    instruction=(
                        "Extract only chrome://inspect or DevTools "
                        "Example URLs visible in this full screenshot."
                    ),
                )
        except (OSError, ValueError) as exc:  # pragma: no cover
            raise analysis_status.AnalysisIncompleteError(
                "environment",
                f"Webview image processing failed for {path.name}: "
                f"{type(exc).__name__}: {exc}",
            ) from exc
    preprod: list[str] = []
    for url in urls:
        matched, cls = _classify_url_string(url)
        if cls == "preprod":
            preprod.append(matched or url)
    return {"image": str(path), "preprod_urls": preprod}


_PAGE_BODY_MARKER_PROMPT_TMPL = """You are looking for ENVIRONMENT-MARKER hostnames anywhere in this FULL screenshot.

The team's non-production hostnames contain one of these markers as part of the hostname: {markers}. Example shapes: "{example_marker}.{example_domain}.co.uk", "xyz-{example_marker}.dub.{example_domain}.com".

Report every hostname CONTAINING one of those markers that is visibly rendered ANYWHERE on the page — including page-body text, cookie-override/debug pages, cookie VALUES shown in a text field, settings or diagnostics screens, and toast/banner text. This is the one check where page-body text COUNTS.

Read the hostname character-for-character. Report only hostnames that visibly contain a configured marker. Never infer or guess.
"""


def _page_body_marker_prompt() -> str:
    """Render the full-frame marker prompt from active team policy."""
    markers = ", ".join(
        f'"{marker}"'
        for marker in env_check._NONPROD_HOST_MARKERS
    )
    return _PAGE_BODY_MARKER_PROMPT_TMPL.format(
        markers=markers,
        example_marker=env_check._NONPROD_HOST_MARKERS[0],
        example_domain=env_check._STOREFRONT_DOMAINS[0],
    )


def _page_body_views(img: "Image.Image") -> list["Image.Image"]:
    """Return overlapping enlarged views so small debug text stays legible."""
    target = 1560
    width, height = img.size

    def enlarge(view: "Image.Image") -> "Image.Image":
        view_width, view_height = view.size
        scale = target / max(view_width, view_height)
        if scale > 1.01:
            view = view.resize(
                (
                    round(view_width * scale),
                    round(view_height * scale),
                ),
                Image.Resampling.LANCZOS,
            )
        return view

    if width >= height and width > 1200:
        half = width // 2
        return [
            enlarge(img.crop((x, 0, min(x + half, width), height)))
            for x in (width // 4, 0, width - half)
        ]
    if height > width and height > 1200:
        half = height // 2
        return [
            enlarge(img.crop((0, y, width, min(y + half, height))))
            for y in (height // 4, 0, height - half)
        ]
    return [enlarge(img)]


def _process_page_body_marker_image(
    path: Path,
    model_id: str,
    model_api_url: str,
    api_key_env: str | None,
    usage_tracker: UsageTracker | None = None,
) -> dict[str, Any]:
    """Recover preprod proof from configured marker hostnames in page content."""
    if Image is None:  # pragma: no cover
        raise analysis_status.AnalysisIncompleteError(
            "environment", "Pillow is unavailable")
    preprod: list[str] = []
    try:
        with Image.open(path) as raw_img:
            image = (
                raw_img.convert("RGB")
                if raw_img.mode not in ("RGB", "L")
                else raw_img
            )
            prompt = _page_body_marker_prompt()
            for view in _page_body_views(image):
                urls = _extract_urls_haiku(
                    view,
                    model_id,
                    model_api_url,
                    api_key_env,
                    usage_tracker,
                    system_prompt=prompt,
                    instruction=(
                        "Extract environment-marker hostnames visible "
                        "anywhere in this full screenshot."
                    ),
                )
                for url in urls:
                    matched, cls = _classify_url_string(url)
                    if cls == "preprod":
                        preprod.append(matched or url)
                if preprod:
                    break
    except (OSError, ValueError) as exc:  # pragma: no cover
        raise analysis_status.AnalysisIncompleteError(
            "environment",
            f"page-body marker scan failed for {path.name}: "
            f"{type(exc).__name__}: {exc}",
        ) from exc
    return {"image": str(path), "preprod_urls": preprod}


def _classify_url_string(
    url: str,
    *,
    trust_truncated_prod: bool = False,
) -> tuple[str, str]:
    """Pick (matched_text, classification) for a URL string from Haiku.

    Left-truncated reads (#TRUNC sentinel) normally can't be trusted from
    the visible subdomain alone: mobile URL bars often hide "feature-pre",
    leaving what looks like a prod host. A visible preprod marker remains
    preprod; a prod-looking host downgrades to "ignore" unless the caller
    knows this is a desktop browser, where the hostname is not left-clipped.
    """
    truncated_left = url.endswith("#TRUNC")
    text = url[:-len("#TRUNC")].strip() if truncated_left else url.strip()
    text_norm = text
    text_norm = text_norm.replace("…", "<trunc>")
    text_norm = text_norm.replace("...", "<trunc>")
    domain_alternation = env_check._domain_alternation(
        env_check._STOREFRONT_DOMAINS)
    for prefix, marker in env_check._TRUNC_PREFIXES:
        truncation = re.compile(
            rf"{re.escape(prefix)}<trunc>"
            rf"((?:{domain_alternation})\.)",
            re.IGNORECASE,
        )
        if truncation.search(text_norm):
            text_norm = truncation.sub(rf"{marker}.\1", text_norm)
            break
    text_norm = text_norm.replace("<trunc>", "")

    text_norm = env_check._normalise_ocr_text(text_norm)
    for m in env_check._URL_TOKEN_RX.finditer(text_norm):
        subdomain = m.group(1) or ""
        tld = (m.group(2) or "").lower()
        if tld not in env_check._STOREFRONT_TLDS:
            continue
        cls = env_check._classify_host(subdomain)
        full_url = text_norm[m.start():].strip()
        cls = env_check._apply_host_policy(m.group(0), cls, full_url)
        if truncated_left and cls == "prod" and not trust_truncated_prod:
            return (m.group(0), "ignore")
        sub_clean = (subdomain or "").lower().strip(".")
        tail_markers = (
            ("feature-preprod",)
            if env_check._NONPROD_HOST_MARKERS
            == env_check._DEFAULT_NONPROD_MARKERS
            else env_check._NONPROD_HOST_MARKERS
        )
        if (
            cls == "prod"
            and sub_clean
            and any(marker.endswith(sub_clean) for marker in tail_markers)
        ):
            return (m.group(0), "ignore")
        return (full_url, cls)
    return ("", "ignore")


_BARE_APEX_RX = re.compile(r"^(?:https?://)?example\.[a-z]{2,3}(?:\.[a-z]{2,3})?/?$",
                           re.IGNORECASE)

_APEX_ECHO_RX = re.compile(
    r"^(?:https?://)?(?:www\.)?example\.([a-z]{2,3}(?:\.[a-z]{2,3})?)/?$",
    re.IGNORECASE,
)


def _same_host(a: str, b: str) -> bool:
    """True when two URL readings reference the same hostname (scheme, trailing slash, and case insensitive)."""
    def norm(u: str) -> str:
        u = (u or "").strip().lower()
        u = re.sub(r"^https?://", "", u)
        return u.split("/", 1)[0]
    return bool(norm(a)) and norm(a) == norm(b)


def _apex_tld(url: str) -> str | None:
    """TLD of an apex/www-apex storefront URL, or None if not that shape."""
    m = _APEX_ECHO_RX.match((url or "").strip())
    return m.group(1).lower() if m else None


def _storefront_tld(url: str) -> str | None:
    """Marketplace TLD of any Example URL shape, including paths."""
    text = env_check._normalise_ocr_text((url or "").strip())
    for match in env_check._URL_TOKEN_RX.finditer(text):
        tld = (match.group(2) or "").lower()
        if tld in env_check._STOREFRONT_TLDS:
            return tld
    return None


def _preprod_tlds(preprod_urls: Iterable[str]) -> set[str]:
    """The example.<tld> marketplaces referenced by preprod readings."""
    tlds: set[str] = set()
    for u in preprod_urls:
        for m in env_check._URL_TOKEN_RX.finditer(u):
            tld = (m.group(2) or "").lower()
            if tld in env_check._STOREFRONT_TLDS:
                tlds.add(tld)
    return tlds


def _suppress_apex_echoes(
    pp_top: list[str],
    pr_top: list[str],
) -> tuple[list[str], list[str]]:
    """Drop apex/www-apex prod reads that echo a same-strip preprod host.

    One cropped strip holds ONE address bar: a preprod host plus an
    apex-shaped prod host on the same example.<tld> means the prod entry
    is a transcription echo (a known transcription ambiguity) — a
    "feature-preprod." marker cannot be misread into existence, while a
    subdomain-drop hallucination yields a clean apex. Prod reads with a
    path or a non-www subdomain are never touched.
    """
    if not pp_top or not pr_top:
        return pr_top, []
    pp_tlds = _preprod_tlds(pp_top)
    kept: list[str] = []
    dropped: list[str] = []
    for u in pr_top:
        tld = _apex_tld(u)
        if tld is not None and tld in pp_tlds:
            dropped.append(u)
        else:
            kept.append(u)
    return kept, dropped


def _split_suspect_apex_prods(
    prod_urls_top: set[str],
    preprod_urls: set[str],
) -> tuple[set[str], set[str]]:
    """Split top-model_api_url prod URLs into (confirmed, suspect) — v16b.

    A BARE-apex prod read (no subdomain, no path — the shape Firefox's
    dimmed-subdomain URL bar produces) on a marketplace where this
    session also produced preprod readings is a suspected misread, not a
    confirmed violation. "www." hosts and anything
    with a path stay confirmed — over-flagging there is the accepted
    trade-off.
    """
    if not prod_urls_top or not preprod_urls:
        return set(prod_urls_top), set()
    pp_tlds = _preprod_tlds(preprod_urls)
    confirmed: set[str] = set()
    suspect: set[str] = set()
    for u in prod_urls_top:
        if _BARE_APEX_RX.match(u.strip()) and _apex_tld(u) in pp_tlds:
            suspect.add(u)
        else:
            confirmed.add(u)
    return confirmed, suspect

_CONFIRM_PROMPT = """You are reading ONE thing from a cropped browser screenshot: the HOSTNAME shown in the address bar.

Transcribe the hostname CHARACTER-FOR-CHARACTER, including every subdomain label before "web" (e.g. "jira.", "www.", "feature-preprod."). Read carefully even if the font is small. Do not shorten, complete, or clean up the text. Ignore tab titles, bookmarks, page content, and DevTools panels — only the address bar hostname.

Respond with ONE JSON object and NOTHING ELSE:
{"host": "<exact hostname text you see>", "complete": <true if the hostname's left edge is fully visible, false if it is cut off or you are unsure>}

If no address bar hostname is visible: {"host": null, "complete": false}
"""

_CONFIRM_FIREFOX_PROMPT = """You are visually verifying the complete HOSTNAME in a Firefox address bar.

You may receive a full screenshot for browser context, followed by TWO
versions of the same address-bar crop: an enlarged color version and a
contrast-enhanced grayscale version. Use the full screenshot only to locate
the real active address bar; transcribe from the enlarged crops. Firefox
deliberately dims the scheme, subdomains, and path while making the
registrable domain brighter. The bright text is NOT necessarily the full
hostname. Inspect the dim text immediately to its LEFT in both crops and use
the browser layout, contrast, and selection state to decide whether that text
belongs to the active hostname.

Configured storefront labels: {storefront_labels}
Known policy hostnames: {policy_hosts}

The policy hostnames are context, not answers: report one only when the image
actually supports it. Firefox may make a host such as "d.portal.com" look like
the bright fragment "portal.com" with a dim "d." prefix.

Return:
- host: the complete hostname, preserving every label, especially
  dimmed labels before the bright registrable domain.
- complete: true only when the hostname's left edge and all dimmed labels are
  fully visible.
- left_prefix: the exact labels before the matching configured storefront
  label, without a trailing dot; for example "d" for "d.portal.com" or
  "gf.aka" for "gf.aka.example.com". Use "" when there is definitely no
  prefix and null when uncertain.
- prefix_confidence: high only when the enhanced image makes the prefix or
  absence of a prefix unambiguous.

Ignore browser tabs, bookmarks, page content, hover/status URLs, and the
Windows/macOS taskbar. Never shorten a hostname to its bright registrable
domain merely because Firefox highlights that fragment. If address-bar text
is selected, highlighted, obscured, clipped, or low-contrast enough that
neighboring characters could be hidden, set complete=false, left_prefix=null,
and prefix_confidence="low". Do not call an incomplete bright fragment a
production hostname.
"""


def _configured_context_hosts() -> tuple[str, ...]:
    """Exact environment hosts configured for the active api_key_env."""
    hosts = {
        host
        for _, configured in env_check._EXPECTED_HOSTS_BY_RUN
        for host in configured
    }
    hosts.update(
        host
        for configured in env_check._ALLOWED_HOST_EXCEPTIONS.values()
        for host in configured
    )
    hosts.update(
        host
        for configured in env_check._REQUIRED_HOST_OVERRIDES.values()
        for host in configured
    )
    return tuple(sorted(hosts))


def _firefox_confirmation_prompt() -> str:
    """Build a storefront-aware visual reasoning prompt for Firefox."""
    storefronts = tuple(env_check._STOREFRONT_DOMAINS)
    return _CONFIRM_FIREFOX_PROMPT.format(
        storefront_labels=", ".join(storefronts) or "(none configured)",
        policy_hosts=(
            ", ".join(_configured_context_hosts())
            or "(no exact host policy configured)"
        ),
    )

_CONFIRM_BOTTOM_PROMPT = """You are reading ONE thing from a cropped strip taken from the BOTTOM of a browser-window screenshot: the hostname of any URL shown there — a status-bar link-hover / network message (e.g. "Transferring data from <host>...", bottom-left) or a mobile browser's bottom address bar.

Transcribe that hostname CHARACTER-FOR-CHARACTER, including every label (e.g. "example.sc.omtrdc.net", "www.example.co.uk", "feature-preprod.example.fr"). Do not shorten, complete, or substitute a similar-looking domain — telemetry hosts like "example.sc.omtrdc.net" must NOT be rewritten to an example.<tld> site. Ignore OS taskbars, clocks, weather widgets, and page content. If the text is too small to read every character with confidence, set complete=false and report exactly the characters you can see.

Respond with ONE JSON object and NOTHING ELSE:
{"host": "<exact hostname text you see>", "complete": <true if every character is clearly legible, else false>}

If no URL/hostname is visible in the strip: {"host": null, "complete": false}
"""


def _confirm_hostname(img: "Image.Image", model_id: str, model_api_url: str,
                      api_key_env: str | None,
                      usage_tracker: UsageTracker | None = None,
                      system_prompt: str = _CONFIRM_PROMPT,
                      ) -> tuple[str, bool] | None:
    """Second-pass hostname read on a 2x-upscaled crop."""
    if Image is None:  # pragma: no cover
        return None
    try:
        w, h = img.size
        up = img.resize((w * 2, h * 2), Image.Resampling.LANCZOS)
        b64 = _image_to_base64_jpeg(up)
    except (OSError, ValueError) as e:
        log.debug("hostname confirm upscale failed: %s: %s",
                  type(e).__name__, e)
        return None
    body = {

        "max_tokens": 256,
        "temperature": 0.0,
        "system": system_prompt,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64",
                 "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": "Transcribe the hostname as instructed."},
            ],
        }],
        "tools": [_HOSTNAME_TOOL],
        "tool_choice": {"type": "tool", "name": _HOSTNAME_TOOL_NAME},
    }
    client = _get_client(model_api_url, api_key_env)
    return _invoke_strict_json(
        client,
        model_id=model_id,
        body=body,
        operation="env_confirm",
        validator=_validate_hostname_response,
        usage_tracker=usage_tracker,
        tool_name=_HOSTNAME_TOOL_NAME,
        malformed_response_fallback_model_id=(
            DEFAULT_MALFORMED_RESPONSE_FALLBACK_MODEL_ID
        ),
    )


def _confirm_firefox_hostname(
    img: "Image.Image",
    model_id: str,
    model_api_url: str,
    api_key_env: str | None,
    usage_tracker: UsageTracker | None = None,
    *,
    context_img: "Image.Image | None" = None,
) -> tuple[str, bool, str | None, str] | None:
    """Read a Firefox hostname using full context plus enhanced crops."""
    if Image is None or ImageOps is None:  # pragma: no cover
        return None
    try:
        w, h = img.size
        up = img.convert("RGB").resize(
            (w * 2, h * 2), Image.Resampling.LANCZOS)
        enhanced = ImageOps.autocontrast(up.convert("L")).convert("RGB")
        color_b64 = _image_to_base64_jpeg(up)
        enhanced_b64 = _image_to_base64_jpeg(enhanced)
        context_b64: str | None = None
        if context_img is not None:
            context = context_img.convert("RGB").copy()
            context.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            context_b64 = _image_to_base64_jpeg(context)
    except (OSError, ValueError) as e:
        log.debug("Firefox hostname confirm enhancement failed: %s: %s",
                  type(e).__name__, e)
        return None
    image_content: list[dict[str, Any]] = []
    if context_b64 is not None:
        image_content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": context_b64,
            },
        })
    image_content.extend([
        {"type": "image", "source": {"type": "base64",
         "media_type": "image/jpeg", "data": color_b64}},
        {"type": "image", "source": {"type": "base64",
         "media_type": "image/jpeg", "data": enhanced_b64}},
    ])
    body = {

        "max_tokens": 256,
        "temperature": 0.0,
        "system": _firefox_confirmation_prompt(),
        "messages": [{
            "role": "user",
            "content": image_content + [
                {"type": "text", "text": (
                    "Both images show the same Firefox address bar. "
                    "Use any preceding full screenshot only as context. "
                    "Report the complete hostname and dim left prefix."
                )},
            ],
        }],
        "tools": [_FIREFOX_HOSTNAME_TOOL],
        "tool_choice": {
            "type": "tool",
            "name": _FIREFOX_HOSTNAME_TOOL_NAME,
        },
    }
    client = _get_client(model_api_url, api_key_env)
    return _invoke_strict_json(
        client,
        model_id=model_id,
        body=body,
        operation="env_confirm_firefox",
        validator=_validate_firefox_hostname_response,
        usage_tracker=usage_tracker,
        tool_name=_FIREFOX_HOSTNAME_TOOL_NAME,
        malformed_response_fallback_model_id=(
            DEFAULT_MALFORMED_RESPONSE_FALLBACK_MODEL_ID
        ),
    )


def _confirm_firefox_prods(
    pr_top: list[str],
    top_crop: "Image.Image | None",
    model_id: str,
    model_api_url: str,
    api_key_env: str | None,
    usage_tracker: UsageTracker | None = None,
    *,
    full_context: "Image.Image | None" = None,
) -> tuple[list[str], list[str], list[str]]:
    """Confirm Firefox prod-shaped reads without promoting ambiguity to R9."""
    if not pr_top:
        return [], [], []
    if top_crop is None:
        return [], [], list(pr_top)
    try:
        second = _confirm_firefox_hostname(
            top_crop,
            model_id,
            model_api_url,
            api_key_env,
            usage_tracker,
            context_img=full_context,
        )
    except analysis_status.AnalysisIncompleteError as exc:
        log.info("Firefox hostname confirmation inconclusive: %s", exc)
        return [], [], list(pr_top)
    if second is None:
        return [], [], list(pr_top)

    host2, complete, left_prefix, prefix_confidence = second
    prefix = (left_prefix or "").strip().lower()
    original_tlds = {
        tld for tld in (_storefront_tld(url) for url in pr_top)
        if tld is not None
    }

    if (
        prefix
        and complete
        and prefix_confidence == "high"
        and not host2.lower().startswith(f"{prefix}.")
    ):
        prefixed_host = f"{prefix}.{host2}"
        prefixed_match, prefixed_class = _classify_url_string(prefixed_host)
        if prefixed_match and prefixed_class in {"preprod", "prod"}:
            host2 = prefixed_host
    matched2, cls2 = _classify_url_string(
        host2 if complete else host2 + "#TRUNC")
    confirmed_tld = _storefront_tld(matched2 or host2)
    same_marketplace = (
        confirmed_tld is not None and confirmed_tld in original_tlds
    )
    if env_check.has_contextual_environment_policy():
        hostname = env_check._canonical_hostname(matched2 or host2)
        same_marketplace = same_marketplace and any(
            hostname == env_check._canonical_hostname(url)
            or hostname.endswith(f".{env_check._canonical_hostname(url)}")
            for url in pr_top
        )

    if cls2 == "preprod" and matched2 and same_marketplace:
        log.info(
            "haiku env_check: Firefox prod read(s) (%s) reclassified "
            "preprod on enhanced confirm pass ('%s')",
            ", ".join(pr_top), host2)
        return [], [matched2], []
    if (
        cls2 == "prod"
        and matched2
        and complete
        and same_marketplace
        and prefix_confidence == "high"
        and (
            bool(prefix)
            or (
                left_prefix == ""
                and env_check._canonical_hostname(matched2)
                in _configured_context_hosts()
                and any(_same_host(matched2, url) for url in pr_top)
            )
        )
    ):
        return [matched2], [], []

    log.info(
        "haiku env_check: Firefox prod read(s) (%s) downgraded as "
        "ambiguous after enhanced confirm pass ('%s', prefix=%r, %s)",
        ", ".join(pr_top), host2, left_prefix, prefix_confidence)
    return [], [], list(pr_top)


def _confirm_contextual_prods(
    pr_top: list[str],
    top_crop: "Image.Image | None",
    model_id: str,
    model_api_url: str,
    api_key_env: str | None,
    usage_tracker: UsageTracker | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Confirm contextual-policy candidates, including PORTAL hosts and paths."""
    if not pr_top:
        return [], [], []
    if top_crop is None:
        return [], [], list(pr_top)
    try:
        second = _confirm_hostname(
            top_crop, model_id, model_api_url, api_key_env, usage_tracker,
            system_prompt=_CONFIRM_PROMPT.replace(
                'before "web"', "in the hostname",
            ),
        )
    except analysis_status.AnalysisIncompleteError as exc:
        log.info("Contextual hostname confirmation inconclusive: %s", exc)
        return [], [], list(pr_top)
    if second is None or not second[1]:
        return [], [], list(pr_top)

    host, _ = second
    matched, classification = _classify_url_string(host)
    if matched:
        matching = [url for url in pr_top if _same_host(url, matched)]
        if classification == "prod" and matching:
            return matching, [], []
        # Recover a dropped prefix only when the independent read proves a
        # configured host extending the initial hostname, not another domain.
        hostname = env_check._canonical_hostname(matched)
        if hostname in _configured_context_hosts() and any(
            hostname.endswith(f".{env_check._canonical_hostname(url)}")
            for url in pr_top
        ):
            if classification == "preprod":
                return [], [matched], []
            if classification == "prod":
                return [matched], [], []
    return [], [], list(pr_top)


def _confirm_marketplace_urls(
    urls: list[str],
    crop: "Image.Image | None",
    expected_marketplace: str | None,
    model_id: str,
    model_api_url: str,
    api_key_env: str | None,
    usage_tracker: UsageTracker | None = None,
) -> tuple[list[str], list[str]]:
    """Verify a foreign active hostname before it can support R13.

    Do not show the expected country or candidate reading to the second
    reader. A corrected assigned host is usable; a foreign host must agree
    with the initial reading. Disagreement remains an advisory.
    """
    expected = str(expected_marketplace or "").strip().upper()
    if not expected:
        return list(urls), []
    foreign = [
        url for url in urls
        if workflow_rules._is_storefront_host(url)
        and workflow_rules._detect_marketplace_from_url(url) not in {None, expected}
    ]
    if not foreign:
        return list(urls), []
    remaining = [url for url in urls if url not in foreign]
    if crop is None:
        return remaining, foreign
    try:
        second = _confirm_hostname(
            crop, model_id, model_api_url, api_key_env, usage_tracker,
            system_prompt=(
                _CONFIRM_PROMPT
                + "\nRead the country suffix carefully. The hostname ends at "
                "the first slash after the scheme; category names, URL paths, "
                "and query parameters are not part of the hostname. Only "
                "the active address bar counts, never a hover/status link."
            ),
        )
    except analysis_status.AnalysisIncompleteError as exc:
        log.info("Marketplace hostname confirmation inconclusive: %s", exc)
        return remaining, foreign
    if second is None:
        return remaining, foreign
    host, complete = second
    if not complete or not workflow_rules._is_storefront_host(host):
        return remaining, foreign
    marketplace = workflow_rules._detect_marketplace_from_url(host)
    matching = [url for url in urls if _same_host(url, host)]
    if marketplace == expected:
        return matching or [host], []
    if marketplace and matching:
        return matching, []
    return remaining, foreign


def _confirm_bare_apex_prods(
    pr_top: list[str],
    top_crop: "Image.Image | None",
    model_id: str,
    model_api_url: str,
    api_key_env: str | None,
    usage_tracker: UsageTracker | None = None,
) -> tuple[list[str], list[str]]:
    """Re-verify bare-apex prod reads with a focused hostname pass.

    Fail-OPEN: any error, or a second read confirming the apex, keeps
    the original prod classification — on this path missing a real
    prod-navigation violation is the greater harm.
    """
    if top_crop is None or not any(_BARE_APEX_RX.match(u) for u in pr_top):
        return pr_top, []
    confirmed: list[str] = []
    recovered_pp: list[str] = []
    if usage_tracker is None:
        second = _confirm_hostname(top_crop, model_id, model_api_url, api_key_env)
    else:
        second = _confirm_hostname(
            top_crop, model_id, model_api_url, api_key_env, usage_tracker)
    for u in pr_top:
        if not _BARE_APEX_RX.match(u):
            confirmed.append(u)
            continue
        if second is None:
            confirmed.append(u)
            continue
        host2, complete = second
        matched2, cls2 = _classify_url_string(
            host2 if complete else host2 + "#TRUNC")
        if cls2 == "prod" and matched2:
            confirmed.append(u)
        elif cls2 == "preprod":
            recovered_pp.append(matched2)
            log.info("haiku env_check: bare-apex '%s' reclassified "
                     "preprod on confirm pass ('%s')", u, host2)
        else:
            log.info("haiku env_check: bare-apex '%s' dropped on "
                     "confirm pass ('%s' -> %s)", u, host2, cls2)
    return confirmed, recovered_pp


def _confirm_bottom_prods(
    pr_bot: list[str],
    bot_crop: "Image.Image | None",
    model_id: str,
    model_api_url: str,
    api_key_env: str | None,
    usage_tracker: UsageTracker | None = None,
) -> tuple[list[str], list[str]]:
    """Re-verify bottom-strip prod reads before they can fire R9-hover.

    Every bottom-strip production read requires independent confirmation
    from a focused, enlarged crop. Browser transfer-status text can show a
    tracking hostname that resembles a storefront host; an uncertain
    transcription must not become a violation.
    """
    if not pr_bot:
        return pr_bot, []
    if bot_crop is None:
        raise analysis_status.AnalysisIncompleteError(
            "environment",
            "bottom URL confirmation crop was unavailable",
        )
    second = _confirm_hostname(
        bot_crop, model_id, model_api_url, api_key_env, usage_tracker,
        system_prompt=_CONFIRM_BOTTOM_PROMPT)
    if second is None:
        raise analysis_status.AnalysisIncompleteError(
            "environment",
            "bottom URL confirmation could not reconcile a production read",
        )
    host2, complete = second
    matched2, cls2 = _classify_url_string(
        host2 if complete else host2 + "#TRUNC")
    if cls2 == "prod" and matched2:
        return pr_bot, []
    if cls2 == "preprod":
        log.info("haiku env_check: bottom prod read(s) (%s) reclassified "
                 "preprod on confirm pass ('%s')",
                 ", ".join(pr_bot), host2)
        return [], [matched2]
    if not complete:
        matched_raw, cls_raw = _classify_url_string(host2)
        if (cls_raw == "prod" and matched_raw
                and not _BARE_APEX_RX.match(matched_raw)
                and any(_same_host(matched_raw, u) for u in pr_bot)):
            log.info("haiku env_check: bottom prod read(s) (%s) kept — "
                     "incomplete confirm read agrees ('%s')",
                     ", ".join(pr_bot), host2)
            return pr_bot, []
    log.info("haiku env_check: bottom prod read(s) (%s) dropped on "
             "confirm pass ('%s' -> %s)", ", ".join(pr_bot), host2, cls2)
    return [], []


def _process_image(
    path: Path,
    model_id: str,
    model_api_url: str,
    api_key_env: str | None,
    region_crop_height: int,
    region_crop_bottom_height: int,
    usage_tracker: UsageTracker | None = None,
    expected_browser: str | None = None,
    expected_execution_mode: str | None = None,
    expected_marketplace: str | None = None,
) -> dict[str, Any]:
    """Crop, send to Haiku, classify URLs.

    Top strip = the active URL bar: a prod URL there is the tester's own
    navigation -> R9 (high). Bottom strip reads remain diagnostic only;
    hover/status targets and mobile bottom-bar ambiguity cannot prove active
    navigation. Desktop address bars retain prod reads that the model marks
    left-truncated because modern Chrome hides the scheme next to the site
    icon without clipping the hostname. With only one crop height configured,
    the single crop is treated as the active URL-bar model_api_url.
    """
    if Image is None:  # pragma: no cover
        return {"image": str(path), "urls": [], "preprod_urls": [],
                "prod_urls": [], "prod_urls_top": [],
                "prod_urls_bottom": [], "preprod_urls_top": [],
                "preprod_urls_bottom": [],
                "suspect_prod_urls_top": [],
                "marketplace_urls": [],
                "marketplace_uncertain_urls": [],
                "navigation_notices": []}
    top_urls: list[str] = []
    bottom_urls: list[str] = []
    confirm_crop: Any = None
    confirm_crop_bottom: Any = None
    bottom_context: Any = None
    full_context: Any = None
    navigation_context: Any = None
    try:
        with Image.open(path) as raw_img:
            image = (
                raw_img.convert("RGB")
                if raw_img.mode not in ("RGB", "L")
                else raw_img
            )
            full_context = image.copy()
            both_strips = (region_crop_height > 0
                           and region_crop_bottom_height > 0)
            if both_strips:
                top_crop = env_check._crop_url_bar_region(
                    image, region_crop_height, 0)
                if top_crop is not None:
                    confirm_crop = top_crop
                    top_urls = _extract_urls_haiku(
                        top_crop, model_id, model_api_url, api_key_env,
                        usage_tracker)
                bot_crop = env_check._crop_url_bar_region(
                    image, 0, region_crop_bottom_height)
                if bot_crop is not None:
                    confirm_crop_bottom = bot_crop
                    bottom_urls = _extract_urls_haiku(
                        bot_crop, model_id, model_api_url, api_key_env,
                        usage_tracker)
                    if bottom_urls:
                        bottom_context = image.copy()
            else:
                cropped = env_check._crop_url_bar_region(
                    image, region_crop_height, region_crop_bottom_height)
                img = cropped if cropped is not None else image
                confirm_crop = cropped
                top_urls = _extract_urls_haiku(
                    img, model_id, model_api_url, api_key_env, usage_tracker)
            if (top_urls or bottom_urls) and env_check._NAVIGATION_NOTICE_LABELS:
                navigation_context = image.copy()
    except analysis_status.AnalysisIncompleteError:
        raise
    except (OSError, ValueError) as exc:  # pragma: no cover
        raise analysis_status.AnalysisIncompleteError(
            "environment",
            f"image processing failed for {path.name}: "
            f"{type(exc).__name__}: {exc}",
        ) from exc

    def _classify(urls, *, trust_truncated_prod=False):
        preprod, prod = [], []
        for u in urls:
            matched, cls = _classify_url_string(
                u,
                trust_truncated_prod=trust_truncated_prod,
            )
            if cls == "preprod":
                preprod.append(matched or u)
            elif cls == "prod":
                prod.append(matched or u)
        return preprod, prod

    pp_top, pr_top = _classify(
        top_urls,
        trust_truncated_prod=(
            expected_execution_mode == execution_policy.DESKTOP_BROWSER
        ),
    )
    pp_bot, pr_bot = _classify(bottom_urls)
    suspect_pr_top: list[str] = []
    if pp_top and pr_top:
        pr_top, echoed = _suppress_apex_echoes(pp_top, pr_top)
        for u in echoed:
            log.info("haiku env_check: apex prod read '%s' dropped as "
                     "echo of same-strip preprod host (%s)", u, path)
    if pr_top:
        if "firefox" in str(expected_browser or "").strip().lower():
            pr_top, recovered_pp, suspect_pr_top = _confirm_firefox_prods(
                pr_top,
                confirm_crop,
                model_id,
                model_api_url,
                api_key_env,
                usage_tracker,
                full_context=full_context,
            )
        elif env_check.has_contextual_environment_policy():
            pr_top, recovered_pp, suspect_pr_top = _confirm_contextual_prods(
                pr_top, confirm_crop, model_id, model_api_url, api_key_env, usage_tracker,
            )
        else:
            pr_top, recovered_pp = _confirm_bare_apex_prods(
                pr_top,
                confirm_crop,
                model_id,
                model_api_url,
                api_key_env,
                usage_tracker,
            )
        pp_top.extend(u for u in recovered_pp if u not in pp_top)
    if pr_bot:
        pr_bot, recovered_pp_bot = _confirm_bottom_prods(
            pr_bot, confirm_crop_bottom, model_id, model_api_url, api_key_env,
            usage_tracker)
        pp_bot.extend(u for u in recovered_pp_bot if u not in pp_bot)
    if pr_bot:
        source = (
            _classify_bottom_source(
                bottom_context, model_id, model_api_url, api_key_env, usage_tracker)
            if bottom_context is not None else None
        )
        if source not in {"browser_status", "mobile_address_bar"}:
            log.info("haiku env_check: bottom prod read(s) (%s) dropped "
                     "because full-frame source is %s",
                     ", ".join(pr_bot), source or "unknown")
            pr_bot = []
    navigation_notices: list[dict[str, str]] = []
    if navigation_context is not None and pr_top:
        label = _classify_navigation_notice(
            navigation_context,
            top_urls,
            "address_bar",
            model_id,
            model_api_url,
            api_key_env,
            usage_tracker,
        )
        if label:
            navigation_notices.extend(
                {"url": url, "label": label, "source": "address_bar"}
                for url in pr_top
            )
            pr_top = []
    if navigation_context is not None and pr_bot:
        label = _classify_navigation_notice(
            navigation_context,
            bottom_urls,
            "hover",
            model_id,
            model_api_url,
            api_key_env,
            usage_tracker,
        )
        if label:
            navigation_notices.extend(
                {"url": url, "label": label, "source": "hover"}
                for url in pr_bot
            )
            pr_bot = []
    # Keep allowed production storefronts in marketplace confirmation while
    # excluding them from R9. Informational redirects above remain excluded
    # from both checks.
    marketplace_candidates = pp_top + pr_top
    for source, urls in (("address_bar", pr_top), ("hover", pr_bot)):
        approved = [url for url in urls if env_check.production_host_approved(url)]
        navigation_notices.extend(
            {"url": url, "label": "approved production storefront", "source": source}
            for url in approved
        )
        urls[:] = [url for url in urls if url not in approved]
    preprod_urls = pp_top + pp_bot
    prod_urls = pr_top + pr_bot
    marketplace_urls, marketplace_uncertain_urls = _confirm_marketplace_urls(
        marketplace_candidates,
        confirm_crop if confirm_crop is not None else full_context,
        expected_marketplace,
        model_id,
        model_api_url,
        api_key_env,
        usage_tracker,
    )
    return {
        "image": str(path),
        "urls": top_urls + bottom_urls,
        "preprod_urls": preprod_urls,
        "prod_urls": prod_urls,
        "preprod_urls_top": pp_top,
        "preprod_urls_bottom": pp_bot,
        "prod_urls_top": pr_top,
        "prod_urls_bottom": pr_bot,
        "suspect_prod_urls_top": suspect_pr_top,
        "marketplace_urls": marketplace_urls,
        "marketplace_uncertain_urls": marketplace_uncertain_urls,
        "navigation_notices": navigation_notices,
    }


def check_env_compliance(
    execution_dir: Path,
    *,
    sample_stride: int = 1,
    sample_ends: int = 3,
    region_crop_height: int = 0,
    region_crop_bottom_height: int = 0,
    model_id: str = DEFAULT_MODEL_ID,
    model_api_url: str = DEFAULT_MODEL_API_URL,
    api_key_env: str | None = None,
    max_workers: int = 8,
    is_webview: bool = False,
    is_prod_validation: bool = False,
    missing_url_is_advisory: bool = False,
    expected_browser: str | None = None,
    expected_execution_mode: str | None = None,
    expected_marketplace: str | None = None,
    usage_tracker: UsageTracker | None = None,
) -> dict[str, Any]:
    """Same signature + return shape as env_check.check_env_compliance."""
    all_images = list(env_check._iter_audit_images(execution_dir))
    images = env_check._select_sampled_images(
        all_images, sample_stride, sample_ends)

    preprod_urls: set[str] = set()
    prod_urls: set[str] = set()
    images_with_preprod = 0
    images_with_prod = 0
    prod_urls_top: set[str] = set()
    prod_urls_bottom: set[str] = set()
    images_with_prod_top = 0
    suspect_prods_top: set[str] = set()
    firefox_suspect_prods_top: set[str] = set()
    contextual_suspect_prods_top: set[str] = set()
    marketplace_urls: set[str] = set()
    marketplace_uncertain_urls: set[str] = set()
    navigation_notices: list[dict[str, str]] = []
    per_image: list[dict[str, Any]] = []

    if not images:
        verdict = "ambiguous"
    else:
        with ThreadPoolExecutor(max_workers=max_workers,
                                thread_name_prefix="haiku-env") as ex:
            futures = {
                ex.submit(_process_image, p, model_id, model_api_url, api_key_env,
                          region_crop_height, region_crop_bottom_height,
                          usage_tracker, expected_browser,
                          expected_execution_mode, expected_marketplace): p
                for p in images
            }
            for fut in as_completed(futures):
                rec = fut.result()
                if not rec["urls"]:
                    continue
                img_preprod = set(rec["preprod_urls"])
                img_prod = set(rec["prod_urls"])
                img_prod_top = set(rec.get("prod_urls_top") or [])
                img_prod_bot = set(rec.get("prod_urls_bottom") or [])
                img_suspect_top = set(
                    rec.get("suspect_prod_urls_top") or [])
                img_notices = list(rec.get("navigation_notices") or [])
                img_marketplace_urls = set(rec.get("marketplace_urls") or [])
                img_marketplace_uncertain = set(
                    rec.get("marketplace_uncertain_urls") or [])
                marketplace_urls.update(img_marketplace_urls)
                marketplace_uncertain_urls.update(img_marketplace_uncertain)
                if img_preprod:
                    images_with_preprod += 1
                    preprod_urls.update(img_preprod)
                if img_prod:
                    images_with_prod += 1
                    prod_urls.update(img_prod)
                if img_prod_top:
                    images_with_prod_top += 1
                    prod_urls_top.update(img_prod_top)
                if img_prod_bot:
                    prod_urls_bottom.update(img_prod_bot)
                if img_suspect_top:
                    suspect_prods_top.update(img_suspect_top)
                    if "firefox" in str(expected_browser or "").lower():
                        firefox_suspect_prods_top.update(img_suspect_top)
                    else:
                        contextual_suspect_prods_top.update(img_suspect_top)
                if img_notices:
                    rel_image = str(Path(rec["image"]).relative_to(
                        execution_dir))
                    navigation_notices.extend({
                        **notice,
                        "image": rel_image,
                    } for notice in img_notices)
                if img_preprod or img_prod or img_suspect_top or img_notices:
                    per_image.append({
                        "image": str(Path(rec["image"]).relative_to(
                            execution_dir)),
                        "preprod_urls": sorted(img_preprod),
                        "preprod_urls_top": sorted(
                            rec.get("preprod_urls_top") or []),
                        "preprod_urls_bottom": sorted(
                            rec.get("preprod_urls_bottom") or []),
                        "prod_urls": sorted(img_prod),
                        "prod_urls_top": sorted(img_prod_top),
                        "prod_urls_bottom": sorted(img_prod_bot),
                        "suspect_prod_urls_top": sorted(img_suspect_top),
                        "marketplace_urls": sorted(img_marketplace_urls),
                        "marketplace_uncertain_urls": sorted(img_marketplace_uncertain),
                        "navigation_notices": img_notices,
                    })

        if is_webview and not preprod_urls:
            with ThreadPoolExecutor(
                    max_workers=max_workers,
                    thread_name_prefix="haiku-env-context") as ex:
                futures = {
                    ex.submit(
                        _process_webview_context_image,
                        p, model_id, model_api_url, api_key_env, usage_tracker,
                    ): p
                    for p in images
                }
                for fut in as_completed(futures):
                    rec = fut.result()
                    recovered = set(rec.get("preprod_urls") or [])
                    if not recovered:
                        continue
                    images_with_preprod += 1
                    preprod_urls.update(recovered)
                    rel_image = str(Path(rec["image"]).relative_to(
                        execution_dir))
                    existing = next(
                        (row for row in per_image
                         if row.get("image") == rel_image),
                        None,
                    )
                    if existing is None:
                        per_image.append({
                            "image": rel_image,
                            "preprod_urls": sorted(recovered),
                            "prod_urls": [],
                            "prod_urls_top": [],
                            "prod_urls_bottom": [],
                            "source": "webview_context",
                        })
                    else:
                        existing["preprod_urls"] = sorted(
                            set(existing.get("preprod_urls") or [])
                            | recovered
                        )
                        existing["source"] = "webview_context"

        if env_check._SCAN_PAGE_BODY_MARKERS and (
            not preprod_urls or env_check._REFERENCE_EVIDENCE
        ):
            with ThreadPoolExecutor(
                    max_workers=max_workers,
                    thread_name_prefix="haiku-env-body") as ex:
                futures = {
                    ex.submit(
                        _process_page_body_marker_image,
                        path,
                        model_id,
                        model_api_url,
                        api_key_env,
                        usage_tracker,
                    ): path
                    for path in images
                }
                for future in as_completed(futures):
                    rec = future.result()
                    recovered = set(rec.get("preprod_urls") or [])
                    if not recovered:
                        continue
                    # References can confirm the assigned marketplace. A
                    # reference to another locale alone cannot prove the
                    # execution used that locale; active contradictions stay.
                    reference_marketplace_urls = {
                        url for url in recovered
                        if expected_marketplace and workflow_rules._detect_marketplace_from_url(url)
                        == expected_marketplace.upper()
                    } if env_check._REFERENCE_EVIDENCE else set()
                    marketplace_urls.update(reference_marketplace_urls)
                    rel_image = str(Path(rec["image"]).relative_to(
                        execution_dir))
                    existing = next(
                        (
                            row
                            for row in per_image
                            if row.get("image") == rel_image
                        ),
                        None,
                    )
                    if existing is None or not existing.get("preprod_urls"):
                        images_with_preprod += 1
                    preprod_urls.update(recovered)
                    if existing is None:
                        per_image.append({
                            "image": rel_image,
                            "preprod_urls": sorted(recovered),
                            "prod_urls": [],
                            "prod_urls_top": [],
                            "prod_urls_bottom": [],
                            "suspect_prod_urls_top": [],
                            "navigation_notices": [],
                            "marketplace_reference_urls": sorted(reference_marketplace_urls),
                            "source": "page_body_markers",
                        })
                    else:
                        existing["preprod_urls"] = sorted(
                            set(existing.get("preprod_urls") or [])
                            | recovered
                        )
                        existing["source"] = "page_body_markers"
                        existing["marketplace_reference_urls"] = sorted(reference_marketplace_urls)

        # Contextual address-bar reads already passed independent confirmation.
        if (not is_prod_validation and prod_urls_top and preprod_urls
                and not env_check.has_contextual_environment_policy()):
            confirmed_top, session_suspect_prods = _split_suspect_apex_prods(
                prod_urls_top, preprod_urls)
            if session_suspect_prods:
                suspect_prods_top.update(session_suspect_prods)
                log.info(
                    "haiku env_check: %d bare-apex prod read(s) (%s) "
                    "downgraded to R9-susp — session shows preprod on "
                    "the same marketplace",
                    len(session_suspect_prods),
                    ", ".join(sorted(session_suspect_prods)))
                prod_urls_top = confirmed_top
                marketplace_urls.difference_update(session_suspect_prods)
                for rec in per_image:
                    downgraded = set(rec["prod_urls_top"]) & session_suspect_prods
                    rec["prod_urls_top"] = sorted(
                        set(rec["prod_urls_top"]) - downgraded)
                    rec["suspect_prod_urls_top"] = sorted(
                        set(rec.get("suspect_prod_urls_top") or []) | downgraded)
                    rec["prod_urls"] = sorted(
                        set(rec["prod_urls_top"])
                        | set(rec.get("prod_urls_bottom") or []))
                prod_urls = {
                    url for rec in per_image for url in rec["prod_urls"]
                }
                images_with_prod = sum(
                    bool(rec["prod_urls"]) for rec in per_image)
                images_with_prod_top = sum(
                    1 for rec in per_image
                    if set(rec["prod_urls_top"]) & prod_urls_top)

        has_real_prod = bool(prod_urls_top)
        if has_real_prod and preprod_urls:
            verdict = "mixed"
        elif has_real_prod:
            verdict = "violation" if not is_prod_validation else "compliant"
        elif preprod_urls:
            verdict = "compliant" if not is_prod_validation else "violation"
        elif env_check._REFERENCE_EVIDENCE and not is_prod_validation and any(
            notice.get("source") == "address_bar" for notice in navigation_notices
        ):
            verdict = "compliant"
        else:
            verdict = "ambiguous"

    findings: list[dict[str, Any]] = []
    if verdict in ("violation", "mixed"):
        finding_fn = (env_check._build_prod_validation_finding
                      if is_prod_validation else env_check._build_finding)
        findings.append(finding_fn(
            verdict=verdict,
            prod_urls=prod_urls_top,
            preprod_urls=preprod_urls,
            images_with_prod=images_with_prod_top,
            images_with_preprod=images_with_preprod,
        ))

    suspect_groups = (
        (
            firefox_suspect_prods_top,
            (
                "Firefox emphasizes the registrable domain while dimming "
                "subdomains and paths. The focused color and "
                "contrast-enhanced reads could not confidently prove a "
                "complete production hostname."
            ),
        ),
        (
            contextual_suspect_prods_top,
            (
                "The focused address-bar read could not independently "
                "confirm the complete hostname."
            ),
        ),
        (
            suspect_prods_top - firefox_suspect_prods_top - contextual_suspect_prods_top,
            (
                "Other screenshots in this session show feature-preprod on "
                "the same marketplace, so the bare-apex reading is likely a "
                "dropped-subdomain transcription."
            ),
        ),
    )
    for suspects, explanation in suspect_groups:
        if not suspects:
            continue
        sample = ", ".join(sorted(suspects)[:3])
        if len(suspects) > 3:
            sample += " ..."
        findings.append({
            "severity": "low",
            "page": None,
            "step_index": None,
            "description": (
                f"Suspected address-bar transcription ({sample}): "
                f"{explanation} NOT counted as a prod violation (R9). "
                "Check the "
                "per_image record for the source screenshot(s) if "
                "manual confirmation is wanted."
            ),
            "source": "env_check",
            "rule": "R9-susp",
            "category": "insufficient_evidence",
            "action": "review_manually",
            "confidence": "low",
            "advisory": True,
        })

    has_approved_navigation = bool(
        navigation_notices
    ) and not is_prod_validation
    if has_approved_navigation:
        labels = ", ".join(sorted({
            notice["label"]
            for notice in navigation_notices
        }))
        findings.append({
            "severity": "low",
            "page": None,
            "step_index": None,
            "description": (
                "Production navigation was shown only for an approved "
                f"informational page ({labels}). This is recorded for "
                "review and does not invalidate the execution."
            ),
            "source": "env_check",
            "rule": "R9-nav",
            "category": "insufficient_evidence",
            "action": "review_manually",
            "confidence": "high",
            "advisory": True,
        })

    if (
        verdict == "ambiguous"
        and not suspect_prods_top
        and not has_approved_navigation
    ):
        if missing_url_is_advisory and all_images:
            description = (
                "Vision model found 0 example.* URLs across "
                f"{len(images)} screenshot(s) in a Webview/Mobile folder. "
                "Environment compliance could not be verified from the "
                "submitted evidence. Missing URL or chrome://inspect evidence "
                "is advisory only for Mobile/Webview execution."
            )
        elif all_images:
            description = (
                "Vision model found no readable Example URL in browser chrome "
                f"across {len(images)} sampled screenshot(s). Cannot verify "
                "environment compliance from the submitted evidence."
            )
        else:
            description = (
                "No screenshot evidence was available for environment "
                "verification. Cannot verify environment compliance."
            )
        if env_check._REFERENCE_EVIDENCE:
            description = (
                "Environment could not be established from the active URLs or "
                f"attached setup/debug references across {len(images)} screenshot(s). "
                "Human review is needed to verify the supplied evidence."
            )
        findings.append({
            "severity": "low" if missing_url_is_advisory else "medium",
            "page": None,
            "step_index": None,
            "description": description,
            "source": "env_check",
            "rule": "R9-amb",
            "category": "insufficient_evidence",
            "action": "add_evidence",
            "confidence": "low" if missing_url_is_advisory else "medium",
            "advisory": missing_url_is_advisory,
        })

    return {
        "version": ENV_CHECK_VERSION,
        "verdict": verdict,
        "preprod_urls": sorted(preprod_urls),
        "prod_urls": sorted(prod_urls),
        "prod_urls_top": sorted(prod_urls_top),
        "prod_urls_bottom": sorted(prod_urls_bottom),
        "suspect_prod_urls_top": sorted(suspect_prods_top),
        "marketplace_urls": sorted(marketplace_urls),
        "marketplace_uncertain_urls": sorted(marketplace_uncertain_urls),
        "images_with_preprod": images_with_preprod,
        "images_with_prod": images_with_prod,
        "images_scanned": len(images),
        "images_total": len(all_images),
        "sample_stride": sample_stride,
        "region_crop_height": region_crop_height,
        "region_crop_bottom_height": region_crop_bottom_height,
        "per_image": per_image,
        "navigation_notices": navigation_notices,
        "findings": findings,
    }
