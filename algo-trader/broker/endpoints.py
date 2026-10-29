"""Alpaca trading endpoints, one base URL variable per credential set.

Both algo sleeves are paper-only. Their base URLs default to the paper
endpoint and any other value is refused at import.
"""

import os

PAPER_TRADING_BASE = "https://paper-api.alpaca.markets/v2"


def _paper_base(variable: str) -> str:
    value = (os.getenv(variable) or "").strip().rstrip("/") or PAPER_TRADING_BASE
    if value != PAPER_TRADING_BASE:
        raise ValueError(f"{variable} must be {PAPER_TRADING_BASE}; the algo sleeves are paper-only")
    return value


ALGO_TRADING_BASE = _paper_base("ALPACA_ALGO_BASE_URL")
OPTIONS_TRADING_BASE = _paper_base("ALPACA_OPTIONS_BASE_URL")
