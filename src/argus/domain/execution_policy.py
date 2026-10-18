"""Deterministic execution-mode policy derived from test metadata."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

DESKTOP_BROWSER = "desktop_browser"
MOBILE_EMULATION_ALLOWED = "mobile_emulation_allowed"
PHYSICAL_DEVICE = "physical_device"
UNKNOWN = "unknown"

_PLAYBACK_SUITE_RE = re.compile(
    r"\bmobile\s*-\s*playback\s*-\s*(?:android|ios)\b",
    flags=re.I,
)
_PHYSICAL_MARKER_RE = re.compile(
    r"\b(?:listen\s+in\s+app|physical\s+device)\b",
    flags=re.I,
)
_WEBVIEW_RE = re.compile(r"\bweb\s*view\b", flags=re.I)
_DESKTOP_RE = re.compile(r"\bdesktop\b", flags=re.I)
_MOBILE_RE = re.compile(r"\bmobile\b", flags=re.I)


def _first_result(metadata: dict[str, Any]) -> dict[str, Any]:
    results = metadata.get("test_results") or []
    return results[0] if results and isinstance(results[0], dict) else {}


def _assigned_text(
    metadata: dict[str, Any],
    execution_dir: Path | str,
) -> str:
    tr = _first_result(metadata)
    # Only the execution's own leaf and its run/tester parents carry
    # assignment signal; the absolute prefix (e.g. ~/Desktop/...) must not.
    dir_text = (
        " ".join(Path(str(execution_dir)).parts[-3:])
        if str(execution_dir) else ""
    )
    return " ".join([
        dir_text,
        str(tr.get("testrun_name") or ""),
        str(tr.get("test_case_name") or ""),
        str(tr.get("test_case_objective") or ""),
        str(tr.get("test_case_precondition") or ""),
    ])


def expected_execution_mode(
    metadata: dict[str, Any],
    execution_dir: Path | str,
) -> str:
    """Resolve required execution mode without inspecting screenshots."""
    tr = _first_result(metadata)
    explicit = str(tr.get("expected_execution_mode") or "").strip().lower()
    explicit_modes = {
        DESKTOP_BROWSER: DESKTOP_BROWSER,
        "desktop": DESKTOP_BROWSER,
        MOBILE_EMULATION_ALLOWED: MOBILE_EMULATION_ALLOWED,
        "mobile": MOBILE_EMULATION_ALLOWED,
        PHYSICAL_DEVICE: PHYSICAL_DEVICE,
        "physical_mobile": PHYSICAL_DEVICE,
        "webview": PHYSICAL_DEVICE,
    }
    if explicit in explicit_modes:
        return explicit_modes[explicit]

    surface_modes = {
        "webview": PHYSICAL_DEVICE,
        "desktop": DESKTOP_BROWSER,
        "mobile": MOBILE_EMULATION_ALLOWED,
    }
    surface = str(tr.get("expected_surface") or "").strip().lower()
    if surface in surface_modes:
        return surface_modes[surface]

    assigned_text = _assigned_text(metadata, execution_dir)
    if _WEBVIEW_RE.search(assigned_text):
        return PHYSICAL_DEVICE
    if (
        _PHYSICAL_MARKER_RE.search(assigned_text)
        or _PLAYBACK_SUITE_RE.search(assigned_text)
    ):
        return PHYSICAL_DEVICE
    if _MOBILE_RE.search(assigned_text):
        return MOBILE_EMULATION_ALLOWED
    if _DESKTOP_RE.search(assigned_text):
        return DESKTOP_BROWSER
    return UNKNOWN
