"""User-facing verdict labels; persisted audit values remain stable."""
from __future__ import annotations

VERDICT_LABELS = {
    "pass": "Compliant",
    "concerns": "Concerns",
    "fail": "Deviation",
    "incomplete": "Incomplete",
}


def verdict_label(verdict: str | None) -> str:
    return VERDICT_LABELS.get(verdict or "", verdict or "Unknown")


def audio_verification_label(verdict: str | None) -> str:
    return {
        "supported": "Supported",
        "concerns": "Concerns",
        "unverified": "Unverified",
        "not_applicable": "Not required by this test",
    }.get(verdict or "", "Not analyzed")


def requirement_label(requirement_id: str) -> str:
    if requirement_id.startswith("step:") and requirement_id[5:].isdigit():
        return f"Step {int(requirement_id[5:]) + 1}"
    return {"team_policy": "Team evidence requirements", "objective": "Test objective"}.get(
        requirement_id, "Required outcome",
    )
