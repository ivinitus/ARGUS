# ARGUS

ARGUS audits manual QA evidence and builds a static review dashboard. It works
from files on disk: extracted metadata, screenshots, and `audit.json` results.

## Screenshots

<p align="center">
  <img src="docs/screenshots/dashboard.png" alt="ARGUS dashboard" width="900">
</p>

<details>
<summary>Mobile layout</summary>

<p align="center">
  <img src="docs/screenshots/mobile.png" alt="ARGUS mobile layout" width="320">
</p>

</details>

## Features

* Static HTML and Markdown reports
* Per-tester rollups
* Finding filters by severity, action, category, and source
* Deterministic workflow checks
* Model-provider adapters for OpenAI, Anthropic, Google Gemini, and Bedrock
* Local test suite with mocked external clients

## Install

Using `uv`:

```bash
scripts/bootstrap.sh
scripts/test.sh
```

Without `uv`:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```

## Configure

Copy the environment template and edit it for your tracker/model setup:

```bash
cp scripts/env.example.sh .env.argus
$EDITOR .env.argus
scripts/bootstrap.sh
```

Useful variables:

```bash
ARGUS_TRACKER_BASE_URL=
ARGUS_PROJECT_ID=
ARGUS_OUTPUT_DIR=./output
ARGUS_MODEL_PROVIDER=openai
ARGUS_MODEL_ID=
ARGUS_API_KEY_ENV=OPENAI_API_KEY
ARGUS_ENV_CHECK_ENGINE=off
```

`.env.argus` and `config.toml` are ignored by git.

## Model Providers

| Provider | `ARGUS_MODEL_PROVIDER` | Default key env |
| --- | --- | --- |
| OpenAI | `openai` | `OPENAI_API_KEY` |
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` |
| Google Gemini | `google` | `GOOGLE_API_KEY` |
| Bedrock-compatible runtime | `bedrock` | uses `region` / `cloud_profile` |

Set `ARGUS_API_BASE_URL` only when using a compatible gateway instead of the
provider default.

## Generate A Report

ARGUS scans an output tree for execution folders containing `audit.json` and
`metadata.json`.

```bash
scripts/report.sh
```

Outputs:

```text
$ARGUS_OUTPUT_DIR/_argus.html
$ARGUS_OUTPUT_DIR/_argus_<timestamp>.html
$ARGUS_OUTPUT_DIR/_report_<timestamp>.md
```

Open `_argus.html` in a browser.

## Run An Audit

After configuring your tracker and model provider:

```bash
scripts/run-audit.sh --key QA-E123456
```

Common entry points:

```bash
python argus.py --key QA-E123456
python argus.py --folder "Regression Folder"
python report.py --out-dir ./output/my-run --html
```

## Repository Layout

```text
argus.py                 end-to-end runner
extractor.py             tracker ingestion
auditor.py               model audit orchestration
workflow_rules.py        deterministic checks
report.py                HTML/Markdown report generation
report_assets/           dashboard CSS/JS
tests/                   unit and integration tests
scripts/                 setup and run helpers
```
