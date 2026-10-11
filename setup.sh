#!/usr/bin/env sh
# One-line bootstrap from a clone: sh setup.sh [--dry-run]
# Installs ALS globally (harness, roles, skill, `als` profile and the `als`
# command). Never edits shell rc files or $CODEX_HOME/config.toml.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
version=$(cat "$here/VERSION" 2>/dev/null || echo unknown)
bin_dir=${ALS_BIN_DIR:-$HOME/.local/bin}
case ${1-} in
 '') ;;
 --dry-run) ;;
 -h|--help) printf 'Usage: sh setup.sh [--dry-run]\n'; exit 0 ;;
 *) printf 'Usage: sh setup.sh [--dry-run]\n' >&2; exit 2 ;;
esac
printf 'Astra–Luna–Sol %s — global setup\n' "$version"
sh "$here/install.sh" --global "$@"
[ "${1-}" = --dry-run ] && exit 0
case ":${PATH-}:" in
 *":$bin_dir:"*) als=als ;;
 *) als=$bin_dir/als
    printf '\n%s is not on PATH. Add this line to your shell profile:\n  export PATH="%s:$PATH"\n' "$bin_dir" "$bin_dir" ;;
esac
printf '\nNext steps:\n'
printf '  1. %s doctor                               # check python3, git, codex features\n' "$als"
printf '  2. cd PROJECT && %s install --hooks-only   # optional: per-project harness hooks\n' "$als"
printf '  3. %s run                                  # codex --profile als\n' "$als"
