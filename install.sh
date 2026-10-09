#!/usr/bin/env sh
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
if [ "$#" -eq 0 ]; then
 printf 'Usage: sh install.sh /path/to/project [--dry-run|--check|--uninstall]\n' >&2
 exit 2
fi
target=$1
shift
exec python3 "$here/install.py" --target "$target" "$@"
