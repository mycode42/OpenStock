#!/usr/bin/env python3
"""Compare the Python and Cython replay timeline builders on stored bars."""

from __future__ import annotations

import argparse
from datetime import date, timedelta
import gc
import json
from pathlib import Path
import statistics
import time

from autoresearch.backtest import execution
from autoresearch.backtest import AlgoPortfolioBacktest, DAILY_WARMUP_DAYS
from market.store import DEFAULT_DB, SQLiteBarStore
from strategies.universe import configured_universe


FEED = "sip"
ADJUSTMENT = "split"


def _inputs(args):
    if args.start and args.end:
        start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    elif args.start or args.end:
        raise ValueError("Pass both --start and --end, or neither")
    else:
        end = min(
            date.fromisoformat(row["last"][:10])
            for row in SQLiteBarStore(args.db).coverage()
            if row["feed"] == FEED and row["adjustment"] == ADJUSTMENT
            and row["timeframe"] == "5Min" and row["symbol"] in set(args.symbols)
        )
        start = end - timedelta(days=args.days)
    if end < start:
        raise ValueError("End date must not be before start date")

    store = SQLiteBarStore(args.db)
    intraday = store.bars(
        args.symbols, feed=FEED, timeframe="5Min", start=start.isoformat(),
        end=end.isoformat(), adjustment=ADJUSTMENT,
    )
    spy_daily = store.bars(
        ["SPY"], feed=FEED, timeframe="1Day",
        start=(start - timedelta(days=DAILY_WARMUP_DAYS)).isoformat(),
        end=end.isoformat(), adjustment=ADJUSTMENT,
    ).get("SPY", [])
    symbols = [symbol for symbol in args.symbols if intraday.get(symbol)]
    if not symbols:
        raise ValueError("No stored intraday bars for the requested symbols and period")
    return start, end, symbols, intraday, spy_daily


def _replay(start, end, symbols, intraday, spy_daily, strategy):
    return AlgoPortfolioBacktest(
        symbols=symbols,
        intraday_bars={name: intraday[name] for name in symbols},
        spy_daily_bars=spy_daily,
        start=start.isoformat(), end=end.isoformat(),
        feed=FEED, data_adjustment=ADJUSTMENT,
        strategies=[strategy], initial_capital=100_000, slippage_bps=5,
        spread_bps=3, commission_per_share=0.005,
        short_borrow_annual_pct=0.03, unknown_borrow_penalty_bps=25,
        force_close_at_end=False,
    )


def _timeline(use_cython, replay):
    saved = execution._build_timeline_cy
    execution._build_timeline_cy = saved if use_cython else None
    try:
        return replay._timeline()
    finally:
        execution._build_timeline_cy = saved


def _timeline_signature(events):
    return [(stamp, list(bars.items())) for stamp, bars in events.items()]


def _report_signature(report):
    return {
        key: report.get(key)
        for key in (
            "net_return_pct", "max_drawdown_pct", "closed_trade_count", "entry_count",
        )
    }


def _full_replay(use_cython, start, end, symbols, intraday, spy_daily, strategy):
    replay = _replay(start, end, symbols, intraday, spy_daily, strategy)
    saved = execution._build_timeline_cy
    execution._build_timeline_cy = saved if use_cython else None
    try:
        started = time.perf_counter()
        result = replay.run()
        elapsed = time.perf_counter() - started
        return elapsed, _report_signature(result)
    finally:
        execution._build_timeline_cy = saved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--symbols", help="comma-separated symbols; default is the Algo universe")
    parser.add_argument("--strategy", default="swing_trend_following")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", help="JSON output path; defaults to /tmp")
    args = parser.parse_args()
    args.symbols = (
        list(dict.fromkeys(part.strip().upper() for part in args.symbols.split(",") if part.strip()))
        if args.symbols else configured_universe("algo")["tickers"]
    )
    if args.days < 1 or args.repeats < 1:
        raise SystemExit("days and repeats must be positive")
    if execution._build_timeline_cy is None:
        raise SystemExit("Cython timeline extension is unavailable; run scripts/build-extension.py")

    start, end, symbols, intraday, spy_daily = _inputs(args)
    replay = _replay(start, end, symbols, intraday, spy_daily, args.strategy)
    python_events = _timeline(False, replay)
    cython_events = _timeline(True, replay)
    parity = _timeline_signature(python_events) == _timeline_signature(cython_events)
    if not parity:
        raise SystemExit("Python and Cython timelines differ")
    event_count = len(cython_events)
    del python_events, cython_events, replay
    gc.collect()

    timings = {"python": [], "cython": []}
    for index in range(args.repeats):
        order = ("python", "cython") if index % 2 == 0 else ("cython", "python")
        for engine in order:
            replay = _replay(start, end, symbols, intraday, spy_daily, args.strategy)
            started = time.perf_counter()
            events = _timeline(engine == "cython", replay)
            timings[engine].append(time.perf_counter() - started)
            del events, replay
            gc.collect()

    full_replay = {}
    for engine in ("python", "cython"):
        elapsed, signature = _full_replay(
            engine == "cython", start, end, symbols, intraday, spy_daily, args.strategy,
        )
        full_replay[engine] = {"wall_seconds": round(elapsed, 3), "metrics": signature}
    if full_replay["python"]["metrics"] != full_replay["cython"]["metrics"]:
        raise SystemExit("Full replay metrics differ between Python and Cython")

    python_median = statistics.median(timings["python"])
    cython_median = statistics.median(timings["cython"])
    result = {
        "database": str(Path(args.db).resolve()),
        "symbols": symbols,
        "start": start.isoformat(), "end": end.isoformat(),
        "bar_count": sum(len(intraday[symbol]) for symbol in symbols),
        "event_timestamp_count": event_count,
        "timeline_exact_parity": parity,
        "timeline_seconds": {
            engine: [round(value, 3) for value in values]
            for engine, values in timings.items()
        },
        "timeline_median_seconds": {
            "python": round(python_median, 3),
            "cython": round(cython_median, 3),
            "speedup": round(python_median / cython_median, 3),
        },
        "full_replay": full_replay,
        "full_replay_metrics_exact_parity": True,
    }
    output_path = Path(args.output or (
        Path("/tmp") / f"algo-replay-timeline-{time.strftime('%Y%m%d-%H%M%S')}.json"
    )).resolve()
    output_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"result": result, "output": str(output_path)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
