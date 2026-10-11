#!/usr/bin/env sh
# One-line bootstrap from a clone: sh setup.sh [--dry-run]
# Installs ALS globally (harness, roles, skill, `als` profile and the `als`
# command). Never edits shell rc files or $CODEX_HOME/config.toml.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
version=$(cat "$here/VERSION" 2>/dev/null || echo unknown)
bin_dir=${ALS_BIN_DIR:-$HOME/.local/bin}
py=${ALS_PYTHON:-python3}
case ${1-} in
 '') ;;
 --dry-run) ;;
 -h|--help) printf 'Usage: sh setup.sh [--dry-run]\n'; exit 0 ;;
 *) printf 'Usage: sh setup.sh [--dry-run]\n' >&2; exit 2 ;;
esac
printf 'Astra–Luna–Sol %s — global setup\n' "$version"
if ! command -v "$py" >/dev/null 2>&1; then
 printf 'REFUSED: %s (Python 3.9+) not found on PATH; install it or set ALS_PYTHON=/path/to/python3\n' "$py" >&2
 exit 2
fi
sh "$here/install.sh" --global "$@"
[ "${1-}" = --dry-run ] && exit 0
# install.py already printed the PATH note when bin_dir is not on PATH.
case ":${PATH-}:" in
 *":$bin_dir:"*) als=als ;;
 *) als=$bin_dir/als ;;
esac
printf '\nNext steps:\n'
printf '  1. %s doctor                               # check python3, git, codex features\n' "$als"
printf '  2. cd PROJECT && %s install --hooks-only   # optional: per-project harness hooks\n' "$als"
printf '  3. %s run                                  # codex --profile als\n' "$als"
