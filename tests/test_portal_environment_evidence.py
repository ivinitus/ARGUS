"""PORTAL audit-review regressions: approved hosts and screenshot-grounded R9."""

import pytest
from PIL import Image

from argus.config.models import PolicySettings
from argus.pipeline import env_check, env_check_model as ech
from argus.reporting import html_report, report


@pytest.fixture(autouse=True)
def portal_policy():
    policy = PolicySettings(
        storefront_domains=["example"],
        expected_environment_markers=["rc", "g", "dev"],
        environment_expected_hosts_by_run={
            "db regression": ["rc.portal.example.com"],
            "website regression": ["g.portal.example.com"],
        },
        environment_allowed_host_exceptions={"PORTAL-T10": ["g.portal.example.com"]},
        environment_required_host_overrides={
            "PORTAL-T20": ["portal.example.com"],
            **{f"PORTAL-T{n}": ["d.portal.example.com"] for n in range(30, 34)},
        },
    )
    env_check.configure(policy)
    yield
    env_check.reset_configuration()


def _apply(result, case="PORTAL-T1", run="DB Regression - weekly"):
    return env_check.apply_contextual_environment_policy(
        {"test_results": [{"test_case_key": case, "testrun_name": run}]},
        result,
    )


def _result(*hosts, bottom=(), suspect=()):
    return {
        "preprod_urls": [],
        "prod_urls": [*hosts, *bottom, *suspect],
        "suspect_prod_urls_top": list(suspect),
        "per_image": [{
            "image": "screenshots/page_001.jpg",
            "preprod_urls_top": [],
            "prod_urls_top": list(hosts),
            "prod_urls_bottom": list(bottom),
            "suspect_prod_urls_top": list(suspect),
        }],
    }


def _render_detail(result, tmp_path):
    findings = result.get("findings") or []
    high = any(f["severity"] == "high" for f in findings)
    summary = report.AuditSummary(
        key="PORTAL-E1", path=tmp_path,
        verdict="fail" if high else "concerns" if findings else "pass",
        findings_count=len(findings),
        severity_counts={
            level: sum(f["severity"] == level for f in findings)
            for level in ("high", "medium", "low")
        },
        rules_fired=[f["rule"] for f in findings], has_high_finding=high,
        schema_version=5, tester=None, tester_status="Pass",
        test_case="PORTAL-T1", findings=findings, env_check=result,
    )
    return html_report._argus_render_detail_panel(summary, tmp_path, [], {}, [])


@pytest.mark.parametrize("run", ["DB Regression", "Website Regression"])
@pytest.mark.parametrize("case,host", [
    ("PORTAL-T10", "g.portal.example.com"),
    ("PORTAL-T20", "portal.example.com"),
    ("PORTAL-T30", "d.portal.example.com"),
    ("PORTAL-T31", "d.portal.example.com"),
    ("PORTAL-T32", "d.portal.example.com"),
    ("PORTAL-T33", "d.portal.example.com"),
])
def test_reviewed_host_exceptions_are_compliant(run, case, host, tmp_path):
    result = _result(host)
    assert _apply(result, case, run) == []
    assert result["verdict"] == "compliant"
    assert "argus-env-violation" not in _render_detail(result, tmp_path)


def test_hover_cannot_override_approved_active_host(tmp_path):
    result = _result("rc.portal.example.com", bottom=["portal.example.com", "g.portal.example.com"])
    assert _apply(result) == []
    assert result["contextual_policy"]["observed_hosts"] == ["rc.portal.example.com"]
    assert "argus-env-violation" not in _render_detail(result, tmp_path)


@pytest.mark.parametrize("case", ["PORTAL-T1", "PORTAL-T20", "PORTAL-T30"])
def test_aggregate_urls_without_screenshot_proof_cannot_be_deviations(case, tmp_path):
    result = {"prod_urls": ["portal.example.com"], "per_image": []}
    findings = _apply(result, case)
    assert all(f["severity"] != "high" for f in findings)
    assert result["verdict"] == "ambiguous"
    panel = _render_detail(result, tmp_path)
    assert "argus-env-violation" not in panel
    assert "argus-finding-medium" in panel


def test_hover_cannot_satisfy_required_homepage_proof():
    result = _result("rc.portal.example.com", bottom=["portal.example.com"])
    findings = _apply(result, "PORTAL-T20")
    assert findings[0]["rule"] == "R9-amb"
    assert findings[0]["severity"] == "medium"


def test_session_suspect_is_not_reintroduced_as_a_confirmed_host():
    result = _result("rc.portal.example.com", "portal.example.com", suspect=["portal.example.com"])
    assert _apply(result) == []
    assert result["contextual_policy"]["observed_hosts"] == ["rc.portal.example.com"]


@pytest.mark.parametrize("hover", [[], ["example.de"]])
def test_session_downgrade_updates_all_production_fields(
    monkeypatch, tmp_path, hover,
):
    env_check.reset_configuration()
    shots = tmp_path / "screenshots"
    shots.mkdir()
    records = {}
    for name, preprod, prod, bottom in [
        ("page_001.jpg", ["feature-preprod.example.de"], [], []),
        ("page_002.jpg", [], ["example.de"], hover),
    ]:
        path = shots / name
        Image.new("RGB", (50, 50)).save(path)
        records[name] = {
            "image": str(path), "urls": preprod + prod + bottom,
            "preprod_urls": preprod, "preprod_urls_top": preprod,
            "prod_urls": prod + bottom, "prod_urls_top": prod,
            "prod_urls_bottom": bottom,
        }
    monkeypatch.setattr(
        ech, "_process_image", lambda path, *a, **k: records[path.name],
    )
    result = ech.check_env_compliance(tmp_path)
    suspect_image = next(
        row for row in result["per_image"] if row["image"].endswith("page_002.jpg")
    )
    assert result["prod_urls_top"] == suspect_image["prod_urls_top"] == []
    assert result["prod_urls"] == suspect_image["prod_urls"] == hover
    assert result["prod_urls_bottom"] == hover
    assert result["images_with_prod"] == int(bool(hover))
    assert result["suspect_prod_urls_top"] == ["example.de"]
    assert suspect_image["suspect_prod_urls_top"] == ["example.de"]
    if not hover:
        assert "argus-env-violation" not in _render_detail(result, tmp_path)


def test_unlisted_storefront_host_is_still_a_deviation_with_source(tmp_path):
    result = _result("www.portal.example.com/account")
    findings = _apply(result)
    assert findings[0]["rule"] == "R9"
    assert findings[0]["severity"] == "high"
    assert "www.portal.example.com" in findings[0]["description"]
    assert "screenshots/page_001.jpg" in findings[0]["description"]
    panel = _render_detail(result, tmp_path)
    assert "argus-env-violation" in panel
    assert "<code>www.portal.example.com</code>" in panel


def test_report_warning_identifies_only_the_unapproved_host(tmp_path):
    result = _result("portal.example.com")
    result["per_image"][0]["preprod_urls_top"] = ["g.portal.example.com"]
    assert _apply(result, "PORTAL-T20")[0]["severity"] == "high"
    panel = _render_detail(result, tmp_path)
    assert "Environment mismatch:" in panel
    assert "<code>g.portal.example.com</code>" in panel
    assert "<code>portal.example.com</code>" not in panel


def test_out_of_scope_context_does_not_show_a_production_warning(tmp_path):
    result = _result("portal.example.com")
    assert _apply(result, run="Unrelated run") == []
    assert "argus-env-violation" not in _render_detail(result, tmp_path)


def test_report_keeps_generic_production_warning_without_context_policy(tmp_path):
    panel = _render_detail({"prod_urls": ["www.example.com"]}, tmp_path)
    assert "Production URLs detected:" in panel
    assert "<code>www.example.com</code>" in panel


def test_unrelated_domain_is_not_an_portal_environment():
    result = _result("rc.portal.example.com", "portal.example.com.example.org")
    assert _apply(result) == []


@pytest.mark.parametrize("source", ["page_body_markers", "webview_context"])
def test_non_address_bar_sources_cannot_be_environment_deviations(source):
    result = _result("portal.example.com")
    result["per_image"][0]["source"] = source
    findings = _apply(result)
    assert findings[0]["rule"] == "R9-amb"


def _scan(monkeypatch, tmp_path, initial, confirmation, *, browser="Chrome"):
    shots = tmp_path / "screenshots"
    shots.mkdir()
    path = shots / "page_001.jpg"
    Image.new("RGB", (500, 400), "white").save(path)
    reads = iter([initial, []])
    monkeypatch.setattr(ech, "_extract_urls_haiku", lambda *a, **k: next(reads))
    monkeypatch.setattr(ech, "_confirm_hostname", lambda *a, **k: confirmation)
    return ech.check_env_compliance(
        tmp_path, region_crop_height=180, region_crop_bottom_height=80,
        expected_browser=browser,
        expected_execution_mode=ech.execution_policy.DESKTOP_BROWSER,
    )


@pytest.mark.parametrize("confirmation", [
    None, ("portal.example.com", False), ("www.retail.example.com", True),
])
def test_unconfirmed_chrome_portal_url_cannot_be_a_deviation(
    monkeypatch, tmp_path, confirmation,
):
    result = _scan(monkeypatch, tmp_path, ["portal.example.com/account"], confirmation)
    findings = _apply(result)
    assert all(f["severity"] != "high" for f in findings)
    assert result["prod_urls_top"] == []
    assert result["suspect_prod_urls_top"] == ["portal.example.com/account"]


def test_confirmed_portal_navigation_remains_a_deviation(monkeypatch, tmp_path):
    result = _scan(
        monkeypatch, tmp_path, ["portal.example.com/account"], ("portal.example.com", True),
    )
    findings = _apply(result)
    assert findings[0]["severity"] == "high"
    assert "screenshots/page_001.jpg" in findings[0]["description"]


@pytest.mark.parametrize("host,case", [
    ("rc.portal.example.com", "PORTAL-T1"),
    ("g.portal.example.com", "PORTAL-T10"),
    ("d.portal.example.com", "PORTAL-T30"),
])
def test_recovered_prefix_proves_approved_environment(
    monkeypatch, tmp_path, host, case,
):
    result = _scan(
        monkeypatch, tmp_path, ["portal.example.com/account"], (host, True),
    )
    assert _apply(result, case) == []
    assert result["contextual_policy"]["observed_hosts"] == [host]


def test_firefox_can_verify_approved_homepage(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ech, "_confirm_firefox_hostname",
        lambda *a, **k: ("portal.example.com", True, "", "high"),
    )
    result = _scan(
        monkeypatch, tmp_path, ["portal.example.com"], None, browser="Firefox",
    )
    assert _apply(result, "PORTAL-T20") == []


@pytest.mark.parametrize("browser", ["Chrome", "Firefox"])
def test_confirmed_homepage_is_not_downgraded_by_gamma_steps(
    monkeypatch, tmp_path, browser,
):
    shots = tmp_path / "screenshots"
    shots.mkdir()
    hosts = {500: "g.portal.example.com", 510: "portal.example.com"}
    for width in hosts:
        Image.new("RGB", (width, 400)).save(shots / f"page_{width}.jpg")
    monkeypatch.setattr(
        ech, "_extract_urls_haiku",
        lambda image, *a, **k: [hosts[image.width]] if image.height == 180 else [],
    )
    monkeypatch.setattr(
        ech, "_confirm_hostname", lambda *a, **k: ("portal.example.com", True),
    )
    monkeypatch.setattr(
        ech, "_confirm_firefox_hostname",
        lambda *a, **k: ("portal.example.com", True, "", "high"),
    )
    result = ech.check_env_compliance(
        tmp_path, region_crop_height=180, region_crop_bottom_height=80,
        expected_browser=browser,
        expected_execution_mode=ech.execution_policy.DESKTOP_BROWSER,
    )
    assert result["prod_urls_top"] == ["portal.example.com"]
    assert result["suspect_prod_urls_top"] == []
    assert _apply(result, "PORTAL-T20", "Website Regression") == []
    assert "argus-env-violation" not in _render_detail(result, tmp_path)


@pytest.mark.parametrize("host,prefix", [
    ("www.retail.example.com", "www"), ("g.retail.example.com", "g"),
])
def test_firefox_different_domain_cannot_confirm_portal_navigation(
    monkeypatch, tmp_path, host, prefix,
):
    monkeypatch.setattr(
        ech, "_confirm_firefox_hostname",
        lambda *a, **k: (host, True, prefix, "high"),
    )
    result = _scan(
        monkeypatch, tmp_path, ["portal.example.com"], None, browser="Firefox",
    )
    assert result["prod_urls_top"] == []
    assert all(f["severity"] != "high" for f in _apply(result))


def test_failed_confirmation_is_not_production_evidence(monkeypatch):
    from argus.pipeline.analysis_status import AnalysisIncompleteError

    def unavailable(*args, **kwargs):
        raise AnalysisIncompleteError("environment", "confirmation unavailable")

    monkeypatch.setattr(ech, "_confirm_hostname", unavailable)
    confirmed, recovered, suspect = ech._confirm_contextual_prods(
        ["portal.example.com/account"], Image.new("RGB", (100, 100)), "m", "r", None,
    )
    assert confirmed == recovered == []
    assert suspect == ["portal.example.com/account"]


def test_no_required_host_in_sample_is_an_evidence_gap():
    result = _result("rc.portal.example.com")
    result.update(images_scanned=3, images_total=40)
    findings = _apply(result, "PORTAL-T30")
    assert findings[0]["severity"] == "medium"
    assert findings[0]["rule"] == "R9-amb"
    assert findings[0]["category"] == "insufficient_evidence"
