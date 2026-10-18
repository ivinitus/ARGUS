# Getting started

Install from the repository with `python -m pip install -e ".[video]"` in an
activated virtual environment. Copy `config.example.toml` to the ignored
`config.toml`, then replace the example Jira URL and project ID with your own.
Use a Jira-hosted Zephyr installation exposing `/rest/atm/1.0` endpoints.

Provide `ARGUS_JIRA_PAT` and `ARGUS_MODEL_API_KEY` in the environment. Alternatively,
store the Jira PAT at the configured `extractor.jira_pat_path` with file mode
`0600`. Model credentials stay in environment variables. `.env.example` lists
variable names; exporting them explicitly works for all commands.

```bash
argus config validate
argus extract --key QA-E123
argus audit --key QA-E123
argus audit --folder "Release regression"
argus serve
```

Use `argus --help` for commands and `argus COMMAND --help` for command options.
Model access is required for audit analysis, but existing reports can be read
and regenerated without making model requests.

## Scheduling and notifications

The application includes a local `argus cycle` command and an optional
`cron_wrapper.sh`. Schedule them using your operating system's task scheduler
with the installation's virtual environment on `PATH` and explicit configuration
and credential variables. `argus control` manages local pause/resume state.

Slack notifications and Socket Mode are optional. Configure local token/webhook
files under `[notify]`. The bot's mutating commands require explicitly listed
Slack member IDs in `notify.bot_admin_users`; an empty allowlist permits no
operators. Report links use `notify.report_url` when configured.

## Container

```bash
docker build -t argus .
docker run --rm argus --help
```

The image runs as a non-root user. Mount a writable data directory at `/data`
and provide a configuration file and environment variables when auditing.
The container publishes no network ports by default.
