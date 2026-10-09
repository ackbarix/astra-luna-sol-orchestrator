#!/usr/bin/env bash
set -euo pipefail
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
project=$(dirname "$here")
exec python3 "$here/token_usage.py" --cwd "$project" "$@"
