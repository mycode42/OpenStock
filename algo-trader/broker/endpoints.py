"""Alpaca trading endpoints, one base URL variable per credential set.

Each base URL defaults to the paper endpoint. Setting the variable overrides it.
"""

import os

PAPER_TRADING_BASE = "https://paper-api.alpaca.markets/v2"


def _base(variable: str) -> str:
    return (os.getenv(variable) or "").strip().rstrip("/") or PAPER_TRADING_BASE


ALGO_TRADING_BASE = _base("ALPACA_EQUITY_BASE_URL")
OPTIONS_TRADING_BASE = _base("ALPACA_OPTIONS_BASE_URL")
