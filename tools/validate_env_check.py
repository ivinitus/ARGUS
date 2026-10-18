"""Validate env_check_model against existing tesseract-based env_check."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "src")
)

from argus import config as argus_config
from argus.pipeline import env_check_model


@dataclass
class ComparisonResult:
    audit_path: str
    exec_dir: str
    tess_verdict: str
    haiku_verdict: str
    tess_prod_urls: list[str]
    tess_preprod_urls: list[str]
    haiku_prod_urls: list[str]
    haiku_preprod_urls: list[str]
    tess_time_s: float
    haiku_time_s: float
    agreement: str
    disagreement_kind: str | None = None


def _classify_disagreement(tess: str, haiku: str) -> str:
    """Type the disagreement so we can summarise."""
    if tess == haiku:
        return "agree"
    bad = {"violation", "mixed"}
    if haiku in bad and tess not in bad:
        return "haiku_more_strict"
    if tess in bad and haiku not in bad:
        return "haiku_more_lenient"
    if tess == "ambiguous" or haiku == "ambiguous":
        return "ambiguous_disagreement"
    return "verdict_split"


def _process_one(audit_path_str: str, profile: str | None) -> dict:
    audit_path = Path(audit_path_str)
    exec_dir = audit_path.parent
    try:
        audit = json.loads(audit_path.read_text())
    except Exception as e:
        return {"error": f"could not read {audit_path}: {e}"}

    tess_existing = audit.get("env_check") or {}
    tess_verdict = tess_existing.get("verdict", "missing")
    tess_prod = tess_existing.get("prod_urls") or []
    tess_preprod = tess_existing.get("preprod_urls") or []
    tess_time = 0.0

    t0 = time.monotonic()
    try:
        h = env_check_model.check_env_compliance(
            exec_dir,
            sample_stride=5,
            region_crop_height=180,
            region_crop_bottom_height=80,
            api_key_env=profile,
            max_workers=4,
        )
    except Exception as e:
        return {"error": f"haiku run failed on {audit_path}: "
                         f"{type(e).__name__}: {e}",
                "audit_path": str(audit_path)}
    haiku_time = time.monotonic() - t0

    haiku_verdict = h["verdict"]
    haiku_prod = h["prod_urls"]
    haiku_preprod = h["preprod_urls"]

    if tess_verdict == haiku_verdict:
        agreement = "agree"
        disagreement_kind = None
    else:
        agreement = "disagree"
        disagreement_kind = _classify_disagreement(tess_verdict, haiku_verdict)

    return asdict(ComparisonResult(
        audit_path=str(audit_path),
        exec_dir=str(exec_dir),
        tess_verdict=tess_verdict,
        haiku_verdict=haiku_verdict,
        tess_prod_urls=tess_prod,
        tess_preprod_urls=tess_preprod,
        haiku_prod_urls=haiku_prod,
        haiku_preprod_urls=haiku_preprod,
        tess_time_s=tess_time,
        haiku_time_s=haiku_time,
        agreement=agreement,
        disagreement_kind=disagreement_kind,
    ))


def _render_report(results: list[dict]) -> str:
    valid = [r for r in results if "error" not in r]
    errors = [r for r in results if "error" in r]
    n = len(valid)
    agree = sum(1 for r in valid if r["agreement"] == "agree")
    disagree = sum(1 for r in valid if r["agreement"] == "disagree")
    by_kind: dict[str, int] = {}
    for r in valid:
        if r.get("disagreement_kind"):
            k = r["disagreement_kind"]
            by_kind[k] = by_kind.get(k, 0) + 1

    lines = [
        "# Haiku env_check validation report",
        "",
        f"- **Audits compared:** {n}",
        f"- **Agreement:** {agree} ({100*agree/n:.0f}%)" if n else "- **Agreement:** 0",
        f"- **Disagreement:** {disagree} ({100*disagree/n:.0f}%)" if n else "- **Disagreement:** 0",
        f"- **Errors:** {len(errors)}",
        "",
    ]
    if by_kind:
        lines += ["## Disagreement breakdown", ""]
        for k, v in sorted(by_kind.items(), key=lambda kv: -kv[1]):
            lines.append(f"- {k}: {v}")
        lines.append("")
    if errors:
        lines += ["## Errors", ""]
        for e in errors[:10]:
            lines.append(f"- `{e.get('audit_path', '?')}`: {e['error']}")
        lines.append("")
    if disagree:
        lines += [
            "## Disagreement detail",
            "",
            "Each row shows a key where tesseract and Haiku disagreed. "
            "For each, eyeball the screenshots in the listed exec_dir "
            "to decide which classification is correct.",
            "",
            "| Exec dir | Tess verdict | Haiku verdict | Tess prod URLs | Haiku prod URLs | Kind |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for r in valid:
            if r["agreement"] != "disagree":
                continue
            tp = ", ".join(r["tess_prod_urls"][:3]) or "-"
            hp = ", ".join(r["haiku_prod_urls"][:3]) or "-"
            ed = r["exec_dir"].rsplit("/", 2)
            short = "/".join(ed[-2:]) if len(ed) >= 2 else r["exec_dir"]
            lines.append(
                f"| `{short}` | {r['tess_verdict']} | {r['haiku_verdict']} | "
                f"{tp} | {hp} | {r['disagreement_kind']} |"
            )
    if valid:
        haiku_times = [r["haiku_time_s"] for r in valid if r["haiku_time_s"]]
        if haiku_times:
            lines += [
                "",
                "## Haiku timing",
                "",
                f"- mean per-audit: {sum(haiku_times)/len(haiku_times):.1f}s",
                f"- min: {min(haiku_times):.1f}s, max: {max(haiku_times):.1f}s",
            ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keys-file", type=Path, default=None,
                        help="file with one audit.json path per line")
    parser.add_argument("--out-report", type=Path,
                        default=Path("/tmp/haiku_validation_report.md"))
    parser.add_argument("--out-json", type=Path,
                        default=Path("/tmp/haiku_validation_results.json"))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")

    settings = argus_config.load(args.config)
    profile = settings.auditor.api_key_env

    if not args.keys_file or not args.keys_file.exists():
        print(f"--keys-file required and must exist: {args.keys_file}",
              file=sys.stderr)
        return 1

    paths = []
    for line in args.keys_file.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split(None, 1)
        path = parts[1] if len(parts) == 2 else parts[0]
        paths.append(path)

    print(f"validate: {len(paths)} audits to compare (workers={args.workers})",
          file=sys.stderr)

    results: list[dict] = []
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(_process_one, p, profile): p for p in paths}
        for i, fut in enumerate(as_completed(futures), start=1):
            r = fut.result()
            results.append(r)
            if "error" in r:
                print(f"[{i}/{len(paths)}] ERROR: {r['error']}",
                      file=sys.stderr)
            else:
                print(f"[{i}/{len(paths)}] {r['agreement']} "
                      f"tess={r['tess_verdict']} haiku={r['haiku_verdict']} "
                      f"({r['haiku_time_s']:.1f}s)",
                      file=sys.stderr)

    report = _render_report(results)
    args.out_report.write_text(report)
    args.out_json.write_text(json.dumps(results, indent=2))
    print(f"\nvalidate: wrote {args.out_report}", file=sys.stderr)
    print(f"validate: wrote {args.out_json}", file=sys.stderr)

    valid = [r for r in results if "error" not in r]
    if not valid:
        return 1
    agree = sum(1 for r in valid if r["agreement"] == "agree")
    print(f"validate: agreement {agree}/{len(valid)} "
          f"({100*agree/len(valid):.0f}%)",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
