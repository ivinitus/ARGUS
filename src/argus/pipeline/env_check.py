"""URL-classification helpers for the environment-compliance check."""
from __future__ import annotations

import fnmatch
import logging
import re
from pathlib import Path
from typing import Any, Iterable

try:  # pragma: no cover
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

ENV_CHECK_VERSION = 6

_DEFAULT_STOREFRONT_DOMAINS: tuple[str, ...] = ("example",)
_DEFAULT_NONPROD_MARKERS: tuple[str, ...] = (
    "feature-preprod",
    "preprod",
    "staging",
)
_DEFAULT_STOREFRONT_TLDS = frozenset({
    "com", "de", "co.uk", "co.jp", "fr", "it", "es", "jp", "ca", "com.au",
    "in", "com.br",
})

_STOREFRONT_DOMAINS: tuple[str, ...] = _DEFAULT_STOREFRONT_DOMAINS
# Kept as `_STOREFRONT_TLDS` because env_check_model reads this module attribute.
_STOREFRONT_TLDS = frozenset(_DEFAULT_STOREFRONT_TLDS)


def _domain_alternation(domains: tuple[str, ...]) -> str:
    return "|".join(re.escape(domain) for domain in domains)


def _marker_alternation(markers: tuple[str, ...]) -> str:
    tokens = tuple(dict.fromkeys((*markers, "dev", "www", "mobile")))
    return "|".join(re.escape(token) for token in tokens)


def _compile_url_patterns(
    domains: tuple[str, ...],
    markers: tuple[str, ...],
) -> tuple[re.Pattern, re.Pattern, re.Pattern, re.Pattern, re.Pattern]:
    """Compile every domain-dependent OCR pattern."""
    domain = _domain_alternation(domains)
    url_token_rx = re.compile(
        r"(?:(?:https?:)?/{0,2})?"
        # Anchor the host on both sides: "notexample.com", "otherexample.co.uk"
        # and "example.community" must not read as the storefront domain.
        r"(?<![a-z0-9-])"
        r"((?:[a-z0-9][a-z0-9\-]*\.)*?)"
        rf"(?:{domain})"
        r"\.([a-z]{2,3}(?:\.[a-z]{2,3})?)"
        r"(?![a-z0-9-])",
        re.IGNORECASE,
    )
    ocr_tld_comma_rx = re.compile(
        rf"\b(co|com|{domain})\s*,\s*(jp|uk|au|nz|in|id|za|br)\b",
        re.IGNORECASE,
    )
    ocr_missing_dot_rx = re.compile(
        rf"\b({domain})[.]?[a-z]?(com|de|fr|it|es|ca|in|jp)\b",
        re.IGNORECASE,
    )
    ocr_subdomain_space_rx = re.compile(
        rf"\b({_marker_alternation(markers)})\s+({domain})\b",
        re.IGNORECASE,
    )
    ocr_internal_merge_rx = re.compile(
        r"\b(jira|cms|wiki|api|mail|internal|admin|jenkins|identity|sso|okta|"
        rf"cdn|search|chk|help|accounts)({domain})\b",
        re.IGNORECASE,
    )
    return (
        url_token_rx,
        ocr_tld_comma_rx,
        ocr_missing_dot_rx,
        ocr_subdomain_space_rx,
        ocr_internal_merge_rx,
    )


(
    _URL_TOKEN_RX,
    _OCR_TLD_COMMA_RX,
    _OCR_MISSING_DOT_RX,
    _OCR_SUBDOMAIN_SPACE_RX,
    _OCR_INTERNAL_SUBDOMAIN_MERGE_RX,
) = _compile_url_patterns(_STOREFRONT_DOMAINS, _DEFAULT_NONPROD_MARKERS)

_URL_PRE_MARKERS = ("http", "://", "//", "ttps:", "htps")


def _normalise_ocr_text(text: str) -> str:
    """Repair targeted OCR mis-reads in URL hostnames before regex matching."""
    text = _OCR_TLD_COMMA_RX.sub(lambda m: f"{m.group(1)}.{m.group(2)}", text)
    text = _OCR_MISSING_DOT_RX.sub(lambda m: f"{m.group(1)}.{m.group(2)}", text)
    text = _OCR_SUBDOMAIN_SPACE_RX.sub(lambda m: f"{m.group(1)}.{m.group(2)}", text)
    text = _OCR_INTERNAL_SUBDOMAIN_MERGE_RX.sub(lambda m: f"{m.group(1)}.{m.group(2)}", text)
    return text


def _has_url_context(text: str, start: int, end: int) -> bool:
    """Return True if the matched span sits in URL-bar context."""
    matched = text[start:end]
    if "/" in matched or "://" in matched:
        return True
    if end < len(text) and text[end] in "/?#":
        return True
    pre = text[max(0, start - 8):start].lower()
    return any(mark in pre for mark in _URL_PRE_MARKERS)

_INTERNAL_HOST_PREFIXES = frozenset({
    "jira", "cms", "wiki", "jenkins", "admin", "internal",
    "identity", "sso", "okta", "mail",
    "api", "cdn",
    "help",
    "aiuto",
    "ikuto",
    "ayuda",
    "aide",
    "merci",
    "hilfe",
    "ajuda",
    "support",
})

_NONPROD_HOST_MARKERS: tuple[str, ...] = _DEFAULT_NONPROD_MARKERS
_NONPROD_HOST_COMPONENTS = frozenset({"dev"})
_IGNORED_HOST_PATTERNS: tuple[str, ...] = ()
_COMMENT_REQUIRED_MARKERS: tuple[str, ...] = ()
_NAVIGATION_NOTICE_LABELS: tuple[str, ...] = ()
_APPROVED_PROD_PATHS: tuple[str, ...] = ()
_APPROVED_PROD_HOST_PATTERNS: tuple[str, ...] = ()
_REFERENCE_EVIDENCE: bool = False
_SCAN_PAGE_BODY_MARKERS: bool = False
_EXPECTED_HOSTS_BY_RUN: tuple[tuple[str, frozenset[str]], ...] = ()
_ALLOWED_HOST_EXCEPTIONS: dict[str, frozenset[str]] = {}
_REQUIRED_HOST_OVERRIDES: dict[str, frozenset[str]] = {}

_DEFAULT_TRUNC_PREFIXES: tuple[tuple[str, str], ...] = (
    ("feature-pre", "feature-preprod"),
    ("feature-pr", "feature-preprod"),
    ("feature-p", "feature-preprod"),
    ("feature-", "feature-preprod"),
    ("feature", "feature-preprod"),
    ("preprod-", "feature-preprod"),
    ("prepro", "feature-preprod"),
    ("prepr", "feature-preprod"),
)
_TRUNC_PREFIXES: tuple[tuple[str, str], ...] = _DEFAULT_TRUNC_PREFIXES


def _generate_trunc_prefixes(
    markers: tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    """Build visible-prefix repairs for configured environment markers."""
    pairs: dict[str, str] = {}
    for marker in markers:
        for index in range(len(marker) - 1, 3, -1):
            pairs.setdefault(marker[:index], marker)
        pairs.setdefault(f"{marker}-", marker)
    return tuple(sorted(pairs.items(), key=lambda item: -len(item[0])))


def _classify_host(subdomain: str) -> str:
    """Classify a parsed subdomain chain as ``preprod``, ``prod``, or ``ignore``."""
    sub = (subdomain or "").lower().strip(".")
    parts = [p for p in sub.split(".") if p]
    for marker in _NONPROD_HOST_MARKERS:
        if marker in sub:
            return "preprod"
    if _NONPROD_HOST_COMPONENTS.intersection(parts):
        return "preprod"
    leaf = parts[0] if parts else ""
    if leaf in _INTERNAL_HOST_PREFIXES:
        return "ignore"
    return "prod"


def configure(policy: Any) -> None:
    """Apply profile-specific storefront and exact-host environment policy."""
    global _STOREFRONT_DOMAINS, _NONPROD_HOST_MARKERS, _STOREFRONT_TLDS
    global _URL_TOKEN_RX, _OCR_TLD_COMMA_RX, _OCR_MISSING_DOT_RX
    global _OCR_SUBDOMAIN_SPACE_RX, _OCR_INTERNAL_SUBDOMAIN_MERGE_RX
    global _IGNORED_HOST_PATTERNS, _COMMENT_REQUIRED_MARKERS
    global _NAVIGATION_NOTICE_LABELS, _APPROVED_PROD_PATHS
    global _APPROVED_PROD_HOST_PATTERNS, _REFERENCE_EVIDENCE
    global _SCAN_PAGE_BODY_MARKERS, _TRUNC_PREFIXES
    global _EXPECTED_HOSTS_BY_RUN, _ALLOWED_HOST_EXCEPTIONS
    global _REQUIRED_HOST_OVERRIDES

    configured_domains = getattr(policy, "storefront_domains", None)
    single_domain = getattr(policy, "storefront_domain", None)
    if configured_domains:
        domains = tuple(
            domain.lower() for domain in configured_domains if domain
        )
    elif single_domain:
        domains = (single_domain.lower(),)
    else:
        domains = _DEFAULT_STOREFRONT_DOMAINS

    configured_markers = getattr(policy, "expected_environment_markers", None)
    markers = (
        tuple(marker.lower() for marker in configured_markers if marker)
        if configured_markers
        else _DEFAULT_NONPROD_MARKERS
    )

    (
        _URL_TOKEN_RX,
        _OCR_TLD_COMMA_RX,
        _OCR_MISSING_DOT_RX,
        _OCR_SUBDOMAIN_SPACE_RX,
        _OCR_INTERNAL_SUBDOMAIN_MERGE_RX,
    ) = _compile_url_patterns(domains, markers)
    _STOREFRONT_DOMAINS = domains
    _NONPROD_HOST_MARKERS = markers
    _TRUNC_PREFIXES = (
        _DEFAULT_TRUNC_PREFIXES
        if markers == _DEFAULT_NONPROD_MARKERS
        else _generate_trunc_prefixes(markers)
    )
    _IGNORED_HOST_PATTERNS = tuple(
        pattern.lower()
        for pattern in getattr(
            policy, "ignored_environment_host_patterns", ()
        )
        if pattern
    )
    _COMMENT_REQUIRED_MARKERS = tuple(
        marker.lower()
        for marker in getattr(
            policy, "comment_required_environment_markers", ()
        )
        if marker
    )
    _NAVIGATION_NOTICE_LABELS = tuple(
        label.lower().strip()
        for label in getattr(
            policy, "production_navigation_notice_labels", ()
        )
        if label and label.strip()
    )
    _APPROVED_PROD_PATHS = tuple(
        path.lower().strip()
        for path in getattr(policy, "approved_production_paths", ())
        if path and path.strip()
    )
    _SCAN_PAGE_BODY_MARKERS = bool(
        getattr(policy, "env_markers_in_page_body", False)
    )
    _APPROVED_PROD_HOST_PATTERNS = tuple(
        pattern.lower().strip()
        for pattern in getattr(policy, "approved_production_host_patterns", ())
        if pattern.strip()
    )
    _REFERENCE_EVIDENCE = bool(getattr(policy, "environment_reference_evidence", False))

    marketplace_tlds = getattr(policy, "marketplace_tlds", None)
    _STOREFRONT_TLDS = (
        frozenset(
            tld.lstrip(".").lower()
            for tld in marketplace_tlds.values()
            if tld
        )
        if marketplace_tlds
        else frozenset(_DEFAULT_STOREFRONT_TLDS)
    )

    expected_hosts = getattr(
        policy, "environment_expected_hosts_by_run", {})
    _EXPECTED_HOSTS_BY_RUN = tuple(sorted(
        (
            (
                selector.strip().casefold(),
                frozenset(
                    _canonical_hostname(host)
                    for host in hosts
                    if host
                ),
            )
            for selector, hosts in expected_hosts.items()
            if selector.strip()
        ),
        key=lambda item: (-len(item[0]), item[0]),
    ))
    _ALLOWED_HOST_EXCEPTIONS = {
        key.strip().upper(): frozenset(
            _canonical_hostname(host) for host in hosts if host
        )
        for key, hosts in getattr(
            policy, "environment_allowed_host_exceptions", {}).items()
        if key.strip()
    }
    _REQUIRED_HOST_OVERRIDES = {
        key.strip().upper(): frozenset(
            _canonical_hostname(host) for host in hosts if host
        )
        for key, hosts in getattr(
            policy, "environment_required_host_overrides", {}).items()
        if key.strip()
    }


def reset_configuration() -> None:
    """Restore the built-in web storefront URL policy."""
    global _STOREFRONT_DOMAINS, _NONPROD_HOST_MARKERS, _STOREFRONT_TLDS
    global _URL_TOKEN_RX, _OCR_TLD_COMMA_RX, _OCR_MISSING_DOT_RX
    global _OCR_SUBDOMAIN_SPACE_RX, _OCR_INTERNAL_SUBDOMAIN_MERGE_RX
    global _IGNORED_HOST_PATTERNS, _COMMENT_REQUIRED_MARKERS
    global _NAVIGATION_NOTICE_LABELS, _APPROVED_PROD_PATHS
    global _APPROVED_PROD_HOST_PATTERNS, _REFERENCE_EVIDENCE
    global _SCAN_PAGE_BODY_MARKERS, _TRUNC_PREFIXES
    global _EXPECTED_HOSTS_BY_RUN, _ALLOWED_HOST_EXCEPTIONS
    global _REQUIRED_HOST_OVERRIDES

    _STOREFRONT_DOMAINS = _DEFAULT_STOREFRONT_DOMAINS
    _NONPROD_HOST_MARKERS = _DEFAULT_NONPROD_MARKERS
    _STOREFRONT_TLDS = frozenset(_DEFAULT_STOREFRONT_TLDS)
    _IGNORED_HOST_PATTERNS = ()
    _COMMENT_REQUIRED_MARKERS = ()
    _NAVIGATION_NOTICE_LABELS = ()
    _APPROVED_PROD_PATHS = ()
    _APPROVED_PROD_HOST_PATTERNS = ()
    _REFERENCE_EVIDENCE = False
    _SCAN_PAGE_BODY_MARKERS = False
    _TRUNC_PREFIXES = _DEFAULT_TRUNC_PREFIXES
    (
        _URL_TOKEN_RX,
        _OCR_TLD_COMMA_RX,
        _OCR_MISSING_DOT_RX,
        _OCR_SUBDOMAIN_SPACE_RX,
        _OCR_INTERNAL_SUBDOMAIN_MERGE_RX,
    ) = _compile_url_patterns(_STOREFRONT_DOMAINS, _NONPROD_HOST_MARKERS)
    _EXPECTED_HOSTS_BY_RUN = ()
    _ALLOWED_HOST_EXCEPTIONS = {}
    _REQUIRED_HOST_OVERRIDES = {}


def _canonical_hostname(value: str) -> str:
    """Return a lowercase hostname from a model/OCR URL token."""
    text = (value or "").strip().lower().removesuffix("#trunc")
    text = re.sub(r"^(?:https?:)?//", "", text)
    return text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]


def production_host_approved(url: str) -> bool:
    return any(
        fnmatch.fnmatchcase(_canonical_hostname(url), pattern)
        for pattern in _APPROVED_PROD_HOST_PATTERNS
    )


def _canonical_path(value: str) -> str:
    """Return a lowercase URL path, excluding query and fragment."""
    text = (value or "").strip().lower().removesuffix("#trunc")
    text = re.sub(r"^(?:https?:)?//", "", text)
    slash = text.find("/")
    if slash < 0:
        return "/"
    return text[slash:].split("?", 1)[0].split("#", 1)[0]


_LOCALE_SEGMENT_RX = re.compile(r"^/[a-z]{2}[_-][a-z]{2}(?=/)")


def _path_matches_prefix(path: str, prefix: str) -> bool:
    """Match an approved page and descendants, never a sibling prefix."""
    prefix = prefix.rstrip("/") or "/"
    if prefix == "/":
        return False
    return path == prefix or path.startswith(prefix + "/")


def _path_approved(url: str) -> bool:
    """Return whether a production URL is an approved setup path."""
    if not _APPROVED_PROD_PATHS:
        return False
    path = _canonical_path(url)
    candidates = [path]
    delocalised = _LOCALE_SEGMENT_RX.sub("", path)
    if delocalised != path:
        candidates.append(delocalised)
    return any(
        _path_matches_prefix(candidate, prefix)
        for candidate in candidates
        for prefix in _APPROVED_PROD_PATHS
    )


def _apply_host_policy(
    matched_text: str,
    cls: str,
    full_url: str = "",
) -> str:
    """Apply configured hostname exclusions and approved production paths."""
    if cls != "prod":
        return cls
    host = _canonical_hostname(matched_text)
    if any(
        fnmatch.fnmatchcase(host, pattern)
        for pattern in _IGNORED_HOST_PATTERNS
    ):
        return "ignore"
    if _path_approved(full_url or matched_text):
        return "ignore"
    return cls


def _downgrade_secondary_domain_prod(
    matched_text: str,
    cls: str,
    full_url: str = "",
) -> str:
    """Backward-compatible alias for the environment policy stage."""
    return _apply_host_policy(matched_text, cls, full_url)


def _url_token_with_path(text: str, end: int) -> str:
    """Recover the path following a host-only URL regex match."""
    tail = text[end:]
    if not tail.startswith("/"):
        return ""
    stop = next(
        (index for index, char in enumerate(tail) if char.isspace()),
        len(tail),
    )
    return tail[:stop]


def has_contextual_environment_policy() -> bool:
    """Return whether exact host rules replace the generic R9 verdict."""
    return bool(
        _EXPECTED_HOSTS_BY_RUN
        or _ALLOWED_HOST_EXCEPTIONS
        or _REQUIRED_HOST_OVERRIDES
    )


def _contextual_host_rule(
    metadata: dict[str, Any],
) -> tuple[str, str, frozenset[str], frozenset[str], frozenset[str]]:
    """Return selector, test key, baseline, exceptions, and required hosts."""
    results = metadata.get("test_results") or []
    test_result = results[0] if results else {}
    run_name = str(test_result.get("testrun_name") or "")
    test_case_key = str(test_result.get("test_case_key") or "").strip().upper()
    run_folded = run_name.casefold()

    selector = ""
    baseline: frozenset[str] = frozenset()
    for candidate, hosts in _EXPECTED_HOSTS_BY_RUN:
        if candidate in run_folded:
            selector = candidate
            baseline = hosts
            break

    exceptions = _ALLOWED_HOST_EXCEPTIONS.get(
        test_case_key, frozenset())
    required = _REQUIRED_HOST_OVERRIDES.get(
        test_case_key, frozenset())
    return selector, test_case_key, baseline, exceptions, required


def _observed_context_host_images(
    result: dict[str, Any],
) -> dict[str, list[str]]:
    """Index active address-bar hosts by their source screenshot.

    Aggregate URL lists mix address bars, hover targets, and older suspect
    reads. They cannot establish either compliance or a deviation.
    """
    session_suspects = set(result.get("suspect_prod_urls_top") or []) - set(
        result.get("prod_urls_top") or []
    )
    storefronts = tuple(
        f"{domain}.{tld}"
        for domain in _STOREFRONT_DOMAINS
        for tld in _STOREFRONT_TLDS
    )
    observed: dict[str, set[str]] = {}
    for row in result.get("per_image") or []:
        image = row.get("image")
        if not image or row.get("source") not in {None, "address_bar"}:
            continue
        raw_urls = set(row.get("preprod_urls_top") or [])
        raw_urls.update(row.get("prod_urls_top") or [])
        raw_urls.difference_update(session_suspects)
        raw_urls.difference_update(row.get("suspect_prod_urls_top") or [])
        for url in raw_urls:
            if url.lower().endswith("#trunc"):
                continue
            host = _canonical_hostname(url)
            if any(
                host == storefront or host.endswith(f".{storefront}")
                for storefront in storefronts
            ):
                observed.setdefault(host, set()).add(image)
    return {host: sorted(images) for host, images in sorted(observed.items())}


def _suspect_hosts_for_required_policy(
    result: dict[str, Any],
    required: frozenset[str],
) -> frozenset[str]:
    """Ambiguous model reads that could be a dim-prefix required host."""
    raw_suspects: set[str] = set(
        result.get("suspect_prod_urls_top") or []
    )
    for row in result.get("per_image") or []:
        raw_suspects.update(row.get("suspect_prod_urls_top") or [])
    suspects = {
        host
        for value in raw_suspects
        if (host := _canonical_hostname(value))
    }
    return frozenset(
        suspect
        for suspect in suspects
        if any(
            expected == suspect or expected.endswith(f".{suspect}")
            for expected in required
        )
    )


def apply_contextual_environment_policy(
    metadata: dict[str, Any],
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    """Replace generic R9 output with run/test-case exact-host policy."""
    selector, test_case_key, baseline, exceptions, required = (
        _contextual_host_rule(metadata)
    )
    if not baseline and not exceptions and not required:
        result["verdict"] = "not_applicable"
        result["findings"] = []
        result["contextual_policy"] = {
            "applied": False,
            "test_case_key": test_case_key,
        }
        return []

    allowed = baseline | exceptions | required
    observed_host_images = _observed_context_host_images(result)
    observed = frozenset(observed_host_images)
    wrong = observed - allowed
    missing_required = required - observed
    suspect_required = _suspect_hosts_for_required_policy(result, required)
    findings: list[dict[str, Any]] = []

    results = metadata.get("test_results") or []
    test_result = results[0] if results else {}
    run_name = str(test_result.get("testrun_name") or "unknown run")
    context = (
        f"{test_case_key} in {run_name}"
        if test_case_key else run_name
    )

    if wrong:
        source_images = sorted({
            image for host in wrong for image in observed_host_images[host]
        })
        findings.append({
            "severity": "high",
            "page": None,
            "step_index": None,
            "description": (
                f"Environment mismatch for {context}: URL-bar evidence shows "
                f"{', '.join(sorted(wrong))}, but the approved host set is "
                f"{', '.join(sorted(allowed))}. Source screenshots: "
                f"{', '.join(source_images[:3])}"
                f"{' (additional sources in per_image)' if len(source_images) > 3 else ''}."
            ),
            "source": "env_check",
            "rule": "R9",
        })
    elif missing_required:
        if suspect_required:
            findings.append({
                "severity": "medium",
                "page": None,
                "step_index": None,
                "description": (
                    f"Environment could not be verified for {context}: "
                    f"the address-bar reading {', '.join(sorted(suspect_required))} "
                    "could not be confirmed as a complete hostname proving "
                    f"{', '.join(sorted(required))}. This is not evidence "
                    "that the run used production."
                ),
                "source": "env_check",
                "rule": "R9-amb",
                "category": "insufficient_evidence",
                "action": "review_manually",
                "confidence": "low",
            })
        else:
            findings.append({
                "severity": "medium",
                "page": None,
                "step_index": None,
                "description": (
                    f"Required environment could not be verified for {context}: "
                    f"the policy requires {', '.join(sorted(required))}, but "
                    f"the sampled URL bars show "
                    f"{', '.join(sorted(observed)) if observed else 'no approved host'}. "
                    "A missing sampled URL does not establish an environment mismatch."
                ),
                "source": "env_check",
                "rule": "R9-amb",
                "category": "insufficient_evidence",
                "action": "add_evidence",
                "confidence": "medium",
            })
    elif not observed.intersection(allowed):
        findings.append({
            "severity": "medium",
            "page": None,
            "step_index": None,
            "description": (
                f"Environment could not be verified for {context}: no sampled "
                f"URL bar visibly proves an approved host "
                f"({', '.join(sorted(allowed))})."
            ),
            "source": "env_check",
            "rule": "R9-amb",
            "category": "insufficient_evidence",
            "action": "add_evidence",
            "confidence": "medium",
        })

    result["verdict"] = (
        "violation" if findings and findings[0]["severity"] == "high"
        else "ambiguous" if findings
        else "compliant"
    )
    result["findings"] = findings
    result["contextual_policy"] = {
        "applied": True,
        "selector": selector,
        "test_case_key": test_case_key,
        "expected_hosts": sorted(baseline),
        "exception_hosts": sorted(exceptions),
        "required_hosts": sorted(required),
        "allowed_hosts": sorted(allowed),
        "observed_hosts": sorted(observed),
        "observed_host_images": observed_host_images,
        "suspect_required_hosts": sorted(suspect_required),
    }
    return findings


def extract_urls(ocr_text: str) -> list[tuple[str, str]]:
    """Extract configured storefront URLs from OCR output and classify each."""
    text = _normalise_ocr_text(ocr_text or "")
    results: list[tuple[str, str]] = []
    for m in _URL_TOKEN_RX.finditer(text):
        subdomain = m.group(1) or ""
        tld = (m.group(2) or "").lower()
        if tld not in _STOREFRONT_TLDS:
            continue
        has_subdomain = bool(subdomain.strip("."))
        if not has_subdomain and not _has_url_context(text, m.start(), m.end()):
            continue
        cls = _classify_host(subdomain)
        full_url = m.group(0)
        if _APPROVED_PROD_PATHS:
            full_url += _url_token_with_path(text, m.end())
        cls = _apply_host_policy(m.group(0), cls, full_url)
        results.append((m.group(0), cls))
    return results


_BLOCKER_COMMENT_RE = re.compile(
    r"\b(block(?:ed|er|ing)?|issue|down|outage|unavailable|"
    r"not\s+working|fail(?:ed|ure)?|broken)\b",
    flags=re.I,
)


def _fallback_markers_in_storefront_host(url: str) -> set[str]:
    """Find fallback markers in the storefront-adjacent hostname label."""
    host = _canonical_hostname(url)
    if not host:
        return set()
    found: set[str] = set()
    for match in _URL_TOKEN_RX.finditer(host):
        chain = (match.group(1) or "").strip(".")
        if not chain:
            continue
        adjacent = chain.split(".")[-1]
        found.update(
            marker
            for marker in _COMMENT_REQUIRED_MARKERS
            if marker in adjacent
        )
    return found


def check_required_environment_comment(
    metadata: dict[str, Any],
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    """Require an explanation when a configured fallback environment is used."""
    if not _COMMENT_REQUIRED_MARKERS:
        return []
    if result.get("verdict") in {"violation", "mixed"}:
        return []
    used = sorted({
        marker
        for url in result.get("preprod_urls") or []
        for marker in _fallback_markers_in_storefront_host(str(url))
    })
    if not used:
        return []

    test_results = metadata.get("test_results") or []
    test_result = test_results[0] if test_results else {}
    comments = [str(test_result.get("comment") or "")]
    comments.extend(
        str(step.get("comment") or "")
        for step in test_result.get("steps") or []
        if isinstance(step, dict)
    )
    environment_terms = set(_NONPROD_HOST_MARKERS) | set(used)
    environment_terms.update(
        term.replace("-", " ")
        for term in tuple(environment_terms)
    )
    explained = any(
        _BLOCKER_COMMENT_RE.search(comment)
        and any(term in comment.lower() for term in environment_terms)
        for comment in comments
    )
    if explained:
        return []

    return [{
        "severity": "medium",
        "page": None,
        "step_index": None,
        "description": (
            "The screenshots show the fallback environment "
            f"({', '.join(used)}), but no tester comment explains a "
            "preprod blocker. Add a comment documenting why preprod was "
            "unavailable and why execution continued on the fallback."
        ),
        "source": "env_check",
        "rule": "R9-doc",
    }]


def _crop_url_bar_region(
    img: "Image.Image",
    region_height: int,
    bottom_region_height: int = 0,
) -> "Image.Image | None":
    """Crop ``img`` to the top ``region_height`` + bottom ``bottom_region_height`` pixels."""
    if region_height <= 0 and bottom_region_height <= 0:
        return None
    top_h = max(0, region_height)
    bot_h = max(0, bottom_region_height)
    h = img.height
    gap = 10 if top_h > 0 and bot_h > 0 else 0
    if h <= top_h + bot_h + gap:
        return None
    if top_h > 0 and bot_h == 0:
        return img.crop((0, 0, img.width, top_h))
    if bot_h > 0 and top_h == 0:
        return img.crop((0, h - bot_h, img.width, h))
    top_img = img.crop((0, 0, img.width, top_h))
    bot_img = img.crop((0, h - bot_h, img.width, h))
    combined_h = top_h + gap + bot_h
    combined = Image.new(img.mode, (img.width, combined_h), color="white")
    combined.paste(top_img, (0, 0))
    combined.paste(bot_img, (0, top_h + gap))
    return combined


def _iter_audit_images(execution_dir: Path) -> Iterable[Path]:
    """Yield every screenshot path under ``execution_dir/screenshots/``."""
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
            f"prod URL(s) in the URL bar ({prod_sample}). Profile "
            "policy requires testing on the configured pre-production storefront."
            + context
        ),
        "source": "env_check",
        "rule": "R9",
    }


def _build_prod_validation_finding(
    *,
    verdict: str,
    prod_urls: set[str],
    preprod_urls: set[str],
    images_with_prod: int,
    images_with_preprod: int,
) -> dict[str, Any]:
    """Construct the HIGH-severity env_check finding for a "Prod Validation" folder."""
    preprod_sample = ", ".join(sorted(preprod_urls)[:3])
    if len(preprod_urls) > 3:
        preprod_sample += " ..."
    if verdict == "mixed":
        context = (
            f" {images_with_prod} screenshot(s) also show prod URLs, "
            "indicating mixed-environment testing — still non-compliant: "
            "the whole flow must run on production."
        )
    else:
        context = " No production URL was detected in any screenshot."
    return {
        "severity": "high",
        "page": None,
        "step_index": None,
        "description": (
            "Environment compliance violation detected by deterministic "
            f"URL extraction: {images_with_preprod} screenshot(s) show "
            f"preprod URL(s) in the URL bar ({preprod_sample}). This is a "
            "Prod Validation folder — Profile policy requires testing on "
            "production (the configured production storefront), not feature-preprod." + context
        ),
        "source": "env_check",
        "rule": "R9",
    }


def _select_sampled_images(
    images: list[Path],
    sample_stride: int,
    sample_ends: int,
) -> list[Path]:
    """Pick the subset of `images` to actually OCR."""
    n = len(images)
    if sample_stride <= 1 or n == 0 or sample_ends * 2 >= n:
        return list(images)
    indices: set[int] = set()
    for i in range(sample_ends):
        indices.add(i)
        indices.add(n - 1 - i)
    for i in range(0, n, sample_stride):
        indices.add(i)
    return [images[i] for i in sorted(indices)]
