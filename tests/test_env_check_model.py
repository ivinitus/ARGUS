"""Unit tests for env_check_model's pure classification layer."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from argus.config.models import PolicySettings
from argus.pipeline import env_check, env_check_model as ech


def _cls(url: str) -> str:
    return ech._classify_url_string(url)[1]


class TestClassifyBasics:
    def test_full_preprod(self):
        assert _cls("https://feature-preprod.example.com/library") == "preprod"

    def test_bare_prod(self):
        assert _cls("example.de/ep/plan-picker") == "prod"

    def test_www_prod(self):
        assert _cls("https://www.example.co.uk") == "prod"

    def test_internal_tool_ignored(self):
        assert _cls("jira.example.com/secure/Tests.jspa") == "ignore"

    def test_other_domain_ignored(self):
        assert _cls("https://tools.other.test/viewAccounts") == "ignore"

    def test_help_locale_ignored(self):
        assert _cls("hilfe.example.de/topic") == "ignore"


class TestTruncFlagDowngrade:
    """The v5 path: Haiku set truncated_left, sentinel #TRUNC appended."""

    def test_flagged_prod_read_downgrades(self):
        assert _cls("prod.example.com/cart#TRUNC") == "ignore"

    def test_flagged_bare_downgrades(self):
        assert _cls("example.com#TRUNC") == "ignore"

    def test_flagged_preprod_stays_preprod(self):
        assert _cls("preprod.example.com/library#TRUNC") == "preprod"

    def test_desktop_flagged_prod_read_stays_actionable(self):
        matched, cls = ech._classify_url_string(
            "example.it/typ?productBorrowedPreviously=false#TRUNC",
            trust_truncated_prod=True,
        )
        assert matched == (
            "example.it/typ?productBorrowedPreviously=false"
        )
        assert cls == "prod"


class TestTruncTailGuard:
    """v15: left-cut tails classify ignore even WITHOUT the flag."""

    def test_od_tail(self):
        assert _cls("od.example.co.uk") == "ignore"

    def test_rod_tail(self):
        assert _cls("rod.example.co.uk") == "ignore"

    def test_prod_tail(self):
        assert _cls("prod.example.co.uk") == "ignore"

    def test_d_tail(self):
        assert _cls("d.example.com") == "ignore"

    def test_eprod_tail(self):
        assert _cls("eprod.example.com") == "ignore"

    def test_with_scheme_and_path(self):
        assert _cls("https://prod.example.com/pd/B01ABC") == "ignore"

    def test_longer_tails_still_preprod(self):
        assert _cls("e-preprod.example.com") == "preprod"
        assert _cls("ature-preprod.example.de") == "preprod"

    def test_non_tail_leaves_stay_prod(self):
        assert _cls("m.example.com") == "prod"
        assert _cls("accounts.example.com") == "prod"
        assert _cls("orod.example.com") == "prod"


class TestEllipsisCompletion:
    def test_feature_pre_ellipsis(self):
        assert _cls("feature-pre…example.de/library") == "preprod"

    def test_feature_pre_three_dots(self):
        assert _cls("feature-pre...example.co.jp") == "preprod"


class TestMatchedTextShape:
    def test_returns_matched_host(self):
        matched, cls = ech._classify_url_string(
            "https://feature-preprod.example.com/library")
        assert cls == "preprod"
        assert "feature-preprod.example.com" in matched

    def test_discards_text_before_matched_host(self):
        matched, cls = ech._classify_url_string(
            "Browser label https://feature-preprod.example.com/library?x=1",
        )
        assert cls == "preprod"
        assert matched == (
            "https://feature-preprod.example.com/library?x=1"
        )

    def test_no_url_returns_empty_ignore(self):
        assert ech._classify_url_string("no urls here") == ("", "ignore")


class TestBareApexRegex:
    def test_matches_hallucination_shape(self):
        rx = ech._BARE_APEX_RX
        assert rx.match("https://example.com")
        assert rx.match("https://example.co.jp")
        assert rx.match("example.de")
        assert rx.match("example.co.uk/")

    def test_rejects_subdomained_and_pathed(self):
        rx = ech._BARE_APEX_RX
        assert not rx.match("www.example.com")
        assert not rx.match("jira.example.com")
        assert not rx.match("example.de/ep/plan-picker")
        assert not rx.match("feature-preprod.example.com")
        assert not rx.match("example.com/library?x=1")


class TestConfirmBareApexProds:
    """Decision layer of the v15(d) confirm pass — _confirm_hostname is monkeypatched; no Model API."""

    class _FakeCrop:
        size = (100, 20)

    def _run(self, monkeypatch, pr_top, second):
        monkeypatch.setattr(
            ech, "_confirm_hostname",
            lambda img, model_id, region, profile: second)
        return ech._confirm_bare_apex_prods(
            pr_top, self._FakeCrop(), "m", "r", None)

    def test_jira_reveal_drops_prod(self, monkeypatch):
        confirmed, pp = self._run(
            monkeypatch, ["https://example.com"],
            ("jira.example.com", True))
        assert confirmed == []
        assert pp == []

    def test_preprod_reveal_recovers(self, monkeypatch):
        confirmed, pp = self._run(
            monkeypatch, ["https://example.co.jp"],
            ("feature-preprod.example.co.jp", True))
        assert confirmed == []
        assert pp and "feature-preprod.example.co.jp" in pp[0]

    def test_agreeing_bare_read_keeps_prod(self, monkeypatch):
        confirmed, pp = self._run(
            monkeypatch, ["example.de"], ("example.de", True))
        assert confirmed == ["example.de"]
        assert pp == []

    def test_error_keeps_prod(self, monkeypatch):
        confirmed, pp = self._run(
            monkeypatch, ["https://example.com"], None)
        assert confirmed == ["https://example.com"]

    def test_incomplete_host_drops_as_inconclusive(self, monkeypatch):
        confirmed, pp = self._run(
            monkeypatch, ["https://example.com"], ("example.com", False))
        assert confirmed == []

    def test_non_bare_entries_untouched_without_call(self, monkeypatch):
        called = {"n": 0}

        def spy(img, model_id, region, profile):
            called["n"] += 1
            return ("example.de", True)
        monkeypatch.setattr(ech, "_confirm_hostname", spy)
        confirmed, pp = ech._confirm_bare_apex_prods(
            ["www.example.de/ep/plan-picker"], self._FakeCrop(),
            "m", "r", None)
        assert confirmed == ["www.example.de/ep/plan-picker"]
        assert called["n"] == 0

    def test_no_crop_skips_confirm(self):
        confirmed, pp = ech._confirm_bare_apex_prods(
            ["https://example.com"], None, "m", "r", None)
        assert confirmed == ["https://example.com"]


class TestConfirmFirefoxProds:
    class _FakeCrop:
        size = (100, 20)

    def _run(self, monkeypatch, pr_top, second):
        monkeypatch.setattr(
            ech,
            "_confirm_firefox_hostname",
            lambda *args, **kwargs: second,
        )
        return ech._confirm_firefox_prods(
            pr_top, self._FakeCrop(), "m", "r", None)

    def test_dim_preprod_prefix_is_recovered(self, monkeypatch):
        confirmed, preprod, suspect = self._run(
            monkeypatch,
            ["example.de/library/titles?ref=a_typ_nhe_library"],
            ("example.de", True, "feature-preprod", "high"),
        )
        assert confirmed == []
        assert preprod == ["feature-preprod.example.de"]
        assert suspect == []

    def test_full_preprod_hostname_is_recovered(self, monkeypatch):
        confirmed, preprod, suspect = self._run(
            monkeypatch,
            ["example.de/pd/example"],
            ("feature-preprod.example.de", True,
             "feature-preprod", "high"),
        )
        assert confirmed == []
        assert preprod == ["feature-preprod.example.de"]
        assert suspect == []

    def test_dim_portal_development_prefix_is_recovered(self, monkeypatch):
        env_check.configure(PolicySettings(
            storefront_domains=["portal", "shop"],
            expected_environment_markers=["rc", "g", "gf"],
        ))
        try:
            confirmed, preprod, suspect = self._run(
                monkeypatch,
                ["portal.com/producer/earnings"],
                ("portal.com", True, "d", "high"),
            )
            assert confirmed == ["d.portal.com"]
            assert preprod == []
            assert suspect == []
        finally:
            env_check.reset_configuration()

    def test_bare_apex_remains_advisory_even_with_high_confidence(
            self, monkeypatch):
        confirmed, preprod, suspect = self._run(
            monkeypatch,
            ["example.de/library"],
            ("example.de", True, "", "high"),
        )
        assert confirmed == []
        assert preprod == []
        assert suspect == ["example.de/library"]

    def test_complete_www_prod_is_confirmed(self, monkeypatch):
        confirmed, preprod, suspect = self._run(
            monkeypatch,
            ["www.example.de/library"],
            ("www.example.de", True, "www", "high"),
        )
        assert confirmed == ["www.example.de"]
        assert preprod == []
        assert suspect == []

    def test_uncertain_prefix_is_advisory(self, monkeypatch):
        confirmed, preprod, suspect = self._run(
            monkeypatch,
            ["example.de/library"],
            ("example.de", True, None, "low"),
        )
        assert confirmed == []
        assert preprod == []
        assert suspect == ["example.de/library"]

    def test_missing_crop_is_advisory(self):
        confirmed, preprod, suspect = ech._confirm_firefox_prods(
            ["example.de/library"], None, "m", "r", None)
        assert confirmed == []
        assert preprod == []
        assert suspect == ["example.de/library"]


def test_firefox_prompt_uses_active_storefront_and_policy_hosts():
    env_check.configure(PolicySettings(
        storefront_domains=["portal", "shop"],
        expected_environment_markers=["rc", "g", "gf"],
        environment_expected_hosts_by_run={
            "db regression": ["rc.portal.com"],
            "website regression": ["g.portal.com"],
        },
        environment_required_host_overrides={
            "PORTAL-T2547": ["d.portal.com"],
        },
    ))

    prompt = ech._firefox_confirmation_prompt()

    assert "Configured storefront labels: portal, shop" in prompt
    assert "d.portal.com" in prompt
    assert "bright fragment \"portal.com\"" in prompt
    env_check.reset_configuration()


class TestConfirmBottomProds:
    """v17: bottom-strip prod reads are fail-CLOSED behind a confirm pass before R9-hover can fire."""

    class _FakeCrop:
        size = (100, 20)

    def _run(self, monkeypatch, pr_bot, second):
        monkeypatch.setattr(
            ech, "_confirm_hostname", lambda *a, **k: second)
        return ech._confirm_bottom_prods(
            pr_bot, self._FakeCrop(), "m", "r", None)

    def test_tracking_host_reveal_drops(self, monkeypatch):
        confirmed, pp = self._run(
            monkeypatch, ["example.co.uk"],
            ("example.sc.omtrdc.net", True))
        assert confirmed == []
        assert pp == []

    def test_genuine_hover_prod_kept(self, monkeypatch):
        confirmed, pp = self._run(
            monkeypatch, ["www.example.co.uk"],
            ("www.example.co.uk", True))
        assert confirmed == ["www.example.co.uk"]
        assert pp == []

    def test_preprod_reveal_recovers(self, monkeypatch):
        confirmed, pp = self._run(
            monkeypatch, ["example.fr"],
            ("feature-preprod.example.fr", True))
        assert confirmed == []
        assert pp and "feature-preprod.example.fr" in pp[0]

    def test_error_marks_analysis_incomplete(self, monkeypatch):
        with pytest.raises(
            ech.analysis_status.AnalysisIncompleteError,
            match="could not reconcile",
        ):
            self._run(monkeypatch, ["example.co.uk"], None)

    def test_incomplete_bare_apex_agreement_still_drops(self, monkeypatch):
        confirmed, pp = self._run(
            monkeypatch, ["example.co.uk"], ("example.co.uk", False))
        assert confirmed == []

    def test_incomplete_but_agreeing_subdomained_read_kept(self, monkeypatch):
        confirmed, pp = self._run(
            monkeypatch, ["www.example.co.uk"],
            ("https://www.example.co.uk/pg/video-credits", False))
        assert confirmed == ["www.example.co.uk"]
        assert pp == []

    def test_incomplete_disagreeing_read_drops(self, monkeypatch):
        confirmed, pp = self._run(
            monkeypatch, ["www.example.co.uk"],
            ("www.example.de", False))
        assert confirmed == []

    def test_incomplete_left_cut_agreement_drops(self, monkeypatch):
        confirmed, pp = self._run(
            monkeypatch, ["prod.example.com"], ("prod.example.com", False))
        assert confirmed == []

    def test_no_crop_marks_analysis_incomplete(self, monkeypatch):
        monkeypatch.setattr(
            ech, "_confirm_hostname",
            lambda *a, **k: ("www.example.co.uk", True))
        with pytest.raises(
            ech.analysis_status.AnalysisIncompleteError,
            match="crop was unavailable",
        ):
            ech._confirm_bottom_prods(
                ["www.example.co.uk"], None, "m", "r", None)

    def test_empty_input_no_call(self, monkeypatch):
        called = {"n": 0}

        def spy(*a, **k):
            called["n"] += 1
            return ("www.example.co.uk", True)
        monkeypatch.setattr(ech, "_confirm_hostname", spy)
        confirmed, pp = ech._confirm_bottom_prods(
            [], self._FakeCrop(), "m", "r", None)
        assert confirmed == []
        assert called["n"] == 0


class TestApexEchoSuppression:
    """v16(a): same-strip preprod + apex prod = transcription echo."""

    def test_www_echo_dropped(self):
        kept, dropped = ech._suppress_apex_echoes(
            ["feature-preprod.example.co.jp"],
            ["https://www.example.co.jp"])
        assert kept == []
        assert dropped == ["https://www.example.co.jp"]

    def test_bare_echo_dropped(self):
        kept, dropped = ech._suppress_apex_echoes(
            ["feature-preprod.example.de"], ["example.de"])
        assert kept == []
        assert dropped == ["example.de"]

    def test_other_marketplace_kept(self):
        kept, dropped = ech._suppress_apex_echoes(
            ["feature-preprod.example.de"], ["https://www.example.com"])
        assert kept == ["https://www.example.com"]
        assert dropped == []

    def test_distinct_subdomain_kept(self):
        kept, dropped = ech._suppress_apex_echoes(
            ["feature-preprod.example.de"], ["accounts.example.de"])
        assert kept == ["accounts.example.de"]
        assert dropped == []

    def test_no_preprod_noop(self):
        kept, dropped = ech._suppress_apex_echoes([], ["example.de"])
        assert kept == ["example.de"]
        assert dropped == []


class TestSplitSuspectApexProds:
    """v16(b): bare-apex prod reads in an otherwise-preprod session."""

    def test_bare_apex_with_preprod_is_suspect(self):
        conf, susp = ech._split_suspect_apex_prods(
            {"example.de", "https://example.de"},
            {"feature-preprod.example.de"})
        assert conf == set()
        assert susp == {"example.de", "https://example.de"}

    def test_www_host_stays_confirmed(self):
        conf, susp = ech._split_suspect_apex_prods(
            {"https://www.example.ca"}, {"feature-preprod.example.ca"})
        assert conf == {"https://www.example.ca"}
        assert susp == set()

    def test_no_preprod_all_confirmed(self):
        conf, susp = ech._split_suspect_apex_prods({"example.de"}, set())
        assert conf == {"example.de"}
        assert susp == set()

    def test_other_marketplace_confirmed(self):
        conf, susp = ech._split_suspect_apex_prods(
            {"example.com"}, {"feature-preprod.example.de"})
        assert conf == {"example.com"}
        assert susp == set()


def _fake_rec(execution_dir, name, preprod_top=(), prod_top=(),
              prod_bottom=(), suspect_top=(), navigation_notices=()):
    """A canned _process_image record for check_env_compliance tests."""
    return {
        "image": str(execution_dir / "screenshots" / name),
        "urls": (
            list(preprod_top)
            + list(prod_top)
            + list(prod_bottom)
            + list(suspect_top)
            + [notice["url"] for notice in navigation_notices]
        ),
        "preprod_urls": list(preprod_top),
        "prod_urls": list(prod_top) + list(prod_bottom),
        "preprod_urls_top": list(preprod_top),
        "preprod_urls_bottom": [],
        "prod_urls_top": list(prod_top),
        "prod_urls_bottom": list(prod_bottom),
        "suspect_prod_urls_top": list(suspect_top),
        "navigation_notices": list(navigation_notices),
    }


class TestSessionSuspectDowngrade:
    """check_env_compliance end-to-end with _process_image mocked."""

    def _run(self, monkeypatch, tmp_path, recs, **kwargs):
        by_path = {r["image"]: r for r in recs}
        monkeypatch.setattr(
            ech.env_check, "_iter_audit_images",
            lambda d: [Path(r["image"]) for r in recs])
        monkeypatch.setattr(
            ech, "_process_image",
            lambda path, *a, **k: by_path[str(path)])
        return ech.check_env_compliance(
            tmp_path, region_crop_height=180,
            region_crop_bottom_height=80, **kwargs)

    def test_firefox_highlight_fp_downgraded(self, monkeypatch, tmp_path):
        result = self._run(monkeypatch, tmp_path, [
            _fake_rec(tmp_path, "page_001.jpg",
                      preprod_top=["feature-preprod.example.de"]),
            _fake_rec(tmp_path, "page_002.jpg", prod_top=["example.de"]),
        ])
        assert result["verdict"] == "compliant"
        rules = [f["rule"] for f in result["findings"]]
        assert rules == ["R9-susp"]
        assert result["findings"][0]["severity"] == "low"
        assert result["findings"][0]["advisory"] is True
        assert result["prod_urls"] == []
        assert result["suspect_prod_urls_top"] == ["example.de"]

    def test_genuine_www_prod_still_fires(self, monkeypatch, tmp_path):
        result = self._run(monkeypatch, tmp_path, [
            _fake_rec(tmp_path, "page_001.jpg",
                      preprod_top=["feature-preprod.example.ca"]),
            _fake_rec(tmp_path, "page_002.jpg",
                      prod_top=["https://www.example.ca"]),
        ])
        assert result["verdict"] == "mixed"
        rules = [f["rule"] for f in result["findings"]]
        assert "R9" in rules
        assert "R9-susp" not in rules

    def test_desktop_chrome_marketplace_switch_stays_violation(
            self, monkeypatch, tmp_path):
        result = self._run(monkeypatch, tmp_path, [
            _fake_rec(
                tmp_path,
                "page_001.jpg",
                preprod_top=["feature-preprod.example.com"],
            ),
            _fake_rec(
                tmp_path,
                "page_052.jpg",
                prod_top=["example.it"],
            ),
        ], expected_browser="Chrome",
           expected_execution_mode=ech.execution_policy.DESKTOP_BROWSER)
        assert result["verdict"] == "mixed"
        assert [f["rule"] for f in result["findings"]] == ["R9"]

    def test_prod_validation_not_downgraded(self, monkeypatch, tmp_path):
        result = self._run(monkeypatch, tmp_path, [
            _fake_rec(tmp_path, "page_001.jpg",
                      preprod_top=["feature-preprod.example.fr"]),
            _fake_rec(tmp_path, "page_002.jpg", prod_top=["example.fr"]),
        ], is_prod_validation=True)
        assert result["verdict"] == "mixed"
        rules = [f["rule"] for f in result["findings"]]
        assert "R9" in rules
        assert "R9-susp" not in rules

    def test_prod_only_session_still_violation(self, monkeypatch, tmp_path):
        result = self._run(monkeypatch, tmp_path, [
            _fake_rec(tmp_path, "page_001.jpg", prod_top=["example.de"]),
        ])
        assert result["verdict"] == "violation"
        assert [f["rule"] for f in result["findings"]] == ["R9"]
        assert result["prod_urls_top"] == ["example.de"]

    def test_firefox_uncertain_prod_only_is_advisory(
            self, monkeypatch, tmp_path):
        result = self._run(monkeypatch, tmp_path, [
            _fake_rec(
                tmp_path,
                "page_001.jpg",
                suspect_top=["example.de/library"],
            ),
        ], expected_browser="Firefox")
        assert result["verdict"] == "ambiguous"
        assert [f["rule"] for f in result["findings"]] == ["R9-susp"]
        assert result["findings"][0]["severity"] == "low"
        assert result["suspect_prod_urls_top"] == ["example.de/library"]

    def test_mixed_firefox_and_session_suspects_get_source_explanations(
            self, monkeypatch, tmp_path):
        result = self._run(monkeypatch, tmp_path, [
            _fake_rec(
                tmp_path,
                "page_001.jpg",
                preprod_top=["feature-preprod.example.de"],
                suspect_top=["example.de/library"],
            ),
            _fake_rec(
                tmp_path,
                "page_002.jpg",
                prod_top=["example.de"],
            ),
        ], expected_browser="Firefox")

        descriptions = [
            finding["description"] for finding in result["findings"]
        ]
        assert len(descriptions) == 2
        assert any(
            "example.de/library" in description
            and "Firefox emphasizes the registrable domain" in description
            for description in descriptions
        )
        assert any(
            "example.de):" in description
            and "Other screenshots in this session" in description
            for description in descriptions
        )

    def test_bottom_prod_url_is_diagnostic_only(
            self, monkeypatch, tmp_path):
        result = self._run(monkeypatch, tmp_path, [
            _fake_rec(
                tmp_path,
                "page_001.jpg",
                preprod_top=["feature-preprod.example.de"],
                prod_bottom=["www.example.de"],
            ),
        ])
        assert result["verdict"] == "compliant"
        assert result["prod_urls_bottom"] == ["www.example.de"]
        assert "R9" not in [finding["rule"] for finding in result["findings"]]
        assert "R9-hover" not in [
            finding["rule"] for finding in result["findings"]
        ]

    def test_approved_navigation_does_not_also_emit_ambiguous_finding(
            self, monkeypatch, tmp_path):
        result = self._run(monkeypatch, tmp_path, [
            _fake_rec(
                tmp_path,
                "page_001.jpg",
                navigation_notices=[{
                    "url": "www.example.com/legal/conditions-of-use",
                    "label": "Conditions of Use",
                    "source": "address_bar",
                }],
            ),
        ])

        assert result["verdict"] == "ambiguous"
        assert [finding["rule"] for finding in result["findings"]] == [
            "R9-nav",
        ]

    def test_webview_context_recovers_midframe_preprod(
            self, monkeypatch, tmp_path):
        rec = _fake_rec(tmp_path, "page_001.jpg")
        monkeypatch.setattr(
            ech,
            "_process_webview_context_image",
            lambda path, *a, **k: {
                "image": str(path),
                "preprod_urls": ["feature-preprod.example.in"],
            },
        )
        result = self._run(
            monkeypatch, tmp_path, [rec], is_webview=True)
        assert result["verdict"] == "compliant"
        assert result["preprod_urls"] == [
            "feature-preprod.example.in"]
        assert "R9-amb" not in [
            finding["rule"] for finding in result["findings"]]
        assert result["per_image"][0]["source"] == "webview_context"

    def test_non_webview_does_not_run_context(
            self, monkeypatch, tmp_path):
        rec = _fake_rec(tmp_path, "page_001.jpg")

        def fail_if_called(*args, **kwargs):
            raise AssertionError("context recovery should be Webview-only")

        monkeypatch.setattr(
            ech, "_process_webview_context_image", fail_if_called)
        result = self._run(monkeypatch, tmp_path, [rec])
        assert result["verdict"] == "ambiguous"
        assert [finding["rule"] for finding in result["findings"]] == [
            "R9-amb",
        ]
        assert result["findings"][0]["severity"] == "medium"
        assert result["findings"][0]["advisory"] is False

    def test_mobile_missing_url_is_low_advisory(
            self, monkeypatch, tmp_path):
        result = self._run(
            monkeypatch,
            tmp_path,
            [_fake_rec(tmp_path, "page_001.jpg")],
            missing_url_is_advisory=True,
        )
        [finding] = result["findings"]
        assert finding["rule"] == "R9-amb"
        assert finding["severity"] == "low"
        assert finding["advisory"] is True


class TestDesktopTruncatedProd:
    def _run(self, monkeypatch, tmp_path, expected_execution_mode):
        from PIL import Image as PILImage

        image_path = tmp_path / "page_052.jpg"
        PILImage.new("RGB", (200, 300), "white").save(image_path)
        reads = iter([
            ["example.it/typ?productBorrowedPreviously=false#TRUNC"],
            [],
        ])
        monkeypatch.setattr(
            ech,
            "_extract_urls_haiku",
            lambda *args, **kwargs: next(reads),
        )
        return ech._process_image(
            image_path,
            "model",
            "region",
            None,
            180,
            80,
            expected_browser="Chrome",
            expected_execution_mode=expected_execution_mode,
        )

    def test_desktop_chrome_keeps_prod_url(self, monkeypatch, tmp_path):
        result = self._run(
            monkeypatch,
            tmp_path,
            ech.execution_policy.DESKTOP_BROWSER,
        )
        assert result["prod_urls_top"] == [
            "example.it/typ?productBorrowedPreviously=false",
        ]

    def test_mobile_chrome_keeps_truncation_guard(
            self, monkeypatch, tmp_path):
        result = self._run(
            monkeypatch,
            tmp_path,
            ech.execution_policy.MOBILE_EMULATION_ALLOWED,
        )
        assert result["prod_urls_top"] == []


class TestMarketplaceConfirmation:
    @pytest.mark.parametrize("second, confirmed, uncertain", [
        (("feature-preprod.example.in", True),
         ["feature-preprod.example.in"], []),
        (("feature-preprod.example.ca", True),
         ["feature-preprod.example.ca/ca/Religion-Spirituality"], []),
        (("feature-preprod.example.ca", False),
         [], ["feature-preprod.example.ca/ca/Religion-Spirituality"]),
        (("feature-preprod.example.de", True),
         [], ["feature-preprod.example.ca/ca/Religion-Spirituality"]),
        (None, [], ["feature-preprod.example.ca/ca/Religion-Spirituality"]),
    ])
    def test_foreign_hostname_needs_independent_confirmation(
            self, monkeypatch, second, confirmed, uncertain):
        monkeypatch.setattr(
            ech, "_confirm_hostname", lambda *args, **kwargs: second)
        actual = ech._confirm_marketplace_urls(
            ["feature-preprod.example.ca/ca/Religion-Spirituality"],
            object(), "IN", "model", "region", None,
        )
        assert actual == (confirmed, uncertain)

    def test_assigned_marketplace_needs_no_extra_call(self, monkeypatch):
        def unexpected(*args, **kwargs):
            pytest.fail("Assigned marketplace should not need confirmation")

        monkeypatch.setattr(ech, "_confirm_hostname", unexpected)
        urls = ["feature-preprod.example.in/cat/Religion-Spirituality"]
        assert ech._confirm_marketplace_urls(
            urls, object(), "IN", "model", "region", None,
        ) == (urls, [])

    def test_unavailable_confirmation_is_advisory(self, monkeypatch):
        def unavailable(*args, **kwargs):
            raise ech.analysis_status.AnalysisIncompleteError(
                "environment", "confirmation unavailable")

        monkeypatch.setattr(ech, "_confirm_hostname", unavailable)
        urls = ["feature-preprod.example.ca/cat/Religion-Spirituality"]
        assert ech._confirm_marketplace_urls(
            urls, object(), "IN", "model", "region", None,
        ) == ([], urls)
        assert ech._confirm_marketplace_urls(
            urls, None, "IN", "model", "region", None,
        ) == ([], urls)

    def test_e220304_misread_and_hover_do_not_create_r13(
            self, monkeypatch, tmp_path):
        """Replay the two saved misreads through extraction and R13."""
        from PIL import Image as PILImage
        from argus.domain import workflow_rules
        from argus.pipeline import env_check_merge

        shots = tmp_path / "screenshots"
        shots.mkdir()
        for name in ("page_001.jpg", "page_006.jpg"):
            PILImage.new("RGB", (800, 600), "white").save(shots / name)
        reads = iter([
            ["feature-preprod.example.in/"],
            ["feature-preprod.example.de/subscription/confirmation"],
            ["feature-preprod.example.ca/ca/Religion-Spirituality"],
            [],
        ])
        monkeypatch.setattr(
            ech, "_extract_urls_haiku", lambda *args, **kwargs: next(reads))
        calls = []

        def confirm(crop, *args, **kwargs):
            calls.append(crop.size)
            return "feature-preprod.example.in", True

        monkeypatch.setattr(ech, "_confirm_hostname", confirm)
        result = ech.check_env_compliance(
            tmp_path, region_crop_height=180, region_crop_bottom_height=80,
            expected_browser="Chrome", expected_execution_mode="desktop_browser",
            expected_marketplace="IN", max_workers=1,
        )
        assert calls == [(800, 180)]
        assert result["marketplace_urls"] == [
            "feature-preprod.example.in", "feature-preprod.example.in/",
        ]
        assert result["marketplace_uncertain_urls"] == []
        # Preserve the original model readings as diagnostics, without
        # treating the bottom hover reading as active navigation.
        assert any(".de/" in url for url in result["preprod_urls"])
        assert any(".ca/" in url for url in result["preprod_urls"])
        saved = env_check_merge.build_env_check_field(result)
        assert workflow_rules.check_marketplace_match(
            {"test_results": [{"marketplace": "IN"}]},
            {"env_check": saved},
        ) == []
        page6 = next(row for row in saved["per_image"]
                     if row["image"].endswith("page_006.jpg"))
        assert page6["marketplace_urls"] == ["feature-preprod.example.in"]


class TestBottomSourceGuard:
    def _run(self, monkeypatch, tmp_path, source):
        from PIL import Image as PILImage

        image_path = tmp_path / "page.jpg"
        PILImage.new("RGB", (200, 300), "white").save(image_path)
        reads = iter([[], ["https://www.example.de"]])
        monkeypatch.setattr(
            ech, "_extract_urls_haiku",
            lambda *args, **kwargs: next(reads),
        )
        monkeypatch.setattr(
            ech, "_confirm_bottom_prods",
            lambda urls, *args, **kwargs: (urls, []),
        )
        monkeypatch.setattr(
            ech, "_classify_bottom_source",
            lambda *args, **kwargs: source,
        )
        return ech._process_image(
            image_path, "model", "region", None, 180, 80)

    def test_mail_client_bottom_url_is_dropped(self, monkeypatch, tmp_path):
        result = self._run(monkeypatch, tmp_path, "email_client")
        assert result["prod_urls_bottom"] == []
        assert result["prod_urls"] == []

    def test_real_browser_hover_url_is_retained(self, monkeypatch, tmp_path):
        result = self._run(monkeypatch, tmp_path, "browser_status")
        assert result["prod_urls_bottom"] == [
            "https://www.example.de"]


class TestNormaliseExpectedDevice:
    def test_every_extractor_token_maps_to_a_family(self):
        from argus.pipeline import device_check as dc
        from argus.pipeline.extractor import parse_device_from_testrun_name
        expected_family = {
            "Samsung": "android", "Pixel": "android", "Android": "android",
            "iPhone": "ios", "iPad": "ios", "iOS": "ios",
        }
        for token, family in expected_family.items():
            parsed = parse_device_from_testrun_name(
                f"E2E Flow - GMA - US Mobile - Chrome - {token}")
            assert parsed == token
            assert dc._normalise_expected_device(parsed) == family, token

    def test_desktop_and_unknown(self):
        from argus.pipeline import device_check as dc
        assert dc._normalise_expected_device("Desktop") == "desktop"
        assert dc._normalise_expected_device("Web") == "desktop"
        assert dc._normalise_expected_device(None) is None
        assert dc._normalise_expected_device("") is None
        assert dc._normalise_expected_device("Roku") is None


class TestNonDictJsonResponse:
    """Non-object model output remains incomplete after a bounded retry."""

    def _run_with_response(self, monkeypatch, text):
        class _Client:
            def invoke_model(self, model_id, body):
                import io as _io
                import json as _json
                payload = {"content": [{"type": "text", "text": text}],
                           "stop_reason": "end_turn"}
                return {"body": _io.BytesIO(_json.dumps(payload).encode())}

        monkeypatch.setattr(ech, "_get_client", lambda *a, **kw: _Client())
        from PIL import Image as PILImage
        return ech._extract_urls_haiku(
            PILImage.new("RGB", (8, 8), "white"), "m", "https://api.anthropic.com/v1/messages", None)

    def test_bare_list_is_incomplete(self, monkeypatch):
        with pytest.raises(ech.analysis_status.AnalysisIncompleteError):
            self._run_with_response(
                monkeypatch,
                '["feature-preprod.example.com/library"]',
            )

    def test_bare_string_is_incomplete(self, monkeypatch):
        with pytest.raises(ech.analysis_status.AnalysisIncompleteError):
            self._run_with_response(monkeypatch, '"no urls"')

    def test_bare_number_is_incomplete(self, monkeypatch):
        with pytest.raises(ech.analysis_status.AnalysisIncompleteError):
            self._run_with_response(monkeypatch, "42")


def _model_client_response(text: str) -> dict:
    payload = {
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
    }
    return {"body": io.BytesIO(json.dumps(payload).encode())}


def _model_client_tool_response(name: str, tool_input: dict) -> dict:
    payload = {
        "content": [{
            "type": "tool_use",
            "id": "toolu_test",
            "name": name,
            "input": tool_input,
        }],
        "stop_reason": "tool_use",
    }
    return {"body": io.BytesIO(json.dumps(payload).encode())}


def test_strict_json_accepts_forced_tool_output(monkeypatch):
    calls = []

    def fake_invoke(client, **kwargs):
        calls.append(kwargs)
        return _model_client_tool_response(
            ech._URL_TOOL_NAME,
            {
                "urls": [{
                    "url": "feature-preprod.example.com",
                    "truncated_left": False,
                }],
            },
        )

    monkeypatch.setattr(ech.model_client, "invoke_model", fake_invoke)

    result = ech._invoke_strict_json(
        object(),
        model_id="model",
        body={"messages": []},
        operation="env_check",
        validator=ech._validate_urls_response,
        usage_tracker=None,
        tool_name=ech._URL_TOOL_NAME,
    )

    assert result == ["feature-preprod.example.com"]
    assert len(calls) == 1


def test_tool_validators_ignore_additional_model_metadata():
    assert ech._validate_urls_response({
        "urls": [{
            "url": "feature-preprod.example.com",
            "truncated_left": False,
            "confidence": "high",
        }],
        "explanation": "address bar",
    }) == ["feature-preprod.example.com"]
    assert ech._validate_source_response({
        "source": "browser_status",
        "confidence": "high",
    }) == "browser_status"
    assert ech._validate_hostname_response({
        "host": "feature-preprod.example.com",
        "complete": True,
        "confidence": "high",
    }) == ("feature-preprod.example.com", True)


def test_strict_json_rejects_multiple_forced_tool_calls(monkeypatch):
    payload = {
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_one",
                "name": ech._URL_TOOL_NAME,
                "input": {"urls": []},
            },
            {
                "type": "tool_use",
                "id": "toolu_two",
                "name": ech._URL_TOOL_NAME,
                "input": {"urls": []},
            },
        ],
        "stop_reason": "tool_use",
    }
    calls = 0

    def fake_invoke(client, **kwargs):
        nonlocal calls
        calls += 1
        return {"body": io.BytesIO(json.dumps(payload).encode())}

    monkeypatch.setattr(ech.model_client, "invoke_model", fake_invoke)

    with pytest.raises(
        ech.analysis_status.AnalysisIncompleteError,
        match="multiple tool calls",
    ):
        ech._invoke_strict_json(
            object(),
            model_id="model",
            body={"messages": []},
            operation="env_check",
            validator=ech._validate_urls_response,
            usage_tracker=None,
            tool_name=ech._URL_TOOL_NAME,
        )
    assert calls == 3


def test_strict_json_retries_malformed_response_once(monkeypatch):
    responses = iter([
        _model_client_response('{"urls":'),
        _model_client_response(
            '{"urls":[{"url":"feature-preprod.example.com",'
            '"truncated_left":false}]}'
        ),
    ])
    calls = []

    def fake_invoke(client, **kwargs):
        calls.append(kwargs)
        return next(responses)

    monkeypatch.setattr(ech.model_client, "invoke_model", fake_invoke)

    result = ech._invoke_strict_json(
        object(),
        model_id="model",
        body={"messages": []},
        operation="env_check",
        validator=ech._validate_urls_response,
        usage_tracker=None,
    )

    assert result == ["feature-preprod.example.com"]
    assert len(calls) == 2
    assert all(call["allow_untagged_multimodal"] is True for call in calls)
    firstbody = json.loads(calls[0]["body"])
    retrybody = json.loads(calls[1]["body"])
    assert firstbody == {"messages": []}
    assert retrybody["messages"][0]["role"] == "assistant"
    assert retrybody["messages"][0]["content"][0]["text"] == '{"urls":'
    repair_text = retrybody["messages"][1]["content"][0]["text"]
    assert "exactly ONE complete JSON object" in repair_text
    assert "second JSON object" in repair_text


@pytest.mark.parametrize(
    "response_text",
    [
        (
            "```json\n"
            '{"urls":[{"url":"feature-preprod.example.com",'
            '"truncated_left":false}]}\n'
            "```"
        ),
        (
            "The result is:\n"
            '{"urls":[{"url":"feature-preprod.example.com",'
            '"truncated_left":false}]}'
        ),
    ],
)
def test_strict_json_accepts_single_object_envelopes_without_retry(
    monkeypatch,
    response_text,
):
    calls = 0

    def fake_invoke(client, **kwargs):
        nonlocal calls
        calls += 1
        return _model_client_response(response_text)

    monkeypatch.setattr(ech.model_client, "invoke_model", fake_invoke)

    result = ech._invoke_strict_json(
        object(),
        model_id="model",
        body={"messages": []},
        operation="env_check",
        validator=ech._validate_urls_response,
        usage_tracker=None,
    )

    assert result == ["feature-preprod.example.com"]
    assert calls == 1


def test_strict_json_rejects_multiple_objects(monkeypatch):
    calls = 0

    def fake_invoke(client, **kwargs):
        nonlocal calls
        calls += 1
        return _model_client_response('{"urls":[]}\n{"urls":[]}')

    monkeypatch.setattr(ech.model_client, "invoke_model", fake_invoke)

    with pytest.raises(
        ech.analysis_status.AnalysisIncompleteError,
        match="multiple JSON objects",
    ):
        ech._invoke_strict_json(
            object(),
            model_id="model",
            body={"messages": []},
            operation="env_check",
            validator=ech._validate_urls_response,
            usage_tracker=None,
        )
    assert calls == 3


def test_strict_json_schema_omission_remains_incomplete(monkeypatch):
    calls = 0

    def fake_invoke(client, **kwargs):
        nonlocal calls
        calls += 1
        return _model_client_response('{"unexpected":[]}')

    monkeypatch.setattr(ech.model_client, "invoke_model", fake_invoke)

    with pytest.raises(
        ech.analysis_status.AnalysisIncompleteError,
        match="remained invalid after 3 attempts",
    ):
        ech._invoke_strict_json(
            object(),
            model_id="model",
            body={"messages": []},
            operation="env_check",
            validator=ech._validate_urls_response,
            usage_tracker=None,
        )
    assert calls == 3


def test_strict_json_uses_bounded_fallback_for_schema_omission(monkeypatch):
    calls = []

    def fake_invoke(client, **kwargs):
        calls.append(kwargs)
        if kwargs["model_id"] == ech.DEFAULT_MODEL_ID:
            return _model_client_tool_response(ech._URL_TOOL_NAME, {})
        return _model_client_tool_response(
            ech._URL_TOOL_NAME,
            {
                "urls": [{
                    "url": "feature-preprod.example.com",
                    "truncated_left": False,
                }],
            },
        )

    monkeypatch.setattr(ech.model_client, "invoke_model", fake_invoke)

    result = ech._invoke_strict_json(
        object(),
        model_id=ech.DEFAULT_MODEL_ID,
        body={
            "temperature": 0.0, "messages": [],
            "tools": [ech._URL_TOOL],
            "tool_choice": {"type": "tool", "name": ech._URL_TOOL_NAME},
        },
        operation="env_check",
        validator=ech._validate_urls_response,
        usage_tracker=None,
        tool_name=ech._URL_TOOL_NAME,
        malformed_response_fallback_model_id=(
            ech.DEFAULT_MALFORMED_RESPONSE_FALLBACK_MODEL_ID
        ),
    )

    assert result == ["feature-preprod.example.com"]
    assert [call["model_id"] for call in calls] == [
        ech.DEFAULT_MODEL_ID,
        ech.DEFAULT_MODEL_ID,
        ech.DEFAULT_MODEL_ID,
        ech.DEFAULT_MALFORMED_RESPONSE_FALLBACK_MODEL_ID,
    ]
    for call in calls:
        assert "temperature" not in json.loads(call["body"])
    primary = json.loads(calls[0]["body"])
    assert primary["thinking"] == {"type": "disabled"}
    assert primary["tool_choice"] == {"type": "tool", "name": ech._URL_TOOL_NAME}
    fallback = json.loads(calls[-1]["body"])
    assert fallback["thinking"] == {"type": "between_tools"}
    assert fallback["tool_choice"] == {"type": "auto"}
    assert f"calling the {ech._URL_TOOL_NAME} tool" in fallback["system"]


def test_strict_json_fallback_remains_fail_closed(monkeypatch):
    calls = 0

    def fake_invoke(client, **kwargs):
        nonlocal calls
        calls += 1
        return _model_client_tool_response(ech._URL_TOOL_NAME, {})

    monkeypatch.setattr(ech.model_client, "invoke_model", fake_invoke)

    with pytest.raises(
        ech.analysis_status.AnalysisIncompleteError,
        match="remained invalid after 4 attempts",
    ):
        ech._invoke_strict_json(
            object(),
            model_id="haiku",
            body={"messages": []},
            operation="env_check",
            validator=ech._validate_urls_response,
            usage_tracker=None,
            tool_name=ech._URL_TOOL_NAME,
            malformed_response_fallback_model_id=(
                ech.DEFAULT_MALFORMED_RESPONSE_FALLBACK_MODEL_ID
            ),
        )
    assert calls == 4


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("AccessDeniedException"),
        ech.model_client.ModelInterventionError("guardrail intervened"),
    ],
)
def test_strict_json_does_not_retry_invocation_failure(monkeypatch, error):
    calls = 0

    def fake_invoke(client, **kwargs):
        nonlocal calls
        calls += 1
        raise error

    monkeypatch.setattr(ech.model_client, "invoke_model", fake_invoke)

    with pytest.raises(type(error), match=str(error)):
        ech._invoke_strict_json(
            object(),
            model_id="model",
            body={"messages": []},
            operation="env_check",
            validator=ech._validate_urls_response,
            usage_tracker=None,
        )
    assert calls == 1


class TestSystemPromptCaching:
    """The system prompt must go out as a cache-marked block list."""

    def _capturebody(self, monkeypatch, module, fn, *args, **kwargs):
        captured = {}

        class _Client:
            def invoke_model(self, model_id, body):
                import io as _io
                import json as _json
                captured["body"] = body
                response_text = (
                    '{"urls":[]}' if fn is ech._extract_urls_haiku else "{}"
                )
                payload = {"content": [{
                    "type": "text",
                    "text": response_text,
                }],
                           "stop_reason": "end_turn"}
                return {"body": _io.BytesIO(_json.dumps(payload).encode())}

        monkeypatch.setattr(module, "_get_client", lambda *a, **kw: _Client())
        fn(*args, **kwargs)
        return captured["body"]

    def _tiny_image(self):
        from PIL import Image as PILImage
        return PILImage.new("RGB", (8, 8), "white")

    def test_env_check_system_prompt_is_cached_block(self, monkeypatch):
        body = self._capturebody(
            monkeypatch, ech, ech._extract_urls_haiku,
            self._tiny_image(), "model-x", "https://api.anthropic.com/v1/messages", None,
        )
        assert body["system"] == [{
            "type": "text",
            "text": ech.SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }]
        assert body["tools"] == [ech._URL_TOOL]
        assert body["tool_choice"] == {
            "type": "tool",
            "name": ech._URL_TOOL_NAME,
        }

    def test_device_check_system_prompt_is_cached_block(self, monkeypatch):
        from argus.pipeline import device_check as dc
        body = self._capturebody(
            monkeypatch, dc, dc._classify_one_image,
            self._tiny_image(), "model-x", "https://api.anthropic.com/v1/messages", None,
        )
        assert body["system"] == [{
            "type": "text",
            "text": dc.SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }]
