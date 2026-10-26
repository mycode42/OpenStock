"""Research-only universe used by options history collectors."""

from __future__ import annotations

import json
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
    from datetime import date

    value = starts.get(symbol.strip().upper()) if isinstance(starts, dict) else None
    return max(default, date.fromisoformat(value)) if value else default


__all__ = ["UNIVERSE_PATH", "history_start_date", "history_underlyings", "load_history_universe"]
