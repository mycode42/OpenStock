#!/usr/bin/env sh
# Apply the Algo automation definitions to the OpenClaw gateway (silent LLM run, LLM digest, and the rest).
# Run as the OpenClaw service user, from anywhere. Needs the Telegram chat ID for the digest.
# Set ALGO_TRADER_PYTHON to the Poetry environment's python (`poetry env info -p`/bin/python).
set -eu
: "${ALGO_REPORT_TELEGRAM_TO:?set ALGO_REPORT_TELEGRAM_TO to the report chat ID}"
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
PYTHON="${ALGO_TRADER_PYTHON:-python3}"
exec "$PYTHON" scripts/setup_automations.py --backend openclaw "$@"
