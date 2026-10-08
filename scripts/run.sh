#!/usr/bin/env bash
set -euo pipefail
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
project=$(dirname "$here")
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
cmd=(codex -m gpt-6-astra -c 'model_reasoning_effort="medium"' -c 'agents.enabled=true' -c 'agents.max_concurrent_threads_per_session=6' -c 'agents.default_subagent_model="gpt-6-luna"' -c 'agents.default_subagent_reasoning_effort="max"')
if "$yolo"; then cmd+=(--yolo); fi
cmd+=("${extra[@]}")
if "$dry"; then
 if "$configure"; then echo 'Would configure OpenCodex globally: v1, 6 threads, Luna/Sol/Astra, max cap'; fi
 printf 'Would execute: '; printf '%q ' "${cmd[@]}"; echo; exit 0
fi
if "$configure"; then
 ocx v2 mode v1
 ocx v2 threads 6
 ocx agent subagents set gpt-6-luna,gpt-6.1-sol,gpt-6-astra
 ocx effort set --subagent max
fi
if ! command -v codex >/dev/null; then echo 'Codex CLI missing' >&2; exit 1; fi
cd "$project"
exec "${cmd[@]}"
