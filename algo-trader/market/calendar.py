"""Alpaca's regular equity-session calendar for Algo safety checks."""

import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import requests

from runtime.http import get_session

from broker.accounts import get_account_for_mode


MARKET_TZ = ZoneInfo("America/New_York")
API_BASE = "https://paper-api.alpaca.markets/v2"


@dataclass(frozen=True)
class MarketSchedule:
    trading_date: date
    opens_at: datetime
    closes_at: datetime

    def market_open(self, now: datetime) -> bool:
        local_now = now.astimezone(MARKET_TZ)
        return self.opens_at <= local_now < self.closes_at


EXTENDED_SESSIONS = frozenset({"premarket", "after_hours", "overnight"})


def extended_session_name(now: datetime, schedule_for) -> str | None:
    """Name the open Alpaca extended session at ``now``, or ``None``.

    ``schedule_for(on_date)`` returns the regular-session window for a trading
    date, or ``None`` on an exchange holiday. Overnight 20:00-04:00 ET belongs
    to the following trading date, as in ``market.sessions.get_trading_window``.
    """
    local = now.astimezone(MARKET_TZ)
    day = local.date()
    if local.time() < time(4, 0):
        return "overnight" if schedule_for(day) is not None else None
    if local.time() >= time(20, 0):
        return "overnight" if schedule_for(day + timedelta(days=1)) is not None else None
    schedule = schedule_for(day)
    if schedule is None:
        return None
    if local < schedule.opens_at:
        return "premarket"
    if local >= schedule.closes_at:
        return "after_hours"
    return None


def _wall_time(value: str, trading_date: date) -> datetime:
    try:
        parsed = time.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid Alpaca calendar time: {value!r}") from exc
    return datetime.combine(trading_date, parsed, tzinfo=MARKET_TZ)


_SCHEDULE_CACHE: dict[tuple[str, date], MarketSchedule | None] = {}
_SCHEDULE_CACHE_LOCK = threading.Lock()
_SCHEDULE_CACHE_DAYS = 7


def get_market_schedule(
    *,
    on_date: date | None = None,
    mode: str = "algo",
) -> MarketSchedule | None:
    """Return the regular-session window, or ``None`` on an exchange holiday.

    A published calendar entry does not change for its date, so successful
    lookups are cached for the process. Failed lookups are never cached, so an
    outage still fails closed on the next check.
    """
    trading_date = on_date or datetime.now(MARKET_TZ).date()
    key = (mode, trading_date)
    with _SCHEDULE_CACHE_LOCK:
        if key in _SCHEDULE_CACHE:
            return _SCHEDULE_CACHE[key]
    schedule = _fetch_market_schedule(trading_date, mode)
    with _SCHEDULE_CACHE_LOCK:
        _SCHEDULE_CACHE[key] = schedule
        for stale in [k for k in _SCHEDULE_CACHE if (trading_date - k[1]).days > _SCHEDULE_CACHE_DAYS]:
            del _SCHEDULE_CACHE[stale]
    return schedule


def _fetch_market_schedule(trading_date: date, mode: str) -> MarketSchedule | None:
    response = get_session().get(
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
    return MarketSchedule(
        trading_date=trading_date,
        opens_at=_wall_time(row.get("open"), trading_date),
        closes_at=_wall_time(row.get("close"), trading_date),
    )
