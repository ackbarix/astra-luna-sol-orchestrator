#!/usr/bin/env sh
# Astra-Luna-Sol installer front end. See `sh install.sh --help`.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
usage() {
 cat <<'USAGE'
Usage:
  sh install.sh PROJECT [--dry-run] [--skip-hooks] [--adopt] [--policy-file PATH]
                        [--check | --uninstall | --force-uninstall]
  sh install.sh PROJECT --hooks-only [--dry-run] [--adopt]
                        [--check | --uninstall | --force-uninstall]
  sh install.sh --global [--dry-run] [--adopt] [--check | --uninstall | --force-uninstall]

  PROJECT            existing project directory (project-local install, default)
  --global           harness in ${ALS_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/als},
                     roles/skill/profile in ${CODEX_HOME:-$HOME/.codex}; use `codex --profile als`
  --hooks-only       per-project .codex/hooks.json calling the global harness
  --dry-run          print the plan, write nothing
  --skip-hooks       project mode: no hooks (removes prior ALS hook groups only)
  --adopt            own existing files byte-identical to this release (lost manifest)
  --policy-file PATH project mode: in-repo file for the policy block (default AGENTS.md)
  --check            exit 0 ok, 1 not installed, 2 drift/refused, 4 outdated
  --uninstall        remove ALS files (missing ones are fine; modified ones refuse)
  --force-uninstall  back up drifted files to backup-<UTC>/, then remove managed paths
USAGE
}
if [ "$#" -eq 0 ]; then
 usage >&2
 exit 2
fi
case $1 in
 -h|--help) usage; exit 0 ;;
esac
if ! command -v python3 >/dev/null 2>&1; then
 printf '%s\n' 'REFUSED: python3 (3.11+) not found on PATH; install it and retry' >&2
 exit 2
fi
case $1 in
 --global) shift; exec python3 "$here/install.py" --global "$@" ;;
 -*) usage >&2; exit 2 ;;
esac
target=$1
shift
case $target in
 *[![:space:]]*) ;;
 *) printf '%s\n' 'REFUSED: empty project path' >&2; usage >&2; exit 2 ;;
esac
exec python3 "$here/install.py" --target "$target" "$@"
