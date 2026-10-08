#!/usr/bin/env bash
set -euo pipefail
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
project=$(dirname "$here")
# Read-only sessions report, defaults to current installed project.
exec python3 "$here/token_usage.py" --cwd "$project" "$@"

