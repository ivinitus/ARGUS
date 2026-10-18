"""Config loader tests: profiles, precedence, validation, and overrides."""
import json
from pathlib import Path

import pytest

from argus import config as argus_config


























def test_missing_explicit_file_is_an_error(tmp_path: Path):
    with pytest.raises(argus_config.ConfigError, match="config file not found"):
        argus_config.load(tmp_path / "nope.toml")


def test_file_overrides(tmp_path: Path):
    p = tmp_path / "c.toml"
    p.write_text(
        '[extractor]\nbase_url = "https://example.test"\nout_dir = "./stuff"\n'
        '[auditor]\nmodel_id = "mid"\nmodel_api_url = "https://models.example.com/v1/messages"\n'
        'temperature = 0.3\nmax_pages = 5\n'
        '[logging]\nverbose = true\n'
    )
    cfg = argus_config.load(p)
    assert cfg.extractor.base_url == "https://example.test"
    assert cfg.extractor.out_dir == Path("./stuff")
    assert cfg.auditor.model_id == "mid"
    assert cfg.auditor.model_api_url == "https://models.example.com/v1/messages"
    assert cfg.auditor.temperature == 0.3
    assert cfg.auditor.max_pages == 5
    assert cfg.logging.verbose is True


def test_partial_file_uses_defaults_for_rest(tmp_path: Path):
    p = tmp_path / "c.toml"
    p.write_text('[auditor]\ntemperature = 0.7\n')
    cfg = argus_config.load(p)
    assert cfg.auditor.temperature == 0.7
    assert cfg.auditor.model_id == "claude-sonnet-5-5"
    assert cfg.extractor.base_url == "https://jira.example.com"


@pytest.mark.parametrize("mode", ["disabled", "advisory", "enforced"])
def test_r22_policy_defaults_to_advisory_and_accepts_modes(
        tmp_path: Path, mode: str):
    assert argus_config.load(None).policy.r22_mode == "advisory"
    config_path = tmp_path / "r22.toml"
    config_path.write_text(f'[policy]\nr22_mode = "{mode}"\n')
    assert argus_config.load(config_path).policy.r22_mode == mode


def test_r22_policy_rejects_unknown_mode(tmp_path: Path):
    config_path = tmp_path / "r22.toml"
    config_path.write_text('[policy]\nr22_mode = "unknown"\n')
    with pytest.raises(
        argus_config.ConfigError,
        match="policy.r22_mode must be 'disabled', 'advisory', or 'enforced'",
    ):
        argus_config.load(config_path)


def test_unknown_key_warns_but_loads(tmp_path: Path, capsys):
    p = tmp_path / "c.toml"
    p.write_text('[auditor]\ntemprature = 0.7\n[nonsense]\nx = 1\n')
    cfg = argus_config.load(p)
    err = capsys.readouterr().err
    assert "temprature" in err
    assert "nonsense" in err
    assert cfg.auditor.temperature == 0.0


def test_declared_fields_are_loaded_generically(tmp_path: Path):
    p = tmp_path / "c.toml"
    p.write_text(
        "[auditor]\nhistory_in_prompt = true\nhistory_max_entries = 9\n"
        "chunk_threshold = 60\nchunk_size = 40\nchunk_overlap = 4\n"
        "chunk_page_byte_budget_mb = 5\n"
        "chunk_attachment_byte_budget_mb = 7\n"
        "repair_context_max_chars = 4096\n"
        "[folders]\nauto_name_prefix = \"Release validation\"\n"
        "[notify]\nreport_url = \"https://reports.example.com\"\n"
        "[server]\nport = 4123\n"
    )
    cfg = argus_config.load(p)
    assert cfg.auditor.history_in_prompt is True
    assert cfg.auditor.history_max_entries == 9
    assert cfg.auditor.chunk_threshold == 60
    assert cfg.auditor.chunk_size == 40
    assert cfg.auditor.chunk_overlap == 4
    assert cfg.auditor.chunk_page_byte_budget_mb == 5
    assert cfg.auditor.chunk_attachment_byte_budget_mb == 7
    assert cfg.auditor.repair_context_max_chars == 4096
    assert cfg.folders.auto_name_prefix == "Release validation"
    assert cfg.notify.report_url == "https://reports.example.com"
    assert cfg.server.port == 4123


def test_cost_monitor_settings_and_rates_are_configurable(tmp_path: Path):
    p = tmp_path / "costs.toml"
    p.write_text(
        "[costs]\n"
        "refresh_seconds = 60\n"
        "monthly_budget_usd = 25.0\n"
        "warning_percent = 75.0\n"
        "critical_percent = 95.0\n"
        '[costs.model_rates."custom-model"]\n'
        "input_per_million = 1.0\n"
        "output_per_million = 2.0\n"
        "cache_write_per_million = 1.25\n"
        "cache_read_per_million = 0.1\n"
    )
    cfg = argus_config.load(p)
    assert cfg.costs.refresh_seconds == 60
    assert cfg.costs.monthly_budget_usd == 25.0
    assert cfg.costs.warning_percent == 75.0
    assert cfg.costs.critical_percent == 95.0
    assert cfg.costs.model_rates["custom-model"]["output_per_million"] == 2.0


def test_invalid_types_and_ranges_fail_fast(tmp_path: Path):
    wrong_type = tmp_path / "wrong-type.toml"
    wrong_type.write_text("[batch]\nconcurrency = \"many\"\n")
    with pytest.raises(argus_config.ConfigError, match="must be an integer"):
        argus_config.load(wrong_type)

    wrong_range = tmp_path / "wrong-range.toml"
    wrong_range.write_text("[server]\nport = 70000\n")
    with pytest.raises(argus_config.ConfigError, match="server.port"):
        argus_config.load(wrong_range)

    wrong_thresholds = tmp_path / "wrong-thresholds.toml"
    wrong_thresholds.write_text(
        "[costs]\nwarning_percent = 100\ncritical_percent = 80\n")
    with pytest.raises(argus_config.ConfigError, match="critical_percent"):
        argus_config.load(wrong_thresholds)

    bad_chunking = tmp_path / "bad-chunking.toml"
    bad_chunking.write_text(
        "[auditor]\nchunk_size = 5\nchunk_overlap = 5\n")
    with pytest.raises(argus_config.ConfigError, match="chunk_overlap"):
        argus_config.load(bad_chunking)

    bad_budget = tmp_path / "bad-budget.toml"
    bad_budget.write_text(
        "[auditor]\nchunk_page_byte_budget_mb = 0\n")
    with pytest.raises(
            argus_config.ConfigError, match="chunk_page_byte_budget_mb"):
        argus_config.load(bad_budget)

    empty_ignored_status = tmp_path / "empty-ignored-status.toml"
    empty_ignored_status.write_text(
        '[extractor]\nignored_execution_statuses = ["Automation Pass", " "]\n'
    )
    with pytest.raises(
        argus_config.ConfigError,
        match="ignored_execution_statuses cannot contain empty names",
    ):
        argus_config.load(empty_ignored_status)

    bad_rule_severity = tmp_path / "bad-rule-severity.toml"
    bad_rule_severity.write_text(
        '[policy.rule_severities]\nR1 = "urgent"\n'
    )
    with pytest.raises(
        argus_config.ConfigError,
        match="policy.rule_severities.R1 must be low, medium, or high",
    ):
        argus_config.load(bad_rule_severity)

    insecure_ticket_url = tmp_path / "insecure-ticket-url.toml"
    insecure_ticket_url.write_text(
        '[general]\nticket_template_url = "http://tickets.example.test"\n'
    )
    with pytest.raises(
        argus_config.ConfigError,
        match="general.ticket_template_url must be empty or use https://",
    ):
        argus_config.load(insecure_ticket_url)


def test_default_runtime_is_local():
    cfg = argus_config.load(profile="web")
    assert cfg.general.runtime == "local"


def test_invalid_runtime_rejected(tmp_path: Path):
    bad = tmp_path / "bad-runtime.toml"
    bad.write_text('[general]\nruntime = "prod"\n')
    with pytest.raises(argus_config.ConfigError, match="general.runtime"):
        argus_config.load(bad)


def test_policy_rule_validation_accepts_runtime_rule(tmp_path: Path):
    config_path = tmp_path / "runtime-rule.toml"
    config_path.write_text(
        '[policy]\n'
        'disabled_rules = ["R-runtime"]\n'
        '[policy.rule_severities]\n'
        '"R-runtime" = "high"\n'
    )

    cfg = argus_config.load(config_path)

    assert cfg.policy.disabled_rules == ["R-runtime"]
    assert cfg.policy.rule_severities == {"R-runtime": "high"}


@pytest.mark.parametrize(
    ("config_text", "message"),
    [
        (
            '[policy]\nenabled_rules = ["R9doc"]\n',
            "policy.enabled_rules has invalid rule 'R9doc'",
        ),
        (
            '[policy]\ndisabled_rules = ["R9doc"]\n',
            "policy.disabled_rules has invalid rule 'R9doc'",
        ),
        (
            '[policy.rule_severities]\nR9doc = "medium"\n',
            "policy.rule_severities has invalid rule 'R9doc'",
        ),
    ],
)
def test_policy_rule_validation_rejects_unknown_rules(
        tmp_path: Path, config_text: str, message: str):
    config_path = tmp_path / "unknown-rule.toml"
    config_path.write_text(config_text)

    with pytest.raises(argus_config.ConfigError, match=message):
        argus_config.load(config_path)








def test_empty_base_url_reports_one_error_and_model_id_validated(
        tmp_path: Path):
    """Verify empty base URLs and model IDs produce concise validation errors."""
    bad = tmp_path / "bad.toml"
    bad.write_text('[extractor]\nbase_url = ""\n'
                   '[auditor]\nmodel_id = ""\n')
    with pytest.raises(argus_config.ConfigError) as exc:
        argus_config.load(bad)
    msg = str(exc.value)
    assert msg.count("base_url") == 1
    assert "auditor.model_id is required" in msg


def test_default_model_id_matches_auditor_constant():
    """Verify the config and auditor default model IDs match."""
    from argus.config.models import AuditorSettings
    from argus.pipeline.auditor import DEFAULT_MODEL_ID
    assert AuditorSettings().model_id == DEFAULT_MODEL_ID


def test_environment_config_has_precedence(tmp_path: Path, monkeypatch):
    env_config = tmp_path / "env.toml"
    env_config.write_text("[batch]\nconcurrency = 3\nextractor_concurrency = 2\n")
    explicit = tmp_path / "explicit.toml"
    explicit.write_text("[batch]\nconcurrency = 4\n")
    monkeypatch.setenv("ARGUS_CONFIG", str(env_config))
    cfg = argus_config.load(explicit)
    assert cfg.batch.concurrency == 4
    assert cfg.batch.extractor_concurrency == 2


def test_display_redacts_secret_paths():
    cfg = argus_config.load(profile="web")
    rendered = json.dumps(argus_config.as_dict(cfg))
    assert "slack_webhook" not in rendered
    assert "<redacted>" in rendered


def test_audit_config_from_settings_copies_all_shared_fields(tmp_path: Path):
    from argus.pipeline import auditor
    settings = argus_config.load(None)
    au = settings.auditor
    au.temperature = 0.42
    au.env_check_sample_stride = 7
    au.chunk_max_parallel = 3
    cfg = auditor.AuditConfig.from_settings(au, execution_dir=tmp_path)
    for name in vars(au):
        if name in auditor.AuditConfig.__dataclass_fields__:
            assert getattr(cfg, name) == getattr(au, name), name
    assert cfg.variant_name == "v5_7"
    assert cfg.system_prompt == auditor.SYSTEM_PROMPT_V5_7
    cfg2 = auditor.AuditConfig.from_settings(
        au, execution_dir=tmp_path, temperature=0.0)
    assert cfg2.temperature == 0.0


def test_parallelism_settings_are_loaded_and_validated(tmp_path: Path):
    defaults = argus_config.ArgusConfig()
    assert defaults.auditor.max_inflight_model_calls == 12
    assert defaults.batch.history_refresh_seconds == 900.0

    ok = tmp_path / "ok.toml"
    ok.write_text(
        "[auditor]\nmax_inflight_model_calls = 20\n"
        "[batch]\nhistory_refresh_seconds = 120\n"
    )
    cfg = argus_config.load(ok)
    assert cfg.auditor.max_inflight_model_calls == 20
    assert cfg.batch.history_refresh_seconds == 120.0

    bad_inflight = tmp_path / "bad-inflight.toml"
    bad_inflight.write_text("[auditor]\nmax_inflight_model_calls = 0\n")
    with pytest.raises(
        argus_config.ConfigError,
        match="auditor.max_inflight_model_calls must be positive",
    ):
        argus_config.load(bad_inflight)

    bad_refresh = tmp_path / "bad-refresh.toml"
    bad_refresh.write_text("[batch]\nhistory_refresh_seconds = -1\n")
    with pytest.raises(
        argus_config.ConfigError,
        match="batch.history_refresh_seconds must be >= 0",
    ):
        argus_config.load(bad_refresh)
def test_reader_folders_exclude_descendant_runs():
    from argus import config

    assert config.load(profile="reader").folders.include_descendant_runs is False
    assert config.ArgusConfig().folders.include_descendant_runs is True


@pytest.mark.parametrize("profile", argus_config.available_profiles())
def test_public_profiles_are_portable_and_configurable(profile, tmp_path):
    from argus.pipeline.auditor import AuditConfig

    settings = argus_config.load(profile=profile)
    assert settings.extractor.base_url == "https://jira.example.com"
    assert settings.extractor.project_id == "10000"
    assert settings.notify.bot_admin_users == []
    assert settings.general.docs_url == ""
    assert settings.general.ticket_template_url == ""
    assert settings.auditor.video_audio_model_id is None
    assert settings.policy.environment_expected_hosts_by_run == {}
    assert settings.policy.environment_allowed_host_exceptions == {}
    assert settings.policy.environment_required_host_overrides == {}
    audit = AuditConfig.from_settings(settings.auditor, execution_dir=tmp_path)
    assert audit.model_api_url == settings.auditor.model_api_url
    assert audit.api_key_env == "ARGUS_MODEL_API_KEY"
    assert audit.system_prompt


def test_standard_environment_settings_override_profile(monkeypatch):
    monkeypatch.setenv("ARGUS_JIRA_URL", "https://issues.example.org")
    monkeypatch.setenv("ARGUS_JIRA_PROJECT_ID", "23456")
    monkeypatch.setenv("ARGUS_MODEL_ID", "custom-model")
    monkeypatch.setenv("ARGUS_MODEL_API_URL", "https://models.example.org/v1/messages")
    monkeypatch.setenv("ARGUS_REPORT_URL", "http://127.0.0.1:2405")
    settings = argus_config.load(profile="web")
    assert settings.extractor.base_url == "https://issues.example.org"
    assert settings.extractor.project_id == "23456"
    assert settings.auditor.model_id == "custom-model"
    assert settings.auditor.model_api_url == "https://models.example.org/v1/messages"
    assert settings.notify.report_url == "http://127.0.0.1:2405"


@pytest.mark.parametrize("field,value", [("model_api_url", "http://models.example.com"), ("api_key_env", ""), ("video_model_id", ""), ("audio_api_format", "unknown")])
def test_invalid_model_configuration_fails_early(tmp_path, field, value):
    path = tmp_path / "model.toml"
    path.write_text(f"[auditor]\n{field} = {json.dumps(value)}\n")
    with pytest.raises(argus_config.ConfigError, match=field):
        argus_config.load(path)


def test_custom_video_model_is_forwarded(tmp_path):
    from argus.pipeline.auditor import AuditConfig

    path = tmp_path / "video.toml"
    path.write_text('[auditor]\nvideo_model_id = "custom-video-model"\n')
    settings = argus_config.load(path, profile="mobile")
    audit = AuditConfig.from_settings(settings.auditor, execution_dir=tmp_path)
    assert audit.video_model_id == "custom-video-model"
    assert audit.variant_name == "mobile_v2"
