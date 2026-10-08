#!/usr/bin/env sh
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
printf 'Astra–Luna–Sol Orchestrator — project installer\n'
printf 'Existing project directory (e.g. /data/reactive-shortterm): '
IFS= read -r project || exit 2
if [ -z "$project" ]; then echo 'Cancelled: empty path' >&2; exit 2; fi
"$here/install.sh" "$project" --dry-run || exit $?
printf 'Proceed with the installation above? [y/N] '
IFS= read -r ok || exit 2
case "$ok" in
  y|Y|yes|YES) exec "$here/install.sh" "$project" ;;
  *) echo 'Cancelled; nothing changed.' ;;
esac

