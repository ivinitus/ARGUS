"""Resolve sign-in aliases with the existing Jira service credential flow."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests


class IdentityUnavailable(RuntimeError):
    """Transient Jira lookup failure; no roster may be saved."""


class JiraReviewerResolver:
    def __init__(self, provider: Any, base_url: str):
        self.provider, self.base_url = provider, base_url.rstrip("/")

    @classmethod
    def from_environment(cls) -> JiraReviewerResolver:
        from argus import config
        from argus.pipeline.jira_credentials import build_provider

        settings = config.load()
        return cls(build_provider(settings), settings.extractor.base_url)

    def __call__(self, aliases: list[str]) -> list[dict[str, str]]:
        try:
            token = self.provider.get_pat()
        except Exception:
            raise IdentityUnavailable("Jira lookup is unavailable. Try again shortly.") from None
        if not token:
            raise IdentityUnavailable("Jira lookup is unavailable. Try again shortly.")
        deadline = time.monotonic() + 12

        def lookup(alias: str) -> dict[str, str]:
            if time.monotonic() >= deadline:
                raise IdentityUnavailable("Jira lookup timed out. Try again shortly.")
            try:
                # One session per thread; service credentials never follow redirects.
                with requests.Session() as session:
                    response = session.get(
                        f"{self.base_url}/rest/api/2/user",
                        params={"username": alias},
                        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                        timeout=(2, 3), allow_redirects=False,
                    )
                if response.status_code == 404:
                    raise ValueError(f"Jira could not find '{alias}'. Check the sign-in alias.")
                if response.status_code != 200:
                    raise IdentityUnavailable("Jira lookup is unavailable. Try again shortly.")
                value = response.json()
            except requests.RequestException:
                raise IdentityUnavailable("Jira lookup is unavailable. Try again shortly.") from None
            if (
                not isinstance(value, dict) or value.get("active") is not True
                or str(value.get("name", "")).casefold() != alias
                or not isinstance(value.get("key"), str) or not value["key"].strip()
            ):
                raise ValueError(f"Jira could not verify an active user for '{alias}'.")
            return {"alias": alias, "jira_key": value["key"],
                    "name": value.get("displayName") or alias}

        # Limit concurrency and stop scheduling lookups after the request budget.
        with ThreadPoolExecutor(max_workers=10) as pool:
            return list(pool.map(lookup, aliases))
