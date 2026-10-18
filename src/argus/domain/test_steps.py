"""Recognize written test steps, including rich-text Zephyr content."""
from __future__ import annotations

import html
import re
from typing import Any


def has_step_definition(step: dict[str, Any]) -> bool:
    """Empty rich-text editor markup is not a test instruction or expectation."""
    for key in ("description", "expected_result"):
        value = str(step.get(key) or "")
        if re.search(
            r"<(?:img|video|audio|object|embed|iframe|svg|canvas|picture|source|math)\b",
            value, re.I,
        ):
            return True
        text = html.unescape(re.sub(r"<[^>]*>", "", value))
        if text.replace("\u200b", "").replace("\ufeff", "").strip():
            return True
    return False
