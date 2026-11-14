#!/usr/bin/env sh
# Render the OpenStock systemd user units into ~/.config/systemd/user.
# No root needed. By default this writes all templates; optional filenames
# let callers install only selected units without rewriting unrelated units.
# Start the dashboard with: systemctl --user enable --now openstock.service
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
NODE=$(command -v node)
TARGET="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

mkdir -p "$TARGET"
write_unit() {
    template=$1
    name=$(basename "$template")
    sed -e "s#{ROOT}#$ROOT#g" -e "s#{NODE}#$NODE#g" "$template" > "$TARGET/$name"
    echo "wrote $TARGET/$name"
}

if [ "$#" -gt 0 ]; then
    for name in "$@"; do
        case "$name" in
            */*|..*) echo "unit name must be a file in ops/systemd/user: $name" >&2; exit 2 ;;
        esac
        template="$ROOT/ops/systemd/user/$name"
        if [ ! -f "$template" ]; then
            echo "unit template not found: $template" >&2
            exit 2
        fi
        write_unit "$template"
    done
else
    for template in "$ROOT"/ops/systemd/user/*; do
        [ -f "$template" ] || continue
        write_unit "$template"
    done
fi

if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload || true
fi
echo "Unit files rendered. Enable desired services/timers with systemctl --user enable --now <unit>."
