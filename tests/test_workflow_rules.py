"""Unit tests for workflow_rules.py — deterministic, metadata-only rules."""
from __future__ import annotations

import pytest

from argus.config.models import PolicySettings
from argus.domain import workflow_rules as wr


@pytest.fixture(autouse=True)
def _reset_workflow_rules_config():
    """configure() writes module globals; keep them from leaking between tests."""
    wr.reset_configuration()
    yield
    wr.reset_configuration()


def _meta(*, overall_status=None, overall_status_id=None,
          overall_comment="", overall_trace_links=None, steps=None):
    """Build a minimal metadata dict matching build_metadata's output shape."""
    return {
        "test_results": [{
            "status": overall_status,
            "status_id": overall_status_id,
            "comment": overall_comment,
            "trace_links": overall_trace_links or [],
            "steps": steps or [],
        }]
    }




def test_r0_fires_when_top_level_status_unresolved():
    findings = wr.check_rule_engine_health(_meta(
        overall_status=None,
        overall_status_id=39,
        steps=[{"index": 1, "status": "Pass", "status_id": 39}],
    ))
    assert len(findings) == 1
    f = findings[0]
    assert f["severity"] == "high"
    assert f["rule"] == "R0"
    assert f["source"] == "rule"
    assert f["step_index"] is None
    assert "status-name map" in f["description"].lower() or \
           "status name" in f["description"].lower() or \
           "map did not resolve" in f["description"]


def test_r0_fires_when_step_status_unresolved():
    findings = wr.check_rule_engine_health(_meta(
        overall_status="Pass",
        overall_status_id=39,
        steps=[
            {"index": 1, "status": "Pass", "status_id": 39},
            {"index": 2, "status": None, "status_id": 9999},
        ],
    ))
    assert len(findings) == 1
    assert findings[0]["rule"] == "R0"


def test_r0_silent_when_all_statuses_resolved():
    assert wr.check_rule_engine_health(_meta(
        overall_status="Pass",
        overall_status_id=39,
        steps=[{"index": 1, "status": "Pass", "status_id": 39}],
    )) == []


def test_r0_silent_when_no_status_ids_at_all():
    assert wr.check_rule_engine_health(_meta(
        overall_status=None,
        steps=[{"index": 1, "status": None}],
    )) == []


def test_r0_silent_on_empty_metadata():
    assert wr.check_rule_engine_health({}) == []
    assert wr.check_rule_engine_health({"test_results": []}) == []


def test_r0_fires_only_once_per_execution():
    findings = wr.check_rule_engine_health(_meta(
        overall_status=None,
        overall_status_id=39,
        steps=[
            {"index": 1, "status": None, "status_id": 9999},
            {"index": 2, "status": None, "status_id": 9999},
            {"index": 3, "status": None, "status_id": 9999},
        ],
    ))
    assert len(findings) == 1




def test_r1_fires_on_in_progress_step_without_comment():
    findings = wr.check_in_progress_steps(_meta(steps=[
        {"index": 1, "status": "Pass"},
        {"index": 2, "status": "In Progress"},
        {"index": 3, "status": "Pass"},
    ]))
    assert len(findings) == 1
    f = findings[0]
    assert f["severity"] == "medium"
    assert f["step_index"] == 2
    assert f["rule"] == "R1"
    assert f["source"] == "rule"
    assert "no explanatory comment" in f["description"]


def test_r1_silent_when_in_progress_step_has_comment():
    assert wr.check_in_progress_steps(_meta(steps=[
        {"index": 1, "status": "In Progress",
         "comment": "Waiting on upstream data fix PROD-123"},
    ])) == []


def test_r1_treats_whitespace_only_comment_as_no_comment():
    findings = wr.check_in_progress_steps(_meta(steps=[
        {"index": 1, "status": "In Progress", "comment": "   \n\t  "},
    ]))
    assert len(findings) == 1
    assert findings[0]["rule"] == "R1"


def test_r1_silent_when_no_in_progress():
    assert wr.check_in_progress_steps(_meta(steps=[
        {"index": 1, "status": "Pass"},
        {"index": 2, "status": "Fail"},
    ])) == []


def test_r1_silent_when_steps_missing():
    assert wr.check_in_progress_steps({"test_results": []}) == []
    assert wr.check_in_progress_steps({}) == []




def test_r2_fires_on_pass_overall_with_fail_step():
    findings = wr.check_fail_step_pass_overall(_meta(
        overall_status="Pass",
        steps=[
            {"index": 1, "status": "Pass"},
            {"index": 2, "status": "Fail"},
        ],
    ))
    assert len(findings) == 1
    assert findings[0]["severity"] == "high"
    assert findings[0]["step_index"] == 2
    assert findings[0]["rule"] == "R2"


def test_r2_silent_when_overall_not_pass():
    assert wr.check_fail_step_pass_overall(_meta(
        overall_status="Fail",
        steps=[{"index": 1, "status": "Fail"}],
    )) == []


def test_r2_silent_when_no_fail_step():
    assert wr.check_fail_step_pass_overall(_meta(
        overall_status="Pass",
        steps=[{"index": 1, "status": "Pass"}],
    )) == []


def test_r2_silent_when_overall_status_missing():
    assert wr.check_fail_step_pass_overall(_meta(
        overall_status=None,
        steps=[{"index": 1, "status": "Fail"}],
    )) == []


def test_r2_fires_for_multiple_fail_steps():
    findings = wr.check_fail_step_pass_overall(_meta(
        overall_status="Pass",
        steps=[
            {"index": 1, "status": "Fail"},
            {"index": 2, "status": "Pass"},
            {"index": 3, "status": "Fail"},
        ],
    ))
    assert [f["step_index"] for f in findings] == [1, 3]


def test_r2b_fires_when_pass_overall_has_only_not_executed_steps():
    findings = wr.check_pass_overall_with_no_executed_steps(_meta(
        overall_status="Pass",
        steps=[
            {"index": 1, "status": "Not Executed"},
            {"index": 2, "status": "Not Executed"},
        ],
    ))

    assert len(findings) == 1
    assert findings[0]["rule"] == "R2b"
    assert findings[0]["severity"] == "high"


@pytest.mark.parametrize(
    ("overall_status", "steps"),
    [
        ("Fail", [{"index": 1, "status": "Not Executed"}]),
        ("Pass", [{"index": 1, "status": "Pass"}]),
        ("Pass", [
            {"index": 1, "status": "Not Executed"},
            {"index": 2, "status": "Pass"},
        ]),
        ("Pass", []),
    ],
)
def test_r2b_does_not_fire_outside_all_not_executed_pass(
    overall_status,
    steps,
):
    assert wr.check_pass_overall_with_no_executed_steps(_meta(
        overall_status=overall_status,
        steps=steps,
    )) == []




def test_r3_function_still_computes_the_deprecated_reading():
    findings = wr.check_blocked_step_pass_overall(_meta(
        overall_status="Pass",
        steps=[
            {"index": 1, "status": "Pass"},
            {"index": 2, "status": "Blocked"},
        ],
    ))
    assert len(findings) == 1
    assert findings[0]["rule"] == "R3"


def test_r3_not_registered_in_aggregator():
    assert wr.check_blocked_step_pass_overall not in wr._RULES


def test_r3_does_not_fire_via_run_workflow_rules():
    findings = wr.run_workflow_rules(_meta(
        overall_status="Pass",
        overall_comment="account: tester+t1@example.com",
        steps=[
            {"index": 1, "status": "Pass"},
            {"index": 2, "status": "Blocked"},
        ],
    ))
    rules_hit = [f["rule"] for f in findings]
    assert "R3" not in rules_hit
    assert rules_hit == []




def test_r4_fires_when_overall_fail_with_all_pass_steps():
    findings = wr.check_fail_overall_without_failing_step(_meta(
        overall_status="Fail",
        steps=[
            {"index": 1, "status": "Pass"},
            {"index": 2, "status": "Pass"},
        ],
    ))
    assert len(findings) == 1
    f = findings[0]
    assert f["severity"] == "high"
    assert f["rule"] == "R4"
    assert f["source"] == "rule"
    assert f["step_index"] is None


def test_r4_silent_when_any_step_is_fail():
    assert wr.check_fail_overall_without_failing_step(_meta(
        overall_status="Fail",
        steps=[
            {"index": 1, "status": "Pass"},
            {"index": 2, "status": "Fail"},
        ],
    )) == []


def test_r4_silent_when_any_step_is_blocked():
    assert wr.check_fail_overall_without_failing_step(_meta(
        overall_status="Fail",
        steps=[
            {"index": 1, "status": "Pass"},
            {"index": 2, "status": "Blocked"},
        ],
    )) == []


def test_r4_silent_when_overall_not_fail():
    assert wr.check_fail_overall_without_failing_step(_meta(
        overall_status="Pass",
        steps=[{"index": 1, "status": "Pass"}],
    )) == []


def test_r4_silent_when_no_steps():
    assert wr.check_fail_overall_without_failing_step(_meta(
        overall_status="Fail",
        steps=[],
    )) == []


def test_r4_silent_when_any_step_unresolved():
    assert wr.check_fail_overall_without_failing_step(_meta(
        overall_status="Fail",
        steps=[
            {"index": 1, "status": "Pass"},
            {"index": 2, "status": None},
        ],
    )) == []




def test_r5_fires_on_issue_keyword_in_pass_comment():
    findings = wr.check_pass_step_with_issue_comment(_meta(steps=[
        {"index": 1, "status": "Pass",
         "comment": "Had to retry because the first attempt crashed."},
    ]))
    assert len(findings) == 1
    assert findings[0]["severity"] == "medium"
    assert findings[0]["rule"] == "R5"


def test_r5_case_insensitive_keyword_match():
    findings = wr.check_pass_step_with_issue_comment(_meta(steps=[
        {"index": 1, "status": "Pass",
         "comment": "The page BROKEN during initial load."},
    ]))
    assert len(findings) == 1


def test_r5_ignores_allowed_alternate_browser_substitution():
    findings = wr.check_pass_step_with_issue_comment(_meta(steps=[
        {
            "index": 11,
            "status": "Pass",
            "description": (
                "Open firefox or any other browser and go to example.ca"
            ),
            "expected_result": "The banner appears in the new browser",
        },
        {
            "index": 12,
            "status": "Pass",
            "description": "Login with the same user in firefox",
            "expected_result": "The banner appears in the new browser",
            "comment": "Tested in edge because firefox has issue",
        },
    ]))

    assert findings == []


def test_r5_keeps_browser_substitution_when_alternative_is_not_allowed():
    findings = wr.check_pass_step_with_issue_comment(_meta(steps=[{
        "index": 12,
        "status": "Pass",
        "description": "Verify the Firefox-specific login flow",
        "expected_result": "Firefox retains the expected session state",
        "comment": "Tested in edge because firefox has issue",
    }]))

    assert len(findings) == 1
    assert findings[0]["step_index"] == 12
    assert "Step 13" in findings[0]["description"]


def test_r5_issue_matching_uses_word_boundaries():
    assert wr.check_pass_step_with_issue_comment(_meta(steps=[{
        "index": 1,
        "status": "Pass",
        "comment": "Changed to the new banner and verified it.",
    }])) == []


def test_r5_ignores_explicit_no_issue_comment():
    assert wr.check_pass_step_with_issue_comment(_meta(steps=[{
        "index": 1,
        "status": "Pass",
        "comment": "No issues observed; working as expected.",
    }])) == []


def test_r5_redacts_credentials_from_embedded_comment():
    comment = ("error on TYP page. account "
               "qa.user+automation@example.test fixture7!token "
               "SYNTHETICACCOUNT123")
    findings = wr.check_pass_step_with_issue_comment(_meta(steps=[
        {"index": 1, "status": "Pass", "comment": comment},
    ]))
    assert len(findings) == 1
    desc = findings[0]["description"]
    assert "fixture7!token" not in desc
    assert "[redacted]" in desc
    assert "qa.user+automation@example.test" in desc
    assert "SYNTHETICACCOUNT123" in desc


def test_redact_credentials_masks_only_near_email():
    assert wr.redact_credentials(
        "qa.user@example.test fixture9!secret SYNTHETICACCOUNT456"
    ) == "qa.user@example.test [redacted] SYNTHETICACCOUNT456"
    assert "[redacted]" in wr.redact_credentials(
        "u@example.test Copy To Clipboard fixture2%token Copy To Clipboard X")
    raw = "crashed, retried with build 20260731abc and it worked"
    assert wr.redact_credentials(raw) == raw
    raw = "account u@example.test then https://example.test/item/ABC loaded fine"
    assert wr.redact_credentials(raw) == raw
    assert wr.redact_credentials("") == ""


def test_redact_credentials_masks_mixed_case_and_dotted_passwords():
    for secret in ("Fixture9Secret", "fixture.9.secret", "F1xture-Secret",
                   "S3CRETpass"):
        out = wr.redact_credentials(f"qa.user@example.test {secret} TRAILING")
        assert secret not in out, secret
        assert out == "qa.user@example.test [redacted] TRAILING"


def test_redact_credentials_masks_explicit_password_labels_without_email():
    for text in (
        "Password: Fixture9Secret",
        "passwd=fixture.9.secret",
        "pwd: F1xture-Secret",
        "passcode: Mobile7Code",
        "Password is S3CRETpass",
        "Passcode is Mobile8Code",
    ):
        out = wr.redact_credentials(text)
        assert "[redacted]" in out
        assert not any(
            secret in out
            for secret in (
                "Fixture9Secret",
                "fixture.9.secret",
                "F1xture-Secret",
                "S3CRETpass",
                "Mobile7Code",
                "Mobile8Code",
            )
        )


def test_redact_credentials_keeps_non_secret_password_outcomes():
    assert wr.redact_credentials(
        "The password is incorrect and login failed"
    ) == "The password is incorrect and login failed"


def test_redact_credentials_keeps_identity_and_ids():
    out = wr.redact_credentials(
        "qa.user@example.test Fixture9Secret SYNTHETICACCOUNT456 "
        "https://example.test/orders/A1b2c3")
    assert "qa.user@example.test" in out
    assert "SYNTHETICACCOUNT456" in out
    assert "https://example.test/orders/A1b2c3" in out
    assert out.count("[redacted]") == 1


def test_redact_credentials_masks_token_straddling_window_end():
    """A secret crossing the 120-char window edge is masked whole."""
    filler = "w" * 110
    text = f"qa.user@example.test {filler} pass9word!secret-tail"
    out = wr.redact_credentials(text)
    assert "pass9word" not in out
    assert "secret-tail" not in out
    assert out.endswith("[redacted]")


def test_redact_credentials_masks_letters_and_multiple_symbols():
    for secret in (
        "abc#def%ghijklmn",
        "ab#cd%efgh",
        "ab!cd*efgh",
        "Qa&bcd^efgh",
        "zz$xx%ccvv",
    ):
        out = wr.redact_credentials(
            f"qa.user@example.test {secret} TRAILING")
        assert secret not in out
        assert out == "qa.user@example.test [redacted] TRAILING"


def test_redact_credentials_keeps_single_symbol_reviewer_tokens():
    for value in (
        "A5W29LTCRWJA0",
        "QA-E213218",
        "pre-prod.retail.example.com",
        "US=pass&UK=pass",
        "OOBE+wallet",
    ):
        text = f"qa.user@example.test {value} TRAILING"
        assert wr.redact_credentials(text) == text


def test_redact_credentials_keeps_punctuated_reviewer_prose():
    for value in (
        "test!ok?yes",
        "**bold**text",
        "Feature#Ready%now",
    ):
        text = f"qa.user@example.test {value} TRAILING"
        assert wr.redact_credentials(text) == text


def test_redact_credentials_masks_mixed_case_symbol_password():
    text = "qa.user@example.test Secret!Pass? TRAILING"
    assert wr.redact_credentials(text) == (
        "qa.user@example.test [redacted] TRAILING"
    )


def test_marketplace_detection_supports_multiple_storefront_domains():
    from argus.config.models import PolicySettings

    wr.configure(PolicySettings(
        storefront_domain="example",
        storefront_domains=["example", "sample"],
        marketplace_tlds={
            "US": ".com",
            "UK": ".co.uk",
            "AU": ".com.au",
        },
    ))
    try:
        assert wr._detect_marketplace_from_url(
            "https://pre-prod.retail.example.com.au/oobe") == "AU"
        assert wr._detect_marketplace_from_url(
            "https://feature-preprod.example.co.uk/library") == "UK"
        assert not wr._is_storefront_host(
            "https://service.emergent.dub.retail.example.com/debug")
    finally:
        wr.reset_configuration()


def test_r5_silent_when_comment_has_no_keywords():
    assert wr.check_pass_step_with_issue_comment(_meta(steps=[
        {"index": 1, "status": "Pass",
         "comment": "Worked fine, no problems observed."},
    ])) == []


def test_r5_silent_when_step_not_pass():
    assert wr.check_pass_step_with_issue_comment(_meta(steps=[
        {"index": 1, "status": "Fail",
         "comment": "Crashed on click."},
    ])) == []


def test_r5_silent_on_empty_comment():
    assert wr.check_pass_step_with_issue_comment(_meta(steps=[
        {"index": 1, "status": "Pass", "comment": ""},
    ])) == []
    assert wr.check_pass_step_with_issue_comment(_meta(steps=[
        {"index": 1, "status": "Pass"},
    ])) == []




def test_r6_fires_when_passed_with_issue_has_no_evidence():
    findings = wr.check_passed_with_issue_has_evidence(_meta(
        overall_status="Passed With Issue",
        overall_comment="",
        overall_trace_links=[],
        steps=[{"index": 1, "status": "Pass", "comment": "",
                "trace_links": []}],
    ))
    assert len(findings) == 1
    f = findings[0]
    assert f["severity"] == "medium"
    assert f["rule"] == "R6"
    assert f["source"] == "rule"
    assert f["step_index"] is None


def test_r6_silent_when_overall_has_comment():
    assert wr.check_passed_with_issue_has_evidence(_meta(
        overall_status="Passed With Issue",
        overall_comment="Payment flow showed a transient toast error; retry worked.",
        steps=[{"index": 1, "status": "Pass"}],
    )) == []


def test_r6_silent_when_overall_has_trace_links():
    assert wr.check_passed_with_issue_has_evidence(_meta(
        overall_status="Passed With Issue",
        overall_comment="",
        overall_trace_links=["QA-BUG-123"],
        steps=[{"index": 1, "status": "Pass"}],
    )) == []


def test_r6_silent_when_any_step_has_comment():
    assert wr.check_passed_with_issue_has_evidence(_meta(
        overall_status="Passed With Issue",
        overall_comment="",
        steps=[
            {"index": 1, "status": "Pass", "comment": ""},
            {"index": 2, "status": "Pass",
             "comment": "Observed intermittent 500, retried successfully."},
        ],
    )) == []


def test_r6_silent_when_any_step_has_trace_link():
    assert wr.check_passed_with_issue_has_evidence(_meta(
        overall_status="Passed With Issue",
        overall_comment="",
        steps=[
            {"index": 1, "status": "Pass", "trace_links": []},
            {"index": 2, "status": "Pass", "trace_links": ["QA-BUG-9"]},
        ],
    )) == []


def test_r6_silent_when_overall_not_passed_with_issue():
    assert wr.check_passed_with_issue_has_evidence(_meta(
        overall_status="Pass",
        overall_comment="",
        steps=[{"index": 1, "status": "Pass"}],
    )) == []
    assert wr.check_passed_with_issue_has_evidence(_meta(
        overall_status="Fail",
        overall_comment="",
        steps=[{"index": 1, "status": "Fail"}],
    )) == []


def test_r6_treats_whitespace_only_comment_as_empty():
    findings = wr.check_passed_with_issue_has_evidence(_meta(
        overall_status="Passed With Issue",
        overall_comment="   \n  ",
        steps=[{"index": 1, "status": "Pass", "comment": "  "}],
    ))
    assert len(findings) == 1
    assert findings[0]["rule"] == "R6"




def test_r16_fires_when_overall_fail_has_no_comment_or_traceLinks():
    findings = wr.check_fail_or_blocked_has_documentation(_meta(
        overall_status="Fail",
        overall_comment="",
        overall_trace_links=[],
        steps=[{"index": 0, "status": "Fail"}],
    ))
    assert len(findings) == 1
    assert findings[0]["rule"] == "R16"
    assert findings[0]["severity"] == "high"
    assert "Fail" in findings[0]["description"]


def test_r16_fires_when_overall_blocked_has_no_documentation():
    findings = wr.check_fail_or_blocked_has_documentation(_meta(
        overall_status="Blocked",
        overall_comment="",
        steps=[{"index": 0, "status": "Blocked"}],
    ))
    assert len(findings) == 1
    assert findings[0]["rule"] == "R16"
    assert "Blocked" in findings[0]["description"]


def test_r16_silent_when_overall_has_comment():
    findings = wr.check_fail_or_blocked_has_documentation(_meta(
        overall_status="Fail",
        overall_comment="payment service returned 500 on submit",
    ))
    assert findings == []


def test_r16_silent_when_overall_has_trace_links():
    findings = wr.check_fail_or_blocked_has_documentation(_meta(
        overall_status="Fail",
        overall_trace_links=[{"key": "QA-BUG-1"}],
    ))
    assert findings == []


def test_r16_silent_when_any_step_has_comment():
    findings = wr.check_fail_or_blocked_has_documentation(_meta(
        overall_status="Fail",
        overall_comment="",
        steps=[
            {"index": 1, "status": "Fail",
             "comment": "step 1 timed out, see logs"},
        ],
    ))
    assert findings == []


def test_r16_silent_when_any_step_has_trace_link():
    findings = wr.check_fail_or_blocked_has_documentation(_meta(
        overall_status="Fail",
        overall_comment="",
        steps=[
            {"index": 1, "status": "Fail",
             "trace_links": [{"key": "QA-BUG-2"}]},
        ],
    ))
    assert findings == []


def test_r16_silent_when_overall_pass():
    findings = wr.check_fail_or_blocked_has_documentation(_meta(
        overall_status="Pass",
    ))
    assert findings == []


def test_r16_silent_when_overall_pwi():
    """PWI is R6's territory; R16 explicitly only covers Fail and Blocked."""
    findings = wr.check_fail_or_blocked_has_documentation(_meta(
        overall_status="Passed With Issue",
        overall_comment="",
    ))
    assert findings == []


def test_r16_treats_whitespace_only_comment_as_empty():
    findings = wr.check_fail_or_blocked_has_documentation(_meta(
        overall_status="Fail",
        overall_comment="   \n\t  ",
    ))
    assert len(findings) == 1
    assert findings[0]["rule"] == "R16"




def test_r7_fires_when_pwi_overall_has_no_fail_step():
    findings = wr.check_passed_with_issue_requires_fail_step(_meta(
        overall_status="Passed With Issue",
        overall_comment="had a minor hiccup",
        steps=[
            {"index": 1, "status": "Pass"},
            {"index": 2, "status": "Blocked"},
        ],
    ))
    assert len(findings) == 1
    f = findings[0]
    assert f["severity"] == "medium"
    assert f["rule"] == "R7"
    assert f["source"] == "rule"
    assert f["step_index"] is None


def test_r7_silent_when_any_step_is_fail():
    assert wr.check_passed_with_issue_requires_fail_step(_meta(
        overall_status="Passed With Issue",
        overall_comment="step 2 failed but was a non-blocker",
        steps=[
            {"index": 1, "status": "Pass"},
            {"index": 2, "status": "Fail"},
        ],
    )) == []


def test_r7_silent_when_overall_not_pwi():
    assert wr.check_passed_with_issue_requires_fail_step(_meta(
        overall_status="Pass",
        steps=[{"index": 1, "status": "Pass"}],
    )) == []
    assert wr.check_passed_with_issue_requires_fail_step(_meta(
        overall_status="Fail",
        steps=[{"index": 1, "status": "Fail"}],
    )) == []


def test_r7_silent_when_no_steps():
    assert wr.check_passed_with_issue_requires_fail_step(_meta(
        overall_status="Passed With Issue",
        steps=[],
    )) == []


def test_r7_silent_when_any_step_unresolved():
    assert wr.check_passed_with_issue_requires_fail_step(_meta(
        overall_status="Passed With Issue",
        steps=[
            {"index": 1, "status": "Pass"},
            {"index": 2, "status": None},
        ],
    )) == []




def test_r8_fires_when_blocked_overall_has_no_blocked_step():
    findings = wr.check_blocked_overall_requires_blocked_step(_meta(
        overall_status="Blocked",
        steps=[
            {"index": 1, "status": "Pass"},
            {"index": 2, "status": "Fail"},
        ],
    ))
    assert len(findings) == 1
    f = findings[0]
    assert f["severity"] == "high"
    assert f["rule"] == "R8"
    assert f["source"] == "rule"
    assert f["step_index"] is None


def test_r8_silent_when_blocked_step_exists():
    assert wr.check_blocked_overall_requires_blocked_step(_meta(
        overall_status="Blocked",
        steps=[
            {"index": 1, "status": "Pass"},
            {"index": 2, "status": "Blocked"},
        ],
    )) == []


def test_r8_silent_when_overall_not_blocked():
    assert wr.check_blocked_overall_requires_blocked_step(_meta(
        overall_status="Pass",
        steps=[{"index": 1, "status": "Pass"}],
    )) == []


def test_r8_silent_when_no_steps():
    assert wr.check_blocked_overall_requires_blocked_step(_meta(
        overall_status="Blocked",
        steps=[],
    )) == []


def test_r8_silent_when_any_step_unresolved():
    assert wr.check_blocked_overall_requires_blocked_step(_meta(
        overall_status="Blocked",
        steps=[
            {"index": 1, "status": None},
            {"index": 2, "status": "Blocked"},
        ],
    )) == []




def test_run_workflow_rules_aggregates_across_rules():
    meta = _meta(
        overall_status="Pass",
        overall_status_id=39,
        steps=[
            {"index": 0, "status": None, "status_id": 9999},
            {"index": 1, "status": "In Progress"},
            {"index": 2, "status": "Fail"},
            {"index": 3, "status": "Blocked"},
            {"index": 4, "status": "Pass", "comment": "hit an error"},
            {"index": 5, "status": "Pass"},
        ],
    )
    findings = wr.run_workflow_rules(meta)
    rules_hit = [f["rule"] for f in findings]
    assert rules_hit == ["R0", "R1", "R2", "R5", "R11", "R22"]
    assert "R3" not in rules_hit
    assert all(f["source"] == "rule" for f in findings)
    sev_by_rule = {f["rule"]: f["severity"] for f in findings}
    assert sev_by_rule == {
        "R0": "high",
        "R1": "medium",
        "R2": "high",
        "R5": "medium",
        "R11": "medium",
        "R22": "low",
    }


def test_run_workflow_rules_fires_r4_on_overall_fail_all_pass_steps():
    meta = _meta(
        overall_status="Fail",
        steps=[
            {"index": 1, "status": "Pass"},
            {"index": 2, "status": "Pass"},
        ],
    )
    findings = wr.run_workflow_rules(meta)
    assert [f["rule"] for f in findings] == ["R4", "R16", "R22"]
    rule_to_sev = {f["rule"]: f["severity"] for f in findings}
    assert rule_to_sev == {"R4": "high", "R16": "high", "R22": "low"}


def test_run_workflow_rules_fires_r6_on_unsupported_passed_with_issue():
    meta = _meta(
        overall_status="Passed With Issue",
        overall_comment="",
        overall_trace_links=[],
        steps=[
            {"index": 1, "status": "Pass"},
            {"index": 2, "status": "Fail"},
        ],
    )
    findings = wr.run_workflow_rules(meta)
    rules = [f["rule"] for f in findings]
    assert rules == ["R6", "R11", "R22"]
    sev = {f["rule"]: f["severity"] for f in findings}
    assert sev == {"R6": "medium", "R11": "medium", "R22": "low"}


def test_run_workflow_rules_fires_r7_on_pwi_without_fail_step():
    meta = _meta(
        overall_status="Passed With Issue",
        overall_comment="some context — ran as tester+t1@example.com",
        steps=[
            {"index": 1, "status": "Pass"},
            {"index": 2, "status": "Blocked"},
        ],
    )
    findings = wr.run_workflow_rules(meta)
    assert [f["rule"] for f in findings] == ["R7"]
    assert findings[0]["severity"] == "medium"


def test_run_workflow_rules_fires_r8_on_blocked_overall_without_blocked_step():
    meta = _meta(
        overall_status="Blocked",
        steps=[
            {"index": 1, "status": "Pass"},
            {"index": 2, "status": "Fail",
             "trace_links": ["QA-BUG-1"]},
        ],
    )
    findings = wr.run_workflow_rules(meta)
    assert [f["rule"] for f in findings] == ["R8"]
    assert findings[0]["severity"] == "high"


def test_run_workflow_rules_empty_when_execution_clean():
    meta = _meta(
        overall_status="Pass",
        overall_status_id=39,
        overall_comment="tester+clean@example.com / <pw>",
        steps=[
            {"index": 1, "status": "Pass", "status_id": 39},
            {"index": 2, "status": "Pass", "status_id": 39},
        ],
    )
    assert wr.run_workflow_rules(meta) == []


def test_run_workflow_rules_accepts_pass_with_nonblocking_blocked_step():
    meta = _meta(
        overall_status="Pass",
        overall_status_id=39,
        overall_comment="tester+t2@example.com",
        steps=[
            {"index": 1, "status": "Pass", "status_id": 39},
            {"index": 2, "status": "Blocked", "status_id": 41},
        ],
    )
    assert wr.run_workflow_rules(meta) == []


def test_run_workflow_rules_safe_on_empty_metadata():
    assert wr.run_workflow_rules({}) == []
    assert wr.run_workflow_rules({"test_results": []}) == []




def test_r10_silent_on_configured_external_ticket_url_in_comment():
    from argus.config.models import PolicySettings
    wr.configure(PolicySettings(defect_url_hosts=["issues.example.com"]))
    findings = wr.check_defect_refs_in_comment_text(_meta(
        overall_status="Pass",
        steps=[
            {"index": 1, "status": "Pass",
             "comment": "see https://issues.example.com/browse/BUG-2 for ticket"},
        ],
    ))
    assert findings == []


def test_r10_fires_on_jira_key_in_comment():
    findings = wr.check_defect_refs_in_comment_text(_meta(
        steps=[
            {"index": 3, "status": "Pass",
             "comment": "blocked, see QA-BUG-12345 for context"},
        ],
    ))
    assert len(findings) == 1
    assert findings[0]["rule"] == "R10"
    assert findings[0]["step_index"] == 3
    assert "QA-BUG-12345" in findings[0]["description"]


def test_r10_silent_when_trace_links_present():
    findings = wr.check_defect_refs_in_comment_text(_meta(
        steps=[
            {"index": 1, "status": "Pass",
             "comment": "see QA-BUG-12345",
             "trace_links": ["QA-BUG-12345"]},
        ],
    ))
    assert findings == []


def test_r10_silent_on_irrelevant_key_prefixes():
    findings = wr.check_defect_refs_in_comment_text(_meta(
        steps=[
            {"index": 1, "status": "Pass",
             "comment": "context FOO-1234 unrelated"},
        ],
    ))
    assert findings == []


def test_r10_silent_on_empty_comment():
    findings = wr.check_defect_refs_in_comment_text(_meta(
        steps=[{"index": 1, "status": "Pass", "comment": ""}],
    ))
    assert findings == []




def test_r11_fires_on_fail_step_with_no_defect_ref_anywhere():
    findings = wr.check_fail_step_requires_defect_ref(_meta(
        steps=[
            {"index": 4, "status": "Fail", "comment": "didn't work"},
        ],
    ))
    assert len(findings) == 1
    f = findings[0]
    assert f["rule"] == "R11"
    assert f["severity"] == "medium"
    assert f["step_index"] == 4
    assert "trace_links" in f["description"]


def test_r11_silent_when_step_has_trace_links():
    """Form (a): trace_links attached — canonical path satisfied."""
    findings = wr.check_fail_step_requires_defect_ref(_meta(
        steps=[
            {"index": 1, "status": "Fail",
             "comment": "broken",
             "trace_links": ["QA-BUG-12345"]},
        ],
    ))
    assert findings == []


def test_r11_accepts_execution_web_link_defect_reference():
    metadata = _meta(
        overall_status="Fail",
        steps=[{"index": 1, "status": "Fail", "comment": ""}],
    )
    metadata["test_results"][0]["weblinks"] = [
        "https://issues.example.com/browse/BUG-1"
    ]
    assert wr.check_fail_step_requires_defect_ref(metadata) == []


def test_r11_silent_when_comment_has_issue_url():
    findings = wr.check_fail_step_requires_defect_ref(_meta(
        steps=[
            {"index": 2, "status": "Fail",
             "comment": "see https://issues.example.com/browse/BUG-2"},
        ],
    ))
    assert findings == []


def test_r11_silent_when_comment_has_jira_key():
    findings = wr.check_fail_step_requires_defect_ref(_meta(
        steps=[
            {"index": 3, "status": "Fail",
             "comment": "broken — QA-BUG-9999 filed"},
        ],
    ))
    assert findings == []


def test_r11_silent_on_non_fail_steps():
    """Pass / Blocked / In Progress steps don't need a defect ref."""
    findings = wr.check_fail_step_requires_defect_ref(_meta(
        steps=[
            {"index": 1, "status": "Pass", "comment": ""},
            {"index": 2, "status": "Blocked", "comment": ""},
            {"index": 3, "status": "In Progress", "comment": ""},
        ],
    ))
    assert findings == []


def test_r11_fires_per_offending_fail_step():
    """Multi-fire: 2 undocumented Fail steps => 2 findings."""
    findings = wr.check_fail_step_requires_defect_ref(_meta(
        steps=[
            {"index": 1, "status": "Fail", "comment": "broken"},
            {"index": 2, "status": "Pass"},
            {"index": 3, "status": "Fail",
             "trace_links": ["QA-BUG-1"]},
            {"index": 4, "status": "Fail", "comment": ""},
        ],
    ))
    assert len(findings) == 2
    indices = sorted(f["step_index"] for f in findings)
    assert indices == [1, 4]


def test_r11_aggregator_includes_r11_in_rules_tuple():
    """R11 must run via run_workflow_rules, not just the standalone fn."""
    findings = wr.run_workflow_rules(_meta(
        overall_status="Passed With Issue",
        overall_comment="some context",
        steps=[
            {"index": 1, "status": "Pass"},
            {"index": 2, "status": "Fail", "comment": "broken"},
        ],
    ))
    rules_hit = {f["rule"] for f in findings}
    assert "R11" in rules_hit




def test_r11_fires_on_pwi_step_with_no_defect_ref():
    findings = wr.check_fail_step_requires_defect_ref(_meta(
        steps=[
            {"index": 5, "status": "Passed With Issue",
             "comment": "saw a glitch but moved on"},
        ],
    ))
    assert len(findings) == 1
    f = findings[0]
    assert f["rule"] == "R11"
    assert f["severity"] == "medium"
    assert f["step_index"] == 5
    assert "Passed With Issue" in f["description"]
    assert "PWI without" in f["description"]


def test_r11_silent_on_pwi_step_with_trace_links():
    findings = wr.check_fail_step_requires_defect_ref(_meta(
        steps=[
            {"index": 1, "status": "Passed With Issue",
             "comment": "minor",
             "trace_links": ["QA-BUG-1"]},
        ],
    ))
    assert findings == []


def test_r11_silent_on_pwi_step_with_defect_ref_in_comment():
    findings = wr.check_fail_step_requires_defect_ref(_meta(
        steps=[
            {"index": 2, "status": "Passed With Issue",
             "comment": "see QA-BUG-12345 for the glitch"},
        ],
    ))
    assert findings == []


def test_r11_silent_on_clean_pass_step():
    findings = wr.check_fail_step_requires_defect_ref(_meta(
        steps=[{"index": 1, "status": "Pass", "comment": ""}],
    ))
    assert findings == []


def test_r11_distinguishes_fail_vs_pwi_wording():
    """Wording differs by status so reviewers see what's wrong."""
    fail_findings = wr.check_fail_step_requires_defect_ref(_meta(
        steps=[{"index": 1, "status": "Fail", "comment": "broken"}],
    ))
    pwi_findings = wr.check_fail_step_requires_defect_ref(_meta(
        steps=[{"index": 1, "status": "Passed With Issue",
                "comment": "minor"}],
    ))
    assert "marked Fail" in fail_findings[0]["description"]
    assert "marked 'Passed With Issue'" in pwi_findings[0]["description"]


def test_r11_fires_on_mixed_fail_and_pwi():
    findings = wr.check_fail_step_requires_defect_ref(_meta(
        steps=[
            {"index": 1, "status": "Fail", "comment": "broken"},
            {"index": 2, "status": "Pass"},
            {"index": 3, "status": "Passed With Issue",
             "comment": "minor glitch"},
            {"index": 4, "status": "Blocked", "comment": ""},
        ],
    ))
    indices = sorted(f["step_index"] for f in findings)
    assert indices == [1, 3]


class _R20Entry:
    def __init__(self, status, e_key="E-PRIOR", date="2026-06-01"):
        self.status = status
        self.e_key = e_key
        self.date = date


class _R20Index:
    def __init__(self, entries):
        self._entries = entries

    def get_history(self, tc_key, mp):
        return list(self._entries)


def _status_meta(status, *, comment="", tc="QA-T1", mp="US",
                 execution_date=None):
    return {"test_results": [{
        "status": status,
        "test_case_key": tc,
        "marketplace": mp,
        "executed_by_name": "Tester X",
        "comment": comment,
        "trace_links": [],
        "steps": [],
        "execution_date": execution_date,
    }]}


def test_r20_undocumented_blocked_is_high():
    f = wr.build_r20_finding(_status_meta("Blocked"))
    assert f["rule"] == "R20" and f["severity"] == "high"
    assert "No tester comment" in f["description"]


def test_r20_documented_blocked_is_medium():
    f = wr.build_r20_finding(_status_meta("Blocked", comment="env down"))
    assert f["severity"] == "medium"


def test_r20_escalates_on_prior_blocked_history():
    idx = _R20Index([_R20Entry("Blocked")])
    f = wr.build_r20_finding(_status_meta("Blocked", comment="env down"),
                             idx)
    assert f["severity"] == "high"
    assert "ALSO Blocked" in f["description"]


def test_r20_no_escalation_when_prior_was_pass():
    idx = _R20Index([_R20Entry("Pass")])
    f = wr.build_r20_finding(_status_meta("Blocked", comment="env down"),
                             idx)
    assert f["severity"] == "medium"


def test_r20_history_skips_current_execution():
    idx = _R20Index([_R20Entry("Blocked", e_key="E-CURRENT")])
    f = wr.build_r20_finding(_status_meta("Blocked", comment="env down"),
                             idx, current_e_key="E-CURRENT")
    assert f["severity"] == "medium"


def test_r20_none_on_other_statuses():
    assert wr.build_r20_finding(_status_meta("Pass")) is None
    assert wr.build_r20_finding(_status_meta("In Progress")) is None


def test_r21_in_progress_severity_ladder():
    high = wr.build_r21_finding(_status_meta("In Progress"))
    assert high["rule"] == "R21" and high["severity"] == "high"
    med = wr.build_r21_finding(
        _status_meta("In Progress", comment="resuming Monday"))
    assert med["severity"] == "medium"
    assert wr.build_r21_finding(_status_meta("Blocked")) is None


def test_r21_credentials_only_comment_does_not_explain_state():
    md = _status_meta(
        "In Progress",
        comment=(
            "tester+sample@example.com Copy To Clipboard "
            "afxwjny1yqm7718# Copy To Clipboard A15KM1QSDCBNT4"
        ),
    )
    finding = wr.build_r21_finding(md)
    assert finding["severity"] == "high"
    assert "No comment explains" in finding["description"]


def test_r21_credentials_plus_explanation_is_documented():
    md = _status_meta(
        "In Progress",
        comment=(
            "tester+sample@example.com afxwjny1yqm7718# "
            "A15KM1QSDCBNT4 waiting for the daily deal email"
        ),
    )
    finding = wr.build_r21_finding(md)
    assert finding["severity"] == "medium"
    assert "comment explains the state" in finding["description"]


def test_r21_trace_link_counts_as_explanation():
    md = _status_meta("In Progress")
    md["test_results"][0]["trace_links"] = ["QA-BUG-1"]
    assert wr.build_r21_finding(md)["severity"] == "medium"




def test_r22_fires_when_executed_with_no_account_in_comments():
    f = wr.check_test_account_in_comments(_status_meta("Pass"))
    assert len(f) == 1
    assert f[0]["rule"] == "R22"
    assert f[0]["severity"] == "low"
    assert f[0]["step_index"] is None
    assert "Tester X" in f[0]["description"]
    assert f[0]["advisory"] is True
    assert "password" in f[0]["description"].lower()


def test_r22_enforced_mode_is_operational_medium():
    wr.configure(PolicySettings(r22_mode="enforced"))
    [finding] = wr.check_test_account_in_comments(_status_meta("Pass"))
    assert finding["severity"] == "medium"
    assert finding["advisory"] is False


@pytest.mark.parametrize("enabled", [[], ["R0", "R1", "R22"]])
def test_r22_disabled_suppresses_findings_even_with_allowlist_and_severity(enabled):
    wr.configure(PolicySettings(
        r22_mode="disabled", enabled_rules=enabled, rule_severities={"R22": "high"},
    ))
    for status in ("Pass", "Fail", "Passed With Issue"):
        metadata = _status_meta(status)
        assert wr.check_test_account_in_comments(metadata) == []
        assert all(f["rule"] != "R22" for f in wr.run_workflow_rules(metadata))
    findings = [
        {"rule": "R0", "severity": "high"},
        {"rule": "R1", "severity": "medium"},
        {"rule": "r22", "severity": "low"},
    ]
    assert wr.filter_enabled(findings) == findings[:2]
    wr.configure(PolicySettings(r22_mode="advisory"))
    assert wr.check_test_account_in_comments(_status_meta("Pass"))


def test_r22_silent_when_top_comment_has_account_email():
    md = _status_meta(
        "Pass",
        comment="qa.user+test@example.test / synthetic-password")
    assert wr.check_test_account_in_comments(md) == []


def test_r22_silent_when_step_comment_has_account_email():
    md = _status_meta("Fail")
    md["test_results"][0]["steps"] = [
        {"index": 1, "status": "Fail",
         "comment": "used qa.user+test@example.test"},
    ]
    assert wr.check_test_account_in_comments(md) == []


def test_r22_fires_on_all_executed_statuses():
    for status in ("Pass", "Fail", "Passed With Issue"):
        assert wr.check_test_account_in_comments(_status_meta(status)), (
            f"R22 should fire for executed status {status!r}")


def test_r22_silent_on_non_executed_statuses():
    for status in ("Blocked", "In Progress", "Not Applicable",
                   "Not Executed", None):
        assert wr.check_test_account_in_comments(
            _status_meta(status)) == [], (
            f"R22 must not fire for status {status!r}")


def test_r22_plain_text_without_email_still_fires():
    md = _status_meta("Pass", comment="worked fine on retry")
    assert len(wr.check_test_account_in_comments(md)) == 1


def test_r22_registered_in_rules_tuple():
    assert wr.check_test_account_in_comments in wr._RULES




def _passed_vocabulary(**overrides):
    """PolicySettings for a project that spells its statuses differently."""
    statuses = {
        "pass": "Passed",
        "fail": "Failed",
        "blocked": "Blocked",
        "in_progress": "Executing",
        "passed_with_issue": "Passed With Defect",
    }
    statuses.update(overrides)
    return PolicySettings(statuses=statuses)


def test_status_role_maps_default_vocabulary():
    assert wr.status_role("Pass") == "pass"
    assert wr.status_role("Fail") == "fail"
    assert wr.status_role("In Progress") == "in_progress"
    assert wr.status_role("Passed With Issue") == "passed_with_issue"


def test_status_role_tolerates_spelling_variants():
    assert wr.status_role("PassedWithIssue") == "passed_with_issue"
    assert wr.status_role("InProgress") == "in_progress"
    assert wr.status_role("N/A") == "not_applicable"
    assert wr.status_role("not-applicable") == "not_applicable"
    assert wr.status_role("  pass  ") == "pass"


def test_status_role_returns_none_for_unknown():
    assert wr.status_role("Passed") is None
    assert wr.status_role("") is None
    assert wr.status_role(None) is None


def test_r0_fires_on_unknown_status_name():
    """The silent-clean case: ids resolved, but to names we don't know."""
    findings = wr.check_rule_engine_health(_meta(
        overall_status="Passed",
        overall_status_id=39,
        steps=[{"index": 1, "status": "Passed", "status_id": 39}],
    ))
    assert len(findings) == 1
    f = findings[0]
    assert f["rule"] == "R0"
    assert f["severity"] == "high"
    assert "'Passed'" in f["description"]
    assert "pass='Pass'" in f["description"]


def test_r0_names_step_level_unknown_statuses():
    findings = wr.check_rule_engine_health(_meta(
        overall_status="Pass",
        overall_status_id=39,
        steps=[{"index": 1, "status": "Wibble", "status_id": 40}],
    ))
    assert len(findings) == 1
    assert "'Wibble'" in findings[0]["description"]


def test_r0_unknown_status_fires_once_with_all_names():
    findings = wr.check_rule_engine_health(_meta(
        overall_status="Passed",
        overall_status_id=39,
        steps=[
            {"index": 1, "status": "Passed", "status_id": 39},
            {"index": 2, "status": "Skipped", "status_id": 41},
        ],
    ))
    assert len(findings) == 1
    desc = findings[0]["description"]
    assert "'Passed'" in desc and "'Skipped'" in desc


def test_r0_prefers_unresolved_id_message_when_both_conditions_hold():
    findings = wr.check_rule_engine_health(_meta(
        overall_status=None,
        overall_status_id=39,
        steps=[{"index": 1, "status": "Passed", "status_id": 40}],
    ))
    assert len(findings) == 1
    assert "status-name map did not resolve" in findings[0]["description"]


def test_r0_silent_on_known_variant_spellings():
    for status in ("PassedWithIssue", "N/A", "Not Executed", "Retest"):
        assert wr.check_rule_engine_health(_meta(
            overall_status=status,
            overall_status_id=39,
        )) == [], f"R0 must not fire for known variant {status!r}"


def test_configure_makes_a_renamed_vocabulary_valid():
    wr.configure(_passed_vocabulary())
    assert wr.status_role("Passed") == "pass"
    assert wr.status_role("Executing") == "in_progress"
    assert wr.status_role("Pass") is None
    assert wr.check_rule_engine_health(_meta(
        overall_status="Passed",
        overall_status_id=39,
    )) == []


def test_configured_vocabulary_makes_rules_evaluate():
    """The whole point: renamed statuses must still trigger the rules."""
    wr.configure(_passed_vocabulary())
    findings = wr.check_fail_step_pass_overall(_meta(
        overall_status="Passed",
        steps=[{"index": 1, "status": "Failed"}],
    ))
    assert len(findings) == 1
    assert findings[0]["rule"] == "R2"
    findings = wr.check_in_progress_steps(_meta(
        steps=[{"index": 1, "status": "Executing", "comment": ""}],
    ))
    assert len(findings) == 1
    assert findings[0]["rule"] == "R1"


def test_configured_vocabulary_reaches_r16_and_r22():
    wr.configure(_passed_vocabulary())
    findings = wr.check_fail_or_blocked_has_documentation(_meta(
        overall_status="Failed",
        steps=[{"index": 1, "status": "Failed"}],
    ))
    assert len(findings) == 1 and findings[0]["rule"] == "R16"
    findings = wr.check_test_account_in_comments(_meta(
        overall_status="Passed",
        steps=[{"index": 1, "status": "Passed"}],
    ))
    assert len(findings) == 1 and findings[0]["rule"] == "R22"


def test_configure_merges_over_defaults():
    wr.configure(PolicySettings(statuses={"pass": "Passed"}))
    assert wr.status_role("Passed") == "pass"
    assert wr.status_role("Fail") == "fail"
    assert wr.status_role("Not Applicable") == "not_applicable"


def test_configure_ignores_unknown_role_key_loudly(capsys):
    wr.configure(PolicySettings(statuses={"passs": "Passed"}))
    err = capsys.readouterr().err
    assert "unknown [policy] statuses key" in err
    assert "'passs'" in err
    assert wr.status_role("Pass") == "pass"


def test_empty_statuses_preserves_default_behaviour():
    wr.configure(PolicySettings())
    assert wr.status_role("Pass") == "pass"
    assert wr.check_rule_engine_health(_meta(
        overall_status="Pass",
        overall_status_id=39,
    )) == []


def test_unknown_status_warns_on_stderr_once_per_process(capsys):
    md = _meta(overall_status="Passed", overall_status_id=39)
    wr.check_rule_engine_health(md)
    first = capsys.readouterr().err
    assert "unrecognised execution status" in first
    wr.check_rule_engine_health(md)
    assert capsys.readouterr().err == ""


def _f(rule: str) -> dict:
    return {"severity": "medium", "page": None, "step_index": None,
            "description": "x", "source": "rule", "rule": rule}


def test_filter_enabled_never_drops_r0():
    """Verify rule filters always retain R0."""
    wr.configure(PolicySettings(enabled_rules=["R1"]))
    kept = wr.filter_enabled([_f("R0"), _f("R1"), _f("R2")])
    assert [f["rule"] for f in kept] == ["R0", "R1"]


def test_filter_enabled_matches_sub_ids_by_base_rule():
    """Verify base rule IDs enable their sub-rules."""
    wr.configure(PolicySettings(enabled_rules=["R14"]))
    kept = wr.filter_enabled([_f("R14a"), _f("R14b"), _f("R17")])
    assert [f["rule"] for f in kept] == ["R14a", "R14b"]


@pytest.mark.parametrize(
    ("rule", "expected"),
    [
        ("R14a", "R14"),
        ("r9-doc", "R9"),
        ("R2B22", "R2"),
        ("custom", "CUSTOM"),
    ],
)
def test_rule_base_normalizes_parent_family(rule, expected):
    assert wr.rule_base(rule) == expected


def test_filter_enabled_empty_allowlist_keeps_everything():
    assert len(wr.filter_enabled([_f("R1"), _f("R14a")])) == 2


def test_filter_enabled_applies_exact_severity_override_without_mutation():
    original = _f("R1")
    wr.configure(PolicySettings(rule_severities={"R1": "high"}))

    [updated] = wr.filter_enabled([original])

    assert updated["severity"] == "high"
    assert original["severity"] == "medium"


def test_filter_enabled_applies_subrule_severity_override():
    wr.configure(PolicySettings(
        enabled_rules=["R9"],
        rule_severities={"R9-DOC": "medium"},
    ))

    [updated] = wr.filter_enabled([{
        **_f("R9-doc"),
        "severity": "high",
    }])

    assert updated["severity"] == "medium"


def test_r13_flags_marketplace_switch_even_when_assigned_marketplace_seen():
    metadata = {
        "test_results": [{
            "marketplace": "US",
        }],
    }
    audit = {
        "env_check": {
            "preprod_urls": ["feature-preprod.example.com"],
            "prod_urls": ["example.it/typ"],
        },
    }

    [finding] = wr.check_marketplace_match(metadata, audit)

    assert finding["rule"] == "R13"
    assert finding["severity"] == "high"
    assert "different marketplace (IT)" in finding["description"]
    assert "switched marketplaces during the run" in finding["description"]


def test_r13_accepts_only_assigned_marketplace_urls():
    metadata = {
        "test_results": [{
            "marketplace": "US",
        }],
    }
    audit = {
        "env_check": {
            "preprod_urls": ["feature-preprod.example.com"],
            "prod_urls": ["www.example.com/typ"],
        },
    }

    assert wr.check_marketplace_match(metadata, audit) == []


def test_reset_configuration_restores_every_default():
    wr.configure(PolicySettings(
        statuses={"pass": "Passed"},
        defect_key_prefixes=["FOO-"],
        storefront_domain="example",
        enabled_rules=["R1"],
        marketplace_tlds={"XX": ".xx"},
    ))
    wr.reset_configuration()
    assert wr.status_role("Pass") == "pass"
    assert wr._DEFECT_KEY_PREFIXES == wr._DEFAULT_DEFECT_KEY_PREFIXES
    assert wr._STOREFRONT_DOMAIN == wr._DEFAULT_STOREFRONT_DOMAIN
    assert wr._ENABLED_RULES == frozenset()
    assert wr._TLD_TO_MARKETPLACE == wr._DEFAULT_TLD_TO_MARKETPLACE
