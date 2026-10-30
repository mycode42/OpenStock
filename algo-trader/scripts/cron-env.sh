# Sourced by cron jobs that call systemctl --user. Cron has no user session bus, so this sets the bus for the
# current user. The uid comes from the account running cron, so the crontab needs no numeric id.
XDG_RUNTIME_DIR="/run/user/$(id -u)"
export XDG_RUNTIME_DIR
DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
export DBUS_SESSION_BUS_ADDRESS
