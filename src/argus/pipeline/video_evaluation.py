"""Compare video models on extracted evidence without changing saved audits."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import time
from typing import Any

from argus import config, runtime
from argus.core.costs import UsageTracker
from argus.core.redaction import redact_credentials
from argus.pipeline import auditor, model_client, video_evidence
from argus.reporting.verdicts import verdict_label



def evaluate(
    cfg: auditor.AuditConfig,
    models: list[str],
    output_dir: Path,
) -> list[dict[str, Any]]:
    """Write an isolated, resumable-by-inspection comparison for one execution.

    The destination must be new and outside the extracted execution. Guardrail
    failures stop the comparison; a second model is never used to bypass one.
    """
    if not models or any(not model.strip() for model in models):
        raise ValueError("select one or more non-empty video model IDs")
    execution_dir = cfg.execution_dir.resolve()
    destination = output_dir.resolve()
    if destination == execution_dir or execution_dir in destination.parents:
        raise ValueError("evaluation output must be outside the extracted execution")
    metadata = json.loads((execution_dir / "metadata.json").read_text())
    records = video_evidence.load_video_records(execution_dir)
    if not records:
        raise video_evidence.VideoEvidenceError("no validated video recordings found")
    steps = auditor.build_steps_block(metadata, variant_name=cfg.variant_name)
    destination.mkdir(parents=True, exist_ok=False)
    results = []
    for index, model in enumerate(dict.fromkeys(models), start=1):
        candidate = replace(cfg, video_model_id=model, usage_tracker=UsageTracker())
        row: dict[str, Any] = {
            "model": model, "model_id": candidate.video_model_id,
            "judge_model_id": cfg.model_id if cfg.video_audio_model_id else None,
            "audio_model_id": cfg.video_audio_model_id,
            "execution": execution_dir.name,
        }
        started = time.monotonic()
        blocked = None
        try:
            row["audit"] = video_evidence.audit_videos(candidate, steps, records)
            row["status"] = "completed"
        except (
            model_client.ModelConfigurationError, model_client.ModelInputError,
            model_client.ModelInterventionError,
        ) as exc:
            blocked = exc
            row.update(status="guardrail_stopped", error=redact_credentials(str(exc))[:2000])
        except Exception as exc:
            row.update(
                status="error",
                error=f"{type(exc).__name__}: {redact_credentials(str(exc))[:2000]}",
            )
        row["elapsed_seconds"] = round(time.monotonic() - started, 2)
        row["cost"] = candidate.usage_tracker.summary(cfg.cost_rates)
        (destination / f"model-{index}.json").write_text(json.dumps(row, indent=2) + "\n")
        results.append(row)
        (destination / "comparison.md").write_text(render_comparison(results))
        print(f"{execution_dir.name} / {model}: {row['status']} ({row['elapsed_seconds']}s)", flush=True)
        if blocked:
            raise blocked
    return results


def render_comparison(results: list[dict[str, Any]]) -> str:
    lines = [
        "# Video/audio model comparison", "",
        "These are model judgments, not measured accuracy. A human-reviewed reference "
        "is required to compare missed defects, false findings, and audio correctness.", "",
        "| Execution | Model | Result | Audio verification | Findings | Seconds | Unpriced calls |",
        "| --- | --- | --- | --- | ---: | ---: | ---: |",
    ]
    for row in results:
        audit = row.get("audit") or {}
        audio = (audit.get("_video_evidence") or {}).get("audio_verdict", "not analyzed")
        label = verdict_label(audit.get("overall_verdict")) if audit else row["status"]
        cost = row.get("cost") or {}
        lines.append(
            f"| {row['execution']} | {row['model']} | {label} | {audio} | "
            f"{len(audit.get('findings') or [])} | {row['elapsed_seconds']} | "
            f"{cost.get('unpriced_calls', 0)} |"
        )
    lines += [
        "", "Unpriced calls are excluded from token-cost estimates; they are not free. "
        "See each model's JSON for findings, timestamps, coverage limitations, and errors.", "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("execution_dirs", nargs="+", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path, help="New comparison directory")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--profile", help="ARGUS team profile")
    parser.add_argument("--verify-audio", action="store_true", help="Use the configured audio transcription API")
    parser.add_argument("--models", nargs="+", default=None, help="Video model IDs accepted by the configured API")
    args = parser.parse_args(argv)
    settings = config.load(args.config, profile=args.profile)
    if args.verify_audio and not settings.auditor.video_audio_model_id:
        parser.error("--verify-audio requires auditor.video_audio_model_id and audio_api_url")
    config.validate(settings)
    runtime.configure(settings)
    output = args.output_dir.resolve()
    executions = [path.resolve() for path in args.execution_dirs]
    if any(output == path or path in output.parents for path in executions):
        parser.error("--output-dir must be outside the extracted executions")
    output.mkdir(parents=True, exist_ok=False)
    results = []
    for index, execution in enumerate(executions, start=1):
        cfg = auditor.AuditConfig.from_settings(
            settings.auditor, execution_dir=execution,
            cost_rates=settings.costs.model_rates,
        )
        rows = evaluate(cfg, args.models or [cfg.video_model_id], output / f"{index}-{execution.name}")
        results.extend(rows)
        (output / "comparison.md").write_text(render_comparison(results))
    return int(any(row["status"] != "completed" for row in results))


if __name__ == "__main__":
    raise SystemExit(main())
