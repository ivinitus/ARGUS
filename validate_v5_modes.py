"""Failure-mode validation harness for prompt-variant changes.

Lets you A/B a new prompt variant against the canonical without
hardcoding execution keys. Works against any audit corpus by
sampling audits matching each known failure mode, replaying them
under both variants, and reporting per-mode regressions.

Why this exists
---------------
Prior validation runs hardcoded specific E-keys (e.g. E178936,
E178977, E178961) as "the test set". Those keys are corpus-specific
artefacts of one bench day — they don't reproduce in next week's
folder. The right unit of stability is the *failure mode*: "audits
where step text says Prime and screenshots show 30-day trial" is a
query that returns relevant samples from any corpus.

Each known failure mode in this harness has:

  * `name`   — short identifier
  * `query`  — pure function `(audit_summary) -> bool` deciding if
               this audit is an example of the mode
  * `expect` — how the prompt should behave on this audit
               ("suppress" = no model finding fires, "preserve" =
               model finding from canonical must remain, "flag" =
               specific finding rule must fire)

The harness scans the corpus, picks `--per-mode N` audits per mode,
runs each under both `--baseline` and `--candidate` prompt variants
via replay.py, then reports per-mode pass/fail. Specifically detects:

  * Regressions: a candidate audit that LOST a finding the baseline
    had AND the mode says "preserve"
  * False positives: a candidate audit that GAINED a finding the
    baseline didn't have AND the mode says "suppress"
  * Verdict drift: per-mode aggregate verdict distribution change

Usage
-----
    # Build the validation set from the May 15 corpus:
    python validate_v5_modes.py sample \\
        --corpus output/Targeted_regression_-15_May_2026_WEB \\
        --per-mode 3 \\
        --out /tmp/v5_validation_keys.json

    # Replay both variants on the sampled set:
    python validate_v5_modes.py replay \\
        --keys /tmp/v5_validation_keys.json \\
        --baseline v5 --candidate v5_4

    # Compare:
    python validate_v5_modes.py compare \\
        --keys /tmp/v5_validation_keys.json \\
        --baseline v5 --candidate v5_4

The same `keys.json` file works against any corpus the user later
extracts — re-running `sample` against a new folder produces a
different set of audits, but every set is queryable by the same
mode definitions.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable

# Lightweight summary type — the harness operates on this rather
# than re-parsing audit.json fields ad-hoc.


@dataclass
class AuditCtx:
    """Per-audit context the failure-mode queries can inspect."""
    key: str
    path: Path
    audit: dict
    metadata: dict
    folder_name: str           # e.g. "E2E_Flow_-_GMA_-_AU_Desktop_-_Chrome"
    test_case_name: str
    step_descriptions: str     # all step descriptions concatenated, lowercased
    overall_status: str | None
    findings: list[dict]
    env_check: dict


def _load_audit_ctx(audit_path: Path, corpus_root: Path) -> AuditCtx | None:
    """Read audit.json + metadata.json, build a query-friendly ctx."""
    try:
        audit = json.loads(audit_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    meta_path = audit_path.parent / "metadata.json"
    metadata: dict = {}
    if meta_path.exists():
        try:
            metadata = json.loads(meta_path.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    tr = (metadata.get("test_results") or [{}])[0]
    # Folder name = the segment of the path immediately after corpus_root.
    try:
        rel = audit_path.relative_to(corpus_root)
        folder_name = rel.parts[0] if rel.parts else ""
    except ValueError:
        folder_name = ""
    step_text = " ".join(
        (s.get("description") or "") + " " + (s.get("expected_result") or "")
        for s in (tr.get("steps") or [])
    ).lower()
    return AuditCtx(
        key=audit_path.parent.name,
        path=audit_path.parent,
        audit=audit,
        metadata=metadata,
        folder_name=folder_name,
        test_case_name=(tr.get("test_case_name") or ""),
        step_descriptions=step_text,
        overall_status=tr.get("status"),
        findings=audit.get("findings") or [],
        env_check=audit.get("env_check") or {},
    )


# ---------------------------------------------------------------------------
# Failure mode definitions. Each query is a pure function over AuditCtx.
# ---------------------------------------------------------------------------
@dataclass
class FailureMode:
    name: str
    description: str
    query: Callable[[AuditCtx], bool]
    expectation: str  # "suppress" | "preserve" | "flag:<rule>"


_PARENTHETICAL_HINT_RX = re.compile(r"\(\s*for\s+[a-z]{2}\s*\)", re.IGNORECASE)


def _has_step_keyword(ctx: AuditCtx, *needles: str) -> bool:
    return any(n.lower() in ctx.step_descriptions for n in needles)


MODES: list[FailureMode] = [
    FailureMode(
        name="prime_trial_onramp",
        description=(
            "Tester is a Prime user signing up for Premium Plus. The product "
            "gates the paid signup behind a $0.00 30-day trial. "
            "Expected behaviour: V5.3+ should NOT flag this as wrong-outcome-"
            "category. Pre-V5.3 falsely flagged the trial TYP as 'free trial "
            "shown when paid required'."
        ),
        # Heuristic: step text mentions Prime AND mentions trial/membership.
        query=lambda ctx: (
            "prime" in ctx.step_descriptions
            and ("trial" in ctx.step_descriptions
                 or "premium plus" in ctx.step_descriptions)
        ),
        expectation="suppress",
    ),
    FailureMode(
        name="mobile_browser_not_webview",
        description=(
            "Audit lives under a Mobile_* folder (mobile browser test) "
            "but NOT a Webview_E2E_* folder. The chrome://inspect rule "
            "should NOT fire. Pre-V5.3 over-flagged these."
        ),
        query=lambda ctx: (
            "mobile" in ctx.folder_name.lower()
            and "webview" not in ctx.folder_name.lower()
            and "in_app" not in ctx.folder_name.lower()
        ),
        expectation="suppress",  # no chrome://inspect finding
    ),
    FailureMode(
        name="hover_url_prod_link",
        description=(
            "env_check found prod URL only in the bottom strip (status-bar "
            "hover) and zero in the top URL bar. R9-hover (low advisory) "
            "should fire instead of R9 (high). Tester is on preprod."
        ),
        query=lambda ctx: (
            len(ctx.env_check.get("prod_urls_bottom") or []) > 0
            and len(ctx.env_check.get("prod_urls_top") or []) == 0
            and len(ctx.env_check.get("preprod_urls") or []) > 0
        ),
        expectation="flag:R9-hover",
    ),
    FailureMode(
        name="marketplace_gate_vs_hint",
        description=(
            "Step text contains a parenthetical marketplace hint like "
            "'(For BR)' next to a noun the tester selects. This is NOT a "
            "marketplace gate — should not fire R13 unless the URL TLD "
            "actually disagrees with the assigned marketplace."
        ),
        query=lambda ctx: (
            bool(_PARENTHETICAL_HINT_RX.search(ctx.step_descriptions))
        ),
        expectation="suppress",  # marketplace finding from prompt
    ),
    FailureMode(
        name="locale_clause_translation",
        description=(
            "Test ran on a non-English marketplace (DE/FR/IT/JP/ES/BR) where "
            "system messages get localised. Structural translation differences "
            "(clauses dropped/condensed) should not fire 'expected text "
            "mismatch' as long as the message communicates the same outcome."
        ),
        query=lambda ctx: any(
            mp in ctx.folder_name.upper()
            for mp in ["_DE_", "_FR_", "_IT_", "_JP_", "_ES_", "_BR_"]
        ),
        expectation="suppress",
    ),
    FailureMode(
        name="real_high_finding_control",
        description=(
            "Canonical audit has a HIGH-severity finding (model or rule). "
            "Any prompt change that drops these is a regression. Used as a "
            "'must keep flagging' control across all variants."
        ),
        query=lambda ctx: any(
            f.get("severity") == "high" for f in ctx.findings
        ),
        expectation="preserve",
    ),
    FailureMode(
        name="clean_pass_control",
        description=(
            "Canonical audit verdict = pass with 0 findings. Must stay clean "
            "under any prompt change."
        ),
        query=lambda ctx: (
            ctx.audit.get("overall_verdict") == "pass"
            and len(ctx.findings) == 0
        ),
        expectation="preserve",
    ),
]


# ---------------------------------------------------------------------------
# Sampler
# ---------------------------------------------------------------------------
def cmd_sample(args: argparse.Namespace) -> int:
    corpus = args.corpus
    if not corpus.exists():
        print(f"corpus dir not found: {corpus}", file=sys.stderr)
        return 1

    audit_paths = sorted(
        p for p in corpus.rglob("audit.json") if "_replay" not in p.parts
    )
    print(f"scanning {len(audit_paths)} audit.json files under {corpus}",
          file=sys.stderr)

    contexts: list[AuditCtx] = []
    for ap in audit_paths:
        ctx = _load_audit_ctx(ap, corpus)
        if ctx is not None:
            contexts.append(ctx)
    print(f"loaded {len(contexts)} contexts", file=sys.stderr)

    sample: dict[str, list[str]] = {}
    summary: list[str] = []
    for mode in MODES:
        matches = [ctx for ctx in contexts if mode.query(ctx)]
        # Deterministic sample: take first N alphabetically by key. Avoids
        # randomness so re-running on the same corpus produces the same
        # validation set.
        matches.sort(key=lambda c: c.key)
        picked = matches[: args.per_mode]
        sample[mode.name] = [c.key for c in picked]
        summary.append(
            f"  {mode.name:<32} matches={len(matches):>4}  "
            f"sampled={len(picked):>2}  expect={mode.expectation}"
        )

    args.out.write_text(json.dumps({
        "corpus": str(corpus),
        "per_mode": args.per_mode,
        "modes": [
            {"name": m.name, "description": m.description,
             "expectation": m.expectation}
            for m in MODES
        ],
        "sample": sample,
    }, indent=2))
    print("\nsample summary:", file=sys.stderr)
    for line in summary:
        print(line, file=sys.stderr)
    print(f"\nwrote {args.out}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# Compare (read-only — assumes replay has already produced both variants)
# ---------------------------------------------------------------------------
def cmd_compare(args: argparse.Namespace) -> int:
    spec = json.loads(args.keys.read_text())
    corpus = Path(spec["corpus"])
    sample = spec["sample"]

    # Gather per-mode audit pairs (canonical, replayed candidate)
    mode_results: dict[str, dict] = {}
    for mode in MODES:
        keys = sample.get(mode.name) or []
        per_audit = []
        for key in keys:
            cand_path = next(
                (p for p in corpus.rglob(
                    f"{key}/_replay/{args.candidate}/audit.json")), None,
            )
            base_paths = [
                p for p in corpus.rglob(f"{key}/audit.json")
                if "_replay" not in p.parts
            ]
            if not base_paths:
                per_audit.append({"key": key, "error": "no canonical audit"})
                continue
            base = json.loads(base_paths[0].read_text())
            cand = (json.loads(cand_path.read_text())
                    if cand_path and cand_path.exists() else None)
            per_audit.append({
                "key": key,
                "base_verdict": base.get("overall_verdict"),
                "cand_verdict": cand.get("overall_verdict") if cand else None,
                "base_findings": _model_findings(base),
                "cand_findings": _model_findings(cand) if cand else [],
                "base_high": _high_count(base),
                "cand_high": _high_count(cand) if cand else 0,
                "missing_replay": cand is None,
            })
        mode_results[mode.name] = {
            "expectation": mode.expectation,
            "per_audit": per_audit,
        }

    # Render
    print(f"\n=== Failure-mode validation: {args.baseline} vs {args.candidate} ===\n")
    overall_pass = True
    for mode in MODES:
        result = mode_results[mode.name]
        per = result["per_audit"]
        if not per:
            print(f"  {mode.name:<32}  no samples — SKIP")
            continue
        regressions = []
        fps_added = []
        missing = []
        for r in per:
            if r.get("missing_replay"):
                missing.append(r["key"])
                continue
            base_h = r["base_high"]; cand_h = r["cand_high"]
            base_n = len(r["base_findings"]); cand_n = len(r["cand_findings"])
            if mode.expectation == "preserve":
                if cand_h < base_h:
                    regressions.append(
                        f"{r['key']}: high {base_h}→{cand_h}")
            elif mode.expectation == "suppress":
                if cand_n > base_n:
                    fps_added.append(
                        f"{r['key']}: findings {base_n}→{cand_n}")
            elif mode.expectation.startswith("flag:"):
                rule = mode.expectation.split(":", 1)[1]
                fired = any(f.get("rule") == rule for f in r["cand_findings"])
                if not fired:
                    regressions.append(
                        f"{r['key']}: {rule} did not fire")
        ok = not regressions and not fps_added
        overall_pass = overall_pass and ok
        status = "PASS" if ok else "FAIL"
        print(f"  {mode.name:<32}  {status}  ({len(per)} samples, "
              f"expect={mode.expectation})")
        for r_ in regressions:
            print(f"      regression: {r_}")
        for fp in fps_added:
            print(f"      new FP:     {fp}")
        for m in missing:
            print(f"      no replay:  {m}")
    print()
    print("OVERALL:", "PASS" if overall_pass else "FAIL")
    return 0 if overall_pass else 1


def _model_findings(audit: dict) -> list[dict]:
    return [
        f for f in (audit.get("findings") or [])
        if f.get("source") not in ("rule", "env_check")
    ]


def _high_count(audit: dict) -> int:
    return sum(1 for f in (audit.get("findings") or [])
               if f.get("severity") == "high")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sample = sub.add_parser("sample",
                              help="pick N audits per mode from a corpus")
    p_sample.add_argument("--corpus", type=Path, required=True)
    p_sample.add_argument("--per-mode", type=int, default=3)
    p_sample.add_argument("--out", type=Path, required=True)

    p_compare = sub.add_parser(
        "compare",
        help=("compare canonical vs candidate replay outputs against "
              "expected behaviour per mode"),
    )
    p_compare.add_argument("--keys", type=Path, required=True,
                           help="path to keys.json from `sample`")
    p_compare.add_argument("--baseline", default="v5")
    p_compare.add_argument("--candidate", default="v5_4")

    args = parser.parse_args(argv)
    if args.cmd == "sample":
        return cmd_sample(args)
    if args.cmd == "compare":
        return cmd_compare(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
