"""Shared fail-closed completion metadata for one audit key."""
from __future__ import annotations

from typing import Any

VALID_STATUSES = frozenset({"complete", "incomplete", "not_applicable"})
MODEL_FAILURE_KEYS = (
    "_runtime_diagnostic", "_raw", "_invalid_original", "_invalid_repair",
)
MODULES = (
    "extraction",
    "screenshots",
    "environment",
    "xml",
    "model",
    "guardrail",
    "custom_rules",
    "publication",
)


class AnalysisIncompleteError(RuntimeError):
    """A required analysis module could not produce a trustworthy result."""

    def __init__(self, module: str, diagnostic: str) -> None:
        if module not in MODULES:
            raise ValueError(f"unknown analysis module {module!r}")
        super().__init__(diagnostic)
        self.module = module
        self.diagnostic = diagnostic


def model_failure(audit: dict[str, Any]) -> bool:
    """Recognize failed assessments, including artifacts written by older builds."""
    if any(key in audit for key in MODEL_FAILURE_KEYS):
        return True
    if audit.get("error") in {
        "parse_failure", "schema_repair_parse_failure", "schema_validation_failure",
    }:
        return True
    chunks = audit.get("_chunk_diagnostics")
    return isinstance(chunks, dict) and any(
        isinstance(chunk, dict) and model_failure(chunk) for chunk in chunks.values()
    )


def set_module(
    audit: dict[str, Any],
    module: str,
    status: str,
    diagnostic: str | None = None,
) -> dict[str, Any]:
    """Set one module outcome and recompute the key-level status."""
    if module not in MODULES:
        raise ValueError(f"unknown analysis module {module!r}")
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid analysis status {status!r}")
    modules = audit.setdefault("analysis_modules", {})
    if not isinstance(modules, dict):
        modules = audit["analysis_modules"] = {}
    outcome: dict[str, Any] = {"status": status}
    if diagnostic:
        outcome["diagnostic"] = str(diagnostic)[:2_000]
    modules[module] = outcome
    audit["analysis_status"] = derive_status(audit)
    return audit


def derive_status(audit: dict[str, Any]) -> str:
    """Derive complete/incomplete/not_applicable from known module outcomes."""
    if model_failure(audit):
        return "incomplete"
    modules = audit.get("analysis_modules")
    if not isinstance(modules, dict) or not modules:
        status = audit.get("analysis_status")
        return status if status in VALID_STATUSES else "complete"
    statuses = {
        outcome.get("status")
        for outcome in modules.values()
        if isinstance(outcome, dict)
    }
    if "incomplete" in statuses:
        return "incomplete"
    non_publication = {
        module: outcome
        for module, outcome in modules.items()
        if module != "publication" and isinstance(outcome, dict)
    }
    if non_publication and all(
        outcome.get("status") == "not_applicable"
        for outcome in non_publication.values()
    ):
        return "not_applicable"
    if "complete" in statuses:
        return "complete"
    return "not_applicable"


def diagnostic(audit: dict[str, Any]) -> str | None:
    """Return a compact diagnostic for the first incomplete module."""
    if model_failure(audit):
        return "model: model assessment did not complete; re-audit is required"
    modules = audit.get("analysis_modules")
    if not isinstance(modules, dict):
        return None
    for module in MODULES:
        outcome = modules.get(module)
        if isinstance(outcome, dict) and outcome.get("status") == "incomplete":
            detail = str(outcome.get("diagnostic") or "analysis did not complete")
            return f"{module}: {detail}"
    return None
