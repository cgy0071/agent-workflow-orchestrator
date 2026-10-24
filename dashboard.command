#!/bin/sh
# Double-click in macOS Finder, or run ./dashboard.command, to open the workflow dashboard.
# Extra arguments are passed to scripts/dashboard.py. Set AWO_PYTHON to choose an interpreter.
cd "$(dirname "$0")" || exit 1
for candidate in "${AWO_PYTHON:-}" python3 python; do
  if [ -n "$candidate" ] && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    exec "$candidate" -B scripts/dashboard.py "$@"
  fi
done
echo "Python 3.10 or newer was not found. Install it, or set AWO_PYTHON to its path." >&2
exit 1
