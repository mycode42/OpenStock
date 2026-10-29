"""Versioned artifact checks for the scheduled Algo research workflow."""

from __future__ import annotations

from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3


def stable_hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def research_data_version(
    database: str | Path, *, start: date, end: date,
    symbols: list[str], feed: str, adjustment: str,
) -> str:
    """Fingerprint ingestions wholly contained in the range used by the run.

    New bars after ``end`` do not invalidate a session cohort. Updates to an
    in-range request do. Broad legacy requests that include dates before the
    research warm-up range are ignored, so full-history ingestion metadata
    cannot influence current-run cache keys. Older stores without relevant
    ingestion metadata fall back to coverage bounded to the requested dates.
    """
    requested = {symbol.upper() for symbol in symbols} | {"SPY"}
    lower, upper = start.isoformat(), end.isoformat()
    with sqlite3.connect(database) as connection:
        try:
            rows = connection.execute(
                """SELECT id, timeframe, symbols, requested_start, requested_end,
                          rows_written, fetched_at
                   FROM ingestion
                   WHERE feed=? AND adjustment=?
                   ORDER BY id""",
                (feed, adjustment),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        relevant = []
        for _, timeframe, symbol_csv, requested_start, requested_end, rows_written, fetched_at in rows:
            ingest_symbols = {part.strip().upper() for part in str(symbol_csv).split(",") if part.strip()}
            if timeframe == "1Day":
                if "SPY" not in requested:
                    continue
            elif not ingest_symbols.intersection(requested):
                continue
            start_day = str(requested_start or "")[:10]
            end_day = str(requested_end or "")[:10]
            if not start_day or not end_day or start_day < lower or end_day > upper:
                continue
            relevant.append((
                timeframe, sorted(ingest_symbols), requested_start,
                requested_end, rows_written, fetched_at,
            ))
    if relevant:
        return stable_hash(relevant)

    # Pre-ingestion stores fingerprint only bars that can affect this research
    # window, including its explicit warm-up range.
    with sqlite3.connect(database) as connection:
        try:
            coverage = connection.execute(
                """SELECT feed, adjustment, timeframe, symbol, COUNT(*),
                          MIN(timestamp), MAX(timestamp)
                   FROM market_bar
                   WHERE feed=? AND adjustment=? AND timestamp>=? AND timestamp<?
                   GROUP BY feed, adjustment, timeframe, symbol
                   ORDER BY timeframe, symbol""",
                (feed, adjustment, lower, (end + timedelta(days=1)).isoformat()),
            ).fetchall()
        except sqlite3.OperationalError:
            coverage = []
    selected = [row for row in coverage if row[3].upper() in requested]
    if not selected:
        raise ValueError(f"No versionable {feed}/{adjustment} market data found in {database}")
    return stable_hash(selected)


def read_json(path: str | Path) -> tuple[dict | None, bytes | None]:
    source = Path(path)
    try:
        raw = source.read_bytes()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None, None
    return (value, raw) if isinstance(value, dict) else (None, raw)
