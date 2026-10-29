#!/usr/bin/env sh
# Render the OpenStock systemd user units into ~/.config/systemd/user.
# No root needed. This only writes files; start them with
#   systemctl --user enable --now openstock.service openstock-monitor.timer
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
NODE=$(command -v node)
TARGET="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

mkdir -p "$TARGET"
for template in "$ROOT"/ops/systemd/user/*; do
    name=$(basename "$template")
    sed -e "s#{ROOT}#$ROOT#g" -e "s#{NODE}#$NODE#g" "$template" > "$TARGET/$name"
    echo "wrote $TARGET/$name"
done

if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload || true
fi
echo "start with: systemctl --user enable --now openstock.service openstock-monitor.timer"
