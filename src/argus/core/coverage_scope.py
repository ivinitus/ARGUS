"""Keep saved coverage aligned with a restored report's execution scope."""
from __future__ import annotations

from typing import Any


def covers_restored_scope(coverage: dict[str, Any], manifest: Any) -> bool:
    """Require execution identities when a report expands to cached history."""
    if not isinstance(manifest, dict) or not manifest.get("scope_restored_at"):
        return True
    if coverage.get("schema") != 1:
        return False
    scope = manifest.get("scope_keys")
    executions = coverage.get("executions")
    if not isinstance(scope, list) or not isinstance(executions, list):
        return False
    expected = {key for key in scope if isinstance(key, str) and key}
    covered = {
        row["e_key"]
        for row in executions
        if isinstance(row, dict)
        and isinstance(row.get("e_key"), str)
        and row["e_key"]
    }
    return bool(expected) and expected <= covered
