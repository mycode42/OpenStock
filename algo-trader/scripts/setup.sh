#!/usr/bin/env bash
# One-command Algo setup: Python dependencies and the user systemd units.
# Renders the units (no root) but does not start them or create .env. Automations are managed with `openclaw automations`.
set -euo pipefail

cd "$(dirname "$0")/.."

poetry install --with ai,ml,research

# The research cycles import these at run time; fail here, not in the middle of a night's run.
if ! poetry run python -c "import lightgbm, numpy, sklearn, pandas" 2>/dev/null; then
    echo "error: the Poetry environment is missing research dependencies (lightgbm, numpy, scikit-learn, pandas)." >&2
    echo "       check 'poetry env info -p' and rerun this script." >&2
    exit 1
fi

poetry run python scripts/install_units.py

echo "Setup finished. Start the services with: systemctl --user enable --now equity options"
