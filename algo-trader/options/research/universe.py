"""Research-only universe used by options history collectors."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path


UNIVERSE_PATH = Path(__file__).with_name("universe.json")


def load_history_universe() -> dict:
    with UNIVERSE_PATH.open("r", encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict) or not isinstance(payload.get("categories"), dict):
        raise ValueError(f"invalid options history universe: {UNIVERSE_PATH}")
    symbols = history_underlyings(payload)
    if not symbols or any(not symbol.replace(".", "").isalnum() for symbol in symbols):
        raise ValueError(f"invalid ticker in options history universe: {UNIVERSE_PATH}")
    return payload


def history_underlyings(payload: dict | None = None) -> list[str]:
    """Return unique symbols in configured category order."""
    universe = payload if payload is not None else _read_universe()
    categories = universe.get("categories")
    if not isinstance(categories, dict):
        raise ValueError(f"invalid options history universe: {UNIVERSE_PATH}")
    symbols = []
    seen = set()
    for entries in categories.values():
        if not isinstance(entries, list):
            raise ValueError("options history universe categories must be ticker lists")
        for raw in entries:
            symbol = str(raw).strip().upper()
            if symbol and symbol not in seen:
                seen.add(symbol)
                symbols.append(symbol)
    return symbols


def _read_universe() -> dict:
    with UNIVERSE_PATH.open("r", encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict):
        raise ValueError(f"invalid options history universe: {UNIVERSE_PATH}")
    return payload


def history_start_date(symbol: str, default):
    """Return the configured earliest history date for a ticker, if present."""
    starts = load_history_universe().get("history_start_dates", {})
    value = starts.get(symbol.strip().upper()) if isinstance(starts, dict) else None
    return max(default, date.fromisoformat(value)) if value else default


def history_source_ranges(symbol: str, start_date: date, end_date: date) -> list[tuple[str, date, date]]:
    """Map a canonical ticker to its historical source tickers over a date range."""
    canonical = symbol.strip().upper()
    aliases = load_history_universe().get("history_ticker_aliases", {})
    entries = aliases.get(canonical, []) if isinstance(aliases, dict) else []
    if not isinstance(entries, list):
        raise ValueError(f"history ticker aliases for {canonical} must be a list")
    if any(not isinstance(entry, dict) for entry in entries):
        raise ValueError(f"invalid history ticker alias for {canonical}")

    cursor = start_date
    ranges = []
    for entry in sorted(entries, key=lambda row: str(row.get("through", ""))):
        source_symbol = str(entry.get("source_symbol", "")).strip().upper()
        through = date.fromisoformat(str(entry.get("through", "")))
        if not source_symbol:
            raise ValueError(f"history ticker alias for {canonical} has no source symbol")
        if through < cursor:
            continue
        source_end = min(through, end_date)
        ranges.append((source_symbol, cursor, source_end))
        cursor = source_end + timedelta(days=1)
        if cursor > end_date:
            break
    if cursor <= end_date:
        ranges.append((canonical, cursor, end_date))
    return ranges


__all__ = [
    "UNIVERSE_PATH", "history_source_ranges", "history_start_date",
    "history_underlyings", "load_history_universe",
]
