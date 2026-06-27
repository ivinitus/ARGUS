"""Unit + integration tests for env_check.py — OCR-based env compliance check.

The unit suite exercises the pure functions (regex, normalisation,
classification, finding shape) without invoking tesseract. The
integration suite monkey-patches ``env_check.ocr_image`` to return
canned strings so we can assert ``check_env_compliance`` behaviour
against a fake execution directory with an empty screenshots tree.

A ``live_ocr`` test group is gated behind tesseract availability so
CI without tesseract still passes — it generates a synthetic PNG and
asserts tesseract + our pipeline recover the URL end-to-end.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import env_check


# ---------------------------------------------------------------------------
# _normalise_ocr_text — targeted OCR-mangling repair
# ---------------------------------------------------------------------------
class TestNormalise:
    def test_compound_tld_comma_jp(self):
        assert "exampleapp.co.jp" in env_check._normalise_ocr_text(
            "feature-preprod.exampleapp.co,jp/foo"
        )

    def test_compound_tld_comma_uk(self):
        assert "co.uk" in env_check._normalise_ocr_text("exampleapp.co,uk/path")

    def test_missing_dot_com(self):
        assert env_check._normalise_ocr_text("orod.exampleappcom + :") == \
            "orod.exampleapp.com + :"

    def test_missing_dot_de(self):
        assert "ExampleApp.de" in env_check._normalise_ocr_text("ExampleAppde x")

    def test_subdomain_space_feature_preprod(self):
        assert "feature-preprod.exampleapp.de" in env_check._normalise_ocr_text(
            "https://feature-preprod exampleapp.de/foo"
        )

    def test_subdomain_space_www(self):
        assert "www.exampleapp.com" in env_check._normalise_ocr_text("www exampleapp.com")

    def test_private_merge_help(self):
        assert "help.exampleapp.co.jp" in env_check._normalise_ocr_text(
            "helpexampleapp.co,jp/s/?"
        )

    def test_private_merge_tracker(self):
        assert "tracker.exampleapp.com" in env_check._normalise_ocr_text("trackerexampleapp.com/browse")

    def test_prose_not_normalised(self):
        # "exampleapp, however" must NOT become "exampleapp.however"
        out = env_check._normalise_ocr_text("exampleapp, however, was great")
        assert "exampleapp.however" not in out
        # The comma stays in the text
        assert "exampleapp, however" in out


# ---------------------------------------------------------------------------
# extract_urls — regex + URL-context filter + classification
# ---------------------------------------------------------------------------
class TestExtractUrls:
    # ---- true positives: real URLs in URL-bar context -----------------

    def test_preprod_with_protocol_and_path(self):
        urls = env_check.extract_urls("https://feature-preprod.exampleapp.de/typ?foo=bar")
        assert urls == [("https://feature-preprod.exampleapp.de", "preprod")]

    def test_preprod_no_protocol_has_path(self):
        urls = env_check.extract_urls("feature-preprod.exampleapp.de/library")
        assert urls == [("feature-preprod.exampleapp.de", "preprod")]

    def test_preprod_japan_ocr_comma(self):
        urls = env_check.extract_urls("€ > G feature-preprod.exampleapp.co,jp/?x=1")
        assert urls == [("feature-preprod.exampleapp.co.jp", "preprod")]

    def test_prod_with_www_and_path(self):
        urls = env_check.extract_urls("© www.exampleapp.com/cart?x=1")
        assert urls == [("www.exampleapp.com", "prod")]

    def test_prod_bare_with_path(self):
        # Bare ``exampleapp.de`` in URL-context (path) classifies as prod.
        urls = env_check.extract_urls("€ C O 8 = exampleapp.de/?creativeld=foo")
        assert urls == [("exampleapp.de", "prod")]

    def test_prod_mobile_ocr_mangled(self):
        # Mobile Chrome URL bar with OCR noise: dot eaten, prefix garbled
        urls = env_check.extract_urls("fM 2 orod.exampleappcom + @©@ :")
        assert urls == [("orod.exampleapp.com", "prod")]

    def test_prod_mobile_www_clean(self):
        urls = env_check.extract_urls("4:30 www.exampleapp.com")
        assert urls == [("www.exampleapp.com", "prod")]

    # ---- private tools: classified as ignore -------------------------

    def test_tracker_private(self):
        urls = env_check.extract_urls("tracker.exampleapp.com/browse/FOO-1")
        assert urls == [("tracker.exampleapp.com", "ignore")]

    def test_api_private(self):
        urls = env_check.extract_urls("api.exampleapp.com/v1/foo")
        assert urls == [("api.exampleapp.com", "ignore")]

    def test_help_ocr_merged(self):
        # help.exampleapp.<tld> on a prod host = IGNORE (v4: help-locale
        # surfaces don't have a preprod environment, so legitimate
        # help-page navigation is acceptable on prod regardless of
        # whether the rest of the test runs on feature-preprod).
        urls = env_check.extract_urls("€ > G 23% helpexampleapp.co,jp/s/?x=1")
        assert urls == [("help.exampleapp.co.jp", "ignore")]

    # ---- false positives that MUST NOT match --------------------------

    def test_tab_title_ignored(self):
        # Browser tab titles render ``Vielen Dank | ExampleApp.de x`` — no
        # URL context and no subdomain → must not classify as prod.
        assert env_check.extract_urls("Vielen Dank | ExampleApp.de x") == []

    def test_prose_mention_ignored(self):
        assert env_check.extract_urls("Bei ExampleApp.de kannst du lesen") == []

    def test_short_mention_no_context_ignored(self):
        assert env_check.extract_urls("Great site: exampleapp.com today") == []

    def test_unknown_tld_ignored(self):
        # ``exampleapp.xyz`` is not in _EXAMPLEAPP_TLDS, must not match.
        assert env_check.extract_urls("https://exampleapp.xyz/foo") == []

    def test_brazil_tld_matched(self):
        # Regression: ``com.br`` was missing from _EXAMPLEAPP_TLDS, so every
        # Brazil URL was silently dropped before reaching preprod_urls —
        # disabling R13 marketplace-mismatch for the entire BR locale
        # (79 BR testruns in the corpus). The R13 map already had .com.br.
        urls = env_check.extract_urls("feature-preprod.exampleapp.com.br/library")
        assert urls == [("feature-preprod.exampleapp.com.br", "preprod")]
        prod = env_check.extract_urls("www.exampleapp.com.br/")
        assert prod == [("www.exampleapp.com.br", "prod")]


# ---------------------------------------------------------------------------
# _classify_host — direct subdomain classification
# ---------------------------------------------------------------------------
class TestClassifyHost:
    def test_empty_subdomain_is_prod(self):
        # Bare ``exampleapp.<tld>`` with no subdomain. Callers only apply
        # this after URL-context has been verified, so bare form here
        # means "the URL bar showed just the hostname".
        assert env_check._classify_host("") == "prod"

    def test_www_is_prod(self):
        assert env_check._classify_host("www") == "prod"

    def test_feature_preprod_is_preprod(self):
        assert env_check._classify_host("feature-preprod") == "preprod"

    def test_preprod_substring_is_preprod(self):
        assert env_check._classify_host("feature-preprod.eu-west-1") == "preprod"

    def test_staging_is_preprod(self):
        assert env_check._classify_host("staging") == "preprod"

    def test_dev_is_preprod(self):
        assert env_check._classify_host("dev") == "preprod"

    def test_dev_subdomain_component_is_preprod(self):
        # ``dev`` as a full component is still a real non-prod env.
        assert env_check._classify_host("dev.eu-west-1") == "preprod"

    def test_developer_is_not_preprod(self):
        # Regression: ``dev`` was matched as a SUBSTRING, so prod hosts
        # like developer.exampleapp.com / devices.exampleapp.com classified as
        # preprod, suppressing R9 prod-leak findings. They must be prod.
        assert env_check._classify_host("developer") == "prod"
        assert env_check._classify_host("devices") == "prod"

    def test_tracker_is_ignore(self):
        assert env_check._classify_host("tracker") == "ignore"

    def test_api_is_ignore(self):
        assert env_check._classify_host("api") == "ignore"

    def test_help_is_ignore(self):
        # help.exampleapp.<tld> classifies as IGNORE (v4 behaviour). Help
        # Center surfaces don't have a preprod environment, so a test
        # that legitimately navigates to help shows a prod help URL
        # — that's expected, not a violation. (v1: ignore, v2-v3:
        # prod, v4 reverted to ignore based on QA-lead feedback.)
        assert env_check._classify_host("help") == "ignore"

    def test_locale_help_subdomains_are_ignore(self):
        # Same logic for the locale-specific help subdomains across
        # all ExampleApp marketplaces. Each is the canonical Help Center
        # surface for its locale and has no preprod environment.
        assert env_check._classify_host("aiuto") == "ignore"   # IT
        assert env_check._classify_host("ikuto") == "ignore"   # IT (OCR misread)
        assert env_check._classify_host("ayuda") == "ignore"   # ES
        assert env_check._classify_host("aide") == "ignore"    # FR
        assert env_check._classify_host("merci") == "ignore"   # FR (additional)
        assert env_check._classify_host("hilfe") == "ignore"   # DE
        assert env_check._classify_host("ajuda") == "ignore"   # BR (PT)
        assert env_check._classify_host("support") == "ignore"  # legacy

    def test_accounts_is_prod(self):
        # Same logic as help — user-facing account management surface.
        assert env_check._classify_host("accounts") == "prod"

    def test_chk_is_prod(self):
        # Checkout — user-facing.
        assert env_check._classify_host("chk") == "prod"

    def test_search_is_prod(self):
        # Search — user-facing.
        assert env_check._classify_host("search") == "prod"

    def test_sso_is_ignore(self):
        # SSO is genuine auth infrastructure — tester might have an
        # SSO login tab open; doesn't represent app env state.
        assert env_check._classify_host("sso") == "ignore"

    def test_unknown_is_prod(self):
        # This is the load-bearing default: OCR-mangled subdomains
        # (``orod`` for ``prod``/``www``) must flag as prod rather than
        # silently fall through.
        assert env_check._classify_host("orod") == "prod"


# ---------------------------------------------------------------------------
# _has_url_context — URL-bar context detection
# ---------------------------------------------------------------------------
class TestUrlContext:
    def test_protocol_prefix_is_context(self):
        text = "https://exampleapp.com/foo"
        # matched span is 'exampleapp.com'
        assert env_check._has_url_context(text, text.index("exampleapp"), text.index("/foo"))

    def test_path_suffix_is_context(self):
        text = "exampleapp.com/path"
        assert env_check._has_url_context(text, 0, text.index("/"))

    def test_slashes_inside_match_is_context(self):
        text = "//exampleapp.com"
        # include the slashes in the match to simulate what the regex captures
        assert env_check._has_url_context(text, 0, len(text))

    def test_bare_hostname_no_context(self):
        text = "exampleapp.com"
        assert not env_check._has_url_context(text, 0, len(text))

    def test_prose_context_not_url(self):
        text = "try exampleapp.com today"
        start = text.index("exampleapp")
        end = start + len("exampleapp.com")
        assert not env_check._has_url_context(text, start, end)


# ---------------------------------------------------------------------------
# check_env_compliance — verdict + per-image + findings with mocked OCR
# ---------------------------------------------------------------------------
def _make_execution_dir(tmp_path: Path, images: dict[str, str]) -> Path:
    """Create a fake execution dir with empty image files matching keys.

    ``images`` maps relative path (under ``screenshots/``) to a placeholder.
    The actual byte content doesn't matter because ocr_image is mocked.
    """
    exec_dir = tmp_path / "exec"
    shots = exec_dir / "screenshots"
    shots.mkdir(parents=True)
    for rel in images:
        p = shots / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"")  # content irrelevant, we mock OCR
    return exec_dir


class TestBuildFinding:
    def test_violation_finding_shape(self):
        f = env_check._build_finding(
            verdict="violation",
            prod_urls={"www.exampleapp.com"},
            preprod_urls=set(),
            images_with_prod=3,
            images_with_preprod=0,
        )
        assert f["severity"] == "high"
        assert f["source"] == "env_check"
        assert f["rule"] == "R9"
        assert "www.exampleapp.com" in f["description"]
        assert "No feature-preprod" in f["description"]

    def test_mixed_finding_mentions_both(self):
        f = env_check._build_finding(
            verdict="mixed",
            prod_urls={"exampleapp.de"},
            preprod_urls={"feature-preprod.exampleapp.de"},
            images_with_prod=2,
            images_with_preprod=5,
        )
        assert "mixed" in f["description"].lower()
        # Preprod count is mentioned as context.
        assert "5" in f["description"]


# Tesseract-era tests (TestCheckEnvCompliance / TestUpscaleForOcr /
# TestLiveOcr / TestSampling / TestRegionCrop / TestBottomCrop and
# the trailing test_check_env_compliance_* module fns) were removed
# 2026-05-23 when env_check.check_env_compliance was retired in
# favour of env_check_haiku.check_env_compliance. The remaining tests
# above exercise the pure URL-classification helpers env_check_haiku
# reuses (URL regex, classifier, OCR-text normaliser, build-finding).
