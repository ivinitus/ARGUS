"""Manage verdict-calibration keys and historical verdict backfills.

calibration/*.keys are local-only datasets (gitignored, absent on a fresh
clone). `escalate` treats missing key sets as empty; regenerate them from
your audited output with the `keys` and `dismissed` subcommands first.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "src")
)
from argus.core import exec_paths

from argus.domain.finding_inference import (
    STRONG_CONTRADICTION_TERMS,
    apply_contradiction_escalation,
)


def _iter_real_audits(out_dir: Path):
    """Yield (audit_path, audit_dict) for every canonical real audit."""
    for path in exec_paths.iter_audit_jsons(out_dir):
        if "Dummy_runs" in path.parts:
            continue
        try:
            audit = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        yield path, audit


def _model_findings(audit: dict) -> list[dict]:
    """Model-sourced findings only — rule/env_check severities are deterministic and never miscalibrated by the prompt."""
    return [f for f in (audit.get("findings") or [])
            if isinstance(f, dict)
            and f.get("source") not in ("rule", "env_check")]


def _is_under_severe(audit: dict) -> str | None:
    """Return the offending description when the MODEL mis-tiered a contradiction as medium."""
    for f in _model_findings(audit):
        if f.get("_severity_adjusted_from") == "medium":
            return f.get("description") or ""
    if audit.get("overall_verdict") != "concerns":
        return None
    for f in _model_findings(audit):
        if f.get("severity") != "medium":
            continue
        desc = (f.get("description") or "").lower()
        if any(t in desc for t in STRONG_CONTRADICTION_TERMS):
            return f.get("description") or ""
    return None


def _is_over_severe(audit: dict) -> bool:
    """fail verdict whose findings exist but include no high of ANY source = contract breach."""
    if audit.get("overall_verdict") != "fail":
        return False
    findings = [f for f in (audit.get("findings") or [])
                if isinstance(f, dict)]
    return bool(findings) and not any(
        f.get("severity") == "high" for f in findings)


def _cmd_keys(out_dir: Path) -> int:
    under: list[tuple[str, str]] = []
    over: list[str] = []
    for path, audit in _iter_real_audits(out_dir):
        key = path.parent.name
        desc = _is_under_severe(audit)
        if desc is not None:
            under.append((key, desc))
        if _is_over_severe(audit):
            over.append(key)

    under_path = Path("calibration/under_severe.keys")
    over_path = Path("calibration/over_severe.keys")
    under_path.parent.mkdir(parents=True, exist_ok=True)
    under_lines = [
        "# Audits where the MODEL mis-tiered a contradiction as medium —",
        "# either still live (concerns + contradiction-worded medium) or",
        "# already caught by the deterministic escalation guard",
        "# (_severity_adjusted_from == 'medium'). This set measures RAW",
        "# model behaviour and should trend with the model, NOT to ~0:",
        "# the deterministic guard fixes the verdict but the mis-tier",
        "# still happened. Replay diffs on these keys include the",
        "# deterministic flip — do not attribute it to a prompt change.",
    ] + [f"{k}  # {d[:90].replace(chr(10), ' ')}" for k, d in sorted(under)]
    over_lines = [
        "# fail-verdict audits with ZERO high findings (contract breach).",
        "# V5.6 guard set: replay must NOT invent new high findings here.",
    ] + sorted(over)
    under_path.write_text("\n".join(under_lines) + "\n")
    over_path.write_text("\n".join(over_lines) + "\n")
    print(f"under-severe (concerns hiding contradiction): {len(under)} "
          f"-> {under_path}")
    print(f"over-severe  (fail without high finding):     {len(over)} "
          f"-> {over_path}")
    return 0


def _cmd_demote(out_dir: Path, apply: bool) -> int:
    from argus.pipeline import env_check_merge

    hits = 0
    for path, audit in _iter_real_audits(out_dir):
        if not _is_over_severe(audit):
            continue
        if audit.get("_verdict_demoted_from"):
            continue
        hits += 1
        print(f"{'' if apply else 'DRY-RUN '}"
              f"{path.parent.name}: fail -> concerns  ({path})")
        if not apply:
            continue
        audit["overall_verdict"] = "concerns"
        audit["_verdict_demoted_from"] = "fail"
        metadata = None
        md_path = path.parent / "metadata.json"
        if md_path.exists():
            try:
                metadata = json.loads(md_path.read_text())
            except (OSError, json.JSONDecodeError):
                metadata = None
        env_check_merge.write_merged_audit(path, audit, metadata)
    print(f"\n{hits} audit(s) {'demoted' if apply else 'would be demoted'}")
    if apply and hits:
        print("run `argus index sync` to refresh the index")
    return 0


def _matched_term(desc: str) -> str:
    """First contradiction term matching `desc` (for operator display)."""
    d = desc.lower()
    for t in STRONG_CONTRADICTION_TERMS:
        if t in d:
            return t
    return "?"


def _cmd_escalate(out_dir: Path, apply: bool,
                  sample: int = 0, seed: int = 0) -> int:
    """Escalate contradiction-worded mediums in stored audits."""
    from argus.pipeline import auditor
    from argus.pipeline import env_check_merge

    under_keys: set[str] = set()
    under_path = Path("calibration/under_severe.keys")
    if under_path.exists():
        under_keys = {ln.split()[0] for ln in under_path.read_text().splitlines()
                      if ln.strip() and not ln.startswith("#")}
    dismissed_keys: set[str] = set()
    dismissed_path = Path("calibration/dismissed.keys")
    if not dismissed_path.exists():
        dismissed_path = Path("calibration_dismissed.keys")
    if dismissed_path.exists():
        dismissed_keys = {ln.strip() for ln in dismissed_path.read_text().splitlines()
                          if ln.strip() and not ln.startswith("#")}

    hits = 0
    verdict_flips = 0
    under_overlap = 0
    dismissed_overlap = 0
    sampled: list[tuple[str, dict]] = []
    all_matches: list[tuple[str, dict]] = []

    for path, audit in _iter_real_audits(out_dir):
        key = path.parent.name
        escalated: list[dict] = []
        for f in _model_findings(audit):
            target = f if apply else dict(f)
            if apply_contradiction_escalation(target):
                escalated.append(target)
                all_matches.append((key, dict(target)))
        if not escalated:
            continue
        hits += 1
        under_overlap += key in under_keys
        dismissed_overlap += key in dismissed_keys
        old_verdict = audit.get("overall_verdict")
        if apply:
            audit = auditor._finalize_verdict(audit)
            new_verdict = audit.get("overall_verdict")
        else:
            new_verdict = "fail"
        flipped = old_verdict != "fail"
        verdict_flips += flipped
        first = escalated[0]
        anchor = (f"page {first.get('page')}" if first.get("page") is not None
                  else f"step {first.get('step_index')}")
        print(f"{'' if apply else 'DRY-RUN '}{key}: "
              f"{old_verdict} -> {new_verdict}"
              f"{'' if flipped else ' (already fail)'} "
              f"({len(escalated)} finding(s), term: "
              f"{_matched_term(first.get('description') or '')}, {anchor})")
        if apply:
            metadata = None
            md_path = path.parent / "metadata.json"
            if md_path.exists():
                try:
                    metadata = json.loads(md_path.read_text())
                except (OSError, json.JSONDecodeError):
                    metadata = None
            env_check_merge.write_merged_audit(path, audit, metadata)

    print(f"\n{hits} audit(s) with escalations "
          f"({verdict_flips} verdict flip(s), "
          f"{under_overlap} in under_severe.keys, "
          f"{dismissed_overlap} in dismissed keys"
          f"{' — corpus empty' if not dismissed_keys else ''})")
    if apply and hits:
        print("run `argus index sync` to refresh the index")

    if sample and all_matches:
        import random
        rng = random.Random(seed)
        sampled = rng.sample(all_matches, min(sample, len(all_matches)))
        print(f"\n=== SAMPLE ({len(sampled)} of {len(all_matches)} matched "
              f"findings, seed={seed}) — judge each: is this a genuine "
              "contradiction (HIGH per ladder)? ===")
        for i, (key, f) in enumerate(sampled, 1):
            print(f"\n[{i}] {key}  term={_matched_term(f.get('description') or '')}  "
                  f"page={f.get('page')} step={f.get('step_index')}")
            print(f"    {f.get('description')}")
    return 0


def _cmd_dismissed(out_dir: Path) -> int:
    keys: list[str] = []
    corpus: list[dict] = []
    for path, audit in _iter_real_audits(out_dir):
        acks = [a for a in (audit.get("acknowledgments") or [])
                if isinstance(a, dict) and a.get("type") == "dismissed"]
        if not acks:
            continue
        key = path.parent.name
        keys.append(key)
        last = acks[-1]
        corpus.append({
            "key": key,
            "path": str(path),
            "verdict": audit.get("overall_verdict"),
            "variant_name": audit.get("variant_name"),
            "findings": [
                {"severity": f.get("severity"),
                 "rule": f.get("rule"),
                 "source": f.get("source"),
                 "description": f.get("description")}
                for f in (audit.get("findings") or [])
                if isinstance(f, dict)
            ],
            "dismissed_by": last.get("by"),
            "dismissed_at": last.get("at"),
            "reason": last.get("reason"),
        })

    keys_path = Path("calibration/dismissed.keys")
    keys_path.parent.mkdir(parents=True, exist_ok=True)
    corpus_path = out_dir / "_fp_corpus.jsonl"
    keys_path.write_text(
        "# operator-dismissed audits (known FPs). Replay guard-set:\n"
        "# a prompt candidate must NOT reproduce these findings.\n"
        + "\n".join(sorted(keys)) + ("\n" if keys else ""))
    with corpus_path.open("w") as fh:
        for row in corpus:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"dismissed audits: {len(keys)} -> {keys_path}")
    print(f"FP corpus rows:   {len(corpus)} -> {corpus_path}")
    if not keys:
        print("(none yet — dismiss false positives in Slack with "
              "`/argus dismiss <key> <reason>` to grow this corpus)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verdict-calibration key lists + fail-without-high "
                    "backfill.")
    parser.add_argument("--out-dir", type=Path,
                        default=Path(__file__).resolve().parent.parent
                        / "output")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("keys", help="write calibration severity key sets")
    d = sub.add_parser("demote",
                       help="backfill-demote fail-without-high audits "
                            "(DRY-RUN unless --apply)")
    d.add_argument("--apply", action="store_true",
                   help="actually write (default is dry-run — this "
                        "flips verdicts fail -> concerns)")
    e = sub.add_parser(
        "escalate",
        help="backfill-escalate contradiction-worded mediums "
             "(DRY-RUN unless --apply)")
    e.add_argument("--apply", action="store_true",
                   help="actually write (default is dry-run — this "
                        "flips verdicts concerns -> fail)")
    e.add_argument("--sample", type=int, default=0, metavar="N",
                   help="print N random matched findings for manual review")
    e.add_argument("--seed", type=int, default=0,
                   help="RNG seed for --sample (reproducible review sets)")
    sub.add_parser("dismissed",
                   help="harvest /argus dismiss labels into the FP corpus")
    args = parser.parse_args(argv)

    if args.cmd == "keys":
        return _cmd_keys(args.out_dir)
    if args.cmd == "dismissed":
        return _cmd_dismissed(args.out_dir)
    if args.cmd == "escalate":
        return _cmd_escalate(args.out_dir, args.apply,
                             sample=args.sample, seed=args.seed)
    return _cmd_demote(args.out_dir, args.apply)


if __name__ == "__main__":
    sys.exit(main())
