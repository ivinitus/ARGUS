#!/usr/bin/env bash
# Activate the installation's virtual environment before scheduling this script.
set -euo pipefail
argus cycle "$@"
argus prune --apply --if-enabled
