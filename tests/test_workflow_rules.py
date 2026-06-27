"""Unit tests for workflow_rules.py — deterministic, metadata-only rules."""
from __future__ import annotations

import workflow_rules as wr


def _meta(*, overall_status=None, overall_status_id=None,
          overall_comment="", overall_trace_links=None, steps=None):
    """Build a minimal metadata dict matching build_metadata's output shape.

    `overall_status_id` / per-step `status_id` are only needed by tests that
    exercise R0 (the meta-rule that detects unresolved status maps). R1–R6
    look only at `status` (resolved name) and the comment/traceLinks
    fields, so most tests can omit the _id fields.
    """
    return {
        "test_results": [{
            "status": overall_status,
            "status_id": overall_status_id,
            "comment": overall_comment,
            "trace_links": overall_trace_links or [],
            "steps": steps or [],
        }]
    }


# --- R0: meta — rule engine health (unresolved status names) ---------------


def test_r0_fires_when_top_level_status_unresolved():
    # status_id present but status (resolved name) None => map fetch failed.
    findings = wr.check_rule_engine_health(_meta(
        overall_status=None,
        overall_status_id=39,
        steps=[{"index": 1, "status": "Pass", "status_id": 39}],
    ))
    assert len(findings) == 1
    f = findings[0]
    # R0 severity: high (bumped 2026-05-23). Was medium pre-bump; the
    # rule-engine-failed signal must escalate verdict via merge_rule_findings
    # so operators don't miss audits where R1-R8 silently bypassed.
    assert f["severity"] == "high"
    assert f["rule"] == "R0"
    assert f["source"] == "rule"
    assert f["step_index"] is None  # meta rule, no step attribution
    assert "status-name map" in f["description"].lower() or \
           "status name" in f["description"].lower() or \
           "map did not resolve" in f["description"]


def test_r0_fires_when_step_status_unresolved():
    # Top resolved but a step's status didn't resolve => still a partial
    # rule-engine outage.
    findings = wr.check_rule_engine_health(_meta(
        overall_status="Pass",
        overall_status_id=39,
        steps=[
            {"index": 1, "status": "Pass", "status_id": 39},
            {"index": 2, "status": None, "status_id": 9999},  # unresolved
        ],
    ))
    assert len(findings) == 1
    assert findings[0]["rule"] == "R0"


def test_r0_silent_when_all_statuses_resolved():
    # Healthy case: every id resolved => R0 must not fire.
    assert wr.check_rule_engine_health(_meta(
        overall_status="Pass",
        overall_status_id=39,
        steps=[{"index": 1, "status": "Pass", "status_id": 39}],
    )) == []


def test_r0_silent_when_no_status_ids_at_all():
    # Metadata with no status_id anywhere => we can't tell if resolution
    # was attempted or unnecessary. Silent to avoid false positives on
    # fixtures / tests that don't carry status_id.
    assert wr.check_rule_engine_health(_meta(
        overall_status=None,
        steps=[{"index": 1, "status": None}],
    )) == []


def test_r0_silent_on_empty_metadata():
    assert wr.check_rule_engine_health({}) == []
    assert wr.check_rule_engine_health({"test_results": []}) == []


def test_r0_fires_only_once_per_execution():
    # Multiple unresolved steps must not produce multiple R0 findings —
    # the condition is global to the metadata.
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


# --- R1: step In Progress AND no comment -----------------------------------


def test_r1_fires_on_in_progress_step_without_comment():
    findings = wr.check_in_progress_steps(_meta(steps=[
        {"index": 1, "status": "Pass"},
        {"index": 2, "status": "In Progress"},      # no comment -> fires
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
    # Per policy, In Progress with an explanatory comment is acceptable.
    assert wr.check_in_progress_steps(_meta(steps=[
        {"index": 1, "status": "In Progress",
         "comment": "Waiting on upstream data fix PROD-123"},
    ])) == []


def test_r1_treats_whitespace_only_comment_as_no_comment():
    # Whitespace doesn't count as documentation.
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


# --- R2: overall Pass with a Fail step ---------------------------------------


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
    # Overall Fail + step Fail: that's consistent, not a rule violation.
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
    # Status map didn't resolve the id — don't misfire, just skip.
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


# --- R3: DEPRECATED — function still callable in isolation -----------------
# Per policy, R3's Pass+Blocked-step reading is wrong: a Blocked step
# that is not a real blocker is acceptable with overall Pass. The
# function is kept in the module for history, but MUST NOT be in the
# aggregator. These tests lock both guarantees in.


def test_r3_function_still_computes_the_deprecated_reading():
    # Pure-function behaviour unchanged so historical audits can be
    # re-analysed if needed.
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
    # The aggregator must skip R3 so production audits don't pick it up.
    assert wr.check_blocked_step_pass_overall not in wr._RULES


def test_r3_does_not_fire_via_run_workflow_rules():
    # End-to-end: a Pass+Blocked-step metadata should produce no rule
    # findings (R3 absent from the aggregator, no other rule trips).
    findings = wr.run_workflow_rules(_meta(
        overall_status="Pass",
        steps=[
            {"index": 1, "status": "Pass"},
            {"index": 2, "status": "Blocked"},
        ],
    ))
    rules_hit = [f["rule"] for f in findings]
    assert "R3" not in rules_hit
    # No other rule should fire on this clean-per-policy input.
    assert rules_hit == []


# --- R4: overall Fail but no Fail/Blocked step ------------------------------


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
    assert f["step_index"] is None  # structural rule, no step attribution


def test_r4_silent_when_any_step_is_fail():
    # Overall Fail + step Fail: consistent, no rule violation.
    assert wr.check_fail_overall_without_failing_step(_meta(
        overall_status="Fail",
        steps=[
            {"index": 1, "status": "Pass"},
            {"index": 2, "status": "Fail"},
        ],
    )) == []


def test_r4_silent_when_any_step_is_blocked():
    # A Blocked step also justifies an overall Fail.
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
    # Test with 0 steps can't trip the rule (no arithmetic to check).
    assert wr.check_fail_overall_without_failing_step(_meta(
        overall_status="Fail",
        steps=[],
    )) == []


def test_r4_silent_when_any_step_unresolved():
    # R0 already flags status-map failure; R4 should NOT fire on the
    # same data because a None step status can't be judged. Prevents
    # a double-fire between R0 and R4 on degraded metadata.
    assert wr.check_fail_overall_without_failing_step(_meta(
        overall_status="Fail",
        steps=[
            {"index": 1, "status": "Pass"},
            {"index": 2, "status": None},  # unresolved
        ],
    )) == []


# --- R5: Pass step whose comment admits an issue -----------------------------


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


def test_r5_silent_when_comment_has_no_keywords():
    assert wr.check_pass_step_with_issue_comment(_meta(steps=[
        {"index": 1, "status": "Pass",
         "comment": "Worked fine, no problems observed."},
    ])) == []


def test_r5_silent_when_step_not_pass():
    # Fail step with issue comment -> consistent, not a rule violation.
    assert wr.check_pass_step_with_issue_comment(_meta(steps=[
        {"index": 1, "status": "Fail",
         "comment": "Crashed on click."},
    ])) == []


def test_r5_silent_on_empty_comment():
    assert wr.check_pass_step_with_issue_comment(_meta(steps=[
        {"index": 1, "status": "Pass", "comment": ""},
    ])) == []
    assert wr.check_pass_step_with_issue_comment(_meta(steps=[
        {"index": 1, "status": "Pass"},  # no comment field
    ])) == []


# --- R6: Passed With Issue overall with no evidence -------------------------


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
    # Credit for step-level documentation so the rule doesn't fire when
    # the tester wrote the detail on the offending step but left the
    # overall comment empty.
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
    # A comment of only spaces/newlines is not documentation.
    findings = wr.check_passed_with_issue_has_evidence(_meta(
        overall_status="Passed With Issue",
        overall_comment="   \n  ",
        steps=[{"index": 1, "status": "Pass", "comment": "  "}],
    ))
    assert len(findings) == 1
    assert findings[0]["rule"] == "R6"


# --- R16: Fail/Blocked overall requires documentation ---------------------


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
    """If overall is Fail with no comment but a step has documentation,
    we count that as 'documented somewhere' — same exemption R6 uses."""
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


# --- R7: Passed With Issue overall requires a Fail step --------------------


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
    # PWI is the correct downgrade for a non-core-blocker Fail.
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
    # Can't judge an empty steps list; R0 handles that case.
    assert wr.check_passed_with_issue_requires_fail_step(_meta(
        overall_status="Passed With Issue",
        steps=[],
    )) == []


def test_r7_silent_when_any_step_unresolved():
    # Don't fire alongside R0 when status map is incomplete.
    assert wr.check_passed_with_issue_requires_fail_step(_meta(
        overall_status="Passed With Issue",
        steps=[
            {"index": 1, "status": "Pass"},
            {"index": 2, "status": None},
        ],
    )) == []


# --- R8: Blocked overall requires a Blocked step ---------------------------


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
            {"index": 2, "status": "Blocked"},  # even with a Blocked step, None disqualifies
        ],
    )) == []


# --- Aggregator --------------------------------------------------------------


def test_run_workflow_rules_aggregates_across_rules():
    # Execution that trips every currently-registered rule so we can
    # assert both ordering and the severity map in one place. R3 is
    # deprecated and absent — test_r3_does_not_fire_via_run_workflow_rules
    # covers the corresponding negative assertion.
    meta = _meta(
        overall_status="Pass",
        overall_status_id=39,
        steps=[
            # R0: step with unresolved status (status_id present, status None)
            {"index": 0, "status": None, "status_id": 9999},
            # R1: step still In Progress with no comment
            {"index": 1, "status": "In Progress"},
            # R2: overall Pass but step Fail
            {"index": 2, "status": "Fail"},
            # (A Blocked step here would formerly trip R3; now silent.)
            {"index": 3, "status": "Blocked"},
            # R5: Pass step with issue-keyword in comment
            {"index": 4, "status": "Pass", "comment": "hit an error"},
            # Clean step — shouldn't trip anything
            {"index": 5, "status": "Pass"},
        ],
    )
    findings = wr.run_workflow_rules(meta)
    # Ordering per updated aggregator: R0 -> R1 -> R2 -> R4 -> R5 -> R6
    # -> R7 -> R8 -> R11 -> R10. R4/R6/R7/R8 don't fire (overall=Pass).
    # R11 fires because the step-2 Fail has no trace_links and no
    # defect ref in its (empty) comment — undocumented failure.
    # R3 must not appear (deprecated).
    rules_hit = [f["rule"] for f in findings]
    assert rules_hit == ["R0", "R1", "R2", "R5", "R11"]
    assert "R3" not in rules_hit
    assert all(f["source"] == "rule" for f in findings)
    sev_by_rule = {f["rule"]: f["severity"] for f in findings}
    assert sev_by_rule == {
        # R0 is "high" since 2026-05-23 (rule-engine-failed must
        # escalate verdict; was "medium" pre-bump).
        "R0": "high",
        "R1": "medium",
        "R2": "high",
        "R5": "medium",
        "R11": "medium",
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
    # R4 fires because Fail overall has no failing/blocked step.
    # R16 also fires because the test fixture leaves overall_comment
    # empty and provides no traceLinks anywhere — that's exactly the
    # "Fail/Blocked without documentation" gap R16 surfaces. Both
    # findings are independent and both are correct here.
    assert [f["rule"] for f in findings] == ["R4", "R16"]
    rule_to_sev = {f["rule"]: f["severity"] for f in findings}
    assert rule_to_sev == {"R4": "high", "R16": "high"}


def test_run_workflow_rules_fires_r6_on_unsupported_passed_with_issue():
    # R6 and R7 both require PWI overall, but they describe different
    # failures: R6 = no evidence (comment/traceLinks); R7 = no Fail step.
    # This execution has a Fail step (so R7 silent) but no comment AND
    # no trace links, so R6 fires.
    # R11 ALSO fires on the same fixture because the Fail step has no
    # defect ref of its own — R6 and R11 are in genuine tension here
    # (R6: PWI verdict unsupported by docs; R11: this specific Fail
    # step is undocumented). Both are correct; the test asserts both.
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
    assert rules == ["R6", "R11"]
    sev = {f["rule"]: f["severity"] for f in findings}
    assert sev == {"R6": "medium", "R11": "medium"}


def test_run_workflow_rules_fires_r7_on_pwi_without_fail_step():
    # PWI overall + Blocked step but no Fail step: policy says PWI is
    # only correct for Fail steps. R6 silent because we add a comment;
    # R7 fires alone.
    meta = _meta(
        overall_status="Passed With Issue",
        overall_comment="some context",
        steps=[
            {"index": 1, "status": "Pass"},
            {"index": 2, "status": "Blocked"},
        ],
    )
    findings = wr.run_workflow_rules(meta)
    assert [f["rule"] for f in findings] == ["R7"]
    assert findings[0]["severity"] == "medium"


def test_run_workflow_rules_fires_r8_on_blocked_overall_without_blocked_step():
    # The Fail step carries a trace_link so R11 stays silent here and
    # we isolate R8 in this assertion (R11 coverage lives in its
    # own dedicated tests).
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
        steps=[
            {"index": 1, "status": "Pass", "status_id": 39},
            {"index": 2, "status": "Pass", "status_id": 39},
        ],
    )
    assert wr.run_workflow_rules(meta) == []


def test_run_workflow_rules_accepts_pass_with_nonblocking_blocked_step():
    # Policy: Pass overall + Blocked step that is not a real blocker is
    # ACCEPTABLE. R3 used to fire on this; it must not anymore. Other
    # rules (R1/R2/R4/R5/R6/R7/R8) must not be triggered either.
    meta = _meta(
        overall_status="Pass",
        overall_status_id=39,
        steps=[
            {"index": 1, "status": "Pass", "status_id": 39},
            {"index": 2, "status": "Blocked", "status_id": 41},
        ],
    )
    assert wr.run_workflow_rules(meta) == []


def test_run_workflow_rules_safe_on_empty_metadata():
    assert wr.run_workflow_rules({}) == []
    assert wr.run_workflow_rules({"test_results": []}) == []


# --- R10: defect ref pasted in comment text instead of trace_links ---------


def test_r10_fires_on_company_ticket_url_in_comment():
    findings = wr.check_defect_refs_in_comment_text(_meta(
        overall_status="Pass",
        steps=[
            {"index": 1, "status": "Pass",
             "comment": "see https://issues.example.com/D390576007 for ticket"},
        ],
    ))
    assert len(findings) == 1
    f = findings[0]
    assert f["rule"] == "R10"
    assert f["severity"] == "low"
    assert f["step_index"] == 1
    assert "trace_link" in f["description"]


def test_r10_fires_on_tracker_key_in_comment():
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
    # Tester DID attach via trace_links in addition to mentioning in
    # comment — they did the right thing, R10 must not fire.
    findings = wr.check_defect_refs_in_comment_text(_meta(
        steps=[
            {"index": 1, "status": "Pass",
             "comment": "see QA-BUG-12345",
             "trace_links": ["QA-BUG-12345"]},
        ],
    ))
    assert findings == []


def test_r10_silent_on_irrelevant_key_prefixes():
    # Match only the configured defect-key prefixes (QA-/EXAMPLEAPP-/
    # OFFERS-). Other keys mentioned in comments shouldn't fire R10.
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


# --- R11: Fail step requires defect ref (trace_link OR comment ref) --------


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


def test_r11_silent_when_comment_has_company_ticket_url():
    """Form (b): defect URL in comment — acceptable, R11 silent. R10
    will still nudge to also attach via trace_links."""
    findings = wr.check_fail_step_requires_defect_ref(_meta(
        steps=[
            {"index": 2, "status": "Fail",
             "comment": "see https://issues.example.com/D390576007"},
        ],
    ))
    assert findings == []


def test_r11_silent_when_comment_has_tracker_key():
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
             "trace_links": ["QA-BUG-1"]},  # documented, silent
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


# --- R11 — Passed With Issue extension --------------------------------------


def test_r11_fires_on_pwi_step_with_no_defect_ref():
    """A Passed-With-Issue step admits something went wrong; without
    docs that admission is unsupported (and could be hiding a real
    failure mis-classified as PWI to bypass R11)."""
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
    """Plain Pass admits no issue — R11 must not fire even when
    trace_links and comment are both empty. Only Fail and PWI trigger."""
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
    assert indices == [1, 3]  # Fail + PWI; Pass + Blocked silent
