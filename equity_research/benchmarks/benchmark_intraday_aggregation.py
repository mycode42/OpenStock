#!/usr/bin/env python3
"""Compare Python and Cython intraday aggregation on stored Algo bars."""

from __future__ import annotations

import argparse
from datetime import date, datetime
import json
from pathlib import Path
import resource
import sys
import time

from market.store import DEFAULT_DB, SQLiteBarStore
from strategies.multi_factor import features


FEED = "sip"
ADJUSTMENT = "split"
TIMEFRAME = "5Min"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--symbols", help="comma-separated symbols; defaults to every covered symbol")
    parser.add_argument("--start", help="start date; defaults to earliest covered bar")
    parser.add_argument("--end", help="end date; defaults to latest covered bar")
    parser.add_argument("--output", help="JSON result path; defaults to a timestamped /tmp file")
    return parser


def main() -> int:
    args = _parser().parse_args()
    cython_aggregate = features._aggregate_intraday_bars_cy
    if cython_aggregate is None:
        raise SystemExit("Cython extension is not built; run `poetry install` or `poetry build` first")

    store = SQLiteBarStore(args.db)
    coverage = {
        row["symbol"]: row
        for row in store.coverage()
        if row["feed"] == FEED and row["adjustment"] == ADJUSTMENT and row["timeframe"] == TIMEFRAME
    }
    symbols = (
        list(dict.fromkeys(part.strip().upper() for part in args.symbols.split(",") if part.strip()))
        if args.symbols else sorted(coverage)
    )
    missing = sorted(set(symbols) - set(coverage))
    if missing:
        raise SystemExit(f"No split-adjusted SIP 5-minute history for: {', '.join(missing)}")
    start = args.start or min(coverage[symbol]["first"][:10] for symbol in symbols)
    end = args.end or max(coverage[symbol]["last"][:10] for symbol in symbols)
    if date.fromisoformat(start) > date.fromisoformat(end):
        raise SystemExit("--start must be on or before --end")

    database_seconds = python_seconds = cython_seconds = 0.0
    bar_count = daily_row_count = 0
    started = time.perf_counter()
    for index, symbol in enumerate(symbols, 1):
        read_started = time.perf_counter()
        bars = store.bars(
            [symbol], feed=FEED, timeframe=TIMEFRAME,
            start=start, end=end, adjustment=ADJUSTMENT,
        )
        database_seconds += time.perf_counter() - read_started
        bar_count += len(bars.get(symbol, []))

        aggregate_started = time.perf_counter()
        python_result = features._aggregate_intraday_bars_python(bars)
        python_seconds += time.perf_counter() - aggregate_started

        aggregate_started = time.perf_counter()
        cython_result = cython_aggregate(bars, features.ET, features._aggregate_unordered_symbol)
        cython_seconds += time.perf_counter() - aggregate_started
        if python_result != cython_result:
            raise SystemExit(f"Python/Cython output mismatch for {symbol}")
        daily_row_count += len(cython_result.get(symbol, []))
        del bars, python_result, cython_result
        if index % 10 == 0 or index == len(symbols):
            print(f"checked {index}/{len(symbols)} symbols", flush=True)

    output = Path(args.output or (
        Path("/tmp") / f"algo-intraday-aggregation-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    )).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "database": str(Path(args.db).resolve()),
        "feed": FEED,
        "adjustment": ADJUSTMENT,
        "timeframe": TIMEFRAME,
        "start": start,
        "end": end,
        "symbols": len(symbols),
        "bars": bar_count,
        "daily_rows": daily_row_count,
        "parity": "exact",
        "sqlite_read_seconds": round(database_seconds, 3),
        "python_aggregation_seconds": round(python_seconds, 3),
        "cython_aggregation_seconds": round(cython_seconds, 3),
        "speedup": round(python_seconds / cython_seconds, 3) if cython_seconds else None,
        "wall_seconds": round(time.perf_counter() - started, 3),
        "peak_rss_kib": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),
        "python": sys.version,
    }
    output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"result": summary, "output": str(output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
