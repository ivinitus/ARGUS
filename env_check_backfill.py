"""Back-fill env_check results onto existing audit.json files.

Runs env_check (OCR-based URL classification) over every audit's
screenshots, writes the results into audit.json under the ``env_check``
key, and merges any R9 violation finding into the audit's ``findings``
list via the same ``merge_rule_findings`` helper the auditor uses.

The extractor + LLM Runtime auditor are NOT invoked — this is a pure
CPU-only re-scoring of already-extracted screenshots. Useful to:

* Retro-apply the env check to the 145-key canonical corpus and the
  71-key Targeted_regression corpus without spending LLM Runtime dollars
  (~15 min total at parallelism 8, vs ~$8 + 25 min for a V4 re-audit)
* Re-run after an env_check version bump to update verdicts
* Validate a new OCR-normalisation rule on stored data

By default skips audits already at the current ``ENV_CHECK_VERSION``;
use ``--force`` to reprocess.

Usage
-----

    # Back-fill the entire output/ tree:
    python env_check_backfill.py

    # Just one folder:
    python env_check_backfill.py --root output/Targeted_regression_-08_May_2026_WEB

    # Preview without writing:
    python env_check_backfill.py --dry-run

    # Force reprocessing even if version matches:
    python env_check_backfill.py --force
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from concurrent.futures import as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import auditor
# env_check (tesseract) retired 2026-05-23. env_check_haiku is the
# only engine and is imported lazily inside _process_one and
# run_backfill so this module's startup imports stay minimal.

log = logging.getLogger("argus.env_backfill")


# ---------------------------------------------------------------------------
# Per-audit work unit. Returns a status dict so the caller can aggregate.
# ---------------------------------------------------------------------------
@dataclass
class BackfillResult:
    audit_path: Path
    status: str  # "ok" | "skipped" | "no_screenshots" | "error"
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
    engine: str = "haiku",
    region: str = "us-west-2",
    profile: str | None = None,
) -> dict[str, Any]:
    """Run env_check for a single audit.json and update it in place.

    Implemented as a module-level function (no closures) so it can be
    pickled into a ProcessPoolExecutor. `engine` selects between the
    tesseract and Haiku-based pipelines; both produce the same output
    schema.
    """
    audit_path = Path(audit_path_str)
    try:
        audit = json.loads(audit_path.read_text())
    except Exception as e:
        return BackfillResult(
            audit_path=audit_path, status="error",
            detail=f"audit.json read/parse failed: {type(e).__name__}: {e}",
        ).__dict__

    # Pick the engine module + version for skip-detection. The two
    # engines have independent ENV_CHECK_VERSION counters because
    # they classify differently — re-running back-fill with a
    # different engine should always re-process (force=True is the
    # cleaner switch for engine migrations though).
    # Tesseract retired 2026-05-23; only Haiku remains. The `engine`
    # parameter is preserved for backwards-compatible callers but
    # ignored — we always import env_check_haiku.
    if engine and engine != "haiku":
        log.info("env_check_backfill engine=%r requested but tesseract "
                 "is retired; using haiku", engine)
    import env_check_haiku
    engine_module = env_check_haiku
    current_version = env_check_haiku.ENV_CHECK_VERSION

    # Skip if already processed at the current version (unless --force).
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

    # Webview folder heuristic — used to trigger R9-amb on
    # ambiguous verdicts and to tighten sample_stride. Mirrors
    # auditor._is_webview_path so both paths agree. NOTE: mobile-
    # browser folders are NOT webview tests (V5.3 fix); only
    # explicit Webview_E2E_* / in-app folders qualify.
    _exec_path_lower = str(execution_dir).lower()
    is_webview = ("webview" in _exec_path_lower
                  or "in_app" in _exec_path_lower
                  or "in-app" in _exec_path_lower)
    effective_stride = (max(2, sample_stride - 1)
                        if is_webview and sample_stride > 1
                        else sample_stride)

    try:
        ec = engine_module.check_env_compliance(
            execution_dir,
            sample_stride=effective_stride,
            region_crop_height=region_crop_height,
            region_crop_bottom_height=region_crop_bottom_height,
            region=region,
            profile=profile,
            is_webview=is_webview,
        )
    except Exception as e:
        return BackfillResult(
            audit_path=audit_path, status="error",
            detail=f"env_check raised: {type(e).__name__}: {e}",
        ).__dict__

    # If this is a re-run and an old R9 / R13 finding exists in the
    # audit's findings list, drop it so we don't accumulate duplicates
    # on each back-fill. Both are env_check-derived: R9/R9-amb fire
    # from env_check itself, R13 fires post-env_check from the
    # marketplace match. Both are reproducible from current env_check
    # output + metadata so re-deriving them is safe.
    findings = list(audit.get("findings") or [])
    had_old_finding = any(
        (f.get("source") == "env_check"
         or f.get("rule") in ("R9", "R9-amb", "R9-hover", "R13"))
        for f in findings
    )
    if had_old_finding:
        findings = [
            f for f in findings
            if not (f.get("source") == "env_check"
                    or f.get("rule") in ("R9", "R9-amb", "R9-hover", "R13"))
        ]
        audit["findings"] = findings

    audit["env_check"] = {
        "version": ec["version"],
        "verdict": ec["verdict"],
        "preprod_urls": ec["preprod_urls"],
        "prod_urls": ec["prod_urls"],
        "images_with_preprod": ec["images_with_preprod"],
        "images_with_prod": ec["images_with_prod"],
        "images_scanned": ec["images_scanned"],
        "per_image": ec["per_image"],
    }

    added_finding = False
    if ec["findings"]:
        audit = auditor.merge_rule_findings(audit, ec["findings"])
        added_finding = True
    # R13 marketplace match — needs metadata for the assigned MP and
    # env_check output for the URLs. Re-fired on every backfill so a
    # fresh env_check that picks up new URLs (or a metadata file
    # newly stamped with `marketplace`) immediately gets a
    # corresponding R13 verdict.
    import workflow_rules
    metadata_path = execution_dir / "metadata.json"
    metadata = {}
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text())
        except (OSError, json.JSONDecodeError):
            metadata = {}
    r13 = workflow_rules.check_marketplace_match(metadata, audit)
    if r13:
        audit = auditor.merge_rule_findings(audit, r13)
        added_finding = True

    # Bump the schema version tag forward so readers can tell this
    # audit has the env_check field. Never downgrade.
    if audit.get("schema_version", 0) < auditor.AUDIT_SCHEMA_VERSION:
        audit["schema_version"] = auditor.AUDIT_SCHEMA_VERSION

    audit = auditor.enrich_audit(audit, metadata)
    if not dry_run:
        audit_path.write_text(json.dumps(audit, indent=2))

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


# ---------------------------------------------------------------------------
# Discovery + orchestration
# ---------------------------------------------------------------------------
def _discover_audits(root: Path) -> list[Path]:
    """Return all audit.json files under ``root``, excluding replay trees.

    Replay outputs live under ``_replay/<variant>/audit.json`` — we
    skip them because they are meant to be held constant for A/B
    comparison. Back-filling would mutate them and break reproducibility.
    """
    return sorted(
        p for p in root.rglob("audit.json")
        if "_replay" not in p.parts
    )


def run_backfill(
    *,
    root: Path,
    dry_run: bool,
    force: bool,
    workers: int,
    sample_stride: int = 1,
    region_crop_height: int = 0,
    region_crop_bottom_height: int = 0,
    engine: str = "haiku",
    region: str = "us-west-2",
    profile: str | None = None,
) -> int:
    """Orchestrate the back-fill across all audit.json files under ``root``.

    sample_stride is forwarded to env_check.check_env_compliance. Set to
    N>1 to OCR only every Nth screenshot plus the first/last 3, cutting
    OCR work roughly linearly without losing compliance catches (URL
    bars repeat across consecutive pages). Default 1 preserves the
    full-scan historical behaviour.

    region_crop_height is also forwarded. Set to N>0 to OCR only the
    top N pixels of each screenshot (where URL bars live). 5-10x
    fewer pixels per OCR call. 0 = full-image OCR. Stacks with
    sample_stride for combined speedup.

    region_crop_bottom_height adds a bottom strip (stitched to the top
    strip) so status-bar hover URLs and iOS-Safari-style bottom URL
    bars are also captured. 60-80 typical. 0 disables.

    Returns 0 on success (even with skipped/no-screenshot results),
    non-zero if any unrecoverable error occurred.
    """
    audits = _discover_audits(root)
    if not audits:
        log.warning("no audit.json files found under %s", root)
        return 0

    # Tesseract retired 2026-05-23; Haiku is the only engine.
    import env_check_haiku
    version_for_log = env_check_haiku.ENV_CHECK_VERSION
    log.info(
        "back-filling env_check (haiku v%d) across %d audit(s) "
        "under %s (workers=%d, dry_run=%s, force=%s, sample_stride=%d, "
        "region_crop_height=%d, region_crop_bottom_height=%d)",
        version_for_log, len(audits), root, workers, dry_run,
        force, sample_stride, region_crop_height, region_crop_bottom_height,
    )

    results: list[dict[str, Any]] = []
    # Haiku is I/O-bound on LLM Runtime — ThreadPoolExecutor is the right
    # parallelism strategy (vs ProcessPoolExecutor required by the
    # retired tesseract path because pytesseract was thread-unsafe).
    from concurrent.futures import ThreadPoolExecutor as _Executor
    with _Executor(max_workers=workers) as ex:
        futures = {
            ex.submit(
                _process_one, str(p),
                dry_run=dry_run, force=force,
                sample_stride=sample_stride,
                region_crop_height=region_crop_height,
                region_crop_bottom_height=region_crop_bottom_height,
                engine=engine, region=region, profile=profile,
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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root", type=Path, default=Path("output"),
        help="directory to scan for audit.json files (default: output)",
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
        "--engine", choices=["tesseract", "haiku"], default=None,
        help="DEPRECATED — tesseract retired 2026-05-23, haiku is the "
             "only engine. Flag accepted for back-compat with old "
             "scripts but `tesseract` falls through to `haiku` with a "
             "log warning.",
    )
    parser.add_argument(
        "--config", type=Path, default=None,
        help="path to config.toml (default: ./config.toml). Used to "
             "pick up region + cloud_profile for the haiku engine.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.root.exists():
        log.error("root path does not exist: %s", args.root)
        return 2

    # Region + profile come from config.toml; engine field is read
    # for completeness but ignored downstream (tesseract retired).
    import config as argus_config
    settings = argus_config.load(args.config)
    engine = args.engine or settings.auditor.env_check_engine or "haiku"
    region = settings.auditor.region
    profile = settings.auditor.cloud_profile

    # CLI flags default to 0/1 (full-scan, full-image OCR). When the
    # operator doesn't override on the command line, fall back to the
    # production config.toml values — otherwise backfill OCRs the whole
    # screenshot, which causes status-bar hover URLs to leak into the
    # `prod_urls_top` bucket and fire R9 instead of R9-hover. Live audits
    # already use the cropped values via auditor.AuditConfig; backfill
    # should match.
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
        region=region,
        profile=profile,
    )


if __name__ == "__main__":
    sys.exit(main())
