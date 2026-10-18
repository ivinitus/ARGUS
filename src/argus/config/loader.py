"""Named-profile configuration loading and validation."""
from __future__ import annotations

import dataclasses
import os
import re
import sys
import tomllib
import types
from pathlib import Path
from typing import Any, Union, get_args, get_origin, get_type_hints

from argus.core import paths

from .models import POLICY_RULE_IDS, ArgusConfig

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = paths.project_root()
PROFILE_DIR = PACKAGE_ROOT / "profiles"
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"
USER_CONFIG_PATH = Path.home() / ".config" / "argus" / "config.toml"


class ConfigError(ValueError):
    """Raised when configuration exists but is invalid."""


def _read_toml(path: Path, *, required: bool) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise ConfigError(f"config file not found: {path}")
        return {}
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config root must be a table: {path}")
    return data


def _coerce(value: Any, annotation: Any, key: str) -> Any:
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (Union, types.UnionType):
        if value is None and type(None) in args:
            return None
        failures: list[str] = []
        for option in args:
            if option is type(None):
                continue
            try:
                return _coerce(value, option, key)
            except ConfigError as exc:
                failures.append(str(exc))
        raise ConfigError(f"{key} has invalid value {value!r}: {'; '.join(failures)}")
    if annotation is Any:
        return value
    if annotation is Path:
        if not isinstance(value, str):
            raise ConfigError(f"{key} must be a path string")
        return Path(value).expanduser()
    if annotation is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{key} must be true or false")
        return value
    if annotation is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{key} must be an integer")
        return value
    if annotation is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{key} must be a number")
        return float(value)
    if annotation is str:
        if not isinstance(value, str):
            raise ConfigError(f"{key} must be a string")
        return value
    if origin is list:
        if not isinstance(value, list):
            raise ConfigError(f"{key} must be an array")
        item_type = args[0] if args else Any
        return [_coerce(item, item_type, f"{key}[]") for item in value]
    if origin is dict:
        if not isinstance(value, dict):
            raise ConfigError(f"{key} must be a table")
        key_type, value_type = args or (Any, Any)
        return {
            _coerce(k, key_type, f"{key}.<key>"):
            _coerce(v, value_type, f"{key}.{k}")
            for k, v in value.items()
        }
    return value


def _overlay(instance: Any, raw: dict[str, Any], source: Path) -> None:
    known = {field.name: field for field in dataclasses.fields(instance)}
    hints = get_type_hints(type(instance))
    for key, value in raw.items():
        field = known.get(key)
        dotted = f"{type(instance).__name__}.{key}"
        if field is None:
            print(f"config: unknown key {dotted!r} in {source} - ignored",
                  file=sys.stderr)
            continue
        current = getattr(instance, key)
        if dataclasses.is_dataclass(current):
            if not isinstance(value, dict):
                raise ConfigError(f"{dotted} must be a table")
            _overlay(current, value, source)
            continue
        setattr(instance, key, _coerce(value, hints.get(key, Any), dotted))


def _profile_path(name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ConfigError(f"invalid profile name: {name!r}")
    return PROFILE_DIR / f"{name}.toml"


_RUNTIME_ENVIRONMENT_FIELDS: dict[str, tuple[str, str]] = {
    "ARGUS_JIRA_URL": ("extractor", "base_url"),
    "ARGUS_JIRA_PROJECT_ID": ("extractor", "project_id"),
    "ARGUS_MODEL_API_URL": ("auditor", "model_api_url"),
    "ARGUS_MODEL_ID": ("auditor", "model_id"),
    "ARGUS_AUXILIARY_MODEL_ID": ("auditor", "auxiliary_model_id"),
    "ARGUS_VIDEO_MODEL_ID": ("auditor", "video_model_id"),
    "ARGUS_AUDIO_API_URL": ("auditor", "audio_api_url"),
    "ARGUS_VIDEO_AUDIO_MODEL_ID": ("auditor", "video_audio_model_id"),
    "ARGUS_REPORT_URL": ("notify", "report_url"),
    "ARGUS_TEAM_NAME": ("general", "team_name"),
    "ARGUS_TEAM_SLUG": ("general", "team_slug"),
    "ARGUS_DOCS_URL": ("general", "docs_url"),
    "ARGUS_TICKET_TEMPLATE_URL": ("general", "ticket_template_url"),
}

_RUNTIME_ENVIRONMENT_LIST_FIELDS: dict[str, tuple[str, str]] = {
    "ARGUS_HUB_TEAMS": ("hub", "teams"),
}

_RUNTIME_ENVIRONMENT_FLOAT_FIELDS: dict[str, tuple[str, str]] = {
    "ARGUS_MONTHLY_BUDGET_USD": ("costs", "monthly_budget_usd"),
}

_RUNTIME_ENVIRONMENT_PATH_FIELDS: dict[str, tuple[str, str]] = {
    "ARGUS_STATE_DIR": ("general", "state_dir"),
    "ARGUS_OUTPUT_DIR": ("extractor", "out_dir"),
}


def _overlay_runtime_environment(config: ArgusConfig) -> None:
    """Apply non-secret environment settings after TOML overlays."""
    runtime = os.environ.get("ARGUS_RUNTIME")
    if runtime:
        config.general.runtime = runtime
    for env_name, (section_name, field_name) in _RUNTIME_ENVIRONMENT_FIELDS.items():
        value = os.environ.get(env_name)
        if value is not None:
            setattr(getattr(config, section_name), field_name, value)
    for env_name, (section_name, field_name) in (
            _RUNTIME_ENVIRONMENT_LIST_FIELDS.items()):
        value = os.environ.get(env_name)
        if value is not None:
            setattr(
                getattr(config, section_name),
                field_name,
                [item.strip() for item in value.split(",") if item.strip()],
            )
    for env_name, (section_name, field_name) in (
            _RUNTIME_ENVIRONMENT_FLOAT_FIELDS.items()):
        value = os.environ.get(env_name)
        if value is not None:
            try:
                parsed = float(value)
            except ValueError as exc:
                raise ConfigError(f"{env_name} must be a number") from exc
            setattr(getattr(config, section_name), field_name, parsed)
    for env_name, (section_name, field_name) in (
            _RUNTIME_ENVIRONMENT_PATH_FIELDS.items()):
        value = os.environ.get(env_name)
        if value is not None:
            setattr(getattr(config, section_name), field_name, Path(value))


def validate(config: ArgusConfig) -> None:
    errors: list[str] = []
    if config.general.runtime != "local":
        errors.append("general.runtime must be 'local'")
    if (
        config.general.team_slug
        and not re.fullmatch(r"[A-Za-z0-9_.-]+", config.general.team_slug)
    ):
        errors.append(
            "general.team_slug must contain only letters, numbers, '.', "
            "'_' or '-'"
        )
    if (
        config.general.docs_url
        and not config.general.docs_url.startswith("https://")
    ):
        errors.append("general.docs_url must be empty or use https://")
    if (
        config.general.ticket_template_url
        and not config.general.ticket_template_url.startswith("https://")
    ):
        errors.append(
            "general.ticket_template_url must be empty or use https://"
        )
    if not config.extractor.base_url:
        errors.append("extractor.base_url is required")
    elif not re.match(r"^https?://", config.extractor.base_url):
        errors.append("extractor.base_url must be an http(s) URL")
    if not config.extractor.project_id:
        errors.append("extractor.project_id is required")
    if any(not re.fullmatch(r"[1-9][0-9]*", value)
           for value in config.extractor.additional_project_ids):
        errors.append("extractor.additional_project_ids must contain positive numeric project IDs")
    if config.extractor.enumeration_pause_seconds < 0:
        errors.append("extractor.enumeration_pause_seconds must be >= 0")
    if not config.auditor.model_id:
        errors.append("auditor.model_id is required")
    if not config.auditor.model_api_url.startswith("https://"):
        errors.append("auditor.model_api_url must use https://")
    if not config.auditor.api_key_env:
        errors.append("auditor.api_key_env must not be empty")
    if not config.auditor.auxiliary_model_id:
        errors.append("auditor.auxiliary_model_id must not be empty")
    if not config.auditor.video_model_id:
        errors.append("auditor.video_model_id must not be empty")
    if config.auditor.audio_api_url and not config.auditor.audio_api_url.startswith("https://"):
        errors.append("auditor.audio_api_url must be empty or use https://")
    if config.auditor.audio_api_format != "openai":
        errors.append("auditor.audio_api_format must be 'openai'")
    try:
        from argus.pipeline.auditor_prompts import resolve_prompt_variant
        resolve_prompt_variant(config.auditor.prompt_variant)
    except ValueError as exc:
        errors.append(f"auditor.prompt_variant: {exc}")
    if config.auditor.temperature < 0 or config.auditor.temperature > 1:
        errors.append("auditor.temperature must be between 0 and 1")
    if config.auditor.max_output_tokens < 1:
        errors.append("auditor.max_output_tokens must be positive")
    if config.auditor.thinking_mode not in {"disabled", "provider_default"}:
        errors.append("auditor.thinking_mode must be disabled or provider_default")
    if config.auditor.evidence_review_mode not in {"off", "shadow", "enforce"}:
        errors.append("auditor.evidence_review_mode must be off, shadow, or enforce")
    if config.auditor.max_pages is not None and config.auditor.max_pages < 1:
        errors.append("auditor.max_pages must be positive")
    if config.auditor.chunk_threshold < 1:
        errors.append("auditor.chunk_threshold must be positive")
    if config.auditor.chunk_size < 1:
        errors.append("auditor.chunk_size must be positive")
    if (config.auditor.chunk_overlap < 0
            or config.auditor.chunk_overlap >= config.auditor.chunk_size):
        errors.append(
            "auditor.chunk_overlap must be between 0 and chunk_size - 1")
    if config.auditor.chunk_max_parallel < 1:
        errors.append("auditor.chunk_max_parallel must be positive")
    if config.auditor.max_inflight_model_calls < 1:
        errors.append("auditor.max_inflight_model_calls must be positive")
    if config.auditor.chunk_page_byte_budget_mb < 1:
        errors.append("auditor.chunk_page_byte_budget_mb must be positive")
    if config.auditor.chunk_attachment_byte_budget_mb < 1:
        errors.append(
            "auditor.chunk_attachment_byte_budget_mb must be positive")
    if config.auditor.repair_context_max_chars < 1:
        errors.append("auditor.repair_context_max_chars must be positive")
    if config.auditor.env_check_sample_stride < 1:
        errors.append("auditor.env_check_sample_stride must be positive")
    if config.auditor.env_check_engine not in {"model", "tesseract", "off"}:
        errors.append(
            "auditor.env_check_engine must be 'model', 'tesseract', or 'off'"
        )
    if (
        not config.policy.environment_check_enabled
        and config.auditor.env_check_inline
    ):
        errors.append(
            "auditor.env_check_inline must be false when "
            "policy.environment_check_enabled is false"
        )
    if config.policy.r22_mode not in {"disabled", "advisory", "enforced"}:
        errors.append("policy.r22_mode must be 'disabled', 'advisory', or 'enforced'")
    for domain in config.policy.storefront_domains:
        if not re.fullmatch(r"[A-Za-z0-9-]+", domain or ""):
            errors.append(
                f"policy.storefront_domains contains invalid domain {domain!r}"
            )
    contextual_host_maps = (
        (
            "environment_expected_hosts_by_run",
            config.policy.environment_expected_hosts_by_run,
            False,
        ),
        (
            "environment_allowed_host_exceptions",
            config.policy.environment_allowed_host_exceptions,
            True,
        ),
        (
            "environment_required_host_overrides",
            config.policy.environment_required_host_overrides,
            True,
        ),
    )
    hostname_rx = re.compile(
        r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
        r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
        flags=re.I,
    )
    for field_name, mapping, test_case_keys in contextual_host_maps:
        for selector, hosts in mapping.items():
            if not selector.strip():
                errors.append(f"policy.{field_name} cannot contain a blank key")
            if test_case_keys and not re.fullmatch(
                    r"[A-Z][A-Z0-9_]*-T[0-9]+", selector.strip(), flags=re.I):
                errors.append(
                    f"policy.{field_name} has invalid test-case key "
                    f"{selector!r}"
                )
            if not hosts:
                errors.append(
                    f"policy.{field_name}.{selector} must list at least one host"
                )
            for host in hosts:
                if not hostname_rx.fullmatch(host.strip()):
                    errors.append(
                        f"policy.{field_name}.{selector} contains invalid "
                        f"hostname {host!r}"
                    )
    expected_markers = {
        marker.lower()
        for marker in config.policy.expected_environment_markers
    }
    for marker in config.policy.comment_required_environment_markers:
        if marker.lower() not in expected_markers:
            errors.append(
                "policy.comment_required_environment_markers entry "
                f"{marker!r} is not in expected_environment_markers"
            )
    for pattern in config.policy.ignored_environment_host_patterns:
        if not re.fullmatch(r"[A-Za-z0-9*?.-]+", pattern or ""):
            errors.append(
                "policy.ignored_environment_host_patterns contains unsafe "
                f"pattern {pattern!r}"
            )
    for pattern in config.policy.approved_production_host_patterns:
        if (
            not re.fullmatch(r"[A-Za-z0-9*?.-]+", pattern or "")
            or "." not in pattern
            or not re.search(r"[A-Za-z0-9]", pattern)
        ):
            errors.append(
                "policy.approved_production_host_patterns requires a hostname "
                f"pattern with a domain, got {pattern!r}"
            )
    if any(
        not label.strip()
        for label in config.policy.production_navigation_notice_labels
    ):
        errors.append(
            "policy.production_navigation_notice_labels cannot contain "
            "blank labels"
        )
    for path in config.policy.approved_production_paths:
        prefix = (path or "").strip()
        if not prefix.startswith("/"):
            errors.append(
                "policy.approved_production_paths entry "
                f"{path!r} must start with '/'"
            )
        elif prefix == "/":
            errors.append(
                "policy.approved_production_paths cannot contain '/' because "
                "it would approve every production URL"
            )
    status_roles = {
        "pass",
        "fail",
        "blocked",
        "in_progress",
        "passed_with_issue",
        "not_applicable",
        "not_executed",
        "retest",
    }
    for alias, role in config.policy.status_aliases.items():
        if not alias.strip():
            errors.append("policy.status_aliases cannot contain a blank alias")
        if role not in status_roles:
            errors.append(
                f"policy.status_aliases.{alias} must map to one of "
                + ", ".join(sorted(status_roles))
            )
    for rule in config.policy.enabled_rules:
        if rule.upper() not in POLICY_RULE_IDS:
            errors.append(f"policy.enabled_rules has invalid rule {rule!r}")
    for rule in config.policy.disabled_rules:
        normalized_rule = rule.upper()
        if normalized_rule not in POLICY_RULE_IDS:
            errors.append(f"policy.disabled_rules has invalid rule {rule!r}")
        elif normalized_rule == "R0":
            errors.append(
                "policy.disabled_rules cannot disable R0 "
                "(status vocabulary guard)"
            )
    allowed_severities = {"low", "medium", "high"}
    for rule, severity in config.policy.rule_severities.items():
        if rule.upper() not in POLICY_RULE_IDS:
            errors.append(f"policy.rule_severities has invalid rule {rule!r}")
        if severity.lower() not in allowed_severities:
            errors.append(
                f"policy.rule_severities.{rule} must be low, medium, or high"
            )
    if config.batch.concurrency < 1:
        errors.append("batch.concurrency must be positive")
    if not 1 <= config.batch.extractor_concurrency <= config.batch.concurrency:
        errors.append(
            "batch.extractor_concurrency must be between 1 and batch.concurrency"
        )
    if config.batch.history_refresh_seconds < 0:
        errors.append("batch.history_refresh_seconds must be >= 0")
    if config.notify.mode not in {"verbose", "failures", "silent"}:
        errors.append("notify.mode must be verbose, failures, or silent")
    if config.prune.older_than_days < 1:
        errors.append("prune.older_than_days must be at least 1")
    if not 1 <= config.server.port <= 65535:
        errors.append("server.port must be between 1 and 65535")
    if not 1 <= config.hub.port <= 65535:
        errors.append("hub.port must be between 1 and 65535")
    seen_team_slugs: set[str] = set()
    for spec in config.hub.teams:
        slug, separator, name = spec.partition("=")
        slug = slug.strip()
        if (
            not slug
            or not re.fullmatch(r"[A-Za-z0-9_.-]+", slug)
            or (separator and not name.strip())
        ):
            errors.append(
                "hub.teams entries must be 'slug' or 'slug=Display Name'"
            )
            continue
        if slug in seen_team_slugs:
            errors.append(f"hub.teams contains duplicate slug {slug!r}")
        seen_team_slugs.add(slug)
    if config.costs.refresh_seconds < 30:
        errors.append("costs.refresh_seconds must be at least 30")
    if (config.costs.monthly_budget_usd is not None
            and config.costs.monthly_budget_usd <= 0):
        errors.append("costs.monthly_budget_usd must be positive")
    if config.costs.warning_percent <= 0:
        errors.append("costs.warning_percent must be positive")
    if config.costs.critical_percent <= config.costs.warning_percent:
        errors.append(
            "costs.critical_percent must be greater than warning_percent")
    for model, rates in config.costs.model_rates.items():
        missing = [key for key in (
            "input_per_million",
            "output_per_million",
            "cache_write_per_million",
            "cache_read_per_million",
        ) if key not in rates]
        if missing:
            errors.append(
                f"costs.model_rates.{model} is missing {', '.join(missing)}")
            continue
        if any(not isinstance(value, (int, float)) or value < 0
               for value in rates.values()):
            errors.append(
                f"costs.model_rates.{model} values must be non-negative numbers")
    try:
        re.compile(config.extractor.execution_key_pattern)
    except re.error as exc:
        errors.append(f"extractor.execution_key_pattern is invalid: {exc}")
    if any(
        not status.strip()
        for status in config.extractor.ignored_execution_statuses
    ):
        errors.append(
            "extractor.ignored_execution_statuses cannot contain empty names"
        )
    if errors:
        raise ConfigError("invalid ARGUS configuration:\n- " + "\n- ".join(errors))


def load(
    path: Path | None = None,
    *,
    profile: str | None = None,
    validate_config: bool = True,
) -> ArgusConfig:
    """Load defaults, a named profile, and local overrides."""
    explicit_path = Path(path).expanduser() if path is not None else None
    env_path_raw = os.environ.get("ARGUS_CONFIG")
    env_path = Path(env_path_raw).expanduser() if env_path_raw else None

    selected_profile = profile or os.environ.get("ARGUS_PROFILE")
    if selected_profile is None:
        selected_profile = "web"

    config = ArgusConfig()
    config.general.profile = selected_profile
    sources: list[tuple[Path, bool]] = [
        (_profile_path(selected_profile), True),
        (DEFAULT_CONFIG_PATH, False),
        (USER_CONFIG_PATH, False),
    ]
    if env_path is not None:
        sources.append((env_path, True))
    if explicit_path is not None:
        sources.append((explicit_path, True))

    for source, required in sources:
        raw = _read_toml(source, required=required)
        if raw:
            _overlay(config, raw, source)

    _overlay_runtime_environment(config)
    if validate_config:
        validate(config)
    return config


def available_profiles() -> list[str]:
    return sorted(path.stem for path in PROFILE_DIR.glob("*.toml"))


def as_dict(config: ArgusConfig, *, redact: bool = True) -> dict[str, Any]:
    data = dataclasses.asdict(config)

    def normalise(value: Any, key: str = "") -> Any:
        if redact and any(
            token in key.lower()
            for token in ("token", "secret", "webhook", "pat_path")
        ):
            return "<redacted>" if value else value
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, dict):
            return {k: normalise(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [normalise(item, key) for item in value]
        return value

    return normalise(data)
