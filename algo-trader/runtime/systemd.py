"""Systemd status checks shared by service health and account reporting."""

from __future__ import annotations

import subprocess


def service_is_active(unit: str) -> bool | None:
    """Return state of a user-scope unit, or ``None`` when systemd cannot answer."""
    try:
        return subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", unit],
            check=False,
            timeout=5,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return None
