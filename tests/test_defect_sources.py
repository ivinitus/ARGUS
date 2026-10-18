"""Defect tagging across Zephyr Issues, Comments, and Weblinks."""
from copy import deepcopy
from unittest.mock import Mock

import pytest
import requests

from argus import config
from argus.domain import workflow_rules as rules
from argus.pipeline import extractor, orchestrator

EXTERNAL_ISSUE = "https://issues.example.net/browse/EXTERNAL-123"
STATUS_MAP = {39: "Pass", 40: "Fail", 834: "Passed With Issue"}


@pytest.fixture(autouse=True)
def tv_policy():
    settings = config.load(profile="tv")
    settings.policy.external_defect_key_prefixes = ["EXTERNAL-"]
    settings.policy.defect_url_hosts = ["issues.example.net", "jira.example.com"]
    rules.configure(settings.policy)
    yield
    rules.reset_configuration()


def execution():
    return {
        "id": 100, "key": "QA-E100",
        "executionDate": "2026-09-11T00:00:00Z",
        "testResultStatusId": 40,
        "testCase": {"id": 20, "key": "QA-T20"},
        "testScriptResults": [{"index": 0, "testResultStatusId": 40}],
    }


def response(payload, status=200):
    result = Mock()
    result.status_code = status
    result.json.return_value = payload
    result.headers = {}
    return result


@pytest.mark.parametrize("scope", ["execution", "step"])
@pytest.mark.parametrize("fields", [
    {"traceLinks": [{"id": 700, "issueId": 800}]},
    {"traceLinks": [{"id": 700, "issue": {"key": "BUG-123"}}]},
    {"comment": EXTERNAL_ISSUE},
    {"comment": f'<p>Known issue: <a href="{EXTERNAL_ISSUE}">details</a></p>'},
    {"comment": "Tracked in EXTERNAL-123"},
    {"webLinks": [{"id": 700, "url": EXTERNAL_ISSUE, "urlDescription": "Playback bug"}]},
    {"web_links": EXTERNAL_ISSUE},
    {"traceLinks": [{"id": 700, "url": EXTERNAL_ISSUE, "urlDescription": "Playback bug"}]},
    {"traceLinks": [{"id": 700, "webLink": {"url": EXTERNAL_ISSUE}}]},
    {"traceLinks": [{"id": 700, "webLink": f" {EXTERNAL_ISSUE} "}]},
    {"webLinks": [{"webLink": EXTERNAL_ISSUE}]},
    {"webLinks": [f" {EXTERNAL_ISSUE} "]},
    {"traceLinks": [{"urlDescription": EXTERNAL_ISSUE}]},
    {"traceLinks": [{"urlDescription": "EXTERNAL-123"}]},
])
def test_failed_case_accepts_defect_in_any_supported_location(scope, fields):
    raw = execution()
    destination = raw if scope == "execution" else raw["testScriptResults"][0]
    destination.update(fields)
    metadata = extractor.build_metadata([raw], status_map=STATUS_MAP)
    assert rules.check_fail_step_requires_defect_ref(metadata) == []
    assert rules.check_defect_refs_in_comment_text(metadata) == []
    assert rules.check_fail_or_blocked_has_documentation(metadata) == []


def test_issue_id_is_not_confused_with_the_trace_link_record_id():
    raw = execution()
    raw["traceLinks"] = [{"id": 700, "issueId": 800}]
    assert extractor.build_metadata([raw])["test_results"][0]["trace_links"] == ["800"]


@pytest.mark.parametrize("url", [
    "https://wiki.example.test/how-to-test",
    "https://issues.example.net/",
    "https://issues.example.net/browse/",
    "https://issues.example.net.evil.test/browse/OTHER-123",
    "https://evil.test/issues.example.net/browse/OTHER-123",
    "https://issues.example.net@evil.test/browse/OTHER-123",
])
def test_non_defect_web_links_do_not_satisfy_failed_case_rule(url):
    raw = execution()
    raw["traceLinks"] = [{"id": 700, "url": url, "urlDescription": "Reference"}]
    metadata = extractor.build_metadata([raw], status_map=STATUS_MAP)
    assert metadata["test_results"][0]["trace_links"] == []
    assert len(rules.check_fail_step_requires_defect_ref(metadata)) == 1


@pytest.mark.parametrize("field", ["webLinks", "traceLinks"])
def test_web_link_description_does_not_replace_a_non_defect_url(field):
    raw = execution()
    url = "https://wiki.example.test/how-to-test"
    raw[field] = [{"url": url, "urlDescription": "Example EXTERNAL-123"}]
    metadata = extractor.build_metadata([raw], status_map=STATUS_MAP)
    assert metadata["test_results"][0]["weblinks"] == [url]
    assert len(rules.check_fail_step_requires_defect_ref(metadata)) == 1


@pytest.mark.parametrize("value", ["", "   ", "https://wiki.example.test/how-to-test"])
def test_plain_string_web_link_still_requires_an_actual_defect(value):
    raw = execution()
    raw["traceLinks"] = [{"webLink": value}]
    metadata = extractor.build_metadata([raw], status_map=STATUS_MAP)
    assert len(rules.check_fail_step_requires_defect_ref(metadata)) == 1


def test_empty_web_links_are_omitted_and_strings_are_normalized():
    raw = execution()
    raw["webLinks"] = ["", "  ", f" {EXTERNAL_ISSUE} ", EXTERNAL_ISSUE]
    metadata = extractor.build_metadata([raw], status_map=STATUS_MAP)
    assert metadata["test_results"][0]["weblinks"] == [EXTERNAL_ISSUE]


def test_step_reference_does_not_hide_another_steps_missing_defect():
    raw = execution()
    raw["testScriptResults"][0]["webLinks"] = [EXTERNAL_ISSUE]
    raw["testScriptResults"].append({"index": 1, "testResultStatusId": 40})
    findings = rules.check_fail_step_requires_defect_ref(
        extractor.build_metadata([raw], status_map=STATUS_MAP)
    )
    assert [finding["step_index"] for finding in findings] == [1]


def test_external_issue_url_never_requires_storefront_issues_attachment():
    raw = execution()
    raw["testScriptResults"][0]["comment"] = (
        "https://issues.example.net/browse/QA-BUG-123"
    )
    metadata = extractor.build_metadata([raw], status_map=STATUS_MAP)
    assert rules.check_defect_refs_in_comment_text(metadata) == []
    # An actual Example Storefront Jira reference retains the existing Issues policy.
    metadata["test_results"][0]["steps"][0]["comment"] = "QA-BUG-123"
    assert len(rules.check_defect_refs_in_comment_text(metadata)) == 1


def test_external_key_configuration_does_not_leak_between_teams():
    metadata = extractor.build_metadata([execution()], status_map=STATUS_MAP)
    metadata["test_results"][0]["comment"] = "EXTERNAL-123"
    assert rules.check_fail_step_requires_defect_ref(metadata) == []
    rules.configure(config.load(profile="web").policy)
    assert len(rules.check_fail_step_requires_defect_ref(metadata)) == 1


@pytest.mark.parametrize("profile", [
    "web", "desktop", "tablet", "mobile", "reader", "portal",
])
@pytest.mark.parametrize("host", ["issues.example.net", "jira.example.com"])
def test_issue_url_acceptance_is_scoped_to_configured_teams(profile, host):
    metadata = extractor.build_metadata([execution()], status_map=STATUS_MAP)
    metadata["test_results"][0]["weblinks"] = [f"https://{host}/browse/OTHER-123"]
    assert rules.check_fail_step_requires_defect_ref(metadata) == []
    rules.configure(config.load(profile=profile).policy)
    assert len(rules.check_fail_step_requires_defect_ref(metadata)) == 1
    # Existing configured Jira key prefixes remain accepted.
    metadata["test_results"][0]["weblinks"] = [
        "https://jira.example.com/browse/BUG-123",
    ]
    assert rules.check_fail_step_requires_defect_ref(metadata) == []


def test_issue_url_configuration_resets_to_default():
    assert rules._contains_defect_reference(
        "HTTPS://ISSUES.EXAMPLE.NET/browse/OTHER-123"
    )
    rules.reset_configuration()
    assert not rules._contains_defect_reference(
        "https://issues.example.net/browse/OTHER-123"
    )


def test_labcollab_comment_attachment_exemption_is_team_scoped():
    raw = execution()
    raw["testScriptResults"][0]["comment"] = (
        "https://issues.example.net/browse/BUG-123"
    )
    metadata = extractor.build_metadata([raw], status_map=STATUS_MAP)
    assert rules.check_defect_refs_in_comment_text(metadata) == []
    rules.configure(config.load(profile="web").policy)
    assert len(rules.check_defect_refs_in_comment_text(metadata)) == 1


def test_case_weblinks_support_fail_and_pwi_without_more_calls_for_repeated_cases():
    failed = execution()
    pwi = execution()
    pwi["id"] = 101
    pwi["testResultStatusId"] = pwi["testScriptResults"][0]["testResultStatusId"] = 834
    session = Mock()
    session.get.return_value = response([
        {"id": 700, "url": EXTERNAL_ISSUE, "urlDescription": "Known playback issue"},
    ])
    extractor.enrich_test_case_weblinks(
        session, "https://jira.example", [failed, pwi],
        extractor.issue_status_ids(STATUS_MAP),
    )
    session.get.assert_called_once()
    assert session.get.call_args.args[0].endswith("/testcase/20/tracelinks/weblink")
    for raw in (failed, pwi):
        metadata = extractor.build_metadata([raw], status_map=STATUS_MAP)
        assert metadata["test_results"][0]["test_case_weblinks_checked"] is True
        assert rules.check_fail_step_requires_defect_ref(metadata) == []
        assert rules.check_passed_with_issue_has_evidence(metadata) == []
        assert rules.check_fail_or_blocked_has_documentation(metadata) == []


def test_weblink_lookup_is_limited_to_affected_cases_including_failed_steps():
    passed = execution()
    passed["testResultStatusId"] = passed["testScriptResults"][0]["testResultStatusId"] = 39
    failed_step = deepcopy(passed)
    failed_step["testScriptResults"][0]["testResultStatusId"] = 40
    session = Mock()
    session.get.return_value = response([])
    extractor.enrich_test_case_weblinks(
        session, "https://jira.example", [passed, failed_step], {40, 834},
    )
    session.get.assert_called_once()
    assert "test_case_weblinks_checked" not in passed
    assert failed_step["test_case_weblinks_checked"] is True


def test_weblink_lookup_failure_is_not_treated_as_no_tagged_bug():
    session = Mock()
    session.get.side_effect = requests.ConnectionError("offline")
    with pytest.raises(requests.ConnectionError):
        # Bypass retry waits; retry behavior is covered separately.
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(extractor, "_get_with_retry", session.get)
            extractor.enrich_test_case_weblinks(
                session, "https://jira.example", [execution()], {40},
            )


def test_web_links_invalidation_matches_raw_and_normalized_metadata():
    raw = execution()
    old_fingerprint = extractor.build_evidence_fingerprint([raw])
    raw["comment"] = f'<a href="{EXTERNAL_ISSUE}"><b>Bug details</b></a>'
    raw["webLinks"] = [{"url": EXTERNAL_ISSUE}]
    raw["testScriptResults"][0]["webLinks"] = [{"url": EXTERNAL_ISSUE}]
    raw["test_case_weblinks"] = [EXTERNAL_ISSUE]
    raw["test_case_weblinks_checked"] = True
    metadata = extractor.build_metadata([raw], status_map=STATUS_MAP)
    fingerprint = extractor.build_evidence_fingerprint([raw])
    assert fingerprint != old_fingerprint
    assert fingerprint == extractor.build_evidence_fingerprint(metadata["test_results"])
    for scope, field in [
        ("execution", "webLinks"), ("step", "webLinks"),
        ("execution", "test_case_weblinks"), ("execution", "comment"),
    ]:
        changed = deepcopy(raw)
        target = changed if scope == "execution" else changed["testScriptResults"][0]
        del target[field]
        assert extractor.build_evidence_fingerprint([changed]) != fingerprint


def test_summary_refreshes_old_tv_findings_and_tracks_test_case_link_edits():
    raw = execution()
    cached = orchestrator._AuditState(
        execution_date=raw["executionDate"],
        evidence_fingerprint=extractor.build_evidence_fingerprint([raw]),
    )
    session = Mock()

    def summary(links):
        session.get.side_effect = [response(deepcopy(raw)), response(links)]
        return extractor.get_testresult_summary(
            session, "https://jira.example", 100, test_case_weblink_status_ids={40, 834},
        )

    initial = summary([])
    assert not orchestrator._cached_audit_matches_live(cached, initial)
    cached.evidence_fingerprint = extractor.legacy_evidence_fingerprint([raw])
    assert not orchestrator._cached_audit_matches_live(cached, initial)
    cached.evidence_fingerprint = initial["evidence_fingerprint"]
    assert orchestrator._cached_audit_matches_live(cached, summary([]))
    linked = summary([{"url": EXTERNAL_ISSUE}])
    assert not orchestrator._cached_audit_matches_live(cached, linked)
    cached.evidence_fingerprint = linked["evidence_fingerprint"]
    assert orchestrator._cached_audit_matches_live(cached, summary([{"url": EXTERNAL_ISSUE}]))
    assert not orchestrator._cached_audit_matches_live(cached, summary([]))


def test_html_comment_links_survive_without_duplicating_visible_urls():
    assert extractor._strip_html(
        f'<a href="{EXTERNAL_ISSUE}">Bug</a>', preserve_links=True,
    ) == f"Bug ({EXTERNAL_ISSUE})"
    assert extractor._strip_html(
        f'<a href="{EXTERNAL_ISSUE}">{EXTERNAL_ISSUE}</a>', preserve_links=True,
    ) == EXTERNAL_ISSUE
    assert extractor._strip_html(
        '<a href="javascript:alert(1)">Bug</a>', preserve_links=True,
    ) == "Bug"
