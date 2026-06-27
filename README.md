# ARGUS

ARGUS is a local audit and reporting tool for manual QA evidence. It reads
test-execution metadata, screenshots, and model-produced `audit.json` files,
then generates a static HTML dashboard for triage, tester rollups, finding
categories, and follow-up actions.

The repository is intentionally local-first: reports are generated from files
on disk, tests run without network access, and real tracker/model credentials
stay outside the repo.

## Screenshots

The screenshots below use sanitized demo data.

![ARGUS dashboard](docs/screenshots/dashboard.png)

![ARGUS mobile layout](docs/screenshots/mobile.png)

## What It Does

* Aggregates `audit.json` files into a static, filterable HTML report.
* Groups findings by severity, tester, source, category, and next action.
* Flags tester-status disagreements, missing evidence, environment issues, and
  deterministic workflow-rule violations.
* Preserves local-first operation: report generation works from existing audit
  files without calling a tracker or model provider.
* Includes unit and integration tests for the report, rules, replay, extractor,
  and audit orchestration paths.

## Quick Start With uv

ARGUS includes `pyproject.toml` for `uv`.

```bash
scripts/bootstrap.sh
scripts/test.sh
```

`bootstrap.sh` creates `.env.argus`, writes `config.toml` from those variables,
and runs `uv sync --dev`.

If you do not have `uv` installed yet, install it from the official uv docs,
then re-run `scripts/bootstrap.sh`.

## pip Fallback

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```

Both paths verify the local code without calling a live tracker or model
provider.

## Shell Configuration

Copy and edit the environment template:

```bash
cp scripts/env.example.sh .env.argus
$EDITOR .env.argus
scripts/bootstrap.sh
```

The important variables are:

* `ARGUS_TRACKER_BASE_URL`
* `ARGUS_PROJECT_ID`
* `ARGUS_OUTPUT_DIR`
* `ARGUS_MODEL_PROVIDER` (`openai`, `anthropic`, `google`, or `bedrock`)
* `ARGUS_MODEL_ID`
* `ARGUS_MODEL_REGION`
* `ARGUS_CLOUD_PROFILE`
* `ARGUS_API_BASE_URL`
* `ARGUS_API_KEY_ENV`
* `ARGUS_ENV_CHECK_ENGINE`
* `ARGUS_BATCH_CONCURRENCY`
* `ARGUS_EXTRACTOR_CONCURRENCY`

`.env.argus` and `config.toml` are ignored by git.

## Generate A Report

ARGUS expects an output tree containing execution folders with `audit.json` and
`metadata.json` files.

```bash
scripts/report.sh
```

This writes:

* `$ARGUS_OUTPUT_DIR/_argus.html`
* a timestamped `_argus_*.html` archive
* a timestamped `_report_*.md` summary, unless disabled with
  `--no-markdown`

Open `_argus.html` in a browser to use the dashboard.

## Run A Full Audit

```bash
scripts/run-audit.sh --key QA-E123456
```

Full extraction and model-based auditing require adapting `config.toml`,
`extractor.py`, and the model client settings to your own issue tracker and
model provider. The checked-in defaults are placeholders and keep
`env_check_engine = "off"` so a fresh public clone does not call a private
vision provider.

## Model Providers

Set `[auditor].model_provider` or `ARGUS_MODEL_PROVIDER` to one of:

* `openai` - calls the OpenAI Chat Completions API. Set
  `api_key_env = "OPENAI_API_KEY"`.
* `anthropic` - calls the Anthropic Messages API. Set
  `api_key_env = "ANTHROPIC_API_KEY"`.
* `google` - calls the Gemini `generateContent` API. Set
  `api_key_env = "GOOGLE_API_KEY"`.
* `bedrock` - keeps the original Bedrock-compatible runtime path, using
  `region` and `cloud_profile`.

`api_base_url` is optional for compatible gateways. Leave it blank to use the
provider default.

## Configuration

Manual setup still works:

```bash
cp config.example.toml config.toml
```

Then set your local values:

* `[extractor].base_url`
* `[extractor].project_id`
* `[extractor].out_dir`
* `[auditor].model_id`
* `[auditor].model_provider`
* `[auditor].api_key_env`
* provider region/profile/base-url settings, if your adapter needs them
* `[batch]` concurrency limits

Keep `config.toml`, token files, generated reports, PDFs, screenshots, and
local output data out of git. The included `.gitignore` covers those defaults.

## Project Layout

* `argus.py` - end-to-end runner for one execution or a folder.
* `report.py` and `report_assets/` - static HTML and Markdown report
  generation.
* `auditor.py`, `auditor_chunking.py`, `auditor_prompts.py` - model audit
  orchestration and prompt/schema handling.
* `workflow_rules.py` and `coverage.py` - deterministic consistency checks.
* `extractor.py` and `batch.py` - tracker ingestion and batch execution
  helpers.
* `tests/` - local test suite.

## Public Repo Notes

This copy uses generic placeholders for tracker URLs, project keys, model
settings, cloud profiles, and product names. Before publishing your own fork,
review any new local files with:

```bash
rg -n "secret|password|token|api[_-]?key|BEGIN .*PRIVATE KEY|AKIA|ASIA" .
```

Do not commit real customer data, private screenshots, tracker exports,
credentials, generated `output/` folders, or personal `config.toml` files.
