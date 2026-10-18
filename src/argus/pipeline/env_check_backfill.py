"""Back-fill env_check results onto existing audit.json files."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from concurrent.futures import as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from argus.core import exec_paths

log = logging.getLogger("argus.env_backfill")


@dataclass
class BackfillResult:
    audit_path: Path
    status: str
    verdict: str | None = None
    images_scanned: int = 0
    images_with_prod: int = 0
    images_with_preprod: int = 0
    was_finding_added: bool = False
    detail: str = ""


def _process_one(
    audit_path_str: str,
    *,
    dry_run: bool,
    force: bool,
    sample_stride: int = 1,
    region_crop_height: int = 0,
    region_crop_bottom_height: int = 0,
    engine: str = "model",
    model_api_url: str | None = None,
    api_key_env: str | None = None,
    model_rates: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """Run env_check for a single audit.json and update it in place."""
    audit_path = Path(audit_path_str)
    try:
        audit = json.loads(audit_path.read_text())
    except Exception as e:
        return BackfillResult(
            audit_path=audit_path, status="error",
            detail=f"audit.json read/parse failed: {type(e).__name__}: {e}",
        ).__dict__

    if engine and engine != "model":
        log.info("env_check_backfill engine=%r requested but tesseract "
                 "is retired; using model", engine)
    from argus.pipeline import env_check_model
    current_version = env_check_model.ENV_CHECK_VERSION

    existing = audit.get("env_check") or {}
    if not force and existing.get("version") == current_version:
        return BackfillResult(
            audit_path=audit_path, status="skipped",
            detail=f"already at env_check version {current_version}",
        ).__dict__

    execution_dir = audit_path.parent
    if not (execution_dir / "screenshots").exists():
        return BackfillResult(
            audit_path=audit_path, status="no_screenshots",
            detail="screenshots/ directory missing — cannot run env_check",
        ).__dict__

    from argus.pipeline import env_check_merge
    try:
        outcome = env_check_merge.run_and_merge(
            audit,
            execution_dir,
            sample_stride=sample_stride,
            region_crop_height=region_crop_height,
            region_crop_bottom_height=region_crop_bottom_height,
            model_api_url=model_api_url,
            api_key_env=api_key_env,
            model_rates=model_rates,
        )
    except Exception as e:
        return BackfillResult(
            audit_path=audit_path, status="error",
            detail=f"env_check raised: {type(e).__name__}: {e}",
        ).__dict__
    audit = outcome.audit
    ec = outcome.ec
    added_finding = outcome.added_finding
    had_old_finding = outcome.had_stale_finding

    if not dry_run:
        env_check_merge.write_merged_audit(audit_path, audit)

    return BackfillResult(
        audit_path=audit_path, status="ok",
        verdict=ec["verdict"],
        images_scanned=ec["images_scanned"],
        images_with_prod=ec["images_with_prod"],
        images_with_preprod=ec["images_with_preprod"],
        was_finding_added=added_finding,
        detail=(
            f"{'DRY-RUN: would update' if dry_run else 'updated'} "
            f"{'(replaced stale R9/R9-amb/R9-hover/R13)' if had_old_finding else ''}"
        ).strip(),
    ).__dict__


def _discover_audits(root: Path) -> list[Path]:
    """Return all audit.json files under ``root``, excluding replay trees."""
    return sorted(exec_paths.iter_audit_jsons(root))


def run_backfill(
    *,
    root: Path,
    dry_run: bool,
    force: bool,
    workers: int,
    sample_stride: int = 1,
    region_crop_height: int = 0,
    region_crop_bottom_height: int = 0,
    engine: str = "model",
    model_api_url: str | None = None,
    api_key_env: str | None = None,
    model_rates: dict[str, dict[str, float]] | None = None,
) -> int:
    """Orchestrate the back-fill across all audit.json files under ``root``."""
    audits = _discover_audits(root)
    if not audits:
        log.warning("no audit.json files found under %s", root)
        return 0

    from argus.pipeline import env_check_model
    version_for_log = env_check_model.ENV_CHECK_VERSION
    log.info(
        "back-filling env_check (model v%d) across %d audit(s) "
        "under %s (workers=%d, dry_run=%s, force=%s, sample_stride=%d, "
        "region_crop_height=%d, region_crop_bottom_height=%d)",
        version_for_log, len(audits), root, workers, dry_run,
        force, sample_stride, region_crop_height, region_crop_bottom_height,
    )

    results: list[dict[str, Any]] = []
    from concurrent.futures import ThreadPoolExecutor as _Executor
    with _Executor(max_workers=workers) as ex:
        futures = {
            ex.submit(
                _process_one, str(p),
                dry_run=dry_run, force=force,
                sample_stride=sample_stride,
                region_crop_height=region_crop_height,
                region_crop_bottom_height=region_crop_bottom_height,
                engine=engine, model_api_url=model_api_url, api_key_env=api_key_env,
                model_rates=model_rates,
            ): p
            for p in audits
        }
        for i, fut in enumerate(as_completed(futures), start=1):
            try:
                r = fut.result()
            except Exception as e:  # pragma: no cover
                r = BackfillResult(
                    audit_path=futures[fut], status="error",
                    detail=f"worker raised: {type(e).__name__}: {e}",
                ).__dict__
            results.append(r)
            path = Path(r["audit_path"])
            rel = path.relative_to(root) if path.is_relative_to(root) else path
            if r["status"] == "ok":
                log.info(
                    "[%d/%d] %s — %s (scanned=%d prep=%d prod=%d%s)",
                    i, len(audits), rel, r["verdict"],
                    r["images_scanned"], r["images_with_preprod"],
                    r["images_with_prod"],
                    " +R9" if r["was_finding_added"] else "",
                )
            elif r["status"] == "skipped":
                log.debug("[%d/%d] %s — skipped (%s)", i, len(audits), rel, r["detail"])
            else:
                log.warning(
                    "[%d/%d] %s — %s: %s",
                    i, len(audits), rel, r["status"], r["detail"],
                )

    _print_summary(results)
    return 0 if all(r["status"] != "error" for r in results) else 1


def _print_summary(results: list[dict[str, Any]]) -> None:
    """Print a concise breakdown of verdict/status counts at the end."""
    by_status: dict[str, int] = {}
    by_verdict: dict[str, int] = {}
    findings_added = 0
    for r in results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        if r["status"] == "ok" and r.get("verdict"):
            by_verdict[r["verdict"]] = by_verdict.get(r["verdict"], 0) + 1
        if r.get("was_finding_added"):
            findings_added += 1

    print()
    print("=== env_check back-fill summary ===")
    print(f"  audits processed: {sum(by_status.values())}")
    for k in sorted(by_status):
        print(f"    {k:<16} {by_status[k]}")
    if by_verdict:
        print()
        print("  verdicts:")
        for k in ("compliant", "violation", "mixed", "ambiguous"):
            print(f"    {k:<16} {by_verdict.get(k, 0)}")
    print()
    print(f"  R9 findings added: {findings_added}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root", type=Path, default=None,
        help="directory to scan for audit.json files "
             "(default: config out_dir, anchored to the project root)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="compute env_check results but do not write audit.json",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="reprocess even audits already at the current env_check version",
    )
    parser.add_argument(
        "-w", "--workers", type=int, default=8,
        help="parallel OCR workers (default: 8)",
    )
    parser.add_argument(
        "--sample-stride", type=int, default=1,
        help="OCR every Nth screenshot plus first/last 3 (default: 1 = "
             "full scan). Stride=5 empirically preserves all compliance "
             "catches at ~3-5x speedup.",
    )
    parser.add_argument(
        "--region-crop-height", type=int, default=0,
        help="Crop each screenshot to top N pixels before OCR. 0 = "
             "full-page OCR. 180 covers desktop browser chrome with "
             "5-10x fewer pixels per OCR call.",
    )
    parser.add_argument(
        "--region-crop-bottom-height", type=int, default=0,
        help="Also OCR bottom N pixels (stitched with top). Catches "
             "desktop status-bar hover URLs + iOS-Safari bottom URL "
             "bars. 60-80 typical. 0 disables.",
    )
    parser.add_argument(
        "--engine", choices=["tesseract", "model"], default=None,
        help="DEPRECATED — tesseract retired 2026-05-23, model is the "
             "only engine. Flag accepted for back-compat with old "
             "scripts but `tesseract` falls through to `model` with a "
             "log warning.",
    )
    parser.add_argument(
        "--config", type=Path, default=None,
        help="path to config.toml (default: ./config.toml). Used to "
             "configure the model API for environment checks.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    from argus import config as argus_config
    settings = argus_config.load(args.config)
    from argus import runtime
    runtime.configure(settings)

    if not settings.policy.environment_check_enabled:
        log.info(
            "environment check is disabled by profile policy; "
            "nothing to back-fill"
        )
        return 0

    if args.root is None:
        from argus.core import paths as _paths
        args.root = _paths.output_root(settings)
    if not args.root.exists():
        log.error("root path does not exist: %s", args.root)
        return 2
    engine = args.engine or settings.auditor.env_check_engine or "model"
    model_api_url = settings.auditor.model_api_url
    api_key_env = settings.auditor.api_key_env

    sample_stride = (args.sample_stride if args.sample_stride != 1
                     else settings.auditor.env_check_sample_stride)
    region_crop_height = (args.region_crop_height if args.region_crop_height
                          else settings.auditor.env_check_region_crop_height)
    region_crop_bottom_height = (
        args.region_crop_bottom_height if args.region_crop_bottom_height
        else settings.auditor.env_check_region_crop_bottom_height)

    return run_backfill(
        root=args.root,
        dry_run=args.dry_run,
        force=args.force,
        workers=args.workers,
        sample_stride=sample_stride,
        region_crop_height=region_crop_height,
        region_crop_bottom_height=region_crop_bottom_height,
        engine=engine,
        model_api_url=model_api_url,
        api_key_env=api_key_env,
        model_rates=settings.costs.model_rates,
    )


if __name__ == "__main__":
    sys.exit(main())
