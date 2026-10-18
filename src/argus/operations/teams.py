"""Profile-derived team names, local paths, and Jira routing."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class AuditTeam:
    slug: str
    profile: str
    name: str
    allow_auto_folder: bool
    state_dir: str
    output_dir: str


def _packaged_teams() -> tuple[AuditTeam, ...]:
    teams = []
    profiles = Path(__file__).resolve().parents[1] / "profiles"
    for path in sorted(profiles.glob("*.toml")):
        document = tomllib.loads(path.read_text())
        general = document.get("general", {})
        extractor = document.get("extractor", {})
        folders = document.get("folders", {})
        teams.append(AuditTeam(
            slug=general.get("team_slug") or path.stem,
            profile=path.stem,
            name=general.get("team_name") or path.stem.replace("-", " ").title(),
            allow_auto_folder=folders.get("auto_folder_enabled", True),
            state_dir=str(Path(general.get("state_dir", f"~/.argus/{path.stem}")).expanduser()),
            output_dir=extractor.get("out_dir", f"output/{path.stem}"),
        ))
    return tuple(teams)


TEAMS = _packaged_teams()
_BY_SLUG = {team.slug: team for team in TEAMS}


def resolve_team(value: str) -> AuditTeam | None:
    """Resolve a team slug from the packaged profile registry."""
    return _BY_SLUG.get(value.strip().lower())


def team_supports_jira_project(team: AuditTeam, project_id: str | int) -> bool:
    """Check the project's configured profile instead of a corporate registry."""
    from argus.config import load

    settings = load(profile=team.profile)
    return str(project_id) in settings.extractor.project_ids


def _matches_folder_prefix(folded: str, prefix: str) -> bool:
    normalized = " ".join(prefix.split()).casefold()
    if prefix[-1:].isspace():
        return folded == normalized or folded.startswith(normalized + " ")
    return bool(normalized) and folded.startswith(normalized)


def resolve_jira_team(project_id: str | int, folder_name: str) -> AuditTeam | None:
    """Resolve an unambiguous project and folder using profile configuration."""
    from argus.config import load

    project_teams = []
    matches = []
    folded = " ".join(folder_name.split()).casefold()
    for team in TEAMS:
        settings = load(profile=team.profile)
        if str(project_id) not in settings.extractor.project_ids:
            continue
        project_teams.append(team)
        prefixes = [settings.folders.auto_name_prefix, *settings.folders.dashboard_sections]
        if any(_matches_folder_prefix(folded, prefix) for prefix in prefixes):
            matches.append(team)
    if len(matches) == 1:
        return matches[0]
    return project_teams[0] if len(project_teams) == 1 else None


def hub_team_specs() -> str:
    return ",".join(f"{team.slug}={team.name}" for team in TEAMS)
