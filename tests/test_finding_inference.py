"""Guard against re-divergence of shared finding-triage inference."""
from __future__ import annotations

from argus.domain import finding_inference
from argus.pipeline import auditor
from argus.reporting import report


def test_auditor_aliases_point_at_shared_module():
    assert auditor._infer_finding_category is finding_inference.infer_finding_category
    assert auditor._infer_finding_action is finding_inference.infer_finding_action
    assert (
        auditor._infer_finding_confidence
        is finding_inference.infer_finding_confidence
    )
    assert (
        auditor._normalise_findings_for_schema
        is finding_inference.normalise_findings_for_schema
    )
    assert auditor.VALID_FINDING_CATEGORIES is finding_inference.VALID_FINDING_CATEGORIES
    assert auditor.VALID_FINDING_ACTIONS is finding_inference.VALID_FINDING_ACTIONS
    assert auditor.VALID_SEVERITIES is finding_inference.VALID_SEVERITIES


def test_report_enrich_uses_shared_normalisation():
    findings = [
        {"description": "checkout broken", "category": "product_issue",
         "severity": "low"},
        {"description": "no screenshot of the library page",
         "category": "product_issue", "severity": "high"},
        {"description": "prod url seen", "source": "env_check",
         "rule": "R9", "severity": "high"},
        {"description": "banner not rendered on screen", "severity": "high"},
    ]
    via_report = report._report_enrich_audit({"findings": findings})["findings"]
    via_shared = finding_inference.normalise_findings_for_schema(findings)
    assert via_report == via_shared


def test_report_category_enums_match_shared():
    assert set(report._VALID_FINDING_CATEGORIES) == (
        finding_inference.VALID_FINDING_CATEGORIES
    )
    assert set(report._VALID_FINDING_ACTIONS) == (
        finding_inference.VALID_FINDING_ACTIONS - {"none"}
    )


def test_advisory_env_notes_carry_no_action_even_when_stored_otherwise():
    for rule in ("R9-susp", "R9-nav"):
        finding = {
            "rule": rule,
            "source": "env_check",
            "severity": "low",
            "action": "re_run",
            "description": "Approved policy note.",
        }
        category = finding_inference.infer_finding_category(finding)

        assert category == "policy_exception"
        assert finding_inference.infer_finding_action(
            finding,
            category,
        ) == "none"


def test_r2b_is_a_status_fix_action():
    finding = {
        "rule": "R2b",
        "source": "rule",
        "severity": "high",
        "description": "Overall Pass with all steps Not Executed.",
    }
    category = finding_inference.infer_finding_category(finding)

    assert category == "tester_error"
    assert finding_inference.infer_finding_action(
        finding,
        category,
    ) == "fix_status"



def _medium(desc, page=None, step_index=None, **kw):
    f = {"severity": "medium", "description": desc}
    if page is not None:
        f["page"] = page
    if step_index is not None:
        f["step_index"] = step_index
    f.update(kw)
    return f


def test_escalates_e185519_shaped_contradiction_with_page_anchor():
    f = _medium(
        "Step 1 requires purchase 'with cash using express checkout', but "
        "the checkout screenshot (page 4) shows payment via 'Visa finissant "
        "par 8989'. There is no screenshot showing a cash payment. The "
        "evidence directly contradicts this.",
        page=4, step_index=1, confidence="high",
        category="insufficient_evidence",
    )
    assert finding_inference.apply_contradiction_escalation(f) is True
    assert f["severity"] == "high"
    assert f["_severity_adjusted_from"] == "medium"
    assert f["action"] == "review_manually"
    assert f["confidence"] == "high"


def test_escalates_with_step_only_anchor():
    f = _medium("shows the opposite of the expected state", step_index=3)
    assert finding_inference.apply_contradiction_escalation(f) is True
    assert f["severity"] == "high"


def test_no_escalation_without_anchor():
    f = _medium("the screenshots contradict the tester's Pass claim")
    assert finding_inference.apply_contradiction_escalation(f) is False
    assert f["severity"] == "medium"
    assert "_severity_adjusted_from" not in f


def test_no_escalation_for_deterministic_sources():
    for source in ("rule", "env_check", "device_check"):
        f = _medium("contradicts the expected result", page=2, source=source)
        assert finding_inference.apply_contradiction_escalation(f) is False, source
        assert f["severity"] == "medium"


def test_no_escalation_for_low_severity():
    f = {"severity": "low", "page": 2,
         "description": "arguably contradicts the expected result"}
    assert finding_inference.apply_contradiction_escalation(f) is False
    assert f["severity"] == "low"


def test_no_escalation_for_pure_evidence_gap_wording():
    f = _medium(
        "The post-return library state is never shown in any screenshot, "
        "so the refund cannot be confirmed.",
        page=6,
    )
    assert finding_inference.apply_contradiction_escalation(f) is False
    assert f["severity"] == "medium"


def test_no_escalation_when_already_adjusted():
    f = _medium("contradicts the expected result", page=2,
                _severity_adjusted_from="high")
    assert finding_inference.apply_contradiction_escalation(f) is False
    assert f["severity"] == "medium"


def test_calibration_tool_imports_shared_term_list():
    import sys
    from pathlib import Path
    tools_dir = Path(finding_inference.__file__).resolve().parents[3] / "tools"
    sys.path.insert(0, str(tools_dir))
    try:
        import calibration_verdicts
        assert (calibration_verdicts.STRONG_CONTRADICTION_TERMS
                is finding_inference.STRONG_CONTRADICTION_TERMS)
        assert "never shown" not in finding_inference.STRONG_CONTRADICTION_TERMS
    finally:
        sys.path.remove(str(tools_dir))



def test_normalise_escalates_contradiction_medium():
    out = finding_inference.normalise_findings_for_schema([
        _medium("page 4 shows Visa payment, which directly contradicts "
                "the expected cash payment", page=4),
    ])
    assert out[0]["severity"] == "high"
    assert out[0]["_severity_adjusted_from"] == "medium"
    assert out[0]["action"] == "review_manually"


def test_normalise_double_pass_is_idempotent():
    once = finding_inference.normalise_findings_for_schema([
        _medium("directly contradicts the expected result", page=4),
    ])
    twice = finding_inference.normalise_findings_for_schema(once)
    assert twice == once
    assert twice[0]["severity"] == "high"


def test_escalated_step_only_finding_not_demoted_on_repass():
    once = finding_inference.normalise_findings_for_schema([
        _medium("the library screenshot contradicts the tester's Pass "
                "claim — the purchased title is absent", step_index=6),
    ])
    assert once[0]["severity"] == "high"
    assert once[0]["page"] is None if "page" in once[0] else True
    twice = finding_inference.normalise_findings_for_schema(once)
    assert twice[0]["severity"] == "high"
    assert twice[0]["_severity_adjusted_from"] == "medium"


def test_demoted_finding_not_reescalated_on_repass():
    once = finding_inference.normalise_findings_for_schema([
        {"severity": "high", "step_index": 2,
         "description": "the banner on screen contradicts the expected "
                        "copy"},
    ])
    assert once[0]["severity"] == "medium"
    assert once[0]["_severity_adjusted_from"] == "high"
    twice = finding_inference.normalise_findings_for_schema(once)
    assert twice[0]["severity"] == "medium"
    assert twice[0]["_severity_adjusted_from"] == "high"


def test_normalisation_redacts_credentials_in_model_descriptions():
    out = finding_inference.normalise_findings_for_schema([
        {"severity": "medium", "page": 3,
         "description": "tester noted account qa.user@example.test "
                        "Fixture9Secret SYNTHETICACCOUNT456 on the TYP page"},
    ])
    desc = out[0]["description"]
    assert "Fixture9Secret" not in desc
    assert "[redacted]" in desc
    assert "qa.user@example.test" in desc
    assert "SYNTHETICACCOUNT456" in desc


def test_redaction_is_idempotent_across_repasses():
    once = finding_inference.normalise_findings_for_schema([
        {"severity": "medium", "page": 3,
         "description": "account qa.user@example.test Fixture9Secret"},
    ])
    twice = finding_inference.normalise_findings_for_schema(once)
    assert twice[0]["description"] == once[0]["description"]
    assert twice[0]["description"].count("[redacted]") == 1


def test_normalisation_leaves_clean_descriptions_untouched():
    findings = [{"severity": "medium", "page": 1,
                 "description": "checkout returned 500 on submit"}]
    out = finding_inference.normalise_findings_for_schema(findings)
    assert out[0]["description"] == "checkout returned 500 on submit"


def test_explicit_finding_title_is_preserved_and_redacted():
    [finding] = finding_inference.normalise_findings_for_schema([{
        "severity": "medium",
        "title": "Resume prompt missing; password: Fixture9Secret",
        "description": "The expected prompt is absent.",
    }])

    assert finding["title"] == "Resume prompt missing; password: [redacted]"


def test_punctuation_only_finding_title_falls_back_to_description():
    [finding] = finding_inference.normalise_findings_for_schema([{
        "severity": "medium",
        "title": "...",
        "description": "Playback controls are missing from the player.",
    }])

    assert finding["title"] == (
        "Playback controls are missing from the player"
    )


def test_repeated_description_title_is_replaced_with_issue_summary():
    description = (
        "A background notification says 'Your Premium membership has ended, "
        "you no longer have access to your Prime benefits' while the library "
        "still shows the Stone Sisters title. This is a background system "
        "notification, not part of the tested flow, so it's unlikely to be "
        "a real problem, but worth noting in case it affected later steps."
    )
    [finding] = finding_inference.normalise_findings_for_schema([{
        "severity": "low",
        "title": description.split(". ", maxsplit=1)[0],
        "description": description,
    }])

    assert finding["title"] == (
        "Unrelated Premium membership alert appears during Library verification"
    )
    assert finding["title"] not in finding["description"]


def test_background_notification_summary_is_not_product_specific():
    description = (
        "A background notification says 'Your subscription is paused' "
        "while checkout still shows the order summary. This is a background "
        "system notification, not part of the tested flow."
    )

    assert finding_inference.derive_finding_title({
        "title": description.split(". ", maxsplit=1)[0],
        "description": description,
    }) == "Unrelated subscription alert appears during Checkout verification"


def test_legacy_finding_title_skips_step_setup_without_clipping():
    title = finding_inference.derive_finding_title({
        "description": (
            "The step asks to play the audiobook in the Example Storefront app and "
            "verify the app shows a resume prompt. The resume prompt is not "
            "visible in the supplied evidence."
        ),
    })

    assert title == "The resume prompt is not visible in the supplied evidence"
    assert "The step asks" not in title
    assert "..." not in title


def test_low_findings_default_to_advisory_and_do_not_change_verdict():
    findings = finding_inference.normalise_findings_for_schema([{
        "severity": "low",
        "description": "The URL was not visible in the Mobile screenshot.",
    }])
    assert findings[0]["advisory"] is True
    assert finding_inference.derive_verdict(findings) == "pass"


def test_explicit_operational_low_changes_verdict():
    findings = finding_inference.normalise_findings_for_schema([{
        "severity": "low",
        "description": "Approved policy exception requires review.",
        "advisory": False,
    }])
    assert finding_inference.derive_verdict(findings) == "concerns"


def test_deterministic_low_defaults_to_operational():
    findings = finding_inference.normalise_findings_for_schema([{
        "severity": "low",
        "description": "A deterministic policy action remains.",
        "source": "rule",
    }])
    assert findings[0]["advisory"] is False
    assert finding_inference.derive_verdict(findings) == "concerns"


def test_order_diagnostics_and_claim_code_low_findings_remain_operational():
    for description in (
        "Order diagnostics reference does not match.",
        "The claim-code value differs from the approved reference.",
    ):
        [finding] = finding_inference.normalise_findings_for_schema([{
            "severity": "low",
            "description": description,
        }])
        assert finding["advisory"] is False
        assert finding_inference.derive_verdict([finding]) == "concerns"
