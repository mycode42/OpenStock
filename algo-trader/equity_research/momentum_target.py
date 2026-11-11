"""Target basket for the monthly/quarterly cross-sectional momentum book (paper research, no orders).

Rank the universe by 6-month momentum (close 21 sessions ago over close 126 sessions ago minus one: the
12-1 idea on a 6-month window), hold the top five with equal weight, rebalance quarterly.
Reads split-adjusted daily closes from the algo market store.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from runtime.paths import ROOT

LOOKBACK_SESSIONS = 126
SKIP_SESSIONS = 21
TOP_N = 5


def daily_closes(database: Path, symbols: list[str] | None = None) -> dict[str, list[tuple[date, float]]]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        query = ("select symbol, timestamp, close from market_bar where timeframe='1Day' and adjustment='split'")
        rows = connection.execute(query).fetchall()
    finally:
        connection.close()
    series: dict[str, list[tuple[date, float]]] = {}
    for symbol, stamp, close in rows:
        if symbols is not None and symbol not in symbols:
            continue
        series.setdefault(symbol, []).append((date.fromisoformat(stamp[:10]), float(close)))
    for values in series.values():
        values.sort()
    return series


def fill_recent_sessions(database: Path, series: dict[str, list[tuple[date, float]]], *, since: date) -> int:
    """Append daily closes for sessions after each name's last daily bar, from its 5-minute bars.

    A session's close is the last regular-hours 5-minute bar of that New York date. Returns the number of sessions
    added. Daily history is never rewritten.
    """
    from zoneinfo import ZoneInfo
    eastern = ZoneInfo("America/New_York")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "select symbol, timestamp, close from market_bar where timeframe='5Min' and adjustment='split' "
            "and timestamp >= ? order by symbol, timestamp",
            (since.isoformat(),),
        ).fetchall()
    finally:
        connection.close()
    last_close: dict[str, dict[date, float]] = {}
    for symbol, stamp, close in rows:
        if symbol not in series:
            continue
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(eastern)
        if not (9, 30) <= (moment.hour, moment.minute) < (16, 0) or moment.weekday() >= 5:
            continue
        last_close.setdefault(symbol, {})[moment.date()] = float(close)
    added = 0
    for symbol, sessions in last_close.items():
        existing = series[symbol][-1][0] if series[symbol] else None
        for day in sorted(sessions):
            if existing is None or day > existing:
                series[symbol].append((day, sessions[day]))
                added += 1
    return added


def target_basket(series: dict[str, list[tuple[date, float]]], *, top_n: int = TOP_N) -> list[dict]:
    scored = []
    for symbol, values in series.items():
        if len(values) <= LOOKBACK_SESSIONS:
            continue
        recent = values[-1 - SKIP_SESSIONS][1]
        earlier = values[-1 - LOOKBACK_SESSIONS][1]
        if earlier <= 0:
            continue
        scored.append((recent / earlier - 1, symbol, values[-1][0], values[-1][1]))
    scored.sort(reverse=True)
    return [
        {"symbol": symbol, "momentum_6m": round(score, 4), "as_of": as_of.isoformat(), "close": close,
         "weight": round(1 / top_n, 4)}
        for score, symbol, as_of, close in scored[:top_n]
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Momentum paper book: today's target basket (no orders)")
    parser.add_argument("--db", default=str(ROOT / "data" / "algo_market.sqlite3"))
    parser.add_argument("--output", default=str(ROOT / "data" / "research" / "momentum-paper" / "target.json"))
    args = parser.parse_args()
    series = daily_closes(Path(args.db))
    basket = target_basket(series)
    payload = {
        "schema": "momentum_paper_target_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "lookback_sessions": LOOKBACK_SESSIONS,
        "skip_sessions": SKIP_SESSIONS,
        "top_n": TOP_N,
        "rebalance": "quarterly",
        "universe_size": len(series),
        "basket": basket,
        "orders_placed": 0,
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload["basket"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
