"""Feed-aware local store for Alpaca historical bars used by Algo research."""

from __future__ import annotations

import math
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

from src.accounts import get_account_for_mode


DATA_BASE = "https://data.alpaca.markets/v2"
DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "algo_market.sqlite3"


def _utc_timestamp(value: object) -> str:
    timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _finite_number(value: object, field: str, *, minimum: float | None = None) -> float:
    number = float(value)
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        raise ValueError(f"Invalid Alpaca bar field {field}")
    return number


class SQLiteBarStore:
    """Append/replace bars by feed, symbol, interval, adjustment, and time."""

    def __init__(self, path: str | Path = DEFAULT_DB) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS market_bar (
                    feed TEXT NOT NULL,
                    adjustment TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    trade_count REAL,
                    vwap REAL,
                    ingested_at TEXT NOT NULL,
                    PRIMARY KEY (feed, adjustment, timeframe, symbol, timestamp)
                );
                CREATE INDEX IF NOT EXISTS market_bar_lookup
                    ON market_bar (feed, adjustment, timeframe, symbol, timestamp);
                CREATE TABLE IF NOT EXISTS ingestion (
                    id INTEGER PRIMARY KEY,
                    provider TEXT NOT NULL,
                    data_tier TEXT NOT NULL,
                    feed TEXT NOT NULL,
                    adjustment TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    symbols TEXT NOT NULL,
                    requested_start TEXT NOT NULL,
                    requested_end TEXT NOT NULL,
                    rows_written INTEGER NOT NULL,
                    fetched_at TEXT NOT NULL
                );
            """)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(market_bar)")}
            if "ingested_at" not in columns:
                connection.execute("ALTER TABLE market_bar ADD COLUMN ingested_at TEXT NOT NULL DEFAULT ''")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=20)
        connection.execute("PRAGMA busy_timeout=20000")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def write_bars(
        self,
        bars_by_symbol: dict[str, list[dict]],
        *,
        feed: str,
        timeframe: str,
        adjustment: str = "raw",
        data_tier: str = "algo_trader_plus",
        requested_start: str = "",
        requested_end: str = "",
    ) -> int:
        """Upsert a response atomically; a different feed remains a separate dataset."""
        now = datetime.now(timezone.utc).isoformat()
        rows = []
        for symbol, bars in bars_by_symbol.items():
            ticker = str(symbol).upper().strip()
            if not ticker:
                raise ValueError("Alpaca bar response contains an empty symbol")
            for bar in bars:
                timestamp = _utc_timestamp(bar["t"])
                opening = _finite_number(bar["o"], "open", minimum=0)
                high = _finite_number(bar["h"], "high", minimum=0)
                low = _finite_number(bar["l"], "low", minimum=0)
                close = _finite_number(bar["c"], "close", minimum=0)
                volume = _finite_number(bar.get("v", 0), "volume", minimum=0)
                if min(opening, high, low, close) <= 0 or high < max(opening, close, low) or low > min(opening, close, high):
                    raise ValueError(f"Invalid OHLC values in {ticker} bar at {timestamp}")
                count = bar.get("n")
                vwap = bar.get("vw")
                rows.append((
                    feed, adjustment, timeframe, ticker, timestamp,
                    opening, high, low, close, volume,
                    None if count is None else _finite_number(count, "trade count", minimum=0),
                    None if vwap is None else _finite_number(vwap, "vwap", minimum=0),
                    now,
                ))
        with closing(self._connect()) as connection, connection:
            connection.executemany("""
                INSERT INTO market_bar (
                    feed, adjustment, timeframe, symbol, timestamp,
                    open, high, low, close, volume, trade_count, vwap, ingested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(feed, adjustment, timeframe, symbol, timestamp) DO UPDATE SET
                    open=excluded.open, high=excluded.high, low=excluded.low,
                    close=excluded.close, volume=excluded.volume,
                    trade_count=excluded.trade_count, vwap=excluded.vwap,
                    ingested_at=excluded.ingested_at
            """, rows)
            connection.execute("""
                INSERT INTO ingestion (
                    provider, data_tier, feed, adjustment, timeframe, symbols,
                    requested_start, requested_end, rows_written, fetched_at
                ) VALUES ('alpaca', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data_tier, feed, adjustment, timeframe,
                ",".join(sorted(str(symbol).upper() for symbol in bars_by_symbol)),
                requested_start, requested_end, len(rows), now,
            ))
        return len(rows)

    def bars(
        self,
        symbols: list[str],
        *,
        feed: str,
        timeframe: str,
        start: str,
        end: str,
        adjustment: str = "raw",
    ) -> dict[str, list[dict]]:
        if not symbols:
            return {}
        # Date strings sort before the full UTC timestamps stored in the DB.
        with closing(self._connect()) as connection:
            rows = connection.execute("""
                SELECT symbol, timestamp, open, high, low, close, volume, trade_count, vwap, ingested_at
                FROM market_bar
                WHERE feed=? AND adjustment=? AND timeframe=?
                    AND symbol IN (%s) AND timestamp >= ? AND timestamp <= ?
                ORDER BY symbol, timestamp
            """ % ",".join("?" for _ in symbols), (
                feed, adjustment, timeframe, *(symbol.upper() for symbol in symbols),
                f"{start}T00:00:00.000000Z", f"{end}T23:59:59.999999Z",
            )).fetchall()
        output: dict[str, list[dict]] = {symbol.upper(): [] for symbol in symbols}
        for symbol, timestamp, opening, high, low, close, volume, count, vwap, ingested_at in rows:
            bar = {"t": timestamp, "o": opening, "h": high, "l": low, "c": close, "v": volume}
            bar["_ingested_at"] = ingested_at
            if count is not None:
                bar["n"] = count
            if vwap is not None:
                bar["vw"] = vwap
            output.setdefault(symbol, []).append(bar)
        return output

    def coverage(self) -> list[dict]:
        with closing(self._connect()) as connection:
            rows = connection.execute("""
                SELECT feed, adjustment, timeframe, symbol, COUNT(*), MIN(timestamp), MAX(timestamp)
                FROM market_bar GROUP BY feed, adjustment, timeframe, symbol
                ORDER BY feed, timeframe, symbol
            """).fetchall()
        return [
            {"feed": feed, "adjustment": adjustment, "timeframe": timeframe,
             "symbol": symbol, "rows": count, "first": first, "last": last}
            for feed, adjustment, timeframe, symbol, count, first, last in rows
        ]


def fetch_alpaca_bars(
    symbols: list[str],
    *,
    timeframe: str,
    start: str,
    end: str,
    feed: str,
    adjustment: str = "raw",
    mode: str = "algo",
) -> dict[str, list[dict]]:
    """Fetch paginated historical bars with the dedicated Algo market-data key."""
    if not symbols:
        return {}
    account = get_account_for_mode(mode)
    output: dict[str, list[dict]] = {}
    for offset in range(0, len(symbols), 50):
        chunk = [symbol.upper() for symbol in symbols[offset:offset + 50]]
        token = None
        seen_tokens: set[str] = set()
        while True:
            params = {
                "symbols": ",".join(chunk), "timeframe": timeframe,
                "start": start, "end": end, "limit": 10000,
                "sort": "asc", "feed": feed, "adjustment": adjustment,
            }
            if token:
                params["page_token"] = token
            response = None
            for attempt in range(3):
                try:
                    response = requests.get(
                        f"{DATA_BASE}/stocks/bars", headers=account.headers,
                        params=params, timeout=30,
                    )
                    response.raise_for_status()
                    break
                except requests.HTTPError as exc:
                    status = exc.response.status_code if exc.response is not None else 0
                    if status and status < 500 and status != 429:
                        raise
                    if attempt == 2:
                        raise
                    time.sleep(0.5 * (2 ** attempt))
                except requests.RequestException:
                    if attempt == 2:
                        raise
                    time.sleep(0.5 * (2 ** attempt))
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("bars", {}), dict):
                raise ValueError("Alpaca historical bars response is malformed")
            for symbol, bars in (payload.get("bars") or {}).items():
                if isinstance(bars, list):
                    output.setdefault(symbol.upper(), []).extend(bars)
            token = payload.get("next_page_token")
            if not token:
                break
            if not isinstance(token, str) or token in seen_tokens:
                raise ValueError("Alpaca historical bars pagination token repeated or invalid")
            seen_tokens.add(token)
    return output


__all__ = ["DEFAULT_DB", "SQLiteBarStore", "fetch_alpaca_bars"]
