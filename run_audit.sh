#!/usr/bin/env bash
# Pass normal audit options, or enter an exact folder name interactively.
set -euo pipefail
if (($# == 0)); then
    read -r -p 'Zephyr folder name: ' folder
    [[ -n "$folder" ]] || { echo 'A folder name is required.' >&2; exit 2; }
    set -- --folder "$folder"
fi
exec argus audit "$@"
