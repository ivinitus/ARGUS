# Contributing

Use Python 3.11 or newer:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev,video,browser]"
python -m playwright install chromium
pytest -q
ruff check .
python -m build
python tools/check_public_tree.py --archives dist
```

The browser-independent JavaScript tests require Node.js:

```bash
node --test tests/js/*.test.cjs tests/operator-controls.test.mjs
```

Tests block outbound connections. Mock Jira, model APIs, and Slack. Use invented
execution data and reserved example domains in fixtures; do not add credentials,
real evidence, or private infrastructure.

Keep changes to audit policy separate from transport or presentation changes.
When changing a finding, add a regression that demonstrates the underlying
evidence and the expected verdict.
