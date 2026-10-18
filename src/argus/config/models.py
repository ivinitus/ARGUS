"""Typed ARGUS configuration models."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from argus.core.costs import default_model_rates


POLICY_RULE_IDS = frozenset({
    "R0", "R1", "R2", "R2B", "R4", "R5", "R6", "R7", "R8",
    "R9", "R9-DOC", "R9-AMB", "R9-HOVER", "R9-NAV", "R9-SUSP",
    "R10", "R11", "R12", "R13",
    "R14", "R14A", "R14B",
    "R15", "R15A", "R15B", "R15C",
    "R16", "R17", "R18", "R19", "R19-AMB",
    "R20", "R21", "R22", "R-RUNTIME",
})


def _state_file(name: str) -> Path:
    return Path.home() / ".argus" / name


@dataclass
class GeneralSettings:
    product_name: str = "ARGUS"
    profile: str = "web"
    team_name: str = ""
    # Empty preserves the legacy single-dashboard report paths. Set this when
    # publishing into the namespaced multi-team hub.
    team_slug: str = ""
    docs_url: str = ""
    ticket_template_url: str = ""
    state_dir: Path = field(default_factory=lambda: Path.home() / ".argus")
    # The public distribution runs on the local host.
    runtime: str = "local"


@dataclass
class ExtractorSettings:
    base_url: str = ""
    out_dir: Path = Path("./output")
    project_id: str = ""
    additional_project_ids: list[str] = field(default_factory=list)
    jira_pat_env: str = "ARGUS_JIRA_PAT"
    jira_pat_path: Path = field(
        default_factory=lambda: Path.home() / ".argus" / "jira.pat"
    )
    # Politeness pause during folder enumeration: sleep this many seconds
    # after every 10 test-run item listings. Tuned for a personal PAT;
    # may be lowered (or 0) when running with a service-account PAT.
    enumeration_pause_seconds: float = 1.0
    execution_key_pattern: str = r"^[A-Z][A-Z0-9_]*-E\d+$"
    # Folder audits skip executions whose project status matches one of these
    # names. Matching is case-insensitive and treats spaces, hyphens, and
    # underscores as equivalent.
    ignored_execution_statuses: list[str] = field(default_factory=list)
    # Read test-case Weblinks for failed/PWI executions and steps.
    include_test_case_weblinks: bool = False
    # Teams using Weblinks for evidence also need them on passing executions.
    test_case_weblinks_all_statuses: bool = False

    @property
    def project_ids(self) -> tuple[str, ...]:
        """Projects available to this profile, with the default first."""
        return tuple(dict.fromkeys([self.project_id, *self.additional_project_ids]))


@dataclass
class AuditorSettings:
    model_id: str = "claude-sonnet-5-5"
    model_api_url: str = "https://api.anthropic.com/v1/messages"
    api_key_env: str = "ARGUS_MODEL_API_KEY"
    auxiliary_model_id: str = "claude-haiku-5-5"
    video_model_id: str = "claude-sonnet-5-5"
    video_audio_model_id: str | None = None
    audio_api_url: str = ""
    audio_api_key_env: str = "ARGUS_AUDIO_API_KEY"
    audio_api_format: str = "openai"
    # A packaged prompt asset selects evidence policy without team branching.
    prompt_variant: str = "v5_7"
    temperature: float = 0.0
    max_output_tokens: int = 16000
    thinking_mode: str = "disabled"
    # Extra inference is opt-in until a human-labelled replay gate passes.
    evidence_review_mode: str = "off"
    max_pages: int | None = None
    consensus_enabled: bool = True
    debug_output: bool = False
    chunk_threshold: int = 70
    chunk_size: int = 50
    chunk_overlap: int = 5
    chunk_max_parallel: int = 2
    chunk_page_byte_budget_mb: int = 6
    chunk_attachment_byte_budget_mb: int = 8
    repair_context_max_chars: int = 8000
    env_check_inline: bool = False
    env_check_sample_stride: int = 5
    env_check_region_crop_height: int = 180
    env_check_region_crop_bottom_height: int = 80
    env_check_engine: str = "model"
    history_in_prompt: bool = False
    history_max_entries: int = 5
    # Default OFF: forced tool-use changes model behaviour, not just
    # serialization, so flipping this is held to the same replay A/B
    # gate as a prompt change — the v5_tools gate FAILED (2026-07).
    structured_output: bool = False
    # Process-wide cap on concurrent audit-model (Sonnet) invocations across
    # batch workers, chunk fan-out, and consensus confirmation runs.
    max_inflight_model_calls: int = 12
    # Populated once by the shared policy loader; never sent by an audit request.
    rule_snapshot: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class LoggingSettings:
    verbose: bool = False


@dataclass
class BatchSettings:
    concurrency: int = 16
    extractor_concurrency: int = 4
    # Minimum seconds between shared history-index rebuilds (an rglob over
    # the output root). A batch refreshes at most once per interval.
    history_refresh_seconds: float = 900.0


@dataclass
class NotifySettings:
    mode: str = "verbose"
    webhook_path: Path = field(default_factory=lambda: _state_file("slack_webhook"))
    owner_webhook_path: Path = field(
        default_factory=lambda: _state_file("slack_webhook_owner")
    )
    report_url: str = ""
    log_path: Path = field(default_factory=lambda: _state_file("notify.log"))
    app_token_path: Path = field(
        default_factory=lambda: _state_file("slack_app_token")
    )
    bot_token_path: Path = field(
        default_factory=lambda: _state_file("slack_bot_token")
    )
    bot_admin_users: list[str] = field(default_factory=list)


@dataclass
class FolderSettings:
    # Some teams publish child folders separately and must not duplicate
    # those runs in their parent's report.
    include_descendant_runs: bool = True
    # Master safety switch for `audit --auto-folder` and scheduled folder
    # runs. Manual `--folder` and `--key` execution remains available.
    auto_folder_enabled: bool = True
    auto_name_prefix: str = "Targeted regression"
    auto_max_age_days: int = 7
    dashboard_sections: list[str] = field(
        default_factory=lambda: [
            "Targeted regression",
            "Sanity testing",
            "Additional non-automated",
            "Additional coverage",
        ]
    )


@dataclass
class PolicySettings:
    enabled_rules: list[str] = field(default_factory=list)
    # Exact sub-rules disabled even when their parent family is enabled.
    disabled_rules: list[str] = field(default_factory=list)
    defect_key_prefixes: list[str] = field(default_factory=list)
    # External trackers cannot attach issues to this Jira instance.
    external_defect_key_prefixes: list[str] = field(default_factory=list)
    # Exact hosts whose /browse/KEY-123 URLs count regardless of key prefix.
    defect_url_hosts: list[str] = field(default_factory=list)
    storefront_domain: str = ""
    # Multi-domain form used by teams whose evidence spans storefronts.
    # This takes precedence over storefront_domain for URL extraction.
    storefront_domains: list[str] = field(default_factory=list)
    expected_environment_markers: list[str] = field(
        default_factory=lambda: ["feature-preprod", "preprod", "staging"]
    )
    # Environment markers accepted only when a tester comment explains why
    # the normal pre-production environment could not be used.
    comment_required_environment_markers: list[str] = field(
        default_factory=list
    )
    # Shell-style hostname globs excluded from environment evaluation.
    ignored_environment_host_patterns: list[str] = field(
        default_factory=list
    )
    # Labels for informational pages that may intentionally open on prod.
    production_navigation_notice_labels: list[str] = field(
        default_factory=list
    )
    # Narrow production URL paths approved as test setup/preconditions.
    approved_production_paths: list[str] = field(default_factory=list)
    # Production storefronts allowed for this team; marketplace checks still apply.
    approved_production_host_patterns: list[str] = field(default_factory=list)
    # Attached setup/debug references may establish environment and assigned MP.
    environment_reference_evidence: bool = False
    # When URL bars do not prove preprod, permit a full-frame pass that reads
    # configured environment-marker hostnames from page-body/debug content.
    env_markers_in_page_body: bool = False
    # Exact environment host policy selected from execution metadata.
    environment_expected_hosts_by_run: dict[str, list[str]] = field(
        default_factory=dict
    )
    environment_allowed_host_exceptions: dict[str, list[str]] = field(
        default_factory=dict
    )
    environment_required_host_overrides: dict[str, list[str]] = field(
        default_factory=dict
    )
    marketplace_tlds: dict[str, str] = field(default_factory=dict)
    statuses: dict[str, str] = field(default_factory=dict)
    status_aliases: dict[str, str] = field(default_factory=dict)
    # Per-team overrides applied after deterministic rules emit defaults.
    rule_severities: dict[str, str] = field(default_factory=dict)
    # Skip URL/environment and URL-derived marketplace checks while allowing
    # independent post-checks such as device-family R19 to keep running.
    environment_check_enabled: bool = True
    empty_step_status_optional: bool = False
    # disabled skips R22; advisory reports it without affecting the verdict.
    r22_mode: str = "advisory"
    completion_threshold: float = 1.0
    min_assigned_for_completion: int = 5


@dataclass
class PruneSettings:
    """Retention policy for screenshot evidence under extractor.out_dir.

    Only re-downloadable evidence (screenshots/, session PDFs) is ever
    removed; audit artefacts are always kept. `enabled` gates the cron
    auto-prune only — a manual `argus prune` ignores it, because an
    explicit invocation is the operator's decision.
    """
    enabled: bool = True
    older_than_days: float = 60.0


@dataclass
class ServerSettings:
    host: str = "127.0.0.1"
    port: int = 2405


@dataclass
class HubSettings:
    """Local multi-dashboard configuration and published team vocabulary."""

    port: int = 2400
    # Local ``argus serve --hub`` mounts: PROFILE or PROFILE=/output/path.
    mounts: list[str] = field(default_factory=list)
    # Published hub entries: ``slug=Display Name``.
    teams: list[str] = field(default_factory=list)


@dataclass
class CostSettings:
    refresh_seconds: int = 300
    monthly_budget_usd: float | None = None
    warning_percent: float = 80.0
    critical_percent: float = 100.0
    model_rates: dict[str, dict[str, float]] = field(
        default_factory=default_model_rates
    )


@dataclass
class ArgusConfig:
    """Complete runtime settings."""

    general: GeneralSettings = field(default_factory=GeneralSettings)
    extractor: ExtractorSettings = field(default_factory=ExtractorSettings)
    auditor: AuditorSettings = field(default_factory=AuditorSettings)
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    batch: BatchSettings = field(default_factory=BatchSettings)
    notify: NotifySettings = field(default_factory=NotifySettings)
    folders: FolderSettings = field(default_factory=FolderSettings)
    policy: PolicySettings = field(default_factory=PolicySettings)
    prune: PruneSettings = field(default_factory=PruneSettings)
    server: ServerSettings = field(default_factory=ServerSettings)
    hub: HubSettings = field(default_factory=HubSettings)
    costs: CostSettings = field(default_factory=CostSettings)
