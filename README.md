<div align="center">

# ARGUS

**Audit manual QA evidence and turn it into a static, shareable review dashboard.**

ARGUS ingests QA execution evidence — extracted metadata, screenshots, and
model audit results — and produces deterministic HTML and Markdown reports.
No server, no database: everything works from files on disk.

[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-pytest-orange.svg)](tests/)

</div>

---

## Overview

ARGUS scans an output tree for execution folders containing `audit.json` and
`metadata.json`, then renders a review dashboard plus a Markdown summary. It
combines **model-based auditing** (OpenAI, Anthropic, Google Gemini, or
Bedrock) with **deterministic workflow checks**, so findings are reproducible
and reviewable without re-running the model.

<p align="center">
  <img src="docs/screenshots/dashboard.png" alt="ARGUS dashboard" width="900">
</p>

<details>
<summary>Mobile layout</summary>

<p align="center">
  <img src="docs/screenshots/mobile.png" alt="ARGUS mobile layout" width="320">
</p>

</details>

## Contents

- [Features](#features)
- [Quickstart](#quickstart)
- [Installation](#installation)
- [Configuration](#configuration)
- [Model Providers](#model-providers)
- [Usage](#usage)
- [How It Works](#how-it-works)
- [Repository Layout](#repository-layout)
- [Development](#development)
- [License](#license)

## Features

- **Static reports** — self-contained HTML dashboard and Markdown summary; no runtime services required.
- **Per-tester rollups** — aggregate findings and coverage by tester.
- **Rich filtering** — slice findings by severity, action, category, and source.
- **Deterministic checks** — rule-based workflow validation that runs independently of the model.
- **Pluggable providers** — adapters for OpenAI, Anthropic, Google Gemini, and Bedrock-compatible runtimes.
- **Tested** — a local `pytest` suite with mocked external clients.

## Quickstart

```bash
scripts/bootstrap.sh          # create venv, install deps, scaffold config
$EDITOR .env.argus            # set tracker + model provider
scripts/bootstrap.sh          # regenerate config.toml from .env.argus
scripts/report.sh             # render a report from existing audit files
```

Open `$ARGUS_OUTPUT_DIR/_argus.html` in a browser.

## Installation

> **Requirements:** Python 3.11+.

**Using [`uv`](https://github.com/astral-sh/uv) (recommended):**

```bash
scripts/bootstrap.sh
scripts/test.sh
```

**Using `pip`:**

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```

## Configuration

Copy the environment template and edit it for your tracker and model setup:

```bash
cp scripts/env.example.sh .env.argus
$EDITOR .env.argus
scripts/bootstrap.sh          # writes config.toml from .env.argus
```

Commonly used variables:

| Variable | Purpose |
| --- | --- |
| `ARGUS_TRACKER_BASE_URL` | Base URL of your issue/test tracker |
| `ARGUS_PROJECT_ID` | Tracker project identifier |
| `ARGUS_OUTPUT_DIR` | Where reports and audit evidence live (default `./output`) |
| `ARGUS_MODEL_PROVIDER` | `openai`, `anthropic`, `google`, or `bedrock` |
| `ARGUS_MODEL_ID` | Provider-specific model identifier |
| `ARGUS_API_KEY_ENV` | Name of the env var holding the API key |
| `ARGUS_ENV_CHECK_ENGINE` | Environment-validation engine (`off` by default) |

Set `ARGUS_API_BASE_URL` only when routing through a compatible gateway
instead of the provider default.

> [!NOTE]
> `.env.argus` and `config.toml` are git-ignored. Keep real credentials in
> environment variables or ignored files — never commit them.

## Model Providers

| Provider | `ARGUS_MODEL_PROVIDER` | Default key env |
| --- | --- | --- |
| OpenAI | `openai` | `OPENAI_API_KEY` |
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` |
| Google Gemini | `google` | `GOOGLE_API_KEY` |
| Bedrock-compatible runtime | `bedrock` | uses `region` / `cloud_profile` |

For Bedrock, set `ARGUS_MODEL_REGION` and `ARGUS_CLOUD_PROFILE` instead of an
API key.

## Usage

### Generate a report

ARGUS scans the output tree for execution folders with `audit.json` and
`metadata.json`:

```bash
scripts/report.sh
```

This produces:

```text
$ARGUS_OUTPUT_DIR/_argus.html              # latest dashboard
$ARGUS_OUTPUT_DIR/_argus_<timestamp>.html  # timestamped snapshot
$ARGUS_OUTPUT_DIR/_report_<timestamp>.md   # Markdown summary
```

### Run an audit

After configuring your tracker and model provider:

```bash
scripts/run-audit.sh --key QA-E123456
```

### Direct entry points

```bash
python argus.py --key QA-E123456            # extract + audit a single execution
python argus.py --folder "Sprint 47 Regression"   # audit a whole tracker folder
python report.py --out-dir ./output/my-run --html # render a report only
```

Folder mode enumerates executed, not-yet-audited keys and runs the
extract + audit pipeline on each in parallel; already-audited keys (those with
an existing `audit.json`) are skipped. Concurrency is controlled by the
`[batch]` section of `config.toml`.

## How It Works

```text
  tracker / evidence            ARGUS pipeline                  outputs
  ──────────────────            ──────────────                  ───────
  test executions   ──▶  extractor   ─┐
  screenshots                          ├─▶  auditor (model)  ─┐
  metadata                             │                      ├─▶  report  ──▶  HTML + Markdown
                                       └─▶  workflow_rules ───┘
                                            (deterministic)
```

1. **Extract** — `extractor.py` pulls execution metadata and evidence from the tracker into the output tree.
2. **Audit** — `auditor.py` orchestrates model calls; `workflow_rules.py` applies deterministic, rule-based checks.
3. **Report** — `report.py` renders the dashboard and Markdown summary from the resulting `audit.json` files.

## Repository Layout

```text
argus.py                 end-to-end runner
extractor.py             tracker ingestion
auditor.py               model audit orchestration
workflow_rules.py        deterministic checks
report.py                HTML/Markdown report generation
report_assets/           dashboard CSS/JS
scripts/                 setup and run helpers
tests/                   unit and integration tests
```

See [SETUP.md](SETUP.md) for a step-by-step setup walkthrough and the pip
fallback path.

## Development

```bash
scripts/test.sh           # run the suite via uv
pytest -q                 # run the suite directly
```

Tests mock all external clients, so they run offline with no credentials.

## License

Released under the [MIT License](LICENSE).
