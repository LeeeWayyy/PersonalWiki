#!/usr/bin/env bash
# Compatibility wrapper for the Python app startup manager.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 scripts/app_start.py "$@"
