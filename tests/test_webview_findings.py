"""Keep WebView outcome findings while suppressing only optional URL proof gaps."""

import pytest

from argus.pipeline import auditor


def _filter(description, *, severity="medium", variant="v5_7", **fields):
    finding = {
        "severity": severity,
        "page": 3,
        "step_index": 1,
        "description": description,
        **fields,
    }
    audit = {
        "variant_name": variant,
        "overall_verdict": "fail" if severity == "high" else "concerns",
        "findings": [finding],
    }
    metadata = {
        "test_results": [{
            "testrun_name": "Webview E2E - US",
            "status": "Pass",
            "steps": [{"index": 1, "status": "Pass"}],
        }],
    }
    return audit, auditor._drop_false_positive_model_findings(audit, metadata)


@pytest.mark.parametrize("severity", ["medium", "high"])
@pytest.mark.parametrize("description", [
    "The Webview checkout confirmation is missing although step 1 is marked Pass.",
    "The purchased title is not visible in the Webview library after checkout.",
    "The Webview uses the wrong marketplace; chrome://inspect evidence is missing.",
    "chrome://inspect is missing. The Webview checkout shows the wrong price.",
    "The Webview URL is missing from the required confirmation email.",
    "The Webview URL is not visible after the expected redirect failed.",
    "Cannot verify the Webview purchase because the receipt is missing.",
    "The Webview close button is missing.",
    "The payment confirmation is not shown after returning from the Webview.",
])
def test_webview_outcome_findings_survive_optional_evidence_filter(description, severity):
    before, after = _filter(description, severity=severity)

    assert after == before
    assert "_dropped_model_false_positives" not in after


@pytest.mark.parametrize("description", [
    "chrome://inspect is not shown for this Webview execution.",
    "The chrome://inspect/#devices screenshot is missing.",
    "Missing chrome://inspect evidence.",
    "No chrome://inspect screenshot was provided.",
    "chrome://inspect evidence is not visible in the Webview screenshots.",
    "The Webview URL is not visible.",
    "The WebView environment URL is missing.",
    "No URL is visible in the Webview screenshots.",
    "  The Webview URL\nis not visible.  ",
])
def test_only_optional_webview_url_evidence_gap_is_suppressed(description):
    _, after = _filter(
        description, category="insufficient_evidence", action="add_evidence",
    )

    assert after["findings"] == []
    assert after["overall_verdict"] == "pass"
    assert after["_dropped_model_false_positives"] == {
        "optional_webview_url_evidence": 1,
    }


@pytest.mark.parametrize("variant", [
    "desktop_v2", "tablet_v2", "mobile_v2", "reader_v2", "tv_v2", "portal_v2",
])
def test_webview_url_exception_does_not_override_other_team_policy(variant):
    before, after = _filter(
        "The chrome://inspect/#devices screenshot is missing.", variant=variant,
    )

    assert after == before


@pytest.mark.parametrize("fields", [
    {"source": "device_check"},
    {"source": "env_check"},
    {"source": "rule", "rule": "R12"},
    {"assessment_status": "needs_review"},
    {"category": "product_issue"},
    {"action": "attach_defect"},
])
def test_optional_webview_filter_preserves_structured_outcome_and_review_findings(fields):
    before, after = _filter("The Webview URL is not visible.", **fields)

    assert after == before
