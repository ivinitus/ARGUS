"""Replay existing audits with a named prompt variant for measured A/B."""
from __future__ import annotations

import argparse
import collections
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from argus.pipeline import auditor
from argus import config as argus_config
from argus.core import exec_paths
from argus.core import paths
from argus import runtime
from argus.pipeline.auditor_prompts import resolve_prompt_variant
from argus.domain.finding_identity import finding_identity


VARIANTS: dict[str, str | None] = {
    "v1": auditor.SYSTEM_PROMPT_V1,
    "v2": auditor.SYSTEM_PROMPT_V2,
    "v3": auditor.SYSTEM_PROMPT_V3,
    "v4": auditor.SYSTEM_PROMPT_V4,
    "v5": auditor.SYSTEM_PROMPT_V5,
    "v5_4": auditor.SYSTEM_PROMPT_V5_4,
    "v5_5": auditor.SYSTEM_PROMPT_V5_5,
    "v5_6": auditor.SYSTEM_PROMPT_V5_6,
    "v5_7": auditor.SYSTEM_PROMPT_V5_7,
    "v5_hist": auditor.SYSTEM_PROMPT_V5,
    "v5_tools": auditor.SYSTEM_PROMPT_V5,
}

VARIANT_OVERRIDES: dict[str, dict[str, object]] = {
    "v5_hist": {"history_in_prompt": True},
    "v5_tools": {"structured_output": True},
}


def _resolve_variant(name: str) -> str | None:
    """Return the system prompt text for `name`, or raise."""
    if name in VARIANTS:
        return VARIANTS[name]
    return resolve_prompt_variant(name)


def _find_execution_dir(out_dir: Path, key: str) -> Path | None:
    """Locate <out_dir>/.../<key>/ across all layouts the extractor has produced."""
    return exec_paths.find_execution_dir(out_dir, key)


def replay_audit(
    exec_dir: Path,
    variant_name: str,
    settings: argus_config.ArgusConfig | None = None,
) -> int:
    """Run `auditor.run()` against `exec_dir` with the named prompt variant."""
    settings = settings or argus_config.load()
    runtime.configure(settings)
    system_prompt = _resolve_variant(variant_name)
    output_dir = exec_dir / "_replay" / variant_name
    cfg = auditor.AuditConfig.from_settings(
        settings.auditor,
        execution_dir=exec_dir,
        cost_rates=settings.costs.model_rates,
        output_dir=output_dir,
        system_prompt=system_prompt,
        variant_name=variant_name,
        consensus_enabled=True,
        **VARIANT_OVERRIDES.get(variant_name, {}),
    )
    return auditor.run(cfg)


@dataclass
class PerKeyDiff:
    """Single-key diff between two replay variants."""
    key: str
    verdict_a: str | None
    verdict_b: str | None
    sev_a: dict[str, int]
    sev_b: dict[str, int]
    only_in_a: list[str]
    only_in_b: list[str]


def _finding_signature(f: dict) -> str:
    """Readable location plus a content identity, never just step/severity."""
    sev = f.get("severity", "?")
    step = f.get("step_index")
    page = f.get("page")
    if step is not None:
        return f"{sev}|step:{step}|{finding_identity(f)[:24]}"
    if page is not None:
        return f"{sev}|page:{page}|{finding_identity(f)[:24]}"
    return f"{sev}|anon:{finding_identity(f)[:24]}"


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
    """Render a markdown comparison report."""
    verdict_transitions: collections.Counter[tuple[str, str]] = collections.Counter()
    promotions = 0
    demotions = 0
    unchanged = 0
    for d in diffs:
        key = (d.verdict_a or "missing", d.verdict_b or "missing")
        verdict_transitions[key] += 1
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


def _read_keys(keys_file: Path | None, args_keys: list[str]) -> list[str]:
    """Return deduped keys preserving insertion order."""
    keys: list[str] = []
    if args_keys:
        keys = list(args_keys)
    elif keys_file is not None:
        for raw in keys_file.read_text().splitlines():
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


def _cmd_replay(args: argparse.Namespace) -> int:
    """Handle `argus replay --variant V keys...` (the default subcommand)."""
    settings = argus_config.load(args.config)
    runtime.configure(settings)
    out_dir = args.out_dir or paths.output_root(settings)
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
    """Handle `argus replay compare A B --keys-file F`."""
    settings = argus_config.load(args.config)
    runtime.configure(settings)
    out_dir = args.out_dir or paths.output_root(settings)
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
                               "don't exceed the bench-validated model "
                               "concurrency ceiling.")

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
