"""Request compatibility for Claude models invoked through the public Messages API."""
from __future__ import annotations

import re
from typing import Any


def model_generation(model_id: str) -> int | None:
    """Read the family generation, without confusing minor versions or dates."""
    match = re.search(r"claude-(?:sonnet|opus|haiku)-(\d+)", model_id.lower())
    return int(match.group(1)) if match else None


def prepare_request(
    body: dict[str, Any],
    *,
    model_id: str,
    thinking_mode: str = "disabled",
) -> dict[str, Any]:
    """Preserve bounded extraction without sending unsupported 5.x parameters.

    Sonnet 5.5 replaces disabled thinking with ``between_tools`` and rejects
    forced tool choice. Haiku 5.5 still accepts disabled thinking and forced
    tools. Keep provider-default thinking available for audit replays.
    """
    request = dict(body)
    generation = model_generation(model_id)
    if generation is None or generation < 5:
        return request
    for parameter in ("temperature", "top_p", "top_k"):
        request.pop(parameter, None)
    sonnet_55 = re.search(r"claude-sonnet-5-5(?:$|[-:])", model_id.lower()) is not None
    if thinking_mode == "disabled":
        request["thinking"] = {
            "type": "between_tools" if sonnet_55 else "disabled",
        }
    tool_choice = request.get("tool_choice") or {}
    if sonnet_55 and tool_choice.get("type") in {"tool", "any"}:
        request["tool_choice"] = {"type": "auto"}
        name = tool_choice.get("name")
        instruction = (
            f"Return your result by calling the {name} tool."
            if name else "Return your result by calling one of the provided tools."
        )
        system = request.get("system") or ""
        request["system"] = (
            [*system, {"type": "text", "text": instruction}]
            if isinstance(system, list)
            else f"{system}\n\n{instruction}".strip()
        )
    return request
