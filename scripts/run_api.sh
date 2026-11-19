#!/usr/bin/env bash
# Starts the backend on localhost. Reads .env if present.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
HOST="${TRADEDESK_API_HOST:-127.0.0.1}"
PORT="${TRADEDESK_API_PORT:-8000}"
exec python3 -m uvicorn --factory tradedesk.api.app:create_app --host "$HOST" --port "$PORT"
