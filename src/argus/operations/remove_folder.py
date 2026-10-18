"""Remove one local audit folder after explicit confirmation."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from argus import config as argus_config
from argus import runtime
from argus.core import paths
from argus.operations import audit_index, control, notify

FOLDER_SLUG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,159}$")

_TERMINAL_AUDIT_PHASES = frozenset({"failed", "stopped"})


class RemovalRefused(RuntimeError):
    """The request is well-formed but unsafe to carry out right now."""


@dataclass(frozen=True)
class RemovalPlan:
    folder_slug: str
    output_root: Path
    folder_dir: Path | None
    pointer_file: Path | None

    def describe(self) -> list[str]:
        lines = []
        if self.folder_dir is not None:
            lines.append(f"local folder {self.folder_dir}")
        if self.pointer_file is not None:
            lines.append(f"last_folder_out_dir pointer {self.pointer_file}")
        return lines


def _folder_dir_to_remove(output_root: Path, folder_slug: str) -> Path | None:
    """Return the folder directory when it is safe to delete, else refuse.

    Mirrors prune.py's folder guards: the path must be a direct child of
    the output root, not a symlink, a real directory, and carry
    ``_folder_meta.json`` so an operator typo can never select the output
    root itself, a state directory, or a mount point.
    """
    folder_dir = output_root / folder_slug
    if folder_dir.is_symlink():
        raise RemovalRefused(f"{folder_dir} is a symlink; refusing to remove")
    if not folder_dir.exists():
        return None
    if not folder_dir.is_dir():
        raise RemovalRefused(f"{folder_dir} is not a directory")
    try:
        resolved_parent = folder_dir.resolve(strict=True).parent
    except OSError as exc:
        raise RemovalRefused(f"cannot resolve {folder_dir}: {exc}") from exc
    if resolved_parent != output_root.resolve():
        raise RemovalRefused(
            f"{folder_dir} is not a direct child of {output_root}"
        )
    if not (folder_dir / "_folder_meta.json").is_file():
        raise RemovalRefused(
            f"{folder_dir} has no _folder_meta.json; not an ARGUS folder dir"
        )
    return folder_dir


def plan(settings: Any, folder_slug: str) -> RemovalPlan:
    """Validate the request against the filesystem, index, and live audits."""
    if not FOLDER_SLUG_RE.fullmatch(folder_slug) or folder_slug in {".", ".."}:
        raise RemovalRefused("folder must be an exact on-disk slug")
    output_root = paths.output_root(settings)
    folder_dir = _folder_dir_to_remove(output_root, folder_slug)
    if folder_dir is None:
        raise RemovalRefused(
            f"no folder named {folder_slug!r} under {output_root}"
        )

    record = control.current_audit()
    if (
        record
        and not record.get("stale")
        and str(record.get("phase") or "") not in _TERMINAL_AUDIT_PHASES
        and folder_slug in {record.get("folder"), record.get("target")}
    ):
        raise RemovalRefused(
            f"{folder_slug!r} is being audited right now (owner "
            f"{record.get('requested_by') or 'scheduler'}); stop it first"
        )

    pointer_file: Path | None = None
    pointer = settings.general.state_dir / "last_folder_out_dir"
    if folder_dir is not None:
        try:
            recorded = pointer.read_text().splitlines()[0].strip()
        except (OSError, IndexError):
            recorded = ""
        if recorded and recorded == str(folder_dir.resolve()):
            pointer_file = pointer
    return RemovalPlan(
        folder_slug=folder_slug,
        output_root=output_root,
        folder_dir=folder_dir,
        pointer_file=pointer_file,
    )


def apply(removal: RemovalPlan) -> dict[str, Any]:
    """Carry out the validated local removal plan."""
    summary: dict[str, Any] = {
        "folder": removal.folder_slug,
        "deleted_objects": 0,
        "index_entries_removed": 0,
        "removed_dir": None,
        "index_synced": False,
        "pointer_cleared": False,
    }
    if removal.folder_dir is not None:
        # Re-run the guards: the tree may have changed since planning.
        folder_dir = _folder_dir_to_remove(
            removal.output_root,
            removal.folder_slug,
        )
        if folder_dir is not None:
            shutil.rmtree(folder_dir)
            summary["removed_dir"] = str(folder_dir)
    if (removal.output_root / audit_index.DB_FILENAME).exists():
        audit_index.sync(removal.output_root)
        summary["index_synced"] = True
    if removal.pointer_file is not None:
        try:
            removal.pointer_file.unlink()
            summary["pointer_cleared"] = True
        except FileNotFoundError:
            pass
    return summary


def _notify(
    settings: Any,
    *,
    level: str,
    title: str,
    body: str,
) -> None:
    try:
        notify.post(level, title, body, settings=settings)
    except Exception as exc:
        print(
            f"[ARGUS] remove-folder notice not sent: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="argus remove-folder",
        description="Permanently remove one folder's audit output "
                    "(local report directory).",
    )
    parser.add_argument("--folder", required=True,
                        help="exact folder slug as shown on the dashboard")
    parser.add_argument("--yes", action="store_true",
                        help="actually delete; without it only print the plan")
    parser.add_argument("--no-notify", action="store_true",
                        help="skip the Slack notice")
    args = parser.parse_args(argv)

    settings = argus_config.load()
    runtime.configure(settings)
    team = settings.general.team_slug or settings.general.profile
    requested_by = os.environ.get("ARGUS_REQUESTED_BY", "").strip() or (
        os.environ.get("USER", "").strip() or "unknown"
    )

    try:
        removal = plan(settings, args.folder)
    except RemovalRefused as exc:
        print(f"[ARGUS] remove-folder refused: {exc}", file=sys.stderr)
        if args.yes and not args.no_notify:
            _notify(
                settings,
                level="warn",
                title=f"remove-folder {args.folder} refused ({team})",
                body=f"requested by {requested_by}\n{exc}",
            )
        return 1
    except Exception as exc:
        # e.g. the published index is unreadable: nothing was changed, but
        # the operator who asked must hear why nothing happened.
        print(
            f"[ARGUS] remove-folder could not validate {args.folder!r}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        if args.yes and not args.no_notify:
            _notify(
                settings,
                level="warn",
                title=f"remove-folder {args.folder} failed ({team})",
                body=f"requested by {requested_by}\n{type(exc).__name__}: {exc}",
            )
        return 1

    print(
        f"[ARGUS] remove-folder {removal.folder_slug!r} for team {team} "
        f"requested by {requested_by}",
        file=sys.stderr,
    )
    for line in removal.describe():
        print(f"  {'remove' if args.yes else 'would remove'}: {line}",
              file=sys.stderr)
    if not args.yes:
        print("[ARGUS] dry run; pass --yes to delete", file=sys.stderr)
        return 1

    try:
        summary = apply(removal)
    except Exception as exc:
        print(
            f"[ARGUS] remove-folder failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        if not args.no_notify:
            _notify(
                settings,
                level="warn",
                title=f"remove-folder {removal.folder_slug} failed ({team})",
                body=f"requested by {requested_by}\n{type(exc).__name__}: {exc}",
            )
        return 1

    print(json.dumps(summary, indent=2, sort_keys=True))
    if not args.no_notify:
        body_lines = [f"requested by {requested_by}"]
        body_lines.append(
            f"directory removed: {summary['removed_dir'] or 'none present'}"
        )
        _notify(
            settings,
            level="info",
            title=f"folder {removal.folder_slug} removed ({team})",
            body="\n".join(body_lines),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
