#!/usr/bin/env bash
# One-command Algo setup: Python dependencies, then the scheduled automations.
# Extra arguments go to scripts/setup_automations.py, e.g. --dry-run or --backend cron.
# Renders the user systemd units (no root) but does not start them or create .env.
set -euo pipefail

cd "$(dirname "$0")/.."

poetry install --with ai,ml,research

# The research cycles import these at run time; fail here, not in the middle of a night's run.
if ! poetry run python -c "import lightgbm, numpy, sklearn, pandas" 2>/dev/null; then
    echo "error: the Poetry environment is missing research dependencies (lightgbm, numpy, scikit-learn, pandas)." >&2
    echo "       check 'poetry env info -p' and rerun this script." >&2
    exit 1
fi

if [ -z "${ALGO_REPORT_TELEGRAM_TO:-}" ]; then
    echo "note: ALGO_REPORT_TELEGRAM_TO is unset; the agent-turn jobs will not install with the OpenClaw backend" >&2
fi

poetry run python scripts/install_units.py
poetry run python scripts/setup_automations.py "$@"

echo "Setup finished. Start the services with: systemctl --user enable --now equity options"
