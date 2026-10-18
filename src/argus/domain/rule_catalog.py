"""Presentation metadata for the deterministic checks; overrides remain policy-owned."""
from __future__ import annotations

FIXED = {
    "R0": "high", "R-RUNTIME": "high", "R1": "medium", "R2": "high", "R2B": "high",
    "R4": "high", "R5": "medium", "R6": "medium", "R7": "medium", "R8": "high",
    "R10": "low", "R11": "medium", "R13": "high",
    "R15": "medium", "R15A": "medium", "R15B": "medium", "R15C": "medium", "R16": "high",
}
CONDITIONAL = {
    "R14": "Medium for a status change; high when execution history warrants escalation.",
    "R14A": "Medium for a status change; high when execution history warrants escalation.",
    "R14B": "Medium for a status change; high when execution history warrants escalation.",
    "R17": "High without an explanation or defect link; medium when documented.",
    "R18": "High without an explanation; medium when documented.",
    "R20": "High for an undocumented or recurring blocker; medium for a documented first occurrence.",
    "R21": "High without a status explanation; medium when documented.",
}
DESCRIPTIONS = {
    "R0": "Reports invalid status vocabulary or checks that could not run.",
    "R1": "Flags unfinished steps without an explanation.",
    "R2": "Checks whether a passing execution contains failed steps.",
    "R2B": "Checks whether a passing execution has any executed steps.",
    "R4": "Checks whether a failing execution includes a failed or blocked step.",
    "R5": "Checks passing steps whose comments describe a problem.",
    "R6": "Checks whether a passed-with-issue result documents its issue.",
    "R7": "Checks step status consistency for passed-with-issue results.",
    "R8": "Checks whether a blocked execution includes a blocked step.",
    "R9": "Checks the observed environment against the configured hosts and exceptions.",
    "R10": "Identifies defect references recorded only in comments.",
    "R11": "Checks defect documentation for failed or blocked steps.",
    "R12": "Checks whether the supplied evidence supports an assessment.",
    "R13": "Checks whether evidence matches the execution's marketplace.",
    "R14": "Compares the execution with recent results for the same test case.",
    "R15": "Checks execution, marketplace, and assignment coverage across the report.",
    "R16": "Checks explanations and defect links for failed or blocked results.",
    "R17": "Checks why a previously executed case is no longer executed.",
    "R18": "Checks the explanation for a not-applicable result.",
    "R19": "Checks whether evidence matches the expected device family.",
    "R20": "Checks blocked-result documentation and recurring blockers.",
    "R21": "Checks the explanation for an unfinished execution.",
    "R22": "Checks whether a test-account identifier was recorded.",
    "R-RUNTIME": "Reports processing failures that prevent a complete audit.",
}


def metadata(rule: str) -> dict:
    from argus.domain.workflow_rules import rule_base

    parent = rule_base(rule)
    default = FIXED.get(rule)
    if rule == "R22":
        levels, explanation = ["low", "medium"], (
            "No findings in disabled mode; low in advisory mode; medium in enforced mode."
        )
    elif rule in CONDITIONAL:
        levels, explanation = ["medium", "high"], CONDITIONAL[rule]
    elif default:
        levels, explanation = [default], f"{default.capitalize()} when this check produces a finding."
    else:
        levels, explanation = ["low", "medium", "high"], "Severity depends on the evidence and finding."
    return {
        "parent": parent if parent != rule else None,
        "description": DESCRIPTIONS.get(rule, DESCRIPTIONS.get(parent, "Checks the supplied execution evidence.")),
        "default_severities": levels, "default_explanation": explanation,
    }
