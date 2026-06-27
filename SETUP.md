# Setup

1. Use the automated uv setup.

```bash
scripts/bootstrap.sh
```

This creates `.env.argus`, writes `config.toml` from those variables, and runs
`uv sync --dev`.

2. Edit `.env.argus`, then regenerate `config.toml`.

```bash
$EDITOR .env.argus
scripts/bootstrap.sh
```

3. Verify the local code.

```bash
scripts/test.sh
```

4. Generate a local report from existing audit files.

```bash
scripts/report.sh
```

5. Run a full audit after adapting tracker/model settings.

```bash
scripts/run-audit.sh --key QA-E123456
```

## pip fallback

Create a virtual environment manually.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

Copy the example config.

```bash
cp config.example.toml config.toml
```

Edit `config.toml`.

Set your issue-tracker base URL, output directory, LLM model/provider
settings, and batch concurrency. Keep real credentials outside the repo,
for example in environment variables or files ignored by `.gitignore`.
The default `env_check_engine = "off"` is intentional for public clones.

Run a local report from existing audit files.

```bash
python report.py --out-dir ./output/my-run --html
```

Run a full audit after adapting tracker/model settings.

```bash
python argus.py --key QA-E123456
```

The public copy is intentionally generic. The original private deployment
used company-specific tracker projects, model credentials, and notification
plumbing; those details have been removed or replaced with placeholders.

For model calls, set `ARGUS_MODEL_PROVIDER` to `openai`, `anthropic`, or
`bedrock`. OpenAI and Anthropic use the API key environment variable named by
`ARGUS_API_KEY_ENV`; Bedrock uses `ARGUS_MODEL_REGION` and
`ARGUS_CLOUD_PROFILE`.
