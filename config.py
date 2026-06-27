from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(__file__).parent / "config.toml"


@dataclass
class ExtractorSettings:
    base_url: str = "https://tracker.example.com"
    out_dir: Path = Path("./output")
    project_id: str = "PROJECT"


@dataclass
class AuditorSettings:
    model_provider: str = "bedrock"
    model_id: str = "example-model"
    region: str = "example-region"
    cloud_profile: str | None = None
    api_base_url: str | None = None
    api_key_env: str | None = None
    temperature: float = 0.0
    max_pages: int | None = None
    consensus_enabled: bool = True
    debug_output: bool = False
    chunk_max_parallel: int = 4
    env_check_inline: bool = True
    env_check_sample_stride: int = 1
    env_check_region_crop_height: int = 0
    env_check_region_crop_bottom_height: int = 0
    env_check_engine: str = "off"


@dataclass
class LoggingSettings:
    verbose: bool = False


@dataclass
class BatchSettings:
    concurrency: int = 8
    extractor_concurrency: int = 4


@dataclass
class NotifySettings:
    mode: str = "verbose"
    notification_hook_path: Path = field(
        default_factory=lambda: Path.home() / ".argus" / "chat_notification_hook")
    tunnel_url: str = ""
    log_path: Path = field(
        default_factory=lambda: Path.home() / ".argus" / "notify.log")
    app_token_path: Path = field(
        default_factory=lambda: Path.home() / ".argus" / "chat_app_token")
    bot_token_path: Path = field(
        default_factory=lambda: Path.home() / ".argus" / "chat_bot_token")


@dataclass
class ARGUSConfig:
    extractor: ExtractorSettings = field(default_factory=ExtractorSettings)
    auditor: AuditorSettings = field(default_factory=AuditorSettings)
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    batch: BatchSettings = field(default_factory=BatchSettings)
    notify: NotifySettings = field(default_factory=NotifySettings)


def load(path: Path | None = None) -> ARGUSConfig:
    cfg_path = path or DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        return ARGUSConfig()
    with cfg_path.open("rb") as f:
        raw = tomllib.load(f)

    ex = raw.get("extractor", {})
    au = raw.get("auditor", {})
    lg = raw.get("logging", {})
    bt = raw.get("batch", {})
    nt = raw.get("notify", {})

    return ARGUSConfig(
        extractor=ExtractorSettings(
            base_url=ex.get("base_url", ExtractorSettings.base_url),
            out_dir=Path(ex.get("out_dir", ExtractorSettings.out_dir)),
            project_id=str(ex.get("project_id", ExtractorSettings.project_id)),
        ),
        auditor=AuditorSettings(
            model_provider=str(au.get(
                "model_provider", AuditorSettings.model_provider)),
            model_id=au.get("model_id", AuditorSettings.model_id),
            region=au.get("region", AuditorSettings.region),
            cloud_profile=au.get("cloud_profile", AuditorSettings.cloud_profile),
            api_base_url=au.get("api_base_url", AuditorSettings.api_base_url),
            api_key_env=au.get("api_key_env", AuditorSettings.api_key_env),
            temperature=float(au.get("temperature", AuditorSettings.temperature)),
            max_pages=au.get("max_pages"),
            consensus_enabled=bool(au.get("consensus_enabled", AuditorSettings.consensus_enabled)),
            debug_output=bool(au.get("debug_output", AuditorSettings.debug_output)),
            chunk_max_parallel=int(au.get(
                "chunk_max_parallel", AuditorSettings.chunk_max_parallel)),
            env_check_inline=bool(au.get(
                "env_check_inline", AuditorSettings.env_check_inline)),
            env_check_sample_stride=int(au.get(
                "env_check_sample_stride",
                AuditorSettings.env_check_sample_stride)),
            env_check_region_crop_height=int(au.get(
                "env_check_region_crop_height",
                AuditorSettings.env_check_region_crop_height)),
            env_check_region_crop_bottom_height=int(au.get(
                "env_check_region_crop_bottom_height",
                AuditorSettings.env_check_region_crop_bottom_height)),
            env_check_engine=str(au.get(
                "env_check_engine", AuditorSettings.env_check_engine)),
        ),
        logging=LoggingSettings(
            verbose=bool(lg.get("verbose", LoggingSettings.verbose)),
        ),
        batch=BatchSettings(
            concurrency=int(bt.get("concurrency", BatchSettings.concurrency)),
            extractor_concurrency=int(
                bt.get("extractor_concurrency", BatchSettings.extractor_concurrency)),
        ),
        notify=NotifySettings(
            mode=str(nt.get("mode", NotifySettings.mode)),
            notification_hook_path=Path(nt["notification_hook_path"]).expanduser()
            if "notification_hook_path" in nt
            else NotifySettings.__dataclass_fields__[
                "notification_hook_path"].default_factory(),
            tunnel_url=str(nt.get("tunnel_url", NotifySettings.tunnel_url)),
            log_path=Path(nt["log_path"]).expanduser()
            if "log_path" in nt
            else NotifySettings.__dataclass_fields__[
                "log_path"].default_factory(),
            app_token_path=Path(nt["app_token_path"]).expanduser()
            if "app_token_path" in nt
            else NotifySettings.__dataclass_fields__[
                "app_token_path"].default_factory(),
            bot_token_path=Path(nt["bot_token_path"]).expanduser()
            if "bot_token_path" in nt
            else NotifySettings.__dataclass_fields__[
                "bot_token_path"].default_factory(),
        ),
    )
