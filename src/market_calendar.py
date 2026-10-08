"""Broker trading hours used by Day entry and liquidation safeguards.

Alpaca's calendar includes exchange holidays and early closes. Times returned
by the v2 calendar are New York wall times; an empty result means there is no
regular equity session on that date.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import requests

from src.accounts import get_account_for_mode


MARKET_TZ = ZoneInfo("America/New_York")
API_BASE = "https://paper-api.alpaca.markets/v2"


@dataclass(frozen=True)
class DaySchedule:
    trading_date: date
    opens_at: datetime
    closes_at: datetime
    entry_cutoff_at: datetime
    flatten_at: datetime

    def entries_allowed(self, now: datetime) -> bool:
        local_now = now.astimezone(MARKET_TZ)
        return self.opens_at <= local_now < self.entry_cutoff_at

    def flatten_due(self, now: datetime) -> bool:
        return now.astimezone(MARKET_TZ) >= self.flatten_at

    def market_open(self, now: datetime) -> bool:
        local_now = now.astimezone(MARKET_TZ)
        return self.opens_at <= local_now < self.closes_at


def _wall_time(value: str, trading_date: date) -> datetime:
    """Parse a calendar open/close time, rejecting malformed broker data."""
    try:
        parsed = time.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid Alpaca calendar time: {value!r}") from exc
    return datetime.combine(trading_date, parsed, tzinfo=MARKET_TZ)


def get_day_schedule(
    *,
    flatten_by: str,
    flatten_lead_minutes: int = 15,
    entry_cutoff_minutes: int = 15,
    on_date: date | None = None,
    mode: str = "day",
) -> DaySchedule | None:
    """Return Day deadlines for the actual session, or None on a holiday.

    A configured flatten time is an upper bound. Early-close sessions move
    flatten earlier so market orders have time to fill before the bell.
    """
    trading_date = on_date or datetime.now(MARKET_TZ).date()
    response = requests.get(
        f"{API_BASE}/calendar",
        headers=get_account_for_mode(mode).headers,
        params={"start": trading_date.isoformat(), "end": trading_date.isoformat()},
        timeout=10,
    )
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        raise ValueError("Alpaca calendar response was not a list")
    if not rows:
        return None
    if len(rows) != 1 or rows[0].get("date") != trading_date.isoformat():
        raise ValueError("Alpaca calendar returned an unexpected trading date")
    row = rows[0]
    opens_at = _wall_time(row.get("open"), trading_date)
    closes_at = _wall_time(row.get("close"), trading_date)
    if closes_at <= opens_at:
        raise ValueError("Alpaca calendar close must follow open")
    try:
        configured_time = time.fromisoformat(flatten_by)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid configured Day flatten time: {flatten_by!r}") from exc
    configured_flatten = datetime.combine(trading_date, configured_time, tzinfo=MARKET_TZ)
    flatten_at = min(configured_flatten, closes_at - timedelta(minutes=flatten_lead_minutes))
    entry_cutoff_at = flatten_at - timedelta(minutes=entry_cutoff_minutes)
    if entry_cutoff_at <= opens_at or flatten_at >= closes_at:
        raise ValueError("Day entry and flatten deadlines do not fit the market session")
    return DaySchedule(
        trading_date=trading_date,
        opens_at=opens_at,
        closes_at=closes_at,
        entry_cutoff_at=entry_cutoff_at,
        flatten_at=flatten_at,
    )
