"""One-time plain-language backfill for model-written finding descriptions."""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


from argus import config as argus_config
from argus.core import atomic_io, exec_paths
from argus.pipeline import auditor, model_client
from argus.reporting import report
from argus.reporting.auditor_render import render_markdown


_TOOL_NAME = "rewrite_findings"
_MAX_DESCRIPTION_WORDS = 60
_TARGET_DESCRIPTION_WORDS = 50
_RETRY_DESCRIPTION_WORDS = 40
_MAX_REWRITE_ATTEMPTS = 3
_SYSTEM_PROMPT = """You rewrite existing QA audit finding descriptions for clarity.

Write for a QA reviewer who does not know ARGUS rules or internal audit
terminology. For every input item:
- Preserve the exact factual meaning, scope, uncertainty, names, identifiers,
  page references, test statuses, and technical values.
- Use 1-2 short, direct sentences and no more than 60 words.
- Lead with what the screenshots show. Briefly state the expected result only
  when needed to explain the gap; do not repeat the full test instruction.
- When the original is uncertain, retain that uncertainty. Do not turn "could
  not find" or "may" into a definite claim.
- Replace policy jargon with everyday language. Explain an unavoidable
  technical term the first time, for example "Chrome's connected-device page
  (`chrome://inspect/#devices`)".
- Do not define familiar product labels in parentheses or add filler such as
  "actually", "this is a minor gap", or "the core objective".
- Do not change verdicts, severity, category, confidence, action, or any other
  field. Do not add recommendations or facts absent from the original.

Preferred style:
"The screenshots only show the Play and Follow buttons before they were
selected. They do not show the podcast playing or confirm that it was added
to the library, so this step cannot be verified."

Return exactly one rewrite for every supplied id by calling the provided tool.
"""

_REWRITE_TOOL: dict[str, Any] = {
    "name": _TOOL_NAME,
    "description": "Return plain-language descriptions for the supplied findings.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["rewrites"],
        "properties": {
            "rewrites": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id", "description"],
                    "properties": {
                        "id": {"type": "string"},
                        "description": {"type": "string"},
                    },
                },
            },
        },
    },
}


@dataclass(frozen=True)
class Candidate:
    id: str
    audit_path: Path
    finding_index: int
    original_description: str
    test_step: str
    finding_context: dict[str, Any] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )

    def prompt_value(self) -> dict[str, str]:
        return {
            "id": self.id,
            "test_step": self.test_step or "(step description unavailable)",
            "original_description": self.original_description,
        }


@dataclass
class BackfillInput:
    root: Path
    audits: dict[Path, dict[str, Any]]
    metadata: dict[Path, dict[str, Any]]
    candidates: list[Candidate]


def _read_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _step_descriptions(metadata: dict[str, Any]) -> dict[int, str]:
    results = metadata.get("test_results") or []
    test_result = results[0] if results and isinstance(results[0], dict) else {}
    descriptions: dict[int, str] = {}
    for step in test_result.get("steps") or []:
        if not isinstance(step, dict):
            continue
        index = step.get("index")
        if not isinstance(index, int):
            continue
        description = report._plain_step_description(step.get("description"))
        if description:
            descriptions[index] = description
    return descriptions


def collect_backfill_input(root: Path) -> BackfillInput:
    root = root.resolve()
    audits: dict[Path, dict[str, Any]] = {}
    metadata_by_audit: dict[Path, dict[str, Any]] = {}
    candidates: list[Candidate] = []

    for audit_path in sorted(exec_paths.iter_audit_jsons(root)):
        audit = _read_json_object(audit_path)
        metadata_path = audit_path.with_name("metadata.json")
        metadata = (
            _read_json_object(metadata_path) if metadata_path.exists() else {}
        )
        steps = _step_descriptions(metadata)
        findings = audit.get("findings") or []
        if not isinstance(findings, list):
            raise ValueError(f"{audit_path}: findings must be a list")

        audit_candidates: list[Candidate] = []
        for finding_index, finding in enumerate(findings):
            if not isinstance(finding, dict):
                continue
            if finding.get("source") not in (None, "model"):
                continue
            description = finding.get("description")
            if not isinstance(description, str) or not description.strip():
                continue
            step_index = finding.get("step_index")
            audit_candidates.append(Candidate(
                id=f"finding-{len(candidates) + len(audit_candidates):04d}",
                audit_path=audit_path,
                finding_index=finding_index,
                original_description=description.strip(),
                test_step=(
                    steps.get(step_index, "") if isinstance(step_index, int) else ""
                ),
                finding_context={
                    key: copy.deepcopy(value)
                    for key, value in finding.items()
                    if key != "description"
                },
            ))

        if audit_candidates:
            audits[audit_path] = audit
            metadata_by_audit[audit_path] = metadata
            candidates.extend(audit_candidates)

    return BackfillInput(
        root=root,
        audits=audits,
        metadata=metadata_by_audit,
        candidates=candidates,
    )


def _candidate_batches(
    candidates: Iterable[Candidate],
    *,
    max_items: int,
    max_chars: int,
) -> list[list[Candidate]]:
    batches: list[list[Candidate]] = []
    current: list[Candidate] = []
    current_chars = 0
    for candidate in candidates:
        item_chars = len(json.dumps(candidate.prompt_value()))
        if current and (
            len(current) >= max_items or current_chars + item_chars > max_chars
        ):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(candidate)
        current_chars += item_chars
    if current:
        batches.append(current)
    return batches


def _extract_rewrites(
    raw: dict[str, Any],
    expected_ids: set[str],
) -> dict[str, str]:
    tool_inputs: list[dict[str, Any]] = []
    for block in raw.get("content") or []:
        if not isinstance(block, dict):
            continue
        tool_input = block.get("input")
        if (
            block.get("type") == "tool_use"
            and block.get("name") == _TOOL_NAME
            and isinstance(tool_input, dict)
        ):
            tool_inputs.append(tool_input)
    if len(tool_inputs) != 1:
        raise ValueError(
            f"expected one {_TOOL_NAME} tool call, received {len(tool_inputs)}"
        )

    rows = tool_inputs[0].get("rewrites")
    if not isinstance(rows, list):
        raise ValueError("rewrite tool output must contain a rewrites list")

    rewrites: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each rewrite must be an object")
        rewrite_id = row.get("id")
        description = row.get("description")
        if not isinstance(rewrite_id, str) or rewrite_id not in expected_ids:
            raise ValueError(f"unexpected rewrite id: {rewrite_id!r}")
        if rewrite_id in rewrites:
            raise ValueError(f"duplicate rewrite id: {rewrite_id}")
        if not isinstance(description, str) or not description.strip():
            raise ValueError(f"empty description for rewrite id: {rewrite_id}")
        rewrites[rewrite_id] = description.strip()

    missing = expected_ids - rewrites.keys()
    if missing:
        raise ValueError(f"missing rewrite ids: {', '.join(sorted(missing))}")
    return rewrites


def _word_count(value: str) -> int:
    return len(re.findall(r"\S+", value))


def _overlong_rewrite_ids(rewrites: dict[str, str]) -> set[str]:
    return {
        rewrite_id
        for rewrite_id, description in rewrites.items()
        if _word_count(description) > _MAX_DESCRIPTION_WORDS
    }


def _semantic_signature(
    candidate: Candidate,
    description: str,
) -> tuple[Any, ...]:
    probe = copy.deepcopy(candidate.finding_context)
    probe["description"] = description
    normalised = auditor._normalise_findings_for_schema([probe])[0]
    return tuple(
        normalised.get(key)
        for key in (
            "severity",
            "category",
            "action",
            "confidence",
            "_severity_adjusted_from",
        )
    )


def _semantic_drift_ids(
    candidates: list[Candidate],
    rewrites: dict[str, str],
) -> set[str]:
    return {
        candidate.id
        for candidate in candidates
        if _semantic_signature(
            candidate,
            rewrites[candidate.id],
        ) != _semantic_signature(
            candidate,
            candidate.original_description,
        )
    }


def _invoke_rewrite_batch(
    client: Any,
    cfg: auditor.AuditConfig,
    batch: list[Candidate],
    *,
    model_id: str,
    operation: str,
    retry: bool,
) -> dict[str, str]:
    requested_words = (
        _RETRY_DESCRIPTION_WORDS if retry else _TARGET_DESCRIPTION_WORDS
    )
    request = {
        "constraints": {
            "sentences": "1-2",
            "target_maximum_words": requested_words,
            "hard_maximum_words": _MAX_DESCRIPTION_WORDS,
        },
        "findings": [candidate.prompt_value() for candidate in batch],
    }
    if retry:
        request["retry_instruction"] = (
            "The previous rewrite was too long or changed the finding's "
            "classification. Compress it to "
            f"{_RETRY_DESCRIPTION_WORDS} words or fewer while preserving "
            "whether the original describes missing evidence, uncertainty, "
            "or a direct contradiction. Keep only the expected result, the "
            "relevant screenshot evidence, and why the step could not be "
            "confirmed. Remove background, impact, severity rationale, and "
            "repeated context."
        )
    body = auditor._apply_thinking(auditor._apply_temperature({

        "max_tokens": 12000,
        "system": _SYSTEM_PROMPT,
        "messages": [{
            "role": "user",
            "content": [{
                "type": "text",
                "text": model_client.guard_content(
                    json.dumps(request, ensure_ascii=True)
                ),
            }],
        }],
        "tools": [_REWRITE_TOOL],
        "tool_choice": {"type": "tool", "name": _TOOL_NAME},
    }, cfg), cfg)
    response = model_client.invoke_model(
        client,
        model_id=model_id,
        body=json.dumps(body),
        content_type="application/json",
        accept="application/json",
    )
    raw = json.loads(response["body"].read())
    cfg.usage_tracker.record_response(
        raw,
        model_id=model_id,
        operation=operation,
    )
    return _extract_rewrites(
        raw,
        {candidate.id for candidate in batch},
    )


def _rewrite_candidate_list(
    candidates: list[Candidate],
    *,
    cfg: auditor.AuditConfig,
    max_items: int,
    max_chars: int,
    operation: str,
) -> dict[str, str]:
    client = auditor._make_model_client(
        cfg,
        cache_key="finding-copy-backfill",
    )

    rewrites: dict[str, str] = {}
    batches = _candidate_batches(
        candidates,
        max_items=max_items,
        max_chars=max_chars,
    )
    for batch_index, batch in enumerate(batches, start=1):
        print(
            f"[{batch_index}/{len(batches)}] rewriting {len(batch)} finding(s)",
            file=sys.stderr,
        )
        pending = batch
        for attempt in range(1, _MAX_REWRITE_ATTEMPTS + 1):
            batch_rewrites = _invoke_rewrite_batch(
                client,
                cfg,
                pending,
                model_id=cfg.model_id,
                operation=operation,
                retry=attempt > 1,
            )
            overlong = _overlong_rewrite_ids(batch_rewrites)
            semantic_drift = _semantic_drift_ids(pending, batch_rewrites)
            rejected = overlong | semantic_drift
            rewrites.update({
                rewrite_id: description
                for rewrite_id, description in batch_rewrites.items()
                if rewrite_id not in rejected
            })
            if not rejected:
                break
            pending = [
                candidate for candidate in pending
                if candidate.id in rejected
            ]
            if attempt < _MAX_REWRITE_ATTEMPTS:
                print(
                    f"[{batch_index}/{len(batches)}] retrying "
                    f"{len(pending)} rejected finding(s) "
                    f"(attempt {attempt + 1}/{_MAX_REWRITE_ATTEMPTS})",
                    file=sys.stderr,
                )
        else:
            ids = ", ".join(candidate.id for candidate in pending)
            raise ValueError(
                "rewrite descriptions still exceed the length limit or "
                "change semantic classification after "
                f"{_MAX_REWRITE_ATTEMPTS} attempts: {ids}"
            )

    return rewrites


def rewrite_candidates(
    backfill: BackfillInput,
    *,
    api_key_env: str,
    model_api_url: str,
    model_id: str,
    max_items: int,
    max_chars: int,
    model_rates: dict[str, dict[str, float]],
) -> tuple[dict[str, str], dict[str, Any] | None]:
    cfg = auditor.AuditConfig(
        execution_dir=backfill.root,
        api_key_env=api_key_env,
        model_api_url=model_api_url,
        model_id=model_id,
    )
    rewrites = _rewrite_candidate_list(
        backfill.candidates,
        cfg=cfg,
        max_items=max_items,
        max_chars=max_chars,
        operation="finding_copy_backfill",
    )
    return rewrites, cfg.usage_tracker.summary(model_rates)


def rewrite_overlong_audit_findings(
    audit: dict[str, Any],
    metadata: dict[str, Any],
    cfg: auditor.AuditConfig,
) -> dict[str, Any]:
    """Shorten overlong model findings before a fresh audit is persisted."""
    steps = _step_descriptions(metadata)
    candidates: list[Candidate] = []
    for finding_index, finding in enumerate(audit.get("findings") or []):
        if not isinstance(finding, dict):
            continue
        if finding.get("source") not in (None, "model"):
            continue
        description = finding.get("description")
        if not isinstance(description, str):
            continue
        if _word_count(description) <= _MAX_DESCRIPTION_WORDS:
            continue
        step_index = finding.get("step_index")
        candidates.append(Candidate(
            id=f"fresh-finding-{finding_index:04d}",
            audit_path=cfg.execution_dir / "audit.json",
            finding_index=finding_index,
            original_description=description.strip(),
            test_step=(
                steps.get(step_index, "") if isinstance(step_index, int) else ""
            ),
            finding_context={
                key: copy.deepcopy(value)
                for key, value in finding.items()
                if key != "description"
            },
        ))

    if not candidates:
        return audit

    rewrites = _rewrite_candidate_list(
        candidates,
        cfg=cfg,
        max_items=25,
        max_chars=45_000,
        operation="finding_copy_repair",
    )
    updated = copy.deepcopy(audit)
    for candidate in candidates:
        updated["findings"][candidate.finding_index]["description"] = (
            rewrites[candidate.id]
        )
    return updated


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def apply_rewrites(
    backfill: BackfillInput,
    rewrites: dict[str, str],
    *,
    api_key_env: str,
    model_api_url: str,
    model_id: str,
    usage: dict[str, Any] | None,
) -> Path:
    expected_ids = {candidate.id for candidate in backfill.candidates}
    if rewrites.keys() != expected_ids:
        missing = expected_ids - rewrites.keys()
        extra = rewrites.keys() - expected_ids
        raise ValueError(
            f"rewrite set mismatch; missing={sorted(missing)}, extra={sorted(extra)}"
        )

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d_%H%M%SZ")
    backup_root = backfill.root / f"_finding_copy_backup_{stamp}"
    backup_paths: dict[Path, tuple[Path, Path | None]] = {}

    for audit_path in backfill.audits:
        relative_dir = audit_path.parent.relative_to(backfill.root)
        destination_dir = backup_root / relative_dir
        destination_dir.mkdir(parents=True, exist_ok=True)
        audit_backup = destination_dir / "audit.original.json"
        shutil.copy2(audit_path, audit_backup)
        markdown_path = audit_path.with_name("audit.md")
        markdown_backup: Path | None = None
        if markdown_path.exists():
            markdown_backup = destination_dir / "audit.original.md"
            shutil.copy2(markdown_path, markdown_backup)
        backup_paths[audit_path] = (audit_backup, markdown_backup)

    candidates_by_audit: dict[Path, list[Candidate]] = {}
    for candidate in backfill.candidates:
        candidates_by_audit.setdefault(candidate.audit_path, []).append(candidate)

    written: list[Path] = []
    try:
        for audit_path, original in backfill.audits.items():
            updated = copy.deepcopy(original)
            findings = updated["findings"]
            for candidate in candidates_by_audit[audit_path]:
                finding = findings[candidate.finding_index]
                if finding.get("description", "").strip() != (
                    candidate.original_description
                ):
                    raise ValueError(
                        f"{audit_path}: finding {candidate.finding_index} changed "
                        "after collection"
                    )
                finding["description"] = rewrites[candidate.id]

            invariant_copy = copy.deepcopy(updated)
            for candidate in candidates_by_audit[audit_path]:
                invariant_copy["findings"][candidate.finding_index][
                    "description"
                ] = candidate.original_description
            if invariant_copy != original:
                raise ValueError(
                    f"{audit_path}: attempted to change fields other than description"
                )

            atomic_io.atomic_write_json(audit_path, updated)
            written.append(audit_path)
            metadata = backfill.metadata[audit_path]
            debug = any(
                key in updated
                for key in (
                    "_chunk_summary",
                    "_chunk_diagnostics",
                    "_consensus",
                    "_invalid_original",
                    "_invalid_repair",
                    "_raw",
                )
            )
            atomic_io.atomic_write_text(
                audit_path.with_name("audit.md"),
                render_markdown(updated, metadata, debug=debug),
            )
    except Exception:
        for audit_path in written:
            audit_backup, markdown_backup = backup_paths[audit_path]
            atomic_io.atomic_write_text(audit_path, audit_backup.read_text())
            markdown_path = audit_path.with_name("audit.md")
            if markdown_backup is not None:
                atomic_io.atomic_write_text(
                    markdown_path,
                    markdown_backup.read_text(),
                )
        raise

    manifest = {
        "version": 1,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "root": str(backfill.root),
        "api_key_env": api_key_env,
        "model_api_url": model_api_url,
        "model_id": model_id,
        "audits_updated": len(backfill.audits),
        "descriptions_updated": len(backfill.candidates),
        "usage": usage,
        "backups": [
            {
                "audit": str(path.relative_to(backfill.root)),
                "original": str(backup.relative_to(backfill.root)),
                "sha256": _sha256(backup),
            }
            for path, (backup, _markdown) in sorted(
                backup_paths.items(),
                key=lambda item: str(item[0]),
            )
        ],
    }
    atomic_io.atomic_write_json(backup_root / "manifest.json", manifest)
    return backup_root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--api-key-env", default="ARGUS_MODEL_API_KEY")
    parser.add_argument("--model-api-url", default=None)
    parser.add_argument("--model-id", default=None)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--max-batch-chars", type=int, default=45_000)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="invoke model API, back up originals, and update descriptions",
    )
    args = parser.parse_args(argv)

    if args.batch_size < 1 or args.max_batch_chars < 1:
        parser.error("batch limits must be positive")

    settings = argus_config.load()
    model_api_url = args.model_api_url or settings.auditor.model_api_url
    model_id = args.model_id or settings.auditor.model_id
    backfill = collect_backfill_input(args.root)
    print(
        f"found {len(backfill.candidates)} model finding(s) in "
        f"{len(backfill.audits)} audit(s)"
    )
    if not args.apply or not backfill.candidates:
        return 0

    rewrites, usage = rewrite_candidates(
        backfill,
        api_key_env=args.api_key_env,
        model_api_url=model_api_url,
        model_id=model_id,
        max_items=args.batch_size,
        max_chars=args.max_batch_chars,
        model_rates=settings.costs.model_rates,
    )
    backup_root = apply_rewrites(
        backfill,
        rewrites,
        api_key_env=args.api_key_env,
        model_api_url=model_api_url,
        model_id=model_id,
        usage=usage,
    )
    print(f"updated {len(rewrites)} description(s)")
    print(f"backups: {backup_root}")
    if usage is not None:
        print(
            "estimated cost: "
            f"${float(usage.get('estimated_cost_usd') or 0):.6f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
