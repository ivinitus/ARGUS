"""Reclaim disk by pruning old screenshot evidence from the output root."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from argus import config as argus_config
from argus.core import exec_paths, paths
from argus.operations import notify


@dataclass
class FolderPlan:
    """What would happen (or did happen) to one top-level folder dir."""
    name: str
    action: str
    age_days: float | None = None
    targets: list[Path] = field(default_factory=list)
    bytes_: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class PruneResult:
    root: Path
    applied: bool
    older_than_days: float
    folders: list[FolderPlan] = field(default_factory=list)

    @property
    def pruned(self) -> list[FolderPlan]:
        return [f for f in self.folders if f.action == "prune"]

    @property
    def total_bytes(self) -> int:
        return sum(f.bytes_ for f in self.pruned)

    @property
    def total_targets(self) -> int:
        return sum(len(f.targets) for f in self.pruned)

    @property
    def errors(self) -> list[str]:
        return [e for f in self.folders for e in f.errors]

    def to_dict(self) -> dict:
        return {
            "root": str(self.root),
            "applied": self.applied,
            "older_than_days": self.older_than_days,
            "total_targets": self.total_targets,
            "total_bytes": self.total_bytes,
            "total_gb": round(self.total_bytes / 1e9, 3),
            "errors": self.errors,
            "folders": [
                {
                    "name": f.name,
                    "action": f.action,
                    "age_days": (None if f.age_days is None
                                 else round(f.age_days, 1)),
                    "targets": len(f.targets),
                    "bytes": f.bytes_,
                    "errors": f.errors,
                }
                for f in self.folders
            ],
        }


def _folder_age_days(folder: Path, *, now: float | None = None) -> float | None:
    """Days since the newest canonical audit.json under `folder`."""
    newest: float | None = None
    for audit in exec_paths.iter_audit_jsons(folder):
        try:
            mtime = audit.stat().st_mtime
        except OSError:
            continue
        if newest is None or mtime > newest:
            newest = mtime
    if newest is None:
        return None
    return ((now if now is not None else time.time()) - newest) / 86400.0


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def _target_size(target: Path) -> int:
    try:
        if target.is_dir():
            return _dir_size(target)
        return target.stat().st_size
    except OSError:
        return 0


def _prunable_targets(folder: Path) -> list[Path]:
    """Evidence paths under one folder dir that are safe to remove."""
    targets: list[Path] = []
    for exec_dir in sorted({p.parent for p in exec_paths.iter_audit_jsons(folder)}):
        shots = exec_dir / "screenshots"
        if shots.is_dir():
            targets.append(shots)
        videos = exec_dir / "video_evidence"
        if videos.is_dir():
            targets.append(videos)
        targets.extend(sorted(exec_dir.glob("*.pdf")))
    return targets


def _is_single_folder(root: Path, output_root: Path) -> bool:
    """True when `root` is one folder dir rather than the output root."""
    return (
        (root / "_folder_meta.json").exists()
        or (root / "_argus.html").exists()
        or root.resolve().parent == output_root.resolve()
    )


def plan(
    root: Path,
    *,
    older_than_days: float,
    output_root: Path | None = None,
    now: float | None = None,
) -> PruneResult:
    """Compute what pruning would remove. Never deletes anything."""
    result = PruneResult(root=root, applied=False,
                         older_than_days=older_than_days)
    if not root.exists():
        return result

    if _is_single_folder(root, output_root if output_root is not None else root):
        folders = [root]
    else:
        folders = sorted(p for p in root.iterdir() if p.is_dir())

    for folder in folders:
        age = _folder_age_days(folder, now=now)
        if age is None:
            result.folders.append(
                FolderPlan(name=folder.name, action="skipped"))
            continue
        if age < older_than_days:
            result.folders.append(
                FolderPlan(name=folder.name, action="keep", age_days=age))
            continue
        targets = _prunable_targets(folder)
        if not targets:
            result.folders.append(
                FolderPlan(name=folder.name, action="clean", age_days=age))
            continue
        result.folders.append(FolderPlan(
            name=folder.name,
            action="prune",
            age_days=age,
            targets=targets,
            bytes_=sum(_target_size(t) for t in targets),
        ))
    return result


def apply(result: PruneResult) -> PruneResult:
    """Delete the planned targets in place, recording per-folder failures."""
    for folder in result.pruned:
        for target in folder.targets:
            try:
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            except OSError as e:
                folder.errors.append(f"{target}: {e}")
    result.applied = True
    return result


def _render(result: PruneResult) -> str:
    lines: list[str] = []
    for f in result.folders:
        if f.action == "skipped":
            continue
        if f.action == "keep":
            lines.append(f"keep   {f.name}  (newest audit {f.age_days:.0f}d old)")
        elif f.action == "clean":
            lines.append(f"clean  {f.name}  (already pruned)")
        else:
            verb = "PRUNE " if result.applied else "would prune"
            lines.append(
                f"{verb} {f.name}  ({len(f.targets)} target(s), "
                f"{f.bytes_ / 1e9:.2f} GB, newest audit {f.age_days:.0f}d old)"
            )
    mode = ("reclaimed" if result.applied
            else "reclaimable (dry-run; use --apply)")
    lines.append("")
    lines.append(f"{result.total_targets} target(s), "
                 f"{result.total_bytes / 1e9:.2f} GB {mode}")
    return "\n".join(lines)


def _notify(result: PruneResult, *, settings) -> None:
    """Report an applied prune to Slack."""
    gb = result.total_bytes / 1e9
    folders = result.pruned
    if result.errors:
        level, title = "warn", (
            f"prune reclaimed {gb:.2f} GB with "
            f"{len(result.errors)} failure(s)"
        )
    elif not folders:
        level, title = "info", "prune found nothing older than " \
                               f"{result.older_than_days:.0f}d"
    else:
        level, title = "info", f"prune reclaimed {gb:.2f} GB"

    body_lines = [
        f"{f.name}: {len(f.targets)} target(s), {f.bytes_ / 1e9:.2f} GB "
        f"(newest audit {f.age_days:.0f}d old)"
        for f in folders
    ]
    body_lines.extend(f"! {e}" for e in result.errors[:10])
    if len(result.errors) > 10:
        body_lines.append(f"! +{len(result.errors) - 10} more failure(s)")
    notify.post(level, title, "\n".join(body_lines), settings=settings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="argus prune",
        description="Prune old screenshots/PDFs/videos from the output root "
                    "(keeps every audit artefact).")
    parser.add_argument(
        "--root", type=Path, default=None,
        help="output root or a single folder dir "
             "(default: extractor.out_dir)")
    parser.add_argument(
        "--older-than-days", type=float, default=None,
        help="only prune folders whose NEWEST audit is older than this "
             "(default: prune.older_than_days)")
    parser.add_argument(
        "--apply", action="store_true",
        help="actually delete. Without this flag the run is a dry-run "
             "preview.")
    parser.add_argument(
        "--json", action="store_true",
        help="emit machine-readable totals instead of the listing")
    parser.add_argument(
        "--no-notify", action="store_true",
        help="skip the Slack summary that --apply otherwise posts")
    parser.add_argument(
        "--if-enabled", action="store_true",
        help="do nothing unless prune.enabled is set. For the cron step — "
             "an operator running this by hand has already decided.")
    args = parser.parse_args(argv)

    try:
        settings = argus_config.load()
    except Exception as e:
        print(f"prune: config unavailable ({e}); using defaults",
              file=sys.stderr)
        settings = argus_config.ArgusConfig()

    if args.if_enabled and not settings.prune.enabled:
        print("prune disabled (prune.enabled = false)")
        return 0

    output_root = paths.output_root(settings)
    root = args.root if args.root is not None else output_root
    older_than = (args.older_than_days if args.older_than_days is not None
                  else settings.prune.older_than_days)

    if not root.exists():
        print(f"nothing to do: {root} does not exist", file=sys.stderr)
        if args.json:
            print(json.dumps(
                PruneResult(root=root, applied=False,
                            older_than_days=older_than).to_dict(), indent=2))
        return 0

    result = plan(root, older_than_days=older_than, output_root=output_root)
    if args.apply:
        apply(result)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(_render(result))

    if args.apply and not args.no_notify:
        _notify(result, settings=settings)

    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
