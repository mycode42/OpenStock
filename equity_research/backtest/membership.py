"""Point-in-time equity membership for research replays.

The selector's stored membership for each session is the only universe a
replay may trade. A session's trades use membership known before its open,
which is the previous completed close.
"""

from __future__ import annotations

from bisect import bisect_left
from datetime import date
from pathlib import Path

from options.research.history_store import DEFAULT_DATABASE, OptionsHistoryStore


class PointInTimeMembership:
    def __init__(self, members_by_as_of: dict[str, list[str]]) -> None:
        self._dates = sorted(members_by_as_of)
        self._members = {day: [str(t).upper() for t in tickers] for day, tickers in members_by_as_of.items()}

    def symbols_for(self, trading_day: date) -> list[str]:
        """Members known before ``trading_day`` opens; empty before any membership exists."""
        index = bisect_left(self._dates, trading_day.isoformat()) - 1
        if index < 0:
            return []
        return list(self._members[self._dates[index]])


def load_default_membership(
    database: str | Path = DEFAULT_DATABASE, rules_sha256: str | None = None,
) -> PointInTimeMembership | None:
    """Membership for the rules currently in force, or None when none is stored."""
    from options.research.universe_selection import _rules_hash, load_rules

    store = OptionsHistoryStore(Path(database))
    sha = rules_sha256 or _rules_hash(load_rules())
    members = store.membership_by_session(sha)
    return PointInTimeMembership(members) if members else None
