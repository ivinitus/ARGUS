"""Vision-model env-compliance check (Claude Haiku 4.5).

Drop-in replacement for env_check.py's check_env_compliance() with
identical output schema (version, verdict, preprod_urls, prod_urls,
per_image, findings, etc). Replaces tesseract OCR — eliminates known
FP/FN modes (page-body brand reads, mobile URL-bar truncation,
left-truncated "feature-pre…exampleapp.de" misclassification).

Cost ~$0.001/image; ~$0.50-$1.50/folder vs negligible audit-phase cost.
URL classification reuses env_check._classify_host (single policy source).
"""
from __future__ import annotations

import base64
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config as BotoConfig

import env_check  # reuse _classify_host, _build_finding, _select_sampled_images, _iter_audit_images, _crop_url_bar_region

try:  # pragma: no cover
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

# Bumped over env_check.ENV_CHECK_VERSION (currently 3) so back-fill
# treats Haiku-classified audits as a new generation. Stored audits at
# the previous version re-run under back-fill on bumps.
#
# v4: initial Haiku-based classifier
# v5: left-truncation downgrade — Haiku reports truncated_left=true
#     for mobile URL bars where "feature-pre" is hidden behind the
#     search icon and only "prod.exampleapp.com" is visible. Such reads
#     used to false-fire R9 (prod violation); v5 classifies them as
#     "ignore" instead. Bumped to force re-classification of audits
#     stored under v4 that may carry these FPs.
# v6: help-locale subdomain ignore-list extended (env_check.py v4 →
#     mirror here). aiuto.exampleapp.it, ayuda.exampleapp.es, aide.exampleapp.fr,
#     hilfe.exampleapp.de, ajuda.exampleapp.com.br, help.exampleapp.<tld> all
#     classify as "ignore" via env_check._classify_host. Backfill
#     re-processes audits with stale R9 firings on legitimate help
#     navigation.
# v7: region-attributed prod URL detection. Top-region URLs (real
#     URL bar) trigger R9 (high). Bottom-region-only URLs (status-bar
#     hover, page-body links) trigger R9-hover (low advisory) — the
#     tester is on preprod but the page contains a hardcoded prod
#     link. Distinguishes "tester ran on prod" (real R9) from "page
#     content has prod link" (product issue, tester innocent).
# v8: merci.exampleapp.fr added to help-locale ignore list (env_check.py
#     v5 → mirror here). Operator-confirmed FR help-page host with
#     no preprod equivalent. Back-fill re-processes audits with stale
#     R9 firings on legitimate merci.exampleapp.fr navigation.
# v9: ikuto added as OCR-misread alias for aiuto.exampleapp.it
#     (env_check.py v6 → mirror here). Vision-model occasionally
#     reads "aiuto" as "ikuto" on low-res mobile screenshots.
# v10: Haiku prompt tightened to refuse browser tab titles. Vision
#      model was reading "Account Details | ExampleApp.ca" tab titles
#      as URLs and reporting them as prod_urls_top, false-firing R9
#      on webview/desktop screenshots that had only a background tab
#      with exampleapp.<tld> in its title (no actual address bar
#      navigation to that host). Operator-confirmed 2026-05-25 via
#      QA-E181469 / page_005.jpg. Back-fill re-classifies stored
#      audits with stale tab-title-derived R9.
# v11: salvage-path regex now preserves the truncated_left marker.
#      Pre-v11, when Haiku returned malformed/truncated JSON the
#      salvage fallback dropped the truncated_left field and a mobile
#      URL bar showing "prod.exampleapp.com" (actually feature-preprod
#      truncated) got classified as prod → false R9. Operator-flagged
#      2026-05-26 after observing many "JSON parse failed" warnings on
#      the April folder run. Backfill re-classifies stored audits.
ENV_CHECK_VERSION = 11

DEFAULT_MODEL_ID = "example-vision-model"
DEFAULT_REGION = "us-west-2"

# System prompt: tightly scoped. Haiku is asked ONLY to extract URLs
# visible in the actual browser URL bar — not page body text, not
# in-page links, not link-target text. Returns JSON for deterministic
# parsing. The "no URLs" path is explicit so the model doesn't
# hallucinate a URL when one isn't visible.
SYSTEM_PROMPT = """You are extracting URLs from a screenshot of a browser window.

You will be shown a cropped strip from a screenshot. The strip contains the browser's URL bar (top), and may also contain a status-bar hover URL (bottom-left of the original browser window) or a mobile browser's bottom URL bar.

YOUR ONLY JOB:
- List every URL that is VISIBLY DISPLAYED in the actual browser URL bar (chrome) or in a status-bar hover region.
- Do NOT include URLs that appear inside the page body (clickable links rendered as part of the web page content).
- Do NOT include URLs that appear in screenshots of other browser windows or developer tools.
- Do NOT include text from BROWSER TAB TITLES. Tab titles look like "Page Name | ExampleApp.ca" or "Account Details — ExampleApp.de" inside a browser tab — they are page-title text the site sets, NOT the active URL. The active URL only lives in the address/URL bar below the tabs. If only a tab title is visible (no address bar reading), return {"urls": []}.
- Do NOT synthesise a URL from a hostname-looking token. Extract only fully-formed URLs that you can see typed into the address bar (with or without scheme/path). A bare domain rendered inside text like "Privacy | ExampleApp.ca" is page-title decoration, not a URL.
- Do NOT guess or fabricate URLs that aren't clearly visible.
- TRUNCATION DETECTION (critical for mobile browsers): mobile URL bars often hide the LEADING portion of a long URL behind a search/lock icon, so what's visible is just the END of the URL (e.g. "prod.exampleapp.com" when the actual URL is "feature-preprod.exampleapp.com"). When the visible text starts mid-URL with no ellipsis indicator, the URL is TRUNCATED at the start. Detect this by checking: is there a search/lock icon immediately to the LEFT of the URL text? Does the URL begin with a host-component that looks like the END of a longer subdomain (e.g. "prod" alone, "preprod" alone)? Mark such URLs as truncated_left=true.
- Ellipsis-style truncation ("feature-pre…exampleapp.de") should be marked truncated_left=true as well.

OUTPUT FORMAT — respond with ONE JSON object and NOTHING ELSE. No prose, no markdown, no code fences. The first character must be `{` and the last must be `}`. Shape:

{
  "urls": [
    {"url": "https://feature-preprod.exampleapp.com/...", "truncated_left": false},
    {"url": "prod.exampleapp.com/cart", "truncated_left": true}
  ]
}

If no URL is visible in the URL bar / status-bar regions, return:
{"urls": []}

For backwards compatibility, plain string URLs are also accepted (treated as truncated_left=false), but PREFER the object form so truncation is explicit.
"""

# Same upscale + crop reuse as the tesseract path so we benchmark
# apples-to-apples — Haiku gets the same input region tesseract would.
# (Haiku doesn't NEED upscale, but matching keeps the comparison fair
# and lets us swap implementations without changing call sites.)


# Per-thread LLM Runtime client — same pattern as bench_llm_runtime_concurrency.
import threading
_thread_local = threading.local()


def _get_client(region: str, profile: str | None):
    client = getattr(_thread_local, "client", None)
    if client is None:
        cfg = BotoConfig(
            retries={"mode": "adaptive", "max_attempts": 5},
            read_timeout=60,
            connect_timeout=15,
            max_pool_connections=64,
        )
        if profile:
            session = boto3.Session(profile_name=profile)
            client = session.client("llm_runtime-runtime", region_name=region,
                                    config=cfg)
        else:
            client = boto3.client("llm_runtime-runtime", region_name=region,
                                  config=cfg)
        _thread_local.client = client
    return client


def _image_to_base64_jpeg(img: "Image.Image", quality: int = 85) -> str:
    """Encode a PIL Image as base64 JPEG. Haiku accepts JPEG; smaller
    payload than PNG."""
    buf = BytesIO()
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.save(buf, format="JPEG", quality=quality)
    return base64.standard_b64encode(buf.getvalue()).decode("ascii")


def _extract_urls_haiku(
    img: "Image.Image",
    model_id: str,
    region: str,
    profile: str | None,
) -> list[str]:
    """Send one cropped image to Haiku, parse the URL list back.

    Returns a list of URL strings. On error or invalid JSON, returns
    [] — same fail-open behaviour as env_check.ocr_image. env_check
    is additive; if URL extraction fails we don't crash the audit.
    """
    if Image is None:  # pragma: no cover
        log.warning("Pillow not available — env_check_haiku disabled")
        return []
    try:
        b64 = _image_to_base64_jpeg(img)
    except Exception as e:  # pragma: no cover
        log.warning("haiku env_check: image encode failed: %s: %s",
                    type(e).__name__, e)
        return []
    client = _get_client(region, profile)
    body = {
        "anthropic_version": "llm_runtime-2023-05-31",
        # 1024 (was 512). Pages with deep query strings on ExampleApp URLs
        # — e.g. account/membership/cancel?ref_pageLoadId=... — produced
        # responses that Haiku truncated mid-URL at 512, breaking the
        # JSON parse and dropping the page's URLs entirely. 1024 covers
        # the full URL + JSON envelope at p99 and is still well below
        # Haiku's per-call output budget.
        "max_tokens": 1024,
        "temperature": 0.0,
        "system": SYSTEM_PROMPT,
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
                "text": "Extract URLs visible in the URL bar / status bar regions.",
            }],
        }],
    }
    try:
        resp = client.invoke_model(
            modelId=model_id,
            body=json.dumps(body),
            contentType="application/json",
            accept="application/json",
        )
        raw = json.loads(resp["body"].read())
    except Exception as e:
        log.warning("haiku env_check: invoke failed: %s: %s",
                    type(e).__name__, e)
        return []
    blocks = raw.get("content") or []
    text = "".join(b.get("text", "") for b in blocks
                   if b.get("type") == "text").strip()
    # Strip code fences if Haiku produced them despite instructions.
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # First-line repair: try the first balanced {…} block.
        first = text.find("{")
        last = text.rfind("}")
        if first >= 0 and last > first:
            try:
                parsed = json.loads(text[first:last + 1])
            except json.JSONDecodeError:
                parsed = None
        else:
            parsed = None
        # Second-line repair: response was truncated mid-URL (e.g. when
        # max_tokens cut off the closing `]`/`}`). Salvage whatever
        # complete URL strings appear in the raw text — better to keep
        # the partial signal than drop the page entirely.
        #
        # CRITICAL: must also preserve the `truncated_left` marker for
        # mobile URL bars where "feature-pre" is hidden behind a search
        # icon. Without that marker, the downstream classifier treats
        # "prod.exampleapp.com" as a real prod hit and fires R9. The
        # multiline regex below pulls each {"url": ..., "truncated_left":
        # ...} object even when the surrounding JSON is malformed.
        if parsed is None:
            log.warning("haiku env_check: JSON parse failed (likely "
                        "truncated response); salvaging URLs from text")
            out: list[str] = []
            # Object-shaped entries first: capture URL + truncated_left.
            obj_rx = re.compile(
                r'\{\s*"url"\s*:\s*"((?:https?://)?[a-zA-Z0-9][^\s"]*'
                r'exampleapp\.[a-zA-Z.]{2,7}[^\s"]*)"'
                r'(?:\s*,\s*"truncated_left"\s*:\s*(true|false))?',
            )
            consumed_offsets: set[int] = set()
            for m in obj_rx.finditer(text):
                url = m.group(1).strip()
                if not url:
                    continue
                trunc = (m.group(2) or "").lower() == "true"
                out.append(url + "#TRUNC" if trunc else url)
                consumed_offsets.add(m.start(1))
            # Plain-string fallback for URLs that didn't match the
            # object form (e.g. partial output that lost its envelope).
            # Skip URLs already captured above.
            string_rx = re.compile(
                r'"((?:https?://)?[a-zA-Z0-9][^\s"]*exampleapp\.'
                r'[a-zA-Z.]{2,7}[^\s"]*)"',
            )
            for m in string_rx.finditer(text):
                if m.start(1) in consumed_offsets:
                    continue
                u = m.group(1).strip()
                if u:
                    out.append(u)
            return out
    raw_urls = parsed.get("urls", [])
    if not isinstance(raw_urls, list):
        return []
    # Accept both shapes: legacy string-only, and the new
    # {url, truncated_left} object. Returns a flat list of
    # "URL[#TRUNC]" strings — the [#TRUNC] suffix is a private marker
    # the downstream classifier consumes; never surfaces to UI.
    out: list[str] = []
    for u in raw_urls:
        if isinstance(u, str):
            if u.strip():
                out.append(u.strip())
        elif isinstance(u, dict):
            url = (u.get("url") or "").strip()
            if not url:
                continue
            if u.get("truncated_left"):
                # Private sentinel — see _classify_url_string for handling.
                out.append(url + "#TRUNC")
            else:
                out.append(url)
    return out


def _classify_url_string(url: str) -> tuple[str, str]:
    """Pick (matched_text, classification) for a URL string from Haiku.

    Reuses the tesseract path's regex + _classify_host so the policy is
    identical. Important: we tolerate URL strings WITHOUT scheme
    ("exampleapp.com/foo") and WITH truncation indicators
    ("feature-pre…exampleapp.de") because Haiku produces both naturally.

    LEFT-TRUNCATION HANDLING: when Haiku reports the URL was truncated
    at the start (private "#TRUNC" sentinel suffix added by
    _extract_urls_haiku), we cannot trust the visible subdomain alone
    to classify env. Mobile URL bars often hide "feature-pre" behind
    the search icon, leaving just "prod.exampleapp.com" visible — that's
    actually preprod, not prod. Resolution rule:
      * Visible subdomain looks like a preprod marker
        (preprod / staging / dev / feature-pre*) → still preprod.
      * Visible subdomain looks like prod (no marker, or just "www" /
        "m" / bare) → DOWNGRADE to "ignore" (inconclusive). The
        deterministic R9 fail-flag should not fire on a visually
        truncated phone URL bar — that's a known FP class.
    Returns ("", "ignore") when the URL doesn't reference exampleapp.<tld>.
    """
    truncated_left = url.endswith("#TRUNC")
    text = url[:-len("#TRUNC")].strip() if truncated_left else url.strip()
    # Truncation handling: "feature-pre…exampleapp.de" → treat the prefix
    # before the ellipsis as a partial subdomain. The downstream
    # classifier checks for substring "feature-preprod" / "preprod" /
    # "staging" / "dev" so a prefix like "feature-pre" doesn't match
    # any marker. Insert a likely-completion when the prefix is one of
    # the known partial preprod fragments. Keep it conservative: only
    # specific known prefixes get expanded, otherwise the truncation
    # is left as-is (and may classify as prod, which we accept as the
    # safe over-flag).
    text_norm = text
    # Replace common ellipsis chars with a marker we can reason about.
    text_norm = text_norm.replace("…", "<TRUNC>")
    text_norm = text_norm.replace("...", "<TRUNC>")
    # Known preprod prefix completions for truncated URLs.
    preprod_partial_prefixes = (
        "feature-pre", "feature-pr", "feature-p", "feature-",
        "feature", "preprod-", "prepro", "prepr",
    )
    for prefix in preprod_partial_prefixes:
        if f"{prefix}<TRUNC>exampleapp." in text_norm.lower():
            text_norm = re.sub(
                rf"{re.escape(prefix)}<TRUNC>(exampleapp\.)",
                r"feature-preprod.\1",
                text_norm,
                flags=re.IGNORECASE,
            )
            break
    # Restore any remaining truncation markers as empty (so the URL
    # extraction regex sees a clean hostname after our fix-ups).
    text_norm = text_norm.replace("<TRUNC>", "")

    # Reuse tesseract path's URL regex / classification.
    text_norm = env_check._normalise_ocr_text(text_norm)
    for m in env_check._URL_TOKEN_RX.finditer(text_norm):
        subdomain = m.group(1) or ""
        tld = (m.group(2) or "").lower()
        if tld not in env_check._EXAMPLEAPP_TLDS:
            continue
        cls = env_check._classify_host(subdomain)
        # Left-truncation downgrade. When Haiku said the URL was
        # truncated AND the visible subdomain classified as prod (no
        # preprod / staging / dev marker visible), we cannot trust
        # this is actually prod — "feature-preprod.exampleapp.com" on a
        # mobile URL bar with the prefix hidden behind the search
        # icon shows up as "prod.exampleapp.com" identically. Downgrade
        # to "ignore" so R9 doesn't fire a false-prod-violation.
        # Preprod classification stays — if we can SEE a preprod
        # marker in the visible portion, that's positive evidence.
        if truncated_left and cls == "prod":
            return (m.group(0), "ignore")
        return (m.group(0), cls)
    return ("", "ignore")


def _process_image(
    path: Path,
    model_id: str,
    region: str,
    profile: str | None,
    region_crop_height: int,
    region_crop_bottom_height: int,
) -> dict[str, Any]:
    """Crop, send to Haiku, classify URLs. Returns per-image record.

    Splits the image into TWO Haiku calls when both top and bottom
    crop heights are configured:
      * top region (real URL bar) — URLs here are the user's actual
        navigation. Prod URL here = tester is on prod = real R9.
      * bottom region (status-bar hover URL on desktop, mobile
        bottom URL bar on iOS Safari) — URLs here on desktop are
        page-body links the user is hovering, NOT navigation.
        Prod URL here on a tester who is otherwise on preprod is
        a PRODUCT ISSUE (page contains hardcoded prod link), not
        a tester compliance violation.
    Tagging URLs by region (top vs bottom) lets _build_finding
    downstream distinguish these cases and fire R9-hover (low,
    advisory) instead of R9 (high, tester violation) on the
    page-body-hardcoded-link pattern.

    When only one crop dimension is set, falls back to single-call
    behaviour and tags every URL as `top` (the historical default,
    preserves R9 firing semantics for callers that don't configure
    bottom-strip OCR).
    """
    if Image is None:  # pragma: no cover
        return {"image": str(path), "urls": [], "preprod_urls": [],
                "prod_urls": [], "prod_urls_top": [],
                "prod_urls_bottom": [], "preprod_urls_top": [],
                "preprod_urls_bottom": []}
    top_urls: list[str] = []
    bottom_urls: list[str] = []
    try:
        with Image.open(path) as raw_img:
            if raw_img.mode not in ("RGB", "L"):
                raw_img = raw_img.convert("RGB")
            # Strategy: when BOTH crops are configured, OCR each strip
            # separately so we can attribute findings to top/bottom.
            # When only one is set, fall back to the legacy single-call
            # path so we don't double Haiku spend on every image.
            both_strips = (region_crop_height > 0
                           and region_crop_bottom_height > 0)
            if both_strips:
                # Top-only crop.
                top_crop = env_check._crop_url_bar_region(
                    raw_img, region_crop_height, 0)
                if top_crop is not None:
                    top_urls = _extract_urls_haiku(
                        top_crop, model_id, region, profile)
                # Bottom-only crop.
                bot_crop = env_check._crop_url_bar_region(
                    raw_img, 0, region_crop_bottom_height)
                if bot_crop is not None:
                    bottom_urls = _extract_urls_haiku(
                        bot_crop, model_id, region, profile)
            else:
                # Single combined / full-image call. Treat all URLs as
                # `top` since we can't disambiguate without two passes.
                cropped = env_check._crop_url_bar_region(
                    raw_img, region_crop_height, region_crop_bottom_height)
                img = cropped if cropped is not None else raw_img
                top_urls = _extract_urls_haiku(img, model_id, region, profile)
    except Exception as e:  # pragma: no cover
        log.warning("haiku env_check: failed on %s: %s: %s",
                    path, type(e).__name__, e)

    # Classify each strip separately so the per-image record carries
    # the region attribution downstream.
    def _classify(urls):
        preprod, prod = [], []
        for u in urls:
            matched, cls = _classify_url_string(u)
            if cls == "preprod":
                preprod.append(matched or u)
            elif cls == "prod":
                prod.append(matched or u)
        return preprod, prod

    pp_top, pr_top = _classify(top_urls)
    pp_bot, pr_bot = _classify(bottom_urls)
    # Combined lists kept for backward compat with consumers that don't
    # care about region attribution.
    preprod_urls = pp_top + pp_bot
    prod_urls = pr_top + pr_bot
    return {
        "image": str(path),
        "urls": top_urls + bottom_urls,
        "preprod_urls": preprod_urls,
        "prod_urls": prod_urls,
        "preprod_urls_top": pp_top,
        "preprod_urls_bottom": pp_bot,
        "prod_urls_top": pr_top,
        "prod_urls_bottom": pr_bot,
    }


def check_env_compliance(
    execution_dir: Path,
    *,
    sample_stride: int = 1,
    sample_ends: int = 3,
    region_crop_height: int = 0,
    region_crop_bottom_height: int = 0,
    model_id: str = DEFAULT_MODEL_ID,
    region: str = DEFAULT_REGION,
    profile: str | None = None,
    max_workers: int = 8,
    is_webview: bool = False,
) -> dict[str, Any]:
    """Same signature + return shape as env_check.check_env_compliance.

    Privately uses Haiku 4.5 on LLM Runtime to extract URLs from cropped
    URL-bar regions, then reuses the tesseract path's classifier
    (_classify_host) and finding shape (_build_finding) so callers
    (auditor.run, env_check_backfill, report.py) don't need to change.

    Concurrency: per-image Haiku calls dispatch via a ThreadPool with
    `max_workers` parallel calls. Each call is ~1-2s so 8 workers is
    enough to OCR a 50-image folder in ~10 seconds wall time.

    Falls back gracefully on LLM Runtime errors — same fail-open behaviour
    as env_check (R9 silent rather than crash).
    """
    all_images = list(env_check._iter_audit_images(execution_dir))
    images = env_check._select_sampled_images(
        all_images, sample_stride, sample_ends)

    preprod_urls: set[str] = set()
    prod_urls: set[str] = set()
    images_with_preprod = 0
    images_with_prod = 0
    # Region-attributed accumulators. "top" prod URLs (real URL bar)
    # = tester actually navigated to prod = R9 high. "bottom" prod
    # URLs (status-bar hover, page-body links) on a tester who is
    # otherwise on preprod = product issue (page contains hardcoded
    # prod link), not a tester compliance violation = R9-hover low.
    prod_urls_top: set[str] = set()
    prod_urls_bottom: set[str] = set()
    images_with_prod_top = 0
    per_image: list[dict[str, Any]] = []

    if not images:
        verdict = "ambiguous"
    else:
        with ThreadPoolExecutor(max_workers=max_workers,
                                thread_name_prefix="haiku-env") as ex:
            futures = {
                ex.submit(_process_image, p, model_id, region, profile,
                          region_crop_height, region_crop_bottom_height): p
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
                if img_preprod or img_prod:
                    per_image.append({
                        "image": str(Path(rec["image"]).relative_to(
                            execution_dir)),
                        "preprod_urls": sorted(img_preprod),
                        "prod_urls": sorted(img_prod),
                        # Region attribution kept on per-image records
                        # so the report renderer / future tooling can
                        # show the operator WHICH region triggered.
                        "prod_urls_top": sorted(img_prod_top),
                        "prod_urls_bottom": sorted(img_prod_bot),
                    })

        # Verdict computation prefers the TOP-region view when both
        # data are available: a tester is "on prod" only if a prod
        # URL appears in the actual URL bar. Bottom-only prod URLs
        # (status-bar hover, page-body links) are evidence of a
        # product bug, not a navigation violation.
        has_real_prod = bool(prod_urls_top)  # top region only
        # Fall back to combined `prod_urls` when region data is empty
        # (single-call legacy path, or no bottom strip configured).
        if not prod_urls_top and not prod_urls_bottom and prod_urls:
            has_real_prod = True
        if has_real_prod and preprod_urls:
            verdict = "mixed"
        elif has_real_prod:
            verdict = "violation"
        elif preprod_urls:
            verdict = "compliant"
        else:
            verdict = "ambiguous"

    findings: list[dict[str, Any]] = []
    if verdict in ("violation", "mixed"):
        # Use the top-region prod URL set when available, since that's
        # what the verdict was built from. Fall back to combined when
        # legacy / single-call path didn't produce regional data.
        violation_prod_urls = prod_urls_top or prod_urls
        violation_image_count = (images_with_prod_top
                                 if prod_urls_top else images_with_prod)
        findings.append(env_check._build_finding(
            verdict=verdict,
            prod_urls=violation_prod_urls,
            preprod_urls=preprod_urls,
            images_with_prod=violation_image_count,
            images_with_preprod=images_with_preprod,
        ))
    # Hover/footer-only prod links — fire R9-hover (low, advisory).
    # The tester is on preprod (verdict is compliant or ambiguous in
    # the URL-bar view), but the page contains hardcoded prod links
    # visible in the status bar / footer. Product team's problem to
    # fix; tester is innocent. NOT included in R9 / mixed verdict.
    if prod_urls_bottom and not prod_urls_top:
        prod_sample = ", ".join(sorted(prod_urls_bottom)[:3])
        if len(prod_urls_bottom) > 3:
            prod_sample += " ..."
        findings.append({
            "severity": "low",
            "page": None,
            "step_index": None,
            "description": (
                "The page body / status-bar hover region contains "
                f"hardcoded production URL(s) ({prod_sample}). The "
                "tester is on preprod (URL bar shows feature-preprod), "
                "but a link or footer reference on the page points at "
                "the prod host. This is a PRODUCT issue (the preprod "
                "page should not contain prod URLs) — the tester is "
                "not at fault. File a defect against the page."
            ),
            "source": "env_check",
            "rule": "R9-hover",
        })

    if (verdict == "ambiguous" and is_webview and len(all_images) > 0):
        # R9-amb: see env_check.py for rationale (Webview folder + 0
        # URLs detected = unverified env compliance even when the
        # vision model claims preprod throughout).
        findings.append({
            "severity": "medium",
            "page": None,
            "step_index": None,
            "description": (
                "Vision model found 0 exampleapp.* URLs across "
                f"{len(images)} screenshot(s) in a Webview/Mobile folder. "
                "Webview tests should still show the URL in chrome://inspect "
                "or in a desktop preview. Cannot verify env compliance."
            ),
            "source": "env_check",
            "rule": "R9-amb",
        })

    return {
        "version": ENV_CHECK_VERSION,
        "verdict": verdict,
        "preprod_urls": sorted(preprod_urls),
        "prod_urls": sorted(prod_urls),
        "images_with_preprod": images_with_preprod,
        "images_with_prod": images_with_prod,
        "images_scanned": len(images),
        "images_total": len(all_images),
        "sample_stride": sample_stride,
        "region_crop_height": region_crop_height,
        "region_crop_bottom_height": region_crop_bottom_height,
        "per_image": per_image,
        "findings": findings,
    }
