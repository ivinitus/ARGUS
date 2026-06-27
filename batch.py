"""Parallel batch runner.

After each audit lands, env_check fires into a side ThreadPool so every
audit.json gets env_check populated whether launched via argus.py folder
mode or batch.py standalone — no manual env_check_backfill needed.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import argus_control
import auditor
import config as argus_config
import extractor

log = logging.getLogger("argus.batch")


@dataclass
class KeyResult:
    key: str
    status: str  # "ok" | "skipped" | "failed" | "auth_failed"
    verdict: str | None = None      # only set on status="ok"
    findings: int | None = None     # only set on status="ok"
    reason: str | None = None       # set on status in {skipped, failed, auth_failed}
    # Best-effort: None when metadata.json wasn't written (extractor errored early).
    tester: str | None = None
    elapsed_s: float = 0.0
    # audit.json path for on_audit_complete callbacks (env_check etc.).
    audit_path: Path | None = None


def read_keys(stdin_: Iterable[str] | None, keys_file: Path | None) -> list[str]:
    """Read keys from stdin or a file. Blank lines and `#` comments skipped."""
    if keys_file is not None:
        lines = keys_file.read_text().splitlines()
    elif stdin_ is not None:
        lines = list(stdin_)
    else:
        return []
    keys: list[str] = []
    for raw in lines:
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        keys.append(s)
    # Dedupe while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            unique.append(k)
    return unique


def _has_any_evidence(work_dir: Path) -> bool:
    """True if there's any auditable evidence: PDF page images OR
    tester-attached step images. Short-circuits on the first hit.

    Previously only checked page_*.{jpg,png}, which missed mobile/webview
    playback tests where testers paste device screenshots directly under
    step_attachments/ (observed Targeted_regression_-08_May_2026 on 6 keys).
    """
    shots = work_dir / "screenshots"
    if not shots.exists():
        return False
    for _ in shots.rglob("page_*.jpg"):
        return True
    for _ in shots.rglob("page_*.png"):
        return True
    step_dir = shots / "step_attachments"
    if step_dir.is_dir():
        for p in step_dir.iterdir():
            if (p.is_file()
                    and p.suffix.lower() in {".jpg", ".jpeg", ".png",
                                             ".gif", ".webp"}):
                return True
    return False


def _read_tester(work_dir: Path) -> str | None:
    """Tester display name from metadata.json; falls back to raw userKey,
    None if metadata is missing/unreadable."""
    meta_path = work_dir / "metadata.json"
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    results = meta.get("test_results") or []
    if not results:
        return None
    tr = results[0]
    return tr.get("executed_by_name") or tr.get("executed_by")


def _write_no_evidence_audit(
    work_dir: Path,
    key: str,
    tester: str | None,
) -> None:
    """Synthetic fail-verdict audit.json for zero-evidence submissions.

    Puts the key in the same compliance funnel as any other failure
    (ARGUS scans audit.json files). Seeded with R12; no LLM Runtime call;
    schema_version + variant_name match auditor.run's output shape.
    """
    audit_path = work_dir / "audit.json"
    md_path = work_dir / "audit.md"
    name_for_msg = f"Tester '{tester}' " if tester else "The tester "
    finding = {
        "severity": "high",
        "page": None,
        "step_index": None,
        "description": (
            f"{name_for_msg}submitted this execution with NO evidence "
            "attached — no session PDF, no per-step screenshots, no "
            "images of any kind. Per compliance policy a test execution "
            "must include screenshots evidencing each step's expected "
            "result. Without any evidence the audit cannot verify the "
            "test was performed at all. The tester must re-submit with "
            "a session PDF or per-step images attached, or the "
            "execution must be re-run."
        ),
        "source": "rule",
        "rule": "R12",
    }
    audit = {
        "overall_verdict": "fail",
        "summary": (
            "No evidence submitted. The tester did not attach a session "
            "PDF or any per-step screenshots. The audit pipeline cannot "
            "verify the test was performed — re-submit with evidence."
        ),
        "findings": [finding],
        "schema_version": auditor.AUDIT_SCHEMA_VERSION,
        "variant_name": "no-evidence",  # not a model variant; flags origin
    }
    # R12 + R16 fire together (independent gaps: no proof vs no explanation).
    metadata: dict = {}
    meta_path = work_dir / "metadata.json"
    if meta_path.exists():
        try:
            metadata = json.loads(meta_path.read_text())
        except (OSError, json.JSONDecodeError):
            metadata = {}

    # Run rule engine alongside R12 (R6/R11/R16 etc.). Local import to
    # avoid cycles; rule-engine failure must not block the audit write.
    try:
        import workflow_rules as _wr
        rule_findings = _wr.run_workflow_rules(metadata) if metadata else []
        if rule_findings:
            audit["findings"].extend(rule_findings)
    except Exception:  # pragma: no cover
        pass

    audit = auditor.enrich_audit(audit, metadata)
    audit_path.write_text(json.dumps(audit, indent=2))
    try:
        md_path.write_text(auditor.render_markdown(audit, metadata))
    except Exception:  # pragma: no cover
        pass


def _write_not_applicable_audit(
    work_dir: Path,
    key: str,
) -> None:
    """Synthetic audit.json for N/A executions.

    Surfaces an N/A item in the main table (alongside Pass/Fail/PWI)
    instead of only in the R15a coverage drawer. Seeded with R18 (high
    if no comment + no traceLinks, else medium); full rule engine runs
    so R6/R11/R16/R17 fire when applicable. No LLM Runtime, no env_check.
    """
    audit_path = work_dir / "audit.json"
    md_path = work_dir / "audit.md"
    metadata: dict = {}
    meta_path = work_dir / "metadata.json"
    if meta_path.exists():
        try:
            metadata = json.loads(meta_path.read_text())
        except (OSError, json.JSONDecodeError):
            metadata = {}

    findings: list[dict] = []
    try:
        import workflow_rules as _wr
        r18 = _wr.build_r18_finding(metadata)
        if r18:
            findings.append(r18)
        rule_findings = _wr.run_workflow_rules(metadata) if metadata else []
        findings.extend(rule_findings)
        try:
            import history_index as _hi
            hidx = _hi.HistoryIndex.load_or_build(work_dir.parent.parent.parent)
            r17 = _wr.check_immediate_prior_executed(metadata, hidx)
            if r17:
                findings.extend(r17)
        except Exception:
            pass  # history-aware checks are best-effort
    except Exception:  # pragma: no cover
        pass

    # Verdict: high => fail, any finding => concerns, else pass.
    if any(f.get("severity") == "high" for f in findings):
        verdict = "fail"
    elif findings:
        verdict = "concerns"
    else:
        verdict = "pass"

    audit = {
        "overall_verdict": verdict,
        "summary": (
            f"Test case marked Not Applicable. "
            + ("Tester provided no documentation explaining the "
               "determination — review whether the N/A is legitimate "
               "or a side-step."
               if any(f.get("rule") == "R18"
                      and f.get("severity") == "high" for f in findings)
               else "See findings for any related compliance signals.")
        ),
        "findings": findings,
        "schema_version": auditor.AUDIT_SCHEMA_VERSION,
        # Distinct from "no-evidence" so the dashboard renders its own pill.
        "variant_name": "not-applicable",
    }
    audit = auditor.enrich_audit(audit, metadata)
    audit_path.write_text(json.dumps(audit, indent=2))
    try:
        md_path.write_text(auditor.render_markdown(audit, metadata))
    except Exception:  # pragma: no cover
        pass


def _run_env_check_for_key(
    result: "KeyResult",
    settings: argus_config.ARGUSConfig,
) -> None:
    """Run env_check on one audit and merge the result into audit.json.

    Idempotent (no-op if env_check is already at current version).
    Errors propagate to the env_check pool's future for per-key logging.
    """
    audit_path = result.audit_path
    if audit_path is None or not audit_path.exists():
        return
    audit = json.loads(audit_path.read_text())
    execution_dir = audit_path.parent

    # Tesseract retired 2026-05-23; only Haiku remains.
    import env_check_haiku as engine_module

    existing = audit.get("env_check") or {}
    if existing.get("version") == engine_module.ENV_CHECK_VERSION:
        return  # already done at current version

    # Path heuristic: Webview folders trigger BOTH the R9-amb
    # finding (when ambiguous) AND a tighter sample_stride so we
    # scan more pages there — those folders are where env
    # compliance ambiguity matters most.
    is_webview = auditor._is_webview_path(execution_dir)
    base_stride = settings.auditor.env_check_sample_stride
    stride = max(2, base_stride - 1) if is_webview and base_stride > 1 else base_stride

    ec = engine_module.check_env_compliance(
        execution_dir,
        sample_stride=stride,
        region_crop_height=settings.auditor.env_check_region_crop_height,
        region_crop_bottom_height=settings.auditor.env_check_region_crop_bottom_height,
        region=settings.auditor.region,
        profile=settings.auditor.cloud_profile,
        is_webview=is_webview,
    )

    # Drop stale R9/R9-amb/R13 — all env_check-derived and re-emitted below.
    findings = list(audit.get("findings") or [])
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
        "images_total": ec.get("images_total", ec["images_scanned"]),
        "sample_stride": ec.get("sample_stride", 1),
        "region_crop_height": ec.get("region_crop_height", 0),
        "region_crop_bottom_height": ec.get("region_crop_bottom_height", 0),
        "per_image": ec["per_image"],
    }
    if ec["findings"]:
        audit = auditor.merge_rule_findings(audit, ec["findings"])
    # R13 marketplace check (deterministic, local metadata.json read).
    import workflow_rules
    metadata_path = execution_dir / "metadata.json"
    metadata: dict = {}
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text())
        except (OSError, json.JSONDecodeError):
            metadata = {}
    r13 = workflow_rules.check_marketplace_match(metadata, audit)
    if r13:
        audit = auditor.merge_rule_findings(audit, r13)

    if audit.get("schema_version", 0) < auditor.AUDIT_SCHEMA_VERSION:
        audit["schema_version"] = auditor.AUDIT_SCHEMA_VERSION

    audit = auditor.enrich_audit(audit, metadata)
    audit_path.write_text(json.dumps(audit, indent=2))


def _process_key(
    key: str,
    settings: argus_config.ARGUSConfig,
    extractor_sem: threading.Semaphore,
) -> KeyResult:
    """Extract + audit one key end-to-end."""
    t0 = time.monotonic()
    # /argus pause: in-flight LLM Runtime calls can't be cancelled, so the
    # practical worst case is one-audit-duration (~75s) before pause hits.
    if argus_control.is_paused():
        return KeyResult(
            key=key, status="skipped",
            reason="argus paused via /argus pause",
            elapsed_s=time.monotonic() - t0,
        )
    ex_cfg = extractor.ExtractorConfig(
        execution_key=key,
        out_dir=settings.extractor.out_dir,
        base_url=settings.extractor.base_url,
    )

    # Throttle Tracker side.
    with extractor_sem:
        rc, work_dir = extractor.run(ex_cfg)
    if rc != 0 or work_dir is None:
        return KeyResult(key=key, status="failed",
                         reason=f"extractor rc={rc}",
                         elapsed_s=time.monotonic() - t0)

    tester = _read_tester(work_dir)

    # No evidence: write a synthetic R12 fail-audit so the key is visible
    # in ARGUS. KeyResult stays "skipped" so retry tooling sees it correctly.
    if not _has_any_evidence(work_dir):
        _write_no_evidence_audit(work_dir, key, tester)
        return KeyResult(key=key, status="skipped",
                         reason="no PDF or image attachments",
                         tester=tester,
                         audit_path=work_dir / "audit.json",
                         elapsed_s=time.monotonic() - t0)

    au_cfg = auditor.AuditConfig(
        execution_dir=work_dir,
        model_provider=settings.auditor.model_provider,
        model_id=settings.auditor.model_id,
        region=settings.auditor.region,
        cloud_profile=settings.auditor.cloud_profile,
        api_base_url=settings.auditor.api_base_url,
        api_key_env=settings.auditor.api_key_env,
        max_pages=settings.auditor.max_pages,
        temperature=settings.auditor.temperature,
        consensus_enabled=settings.auditor.consensus_enabled,
        debug_output=settings.auditor.debug_output,
        chunk_max_parallel=settings.auditor.chunk_max_parallel,
        env_check_inline=settings.auditor.env_check_inline,
        env_check_sample_stride=settings.auditor.env_check_sample_stride,
        env_check_region_crop_height=settings.auditor.env_check_region_crop_height,
        env_check_region_crop_bottom_height=settings.auditor.env_check_region_crop_bottom_height,
        env_check_engine=settings.auditor.env_check_engine,
    )
    rc = auditor.run(au_cfg)
    if rc != 0:
        return KeyResult(key=key, status="failed",
                         reason=f"auditor rc={rc}",
                         tester=tester,
                         elapsed_s=time.monotonic() - t0)

    # Read the audit back so the progress line can show verdict + finding count.
    verdict: str | None = None
    findings: int | None = None
    audit_path = work_dir / "audit.json"
    try:
        audit = json.loads(audit_path.read_text())
        verdict = audit.get("overall_verdict")
        findings = len(audit.get("findings") or [])
    except (OSError, json.JSONDecodeError):
        pass  # audit ran but summary unreadable — still count as success

    return KeyResult(key=key, status="ok", verdict=verdict, findings=findings,
                     tester=tester, elapsed_s=time.monotonic() - t0,
                     audit_path=audit_path)


def run_batch(
    keys: list[str],
    settings: argus_config.ARGUSConfig,
    concurrency: int,
    extractor_concurrency: int,
    out: object = sys.stdout,
    on_audit_complete=None,
    env_check_auto: bool = True,
) -> list[KeyResult]:
    """Run `keys` in parallel; one progress line per completion.

    env_check_auto=True spins an private pool that fires env_check on
    every successful audit and is drained before return, so the caller
    sees fully populated audit.json. False disables (tests, or when
    env_check shouldn't run).

    on_audit_complete(result) runs on the as_completed loop thread —
    MUST NOT block. Exceptions are caught + logged. Adds to env_check;
    doesn't replace it.
    """
    # Clamp to [1, concurrency] before semaphore creation; surface clamped
    # value so the upstream header isn't silently inaccurate.
    requested_extractor = extractor_concurrency
    extractor_concurrency = max(1, min(extractor_concurrency, concurrency))
    if extractor_concurrency != requested_extractor:
        print(
            f"batch: extractor_concurrency={requested_extractor} clamped to "
            f"{extractor_concurrency} (must be in [1, concurrency])",
            file=out,
        )
        getattr(out, "flush", lambda: None)()
    extractor_sem = threading.Semaphore(extractor_concurrency)
    results: list[KeyResult] = []
    total = len(keys)

    # Auto env_check pool. Sized for haiku (I/O-bound, benefits from 2x
    # batch concurrency); tesseract (CPU-bound) just matches batch
    # concurrency. The pool is created up-front so the callback path
    # below can submit into it cheaply.
    env_pool: ThreadPoolExecutor | None = None
    env_futs: list = []
    if env_check_auto:
        if settings.auditor.env_check_engine == "haiku":
            env_workers = max(4, concurrency * 2)
        else:
            env_workers = concurrency
        env_pool = ThreadPoolExecutor(
            max_workers=env_workers, thread_name_prefix="env_check")

    try:
        with ThreadPoolExecutor(
                max_workers=concurrency, thread_name_prefix="argus") as ex:
            futures = {
                ex.submit(_process_key, k, settings, extractor_sem): k
                for k in keys
            }
            for fut in as_completed(futures):
                key = futures[fut]
                try:
                    res = fut.result()
                except extractor.AuthError as e:
                    res = KeyResult(key=key, status="auth_failed",
                                    reason=str(e))
                except Exception as e:
                    res = KeyResult(key=key, status="failed",
                                    reason=f"{e.__class__.__name__}: {e}")
                results.append(res)
                _print_progress(res, len(results), total, out=out)
                # Skip non-ok results (no audit.json to update).
                if env_pool is not None and res.status == "ok":
                    env_futs.append(
                        env_pool.submit(_run_env_check_for_key,
                                        res, settings)
                    )
                if on_audit_complete is not None and res.status == "ok":
                    try:
                        on_audit_complete(res)
                    except Exception as cb_err:
                        print(f"[batch] on_audit_complete raised "
                              f"{type(cb_err).__name__}: {cb_err} "
                              f"for {res.key}", file=out)
    finally:
        # Drain so callers see fully populated audit.json. With haiku
        # ~5s/call the tail is 5-15s.
        if env_pool is not None:
            n_done = 0
            n_failed = 0
            n_timed_out = 0
            for fut in env_futs:
                try:
                    fut.result(timeout=300)
                    n_done += 1
                except TimeoutError:
                    # The per-future timeout fired but the underlying task is
                    # STILL RUNNING. A plain shutdown(wait=True) below would
                    # then block on that same wedged task with no timeout,
                    # hanging the whole cron run indefinitely. Track it so we
                    # can shut down non-blocking instead.
                    n_timed_out += 1
                    print("[batch] env_check timed out after 300s "
                          "(task still running; will not block shutdown)",
                          file=out)
                except Exception as e:
                    n_failed += 1
                    print(f"[batch] env_check raised "
                          f"{type(e).__name__}: {e}", file=out)
            if env_futs:
                msg = (f"[batch] env_check completed for "
                       f"{n_done}/{len(env_futs)} keys")
                if n_failed:
                    msg += f" ({n_failed} failed — see warnings above)"
                if n_timed_out:
                    msg += f" ({n_timed_out} timed out)"
                print(msg, file=out)
            # If a task wedged, don't block forever waiting on it. Cancel
            # what hasn't started; let the daemon/process exit reap the rest.
            env_pool.shutdown(wait=(n_timed_out == 0), cancel_futures=True)

    return results


def _print_progress(r: KeyResult, done: int, total: int, out: object) -> None:
    if r.status == "ok":
        n = r.findings if r.findings is not None else 0
        v = r.verdict or "?"
        line = (f"[{done}/{total}] {r.key} ok "
                f"({v}, {n} finding{'s' if n != 1 else ''}, {r.elapsed_s:0.1f}s)")
    elif r.status == "skipped":
        line = f"[{done}/{total}] {r.key} skipped ({r.reason or 'no screenshots'})"
    elif r.status == "auth_failed":
        line = f"[{done}/{total}] {r.key} AUTH_FAILED  {r.reason or ''}"
    else:
        line = f"[{done}/{total}] {r.key} FAILED  {r.reason or ''}"
    print(line, file=out)
    getattr(out, "flush", lambda: None)()


def write_failed_keys(results: list[KeyResult], out_dir: Path) -> Path | None:
    """Write failed keys to <out_dir>/_failed_keys.txt for retry.

    Excludes "skipped" — those would skip again on retry.
    """
    failed = [r.key for r in results if r.status in ("failed", "auth_failed")]
    if not failed:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "_failed_keys.txt"
    path.write_text("\n".join(failed) + "\n")
    return path


def format_skipped_by_tester(results: list[KeyResult]) -> str | None:
    """Skipped-keys block grouped by tester (largest group first).

    Returns None when nothing was skipped. Unknown tester => "unknown".
    """
    skipped = [r for r in results if r.status == "skipped"]
    if not skipped:
        return None
    groups: dict[str, list[str]] = {}
    for r in skipped:
        name = r.tester or "unknown"
        groups.setdefault(name, []).append(r.key)
    lines = ["needs evidence (tester didn't attach any PDF or image):"]
    for name in sorted(groups, key=lambda n: (-len(groups[n]), n.lower())):
        ks = groups[name]
        lines.append(f"  {name} ({len(ks)}): {', '.join(ks)}")
    return "\n".join(lines)


def summarize(results: list[KeyResult]) -> str:
    ok = sum(1 for r in results if r.status == "ok")
    skipped = sum(1 for r in results if r.status == "skipped")
    failed = sum(1 for r in results if r.status == "failed")
    auth = sum(1 for r in results if r.status == "auth_failed")
    parts = [f"{ok} succeeded"]
    if skipped:
        parts.append(f"{skipped} skipped")
    parts.append(f"{failed} failed")
    parts.append(f"{auth} auth-failed")
    return ", ".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ARGUS parallel batch runner")
    parser.add_argument("--keys-file", type=Path,
                        help="file with one execution key per line "
                             "(default: read from stdin)")
    parser.add_argument("--config", type=Path, default=None,
                        help="path to config.toml (default: ./config.toml)")
    parser.add_argument("--concurrency", type=int, default=None,
                        help="total parallel keys (overrides config.toml)")
    parser.add_argument("--extractor-concurrency", type=int, default=None,
                        help="parallel extract calls (overrides config.toml)")
    parser.add_argument("--debug", action="store_true",
                        help="include per-chunk breakdown and raw diagnostics "
                             "in audit.md (audit.json always carries full data)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    settings = argus_config.load(args.config)
    extractor.configure_logging(args.verbose or settings.logging.verbose)

    # CLI --debug wins over config.toml's audit.debug_output.
    if args.debug:
        settings.auditor.debug_output = True

    concurrency = args.concurrency or settings.batch.concurrency
    extractor_concurrency = args.extractor_concurrency or settings.batch.extractor_concurrency

    if args.keys_file:
        keys = read_keys(None, args.keys_file)
    else:
        if sys.stdin.isatty():
            parser.error("no keys: provide --keys-file or pipe keys via stdin")
        keys = read_keys(sys.stdin, None)

    if not keys:
        print("batch: no keys to process", file=sys.stderr)
        return 1

    print(f"batch: {len(keys)} keys, concurrency={concurrency} "
          f"(extractor={extractor_concurrency})", file=sys.stderr)

    results = run_batch(keys, settings, concurrency, extractor_concurrency, out=sys.stderr)

    failed_path = write_failed_keys(results, settings.extractor.out_dir)
    summary = summarize(results)
    print(f"batch: {summary}", file=sys.stderr)
    skipped_block = format_skipped_by_tester(results)
    if skipped_block:
        print(skipped_block, file=sys.stderr)
    if failed_path:
        print(f"batch: failed keys -> {failed_path} "
              f"(retry: python batch.py --keys-file {failed_path})",
              file=sys.stderr)

    # rc=2 auth failure (extractor convention), 1 other failure, 0 clean.
    if any(r.status == "auth_failed" for r in results):
        return 2
    if any(r.status == "failed" for r in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
