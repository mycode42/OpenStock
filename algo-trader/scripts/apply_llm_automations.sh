#!/usr/bin/env sh
# Apply the Algo automation definitions to the OpenClaw gateway (silent LLM run, LLM digest, and the rest).
# Run as the OpenClaw service user, from anywhere. Needs the Telegram chat ID for the digest.
set -eu
: "${ALGO_REPORT_TELEGRAM_TO:?set ALGO_REPORT_TELEGRAM_TO to the report chat ID}"
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
exec "/home/openclaw/.cache/pypoetry/virtualenvs/algo-trader-lprB-8wK-py3.12/bin/python" scripts/setup_automations.py --backend openclaw "$@"
