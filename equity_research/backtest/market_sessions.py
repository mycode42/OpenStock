"""New York session labels used by extended-hours replay."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import math
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")


def market_session(timestamp: datetime) -> str | None:
    """Return the trading session for a timestamp, or None when markets are closed."""
    local = timestamp.astimezone(ET) if timestamp.tzinfo else timestamp.replace(tzinfo=ET)
    weekday = local.weekday()
    clock = local.time().replace(tzinfo=None)

    # BOATS runs Sunday 20:00 through Friday 04:00 ET. The evening portion
    # belongs to the following New York trading date.
    if clock >= time(20, 0):
        return "overnight" if weekday in {6, 0, 1, 2, 3} else None
    if clock < time(4, 0):
        return "overnight" if weekday in {0, 1, 2, 3, 4} else None
    if weekday >= 5:
        return None
    if clock < time(9, 30):
        return "pre_market"
    if clock < time(16, 0):
        return "regular"
    if clock < time(20, 0):
        return "after_hours"
    return None


def trading_date(timestamp: datetime, *, include_extended_hours: bool = False) -> date:
    """Map an event to its replay day; overnight evening bars roll to tomorrow."""
    local = timestamp.astimezone(ET) if timestamp.tzinfo else timestamp.replace(tzinfo=ET)
    if (
        include_extended_hours
        and market_session(local) == "overnight"
        and local.time().replace(tzinfo=None) >= time(20, 0)
    ):
        return local.date() + timedelta(days=1)
    return local.date()


def extended_session_bars(
    bars_by_symbol: dict[str, list[dict]], *, session: str,
) -> tuple[dict[str, list[dict]], int, int]:
    """Keep valid OHLC bars from one non-regular session family."""
    if session not in {"extended", "overnight"}:
        raise ValueError("session must be extended or overnight")
    expected = {"pre_market", "after_hours"} if session == "extended" else {"overnight"}
    kept_by_symbol: dict[str, list[dict]] = {}
    invalid = 0
    outside = 0
    for symbol, bars in bars_by_symbol.items():
        kept = []
        for bar in bars:
            try:
                timestamp = datetime.fromisoformat(str(bar["t"]).replace("Z", "+00:00"))
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=ET)
                if market_session(timestamp) not in expected:
                    outside += 1
                    continue
                opening, high, low, close = (float(bar[key]) for key in ("o", "h", "l", "c"))
                if (
                    not all(math.isfinite(value) and value > 0 for value in (opening, high, low, close))
                    or high < max(opening, close, low)
                    or low > min(opening, close, high)
                ):
                    invalid += 1
                    continue
            except (KeyError, TypeError, ValueError, OverflowError):
                invalid += 1
                continue
            kept.append(bar)
        if kept:
            kept_by_symbol[symbol.upper()] = kept
    return kept_by_symbol, outside, invalid


__all__ = ["ET", "market_session", "trading_date", "extended_session_bars"]
