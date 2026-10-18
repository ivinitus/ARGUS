"""Immutable presentation context for report and hub renderers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_DOCS_URL = ""


@dataclass(frozen=True)
class TeamLink:
    slug: str
    name: str


@dataclass(frozen=True)
class ReportUiContext:
    """Presentation values that must not live in process-global state."""

    dashboard_name: str = "ARGUS"
    dashboard_slug: str = ""
    teams: tuple[TeamLink, ...] = ()
    docs_url: str = DEFAULT_DOCS_URL
    ticket_template_url: str = ""
    asset_base_path: str = "/_assets"
    team_base_path: str = ""
    api_url: str = ""


DEFAULT_UI_CONTEXT = ReportUiContext()


def parse_team_specs(specs: list[str]) -> tuple[TeamLink, ...]:
    """Parse ``slug=Display Name`` team declarations."""
    teams: list[TeamLink] = []
    seen: set[str] = set()
    for spec in specs:
        slug, separator, name = spec.partition("=")
        slug = slug.strip()
        name = name.strip() if separator else slug.replace("-", " ").title()
        if not slug or slug in seen:
            continue
        seen.add(slug)
        teams.append(TeamLink(slug=slug, name=name or slug))
    return tuple(teams)


def from_settings(settings: Any) -> ReportUiContext:
    """Build a renderer context from the resolved application settings."""
    profile = str(settings.general.profile or "web").strip()
    dashboard_slug = str(settings.general.team_slug or "").strip()
    dashboard_name = (
        str(settings.general.team_name or "").strip()
        or profile.replace("-", " ").title()
    )
    asset_base_path = "/_assets"
    team_base_path = ""
    return ReportUiContext(
        dashboard_name=dashboard_name,
        dashboard_slug=dashboard_slug,
        teams=parse_team_specs(list(settings.hub.teams)),
        docs_url=str(settings.general.docs_url or "").strip(),
        ticket_template_url=str(
            settings.general.ticket_template_url or ""
        ).strip(),
        asset_base_path=asset_base_path,
        team_base_path=team_base_path,
        api_url="",
    )
