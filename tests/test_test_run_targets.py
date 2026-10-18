from __future__ import annotations

from types import SimpleNamespace

import pytest

from argus.operations import test_run_targets
from argus.operations.teams import resolve_jira_team


@pytest.fixture(autouse=True)
def configured_project_routing(monkeypatch):
    from argus import config
    original = config.load

    def load(*args, **kwargs):
        settings = original(*args, **kwargs)
        profile = settings.general.profile
        settings.extractor.project_id = {"mobile": "10001", "portal": "10002"}.get(profile, "10000")
        settings.extractor.additional_project_ids = []
        return settings

    monkeypatch.setattr(config, "load", load)


@pytest.mark.parametrize("prefix", ["", "v2/"])
def test_parse_jira_reference_accepts_run_and_execution_forms(prefix):
    run_url = (
        "https://jira.example.com/secure/Tests.jspa"
        f"#/{prefix}testPlayer/QA-C21462"
    )
    execution_url = (
        "https://jira.example.com/secure/Tests.jspa"
        f"#/{prefix}testPlayer/testExecution/QA-E123"
    )

    assert test_run_targets.parse_jira_reference(
        "qa-c21462"
    ) == test_run_targets.ParsedJiraReference("run", "QA-C21462")
    assert test_run_targets.parse_jira_reference(
        run_url
    ) == test_run_targets.ParsedJiraReference("run", "QA-C21462")
    assert test_run_targets.parse_jira_reference(
        f"<{execution_url}|Open execution>"
    ) == test_run_targets.ParsedJiraReference("execution", "QA-E123")


@pytest.mark.parametrize(
    "value",
    [
        (
            "https://evil.example/secure/Tests.jspa"
            "#/testPlayer/QA-C21462"
        ),
        (
            "http://jira.example.com/secure/Tests.jspa"
            "#/testPlayer/QA-C21462"
        ),
        (
            "https://jira.example.com/secure/Tests.jspa"
            "#/testPlayer/testExecution/QA-C1"
        ),
        (
            "https://jira.example.com/secure/Tests.jspa"
            "#/testPlayer/QA-E1"
        ),
    ],
)
def test_parse_jira_reference_rejects_untrusted_or_mismatched_urls(value):
    with pytest.raises(test_run_targets.JiraReferenceError):
        test_run_targets.parse_jira_reference(value)


def test_parse_jira_reference_leaves_plain_folder_names_alone():
    assert test_run_targets.parse_jira_reference(
        "Web regression September"
    ) is None


@pytest.mark.parametrize("value", [
    "MOBILE-T40812", "mobile-t40812 ",
    "https://jira.example.com/secure/Tests.jspa#/testCase/MOBILE-T40812",
    "https://jira.example.com/secure/Tests.jspa#/v2/testCase/MOBILE-T40812",
])
def test_case_keys_and_links_are_not_folders(value):
    assert test_run_targets.parse_manual_targets(value) == [("case", "MOBILE-T40812")]


def test_case_and_execution_comma_batch_preserves_every_unique_target():
    assert test_run_targets.parse_manual_targets(
        "MOBILE-T40812, mobile-t40813, MOBILE-E1924728, MOBILE-T40812",
    ) == [("case", "MOBILE-T40812"), ("case", "MOBILE-T40813"), ("key", "MOBILE-E1924728")]


@pytest.mark.parametrize("value", [
    "mobile-p3",
    "https://jira.example.com/secure/Tests.jspa#/testPlan/MOBILE-P3",
    "https://jira.example.com/secure/Tests.jspa#/v2/testPlan/MOBILE-P3",
    "<https://jira.example.com/secure/Tests.jspa#/testPlan/MOBILE-P3|Plan>",
])
def test_parse_test_plan_keys_and_urls(value):
    assert test_run_targets.parse_jira_reference(value) == (
        test_run_targets.ParsedJiraReference("plan", "MOBILE-P3")
    )


@pytest.mark.parametrize("value", [
    "https://evil.example/secure/Tests.jspa#/testPlan/MOBILE-P3",
    "https://jira.example.com/secure/Tests.jspa#/testPlayer/MOBILE-P3",
    "https://jira.example.com/secure/Tests.jspa#/testPlan/MOBILE-C3",
])
def test_parse_test_plan_rejects_untrusted_or_mismatched_urls(value):
    with pytest.raises(test_run_targets.JiraReferenceError):
        test_run_targets.parse_jira_reference(value)


def test_plan_resolver_uses_plan_folder_tree(monkeypatch):
    from unittest.mock import Mock
    from argus.pipeline import extractor, test_plans

    monkeypatch.setattr(extractor, "build_session", lambda _: object())
    monkeypatch.setattr(test_plans, "find_plan_by_key", lambda *args: {
        "id": 4, "key": "MOBILE-P3", "projectId": 10001,
        "folderId": 10237, "name": "App release",
    })
    folder = Mock(return_value="Android plans")
    monkeypatch.setattr(extractor, "find_folder_name_by_id", folder)
    target = test_run_targets.JiraAuditTargetResolver(
        SimpleNamespace(base_url="https://jira.example.com")
    ).resolve("MOBILE-P3")
    assert target.audit_kind == "plan"
    assert target.audit_value == "MOBILE-P3"
    assert target.team.slug == "mobile"
    assert folder.call_args.kwargs == {"folder_type": "testplan"}


def test_split_manual_references_supports_commas_and_bounds():
    assert test_run_targets.split_manual_references(
        "QA-C1, QA-E2"
    ) == ["QA-C1", "QA-E2"]
    with pytest.raises(test_run_targets.JiraReferenceError, match="empty"):
        test_run_targets.split_manual_references("QA-C1,")
    with pytest.raises(test_run_targets.JiraReferenceError, match="at most"):
        test_run_targets.split_manual_references(
            ",".join(f"QA-C{i}" for i in range(21))
        )


def test_parse_manual_targets_normalizes_and_deduplicates_mixed_references():
    assert test_run_targets.parse_manual_targets(
        "qa-c1, QA-E2, Regression Sept 11, MOBILE-P367, "
        "https://jira.example.com/secure/Tests.jspa#/testPlayer/QA-C1"
    ) == [
        ("run", "QA-C1"), ("key", "QA-E2"),
        ("folder", "Regression Sept 11"), ("plan", "MOBILE-P367"),
    ]


@pytest.mark.parametrize("value", [
    "", "QA-C1,", ",QA-C1", "QA-C1, ,QA-C2",
    "QA-C1, bad;command", "QA-C1, " + "x" * 129,
    ",".join(["QA-C1"] * 21),
    "QA-C1, https://evil.example/secure/Tests.jspa#/testPlayer/QA-C2",
])
def test_parse_manual_targets_rejects_invalid_batch_before_dispatch(value):
    with pytest.raises(test_run_targets.JiraReferenceError):
        test_run_targets.parse_manual_targets(value)


@pytest.mark.parametrize(
    ("project_id", "folder_name", "team_slug"),
    [
        ("10000", "Web regression September", "web"),
        ("10000", "Desktop regression September", "desktop"),
        ("10000", "Tablet regression September", "tablet"),
        ("10000", "Reader regression September", "reader"),
        ("10000", "TV regression September", "tv"),
        ("10001", "Unconventional app folder", "mobile"),
        ("10002", "Unconventional PORTAL folder", "portal"),
    ],
)
def test_resolve_jira_team_uses_project_and_folder(
    project_id,
    folder_name,
    team_slug,
):
    team = resolve_jira_team(project_id, folder_name)
    assert team is not None
    assert team.slug == team_slug


def test_resolve_jira_team_fails_closed_for_unknown_shared_project_folder():
    assert resolve_jira_team("10000", "Mystery regression") is None


def _install_extractor_stub(monkeypatch):
    import argus.pipeline.extractor as extractor

    session = object()
    monkeypatch.setattr(extractor, "build_session", lambda unused: session)
    monkeypatch.setattr(
        extractor,
        "find_test_run_by_key",
        lambda actual_session, base_url, key: {
            "id": 39111,
            "key": key,
            "name": "Webview E2E -US",
            "projectId": 10000,
            "folderId": 18885,
        },
    )
    monkeypatch.setattr(
        extractor,
        "find_test_result_context",
        lambda actual_session, base_url, key: {
            "id": 99,
            "key": key,
            "testRun": {
                "id": 39111,
                "key": "QA-C21462",
                "name": "Webview E2E -US",
                "projectId": 10000,
                "folderId": 18885,
            },
        },
    )
    monkeypatch.setattr(
        extractor,
        "find_folder_name_by_id",
        lambda actual_session, base_url, project_id, folder_id: (
            "Web regression September"
        ),
    )


def test_target_resolver_maps_run_to_exact_run_and_parent_folder(monkeypatch):
    _install_extractor_stub(monkeypatch)
    settings = SimpleNamespace(base_url="https://jira.example.com")

    target = test_run_targets.JiraAuditTargetResolver(settings).resolve(
        "QA-C21462"
    )

    assert target.reference_kind == "run"
    assert target.audit_kind == "run"
    assert target.audit_value == "QA-C21462"
    assert target.folder_name == "Web regression September"
    assert target.team.slug == "web"


def test_target_resolver_maps_execution_to_single_key(monkeypatch):
    _install_extractor_stub(monkeypatch)
    settings = SimpleNamespace(base_url="https://jira.example.com")

    target = test_run_targets.JiraAuditTargetResolver(settings).resolve(
        "QA-E123"
    )

    assert target.reference_kind == "execution"
    assert target.audit_kind == "key"
    assert target.audit_value == "QA-E123"
    assert target.folder_name == "Web regression September"
    assert target.team.slug == "web"
