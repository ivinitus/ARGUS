"""Replay existing audits with a named prompt variant for measured A/B.

Motivation
----------
Zero "fail" verdicts across 145 audits is a calibration problem (the
SYSTEM_PROMPT leads with "be lenient"), not a capability problem — the
model catches real contradictions (UK running US-only step, wrong credit
count, order-rejected vs. success) but labels them medium. Fixing it
means trying a new system prompt and MEASURING whether the change
improves accuracy, rather than swapping prompts blind.

`replay.py` runs `auditor.run()` against an existing execution dir
(already has metadata.json + screenshots) with a named prompt variant.
It writes output to `<exec_dir>/_replay/<variant>/audit.json` and
`audit.md`, never overwriting the canonical `audit.json`. Each variant
gets its own subdir so you can keep V1, V2, … side-by-side for later
comparison.

Variants are registered as `(name, prompt_text)` pairs in
`VARIANTS`. `--variant <name>` picks one. The canonical production
prompt is registered as `v1` so running `--variant v1` reproduces the
original baseline under the replay infrastructure (useful to re-audit
at the same temperature with the same code path both variants will use).

CLI
---
    # Replay one key with variant v2
    python replay.py replay --variant v2 QA-E174988

    # Replay a list of keys from a file (same format as _failed_keys.txt)
    python replay.py replay --variant v2 --keys-file replay_sample.txt

    # Point at a different output tree (default: config.toml extractor.out_dir)
    python replay.py replay --variant v2 --out-dir output/ \
        --keys-file sample.txt

    # Compare two finished replays (writes <out_dir>/_replay_compare.md)
    python replay.py compare v1 v2 --keys-file replay_sample.txt

Guarantees
----------
- Canonical audit.json is never touched. Replay writes are confined to
  `_replay/<variant>/` subdirs.
- Existing `_last_error.json` handling still applies, but scoped to the
  replay subdir so replays don't pollute the canonical failure signal.
- Uses the same `auditor.run()` code path as production: schema
  validation, consensus, chunking, workflow-rule merging. The ONLY
  thing that changes between variants is the system prompt.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import auditor
import config as argus_config


# ---------------------------------------------------------------------------
# Variant registry
# ---------------------------------------------------------------------------
# Map variant_name -> prompt_text. Adding a new variant is a two-line
# change: pick a name, paste the prompt. None is still honoured as a
# "use auditor.SYSTEM_PROMPT at resolve time" sentinel (useful for an
# experiment that specifically wants to track whatever is currently
# canonical), but the registered v1 below deliberately points at
# SYSTEM_PROMPT_V1 instead — v1 is a historical snapshot, it should
# never shift when the canonical changes.
VARIANTS: dict[str, str | None] = {
    # v1 — the pre-2026-05-09 canonical baseline. Pointed at
    # SYSTEM_PROMPT_V1 explicitly (not the None sentinel) so that after
    # SYSTEM_PROMPT was retargeted from V1 to V4, v1 replays keep
    # sending the ORIGINAL V1 text. This is what keeps
    # `replay compare v1 v4` truthful indefinitely — the promotion
    # event doesn't silently re-label history.
    "v1": auditor.SYSTEM_PROMPT_V1,
    # v2 — DEPRECATED. Pre-policy severity-ladder draft. Kept registered
    # so historical A/Bs (`replay compare v2 v3`) remain runnable and
    # the evolution of the prompt is traceable.
    "v2": auditor.SYSTEM_PROMPT_V2,
    # v3 — prior-generation policy-grounded variant. Supplanted by v4.
    # Kept registered so `replay compare v3 v4` works for the targeted
    # delta validation.
    "v3": auditor.SYSTEM_PROMPT_V3,
    # v4 — CURRENT production canonical as of 2026-05-09
    # (auditor.SYSTEM_PROMPT == SYSTEM_PROMPT_V4). Registered here so
    # explicit `replay --variant v4` still works and produces output
    # under `_replay/v4/` alongside the canonical audit.json. Useful
    # for re-auditing stored V1-era canonical artefacts against the
    # current V4 prompt without overwriting them.
    "v4": auditor.SYSTEM_PROMPT_V4,
    # v5 — V4 + four targeted FP-suppression deltas (DevTools-emulation
    # exception, email/purchase-history exception, locale-text
    # exception, Pass+Blocked don't-flag bullet). Registered for
    # replay/A-B but NOT promoted to canonical until validated
    # against the 145-audit corpus. See SYSTEM_PROMPT_V5 docstring
    # in auditor.py for the full delta list and rationale.
    "v5": auditor.SYSTEM_PROMPT_V5,
    # v5_4 — V5 trimmed (~50% prose reduction, all directives
    # preserved). Registered for failure-mode-harness validation
    # before promotion. See SYSTEM_PROMPT_V5_4 docstring.
    # FAILED harness 2026-05-24: dropped HIGH catches on E178885,
    # E178891, E178936; lost locale-translation suppress on E178973.
    # Kept registered for diagnostic A/B; NOT a promotion candidate.
    "v5_4": auditor.SYSTEM_PROMPT_V5_4,
    # v5_5 — V5.4 with the 3 regressing cuts surgically restored:
    # locale-translation worked example (French/English),
    # explicit "stay HIGH" anchors on direct-contradiction bullets,
    # payment-method/product-variant case in wrong-outcome bullet.
    # ALSO FAILED harness 2026-05-24 (E178885 cash→Visa still lost,
    # E178891 still caught only 1 of 2 parallel HIGHs). Suggests V5's
    # prompt density carries weight beyond the literal directives.
    # Kept registered as a diagnostic artefact; NOT a promotion
    # candidate. See SYSTEM_PROMPT_V5_5 docstring in auditor.py.
    "v5_5": auditor.SYSTEM_PROMPT_V5_5,
}


def _resolve_variant(name: str) -> str | None:
    """Return the system prompt text for `name`, or raise.

    None is a valid sentinel meaning "use the auditor module default" —
    that's how we register the baseline v1 variant without duplicating
    the prompt string.
    """
    if name not in VARIANTS:
        known = ", ".join(sorted(VARIANTS.keys()))
        raise ValueError(
            f"unknown variant {name!r}; known variants: {known}"
        )
    return VARIANTS[name]


# ---------------------------------------------------------------------------
# Core replay
# ---------------------------------------------------------------------------
def _find_execution_dir(out_dir: Path, key: str) -> Path | None:
    """Locate <out_dir>/.../<key>/ across all layouts the extractor has
    produced (flat, testrun-slug, per-tester). Mirrors the lookup in
    argus._find_existing_workdir so replay and single-key workflows
    agree on which directory belongs to a given E-key."""
    if not out_dir.exists():
        return None
    flat = out_dir / key
    if flat.is_dir():
        return flat
    for candidate in out_dir.rglob(key):
        if candidate.is_dir():
            return candidate
    return None


def replay_audit(
    exec_dir: Path,
    variant_name: str,
    settings: argus_config.ARGUSConfig | None = None,
) -> int:
    """Run `auditor.run()` against `exec_dir` with the named prompt
    variant. Writes outputs to `<exec_dir>/_replay/<variant_name>/`.

    Returns the auditor return code (0 on success, 1 on missing inputs).
    LLM Runtime exceptions propagate up — the caller decides whether to
    keep replaying the rest of the list or abort.
    """
    settings = settings or argus_config.load()
    system_prompt = _resolve_variant(variant_name)
    output_dir = exec_dir / "_replay" / variant_name
    cfg = auditor.AuditConfig(
        execution_dir=exec_dir,
        output_dir=output_dir,
        model_provider=settings.auditor.model_provider,
        model_id=settings.auditor.model_id,
        region=settings.auditor.region,
        cloud_profile=settings.auditor.cloud_profile,
        api_base_url=settings.auditor.api_base_url,
        api_key_env=settings.auditor.api_key_env,
        max_pages=settings.auditor.max_pages,
        temperature=settings.auditor.temperature,
        system_prompt=system_prompt,
        # Stamp the variant name into audit.json so replayed outputs
        # self-describe their provenance — same guarantee the canonical
        # path gives via auditor.AuditConfig.variant_name default.
        variant_name=variant_name,
        debug_output=settings.auditor.debug_output,
        chunk_max_parallel=settings.auditor.chunk_max_parallel,
        env_check_inline=settings.auditor.env_check_inline,
        env_check_sample_stride=settings.auditor.env_check_sample_stride,
        env_check_region_crop_height=settings.auditor.env_check_region_crop_height,
        env_check_region_crop_bottom_height=settings.auditor.env_check_region_crop_bottom_height,
        env_check_engine=settings.auditor.env_check_engine,
    )
    return auditor.run(cfg)


# ---------------------------------------------------------------------------
# Compare
# ---------------------------------------------------------------------------
@dataclass
class PerKeyDiff:
    """Single-key diff between two replay variants.

    Fields are populated best-effort — a variant with a missing audit
    (replay wasn't run, or it errored) is represented by a None verdict
    and empty finding list so the aggregate rollup can still count it.
    """
    key: str
    verdict_a: str | None
    verdict_b: str | None
    sev_a: dict[str, int]
    sev_b: dict[str, int]
    # Finding-key signatures present in A but not B (and vice versa).
    # Signatures match what auditor._finding_key produces (severity +
    # step/page anchor), so description wording differences don't count
    # as distinct findings.
    only_in_a: list[str]
    only_in_b: list[str]


def _finding_signature(f: dict) -> str:
    """Human-readable finding key matching auditor._finding_key semantics.

    We don't import _finding_key directly because it returns a tuple
    that includes id() for unanchored findings, which isn't stable
    across runs. For compare purposes we want a stable string that
    tolerates severity/step/page variations the way consensus does.
    """
    sev = f.get("severity", "?")
    step = f.get("step_index")
    page = f.get("page")
    if step is not None:
        return f"{sev}|step:{step}"
    if page is not None:
        return f"{sev}|page:{page}"
    # Unanchored — fall back to description prefix so two runs emitting
    # the same unlocated finding match. Shorter than a full hash but
    # stable enough for compare.
    desc = (f.get("description") or "").strip()[:60]
    return f"{sev}|anon:{desc}"


def _load_variant_audit(exec_dir: Path, variant: str) -> dict | None:
    """Return the parsed audit.json for a variant, or None if missing."""
    p = exec_dir / "_replay" / variant / "audit.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _sev_counts(audit: dict | None) -> dict[str, int]:
    """Severity histogram for an audit dict — zeros when audit is None."""
    out = {"high": 0, "medium": 0, "low": 0}
    if not audit:
        return out
    for f in audit.get("findings") or []:
        s = f.get("severity")
        if s in out:
            out[s] += 1
    return out


def compute_diff(
    exec_dir: Path,
    variant_a: str,
    variant_b: str,
) -> PerKeyDiff:
    """Compute a PerKeyDiff for one execution between two variants."""
    a = _load_variant_audit(exec_dir, variant_a)
    b = _load_variant_audit(exec_dir, variant_b)
    a_sigs = {_finding_signature(f) for f in (a.get("findings") or [])} \
        if a else set()
    b_sigs = {_finding_signature(f) for f in (b.get("findings") or [])} \
        if b else set()
    return PerKeyDiff(
        key=exec_dir.name,
        verdict_a=a.get("overall_verdict") if a else None,
        verdict_b=b.get("overall_verdict") if b else None,
        sev_a=_sev_counts(a),
        sev_b=_sev_counts(b),
        only_in_a=sorted(a_sigs - b_sigs),
        only_in_b=sorted(b_sigs - a_sigs),
    )


def render_compare_report(
    diffs: list[PerKeyDiff],
    variant_a: str,
    variant_b: str,
) -> str:
    """Render a markdown comparison report.

    Structure:
      - Aggregate rollup: verdict transitions (pass→concerns, etc.),
        severity promotion/demotion counts, unchanged count.
      - Per-key detail: one bullet per key showing verdict + severity
        changes and the signatures newly introduced or removed by B.
    """
    # Aggregate rollups.
    verdict_transitions = collections.Counter()
    promotions = 0
    demotions = 0
    unchanged = 0
    for d in diffs:
        key = (d.verdict_a or "missing", d.verdict_b or "missing")
        verdict_transitions[key] += 1
        # Severity promotion: B has a 'high' or 'medium' finding for a
        # signature that was 'low' or 'medium' in A. Simpler measurable
        # proxy: B has more high findings than A.
        if d.sev_b["high"] > d.sev_a["high"]:
            promotions += 1
        elif d.sev_b["high"] < d.sev_a["high"]:
            demotions += 1
        if (d.verdict_a == d.verdict_b and d.sev_a == d.sev_b
                and not d.only_in_a and not d.only_in_b):
            unchanged += 1

    lines = [
        f"# Replay comparison: {variant_a} vs {variant_b}",
        "",
        f"- **Keys compared:** {len(diffs)}",
        f"- **Unchanged:** {unchanged}",
        f"- **High-severity promotions ({variant_b} added highs):** "
        f"{promotions}",
        f"- **High-severity demotions ({variant_b} removed highs):** "
        f"{demotions}",
        "",
        "## Verdict transitions",
        "",
        "| From (" + variant_a + ") | To (" + variant_b + ") | Count |",
        "| --- | --- | ---: |",
    ]
    for (va, vb), n in sorted(
        verdict_transitions.items(),
        # Sort by count desc, then alpha for stability.
        key=lambda kv: (-kv[1], kv[0]),
    ):
        lines.append(f"| {va} | {vb} | {n} |")

    lines += ["", "## Per-key detail", ""]
    for d in sorted(diffs, key=lambda d: d.key):
        lines.append(
            f"### {d.key}"
        )
        lines.append("")
        lines.append(
            f"- Verdict: **{d.verdict_a or 'missing'}** → "
            f"**{d.verdict_b or 'missing'}**"
        )
        lines.append(
            f"- Severity: A {_fmt_sev(d.sev_a)} → "
            f"B {_fmt_sev(d.sev_b)}"
        )
        if d.only_in_a:
            lines.append(f"- Only in {variant_a} ({len(d.only_in_a)}): "
                         f"{', '.join(d.only_in_a)}")
        if d.only_in_b:
            lines.append(f"- Only in {variant_b} ({len(d.only_in_b)}): "
                         f"{', '.join(d.only_in_b)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _fmt_sev(counts: dict[str, int]) -> str:
    return f"H={counts['high']} M={counts['medium']} L={counts['low']}"


# ---------------------------------------------------------------------------
# Key-list helpers (shared with batch.py style)
# ---------------------------------------------------------------------------
def _read_keys(keys_file: Path | None, args_keys: list[str]) -> list[str]:
    """Return deduped keys preserving insertion order.

    Pulls from either `args_keys` (positional) or `keys_file` (one per
    line). When reading a file, both full-line `#` comments AND inline
    trailing `# ...` comments are stripped so sample files can be
    self-documenting (e.g., `QA-E174988  # UK/US mismatch`).
    Blank lines after stripping are skipped. When both sources are
    provided, positional wins; we don't support merging because the two
    sources have different semantics.
    """
    keys: list[str] = []
    if args_keys:
        keys = list(args_keys)
    elif keys_file is not None:
        for raw in keys_file.read_text().splitlines():
            # Strip inline comments first (everything from # onwards)
            # so `QA-E1  # note` produces just `QA-E1`.
            # Full-line comments collapse to empty and drop out below.
            hash_idx = raw.find("#")
            if hash_idx >= 0:
                raw = raw[:hash_idx]
            s = raw.strip()
            if not s:
                continue
            keys.append(s)
    seen: set[str] = set()
    out: list[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _cmd_replay(args: argparse.Namespace) -> int:
    """Handle `python replay.py --variant V keys...` (the default subcommand)."""
    settings = argus_config.load(args.config)
    out_dir = args.out_dir or settings.extractor.out_dir
    keys = _read_keys(args.keys_file, args.keys)
    if not keys:
        print("replay: no keys to process (use --keys-file or positional keys)",
              file=sys.stderr)
        return 1
    try:
        _resolve_variant(args.variant)
    except ValueError as e:
        print(f"replay: {e}", file=sys.stderr)
        return 1

    total = len(keys)

    # Resolve all exec_dirs up front so we can SKIP the missing ones
    # without dispatching to the worker pool, and so the work submitted
    # to the pool is well-formed.
    plan: list[tuple[int, str, "Path | None"]] = []
    skipped = 0
    for i, key in enumerate(keys, start=1):
        exec_dir = _find_execution_dir(out_dir, key)
        if exec_dir is None:
            print(f"[{i}/{total}] {key} SKIPPED (not found under {out_dir})",
                  file=sys.stderr)
            skipped += 1
        plan.append((i, key, exec_dir))

    work = [(i, k, ed) for (i, k, ed) in plan if ed is not None]
    workers = max(1, min(args.workers, len(work))) if work else 1
    if workers > 1:
        print(f"replay({args.variant}): dispatching {len(work)} keys "
              f"with {workers} workers", file=sys.stderr)

    ok = failed = 0
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _run_one(idx: int, key: str, exec_dir):
        try:
            rc = replay_audit(exec_dir, args.variant, settings=settings)
            return (idx, key, exec_dir, rc, None)
        except Exception as e:
            return (idx, key, exec_dir, None, e)

    if workers == 1 or len(work) <= 1:
        # Preserve pre-parallel behaviour exactly when workers=1 — order
        # matches the input keys file. Logging path is the same as the
        # pool path so output shape doesn't drift between modes.
        for (i, key, exec_dir) in work:
            idx, key, exec_dir, rc, exc = _run_one(i, key, exec_dir)
            if exc is not None:
                print(f"[{idx}/{total}] {key} FAILED: "
                      f"{exc.__class__.__name__}: {exc}",
                      file=sys.stderr)
                failed += 1
            elif rc == 0:
                ok += 1
                print(f"[{idx}/{total}] {key} ok -> "
                      f"{exec_dir / '_replay' / args.variant / 'audit.json'}",
                      file=sys.stderr)
            else:
                failed += 1
                print(f"[{idx}/{total}] {key} rc={rc}", file=sys.stderr)
    else:
        # Parallel dispatch. LLM Runtime has its own adaptive retries inside
        # invoke_model, so this just controls how many keys are in
        # flight at once. Keep this <= settings.batch.concurrency to stay
        # within the bench-validated TPM ceiling — replay shares the same
        # account.
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="replay") as ex:
            futs = {ex.submit(_run_one, i, k, ed): (i, k)
                    for (i, k, ed) in work}
            for fut in as_completed(futs):
                idx, key, exec_dir, rc, exc = fut.result()
                if exc is not None:
                    print(f"[{idx}/{total}] {key} FAILED: "
                          f"{exc.__class__.__name__}: {exc}",
                          file=sys.stderr)
                    failed += 1
                elif rc == 0:
                    ok += 1
                    print(f"[{idx}/{total}] {key} ok -> "
                          f"{exec_dir / '_replay' / args.variant / 'audit.json'}",
                          file=sys.stderr)
                else:
                    failed += 1
                    print(f"[{idx}/{total}] {key} rc={rc}", file=sys.stderr)

    print(f"replay({args.variant}): {ok} ok, {skipped} skipped, "
          f"{failed} failed", file=sys.stderr)
    return 0 if failed == 0 else 1


def _cmd_compare(args: argparse.Namespace) -> int:
    """Handle `python replay.py compare A B --keys-file F`."""
    settings = argus_config.load(args.config)
    out_dir = args.out_dir or settings.extractor.out_dir
    keys = _read_keys(args.keys_file, args.keys)
    if not keys:
        print("compare: no keys to process (use --keys-file or positional keys)",
              file=sys.stderr)
        return 1

    diffs: list[PerKeyDiff] = []
    for key in keys:
        exec_dir = _find_execution_dir(out_dir, key)
        if exec_dir is None:
            print(f"compare: {key} not found under {out_dir}",
                  file=sys.stderr)
            continue
        diffs.append(compute_diff(exec_dir, args.variant_a, args.variant_b))

    md = render_compare_report(diffs, args.variant_a, args.variant_b)
    if not args.no_report_file:
        report_path = out_dir / "_replay_compare.md"
        report_path.write_text(md)
        print(f"compare: wrote {report_path}", file=sys.stderr)
    if args.stdout:
        sys.stdout.write(md)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay audits with named prompt variants; compare variants."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # replay subcommand — the common case. Separate subparser (vs. making
    # it the parent's default) so positional `keys` don't clash with the
    # compare subcommand's positional variant args.
    p_replay = sub.add_parser("replay",
                              help="run audits with a named prompt variant")
    p_replay.add_argument("--variant", required=True,
                          help="prompt variant name (e.g. v1, v2)")
    p_replay.add_argument("keys", nargs="*",
                          help="execution keys to replay (or use --keys-file)")
    p_replay.add_argument("--keys-file", type=Path, default=None)
    p_replay.add_argument("--out-dir", type=Path, default=None,
                          help="root output dir (default: config extractor.out_dir)")
    p_replay.add_argument("--config", type=Path, default=None)
    p_replay.add_argument("--workers", type=int, default=8,
                          help="parallel replay workers (default 8). Each "
                               "worker holds one key end-to-end through "
                               "auditor.run; capped at len(keys). Stay <= "
                               "[batch] concurrency in config.toml so we "
                               "don't exceed the bench-validated LLM Runtime "
                               "concurrency ceiling.")

    # compare subcommand — diff two existing replay variants.
    p_cmp = sub.add_parser("compare",
                           help="compare two existing replay variants")
    p_cmp.add_argument("variant_a")
    p_cmp.add_argument("variant_b")
    p_cmp.add_argument("keys", nargs="*")
    p_cmp.add_argument("--keys-file", type=Path, default=None)
    p_cmp.add_argument("--out-dir", type=Path, default=None)
    p_cmp.add_argument("--config", type=Path, default=None)
    p_cmp.add_argument("--stdout", action="store_true")
    p_cmp.add_argument("--no-report-file", action="store_true")

    args = parser.parse_args(argv)

    if args.command == "compare":
        return _cmd_compare(args)
    return _cmd_replay(args)


if __name__ == "__main__":
    sys.exit(main())
