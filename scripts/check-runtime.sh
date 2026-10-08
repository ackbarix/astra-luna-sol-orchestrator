#!/usr/bin/env bash
set -euo pipefail
if ! command -v ocx >/dev/null 2>&1; then
  echo 'ocx not found on PATH' >&2
  exit 1
fi
if ! command -v codex >/dev/null 2>&1; then
  echo 'codex not found on PATH' >&2
  exit 1
fi
printf '%s\n' 'OpenCodex multi-agent status:'
ocx v2 status
printf '\n%s\n' 'Configured worker roster/effort caps:'
ocx agent status
printf '\n%s\n' 'Codex CLI version:'
codex --version
printf '\n%s\n' 'Live execution still needs a Codex session (/status) and OpenCodex request logs.'

