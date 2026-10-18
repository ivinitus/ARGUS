"""HS evidence exceptions preserve confirmed defects and other teams' policy."""
from copy import deepcopy
import io
import json
from unittest.mock import Mock

import fitz
from PIL import Image
import pytest

from argus import config
from argus.domain import desktop_policy as hs, workflow_rules
from argus.pipeline import auditor, batch, env_check, env_check_model as ech
from argus.pipeline import extractor, linked_evidence

DRIVE = "https://drive.google.com/file/d/evidence-id/view"
ISSUE_URL = "https://issues.example.com/browse/BUG-1"


@pytest.fixture
def policy():
    settings = config.load(profile="desktop")
    settings.policy.storefront_domains = ["example", "shop"]
    settings.policy.approved_production_host_patterns = ["example.*", "*.example.*"]
    workflow_rules.configure(settings.policy)
    env_check.configure(settings.policy)
    yield settings
    workflow_rules.reset_configuration()
    env_check.reset_configuration()


def metadata():
    return {"test_results": [{
        "testrun_name": "HS US iOS shopping app",
        "expected_device": "iPhone", "marketplace": "US",
        "status": "Pass", "comment": "Account qa@example.com",
        "steps": [{"index": 0, "status": "Pass", "description": "Complete purchase",
                   "expected_result": "Purchase completes"}],
    }]}


def audit(findings=()):
    return {"variant_name": "desktop_v2", "overall_verdict": "pass",
            "summary": "Evidence checked.", "findings": list(findings)}


def gap(step=0):
    return {"severity": "medium", "source": "model", "category": "insufficient_evidence",
            "action": "add_evidence", "description": "No purchase history screenshot.",
            "page": None, "step_index": step}


def picture(tmp_path):
    path = tmp_path / "screenshots" / "session" / "page_001.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (100, 100)).save(path)
    return path


def process(path, monkeypatch, urls, marketplace="US", label=None):
    monkeypatch.setattr(ech, "_extract_urls_haiku", lambda *a, **k: urls)
    monkeypatch.setattr(ech, "_classify_navigation_notice", lambda *a, **k: label)
    return ech._process_image(path, "model", "region", None, 0, 0,
                              expected_marketplace=marketplace)


def test_hs_profile_scans_every_attachment_and_fetches_passing_case_links(policy):
    assert policy.auditor.env_check_sample_stride == 1
    assert policy.extractor.include_test_case_weblinks
    assert extractor.ExtractorConfig.from_settings(
        policy.extractor
    ).test_case_weblinks_all_statuses
    for name in ("web", "tablet", "mobile", "reader", "tv", "portal"):
        other = config.load(profile=name)
        assert not other.policy.environment_reference_evidence
        assert not other.policy.approved_production_host_patterns
        assert not other.extractor.test_case_weblinks_all_statuses


@pytest.mark.parametrize("device,removed", [("iPhone", True), ("iOS", True), ("Android", False)])
def test_missing_inspection_exception_is_only_ios_shopping_app(device, removed):
    meta = metadata()
    meta["test_results"][0]["expected_device"] = device
    finding = {**gap(), "description": "chrome://inspect/#devices capture is missing."}
    result = auditor._drop_false_positive_model_findings(audit([finding]), meta)
    assert bool(result["findings"]) is not removed
    block = auditor.build_steps_block(meta, variant_name="desktop_v2")
    assert ("Chrome inspection is not required" in block) is removed


def test_ios_exception_does_not_remove_combined_purchase_contradiction():
    finding = {**gap(), "description":
               "chrome://inspect is missing. The purchase outcome contradicts the expected result."}
    assert auditor._drop_false_positive_model_findings(audit([finding]), metadata())["findings"]


def test_hs_prompt_separates_platforms_and_accepts_production_and_weblinks(policy, tmp_path):
    prompt = auditor.AuditConfig.from_settings(
        policy.auditor, execution_dir=tmp_path,
    ).system_prompt
    assert "Only Android shopping app" in prompt
    assert "iOS shopping app cannot use Chrome inspection" in prompt
    assert "media storefront sites may be verified on production for Desktop" in prompt
    assert "ticket in Weblinks is valid documentation" in prompt


@pytest.mark.parametrize("field", ["weblinks", "test_case_weblinks"])
def test_issue_web_link_documents_failed_step_and_reaches_model(policy, field):
    meta = metadata()
    tr = meta["test_results"][0]
    tr.update(status="Fail", comment="", **{field: [ISSUE_URL]})
    tr["steps"][0]["status"] = "Fail"
    assert workflow_rules.check_fail_step_requires_defect_ref(meta) == []
    assert workflow_rules.check_fail_or_blocked_has_documentation(meta) == []
    assert workflow_rules.check_defect_refs_in_comment_text(meta) == []
    assert ISSUE_URL in auditor.build_steps_block(meta, variant_name="desktop_v2")
    acknowledged = {**gap(), "description": "The tester already reported this purchase issue."}
    assert auditor._drop_false_positive_model_findings(audit([acknowledged]), meta)["findings"] == []


def test_step_weblink_does_not_document_another_steps_defect(policy):
    meta = metadata()
    tr = meta["test_results"][0]
    tr["steps"] = [
        {"index": 0, "status": "Fail", "weblinks": [ISSUE_URL]},
        {"index": 1, "status": "Fail"},
    ]
    assert [f["step_index"] for f in workflow_rules.check_fail_step_requires_defect_ref(meta)] == [1]
    assert ISSUE_URL in auditor.build_steps_block(meta, variant_name="desktop_v2")


def test_passing_test_case_weblink_is_fetched_and_normalized(policy):
    raw = {"id": 1, "testResultStatusId": 39, "testCase": {"id": 20},
           "testScriptResults": []}
    response = Mock(status_code=200)
    response.json.return_value = [{"url": DRIVE}]
    session = Mock()
    session.get.return_value = response
    extractor.enrich_test_case_weblinks(session, "https://jira.example", [raw], {39, 40})
    normalized = extractor.build_metadata([raw], status_map={39: "Pass", 40: "Fail"})
    assert hs.external_evidence(normalized)[0]["url"] == DRIVE


@pytest.mark.parametrize("url", [
    "https://www.example.com/library", "https://www.example.co.uk/pd/title",
])
def test_hs_storefront_production_keeps_marketplace_evidence(policy, tmp_path, monkeypatch, url):
    record = process(picture(tmp_path), monkeypatch, [url],
                     marketplace="UK" if ".co.uk" in url else "US")
    assert record["prod_urls_top"] == []
    assert record["marketplace_urls"] == [url]
    assert record["navigation_notices"][0]["label"] == "approved production storefront"


def test_allowed_production_does_not_hide_wrong_marketplace(policy, tmp_path, monkeypatch):
    url = "https://www.example.co.uk/library"
    monkeypatch.setattr(ech, "_confirm_hostname", lambda *a, **k: (url, True))
    record = process(picture(tmp_path), monkeypatch, [url], marketplace="US")
    assert record["prod_urls_top"] == []
    findings = workflow_rules.check_marketplace_match(metadata(), {"env_check": record})
    assert [f["rule"] for f in findings] == ["R13"]


@pytest.mark.parametrize("label", ["privacy", "conditions of use", "condition of use"])
def test_amazon_legal_redirect_is_exempt(policy, tmp_path, monkeypatch, label):
    record = process(picture(tmp_path), monkeypatch,
                     ["https://www.shop.com/gp/help/customer/display.html"], label=label)
    assert not record["prod_urls_top"]
    assert not record["marketplace_urls"]


def test_retail_shopping_and_other_teams_keep_environment_checks(policy, tmp_path, monkeypatch):
    path = picture(tmp_path)
    url = "https://www.shop.com/cart"
    assert process(path, monkeypatch, [url])["prod_urls_top"] == [url]
    env_check.configure(config.load(profile="web").policy)
    url = "https://www.example.com/library"
    assert process(path, monkeypatch, [url])["prod_urls_top"] == [url]


@pytest.mark.parametrize("foreign_active", [False, True])
def test_full_attachment_reference_is_used_even_with_other_preprod_url(
    policy, tmp_path, monkeypatch, foreign_active,
):
    paths = []
    for index in range(12):
        path = tmp_path / "screenshots" / "session" / f"page_{index:03}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (100, 100)).save(path)
        paths.append(path)
    reference = paths[7]  # Outside the old ends + every-fifth-image sample.
    url = "https://preprod.shop.com/cart"
    foreign = "https://www.shop.co.uk/cart"

    def fake_image(path, *a, **k):
        if foreign_active and path == paths[1]:
            return {
                "image": str(path), "urls": [foreign], "preprod_urls": [],
                "prod_urls": [foreign], "prod_urls_top": [foreign], "marketplace_urls": [foreign],
            }
        return {"image": str(path), "urls": [url] if path == paths[0] else [],
                "preprod_urls": [url] if path == paths[0] else [], "prod_urls": []}

    scanned = []
    def fake_reference(path, *a, **k):
        scanned.append(path)
        return {"image": str(path), "preprod_urls": [url] if path == reference else []}

    monkeypatch.setattr(ech, "_process_image", fake_image)
    monkeypatch.setattr(ech, "_process_page_body_marker_image", fake_reference)
    result = ech.check_env_compliance(
        tmp_path, sample_stride=policy.auditor.env_check_sample_stride,
        expected_marketplace="US",
    )
    assert len(scanned) == result["images_scanned"] == 12
    assert result["verdict"] == ("mixed" if foreign_active else "compliant")
    assert set(result["marketplace_urls"]) == ({url, foreign} if foreign_active else {url})
    assert result["images_with_preprod"] == 2
    assert any(row.get("marketplace_reference_urls") == [url] for row in result["per_image"])
    findings = workflow_rules.check_marketplace_match(metadata(), {"env_check": result})
    assert [f["rule"] for f in findings] == (["R13"] if foreign_active else [])


def test_unreadable_environment_is_human_review_only():
    original = audit([{**gap(), "source": "env_check", "rule": "R9-amb"}])
    result = auditor.enrich_audit(original, metadata())
    assert result["review_status"] == "required"
    assert result["findings"][0]["assessment_status"] == "needs_review"
    assert result["overall_verdict"] == "concerns"


@pytest.mark.parametrize("field", ["comment", "weblinks", "test_case_weblinks"])
def test_drive_evidence_is_reviewed_without_claiming_missing_submission(field):
    meta = metadata()
    meta["test_results"][0][field] = DRIVE if field == "comment" else [DRIVE]
    original = audit([gap()])
    before = deepcopy(original)
    result = auditor.enrich_audit(original, meta)
    assert original == before
    assert result["review_status"] == "required"
    assert result["overall_verdict"] == "concerns"
    assert all(f["assessment_status"] == "needs_review" for f in result["findings"])
    assert all(f["rule"] != "R12" for f in result["findings"] if "rule" in f)
    assert auditor.enrich_audit(result, meta)["findings"] == result["findings"]


def test_drive_link_on_one_step_keeps_other_gaps_and_visible_contradictions():
    meta = metadata()
    meta["test_results"][0]["steps"][0]["weblinks"] = [DRIVE]
    contradiction = {**gap(), "severity": "high", "page": 1, "category": "product_issue",
                     "description": "Checkout shows an error instead of the expected purchase success."}
    result = auditor.enrich_audit(audit([gap(0), gap(1), contradiction]), meta)
    assert result["findings"][0]["assessment_status"] == "needs_review"
    assert result["findings"][1].get("assessment_status") != "needs_review"
    assert result["findings"][2].get("assessment_status") != "needs_review"
    assert result["overall_verdict"] == "fail"


@pytest.mark.parametrize("url", [
    "https://drive.google.com.evil.test/file/d/id",
    "https://example.test/drive.google.com/file/d/id",
    "https://drive.google.com@evil.test/file/d/id",
    "https://wiki.example.test/how-to-test",
])
def test_other_links_are_not_drive_evidence(url):
    meta = metadata()
    meta["test_results"][0]["weblinks"] = [url]
    assert hs.external_evidence(meta) == []


def test_test_instructions_are_not_submitted_drive_evidence():
    meta = metadata()
    meta["test_results"][0]["steps"][0]["expected_result"] = f"Compare with reference {DRIVE}"
    assert hs.external_evidence(meta) == []


def test_other_teams_do_not_get_hs_external_review():
    meta = metadata()
    meta["test_results"][0]["weblinks"] = [DRIVE]
    original = {**audit([gap()]), "variant_name": "tablet_v2"}
    result = auditor.enrich_audit(original, meta)
    assert result.get("review_status") != "required"
    assert len(result["findings"]) == 1


def test_drive_links_in_pdf_annotations_are_available_for_review(tmp_path):
    path = tmp_path / "session.pdf"
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((50, 50), "Recording evidence")
        page.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(40, 35, 200, 60), "uri": DRIVE})
        pdf.save(path)
    enriched = linked_evidence.with_pdf_links(metadata(), tmp_path)
    refs = hs.external_evidence(enriched)
    assert refs[0]["url"] == DRIVE
    assert refs[0]["attachment"] == "session.pdf"
    assert refs[0]["attachment_page"] == 1


@pytest.mark.parametrize("engine", ["off", "haiku"])
def test_drive_only_execution_runs_as_human_review_instead_of_r12(
    policy, tmp_path, monkeypatch, engine,
):
    policy.extractor.out_dir = tmp_path
    policy.auditor.env_check_engine = engine
    meta = metadata()
    meta["test_results"][0]["weblinks"] = [DRIVE]

    def extract(cfg):
        work = tmp_path / cfg.execution_key
        work.mkdir()
        (work / "metadata.json").write_text(json.dumps(meta))
        return 0, work

    monkeypatch.setattr(extractor, "run", extract)
    results = batch.run_batch(
        ["QA-E1"], policy, concurrency=1, extractor_concurrency=1, out=io.StringIO(),
    )
    result = results[0]
    assert result.status == "ok", result.reason
    saved = json.loads(result.audit_path.read_text())
    assert saved["review_status"] == "required"
    assert saved["overall_verdict"] == "concerns"
    assert saved["analysis_status"] == "complete"
    assert "R12" not in {f.get("rule") for f in saved["findings"]}
    assert saved["external_evidence_links"][0]["url"] == DRIVE
    from argus.reporting import report
    summary = report._build_summary(saved, meta, result.audit_path.parent)
    assert summary.review_status == "required"
    assert summary.verdict != "fail"  # The report counts only fail verdicts as deviations.


def test_hs_still_reports_genuinely_empty_submission(policy, tmp_path, monkeypatch):
    policy.extractor.out_dir = tmp_path
    policy.auditor.env_check_engine = "off"

    def extract(cfg):
        work = tmp_path / cfg.execution_key
        work.mkdir()
        (work / "metadata.json").write_text(json.dumps(metadata()))
        return 0, work

    monkeypatch.setattr(extractor, "run", extract)
    results = batch.run_batch(
        ["QA-E2"], policy, concurrency=1, extractor_concurrency=1, out=io.StringIO(),
    )
    saved = json.loads(results[0].audit_path.read_text())
    assert any(f.get("rule") == "R12" for f in saved["findings"])
