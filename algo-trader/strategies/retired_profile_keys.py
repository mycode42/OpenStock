"""Profile keys retired from swing strategy configs.

These settings were validated but never read by signal logic. Stored profiles
may still carry them, so loaders drop them with one warning instead of failing.
"""

from __future__ import annotations

import logging

LOG = logging.getLogger(__name__)

RETIRED_PROFILE_KEYS = frozenset({"cooldown_days", "max_positions", "max_entries_per_day"})

_WARNED: set[tuple[str, tuple[str, ...]]] = set()


def drop_retired_profile_keys(owner: str, values: dict) -> dict:
    """Return values without retired keys; warn once per owner and key set."""
    retired = tuple(sorted(set(values) & RETIRED_PROFILE_KEYS))
    if not retired:
        return values
    marker = (owner, retired)
    if marker not in _WARNED:
        _WARNED.add(marker)
        LOG.warning(
            "Ignoring retired %s profile setting(s): %s; they have no effect",
            owner, ", ".join(retired),
        )
    return {key: value for key, value in values.items() if key not in RETIRED_PROFILE_KEYS}
