#!/usr/bin/env python3
"""Export real intraday session shapes for the OpenStock demo.

Samples complete 5-minute extended-hours sessions (04:00-20:00 ET) for random tickers and random recent days from
data/algo_market.sqlite3, and writes each one as basis points from the session's first open. The demo replays these
shapes at its own prices, so its charts move like real sessions. Output: ../server/demo-sessions.json.

  .venv/bin/python scripts/market/export_demo_sessions.py
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
import json
from pathlib import Path
import random
import sqlite3
import sys
from zoneinfo import ZoneInfo

from runtime.paths import ROOT

MARKET_DB = ROOT / "data" / "algo_market.sqlite3"
OUTPUT = ROOT.parent / "server" / "demo-sessions.json"
ET = ZoneInfo("America/New_York")
BARS_PER_SESSION = 192  # 04:00 to 20:00 ET in 5-minute bars
SESSIONS = 30
SEED = 20261030
LOOKBACK_DAYS = 150  # the extended feed starts on 2026-04-22
MAX_ATTEMPTS = 400


def _session_bars(connection: sqlite3.Connection, symbol: str, day: datetime) -> list[tuple[int, list[float]]]:
    """A full 04:00-20:00 ET session: the regular feed for the regular session, the extended feed around it."""
    start = datetime.combine(day.date(), time(4, 0), tzinfo=ET).astimezone(timezone.utc)
    end = start + timedelta(hours=16)
    rows = []
    for feed in ("sip", "sip_extended"):
        rows.extend(connection.execute(
            """SELECT feed, timestamp, open, high, low, close FROM market_bar
               WHERE feed=? AND timeframe='5Min' AND symbol=? AND timestamp>=? AND timestamp<?
               ORDER BY timestamp""",
            (feed, symbol, start.strftime("%Y-%m-%dT%H:%M:%S"), end.strftime("%Y-%m-%dT%H:%M:%S")),
        ).fetchall())
    by_index: dict[int, tuple[str, list[float]]] = {}
    for feed, stamp, open_, high, low, close in rows:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(ET)
        index = (moment.hour * 60 + moment.minute - 240) // 5
        if not (0 <= index < BARS_PER_SESSION and moment.date() == day.date()):
            continue
        # The regular feed is the complete source for 09:30-16:00; the extended feed only covers the rest.
        if index not in by_index or feed == "sip":
            by_index[index] = (feed, [open_, high, low, close])
    return [(index, values) for index, (_, values) in sorted(by_index.items())]


def main() -> int:
    rng = random.Random(SEED)
    connection = sqlite3.connect(f"file:{MARKET_DB}?mode=ro", uri=True)
    symbols = [row[0] for row in connection.execute(
        "SELECT DISTINCT symbol FROM market_bar WHERE feed='sip_extended' AND timeframe='5Min'")]
    today = datetime.now(ET).date()
    sessions = []
    attempts = 0
    # Each attempt reads one symbol-day with indexed lookups. A day that is not complete is simply skipped.
    while len(sessions) < SESSIONS and attempts < MAX_ATTEMPTS:
        attempts += 1
        symbol = rng.choice(symbols)
        day = datetime.combine(today - timedelta(days=rng.randint(1, LOOKBACK_DAYS)), time(0), tzinfo=ET)
        if day.weekday() >= 5:
            continue
        bars = _session_bars(connection, symbol, day)
        if len(bars) < BARS_PER_SESSION - 2:
            continue
        series = {}
        for index, values in bars:
            series[index] = values
        first_open = series[min(series)][0]
        grid = []
        last_close = first_open
        for index in range(BARS_PER_SESSION):
            values = series.get(index, [last_close, last_close, last_close, last_close])
            last_close = values[3]
            grid.append([round((value / first_open - 1) * 10000) for value in values])
        sessions.append({"symbol": symbol, "date": day.date().isoformat(), "bars_bps": grid})
        print(f"session {len(sessions)}: {symbol} {day.date()} after {attempts} attempts", flush=True)
    connection.close()
    if len(sessions) < SESSIONS:
        print(f"only {len(sessions)} complete sessions found", file=sys.stderr)
        return 1
    OUTPUT.write_text(json.dumps({
        "source": "algo_market.sqlite3, 5Min sip_extended, 04:00-20:00 ET",
        "bar_minutes": 5,
        "fields": ["open", "high", "low", "close"],
        "sessions": sessions,
    }, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"wrote {len(sessions)} sessions to {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
