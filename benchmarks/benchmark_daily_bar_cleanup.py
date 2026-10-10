#!/usr/bin/env python3
"""Compare Python and Cython daily-bar cleanup on stored Algo histories."""

from __future__ import annotations

import argparse
from datetime import date, timedelta
import json
from pathlib import Path
import statistics
import time

from src.algo.market_store import DEFAULT_DB, SQLiteBarStore
from src.algo.strategies import swing_common
from src.algo.strategies.multi_factor.features import aggregate_intraday_bars
from src.algo.strategies.universe import configured_universe


FEED = "sip"
ADJUSTMENT = "split"


def _load_histories(args):
    coverage = SQLiteBarStore(args.db).coverage()
    required = set(args.symbols)
    rows = [
        row for row in coverage
        if row["feed"] == FEED and row["adjustment"] == ADJUSTMENT
        and row["timeframe"] == "5Min" and row["symbol"] in required
    ]
    latest = {row["symbol"]: date.fromisoformat(row["last"][:10]) for row in rows}
    missing = sorted(required - set(latest))
    if missing:
        raise ValueError("No stored daily history for: " + ", ".join(missing))
    end = min(latest.values())
    start = end - timedelta(days=args.days)
    intraday = SQLiteBarStore(args.db).bars(
        args.symbols, feed=FEED, timeframe="5Min", start=start.isoformat(),
        end=end.isoformat(), adjustment=ADJUSTMENT,
    )
    daily = aggregate_intraday_bars(intraday)
    available = {symbol: daily[symbol] for symbol in args.symbols if daily.get(symbol)}
    if not available:
        raise ValueError("No stored daily rows in the requested range")
    return start, end, available


def _clean(use_cython, bars):
    saved = swing_common._clean_daily_bars_cy
    swing_common._clean_daily_bars_cy = saved if use_cython else None
    try:
        return swing_common._clean_daily_bars_uncached(bars)
    finally:
        swing_common._clean_daily_bars_cy = saved


def _signature(rows):
    return [(session, bar, close) for session, bar, close in rows]


def _timed_pass(use_cython, histories, cycles):
    saved = swing_common._clean_daily_bars_cy
    swing_common._clean_daily_bars_cy = saved if use_cython else None
    started = time.perf_counter()
    try:
        for _ in range(cycles):
            for bars in histories.values():
                swing_common._clean_daily_bars_uncached(bars)
        return time.perf_counter() - started
    finally:
        swing_common._clean_daily_bars_cy = saved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--symbols", help="comma-separated symbols; default is the Algo universe")
    parser.add_argument("--days", type=int, default=455,
                        help="calendar history days (365-day research window plus warm-up by default)")
    parser.add_argument("--cycles", type=int, default=2016,
                        help="passes over each symbol history; defaults to 44,352 cleaner calls for 22 symbols")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--output", help="JSON output path; defaults to /tmp")
    args = parser.parse_args()
    args.symbols = (
        list(dict.fromkeys(part.strip().upper() for part in args.symbols.split(",") if part.strip()))
        if args.symbols else configured_universe("algo")["tickers"]
    )
    if args.days < 1 or args.cycles < 1 or args.rounds < 1:
        raise SystemExit("days, cycles, and rounds must be positive")
    if swing_common._clean_daily_bars_cy is None:
        raise SystemExit("Cython daily cleaner is unavailable; run scripts/build-extension.py")

    start, end, histories = _load_histories(args)
    python_rows = {symbol: _clean(False, bars) for symbol, bars in histories.items()}
    cython_rows = {symbol: _clean(True, bars) for symbol, bars in histories.items()}
    if {key: _signature(value) for key, value in python_rows.items()} != {
        key: _signature(value) for key, value in cython_rows.items()
    }:
        raise SystemExit("Python and Cython daily-bar cleanup differs")

    # Include unsorted and invalid rows to exercise the cleanup fallback path.
    edge_rows = [
        {"date": "2025-01-03", "c": "103.5"},
        {"date": "2025-01-02", "c": 102},
        {"date": "bad-date", "c": 101},
        {"date": "2025-01-04", "c": float("nan")},
        {"date": "2025-01-05", "c": 0},
        {"t": "2025-01-06T00:00:00Z", "c": 104},
    ]
    if _signature(_clean(False, edge_rows)) != _signature(_clean(True, edge_rows)):
        raise SystemExit("Python and Cython cleanup differ on unsorted or invalid rows")

    calls_per_pass = len(histories) * args.cycles
    timings = {"python": [], "cython": []}
    for index in range(args.rounds):
        order = ("python", "cython") if index % 2 == 0 else ("cython", "python")
        for engine in order:
            timings[engine].append(_timed_pass(engine == "cython", histories, args.cycles))

    python_median = statistics.median(timings["python"])
    cython_median = statistics.median(timings["cython"])
    result = {
        "database": str(Path(args.db).resolve()),
        "symbols": sorted(histories),
        "start": start.isoformat(), "end": end.isoformat(),
        "history_rows": sum(len(rows) for rows in histories.values()),
        "cleaner_calls_per_pass": calls_per_pass,
        "bar_visits_per_pass": args.cycles * sum(len(rows) for rows in histories.values()),
        "exact_parity": True,
        "edge_case_parity": True,
        "seconds": {key: [round(value, 3) for value in values] for key, values in timings.items()},
        "median_seconds": {
            "python": round(python_median, 3),
            "cython": round(cython_median, 3),
            "speedup": round(python_median / cython_median, 3),
        },
    }
    output_path = Path(args.output or (
        Path("/tmp") / f"algo-daily-cleanup-{time.strftime('%Y%m%d-%H%M%S')}.json"
    )).resolve()
    output_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"result": result, "output": str(output_path)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
