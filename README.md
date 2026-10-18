# ARGUS

ARGUS audits Jira/Zephyr test executions. It downloads test metadata and evidence,
combines deterministic QA rules with model-assisted image analysis, and produces
JSON, Markdown, and interactive HTML reports. Reports show **Compliant**,
**Concerns**, or **Deviation**; incomplete analysis is reported separately.

## Install

Python 3.11 or newer is required. From this repository:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[video]"
cp config.example.toml config.toml
```

The optional `video` extra provides PyAV for local video/audio decoding. Without
it, screenshot/PDF auditing remains available. ARGUS uses the Jira-hosted Zephyr
REST API; it is not a connector for every Zephyr Cloud API variant.

Edit `config.toml` with your Jira base URL and numeric project ID. Set credentials
in your shell (the values below are read interactively, not saved in history):

```bash
read -rs -p "Jira PAT: " ARGUS_JIRA_PAT; export ARGUS_JIRA_PAT; echo
read -rs -p "Model API key: " ARGUS_MODEL_API_KEY; export ARGUS_MODEL_API_KEY; echo
argus config validate
```

The default model endpoint uses the public Anthropic Messages API.
`ANTHROPIC_API_KEY` is also accepted. Model IDs and compatible endpoint URLs are
configurable. No cloud account, corporate login, or private package index is
required. Requests send selected evidence to the configured model provider.

## Run

```bash
argus audit --key QA-E123
argus audit --folder "Release regression"
argus audit --test-plan QA-P123
argus report --out-dir output/Release_regression --html
argus serve
```

Open `http://127.0.0.1:2405/` to browse reports. The server is local to your
machine. A report includes run grouping, execution-date filters, findings,
coverage, and CSV export. Folder/run/plan targets retain their own evidence
scope. Re-auditing an execution updates its result rather than duplicating it.

```bash
argus config profiles
argus --profile mobile audit --folder "Mobile regression"
argus costs
argus prune                 # preview evidence eligible for cleanup
argus prune --apply         # remove eligible evidence, retain audits/metadata
```

Profiles (`web`, `desktop`, `tablet`, `mobile`, `reader`, `tv`, `portal`) are
examples. Configure your own hosts, status names, policies, and credentials;
none contains a production account, team membership, or private service URL.

## Development

```bash
python -m pip install -e ".[dev,video,browser]"
python -m playwright install chromium
pytest -q
ruff check .
python -m build
python tools/check_public_tree.py --archives dist
```

See [Getting started](docs/getting-started.md),
[Configuration](docs/configuration.md), [Rules](docs/rules.md),
[Video and audio](docs/video-audio-audit.md), and
[the migration notes](docs/public-migration.md).
[Contributing](CONTRIBUTING.md) describes the test workflow.

## License

The project code is provided under the [MIT license](LICENSE).
Installed third-party dependencies retain their own licenses.
