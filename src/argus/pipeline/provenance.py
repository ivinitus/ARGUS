"""Reproducible audit configuration fingerprints without credential values."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from typing import Any

_policy_hash: str | None = None
_team_rules_hash: str | None = None


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def file_fingerprint(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def configure(settings: Any) -> None:
    global _policy_hash, _team_rules_hash
    _policy_hash = fingerprint(asdict(settings.policy))
    _team_rules_hash = getattr(getattr(settings, "auditor", None), "rule_snapshot", {}).get("sha256")


def describe(cfg: Any, metadata: dict[str, Any], system_prompt: str) -> dict[str, Any]:
    from argus.pipeline import evidence_review

    settings = {
        "model_id": cfg.model_id,
        "video_model_id": cfg.video_model_id,
        "audio_model_id": cfg.video_audio_model_id,
        "temperature": cfg.temperature,
        "thinking_mode": cfg.thinking_mode,
        "max_output_tokens": cfg.max_output_tokens,
        "consensus_enabled": cfg.consensus_enabled,
        "structured_output": cfg.structured_output,
        "evidence_review_mode": cfg.evidence_review_mode,
        "chunk_size": cfg.chunk_size,
        "chunk_overlap": cfg.chunk_overlap,
        "chunk_threshold": cfg.chunk_threshold,
        "chunk_page_byte_budget_mb": cfg.chunk_page_byte_budget_mb,
        "chunk_attachment_byte_budget_mb": cfg.chunk_attachment_byte_budget_mb,
        "max_pages": cfg.max_pages,
        "history_in_prompt": cfg.history_in_prompt,
        "prompt_sha256": fingerprint(system_prompt),
        "policy_sha256": _policy_hash,
        **({"team_rules_sha256": _team_rules_hash} if _team_rules_hash else {}),
        "review_contract_sha256": fingerprint([
            evidence_review.CONTRACT_VERSION, evidence_review._CORE,
            evidence_review._OBSERVE, evidence_review._REVIEW,
        ]),
    }
    return {
        **settings,
        "configuration_sha256": fingerprint(settings),
        "metadata_sha256": fingerprint(metadata),
        "pipeline_version": "accuracy-v1",
    }
