"""Alpaca market-data feed selection for Basic and Algo Trader Plus."""

from __future__ import annotations

import os


def data_tier() -> str:
    """Return the configured Alpaca data plan; default to the free Basic plan."""
    value = os.getenv("ALPACA_DATA_TIER", "basic").strip().lower().replace("-", "_")
    if value in {"basic", "free"}:
        return "basic"
    if value in {"algo_trader_plus", "plus"}:
        return "algo_trader_plus"
    raise ValueError("ALPACA_DATA_TIER must be basic or algo_trader_plus")


def regular_feed() -> str:
    """Select the regular-session historical and quote feed."""
    return "sip" if data_tier() == "algo_trader_plus" else "iex"


def feeds_for_session(session: str) -> tuple[str, str]:
    """Return (historical bar feed, latest quote feed) for an Algo session."""
    if session == "overnight":
        # Basic gets delayed BOATS history and the indicative overnight quote
        # feed. Plus gets live BOATS data for both endpoints.
        return "boats", "boats" if data_tier() == "algo_trader_plus" else "overnight"
    feed = regular_feed()
    return feed, feed


def historical_delay_seconds(feed: str, session: str) -> int:
    """Basic overnight BOATS history must end at least 15 minutes in the past."""
    if feed == "boats" and session == "overnight" and data_tier() == "basic":
        return 15 * 60 + 5
    return 0


__all__ = ["data_tier", "feeds_for_session", "historical_delay_seconds", "regular_feed"]
