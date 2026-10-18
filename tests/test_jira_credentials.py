"""Portable Jira credentials fail closed and do not cross redirect origins."""
from __future__ import annotations

import pytest
import requests

from argus import config
from argus.pipeline import extractor, jira_credentials


@pytest.fixture(autouse=True)
def isolated_credentials(tmp_path, monkeypatch):
    extractor._credential_provider = None
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ARGUS_JIRA_PAT", raising=False)
    yield
    extractor._credential_provider = None


def test_file_provider_uses_private_token_file(tmp_path):
    token = tmp_path / "jira.pat"
    token.write_text("  example-token  \n")
    token.chmod(0o600)
    assert jira_credentials.LocalCredentialProvider("ARGUS_JIRA_PAT", token).get_pat() == "example-token"


def test_environment_wins_over_token_file(tmp_path, monkeypatch):
    token = tmp_path / "jira.pat"
    token.write_text("file-token")
    token.chmod(0o600)
    monkeypatch.setenv("ARGUS_JIRA_PAT", "environment-token")
    assert jira_credentials.LocalCredentialProvider("ARGUS_JIRA_PAT", token).get_pat() == "environment-token"


def test_dotenv_does_not_override_explicit_environment(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("ARGUS_JIRA_PAT=file-token\n")
    monkeypatch.setenv("ARGUS_JIRA_PAT", "environment-token")
    assert extractor._load_jira_pat() == "environment-token"


def test_public_token_file_is_rejected(tmp_path):
    token = tmp_path / "jira.pat"
    token.write_text("example-token")
    token.chmod(0o644)
    assert jira_credentials.LocalCredentialProvider("ARGUS_JIRA_PAT", token).get_pat() is None


def test_missing_token_fails_before_request():
    with pytest.raises(jira_credentials.JiraCredentialError, match="No Jira personal access token"):
        extractor.build_session()


def test_configured_environment_source_builds_bearer_session(tmp_path, monkeypatch):
    settings = config.ArgusConfig()
    settings.extractor.jira_pat_env = "PROJECT_JIRA_PAT"
    settings.extractor.jira_pat_path = tmp_path / "jira.pat"
    monkeypatch.setenv("PROJECT_JIRA_PAT", "configured-token")
    extractor.configure_credentials(settings)
    with extractor.build_session(settings.extractor) as session:
        assert session.headers["Authorization"] == "Bearer configured-token"
        assert not session.cookies
        assert "Content-Type" not in session.headers


def test_redirect_to_different_origin_strips_token(monkeypatch):
    monkeypatch.setenv("ARGUS_JIRA_PAT", "example-token")
    with extractor.build_session() as session:
        original = session.prepare_request(requests.Request("GET", "https://jira.example.com/api"))
        response = requests.Response()
        response.request = original
        redirected = session.prepare_request(requests.Request("GET", "https://other.example.com/api"))
        session.rebuild_auth(redirected, response)
        assert "Authorization" not in redirected.headers


def test_rejected_token_has_actionable_auth_error():
    response = requests.Response()
    response.status_code = 401
    with pytest.raises(extractor.AuthError, match="required project permissions"):
        extractor._raise_friendly(response, "project lookup")
