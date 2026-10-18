# Configuration

Settings are typed TOML. Precedence, from lowest to highest, is:

1. Built-in defaults.
2. The profile selected by `--profile` or `ARGUS_PROFILE` (default: `web`).
3. `config.toml` in the project/working directory.
4. `~/.config/argus/config.toml`.
5. `ARGUS_CONFIG`.
6. Explicit `--config PATH`.
7. Supported environment overrides for connection settings.

```bash
argus config profiles
argus config show
argus --config ./config.toml config validate
```

The shipped URL `https://jira.example.com` and numeric project ID are examples.
Set `[extractor] base_url`, `project_id`, `out_dir`, and (if needed)
`additional_project_ids`. `ARGUS_JIRA_PAT` supplies the PAT. The configured
`jira_pat_env` and `jira_pat_path` allow alternate local credential sources.
Bearer credentials are not forwarded to another origin on redirects.

## Model analysis

`[auditor]` contains `model_api_url`, `api_key_env`, `model_id`, and
`auxiliary_model_id`. The screenshot auditor uses an Anthropic Messages-compatible
endpoint. `ARGUS_MODEL_API_KEY` (or `ANTHROPIC_API_KEY`) supplies its key. The
public model identifiers retain the original audit model family.

Video is decoded locally and sampled as timestamped frames; `video_model_id`
selects the visual model. Audio transcription is optional: configure
`audio_api_url`, `video_audio_model_id`, `audio_api_key_env`, and
`audio_api_format` (`openai`) for a compatible audio-capable chat
endpoint. Missing audio configuration yields an explicit unsupported/incomplete
result where voice evidence is required.

Chunk size/overlap, batch concurrency, consensus, prompt selection, and rule
settings remain configurable. The complete accepted field list lives in
`src/argus/config/models.py`; invalid values fail validation.

## Policies and local data

Profiles are editable starting points for web, desktop, tablet, mobile, reader,
TV, and portal evidence. They contain no organization-specific exception lists.
Configure `[policy]` status roles, defect prefixes, storefront domains, expected
environment markers, marketplace mappings, enabled rules, and severities for
your own system. A model response cannot silently change this configuration.

`general.state_dir` defaults to `~/.argus`. Reports and extracted evidence use
`extractor.out_dir`. Local control, locks, review state, and rule state use files
or SQLite. The server defaults to `127.0.0.1:2405`.

`[costs.model_rates]` defines your provider's USD rates per million tokens.
No rates are assumed: unknown models retain measured token/call counts and are
marked unpriced. Cost totals are estimates from saved usage, not billing data.

Never commit local configuration, tokens, or generated evidence. `.gitignore`
and the distribution allowlist exclude these from normal packaging.
