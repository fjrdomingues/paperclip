#!/bin/bash
# Wrapper to run classify from launchd, bypassing TCC restrictions on python3.
# bash has Full Disk Access; python3 does not.
set -euo pipefail
export PATH="/Users/fabiodomingues/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
export HOME="/Users/fabiodomingues"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec /usr/local/bin/python3 "$SCRIPT_DIR/run-classify.py"
