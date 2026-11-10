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

# Keys of the retired Bollinger-era mean reversion model. The 21-bar average model is the only one, so these are
# dropped for the intraday and weekly mean reversion profiles only: other families use some of these names.
RETIRED_MEAN_REVERSION_KEYS = frozenset({
    "window", "band_width", "entry_rsi", "exit_rsi", "short_entry_rsi", "short_exit_rsi",
    "threshold_mode", "volatility_lookback", "high_volatility_ratio", "entry_window_minutes", "signal_model",
})
MEAN_REVERSION_OWNERS = frozenset({"intraday_mean_reversion", "weekly_mean_reversion"})


def drop_retired_mean_reversion_keys(owner: str, values: dict) -> dict:
    if owner not in MEAN_REVERSION_OWNERS or not set(values) & RETIRED_MEAN_REVERSION_KEYS:
        return values
    retired = tuple(sorted(set(values) & RETIRED_MEAN_REVERSION_KEYS))
    marker = (owner, retired)
    if marker not in _WARNED:
        _WARNED.add(marker)
        LOG.warning("Ignoring retired %s profile setting(s): %s; no code reads them", owner, ", ".join(retired))
    return {key: value for key, value in values.items() if key not in RETIRED_MEAN_REVERSION_KEYS}
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
