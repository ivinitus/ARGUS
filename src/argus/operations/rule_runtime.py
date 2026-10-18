"""Pin a published team policy for one worker process/run."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import os
from pathlib import Path
from typing import Any

from argus.config.models import PolicySettings
from argus.domain import team_rules

_pinned: dict[tuple[str, str, str], dict] = {}
_current: dict = {}
_auditor_settings: Any = None


def database_path(settings: Any) -> Path:
    configured = os.environ.get("ARGUS_RULE_DATABASE", "").strip()
    return Path(configured).expanduser() if configured else settings.general.state_dir / "rules.sqlite"


def store_from_environment() -> Any:
    from argus.operations.rule_store import SqliteRuleStore

    return SqliteRuleStore(os.environ["ARGUS_RULE_DATABASE"])


def enabled(team: str) -> bool:
    return bool(os.environ.get("ARGUS_RULE_DATABASE")) and team in {
        value.strip() for value in os.environ.get("ARGUS_RULE_TEAMS", "").split(",")
    }


def configure(settings: Any) -> None:
    global _current, _auditor_settings
    snapshot = settings.auditor.rule_snapshot
    team = settings.general.team_slug
    database = database_path(settings)
    if not snapshot and (enabled(team) or database.exists()):
        from argus.operations.rule_service import active_snapshot

        key = (str(database), team, os.environ.get("ARGUS_RUN_ID", "worker"))
        if key not in _pinned:
            from argus.operations.rule_store import SqliteRuleStore
            database.parent.mkdir(parents=True, exist_ok=True)
            store = SqliteRuleStore(str(database))
            if not store.get(team, "head"):
                document = team_rules.validate_document(team_rules.defaults(team))
                snapshot = {"revision": 0, "document": document, "sha256": team_rules.fingerprint(document)}
            else:
                snapshot = active_snapshot(store, team)
            _pinned[key] = deepcopy(snapshot)
        snapshot = _pinned[key]
    if snapshot:
        document = team_rules.validate_document(snapshot["document"])
        digest = team_rules.fingerprint(document)
        if digest != snapshot.get("sha256"):
            raise ValueError("Published team policy fingerprint does not match its content.")
        settings.policy = PolicySettings(**document["policy"])
        settings.auditor.rule_snapshot = deepcopy(snapshot)
        # These implementation switches must agree with the editable policy.
        if not settings.policy.environment_check_enabled:
            settings.auditor.env_check_engine = "off"
            settings.auditor.env_check_inline = False
        elif settings.auditor.env_check_engine == "off":
            settings.auditor.env_check_engine = "model"
        _current = deepcopy(snapshot)
    else:
        _current = {}
    _auditor_settings = deepcopy(settings.auditor)


def current() -> dict:
    return deepcopy(_current)


def digest() -> str:
    return str(_current.get("sha256", ""))


def annotate(audit: dict, snapshot: dict | None = None) -> dict:
    snapshot = _current if snapshot is None else snapshot
    if snapshot:
        audit.setdefault("provenance", {}).update(
            policy_revision=snapshot["revision"],
            team_rules_sha256=snapshot["sha256"],
            policy_sha256=team_rules.fingerprint(snapshot["document"]["policy"]),
        )
    return audit


def coverage_options() -> dict:
    policy = _current.get("document", {}).get("policy", asdict(PolicySettings()))
    return {key: policy[key] for key in ("completion_threshold", "min_assigned_for_completion")}


def finalize_synthetic(audit: dict, metadata: dict, work_dir: Any) -> dict:
    if not _current:
        return audit
    annotate(audit)
    from argus.pipeline import analysis_status, auditor, custom_rules

    if analysis_status.derive_status(audit) == "incomplete":
        return audit
    cfg = auditor.AuditConfig.from_settings(_auditor_settings, work_dir)
    return custom_rules.apply(cfg, metadata, audit)


def reset() -> None:
    """Clear process state in isolated tests and local preview subprocesses."""
    global _current, _auditor_settings
    _pinned.clear()
    _current = {}
    _auditor_settings = None
