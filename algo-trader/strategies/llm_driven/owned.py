"""Long positions the LLM strategy owns, read from the daemon's ownership table.

The analyst reviews these tickers even when they fall outside the live universe,
so the strategy can always decide whether to keep or sell what it opened.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from portfolio.config import ALGO_SERVICE_CONFIG
from runtime.paths import ROOT

OWNER_STRATEGY = "swing_llm_driven"


def owned_long_positions(positions: list[dict], state_path: Path | None = None) -> list[dict]:
    """Broker long positions whose ticker the strategy owns, with the figures an analyst needs."""
    path = state_path or (ROOT / ALGO_SERVICE_CONFIG["state_db"])
    if not path.exists():
        return []
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
        owners = {
            str(ticker).upper()
            for (ticker,) in connection.execute("SELECT ticker FROM position_owner WHERE strategy=?", (OWNER_STRATEGY,))
        }
    owned = []
    for position in positions:
        symbol = str(position.get("symbol") or "").upper()
        quantity = float(position.get("qty") or 0)
        if symbol in owners and quantity > 0:
            owned.append({
                "ticker": symbol,
                "qty": quantity,
                "avg_entry_price": float(position.get("avg_entry_price") or 0),
                "unrealized_plpc": float(position.get("unrealized_plpc") or 0),
            })
    return owned
