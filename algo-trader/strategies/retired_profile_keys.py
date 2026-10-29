"""Profile keys that signal parsers must not receive.

``RETIRED_PROFILE_KEYS`` are gone from the code: no signal logic or portfolio
setting reads them. Stored profiles and archived candidates may still carry
them, so loaders drop them with one warning instead of failing.

``PORTFOLIO_LEVEL_KEYS`` are real settings, but the portfolio layer reads them
from the strategy settings (see portfolio/config.py). Signal parsers ignore
them without a warning.
"""

from __future__ import annotations

import logging

LOG = logging.getLogger(__name__)

RETIRED_PROFILE_KEYS = frozenset({"cooldown_days"})
PORTFOLIO_LEVEL_KEYS = frozenset({"max_entries_per_day", "max_positions"})
_SIGNAL_IGNORED_KEYS = RETIRED_PROFILE_KEYS | PORTFOLIO_LEVEL_KEYS

_WARNED: set[tuple[str, tuple[str, ...]]] = set()


def drop_retired_profile_keys(owner: str, values: dict) -> dict:
    """Return values without keys a signal parser must not receive; warn once for retired keys."""
    retired = tuple(sorted(set(values) & RETIRED_PROFILE_KEYS))
    if retired:
        marker = (owner, retired)
        if marker not in _WARNED:
            _WARNED.add(marker)
            LOG.warning(
                "Ignoring retired %s profile setting(s): %s; no code reads them",
                owner, ", ".join(retired),
            )
    if not set(values) & _SIGNAL_IGNORED_KEYS:
        return values
    return {key: value for key, value in values.items() if key not in _SIGNAL_IGNORED_KEYS}
