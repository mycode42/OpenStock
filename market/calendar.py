"""Alpaca's regular equity-session calendar for Algo safety checks."""

from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import requests

from src.accounts import get_account_for_mode


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


def _wall_time(value: str, trading_date: date) -> datetime:
    try:
        parsed = time.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid Alpaca calendar time: {value!r}") from exc
    return datetime.combine(trading_date, parsed, tzinfo=MARKET_TZ)


def get_market_schedule(
    *,
    on_date: date | None = None,
    mode: str = "algo",
) -> MarketSchedule | None:
    """Return the regular-session window, or ``None`` on an exchange holiday."""
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
    return MarketSchedule(
        trading_date=trading_date,
        opens_at=_wall_time(row.get("open"), trading_date),
        closes_at=_wall_time(row.get("close"), trading_date),
    )
