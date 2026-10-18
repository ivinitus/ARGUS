"""Model usage aggregation and configurable cost estimation."""
from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

log = logging.getLogger("argus.costs")

RATE_FIELDS = (
    "input_per_million",
    "output_per_million",
    "cache_write_per_million",
    "cache_read_per_million",
)
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_write_input_tokens",
    "cache_read_input_tokens",
)

def default_model_rates() -> dict[str, dict[str, float]]:
    """Rates are explicit configuration, so estimates never invent a price.

    Set ``costs.model_rates`` for the provider and models in use. Unconfigured
    models still report calls and tokens, with a visible unpriced-call count.
    """
    return {}


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _normalise_usage(raw: Mapping[str, Any]) -> dict[str, int]:
    usage = raw.get("usage")
    if not isinstance(usage, Mapping):
        return {}
    values = {
        "input_tokens": _non_negative_int(usage.get("input_tokens")),
        "output_tokens": _non_negative_int(usage.get("output_tokens")),
        "cache_write_input_tokens": _non_negative_int(
            usage.get("cache_creation_input_tokens")
            or usage.get("cache_write_input_tokens")
        ),
        "cache_read_input_tokens": _non_negative_int(
            usage.get("cache_read_input_tokens")
        ),
    }
    return values if any(values.values()) else {}


def _matching_rates(
    model_id: str,
    model_rates: Mapping[str, Mapping[str, float]],
) -> Mapping[str, float] | None:
    model_id_lower = model_id.lower()
    matches = (
        (pattern, rates)
        for pattern, rates in model_rates.items()
        if pattern.lower() in model_id_lower
    )
    return next(
        (rates for _pattern, rates in sorted(
            matches, key=lambda item: len(item[0]), reverse=True
        )),
        None,
    )


def estimate_usage_usd(
    usage: Mapping[str, Any],
    rates: Mapping[str, float],
) -> float:
    return (
        _non_negative_int(usage.get("input_tokens"))
        * float(rates["input_per_million"])
        + _non_negative_int(usage.get("output_tokens"))
        * float(rates["output_per_million"])
        + _non_negative_int(usage.get("cache_write_input_tokens"))
        * float(rates["cache_write_per_million"])
        + _non_negative_int(usage.get("cache_read_input_tokens"))
        * float(rates["cache_read_per_million"])
    ) / 1_000_000


def _aggregate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, bool, bool], dict[str, Any]] = {}
    for row in rows:
        model_id = str(row.get("model_id") or "").strip()
        operation = str(row.get("operation") or "unknown").strip()
        if not model_id:
            continue
        # Missing usage from one response cannot erase another response's
        # measured tokens, including when saved summaries are merged/repriced.
        long_context = bool(row.get("long_context"))
        key = (model_id, operation, bool(row.get("usage_unavailable")), long_context)
        target = grouped.setdefault(key, {
            "model_id": model_id,
            "operation": operation,
            "calls": 0,
            **{field: 0 for field in TOKEN_FIELDS},
        })
        target["calls"] += _non_negative_int(row.get("calls") or 1)
        if row.get("usage_unavailable"):
            target["usage_unavailable"] = True
        if long_context:
            target["long_context"] = True
        for field in TOKEN_FIELDS:
            target[field] += _non_negative_int(row.get(field))
    return [grouped[key] for key in sorted(grouped)]


def summarise_usage(
    rows: list[dict[str, Any]],
    model_rates: Mapping[str, Mapping[str, float]],
) -> dict[str, Any]:
    usage = _aggregate_rows(rows)
    estimated = 0.0
    unpriced_calls = 0
    for row in usage:
        rates = _matching_rates(row["model_id"], model_rates)
        if rates is None or row.get("usage_unavailable"):
            row["estimated_cost_usd"] = None
            unpriced_calls += row["calls"]
            continue
        row_cost = estimate_usage_usd(row, rates)
        row["estimated_cost_usd"] = round(row_cost, 8)
        estimated += row_cost
    return {
        "version": 1,
        "currency": "USD",
        "estimated_cost_usd": round(estimated, 8),
        "calls": sum(row["calls"] for row in usage),
        "unpriced_calls": unpriced_calls,
        "usage": usage,
    }


def zero_cost_summary() -> dict[str, Any]:
    """Explicitly mark a synthetic audit as tracked with no model calls."""
    return summarise_usage([], default_model_rates())


def merge_summaries(
    existing: Mapping[str, Any] | None,
    added: Mapping[str, Any] | None,
    model_rates: Mapping[str, Mapping[str, float]],
) -> dict[str, Any] | None:
    rows: list[dict[str, Any]] = []
    saw_summary = False
    for summary in (existing, added):
        if not isinstance(summary, Mapping):
            continue
        saw_summary = True
        current = summary.get("usage")
        if isinstance(current, list):
            rows.extend(deepcopy(row) for row in current if isinstance(row, dict))
    return summarise_usage(rows, model_rates) if rows or saw_summary else None


def reprice_summary(
    summary: Mapping[str, Any],
    model_rates: Mapping[str, Mapping[str, float]],
) -> dict[str, Any] | None:
    return merge_summaries(summary, None, model_rates)


class UsageTracker:
    """Thread-safe collector shared by audit and vision worker pools."""

    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def record_response(
        self,
        raw: Mapping[str, Any],
        *,
        model_id: str,
        operation: str,
    ) -> bool:
        """Record usable token counters and return whether usage was recorded."""
        usage = _normalise_usage(raw)
        if not usage:
            return False
        row: dict[str, Any] = {
            "model_id": model_id,
            "operation": operation,
            "calls": 1,
            **usage,
        }
        with self._lock:
            self._rows.append(row)
        return True

    def record_unpriced_call(self, *, model_id: str, operation: str) -> None:
        """Count calls billed outside the token schema without inventing a price."""
        with self._lock:
            self._rows.append({
                "model_id": model_id,
                "operation": operation,
                "calls": 1,
                "usage_unavailable": True,
            })

    def summary(
        self,
        model_rates: Mapping[str, Mapping[str, float]],
    ) -> dict[str, Any] | None:
        with self._lock:
            rows = deepcopy(self._rows)
        return summarise_usage(rows, model_rates) if rows else None
