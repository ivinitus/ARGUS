"""Conservative, deterministic identities for stored finding comparisons."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def finding_identity(finding: dict[str, Any]) -> str:
    """Distinguish problems even when they share a step, page, or severity.

    Text whitespace and case are normalized, and valid evidence references are
    treated as a set. Negation, amounts, classification, and evidence locations
    remain significant; paraphrases may need review.
    """
    identity = {
        key: finding.get(key)
        for key in (
            "severity", "step_index", "page", "category", "action",
            "requirement_id", "evidence_refs", "assessment_status",
        )
    }
    for key in ("title", "description"):
        identity[key] = " ".join(str(finding.get(key) or "").casefold().split())
    refs = finding.get("evidence_refs")
    if refs is None:
        identity["evidence_refs"] = []
    elif isinstance(refs, list) and all(isinstance(ref, str) for ref in refs):
        identity["evidence_refs"] = sorted(set(refs))
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
