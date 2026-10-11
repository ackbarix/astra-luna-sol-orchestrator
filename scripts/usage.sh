#!/usr/bin/env bash
set -euo pipefail
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
# Project install: <project>/.astra-luna-sol/usage.sh. Global install: use the
# repository of the current directory (main worktree root), else the cwd.
if [ "$(basename -- "$here")" = .astra-luna-sol ]; then
  project=$(dirname -- "$here")
elif common=$(git rev-parse --git-common-dir 2>/dev/null); then
  project=$(CDPATH= cd -- "$common/.." && pwd -P)
else
  project=$(pwd -P)
fi
exec "${ALS_PYTHON:-python3}" "$here/token_usage.py" --cwd "$project" "$@"
