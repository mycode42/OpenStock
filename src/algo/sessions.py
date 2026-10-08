"""Alpaca 24/5 US equity session windows in New York time."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from src.market_data import feeds_for_session


ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class TradingWindow:
    name: str
    trade_date: str
    extended_hours: bool
    bar_feed: str
    quote_feed: str
    regular_open: datetime
    regular_close: datetime

    @property
    def feed(self) -> str:
        """Historical feed alias for callers not yet using the split feeds."""
        return self.bar_feed


def _at(day: date, value: str) -> datetime:
    hour, minute = map(int, value.split(":")[:2])
    return datetime.combine(day, time(hour, minute), tzinfo=ET)


def get_trading_window(now: datetime, calendar: list[dict]) -> TradingWindow | None:
    """Return the active Alpaca session, honoring the regular-session calendar.

    Overnight is assigned to its ending/trading date: 20:00-04:00 ET runs only
    when the following date is a regular market session. Sunday 20:00 therefore
    opens Monday's overnight window, while Friday 20:00 remains closed.
    """
    now = now.astimezone(ET)
    rows = {str(row.get("date")): row for row in calendar if row.get("date")}
    local_day = now.date()
    local_time = now.timetz().replace(tzinfo=None)

    if local_time >= time(20, 0):
        trade_day = local_day + timedelta(days=1)
        row = rows.get(trade_day.isoformat())
        if row:
            regular_open = _at(trade_day, row["open"])
            regular_close = _at(trade_day, row["close"])
            bar_feed, quote_feed = feeds_for_session("overnight")
            return TradingWindow(
                "overnight", trade_day.isoformat(), True, bar_feed, quote_feed,
                regular_open, regular_close,
            )
        return None

    row = rows.get(local_day.isoformat())
    if not row:
        return None
    regular_open = _at(local_day, row["open"])
    regular_close = _at(local_day, row["close"])
    premarket_open = _at(local_day, "04:00")
    afterhours_close = _at(local_day, "20:00")

    if now < premarket_open:
        # This is the tail of the prior evening's overnight session.
        bar_feed, quote_feed = feeds_for_session("overnight")
        return TradingWindow(
            "overnight", local_day.isoformat(), True, bar_feed, quote_feed,
            regular_open, regular_close,
        )
    if premarket_open <= now < regular_open:
        bar_feed, quote_feed = feeds_for_session("premarket")
        return TradingWindow("premarket", local_day.isoformat(), True, bar_feed, quote_feed, regular_open, regular_close)
    if regular_open <= now < regular_close:
        bar_feed, quote_feed = feeds_for_session("regular")
        return TradingWindow("regular", local_day.isoformat(), False, bar_feed, quote_feed, regular_open, regular_close)
    if regular_close <= now < afterhours_close:
        bar_feed, quote_feed = feeds_for_session("after_hours")
        return TradingWindow("after_hours", local_day.isoformat(), True, bar_feed, quote_feed, regular_open, regular_close)
    return None


def next_session_start(now: datetime, calendar: list[dict]) -> datetime | None:
    """Find the next possible 20:00 ET overnight start from upcoming sessions."""
    now = now.astimezone(ET)
    for row in sorted(calendar, key=lambda item: str(item.get("date", ""))):
        try:
            trade_day = date.fromisoformat(str(row["date"]))
        except (KeyError, TypeError, ValueError):
            continue
        overnight_start = datetime.combine(trade_day - timedelta(days=1), time(20, 0), tzinfo=ET)
        premarket_start = _at(trade_day, "04:00")
        for candidate in (overnight_start, premarket_start):
            if candidate > now:
                return candidate
    return None


__all__ = ["TradingWindow", "get_trading_window", "next_session_start"]
