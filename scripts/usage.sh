#!/usr/bin/env bash
set -euo pipefail
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
# Project install: <project>/.astra-luna-sol/usage.sh. Global install: use the
# repository of the current directory (main worktree root), else the cwd.
if [ "$(basename -- "$here")" = .astra-luna-sol ]; then
  project=$(dirname -- "$here")
elif common=$(git rev-parse --git-common-dir 2>/dev/null); then
  # git < 2.13 prints a relative common dir relative to the top level even from
  # a subdirectory: resolve one that does not exist here against --show-cdup.
  case $common in
    /*) ;;
    *) [ -d "$common" ] || common=$(git rev-parse --show-cdup 2>/dev/null)$common ;;
  esac
  # Main checkout only when the common dir is .git; submodules use their top level.
  case $common in
    .git|*/.git) project=$(CDPATH= cd -- "$common/.." && pwd -P) ;;
    *) project=$(git rev-parse --show-toplevel 2>/dev/null || pwd -P) ;;
  esac
else
  project=$(pwd -P)
fi
# No selection option: report the latest session (token_usage.py needs one).
select=(--latest)
for arg in "$@"; do
  case $arg in
    --list|--root|--root=*|--latest|--latest-with-workers|--day|--day=*|-h|--help) select=() ;;
  esac
done
# bash 3.2 (macOS) treats "${empty[@]}" as unbound under set -u.
exec "${ALS_PYTHON:-python3}" "$here/token_usage.py" --cwd "$project" "$@" ${select[@]+"${select[@]}"}
