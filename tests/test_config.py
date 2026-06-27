"""Config loader tests — defaults, file overrides, missing file."""
from pathlib import Path

import config as argus_config


def test_defaults_when_file_missing(tmp_path: Path):
    cfg = argus_config.load(tmp_path / "nope.toml")
    assert cfg.extractor.base_url == "https://tracker.example.com"
    assert cfg.extractor.project_id == "PROJECT"
    assert cfg.auditor.model_provider == "bedrock"
    assert cfg.auditor.model_id == "example-model"
    assert cfg.auditor.region == "example-region"
    assert cfg.auditor.api_base_url is None
    assert cfg.auditor.api_key_env is None
    assert cfg.auditor.env_check_engine == "off"
    assert cfg.auditor.temperature == 0.0
    assert cfg.auditor.max_pages is None
    assert cfg.logging.verbose is False


def test_file_overrides(tmp_path: Path):
    p = tmp_path / "c.toml"
    p.write_text(
        '[extractor]\nbase_url = "https://example.test"\nout_dir = "./stuff"\n'
        '[auditor]\nmodel_provider = "openai"\nmodel_id = "mid"\n'
        'region = "us-east-1"\napi_base_url = "https://api.example.test/v1"\n'
        'api_key_env = "ARGUS_TEST_KEY"\n'
        'temperature = 0.3\nmax_pages = 5\n'
        '[logging]\nverbose = true\n'
    )
    cfg = argus_config.load(p)
    assert cfg.extractor.base_url == "https://example.test"
    assert cfg.extractor.out_dir == Path("./stuff")
    assert cfg.auditor.model_provider == "openai"
    assert cfg.auditor.model_id == "mid"
    assert cfg.auditor.region == "us-east-1"
    assert cfg.auditor.api_base_url == "https://api.example.test/v1"
    assert cfg.auditor.api_key_env == "ARGUS_TEST_KEY"
    assert cfg.auditor.temperature == 0.3
    assert cfg.auditor.max_pages == 5
    assert cfg.logging.verbose is True


def test_partial_file_uses_defaults_for_rest(tmp_path: Path):
    p = tmp_path / "c.toml"
    p.write_text('[auditor]\ntemperature = 0.7\n')
    cfg = argus_config.load(p)
    assert cfg.auditor.temperature == 0.7
    # untouched fields still default
    assert cfg.auditor.model_id == "example-model"
    assert cfg.extractor.base_url == "https://tracker.example.com"
