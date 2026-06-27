#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT"

if [ -f ".env.argus" ]; then
  . ./.env.argus
fi

: "${ARGUS_OUTPUT_DIR:=./output}"

if command -v uv >/dev/null 2>&1; then
  exec uv run argus-report --out-dir "$ARGUS_OUTPUT_DIR" --html "$@"
fi

PYTHON_BIN="python3"
if [ -x ".venv/bin/python" ]; then
  PYTHON_BIN=".venv/bin/python"
fi

exec "$PYTHON_BIN" report.py --out-dir "$ARGUS_OUTPUT_DIR" --html "$@"
