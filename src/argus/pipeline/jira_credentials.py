"""Resolve Jira personal access tokens from standard local credential sources."""
from __future__ import annotations

from pathlib import Path
from typing import Protocol


class JiraCredentialError(RuntimeError):
    """The configured Jira credential could not be loaded safely."""


class JiraCredentialProvider(Protocol):
    def get_pat(self) -> str | None: ...


class LocalCredentialProvider:
    """Read a configured environment variable, .env, or private PAT file."""

    def __init__(self, env_name: str, pat_path: Path) -> None:
        self.env_name = env_name
        self.pat_path = pat_path

    def get_pat(self) -> str | None:
        from argus.pipeline import extractor
        return extractor._load_jira_pat(self.env_name, self.pat_path)


def build_provider(settings) -> JiraCredentialProvider:
    """Create a provider from the selected profile's Jira settings."""
    return LocalCredentialProvider(
        settings.extractor.jira_pat_env, settings.extractor.jira_pat_path)
