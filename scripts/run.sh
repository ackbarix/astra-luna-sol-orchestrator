#!/usr/bin/env bash
set -euo pipefail
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
# Project install: <project>/.astra-luna-sol/run.sh. Global install: the
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
configure=false; yolo=false; dry=false
extra=()
while (($#)); do
 case "$1" in
  --configure-ocx) configure=true ;;
  --yolo) yolo=true ;;
  --dry-run) dry=true ;;
  --help|-h) echo 'Usage: run.sh [--configure-ocx] [--yolo] [--dry-run] [-- codex args]'; exit 0 ;;
  --) shift; extra+=("$@"); break ;;
  *) extra+=("$1") ;;
 esac
 shift
done
cmd=(codex -m gpt-6-astra -c 'model_reasoning_effort="medium"' -c 'agents.enabled=true' -c 'agents.max_concurrent_threads_per_session=6' -c 'agents.max_depth=1' -c 'agents.default_subagent_model="gpt-6-luna"' -c 'agents.default_subagent_reasoning_effort="max"')
if "$yolo"; then cmd+=(--yolo); fi
# bash 3.2 (macOS) treats "${empty[@]}" as unbound under set -u.
cmd+=(${extra[@]+"${extra[@]}"})
if "$dry"; then
 if "$configure"; then echo 'Would change GLOBAL OpenCodex: v1, 6 threads, GPT roster, child effort cap max'; fi
 printf 'Would execute: '; printf '%q ' "${cmd[@]}"; echo; exit 0
fi
if "$configure"; then
 command -v ocx >/dev/null 2>&1 || { echo 'ocx (OpenCodex) not found on PATH; --configure-ocx needs it' >&2; exit 1; }
 echo 'Changing global OpenCodex settings (explicit --configure-ocx). These affect other projects too.'
 ocx v2 mode v1
 ocx v2 threads 6
 ocx agent subagents set gpt-6-luna,gpt-6.1-sol,gpt-6-astra
 ocx effort set --subagent max
fi
command -v codex >/dev/null || { echo 'Codex CLI missing' >&2; exit 1; }
CDPATH= cd -- "$project"
exec "${cmd[@]}"
