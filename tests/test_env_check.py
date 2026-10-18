"""Unit + integration tests for env_check.py — OCR-based env compliance check."""
from __future__ import annotations

from pathlib import Path

import pytest
from argus.pipeline import env_check


@pytest.fixture(autouse=True)
def _reset_env_check_policy():
    env_check.reset_configuration()
    yield
    env_check.reset_configuration()


class TestNormalise:
    def test_compound_tld_comma_jp(self):
        assert "example.co.jp" in env_check._normalise_ocr_text(
            "feature-preprod.example.co,jp/foo"
        )

    def test_compound_tld_comma_uk(self):
        assert "co.uk" in env_check._normalise_ocr_text("example.co,uk/path")

    def test_missing_dot_com(self):
        assert env_check._normalise_ocr_text("orod.examplecom + :") == \
            "orod.example.com + :"

    def test_missing_dot_de(self):
        assert "example.de" in env_check._normalise_ocr_text("examplede x")

    def test_subdomain_space_feature_preprod(self):
        assert "feature-preprod.example.de" in env_check._normalise_ocr_text(
            "https://feature-preprod example.de/foo"
        )

    def test_subdomain_space_www(self):
        assert "www.example.com" in env_check._normalise_ocr_text("www example.com")

    def test_internal_merge_help(self):
        assert "help.example.co.jp" in env_check._normalise_ocr_text(
            "helpexample.co,jp/s/?"
        )

    def test_internal_merge_jira(self):
        assert "jira.example.com" in env_check._normalise_ocr_text("jiraexample.com/browse")

    def test_prose_not_normalised(self):
        out = env_check._normalise_ocr_text("audible, however, was great")
        assert "example.however" not in out
        assert "audible, however" in out


class TestExtractUrls:

    def test_preprod_with_protocol_and_path(self):
        urls = env_check.extract_urls("https://feature-preprod.example.de/typ?foo=bar")
        assert urls == [("https://feature-preprod.example.de", "preprod")]

    def test_preprod_no_protocol_has_path(self):
        urls = env_check.extract_urls("feature-preprod.example.de/library")
        assert urls == [("feature-preprod.example.de", "preprod")]

    def test_preprod_japan_ocr_comma(self):
        urls = env_check.extract_urls("€ > G feature-preprod.example.co,jp/?x=1")
        assert urls == [("feature-preprod.example.co.jp", "preprod")]

    def test_prod_with_www_and_path(self):
        urls = env_check.extract_urls("© www.example.com/cart?x=1")
        assert urls == [("www.example.com", "prod")]

    def test_prod_bare_with_path(self):
        urls = env_check.extract_urls("€ C O 8 = example.de/?creativeld=foo")
        assert urls == [("example.de", "prod")]

    def test_prod_mobile_ocr_mangled(self):
        urls = env_check.extract_urls("fM 2 orod.examplecom + @©@ :")
        assert urls == [("orod.example.com", "prod")]

    def test_prod_mobile_www_clean(self):
        urls = env_check.extract_urls("4:30 www.example.com")
        assert urls == [("www.example.com", "prod")]


    def test_jira_internal(self):
        urls = env_check.extract_urls("jira.example.com/browse/FOO-1")
        assert urls == [("jira.example.com", "ignore")]

    def test_api_internal(self):
        urls = env_check.extract_urls("api.example.com/v1/foo")
        assert urls == [("api.example.com", "ignore")]

    def test_help_ocr_merged(self):
        urls = env_check.extract_urls("€ > G 23% helpexample.co,jp/s/?x=1")
        assert urls == [("help.example.co.jp", "ignore")]


    def test_tab_title_ignored(self):
        assert env_check.extract_urls("Vielen Dank | example.de x") == []

    def test_prose_mention_ignored(self):
        assert env_check.extract_urls("Bei example.de kannst du lesen") == []

    def test_short_mention_no_context_ignored(self):
        assert env_check.extract_urls("Great site: example.com today") == []

    def test_unknown_tld_ignored(self):
        assert env_check.extract_urls("https://example.xyz/foo") == []

    def test_brazil_tld_matched(self):
        urls = env_check.extract_urls("feature-preprod.example.com.br/library")
        assert urls == [("feature-preprod.example.com.br", "preprod")]
        prod = env_check.extract_urls("www.example.com.br/")
        assert prod == [("www.example.com.br", "prod")]


class TestClassifyHost:
    def test_empty_subdomain_is_prod(self):
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
        assert env_check._classify_host("dev.eu-west-1") == "preprod"

    def test_developer_is_not_preprod(self):
        assert env_check._classify_host("developer") == "prod"
        assert env_check._classify_host("devices") == "prod"

    def test_jira_is_ignore(self):
        assert env_check._classify_host("jira") == "ignore"

    def test_api_is_ignore(self):
        assert env_check._classify_host("api") == "ignore"

    def test_help_is_ignore(self):
        assert env_check._classify_host("help") == "ignore"

    def test_locale_help_subdomains_are_ignore(self):
        assert env_check._classify_host("aiuto") == "ignore"
        assert env_check._classify_host("ikuto") == "ignore"
        assert env_check._classify_host("ayuda") == "ignore"
        assert env_check._classify_host("aide") == "ignore"
        assert env_check._classify_host("merci") == "ignore"
        assert env_check._classify_host("hilfe") == "ignore"
        assert env_check._classify_host("ajuda") == "ignore"
        assert env_check._classify_host("support") == "ignore"

    def test_accounts_is_prod(self):
        assert env_check._classify_host("accounts") == "prod"

    def test_chk_is_prod(self):
        assert env_check._classify_host("chk") == "prod"

    def test_search_is_prod(self):
        assert env_check._classify_host("search") == "prod"

    def test_sso_is_ignore(self):
        assert env_check._classify_host("sso") == "ignore"

    def test_unknown_is_prod(self):
        assert env_check._classify_host("orod") == "prod"


def _contextual_policy():
    from argus.config.models import PolicySettings

    return PolicySettings(
        storefront_domains=["portal", "example"],
        expected_environment_markers=["rc", "g", "gf"],
        environment_expected_hosts_by_run={
            "db regression": ["rc.portal.com"],
            "website regression": ["g.portal.com"],
        },
        environment_allowed_host_exceptions={
            "PORTAL-T2045": ["g.portal.com"],
        },
        environment_required_host_overrides={
            "PORTAL-T2787": ["portal.com"],
            "PORTAL-T2547": ["d.portal.com"],
            "PORTAL-T2548": ["d.portal.com"],
            "PORTAL-T2549": ["d.portal.com"],
            "PORTAL-T2550": ["d.portal.com"],
        },
    )


def _contextual_metadata(run: str, test_case: str) -> dict:
    return {"test_results": [{
        "testrun_name": run,
        "test_case_key": test_case,
    }]}


def _contextual_result(*hosts: str) -> dict:
    return {
        "preprod_urls": list(hosts),
        "prod_urls": [],
        "per_image": [{
            "image": "screenshots/page_001.jpg",
            "preprod_urls_top": list(hosts),
            "prod_urls_top": [],
        }],
        "findings": [{"rule": "R9", "severity": "high"}],
    }


class TestContextualEnvironmentPolicy:
    def test_configured_storefront_domains_are_extracted(self):
        env_check.configure(_contextual_policy())

        assert env_check.extract_urls("https://rc.portal.com/title") == [
            ("https://rc.portal.com", "preprod"),
        ]
        assert env_check.extract_urls("https://gf.aka.retail.example.com/offer") == [
            ("https://gf.aka.retail.example.com", "preprod"),
        ]

    def test_run_host_is_compliant(self):
        env_check.configure(_contextual_policy())
        result = _contextual_result("g.portal.com")

        findings = env_check.apply_contextual_environment_policy(
            _contextual_metadata("Website regression - weekly", "PORTAL-T1"),
            result,
        )

        assert findings == []
        assert result["verdict"] == "compliant"
        assert result["contextual_policy"]["observed_hosts"] == ["g.portal.com"]

    def test_wrong_regression_host_is_high(self):
        env_check.configure(_contextual_policy())
        result = _contextual_result("g.portal.com")

        findings = env_check.apply_contextual_environment_policy(
            _contextual_metadata("DB Regression - weekly", "PORTAL-T1"),
            result,
        )

        assert findings[0]["severity"] == "high"
        assert findings[0]["rule"] == "R9"
        assert "rc.portal.com" in findings[0]["description"]

    def test_tax_redirect_exception_is_accepted(self):
        env_check.configure(_contextual_policy())
        result = _contextual_result("g.portal.com")

        findings = env_check.apply_contextual_environment_policy(
            _contextual_metadata("DB Regression - weekly", "PORTAL-T2045"),
            result,
        )

        assert findings == []
        assert result["verdict"] == "compliant"

    def test_home_page_override_needs_evidence_of_production(self):
        env_check.configure(_contextual_policy())
        result = _contextual_result("g.portal.com")

        findings = env_check.apply_contextual_environment_policy(
            _contextual_metadata(
                "Website regression - weekly", "PORTAL-T2787"),
            result,
        )

        assert findings[0]["severity"] == "medium"
        assert findings[0]["rule"] == "R9-amb"
        assert "portal.com" in findings[0]["description"]

    @pytest.mark.parametrize(
        "run_name",
        ["DB Regression - weekly", "Website regression - weekly"],
    )
    @pytest.mark.parametrize(
        "test_case_key",
        ["PORTAL-T2547", "PORTAL-T2548", "PORTAL-T2549", "PORTAL-T2550"],
    )
    def test_earning_statement_overrides_require_development_storefront(
        self,
        run_name,
        test_case_key,
    ):
        env_check.configure(_contextual_policy())
        baseline_host = (
            "rc.portal.com"
            if run_name.startswith("DB")
            else "g.portal.com"
        )
        wrong_result = _contextual_result(baseline_host)

        findings = env_check.apply_contextual_environment_policy(
            _contextual_metadata(run_name, test_case_key),
            wrong_result,
        )

        assert findings[0]["severity"] == "medium"
        assert findings[0]["rule"] == "R9-amb"
        assert "d.portal.com" in findings[0]["description"]

        expected_result = _contextual_result("d.portal.com")
        findings = env_check.apply_contextual_environment_policy(
            _contextual_metadata(run_name, test_case_key),
            expected_result,
        )

        assert findings == []
        assert expected_result["verdict"] == "compliant"

    def test_uncertain_firefox_prefix_is_not_called_production(self):
        env_check.configure(_contextual_policy())
        result = _contextual_result()
        result["suspect_prod_urls_top"] = ["portal.com/producer/earnings"]
        result["per_image"] = [{
            "image": "page_001.jpg",
            "preprod_urls": [],
            "prod_urls": [],
            "prod_urls_top": [],
            "prod_urls_bottom": [],
            "suspect_prod_urls_top": ["portal.com/producer/earnings"],
        }]

        findings = env_check.apply_contextual_environment_policy(
            _contextual_metadata(
                "Website regression - Desktop Firefox",
                "PORTAL-T2547",
            ),
            result,
        )

        assert findings[0]["severity"] == "medium"
        assert findings[0]["rule"] == "R9-amb"
        assert "not evidence that the run used production" in (
            findings[0]["description"]
        )
        assert result["contextual_policy"]["suspect_required_hosts"] == [
            "portal.com",
        ]

    def test_unmatched_run_skips_environment_policy(self):
        env_check.configure(_contextual_policy())
        result = _contextual_result("g.portal.com")

        findings = env_check.apply_contextual_environment_policy(
            _contextual_metadata("Unrelated smoke run", "PORTAL-T1"),
            result,
        )

        assert findings == []
        assert result["verdict"] == "not_applicable"


class TestApprovedProductionPaths:
    @staticmethod
    def _policy():
        from argus.config.models import PolicySettings

        return PolicySettings(
            storefront_domains=["example", "sample"],
            expected_environment_markers=[
                "pre-prod", "preprod", "emergent",
            ],
            approved_production_paths=[
                "/cpe/yourpayments",
                "/gp/wallet",
                "/account/payments",
            ],
            marketplace_tlds={
                "US": ".com",
                "UK": ".co.uk",
                "DE": ".de",
                "AU": ".com.au",
                "JP": ".co.jp",
            },
        )

    @pytest.mark.parametrize(("url", "expected"), [
        ("https://www.retail.example.co.uk/cpe/yourpayments/wallet", "ignore"),
        ("https://www.retail.example.com/gp/wallet", "ignore"),
        ("https://www.example.co.uk/account/payments", "ignore"),
        ("https://www.example.de/en_GB/account/payments", "ignore"),
        ("https://www.retail.example.com/gp/wallet2", "prod"),
        ("https://www.example.de/en_GB/account/membership", "prod"),
        ("https://pre-prod.retail.example.com/setup/onboarding", "preprod"),
    ])
    def test_paths_are_narrow_and_locale_safe(self, url, expected):
        env_check.configure(self._policy())
        assert [
            classification
            for _, classification in env_check.extract_urls(url)
        ] == [expected]

    def test_unsafe_root_path_is_rejected(self, tmp_path):
        from argus import config as argus_config

        config_path = tmp_path / "unsafe.toml"
        config_path.write_text(
            '[policy]\napproved_production_paths = ["/"]\n')
        with pytest.raises(
            argus_config.ConfigError,
            match="would approve every production URL",
        ):
            argus_config.load(config_path)


class TestUrlContext:
    def test_protocol_prefix_is_context(self):
        text = "https://example.com/foo"
        assert env_check._has_url_context(text, text.index("example"), text.index("/foo"))

    def test_path_suffix_is_context(self):
        text = "example.com/path"
        assert env_check._has_url_context(text, 0, text.index("/"))

    def test_slashes_inside_match_is_context(self):
        text = "//example.com"
        assert env_check._has_url_context(text, 0, len(text))

    def test_bare_hostname_no_context(self):
        text = "example.com"
        assert not env_check._has_url_context(text, 0, len(text))

    def test_prose_context_not_url(self):
        text = "try example.com today"
        start = text.index("example")
        end = start + len("example.com")
        assert not env_check._has_url_context(text, start, end)


def _make_execution_dir(tmp_path: Path, images: dict[str, str]) -> Path:
    """Create a fake execution dir with empty image files matching keys."""
    exec_dir = tmp_path / "exec"
    shots = exec_dir / "screenshots"
    shots.mkdir(parents=True)
    for rel in images:
        p = shots / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"")
    return exec_dir


class TestBuildFinding:
    def test_violation_finding_shape(self):
        f = env_check._build_finding(
            verdict="violation",
            prod_urls={"www.example.com"},
            preprod_urls=set(),
            images_with_prod=3,
            images_with_preprod=0,
        )
        assert f["severity"] == "high"
        assert f["source"] == "env_check"
        assert f["rule"] == "R9"
        assert "www.example.com" in f["description"]
        assert "No feature-preprod" in f["description"]

    def test_mixed_finding_mentions_both(self):
        f = env_check._build_finding(
            verdict="mixed",
            prod_urls={"example.de"},
            preprod_urls={"feature-preprod.example.de"},
            images_with_prod=2,
            images_with_preprod=5,
        )
        assert "mixed" in f["description"].lower()
        assert "5" in f["description"]




class TestRunAndMerge:
    @staticmethod
    def _fake_ec(findings=None):
        return {
            "version": 99,
            "verdict": "preprod",
            "preprod_urls": ["feature-preprod.example.com/x"],
            "prod_urls": [],
            "suspect_prod_urls_top": [],
            "images_with_preprod": 2,
            "images_with_prod": 0,
            "images_scanned": 4,
            "images_total": 20,
            "sample_stride": 5,
            "region_crop_height": 180,
            "region_crop_bottom_height": 80,
            "per_image": [],
            "findings": findings or [],
        }

    def _run(self, tmp_path, monkeypatch, audit, *, findings=None,
             stride_seen=None, metadata=None, kwargs_seen=None):
        from argus.pipeline import env_check_model
        from argus.pipeline import env_check_merge

        def fake_engine(execution_dir, *, sample_stride, **kwargs):
            if stride_seen is not None:
                stride_seen.append(sample_stride)
            if kwargs_seen is not None:
                kwargs_seen.update(kwargs)
            return self._fake_ec(findings)

        monkeypatch.setattr(env_check_model, "check_env_compliance",
                            fake_engine)
        return env_check_merge.run_and_merge(
            audit, tmp_path,
            sample_stride=5, region_crop_height=180,
            region_crop_bottom_height=80, model_api_url="https://models.example.com", api_key_env=None,
            metadata=metadata or {},
        )

    def test_env_check_block_has_full_shape(self, tmp_path, monkeypatch):
        out = self._run(tmp_path, monkeypatch,
                        {"overall_verdict": "pass", "summary": "s",
                         "findings": []})
        block = out.audit["env_check"]
        assert block["images_total"] == 20
        assert block["sample_stride"] == 5
        assert block["region_crop_height"] == 180
        assert block["region_crop_bottom_height"] == 80
        assert block["version"] == 99
        assert block["prod_urls_top"] == []
        assert block["prod_urls_bottom"] == []
        assert block["suspect_prod_urls_top"] == []

    def test_expected_browser_is_passed_to_env_check(
            self, tmp_path, monkeypatch):
        kwargs_seen = {}
        self._run(
            tmp_path,
            monkeypatch,
            {"overall_verdict": "pass", "summary": "s", "findings": []},
            metadata={
                "test_results": [{
                    "testrun_name": "E2E DE Desktop Firefox",
                    "expected_browser": "Firefox",
                    "marketplace": "DE",
                }],
            },
            kwargs_seen=kwargs_seen,
        )
        assert kwargs_seen["expected_browser"] == "Firefox"
        assert kwargs_seen["expected_marketplace"] == "DE"
        assert (
            kwargs_seen["expected_execution_mode"]
            == "desktop_browser"
        )

    @pytest.mark.parametrize("metadata", [
        {"test_results": [None]},
        {"test_results": ["not-a-result"]},
        ["not-an-object"],
    ])
    def test_malformed_test_result_metadata_is_ignored(
            self, tmp_path, monkeypatch, metadata):
        kwargs_seen = {}
        self._run(
            tmp_path,
            monkeypatch,
            {"overall_verdict": "pass", "summary": "s", "findings": []},
            metadata=metadata,
            kwargs_seen=kwargs_seen,
        )
        assert kwargs_seen["expected_browser"] is None

    def test_merge_persists_structured_device_check(
            self, tmp_path, monkeypatch):
        from argus.pipeline import device_check

        monkeypatch.setattr(
            device_check,
            "evaluate_device_compliance",
            lambda *args, **kwargs: {
                "version": 4,
                "expected_mode": "mobile_emulation_allowed",
                "expected_family": "android",
                "status": "compliant",
                "reason": "matching emulation",
                "observed_mode": "devtools_emulation",
                "observed_family": "android",
                "confidence": "high",
                "images_checked": 2,
                "signals": [
                    "responsive_device_toolbar",
                    "emulator_profile",
                ],
                "observations": [],
                "findings": [],
            },
        )
        out = self._run(
            tmp_path,
            monkeypatch,
            {"overall_verdict": "pass", "summary": "s", "findings": []},
        )
        assert out.audit["device_check"]["version"] == 4
        assert out.audit["device_check"]["status"] == "compliant"
        assert "responsive_device_toolbar" in out.audit[
            "device_check"]["signals"]
        assert "findings" not in out.audit["device_check"]

    def test_device_check_runs_without_environment_analysis(
            self, tmp_path, monkeypatch):
        from argus.pipeline import device_check
        from argus.pipeline import env_check_model
        from argus.pipeline import env_check_merge

        monkeypatch.setattr(
            env_check_model,
            "check_env_compliance",
            lambda *args, **kwargs: pytest.fail(
                "environment-disabled profile must not invoke Haiku URL checks"
            ),
        )
        monkeypatch.setattr(
            device_check,
            "evaluate_device_compliance",
            lambda *args, **kwargs: {
                "version": device_check.DEVICE_CHECK_VERSION,
                "status": "compliant",
                "findings": [],
            },
        )
        outcome = env_check_merge.run_and_merge(
            {
                "overall_verdict": "concerns",
                "summary": "old environment result",
                "findings": [{
                    "severity": "high",
                    "source": "env_check",
                    "rule": "R9",
                    "description": "stale production URL finding",
                }],
                "env_check": {"version": 1},
            },
            tmp_path,
            sample_stride=5,
            region_crop_height=180,
            region_crop_bottom_height=80,
            model_api_url="https://models.example.com",
            api_key_env=None,
            metadata={},
            environment_enabled=False,
            marketplace_enabled=False,
            device_enabled=True,
        )

        assert "env_check" not in outcome.audit
        assert outcome.audit["device_check"]["status"] == "compliant"
        assert outcome.audit["findings"] == []
        assert outcome.audit["overall_verdict"] == "pass"

    def test_deferred_merge_adds_haiku_usage_to_existing_cost(
            self, tmp_path, monkeypatch):
        from argus.core import costs
        from argus.pipeline import env_check_model
        from argus.pipeline import env_check_merge

        rates = {
            "claude-sonnet-5": {"input_per_million": 3.0, "output_per_million": 6.0, "cache_write_per_million": 0.0, "cache_read_per_million": 0.0},
            "claude-haiku-4-5": {"input_per_million": 0.2, "output_per_million": 1.0, "cache_write_per_million": 0.0, "cache_read_per_million": 0.0},
        }
        main_tracker = costs.UsageTracker()
        main_tracker.record_response(
            {"usage": {"input_tokens": 1_000, "output_tokens": 100}},
            model_id="claude-sonnet-5",
            operation="audit",
        )
        audit = {
            "overall_verdict": "pass",
            "summary": "s",
            "findings": [],
            "cost": main_tracker.summary(rates),
        }

        def fake_engine(*args, usage_tracker=None, **kwargs):
            usage_tracker.record_response(
                {"usage": {"input_tokens": 500, "output_tokens": 50}},
                model_id="claude-haiku-4-5",
                operation="env_check",
            )
            return self._fake_ec()

        monkeypatch.setattr(
            env_check_model, "check_env_compliance", fake_engine)
        outcome = env_check_merge.run_and_merge(
            audit, tmp_path,
            sample_stride=5,
            region_crop_height=180,
            region_crop_bottom_height=80,
            model_api_url="https://models.example.com",
            api_key_env=None,
            metadata={},
            model_rates=rates,
        )

        assert outcome.audit["cost"]["calls"] == 2
        assert outcome.audit["cost"]["estimated_cost_usd"] == pytest.approx(
            0.00375)

    def test_stale_env_findings_are_replaced_not_accumulated(
            self, tmp_path, monkeypatch):
        audit = {
            "overall_verdict": "concerns", "summary": "s",
            "findings": [
                {"severity": "high", "description": "old R9",
                 "source": "env_check", "rule": "R9"},
                {"severity": "medium", "description": "unrelated",
                 "source": "rule", "rule": "R6"},
            ],
        }
        new_r9 = {"severity": "high", "description": "new R9",
                  "source": "env_check", "rule": "R9"}
        out = self._run(tmp_path, monkeypatch, audit, findings=[new_r9])
        descs = [f["description"] for f in out.audit["findings"]]
        assert "old R9" not in descs
        assert "new R9" in descs
        assert "unrelated" in descs
        assert out.had_stale_finding is True
        assert out.added_finding is True

    def test_webview_path_tightens_stride(self, tmp_path, monkeypatch):
        stride_seen: list[int] = []
        webview_dir = tmp_path / "Webview_E2E_Something" / "QA-E1"
        webview_dir.mkdir(parents=True)
        from argus.pipeline import env_check_model
        from argus.pipeline import env_check_merge

        def fake_engine(execution_dir, *, sample_stride, **kwargs):
            stride_seen.append(sample_stride)
            return self._fake_ec()

        monkeypatch.setattr(env_check_model, "check_env_compliance",
                            fake_engine)
        env_check_merge.run_and_merge(
            {"overall_verdict": "pass", "summary": "s", "findings": []},
            webview_dir,
            sample_stride=5, region_crop_height=180,
            region_crop_bottom_height=80, model_api_url="https://models.example.com", api_key_env=None,
            metadata={},
        )
        assert stride_seen == [4]

    def test_schema_version_bumped_never_downgraded(self, tmp_path,
                                                    monkeypatch):
        from argus.pipeline import auditor
        out = self._run(tmp_path, monkeypatch,
                        {"overall_verdict": "pass", "summary": "s",
                         "findings": [], "schema_version": 1})
        assert out.audit["schema_version"] == auditor.AUDIT_SCHEMA_VERSION
        out2 = self._run(tmp_path, monkeypatch,
                         {"overall_verdict": "pass", "summary": "s",
                          "findings": [],
                          "schema_version": auditor.AUDIT_SCHEMA_VERSION + 5})
        assert (out2.audit["schema_version"]
                == auditor.AUDIT_SCHEMA_VERSION + 5)

    def test_stripped_escalation_rolls_verdict_back(self, tmp_path,
                                                    monkeypatch):
        audit = {"overall_verdict": "pass", "summary": "clean run.",
                 "findings": []}
        r9 = {"severity": "high", "description": "prod URL detected",
              "source": "env_check", "rule": "R9"}
        out1 = self._run(tmp_path, monkeypatch, audit, findings=[r9])
        a1 = out1.audit
        assert a1["overall_verdict"] == "fail"
        assert a1["_verdict_escalated_by_rules_from"] == "pass"
        assert "workflow consistency issue" in a1["summary"]
        out2 = self._run(tmp_path, monkeypatch, a1)
        a2 = out2.audit
        assert a2["overall_verdict"] == "pass"
        assert all(f.get("advisory") for f in a2["findings"])
        assert "workflow consistency issue" not in a2["summary"]
        assert "_verdict_escalated_by_rules_from" not in a2

    def test_strip_keeps_surviving_rule_escalation(self, tmp_path,
                                                   monkeypatch):
        from argus.pipeline import auditor
        base = {"overall_verdict": "pass", "summary": "clean run.",
                "findings": []}
        r17 = {"severity": "medium", "description": "prior was Pass",
               "source": "rule", "rule": "R17"}
        audit = auditor.merge_rule_findings(base, [r17])
        assert audit["overall_verdict"] == "concerns"
        r9 = {"severity": "high", "description": "prod URL detected",
              "source": "env_check", "rule": "R9"}
        out1 = self._run(tmp_path, monkeypatch, audit, findings=[r9])
        assert out1.audit["overall_verdict"] == "fail"
        out2 = self._run(tmp_path, monkeypatch, out1.audit)
        a2 = out2.audit
        assert a2["overall_verdict"] == "concerns"
        assert [
            f["rule"] for f in a2["findings"] if not f.get("advisory")
        ] == ["R17"]
        assert "1 workflow consistency issue " in a2["summary"]
        assert a2["summary"].count("Additionally,") == 1

    def test_parse_stub_without_runtime_finding_reconciles_after_clean_rerun(
            self, tmp_path, monkeypatch):
        stub = {"overall_verdict": "fail",
                "summary": "model response could not be parsed",
                "findings": []}
        r9 = {"severity": "high", "description": "prod URL detected",
              "source": "env_check", "rule": "R9"}
        out1 = self._run(tmp_path, monkeypatch, stub, findings=[r9])
        assert out1.audit["overall_verdict"] == "fail"
        assert "_verdict_escalated_by_rules_from" not in out1.audit
        out2 = self._run(tmp_path, monkeypatch, out1.audit)
        assert out2.audit["overall_verdict"] == "pass"
        assert all(f.get("advisory") for f in out2.audit["findings"])


def test_run_and_merge_finalizes_verdict_after_enrich(tmp_path, monkeypatch):
    from argus.pipeline import env_check_model
    from argus.pipeline import env_check_merge

    monkeypatch.setattr(
        env_check_model, "check_env_compliance",
        lambda execution_dir, *, sample_stride, **kw: {
            "version": 99, "verdict": "preprod",
            "preprod_urls": ["feature-preprod.example.com/x"],
            "prod_urls": [], "images_with_preprod": 1,
            "images_with_prod": 0, "images_scanned": 1, "images_total": 1,
            "sample_stride": 1, "region_crop_height": 0,
            "region_crop_bottom_height": 0, "per_image": [], "findings": [],
        })
    audit = {
        "overall_verdict": "concerns",
        "summary": "s",
        "findings": [{
            "severity": "medium", "page": 4,
            "description": "the evidence directly contradicts the "
                           "expected cash payment",
        }],
    }
    outcome = env_check_merge.run_and_merge(
        audit, tmp_path,
        sample_stride=1, region_crop_height=0,
        region_crop_bottom_height=0, model_api_url="https://models.example.com", api_key_env=None,
        metadata={},
    )
    assert outcome.audit["findings"][0]["severity"] == "high"
    assert outcome.audit["overall_verdict"] == "fail"
    assert outcome.audit["_verdict_escalated_from"] == "concerns"
