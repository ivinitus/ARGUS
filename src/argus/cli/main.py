"""Unified ARGUS command-line entry point."""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from pathlib import Path

from argus import __version__, config


def _commands() -> dict[str, str]:
    return {
        "audit": "argus.pipeline.orchestrator",
        "cycle": "argus.pipeline.cycle",
        "extract": "argus.pipeline.extractor",
        "batch": "argus.pipeline.batch",
        "report": "argus.reporting.report",
        "serve": "argus.reporting.server",
        "replay": "argus.pipeline.replay",
        "evaluate": "argus.pipeline.evaluation",
        "monitor": "argus.operations.monitor",
        "diff": "argus.operations.diff_and_notify",
        "notify": "argus.operations.notify",
        "events": "argus.operations.events",
        "costs": "argus.operations.costs",
        "summary": "argus.operations.daily_summary",
        "bot": "argus.operations.bot",
        "control": "argus.operations.control",
        "index": "argus.operations.audit_index",
        "users": "argus.core.users",
        "history": "argus.domain.history_index",
        "backfill-env": "argus.pipeline.env_check_backfill",
        "backfill-users": "argus.pipeline.backfill_users",
        "backfill-reader-policy": "argus.operations.reader_policy_backfill",
        "heal": "argus.pipeline.r12_heal",
        "prune": "argus.operations.prune",
        "rerender": "argus.operations.rerender",
        "manual-batch": "argus.operations.manual_batch",
        "remove-folder": "argus.operations.remove_folder",
        "rules-preview": "argus.operations.rule_preview",
        "rules-demo": "argus.operations.rule_demo",
    }


_INLINE_COMMANDS = ("config",)


def _command_choices() -> list[str]:
    """Every accepted command name, derived from the dispatch table."""
    return [*_commands(), *_INLINE_COMMANDS]


def _config_command(args: list[str], profile: str | None,
                    config_path: Path | None) -> int:
    parser = argparse.ArgumentParser(prog="argus config")
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("profiles", help="list available named profiles")
    subparsers.add_parser("validate", help="validate resolved configuration")
    show = subparsers.add_parser("show", help="print resolved configuration")
    show.add_argument("--no-redact", action="store_true",
                      help="show paths and non-secret values without redaction")
    parsed = parser.parse_args(args)

    if parsed.action == "profiles":
        for name in config.available_profiles():
            print(name)
        return 0

    resolved = config.load(config_path, profile=profile)
    if parsed.action == "validate":
        print(f"configuration valid (profile={resolved.general.profile})")
        return 0

    print(json.dumps(
        config.as_dict(resolved, redact=not parsed.no_redact),
        indent=2,
        sort_keys=True,
    ))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="argus",
        description="ARGUS QA execution extraction, auditing, and reporting",
    )
    parser.add_argument("--profile", help="named profile from argus/profiles")
    parser.add_argument("--config", type=Path,
                        help="highest-priority TOML override")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    parser.add_argument("command", nargs="?", choices=_command_choices())
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    return parser


def _extract_global_options(argv: list[str]) -> tuple[list[str], str | None, Path | None]:
    """Accept --profile/--config before or after the subcommand."""
    remaining: list[str] = []
    profile: str | None = None
    config_path: Path | None = None
    index = 0
    while index < len(argv):
        value = argv[index]
        if value in {"--profile", "--config"}:
            if index + 1 >= len(argv):
                raise config.ConfigError(f"{value} requires a value")
            option_value = argv[index + 1]
            if value == "--profile":
                profile = option_value
            else:
                config_path = Path(option_value)
            index += 2
            continue
        if value.startswith("--profile="):
            profile = value.partition("=")[2]
        elif value.startswith("--config="):
            config_path = Path(value.partition("=")[2])
        else:
            remaining.append(value)
        index += 1
    return remaining, profile, config_path


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        cleaned_argv, profile, config_path = _extract_global_options(raw_argv)
    except config.ConfigError as exc:
        print(f"argus: {exc}", file=sys.stderr)
        return 2
    parsed = _parser().parse_args(cleaned_argv)
    parsed.profile = profile or parsed.profile
    parsed.config = config_path or parsed.config
    if parsed.command is None:
        _parser().print_help()
        return 0

    if parsed.profile:
        os.environ["ARGUS_PROFILE"] = parsed.profile
    if parsed.config:
        os.environ["ARGUS_CONFIG"] = str(parsed.config)

    if parsed.command == "config":
        try:
            return _config_command(
                parsed.arguments,
                profile=parsed.profile,
                config_path=parsed.config,
            )
        except config.ConfigError as exc:
            print(f"argus: {exc}", file=sys.stderr)
            return 2

    module = importlib.import_module(_commands()[parsed.command])
    return int(module.main(parsed.arguments) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
