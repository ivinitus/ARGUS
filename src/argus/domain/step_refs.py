"""Helpers for presenting stored zero-based Zephyr step indexes."""
from __future__ import annotations


def human_step_number(step_index: object) -> int | str:
    """Return the one-based step number shown to testers and reviewers."""
    if isinstance(step_index, int) and not isinstance(step_index, bool):
        return step_index + 1
    return "?"


def prompt_step_reference(step_index: object) -> str:
    """Show a human label while retaining the raw index expected in JSON."""
    human = human_step_number(step_index)
    if isinstance(step_index, int) and not isinstance(step_index, bool):
        return f"Step {human} (step_index={step_index})"
    return f"Step {human}"
