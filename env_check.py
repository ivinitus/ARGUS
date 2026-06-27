"""URL-classification helpers for the environment-compliance check.

Originally this module ran OCR (Tesseract) over every screenshot and
classified URLs deterministically. As of 2026-05-23 the OCR pipeline
is RETIRED — env_check_haiku.py is the only env_check engine in
production. Tesseract was slower (~30s/audit vs Haiku's ~5s),
required pytesseract + tesseract binary on PATH, and had known
FP modes that the OCR-fixup regexes only partially mitigated
(``foature-proprod`` misreads, mobile URL truncation, etc.).

What remains here are the pure URL-classification helpers that
``env_check_haiku.py`` reuses:

  * `_classify_host`              — subdomain → preprod/prod/ignore
  * `_normalise_ocr_text`         — repair common URL-text artefacts
  * `_URL_TOKEN_RX`, `_EXAMPLEAPP_TLDS` — regex / TLD set
  * `_iter_audit_images`          — filesystem walk
  * `_select_sampled_images`      — stride sampling
  * `_crop_url_bar_region`        — PIL crop helper
  * `_build_finding`              — finding-dict factory
  * `extract_urls`                — apply regex + classifier to a string
  * `_has_url_context`            — false-positive filter

Verdict semantics (still the contract Haiku honours):

    - ``compliant``: ≥1 preprod URL seen, 0 prod URLs
    - ``violation``: ≥1 prod URL seen, 0 preprod URLs
    - ``mixed``: both preprod AND prod URLs seen
    - ``ambiguous``: no exampleapp URL extracted anywhere

The ``version`` field is bumped whenever classification semantics
change so back-fill knows to re-process. Tesseract-era versions
(v1-v4) are preserved in the changelog below for traceability.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Iterable

try:  # pragma: no cover
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

# Bump when classification semantics change in a way that would
# reclassify stored audit.json files.
#   v1 — initial OCR+regex impl.
#   v2 — tightened private-tool list (removed help, accounts, chk,
#        search from ignore; those are user-facing ExampleApp surfaces).
#   v3 — region crop now supports a bottom strip too, so status-bar
#        hover URLs and mobile-bottom URL bars get OCR'd. Audits at v1
#        or v2 with region-cropping enabled may have missed those.
#   v4 — help-locale subdomains (help, aiuto, ayuda, aide, hilfe,
#        ajuda, support) re-added to private-tool ignore list.
#        Help Center surfaces don't have a preprod environment;
#        legitimate help-page navigation during testing was being
#        misclassified as a prod violation. Re-process to clear FPs.
#   v5 — merci.exampleapp.fr added to help-locale ignore list.
#        Operator-confirmed FR help-page host with no preprod
#        equivalent (same treatment as aide.exampleapp.fr).
#   v6 — ikuto added as OCR-misread alias for aiuto.exampleapp.it.
#        Vision-model occasionally reads "aiuto" as "ikuto" on
#        low-res mobile screenshots; operator-confirmed both are
#        the Italian help host.
# Back-fill with the current version resets stored audits to current
# classification + capture logic.
ENV_CHECK_VERSION = 6

# All exampleapp TLDs the service runs on. Update if new locales launch.
_EXAMPLEAPP_TLDS = frozenset({
    "com", "de", "co.uk", "co.jp", "fr", "it", "es", "jp", "ca", "com.au",
    "in", "com.br",
})

# URL token regex. We do not require a protocol — Chrome sometimes
# hides ``https://`` in the URL bar, and tesseract mangles it
# occasionally (``htps//`` etc.). We do require a dot-separated host
# path ending in ``exampleapp.<tld>``.
#
# Groups:
#   1 = leading host chain (possibly empty; strip trailing dot)
#   2 = TLD (possibly compound, e.g. ``co.uk``)
_URL_TOKEN_RX = re.compile(
    r"(?:(?:https?:)?/{0,2})?"
    r"((?:[a-z0-9][a-z0-9\-]*\.)*?)"
    r"exampleapp"
    r"\.([a-z]{2,3}(?:\.[a-z]{2,3})?)",
    re.IGNORECASE,
)

# Common OCR mis-reads in hostnames. Tesseract confuses ``.`` with
# ``,`` between TLD components (``co.jp`` → ``co,jp``) roughly 30% of
# the time on 1568px browser screenshots. We fix this specific pattern
# only — general comma→dot replacement would false-fire on English
# prose like "exampleapp, however".
_OCR_TLD_COMMA_RX = re.compile(
    r"\b(co|com|exampleapp)\s*,\s*(jp|uk|au|nz|in|id|za|br)\b",
    re.IGNORECASE,
)

# Mobile Chrome URL bars render the hostname in a small font and OCR
# routinely drops or mangles the dot between ``exampleapp`` and the TLD.
# Accept zero or one interstitial character (OCR reading the dot as
# ``c`` / ``o`` / ``1`` / similar is common at 2x upscale). The
# TLD component is anchored so ``exampleappcom``, ``exampleappccom``, and
# ``exampleapp.com`` all normalise to ``exampleapp.com``.
_OCR_MISSING_DOT_RX = re.compile(
    r"\b(exampleapp)[.]?[a-z]?(com|de|fr|it|es|ca|in|jp)\b",
    re.IGNORECASE,
)

# OCR sometimes reads the dot between subdomain and ``exampleapp`` as a
# space (``feature-preprod exampleapp.de``). This drops the subdomain off
# the regex match, causing the host to classify as bare prod.
# Re-insert for known preprod / app subdomain markers.
_OCR_SUBDOMAIN_SPACE_RX = re.compile(
    r"\b(feature-preprod|preprod|staging|dev|www|mobile)\s+(exampleapp)\b",
    re.IGNORECASE,
)

# Conversely, OCR sometimes concatenates an private-tool subdomain to
# ``exampleapp`` with no separator (``helpexampleapp.co.jp`` for
# ``help.exampleapp.co.jp``). Without this the regex matches ``exampleapp.co.jp``
# with an empty subdomain and (per the URL-context filter's path-found
# branch) classifies as prod. Insert the missing dot so the private-tool
# filter in ``_classify_host`` can ignore these.
_OCR_INTERNAL_SUBDOMAIN_MERGE_RX = re.compile(
    r"\b(tracker|cms|wiki|api|mail|private|admin|jenkins|identity|sso|okta|"
    r"cdn|search|chk|help|accounts)(exampleapp)\b",
    re.IGNORECASE,
)

# URL-context markers preceding a match. Protocols and URL-bar
# ornaments. Looser than ``^https?://`` because tesseract mangles
# protocols. Any of these within ~8 chars before the match = URL.
_URL_PRE_MARKERS = ("http", "://", "//", "ttps:", "htps")


def _normalise_ocr_text(text: str) -> str:
    """Repair targeted OCR mis-reads in URL hostnames before regex matching.

    Currently:

    * compound-TLD comma confusion (``exampleapp.co,jp`` → ``exampleapp.co.jp``)
    * missing-dot in mobile Chrome URL bars (``exampleappcom`` → ``exampleapp.com``)
    * space-as-dot in subdomain chain (``preprod exampleapp.de`` → ``preprod.exampleapp.de``)
    * concatenated private-tool subdomain (``helpexampleapp.co.jp`` → ``help.exampleapp.co.jp``)
    """
    text = _OCR_TLD_COMMA_RX.sub(lambda m: f"{m.group(1)}.{m.group(2)}", text)
    text = _OCR_MISSING_DOT_RX.sub(lambda m: f"{m.group(1)}.{m.group(2)}", text)
    text = _OCR_SUBDOMAIN_SPACE_RX.sub(lambda m: f"{m.group(1)}.{m.group(2)}", text)
    text = _OCR_INTERNAL_SUBDOMAIN_MERGE_RX.sub(lambda m: f"{m.group(1)}.{m.group(2)}", text)
    return text


def _has_url_context(text: str, start: int, end: int) -> bool:
    """Return True if the matched span sits in URL-bar context.

    We require one of:

    * the match itself contains a path/protocol marker (``/``, ``://``, ``//``)
    * the byte immediately after the match is ``/``, ``?``, or ``#`` (path/query/fragment)
    * the ~8 chars before the match contain a URL-shaped token (``http``, ``://``, ``//``)

    Without this filter tesseract happily reports ``ExampleApp.de`` from
    a browser tab title ("Vielen Dank | ExampleApp.de") or page copy
    ("Bei ExampleApp.de kannst du...") as a URL hit, which flooded an
    early version of the check with false positives.
    """
    matched = text[start:end]
    if "/" in matched or "://" in matched:
        return True
    # Post-context: single char after is enough (path, query, fragment).
    if end < len(text) and text[end] in "/?#":
        return True
    # Pre-context: look back up to 8 chars for URL-shaped tokens.
    pre = text[max(0, start - 8):start].lower()
    return any(mark in pre for mark in _URL_PRE_MARKERS)

# Explicit private-tool subdomains — ignored even if visible in a
# screenshot (they are not app-env URLs). These are genuine dev/ops
# tools that would only appear by accident (tester had a bug tracker
# or SSO tab open during the session).
#
# Help-Center subdomains: locale-specific ExampleApp Help surfaces
# (help.exampleapp.com, aiuto.exampleapp.it = Italian "help",
# ayuda.exampleapp.es = Spanish, etc.) do NOT have a preprod environment.
# When a test legitimately routes the user into the Help Center, the
# URL stays on the prod help host regardless of whether the rest of
# the test is on feature-preprod. Treat as IGNORE so env_check
# doesn't false-fire R9 on intentional help-page navigation.
# Updated 2026-05-23 per QA-lead: aiuto.exampleapp.it is acceptable on
# help pages (same applies to all locale equivalents listed below).
#
# Deliberately EXCLUDED from the ignore list (these are user-facing
# ExampleApp product surfaces and MUST be on preprod during compliant
# testing):
#   * accounts — accounts.exampleapp.<tld> is user account management
#   * chk      — checkout flow
#   * search   — search surface
#   * m / www  — canonical mobile / desktop app hosts (caught by
#                default "prod" classification anyway)
# If a tester is seeing any of those on a bare / www / m subdomain
# (no "feature-preprod" in the host), the test is non-compliant.
_INTERNAL_HOST_PREFIXES = frozenset({
    "tracker", "cms", "wiki", "jenkins", "admin", "private",
    "identity", "sso", "okta", "mail",
    "api", "cdn",  # backend / CDN — tolerated because their
                   # environment is usually config-driven not
                   # user-initiated; revisit if real violations
                   # show up in these.
    # Help Center subdomains by locale — no preprod equivalent.
    "help",      # help.exampleapp.com / .co.uk / .ca / .com.au / .in
    "aiuto",     # aiuto.exampleapp.it (Italian)
    "ikuto",     # vision-model OCR misread of aiuto.exampleapp.it on
                 # low-resolution mobile screenshots — operator-confirmed
                 # 2026-05-25, treat identically to aiuto
    "ayuda",     # ayuda.exampleapp.es (Spanish)
    "aide",      # aide.exampleapp.fr (French help)
    "merci",     # merci.exampleapp.fr (French — additional help host;
                 # operator-confirmed 2026-05-24 to be treated like
                 # aide.exampleapp.fr — no preprod equivalent, prod URL
                 # is acceptable when test routes through these pages)
    "hilfe",     # hilfe.exampleapp.de (German)
    "ajuda",     # ajuda.exampleapp.com.br (Brazilian Portuguese)
    "support",   # support.exampleapp.<tld> (legacy / fallback)
})

# Non-preprod staging/dev subdomains. We treat these as preprod-like
# (tester is on a non-prod env, so not a policy violation). QA
# specifically requires ``feature-preprod``, but we avoid flagging
# other dev envs since they are also not prod.
#
# Split into two match modes:
#   * SUBSTRING markers — distinctive enough that a substring hit is
#     always a real non-prod env (``feature-preprod.www`` etc.).
#   * COMPONENT markers — short/ambiguous tokens that MUST match a whole
#     dot-separated host component. ``dev`` as a substring wrongly
#     classified prod hosts like ``developer.exampleapp.com`` and
#     ``devices.exampleapp.com`` as preprod, suppressing R9 prod-leak
#     findings. Requiring a full-component match fixes that while still
#     catching a real ``dev.exampleapp.com``.
_NONPROD_HOST_MARKERS = ("feature-preprod", "preprod", "staging")
_NONPROD_HOST_COMPONENTS = frozenset({"dev"})


def _classify_host(subdomain: str) -> str:
    """Classify a parsed subdomain chain as ``preprod``, ``prod``, or ``ignore``.

    ``subdomain`` is everything before ``.exampleapp.<tld>`` in the matched
    URL, normalised lowercase with the trailing dot stripped. Empty
    string means the URL was bare ``exampleapp.com`` (no subdomain).

    Policy:

    * Subdomain contains any preprod/staging/dev marker → ``preprod``
    * Leftmost component is a known private tool (tracker, cms, ...) → ``ignore``
    * Otherwise → ``prod``

    The default ``prod`` branch is load-bearing: mobile Chrome URL
    bars often OCR with a mangled leaf subdomain (``orod`` instead
    of ``prod`` / ``www``), and we would prefer to over-flag (reviewer
    confirms compliant) than under-flag (violation slips through).
    Callers apply this only to matches that already passed
    ``_has_url_context`` so prose like "exampleapp.com is great" is
    filtered out upstream.
    """
    sub = (subdomain or "").lower().strip(".")
    parts = [p for p in sub.split(".") if p]
    # Preprod / staging markers anywhere in the subdomain chain.
    # Check this FIRST so ``feature-preprod.www.exampleapp.com`` (theoretical)
    # classifies as preprod.
    for marker in _NONPROD_HOST_MARKERS:
        if marker in sub:
            return "preprod"
    # Component markers (``dev``) must match a whole host component, so
    # ``developer``/``devices`` are NOT treated as non-prod.
    if _NONPROD_HOST_COMPONENTS.intersection(parts):
        return "preprod"
    # Explicit private-tool subdomain — ignored.
    leaf = parts[0] if parts else ""
    if leaf in _INTERNAL_HOST_PREFIXES:
        return "ignore"
    # Everything else on ``exampleapp.<tld>`` is treated as prod.
    return "prod"


def extract_urls(ocr_text: str) -> list[tuple[str, str]]:
    """Extract ``exampleapp.<tld>`` URLs from OCR output and classify each.

    Returns a list of ``(matched_text, classification)`` tuples where
    classification is one of ``"preprod"``, ``"prod"``, ``"ignore"``.

    A match is accepted when either:

    * the span sits in URL-bar context (protocol, path/query/fragment,
      or an private slash), OR
    * the match has a non-empty subdomain chain (``foo.exampleapp.com`` —
      which a browser tab title or body-text mention never does; tab
      titles and prose use the bare ``ExampleApp.de`` form)

    Together these filter out the two big false-positive sources
    (tab titles, body-text mentions) while still catching mobile
    Chrome URL bars that lose the protocol and path entirely.
    """
    text = _normalise_ocr_text(ocr_text or "")
    results: list[tuple[str, str]] = []
    for m in _URL_TOKEN_RX.finditer(text):
        subdomain = m.group(1) or ""
        tld = (m.group(2) or "").lower()
        if tld not in _EXAMPLEAPP_TLDS:
            continue
        has_subdomain = bool(subdomain.strip("."))
        if not has_subdomain and not _has_url_context(text, m.start(), m.end()):
            continue
        cls = _classify_host(subdomain)
        results.append((m.group(0), cls))
    return results


def _crop_url_bar_region(
    img: "Image.Image",
    region_height: int,
    bottom_region_height: int = 0,
) -> "Image.Image | None":
    """Crop ``img`` to the top ``region_height`` + bottom
    ``bottom_region_height`` pixels — where URL bars / hover status
    bars / mobile-bottom address bars live.

    When both are >0, the two strips are stitched vertically into a
    single image with a small white gap between them. That lets the
    caller run a single OCR pass over both zones while keeping the
    page body (which accounts for 85-95% of pixels) out of the input.

    Returns None when cropping doesn't apply, so callers can naturally
    fall through to full-image OCR:

      * both heights <= 0 — cropping disabled.
      * image shorter than the combined crop height — cropping would
        produce an image equal to or larger than the source (including
        the gap), so it's a no-op. Most step-attachment images (tester
        phone shots) fall into this bucket and benefit from full OCR
        anyway.

    Returns a new Image (doesn't mutate the source). Safe to pass the
    result through _upscale_for_ocr — the upscale operates on whatever
    Image it receives.

    Why bottom-crop is needed:
      * Desktop browsers show a status-bar URL at the bottom-left when
        hovering a link (e.g. ``www.exampleapp.com``). If the page body
        has prod-pointing links, we want to surface that.
      * iOS Safari / some mobile Chromes render the URL bar at the
        BOTTOM of the viewport, not the top.
      * chrome://inspect device panels show webview URLs in a middle
        region — not fully captured, but 80px from the bottom catches
        most cases.

    Why not just expand the single top crop: URL bars and hover
    status-bars are usually thin (20-40 px) so two narrow strips use
    far less OCR budget than one tall one — the page body in between
    is still excluded. Typical values: top=180, bottom=80 → 260 px of
    OCR per image vs ~1800-3000 px for full-page.
    """
    if region_height <= 0 and bottom_region_height <= 0:
        return None
    top_h = max(0, region_height)
    bot_h = max(0, bottom_region_height)
    h = img.height
    # Combined crop would exceed the source, including the separator
    # gap. Fall through to full-image OCR.
    gap = 10 if top_h > 0 and bot_h > 0 else 0
    if h <= top_h + bot_h + gap:
        return None
    # Single-strip cases first (no stitching overhead).
    if top_h > 0 and bot_h == 0:
        return img.crop((0, 0, img.width, top_h))
    if bot_h > 0 and top_h == 0:
        return img.crop((0, h - bot_h, img.width, h))
    # Both strips — stitch top + (white gap) + bottom into one image.
    # White gap gives tesseract a clean break so text on the bottom
    # strip isn't treated as a continuation of the top.
    top_img = img.crop((0, 0, img.width, top_h))
    bot_img = img.crop((0, h - bot_h, img.width, h))
    combined_h = top_h + gap + bot_h
    combined = Image.new(img.mode, (img.width, combined_h), color="white")
    combined.paste(top_img, (0, 0))
    combined.paste(bot_img, (0, top_h + gap))
    return combined


def _iter_audit_images(execution_dir: Path) -> Iterable[Path]:
    """Yield every screenshot path under ``execution_dir/screenshots/``.

    Mirrors ``batch._has_any_evidence``'s coverage: page_*.{jpg,png}
    from PDF splits plus any step_attachments images. Sorted for
    deterministic output (makes regression tests possible).
    """
    shots = execution_dir / "screenshots"
    if not shots.exists():
        return []
    exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    paths = [
        p for p in shots.rglob("*")
        if p.suffix.lower() in exts and p.is_file()
    ]
    return sorted(paths)


def _build_finding(
    *,
    verdict: str,
    prod_urls: set[str],
    preprod_urls: set[str],
    images_with_prod: int,
    images_with_preprod: int,
) -> dict[str, Any]:
    """Construct the HIGH-severity env_check finding for a non-compliant audit."""
    prod_sample = ", ".join(sorted(prod_urls)[:3])
    if len(prod_urls) > 3:
        prod_sample += " ..."
    if verdict == "mixed":
        context = (
            f" {images_with_preprod} screenshot(s) also show preprod URLs, "
            "indicating mixed-environment testing — still non-compliant: "
            "the whole flow must run on feature-preprod."
        )
    else:
        context = " No feature-preprod URL was detected in any screenshot."
    return {
        "severity": "high",
        "page": None,
        "step_index": None,
        "description": (
            "Environment compliance violation detected by deterministic "
            f"URL extraction: {images_with_prod} screenshot(s) show "
            f"prod URL(s) in the URL bar ({prod_sample}). QA "
            "policy requires testing on feature-preprod.exampleapp.<tld>."
            + context
        ),
        "source": "env_check",
        "rule": "R9",
    }


def _select_sampled_images(
    images: list[Path],
    sample_stride: int,
    sample_ends: int,
) -> list[Path]:
    """Pick the subset of `images` to actually OCR.

    Strategy: always OCR the first `sample_ends` and last `sample_ends`
    images (to catch initial login / final verification URLs), plus every
    `sample_stride`-th image in the middle. Pass `sample_stride=1` to
    OCR every image (original behaviour, no sampling).

    Deterministic, stable: given the same inputs, produces the same
    ordered subset every time. No randomness — empirical coverage over
    the 10 existing R9 corpus shows that stride=5 preserves 100% of
    violation catches (URL bars repeat across consecutive pages so
    sampling loses nothing in practice).

    Special cases:
      - `sample_stride <= 1` returns `images` unchanged (full scan).
      - If `sample_ends * 2 >= len(images)`, returns all images
        (can't meaningfully sample a short list).
    """
    n = len(images)
    if sample_stride <= 1 or n == 0 or sample_ends * 2 >= n:
        return list(images)
    indices: set[int] = set()
    # First N and last N unconditionally.
    for i in range(sample_ends):
        indices.add(i)
        indices.add(n - 1 - i)
    # Every `stride`-th index.
    for i in range(0, n, sample_stride):
        indices.add(i)
    return [images[i] for i in sorted(indices)]



# Tesseract `check_env_compliance` removed 2026-05-23 — env_check_haiku.py
# is the sole engine in production. The pure URL-classification helpers
# above (_classify_host, extract_urls, _crop_url_bar_region,
# _iter_audit_images, _select_sampled_images, _build_finding,
# _normalise_ocr_text, _URL_TOKEN_RX, _EXAMPLEAPP_TLDS) are the public
# surface env_check_haiku reuses.
