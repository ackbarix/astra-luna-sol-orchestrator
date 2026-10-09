#!/usr/bin/env sh
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
printf 'Astra–Luna–Sol 0.2.0 — existing project directory: '
IFS= read -r target || exit 2
[ -n "$target" ] || { echo 'Cancelled'; exit 2; }
sh "$here/install.sh" "$target" --dry-run
printf 'Apply the changes above? [y/N] '
IFS= read -r answer || exit 2
case "$answer" in
 y|Y|yes|YES) exec sh "$here/install.sh" "$target" ;;
 *) echo 'Cancelled; nothing changed.' ;;
esac
