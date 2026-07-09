#!/usr/bin/env bash
# Development bootstrap wrapper for the personal-wiki backend. Runtime startup
# lives in app.serve so local-app launchers do not need shell.
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -x .venv/bin/python ]]; then
  echo "creating backend virtualenv"
  python3 -m venv .venv
fi
# Ensure deps are present (idempotent; fast when already satisfied). Re-installs
# if a previous run left a partial venv.
if ! ./.venv/bin/python -c "import uvicorn, fastapi, multipart" 2>/dev/null; then
  echo "installing backend deps"
  ./.venv/bin/pip install -q --upgrade pip >/dev/null 2>&1 || true
  ./.venv/bin/pip install -q -r requirements.txt
fi

exec ./.venv/bin/python -m app.serve "$@"
