"""Alpaca trading endpoints, one base URL variable per credential set.

Each base URL defaults to the paper endpoint. Setting the variable to any other URL is a live
trading setup, and it is refused unless ALPACA_ALLOW_LIVE=1 is set.
"""

import os

PAPER_TRADING_BASE = "https://paper-api.alpaca.markets/v2"
LIVE_OPT_IN_VARIABLE = "ALPACA_ALLOW_LIVE"


def _base(variable: str) -> str:
    value = (os.getenv(variable) or "").strip().rstrip("/") or PAPER_TRADING_BASE
    if value != PAPER_TRADING_BASE and os.getenv(LIVE_OPT_IN_VARIABLE, "").strip() != "1":
        raise RuntimeError(
            f"{variable} points at a non-paper endpoint; set {LIVE_OPT_IN_VARIABLE}=1 to allow live trading"
        )
    return value


ALGO_TRADING_BASE = _base("ALPACA_EQUITY_BASE_URL")
OPTIONS_TRADING_BASE = _base("ALPACA_OPTIONS_BASE_URL")
