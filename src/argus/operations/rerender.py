"""Republish existing report folders without starting any audits."""
from __future__ import annotations

import argparse
import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from argus import config as argus_config
from argus import runtime
from argus.operations.teams import (
    TEAMS,
    AuditTeam,
    hub_team_specs,
    resolve_team,
)
from argus.pipeline import extractor, orchestrator
from argus.reporting import publisher as report_publisher


_TEAM_ENVIRONMENT_NAMES = (
    "ARGUS_PROFILE",
    "ARGUS_TEAM_NAME",
    "ARGUS_TEAM_SLUG",
    "ARGUS_STATE_DIR",
    "ARGUS_OUTPUT_DIR",
    "ARGUS_HUB_TEAMS",
    "TMPDIR",
)


@contextmanager
def _team_environment(team: AuditTeam) -> Iterator[None]:
    previous = {
        name: os.environ.get(name)
        for name in _TEAM_ENVIRONMENT_NAMES
    }
    os.environ.update({
        "ARGUS_PROFILE": team.profile,
        "ARGUS_TEAM_NAME": team.name,
        "ARGUS_TEAM_SLUG": team.slug,
        "ARGUS_STATE_DIR": team.state_dir,
        "ARGUS_OUTPUT_DIR": team.output_dir,
        "ARGUS_HUB_TEAMS": hub_team_specs(),
        "TMPDIR": team.state_dir,
    })
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _report_folders(
    out_dir: Path,
    *,
    folder_name: str | None = None,
) -> list[Path]:
    """Return immediate folders that have already been published locally."""
    if not out_dir.is_dir():
        return []
    selected_slug = extractor.slugify(folder_name) if folder_name else None
    return sorted(
        folder
        for folder in out_dir.iterdir()
        if (
            folder.is_dir()
            and (folder / "_argus.html").is_file()
            and (selected_slug is None or folder.name == selected_slug)
        )
    )


def _selected_teams(
    *,
    all_teams: bool,
    team_slug: str | None,
) -> tuple[AuditTeam, ...]:
    if all_teams:
        return TEAMS
    slug = team_slug or os.environ.get("ARGUS_TEAM_SLUG", "web")
    selected = resolve_team(slug)
    if selected is None:
        raise ValueError(f"unknown ARGUS team: {slug}")
    return (selected,)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Regenerate and publish existing report folders without "
            "extracting or auditing"
        ),
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--all-teams",
        action="store_true",
        help="republish existing reports for every configured team",
    )
    scope.add_argument(
        "--team",
        help="republish one team (default: ARGUS_TEAM_SLUG or web)",
    )
    content = parser.add_mutually_exclusive_group()
    content.add_argument(
        "--folder",
        help="republish only this existing report folder",
    )
    content.add_argument(
        "--dashboards-only",
        action="store_true",
        help="refresh team landing pages without rebuilding stored reports",
    )
    parser.add_argument(
        "--run",
        help="restrict an existing folder report to this exact Jira C-key",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="highest-priority TOML override",
    )
    args = parser.parse_args(argv)
    if args.run:
        args.run = args.run.strip().upper()
        if not args.folder or args.all_teams:
            parser.error("--run requires --folder and a single team")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*-C[0-9]+", args.run):
            parser.error("--run must be an exact Jira test-run key")

    try:
        teams = _selected_teams(
            all_teams=args.all_teams,
            team_slug=args.team,
        )
    except ValueError as exc:
        parser.error(str(exc))

    published = 0
    dashboards = 0
    failures = 0
    selected_folder_found = False
    for team in teams:
        with _team_environment(team):
            try:
                settings = argus_config.load(
                    args.config,
                    profile=team.profile,
                )
                runtime.configure(settings)
                folders = _report_folders(
                    settings.extractor.out_dir,
                    folder_name=args.folder,
                )
            except Exception as exc:
                failures += 1
                print(
                    f"[ARGUS] rerender {team.slug}: configuration failed "
                    f"({type(exc).__name__}: {exc})",
                    file=sys.stderr,
                )
                continue

            if not args.folder:
                try:
                    if report_publisher.refresh_dashboard():
                        dashboards += 1
                        print(
                            f"[ARGUS] rerender {team.slug}: dashboard refreshed",
                            file=sys.stderr,
                        )
                except Exception as exc:
                    failures += 1
                    print(
                        f"[ARGUS] rerender {team.slug}: dashboard refresh failed "
                        f"({type(exc).__name__}: {exc})",
                        file=sys.stderr,
                    )
            if args.dashboards_only:
                continue

            if not folders:
                if args.folder and args.all_teams:
                    continue
                folder_note = (
                    f" matching {args.folder!r}" if args.folder else ""
                )
                print(
                    f"[ARGUS] rerender {team.slug}: no existing reports"
                    f"{folder_note}",
                    file=sys.stderr,
                )
                if args.folder:
                    failures += 1
                continue

            if args.folder:
                selected_folder_found = True
            for folder in folders:
                print(
                    f"[ARGUS] rerender {team.slug}: {folder.name}",
                    file=sys.stderr,
                )
                if args.run:
                    success = orchestrator._run_folder_list(
                        args.folder,
                        settings,
                        selected_run_keys={args.run},
                        reports_only=True,
                    ) == 0
                else:
                    success = orchestrator._publish_folder_reports(folder, settings)
                if success:
                    published += 1
                else:
                    failures += 1

    if args.folder and args.all_teams and not selected_folder_found:
        failures += 1
        print(
            f"[ARGUS] rerender: no team has an existing report matching "
            f"{args.folder!r}",
            file=sys.stderr,
        )

    print(
        f"[ARGUS] rerender complete: {published} published, "
        f"{failures} failed, {dashboards} dashboards refreshed",
        file=sys.stderr,
    )
    return 1 if failures else 0
