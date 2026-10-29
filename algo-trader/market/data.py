"""Shared Alpaca market-data feeds for the enabled Algo Trader Plus plan."""

from __future__ import annotations

import os


def data_tier() -> str:
    """Validate and return the shared Alpaca plan; all modes require Plus."""
    value = os.getenv("ALPACA_DATA_TIER", "algo_trader_plus").strip().lower().replace("-", "_")
    if value in {"algo_trader_plus", "plus"}:
        return "algo_trader_plus"
    raise ValueError("ALPACA_DATA_TIER must be algo_trader_plus")


def regular_feed() -> str:
    """Select SIP for every regular and extended session across all modes."""
    data_tier()
    return "sip"


def feeds_for_session(session: str) -> tuple[str, str]:
    """Return (historical bar feed, latest quote feed) for an Algo session."""
    data_tier()
    if session == "overnight":
        return "boats", "boats"
    return "sip", "sip"


def historical_delay_seconds(feed: str, session: str) -> int:
    """Return the configured delay; Plus BOATS history uses no extra delay."""
    data_tier()
    return 0


__all__ = ["data_tier", "feeds_for_session", "historical_delay_seconds", "regular_feed"]
