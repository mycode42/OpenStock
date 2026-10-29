#!/usr/bin/env python3
"""Backfill split-adjusted 5-minute SIP bars for pool names missing them.

Idempotent: the store upserts on (feed, adjustment, timeframe, symbol, timestamp),
so re-running a range only refreshes existing rows.
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone

from market.store import DEFAULT_DB, SQLiteBarStore, fetch_alpaca_bars
from options.research.universe import load_history_universe, history_underlyings

LOG = logging.getLogger("backfill_intraday_sip")
YEARS = range(2016, 2027)
CHUNK = 10


def uncovered_symbols(store: SQLiteBarStore) -> list[str]:
    pool = sorted(set(history_underlyings(load_history_universe())))
    with store._connect() as connection:
        covered = {
            row[0] for row in connection.execute(
                "SELECT DISTINCT symbol FROM market_bar WHERE feed='sip' AND adjustment='split' AND timeframe='5Min'"
            )
        }
    return [symbol for symbol in pool if symbol not in covered]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=str(DEFAULT_DB))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    store = SQLiteBarStore(args.database)
    symbols = uncovered_symbols(store)
    LOG.info("uncovered symbols: %d %s", len(symbols), ",".join(symbols))
    if args.dry_run:
        return 0

    end = "2026-10-28T00:00:00Z"
    total = 0
    for year in YEARS:
        start = f"{year}-01-01T00:00:00Z"
        year_end = min(end, f"{year + 1}-01-01T00:00:00Z")
        for offset in range(0, len(symbols), CHUNK):
            chunk = symbols[offset:offset + CHUNK]
            started = datetime.now(timezone.utc)
            bars = fetch_alpaca_bars(
                chunk, timeframe="5Min", start=start, end=year_end,
                feed="sip", adjustment="split", mode="algo",
            )
            written = store.write_bars(
                bars, feed="sip", timeframe="5Min", adjustment="split",
                data_tier="algo_trader_plus", requested_start=start, requested_end=year_end,
            ) if bars else 0
            total += written
            LOG.info(
                "%d %s: %d rows in %.1fs (total %d)", year, ",".join(chunk), written,
                (datetime.now(timezone.utc) - started).total_seconds(), total,
            )
    LOG.info("done: %d rows written", total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
