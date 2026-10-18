"""Human-labelled audit evaluation; inference is explicit and output is isolated."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import re
import time
from typing import Any

from argus import config, runtime
from argus.core.costs import TOKEN_FIELDS, UsageTracker
from argus.core.redaction import redact_credentials
from argus.domain.finding_identity import finding_identity
from argus.pipeline import analysis_status, auditor, model_client, provenance

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}")
_OVERRIDES = {
    "model_id", "video_model_id", "video_audio_model_id", "prompt_variant",
    "temperature", "max_output_tokens", "thinking_mode", "structured_output",
    "consensus_enabled", "evidence_review_mode",
}


def load_manifest(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("cases"), list):
        raise ValueError("expected a version 1 manifest with cases")
    configurations = data.get("configurations")
    if not isinstance(configurations, dict) or not configurations:
        raise ValueError("configurations must include baseline")
    if "baseline" not in configurations:
        raise ValueError("baseline configuration is required")
    for name, overrides in configurations.items():
        if not _NAME.fullmatch(name) or not isinstance(overrides, dict):
            raise ValueError("invalid configuration name or overrides")
        if set(overrides) - _OVERRIDES:
            raise ValueError("unsupported evaluation override")
    seen = set()
    families: dict[tuple[str, str], str] = {}
    for case in data["cases"]:
        if not isinstance(case, dict):
            raise ValueError("case must be an object")
        key = case.get("id")
        if not isinstance(key, str) or not _NAME.fullmatch(key) or key in seen:
            raise ValueError("case IDs must be unique safe names")
        seen.add(key)
        if case.get("split") not in {"development", "holdout"}:
            raise ValueError("case split must be development or holdout")
        if not case.get("profile") or not case.get("case_family"):
            raise ValueError("each case needs a profile and test-case family")
        family = (case["profile"], case["case_family"])
        if family in families and families[family] != case["split"]:
            raise ValueError("test-case family leaks between development and holdout")
        families[family] = case["split"]
        labels = case.get("expected_findings")
        reviewers = case.get("reviewed_by")
        if (
            not isinstance(labels, list) or not isinstance(reviewers, list)
            or not reviewers or any(not isinstance(name, str) or not name.strip() for name in reviewers)
        ):
            raise ValueError("every case needs human-reviewed labels, including clean cases")
        label_ids = set()
        for label in labels:
            if not isinstance(label, dict):
                raise ValueError("expected finding must be an object")
            if not isinstance(label.get("id"), str) or label["id"] in label_ids:
                raise ValueError("expected finding IDs must be unique")
            label_ids.add(label["id"])
            if label.get("severity") not in {"high", "medium", "low"}:
                raise ValueError("expected findings need severity")
            pattern = label.get("description_pattern")
            if not isinstance(pattern, str) or not pattern.strip():
                raise ValueError("expected findings need a human-authored description_pattern")
            re.compile(pattern)
        directory = Path(case["execution_dir"])
        if not directory.is_absolute():
            directory = path.parent / directory
        case["execution_dir"] = str(directory.resolve())
    if not seen:
        raise ValueError("manifest has no cases")
    return data


def _matches(label: dict[str, Any], finding: dict[str, Any]) -> bool:
    if any(
        key in label and label[key] != finding.get(key)
        for key in ("step_index", "category", "requirement_id")
    ):
        return False
    return bool(re.search(
        label["description_pattern"],
        str(finding.get("title") or "") + "\n" + str(finding.get("description") or ""),
    ))


def score(case: dict[str, Any], audit: dict[str, Any]) -> dict[str, Any]:
    """Score the final audit, including deterministic findings, with unique matches."""
    labels = case["expected_findings"]
    findings = [
        finding for finding in audit.get("findings") or []
        if isinstance(finding, dict)
        and finding.get("source") != "runtime"
    ]
    # Confirmed findings receive matches first. Reassignment preserves maximum
    # matching cardinality when human-authored patterns overlap.
    order = sorted(
        range(len(findings)),
        key=lambda index: findings[index].get("assessment_status") == "needs_review",
    )
    assigned: dict[int, int] = {}

    def assign(index: int, visited: set[int]) -> bool:
        for label_idx, label in enumerate(labels):
            if label_idx in visited or not _matches(label, findings[index]):
                continue
            visited.add(label_idx)
            prior = assigned.get(label_idx)
            if prior is None or assign(prior, visited):
                assigned[label_idx] = index
                return True
        return False

    for index in order:
        assign(index, set())
    matched = {index: label_idx for label_idx, index in assigned.items()}
    pending = {
        index for index, finding in enumerate(findings)
        if finding.get("assessment_status") == "needs_review"
    }
    confirmed = set(range(len(findings))) - pending
    high = {index for index in confirmed if findings[index].get("severity") == "high"}
    citations = []
    for index, label_idx in matched.items():
        expected_refs = labels[label_idx].get("evidence_refs")
        if expected_refs:
            actual = set(findings[index].get("evidence_refs") or [])
            if isinstance(findings[index].get("page"), int):
                actual.add(f"p{findings[index]['page']}")
            citations.append(bool(actual.intersection(expected_refs)))
    complete = analysis_status.derive_status(audit) == "complete"
    return {
        "expected": len(labels),
        "true_positives": len(confirmed & matched.keys()),
        "false_positives": len(confirmed - matched.keys()),
        "false_negatives": len(labels) - len(confirmed & matched.keys()),
        "review_detections": len(pending & matched.keys()),
        "review_findings": len(pending),
        "high_predictions": len(high),
        "high_true_positives": sum(
            index in matched and labels[matched[index]]["severity"] == "high"
            for index in high
        ),
        "false_clean": int(
            bool(labels) and complete and audit.get("overall_verdict") == "pass"
            and audit.get("review_status") != "required"
        ),
        "incomplete": int(not complete),
        "citation_checks": len(citations),
        "correct_citations": sum(citations),
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scores = [row["score"] for row in rows]
    totals = {
        key: sum(item[key] for item in scores)
        for key in (scores[0] if scores else [])
    }
    expected = totals.get("expected", 0)
    high = totals.get("high_predictions", 0)
    tp, fp = totals.get("true_positives", 0), totals.get("false_positives", 0)
    elapsed = sorted(row.get("elapsed_seconds", 0) for row in rows)
    cases = {row.get("case_id") for row in rows}
    return {
        **totals, "runs": len(rows),
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / expected if expected else None,
        "review_inclusive_recall": (
            (tp + totals["review_detections"]) / expected if expected else None
        ),
        "high_precision": totals["high_true_positives"] / high if high else None,
        "elapsed_seconds": sum(row.get("elapsed_seconds", 0) for row in rows),
        "p95_seconds": elapsed[max(0, (95 * len(elapsed) + 99) // 100 - 1)] if elapsed else None,
        "citation_accuracy": (
            totals["correct_citations"] / totals["citation_checks"]
            if totals.get("citation_checks") else None
        ),
        "unstable_cases": sum(
            len({row.get("finding_signature") for row in rows if row.get("case_id") == key}) > 1
            for key in cases
        ),
        "unpriced_calls": sum((row.get("cost") or {}).get("unpriced_calls", 0) for row in rows),
        "estimated_cost_usd": round(sum(
            (row.get("cost") or {}).get("estimated_cost_usd", 0) for row in rows
        ), 8),
        "model_calls": sum((row.get("cost") or {}).get("calls", 0) for row in rows),
        "usage_unavailable_runs": sum(row.get("cost") is None for row in rows),
        "tokens": {
            field: sum(
                usage.get(field, 0) for row in rows
                for usage in (row.get("cost") or {}).get("usage", [])
            ) for field in TOKEN_FIELDS
        },
    }


def report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups = {}
    names = sorted({row["configuration"] for row in rows})
    for name in names:
        selected = [row for row in rows if row["configuration"] == name]
        groups[name] = {
            "overall": aggregate(selected),
            "profiles": {
                profile: aggregate([row for row in selected if row["profile"] == profile])
                for profile in sorted({row["profile"] for row in selected})
            },
        }
    gates = {}
    for name in names:
        if name == "baseline":
            continue
        reasons = []
        selected = [row for row in rows if row["configuration"] == name]
        baseline = groups.get("baseline", {}).get("profiles", {})
        for profile, metrics in groups[name]["profiles"].items():
            samples = {
                row["case_id"] for row in selected
                if row["profile"] == profile and row["split"] == "holdout"
            }
            if len(samples) < 10 or any(row["split"] != "holdout" for row in selected):
                reasons.append(f"{profile}: gate requires at least 10 held-out cases")
            baseline_samples = {
                row["case_id"] for row in rows
                if row["configuration"] == "baseline" and row["profile"] == profile
                and row["split"] == "holdout"
            }
            if samples != baseline_samples:
                reasons.append(f"{profile}: baseline and candidate must evaluate the same cases")
            if any(
                len({
                    row.get("repeat") for row in rows
                    if row["case_id"] == key and row["configuration"] == configuration
                }) < 3
                for key in samples for configuration in ("baseline", name)
            ):
                reasons.append(f"{profile}: baseline and candidate require three repeats per case")
            if metrics["high_precision"] is None or metrics["high_precision"] < .95:
                reasons.append(f"{profile}: confirmed-HIGH precision is below 95% or unmeasured")
            reference = baseline.get(profile, {}).get("recall")
            if reference is None or metrics["recall"] is None or metrics["recall"] < reference:
                reasons.append(f"{profile}: recall regressed or cannot be compared")
            if metrics.get("false_clean") or metrics.get("incomplete"):
                reasons.append(f"{profile}: false-clean or incomplete results remain")
            if baseline.get(profile, {}).get("incomplete"):
                reasons.append(f"{profile}: baseline contains incomplete results")
            for key in samples:
                compared = [
                    row for row in rows if row["case_id"] == key
                    and row["configuration"] in {"baseline", name}
                ]
                if any(
                    not all((row.get("provenance") or {}).get(field) for field in (
                        "metadata_sha256", "evidence_sha256", "context_sha256",
                    )) for row in compared
                ):
                    reasons.append(f"{profile}/{key}: comparison provenance is missing")
                inputs = {
                    (
                        row["provenance"].get("metadata_sha256"),
                        row["provenance"].get("evidence_sha256"),
                        row["provenance"].get("context_sha256"),
                    )
                    for row in compared if row.get("provenance")
                }
                if len(inputs) > 1:
                    reasons.append(f"{profile}/{key}: source evidence changed between runs")
        gates[name] = {"passed": not reasons, "reasons": reasons}
    return {"configurations": groups, "promotion_gates": gates}


def run(
    manifest: dict[str, Any], output: Path, *, split: str,
    repeats: int = 3, config_path: Path | None = None,
) -> dict[str, Any]:
    cases = [case for case in manifest["cases"] if case["split"] == split]
    if not cases or repeats < 1:
        raise ValueError("select a nonempty split and positive repeat count")
    destination = output.resolve()
    for case in manifest["cases"]:
        source = Path(case["execution_dir"])
        if destination == source or source in destination.parents:
            raise ValueError("evaluation output must be outside every source execution")
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    rows = []
    for case in cases:
        for name, overrides in manifest["configurations"].items():
            settings = config.load(config_path, profile=case["profile"])
            for key, value in overrides.items():
                setattr(settings.auditor, key, value)
            config.validate(settings)
            runtime.configure(settings)
            for repeat in range(1, repeats + 1):
                target = destination / case["id"] / name / f"repeat-{repeat}"
                cfg = auditor.AuditConfig.from_settings(
                    settings.auditor, execution_dir=Path(case["execution_dir"]),
                    output_dir=target, cost_rates=settings.costs.model_rates,
                )
                cfg = replace(cfg, usage_tracker=UsageTracker())
                started = time.monotonic()
                stopped: Exception | None = None
                try:
                    rc = auditor.run(cfg)
                    if rc:
                        raise ValueError(f"auditor returned {rc}")
                    audit = json.loads((target / "audit.json").read_text())
                except Exception as exc:
                    if isinstance(exc, (
                        model_client.ModelInterventionError,
                        model_client.ModelConfigurationError, model_client.ModelInputError,
                    )):
                        stopped = exc
                    audit = {
                        "analysis_status": "incomplete", "findings": [],
                        "error": redact_credentials(str(exc))[:1000],
                    }
                row = {
                    "case_id": case["id"], "profile": case["profile"], "split": split,
                    "configuration": name, "repeat": repeat,
                    "elapsed_seconds": time.monotonic() - started,
                    "score": score(case, audit),
                    "provenance": audit.get("provenance"),
                    "finding_signature": provenance.fingerprint(sorted(
                        finding_identity(finding) for finding in audit.get("findings") or []
                    )),
                    "cost": cfg.usage_tracker.summary(cfg.cost_rates),
                    "manifest_sha256": provenance.fingerprint(manifest),
                }
                rows.append(row)
                (destination / "results.json").write_text(json.dumps(rows, indent=2) + "\n")
                (destination / "metrics.json").write_text(json.dumps(report(rows), indent=2) + "\n")
                if stopped:
                    raise stopped
    return report(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "run"))
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--split", choices=("development", "holdout"), default="development")
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args(argv)
    manifest = load_manifest(args.manifest)
    if args.command == "validate":
        print(f"Validated {len(manifest['cases'])} human-labelled cases.")
        return 0
    if args.output_dir is None:
        parser.error("run requires a new --output-dir")
    metrics = run(
        manifest, args.output_dir, split=args.split, repeats=args.repeats,
        config_path=args.config,
    )
    print(json.dumps(metrics, indent=2))
    return int(args.split == "holdout" and any(
        not gate["passed"] for gate in metrics["promotion_gates"].values()
    ))


if __name__ == "__main__":
    raise SystemExit(main())
