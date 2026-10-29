"""Software-managed protection rows: the persisted stop, target, and trailing state.

Broker-side bracket, OCO, and stop legs are no longer used. Each open Algo
position has one row here. The strategy scan enforces the levels every scan
while quotes are fresh; this module persists them, prices session-specific
exits, guards exits against duplicates, and reports a stale daemon heartbeat.
"""

from __future__ import annotations

import math
from datetime import datetime
from decimal import Decimal, InvalidOperation

from portfolio.config import get_algo_execution_settings


PROTECTION_CONFIG = {
    # Limit exits in extended sessions cross the book by this fraction:
    # sells are priced below the bid, covers above the ask.
    "extended_aggression_pct": 0.005,
    "overnight_aggression_pct": 0.01,
    "extended_time_in_force": "day",
    "overnight_time_in_force": "day",
    # Alarm when open positions exist and the last scan heartbeat is older
    # than this many scan intervals.
    "heartbeat_stale_scans": 3,
}

PROTECTION_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS position_protection (
    ticker TEXT PRIMARY KEY,
    strategy TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('long', 'short')),
    quantity TEXT NOT NULL,
    entry_price TEXT NOT NULL,
    entry_order_id TEXT,
    initial_stop TEXT NOT NULL,
    target TEXT NOT NULL,
    hard_stop_pct TEXT,
    trailing_stop_pct TEXT,
    trailing_activation_pct TEXT,
    trailing_stop TEXT,
    best_price TEXT,
    phase TEXT NOT NULL CHECK (phase IN ('open', 'exit_pending')),
    last_evaluated_at TEXT,
    exit_client_order_id TEXT,
    exit_order_id TEXT,
    exit_submitted_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daemon_heartbeat (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    scanned_at TEXT NOT NULL,
    session TEXT,
    pid INTEGER
);
"""


def ensure_protection_schema(connection) -> None:
    """Create the protection tables in an existing database if absent."""
    connection.executescript(PROTECTION_TABLES_SQL)


def exit_order_policy(session_name: str | None) -> dict:
    """Order type, TIF, and extended-hours flag that a session accepts for exits."""
    if session_name in (None, "regular"):
        return {"order_type": "market", "time_in_force": "day", "extended_hours": False}
    time_in_force = (
        PROTECTION_CONFIG["overnight_time_in_force"] if session_name == "overnight"
        else PROTECTION_CONFIG["extended_time_in_force"]
    )
    return {"order_type": "limit", "time_in_force": time_in_force, "extended_hours": True}


def exit_limit_price(action: str, bid: object, ask: object, session_name: str) -> float:
    """Price an extended-session exit beyond the closing side of the quote.

    A sell is priced below the bid and a cover above the ask, so a marketable
    limit crosses the book. Raises ValueError for unusable quotes.
    """
    band = (
        PROTECTION_CONFIG["overnight_aggression_pct"] if session_name == "overnight"
        else PROTECTION_CONFIG["extended_aggression_pct"]
    )
    try:
        reference = float(bid if action == "sell" else ask)
    except (TypeError, ValueError) as exc:
        raise ValueError("exit quote is unavailable") from exc
    if not math.isfinite(reference) or reference <= 0:
        raise ValueError("exit quote is unusable")
    price = reference * (1 - band) if action == "sell" else reference * (1 + band)
    rounded = round(price, 2)
    if not math.isfinite(rounded) or rounded <= 0:
        raise ValueError("exit limit price is not positive")
    return rounded


def protection_row_from_state(
    ticker: str,
    state: dict,
    *,
    strategy: str,
    broker_quantity: object,
    broker_entry_price: object,
    risk: dict,
    now: datetime,
) -> dict | None:
    """Build the protection row for an open position from its strategy state.

    Returns None when the state lacks a stop or target, so no unprotected
    position is recorded as protected.
    """
    try:
        quantity = Decimal(str(broker_quantity))
        entry = Decimal(str(broker_entry_price))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not quantity.is_finite() or quantity == 0 or not entry.is_finite() or entry <= 0:
        return None
    stop = _positive(state.get("stop_price"))
    target = _positive(state.get("take_profit"))
    if stop is None or target is None:
        return None
    side = "short" if quantity < 0 else "long"
    if (side == "long" and not stop < target) or (side == "short" and not target < stop):
        return None
    best_key = "trailing_trough_price" if side == "short" else "trailing_peak_price"
    settings = {
        "hard_stop_pct": (risk.get("stop_loss_pct_by_strategy") or {}).get(strategy),
        "trailing_stop_pct": (risk.get("trailing_stop_pct_by_strategy") or {}).get(strategy),
        "trailing_activation_pct": (risk.get("trailing_activation_pct_by_strategy") or {}).get(strategy),
    }
    return {
        "ticker": ticker,
        "strategy": strategy,
        "side": side,
        "quantity": str(abs(quantity)),
        "entry_price": str(entry),
        "entry_order_id": state.get("entry_order_id"),
        "initial_stop": str(stop),
        "target": str(target),
        "hard_stop_pct": _text_or_none(settings["hard_stop_pct"]),
        "trailing_stop_pct": _text_or_none(settings["trailing_stop_pct"]),
        "trailing_activation_pct": _text_or_none(settings["trailing_activation_pct"]),
        "trailing_stop": str(stop),
        "best_price": _text_or_none(state.get(best_key)),
        "phase": "exit_pending" if state.get("phase") == "exit_pending" else "open",
        "last_evaluated_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }


def build_protection_rows(
    portfolio: dict,
    positions: list[dict],
    *,
    risk: dict,
    now: datetime,
) -> tuple[list[dict], list[str]]:
    """Protection rows for every owned, held position, and the owned tickers lacking levels."""
    held = {str(position.get("symbol", "")).upper(): position for position in positions}
    owners = portfolio.get("position_owners") or {}
    states = portfolio.get("strategy_states") or {}
    rows: list[dict] = []
    gaps: list[str] = []
    for ticker, strategy in sorted(owners.items()):
        position = held.get(str(ticker).upper())
        if position is None:
            continue
        state = (states.get(strategy) or {}).get(ticker) or {}
        row = protection_row_from_state(
            ticker,
            state,
            strategy=strategy,
            broker_quantity=position.get("qty"),
            broker_entry_price=position.get("avg_entry_price"),
            risk=risk,
            now=now,
        )
        if row is None:
            gaps.append(ticker)
        else:
            rows.append(row)
    return rows, gaps


def heartbeat_alarm(
    *,
    heartbeat: dict | None,
    open_position_count: int,
    now: datetime,
    scan_interval_seconds: int | None = None,
    stale_scans: int | None = None,
) -> dict | None:
    """Return an alarm dict when open positions exist and the daemon is stale."""
    if open_position_count <= 0:
        return None
    interval = int(scan_interval_seconds or get_algo_execution_settings()["portfolio_scan_seconds"])
    limit = int(stale_scans or PROTECTION_CONFIG["heartbeat_stale_scans"]) * interval
    if not heartbeat or not heartbeat.get("scanned_at"):
        return {"kind": "daemon_heartbeat_missing", "open_positions": open_position_count, "age_seconds": None, "limit_seconds": limit}
    try:
        scanned = datetime.fromisoformat(str(heartbeat["scanned_at"]))
    except ValueError:
        return {"kind": "daemon_heartbeat_invalid", "open_positions": open_position_count, "age_seconds": None, "limit_seconds": limit}
    if scanned.tzinfo is None or now.tzinfo is None:
        return {"kind": "daemon_heartbeat_invalid", "open_positions": open_position_count, "age_seconds": None, "limit_seconds": limit}
    age = (now - scanned).total_seconds()
    if age <= limit:
        return None
    return {"kind": "daemon_heartbeat_stale", "open_positions": open_position_count, "age_seconds": round(age), "limit_seconds": limit}


def _positive(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _text_or_none(value: object) -> str | None:
    return None if value is None else str(value)


__all__ = [
    "PROTECTION_CONFIG",
    "build_protection_rows",
    "ensure_protection_schema",
    "exit_limit_price",
    "exit_order_policy",
    "heartbeat_alarm",
    "protection_row_from_state",
]
