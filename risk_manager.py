#!/usr/bin/env python3
"""
Risk Manager — Hard rules enforcer for Swarm Trader.

Every proposed trade passes through validate_trade() before execution.
Rules are non-negotiable and code-enforced; no LLM can override them.

Usage (standalone check):
  poetry run python risk_manager.py --status
  poetry run python risk_manager.py --status --mode day
  poetry run python risk_manager.py --status --mode algo

Importable:
  from risk_manager import validate_trade, get_portfolio_state
"""

import logging
import math
import os
import sys
import json
import hashlib
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(override=True)

import requests

from src.config import get_mode_config
from src.day_recovery import day_recovery_confirmed
from src.market_calendar import get_day_schedule

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [risk_manager] %(levelname)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("risk_manager")

from src.accounts import get_account_for_mode, verify_account_for_mode

API_BASE = "https://paper-api.alpaca.markets/v2"
MARKET_TZ = ZoneInfo("America/New_York")

# Leveraged ETFs — blocked in swing mode, allowed in day mode
LEVERAGED_ETFS = {
    "TQQQ", "SOXL", "UPRO", "SPXL", "TECL", "LABU", "SOXS", "TECS",
    "SPXS", "SQQQ", "SDOW", "UDOW", "NAIL", "CURE", "HIBL", "HIBS",
}

# Moonshots — hard-blocked in ALL modes (not in any V2 universe)
MOONSHOTS = {"RGTI", "SOUN", "LUNR"}

# Per-mode universe maps: cached after first build to avoid repeated recomputation
_universe_cache: dict[str, tuple] = {}


def _get_universe_maps(mode: str) -> tuple[dict, dict, set, dict]:
    """
    Build and cache ticker→sector, ticker→max_pct, tactical_tickers, and universe
    for the given mode.

    Returns: (ticker_sector, ticker_max_pct, tactical_tickers, universe)
    """
    if mode not in _universe_cache:
        mode_config = get_mode_config(mode)
        universe = mode_config["universe"]
        default_max_pct = mode_config["risk"]["max_position_pct"]

        ticker_sector: dict[str, str] = {}
        ticker_max_pct: dict[str, float] = {}
        for sector_key, sector_data in universe.items():
            per_stock = sector_data.get("max_per_stock_pct", default_max_pct)
            for t in sector_data.get("tickers", []):
                ticker_sector[t] = sector_key
                ticker_max_pct[t] = per_stock

        # "tactical" sector is the high-risk bucket that gets a total-exposure cap
        tactical_tickers = set(universe.get("tactical", {}).get("tickers", []))

        _universe_cache[mode] = (ticker_sector, ticker_max_pct, tactical_tickers, universe)

    return _universe_cache[mode]


@dataclass
class RejectionReason:
    """Structured rejection with rule name and human-readable message."""
    rule: str
    message: str


@dataclass
class ValidationResult:
    """Result of validate_trade()."""
    approved: bool
    reason: str
    rule: Optional[str] = None  # which rule fired on rejection


def _api(endpoint: str, mode: str = None) -> dict | list:
    headers = get_account_for_mode(mode).headers
    r = requests.get(f"{API_BASE}/{endpoint}", headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()


def _is_market_session_date(timestamp_value: str, session_date: str) -> bool:
    """Compare an Alpaca timestamp to a trading day in New York time."""
    try:
        timestamp = datetime.fromisoformat(str(timestamp_value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(MARKET_TZ).date().isoformat() == session_date


def _algo_session_bounds(session_date: str) -> tuple[datetime, datetime]:
    """The Algo trade date runs from the prior 20:00 ET to 20:00 ET."""
    trade_day = date.fromisoformat(session_date)
    start_day = trade_day - timedelta(days=1)
    return (
        datetime(start_day.year, start_day.month, start_day.day, 20, tzinfo=MARKET_TZ),
        datetime(trade_day.year, trade_day.month, trade_day.day, 20, tzinfo=MARKET_TZ),
    )


def _is_in_algo_session(timestamp_value: str, session_date: str) -> bool:
    try:
        timestamp = datetime.fromisoformat(str(timestamp_value).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        start, end = _algo_session_bounds(session_date)
        return start <= timestamp.astimezone(MARKET_TZ) < end
    except (TypeError, ValueError):
        return False


def _get_algo_orders_since(session_start: str, mode: str) -> list[dict]:
    """Fetch all mode orders since the ET session start using broker cursors."""
    orders: list[dict] = []
    after_order_id = None
    for _ in range(100):
        if after_order_id:
            endpoint = (
                "orders?status=all&limit=500&direction=asc&after_order_id="
                f"{quote(after_order_id, safe='')}"
            )
        else:
            endpoint = f"orders?status=all&after={session_start}&limit=500&direction=asc"
        page = _api(endpoint, mode=mode)
        if not isinstance(page, list):
            raise ValueError("Algo order response was not a list")
        orders.extend(item for item in page if isinstance(item, dict))
        if len(page) < 500:
            return orders
        after_order_id = str(page[-1].get("id") or "")
        if not after_order_id:
            raise ValueError("Algo order pagination omitted its last order ID")
    raise RuntimeError("Algo order pagination exceeded 100 pages")


def _count_algo_new_entries_today(
    orders: list[dict],
    fills: list[dict],
    positions: dict[str, dict],
    session_date: str,
) -> int:
    """Count today's filled or pending long and short openings using signed qty."""
    fills_by_order: dict[str, float] = {}
    today_fills: list[tuple[str, datetime, str, float, str]] = []
    filled_delta: dict[str, float] = {}

    for fill in fills:
        ticker = str(fill.get("symbol", "")).upper()
        side = str(fill.get("side", "")).lower()
        if not ticker or side not in {"buy", "sell"}:
            continue
        raw_timestamp = fill.get("transaction_time") or fill.get("date")
        if not raw_timestamp:
            continue
        try:
            timestamp = datetime.fromisoformat(str(raw_timestamp).replace("Z", "+00:00"))
            qty = max(0.0, float(fill.get("qty", fill.get("quantity", 0)) or 0))
        except (TypeError, ValueError):
            continue
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        if not _is_in_algo_session(raw_timestamp, session_date):
            continue
        if qty <= 0:
            continue
        order_id = str(fill.get("order_id") or fill.get("id") or "")
        today_fills.append((ticker, timestamp, side, qty, order_id))
        if order_id:
            fills_by_order[order_id] = fills_by_order.get(order_id, 0.0) + qty
        signed_qty = qty if side == "buy" else -qty
        filled_delta[ticker] = filled_delta.get(ticker, 0.0) + signed_qty

    today_order_ids: set[str] = set()
    events_by_ticker: dict[str, list[tuple[datetime, str, float, bool, str]]] = {}

    for order in orders:
        ticker = str(order.get("symbol", "")).upper()
        side = str(order.get("side", "")).lower()
        submitted_at = order.get("submitted_at") or order.get("created_at")
        if not ticker or side not in {"buy", "sell"} or not submitted_at:
            continue
        try:
            timestamp = datetime.fromisoformat(str(submitted_at).replace("Z", "+00:00"))
            order_qty = max(0.0, float(order.get("qty", 0) or 0))
            reported_filled = max(0.0, float(order.get("filled_qty", 0) or 0))
        except (TypeError, ValueError):
            continue
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        if not _is_in_algo_session(submitted_at, session_date):
            continue

        order_id = str(order.get("id") or "")
        status = str(order.get("status", "")).lower()
        pending = status not in {
            "canceled", "expired", "rejected", "filled", "done_for_day",
            "replaced", "stopped", "suspended", "calculated",
        }
        filled_qty = max(fills_by_order.get(order_id, 0.0), reported_filled)
        if filled_qty <= 0 and not pending:
            continue
        missing_fill_qty = max(0.0, reported_filled - fills_by_order.get(order_id, 0.0))
        if missing_fill_qty:
            delta = missing_fill_qty if side == "buy" else -missing_fill_qty
            filled_delta[ticker] = filled_delta.get(ticker, 0.0) + delta
        projected_qty = max(filled_qty, order_qty if pending else filled_qty)
        events_by_ticker.setdefault(ticker, []).append((timestamp, side, projected_qty, True, order_id))
        if order_id:
            today_order_ids.add(order_id)

    # Prior-session orders (including protective bracket legs) can fill today.
    for ticker, timestamp, side, qty, order_id in today_fills:
        if order_id and order_id in today_order_ids:
            continue
        events_by_ticker.setdefault(ticker, []).append((timestamp, side, qty, False, order_id))

    entries = 0
    for ticker, events in events_by_ticker.items():
        current_qty = float((positions.get(ticker) or {}).get("qty", 0) or 0)
        running_qty = current_qty - filled_delta.get(ticker, 0.0)
        for _, side, qty, countable, order_id in sorted(events, key=lambda event: event[0]):
            if countable and abs(running_qty) < 1e-9:
                entries += 1
            running_qty += qty if side == "buy" else -qty
    return entries


def _pending_short_exposure(open_orders: list[dict], positions: dict[str, dict]) -> dict[str, float]:
    """Reserve remaining opening sell-short orders by ticker."""
    exposure: dict[str, float] = {}
    terminal = {"canceled", "expired", "rejected", "filled", "done_for_day", "replaced", "stopped"}
    for order in open_orders:
        if str(order.get("side", "")).lower() != "sell":
            continue
        symbol = str(order.get("symbol") or "").upper()
        if not symbol:
            raise ValueError("Open sell order has no symbol")
        intent = str(order.get("position_intent") or "").lower()
        position_qty = float((positions.get(symbol) or {}).get("qty", 0) or 0)
        if intent == "sell_to_close" or (not intent and position_qty > 0):
            continue
        if str(order.get("status", "")).lower() in terminal:
            continue
        quantity = max(0.0, float(order.get("qty") or 0) - float(order.get("filled_qty") or 0))
        if quantity <= 0:
            continue
        prices = [
            float(value) for value in (
                order.get("limit_price"), order.get("filled_avg_price"),
                (positions.get(symbol) or {}).get("current_price"),
            ) if value not in (None, "") and float(value) > 0
        ]
        if not prices:
            raise ValueError(f"Open short order for {symbol} has no usable price")
        exposure[symbol] = exposure.get(symbol, 0.0) + quantity * max(prices) * 1.02
    return exposure


def _easy_to_borrow_symbols(mode: str) -> set[str]:
    """Read Alpaca's current easy-to-borrow symbols; unknown/HTB names fail closed."""
    assets = _api("assets?status=active&asset_class=us_equity", mode=mode)
    if not isinstance(assets, list):
        raise ValueError("Alpaca asset response was not a list")
    return {
        str(asset.get("symbol", "")).upper()
        for asset in assets
        if isinstance(asset, dict)
        and asset.get("symbol")
        and asset.get("tradable") is True
        and asset.get("shortable") is True
        and str(asset.get("borrow_status", "")).lower() == "easy_to_borrow"
    }


def _get_algo_fill_activities(session_start: str, mode: str) -> list[dict]:
    """Fetch every Algo fill activity since the current New York session began."""
    activities: list[dict] = []
    page_token = None
    for _ in range(100):
        endpoint = (
            f"account/activities/FILL?after={session_start}"
            "&direction=asc&page_size=100"
        )
        if page_token:
            endpoint += f"&page_token={quote(page_token, safe='')}"
        page = _api(endpoint, mode=mode)
        if not isinstance(page, list):
            raise ValueError("Algo fill activity response was not a list")
        activities.extend(item for item in page if isinstance(item, dict))
        if len(page) < 100:
            return activities
        page_token = str(page[-1].get("id") or "")
        if not page_token:
            raise ValueError("Algo fill activity pagination omitted its next page ID")
    raise RuntimeError("Algo fill activity pagination exceeded 100 pages")


def _get_open_orders(mode: str) -> list[dict]:
    """Read all open orders, including orders submitted on an earlier date."""
    orders: list[dict] = []
    after_order_id = None
    for _ in range(100):
        endpoint = "orders?status=open&limit=500&direction=asc"
        if after_order_id:
            endpoint += f"&after_order_id={quote(after_order_id, safe='')}"
        page = _api(endpoint, mode=mode)
        if not isinstance(page, list):
            raise ValueError("Open order response was not a list")
        orders.extend(item for item in page if isinstance(item, dict))
        if len(page) < 500:
            return orders
        after_order_id = str(page[-1].get("id") or "")
        if not after_order_id:
            raise ValueError("Open order pagination omitted its next page ID")
    raise RuntimeError("Open order pagination exceeded 100 pages")


def _pending_buy_exposure(open_orders: list[dict], positions: dict[str, dict]) -> dict[str, float]:
    """Reserve the still-unfilled value of every open buy order.

    For a market buy without a usable broker price, the amount is unknown; the
    caller must block new entries rather than assume that the order costs zero.
    """
    exposure: dict[str, float] = {}
    for order in open_orders:
        if str(order.get("side", "")).lower() != "buy":
            continue
        symbol = str(order.get("symbol") or "").upper()
        if not symbol:
            raise ValueError("Open buy order has no symbol")
        filled_qty = max(0.0, float(order.get("filled_qty") or 0))
        raw_qty = order.get("qty")
        if raw_qty not in (None, ""):
            remaining_qty = max(0.0, float(raw_qty) - filled_qty)
            if remaining_qty == 0:
                continue
            prices = [
                float(value)
                for value in (
                    order.get("limit_price"), order.get("stop_price"),
                    order.get("filled_avg_price"),
                    (positions.get(symbol) or {}).get("current_price"),
                )
                if value not in (None, "") and float(value) > 0
            ]
            if not prices:
                raise ValueError(f"Open market buy for {symbol} has no usable price")
            value = remaining_qty * max(prices) * 1.02
        else:
            notional = float(order.get("notional") or 0)
            if notional <= 0:
                raise ValueError(f"Open buy for {symbol} has neither quantity nor notional")
            fill_price = float(order.get("filled_avg_price") or 0)
            value = max(0.0, notional - filled_qty * fill_price) * 1.02
        if not math.isfinite(value):
            raise ValueError(f"Open buy for {symbol} has nonfinite exposure")
        exposure[symbol] = exposure.get(symbol, 0.0) + value
    return exposure


def get_portfolio_state(mode: str = None) -> dict:
    """
    Fetch live portfolio state from Alpaca.

    Args:
        mode: "swing", "day", or the dedicated "algo" account route.
              Reads TRADING_MODE env if not specified.

    Returns a dict with:
      equity and its daily/weekly dollar baselines and P&L, cash, cash_pct,
      daily_pnl_pct, weekly_pnl_pct,
      positions (symbol → position dict),
      sector_alloc (sector → $ value),
      trade_count_today (all nonterminal buy orders),
      entry_count_today (new-position buys for Algo; legacy buy count otherwise),
      open_position_count.
    """
    if mode is None:
        mode = os.environ.get("TRADING_MODE", "swing")
    ticker_sector, _, _, _ = _get_universe_maps(mode)

    # A risk snapshot must belong to the configured, active paper account.
    # The executor verifies too, but standalone status and strategy callers
    # must not evaluate a closed or replaced account.
    _, account = verify_account_for_mode(mode)
    positions_raw = _api("positions", mode=mode)

    equity = float(account.get("equity", 0))
    cash = float(account.get("cash", 0))
    last_equity = float(account.get("last_equity", equity))

    daily_pnl_pct = ((equity - last_equity) / last_equity) if last_equity else 0.0
    daily_baseline_equity = (
        last_equity if math.isfinite(last_equity) and last_equity > 0 else None
    )
    daily_pnl_dollars = (
        equity - daily_baseline_equity if daily_baseline_equity is not None else None
    )
    cash_pct = (cash / equity) if equity else 0.0

    positions: dict[str, dict] = {}
    sector_alloc: dict[str, float] = {}

    for p in positions_raw:
        sym = p["symbol"]
        market_value = float(p.get("market_value", 0))
        positions[sym] = {
            "symbol": sym,
            "qty": float(p.get("qty", 0)),
            "market_value": market_value,
            "current_price": float(p.get("current_price", 0)),
            "avg_entry_price": float(p.get("avg_entry_price", 0)),
            "unrealized_plpc": float(p.get("unrealized_plpc", 0)),
            "pct_of_equity": (market_value / equity) if equity else 0,
        }
        sector = ticker_sector.get(sym, "other")
        sector_alloc[sector] = sector_alloc.get(sector, 0) + abs(market_value)

    is_algo = mode == "algo"
    now_et = datetime.now(MARKET_TZ)
    session_day = now_et.date() + timedelta(days=1) if is_algo and now_et.hour >= 20 else now_et.date()
    session_date = session_day.isoformat()
    if is_algo:
        session_start_et, _ = _algo_session_bounds(session_date)
    else:
        session_start_et = datetime(
            now_et.year, now_et.month, now_et.day, tzinfo=MARKET_TZ,
        )
    session_start = session_start_et.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    entry_count_today = None
    entry_count_available = True
    entry_count_error = None
    try:
        # The same paginated New York session query is required for all
        # accounts. A 100-order first page can omit earlier Day entries.
        orders = _get_algo_orders_since(session_start, mode=mode)
        buy_orders_today = [
            o for o in orders
            if str(o.get("side", "")).lower() == "buy"
            and (
                _is_in_algo_session(o.get("submitted_at", ""), session_date)
                if is_algo else _is_market_session_date(o.get("submitted_at", ""), session_date)
            )
            and (
                o.get("status") not in ("canceled", "expired", "rejected")
                or float(o.get("filled_qty") or 0) > 0
            )
        ]
        trade_count_today = len(buy_orders_today)
        if is_algo:
            fills = _get_algo_fill_activities(session_start, mode=mode)
            entry_count_today = _count_algo_new_entries_today(
                orders, fills, positions, session_date,
            )
            trade_count_today = entry_count_today
        else:
            entry_count_today = trade_count_today
    except Exception as exc:
        log.warning("Could not read %s order history: %s", mode, exc)
        trade_count_today = None
        entry_count_today = None
        entry_count_available = False
        entry_count_error = f"Could not read today's {mode} order history"

    pending_buy_value_by_ticker: dict[str, float] = {}
    pending_short_value_by_ticker: dict[str, float] = {}
    pending_exposure_available = True
    pending_exposure_error = None
    easy_to_borrow_symbols: set[str] = set()
    shortability_available = False
    try:
        open_orders = _get_open_orders(mode)
        pending_buy_value_by_ticker = _pending_buy_exposure(
            open_orders, positions,
        )
        if mode == "algo":
            pending_short_value_by_ticker = _pending_short_exposure(open_orders, positions)
        for symbol, value in pending_buy_value_by_ticker.items():
            sector = ticker_sector.get(symbol, "other")
            sector_alloc[sector] = sector_alloc.get(sector, 0.0) + value
        for symbol, value in pending_short_value_by_ticker.items():
            sector = ticker_sector.get(symbol, "other")
            sector_alloc[sector] = sector_alloc.get(sector, 0.0) + value
    except Exception as exc:
        log.warning("Could not value %s pending buy orders: %s", mode, exc)
        pending_exposure_available = False
        pending_exposure_error = f"Could not value {mode} open buy orders"

    if mode == "algo":
        try:
            easy_to_borrow_symbols = _easy_to_borrow_symbols(mode)
            shortability_available = True
        except Exception as exc:
            log.warning("Could not verify %s shortability: %s", mode, exc)

    pending_buy_reservation = sum(pending_buy_value_by_ticker.values())
    available_cash = cash - pending_buy_reservation
    pending_new_symbols = {
        symbol for symbol in pending_buy_value_by_ticker
        if float((positions.get(symbol) or {}).get("qty", 0)) == 0
    }
    pending_new_symbols.update(
        symbol for symbol in pending_short_value_by_ticker
        if float((positions.get(symbol) or {}).get("qty", 0)) == 0
    )

    broker_account_id = str(account.get("id") or account.get("account_number") or "")
    weekly_baseline = _find_weekly_equity_baseline(
        mode=mode, account_id=broker_account_id, session_day=session_day,
    )
    weekly_baseline_equity = weekly_baseline["equity"] if weekly_baseline else None
    weekly_pnl_pct = _estimate_weekly_pnl(
        equity, mode=mode, account_id=broker_account_id, session_day=session_day,
    )
    weekly_pnl_dollars = (
        equity - weekly_baseline_equity
        if weekly_baseline_equity is not None else None
    )

    return {
        "account_id": broker_account_id,
        "equity": equity,
        "daily_baseline_equity": daily_baseline_equity,
        "daily_pnl_dollars": daily_pnl_dollars,
        "cash": cash,
        "cash_pct": cash_pct,
        "available_cash": available_cash,
        "available_cash_pct": available_cash / equity if equity else 0.0,
        "pending_buy_reservation": pending_buy_reservation,
        "pending_buy_value_by_ticker": pending_buy_value_by_ticker,
        "pending_short_value_by_ticker": pending_short_value_by_ticker,
        "pending_new_symbols": sorted(pending_new_symbols),
        "pending_exposure_available": pending_exposure_available,
        "pending_exposure_error": pending_exposure_error,
        "daily_pnl_pct": daily_pnl_pct,
        "weekly_pnl_pct": weekly_pnl_pct,
        "weekly_baseline_equity": weekly_baseline_equity,
        "weekly_baseline_date": weekly_baseline["date"] if weekly_baseline else None,
        "weekly_baseline_kind": weekly_baseline["kind"] if weekly_baseline else None,
        "weekly_pnl_dollars": weekly_pnl_dollars,
        "positions": positions,
        "sector_alloc": sector_alloc,
        "shortability_available": shortability_available,
        "easy_to_borrow_symbols": sorted(easy_to_borrow_symbols),
        "trade_count_today": trade_count_today,
        "entry_count_today": entry_count_today,
        "entry_count_available": entry_count_available,
        "entry_count_error": entry_count_error,
        "open_position_count": len([p for p in positions.values() if p["qty"] != 0]) + len(pending_new_symbols),
    }


def stage_approved_trade(
    portfolio_state: dict,
    ticker: str,
    action: str,
    qty: int,
    entry_price: float,
    mode: str = None,
) -> dict:
    """Reserve an approved long or short entry before validating the next signal.

    execute_trades.py can receive multiple strategy signals at once. Staging
    successful or dry-run entries prevents each signal from being checked
    against the same pre-batch cash, position, and sector totals.
    """
    state = deepcopy(portfolio_state)
    action = action.lower()
    if action not in {"buy", "short"} or qty <= 0 or entry_price <= 0:
        return state
    if mode is None:
        mode = os.environ.get("TRADING_MODE", "swing")
    ticker = ticker.upper()
    trade_value = int(qty) * float(entry_price)
    equity = float(state.get("equity", 0) or 0)
    positions = state.setdefault("positions", {})
    existing = positions.get(ticker)
    previous_value = float((existing or {}).get("market_value", 0) or 0)
    previous_qty = float((existing or {}).get("qty", 0) or 0)
    signed_qty = int(qty) if action == "buy" else -int(qty)
    signed_value = trade_value if action == "buy" else -trade_value
    positions[ticker] = {
        **(existing or {}),
        "symbol": ticker,
        "qty": previous_qty + signed_qty,
        "market_value": previous_value + signed_value,
        "current_price": float(entry_price),
        "pct_of_equity": (previous_value + signed_value) / equity if equity else 0,
    }
    cash = float(state.get("cash", 0) or 0) + (trade_value if action == "short" else -trade_value)
    state["cash"] = cash
    state["cash_pct"] = cash / equity if equity else 0
    is_new_position = (existing is None or previous_qty == 0) and ticker not in set(
        state.get("pending_new_symbols") or []
    )
    if is_new_position:
        state["open_position_count"] = int(state.get("open_position_count", 0)) + 1
    available_cash = float(state.get("available_cash", cash) or 0) + (trade_value if action == "short" else -trade_value)
    state["available_cash"] = available_cash
    state["available_cash_pct"] = available_cash / equity if equity else 0
    previous_trade_count = state.get("trade_count_today")
    if previous_trade_count is not None:
        state["trade_count_today"] = int(previous_trade_count) + 1
    if state.get("entry_count_available", True):
        previous_entry_count = int(
            state.get("entry_count_today", previous_trade_count) or 0
        )
        if mode != "algo" or is_new_position:
            state["entry_count_today"] = previous_entry_count + 1

    ticker_sector, _, _, _ = _get_universe_maps(mode)
    sector = ticker_sector.get(ticker, "other")
    if sector != "other":
        allocation = state.setdefault("sector_alloc", {})
        allocation[sector] = float(allocation.get(sector, 0) or 0) + trade_value
    return state


def _snapshot_dir(mode: str) -> Path:
    """Match the account-isolated paths written by performance_tracker_v2."""
    root = Path(__file__).parent
    if mode == "algo":
        return root / "data" / "snapshots" / "algo"
    if mode == "swing":
        return root / "snapshots"
    return root / "snapshots" / mode


def _risk_baseline_dir(mode: str) -> Path:
    return Path(__file__).parent / "data" / "risk_baselines" / mode


def capture_account_inception_baseline(mode: str, *, now: datetime | None = None) -> dict:
    """Anchor a new, unused paper account to its verified opening equity.

    This is an explicit one-time operation after an Alpaca paper-account reset.
    The first trade must not be allowed to move the baseline forward.
    """
    if mode not in {"swing", "day", "algo"}:
        raise ValueError("An explicit swing, day, or algo account mode is required")
    account, broker = verify_account_for_mode(mode)
    broker_id = str(broker.get("id") or "")
    if not broker_id:
        raise ValueError("Broker account has no ID")
    current = (now or datetime.now(timezone.utc)).astimezone(MARKET_TZ)
    session_day = current.date() + timedelta(days=1) if mode == "algo" and current.hour >= 20 else current.date()
    week_start = session_day - timedelta(days=session_day.weekday())
    try:
        created_at = datetime.fromisoformat(str(broker["created_at"]).replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            raise ValueError("Broker account creation time has no timezone")
        created_local = created_at.astimezone(MARKET_TZ)
        equity = float(broker["equity"])
        cash = float(broker["cash"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Broker account inception details are unavailable") from exc
    if created_local > current or (current - created_local) > timedelta(days=7):
        raise ValueError("Broker account is too old to capture an inception baseline")
    if not math.isfinite(equity) or equity <= 0 or not math.isfinite(cash) or abs(equity - cash) > 0.01:
        raise ValueError("New account equity must be positive and equal to cash")
    positions = _api("positions", mode=mode)
    orders = _api("orders?status=all&limit=1", mode=mode)
    if not isinstance(positions, list) or not isinstance(orders, list) or positions or orders:
        raise ValueError("Inception baseline requires zero positions and zero order history")

    baseline = {
        "snapshot_kind": "account_inception",
        "trading_mode": mode,
        "account_id": broker_id,
        "account_created_at": created_at.astimezone(timezone.utc).isoformat(),
        "timestamp": current.astimezone(timezone.utc).isoformat(),
        "week_start": week_start.isoformat(),
        "equity": equity,
    }
    directory = _risk_baseline_dir(mode)
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(broker_id.encode("utf-8")).hexdigest()[:24]
    path = directory / f"{digest}.json"
    def existing_baseline() -> dict:
        with path.open() as handle:
            existing = json.load(handle)
        if existing != baseline:
            # Timestamp differs on a later invocation; allow only the already
            # captured account, creation time, week and opening equity.
            fields = ("snapshot_kind", "trading_mode", "account_id", "account_created_at", "week_start", "equity")
            if any(existing.get(field) != baseline[field] for field in fields):
                raise ValueError("Existing account inception baseline differs; refusing overwrite")
        return existing
    if path.exists():
        return existing_baseline()
    temp_path = None
    try:
        with NamedTemporaryFile("w", dir=directory, prefix=".inception-", suffix=".tmp", delete=False) as handle:
            temp_path = Path(handle.name)
            json.dump(baseline, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # Create the final name only if absent. Concurrent invocations
            # cannot replace a baseline after an equity move.
            os.link(temp_path, path)
        except FileExistsError:
            return existing_baseline()
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return baseline


def _find_weekly_equity_baseline(
    *, mode: str, account_id: str | None, session_day: date | None = None,
) -> dict | None:
    """Return the account-bound weekly equity baseline and its provenance."""
    mode = mode or "swing"
    if not account_id:
        return None
    today = session_day or datetime.now(MARKET_TZ).date()
    week_start = today - timedelta(days=today.weekday())
    previous_week_start = week_start - timedelta(days=7)
    # A newly created, never-traded paper account has no preceding close.
    # Its explicit inception record is the first week-of-life baseline.
    for baseline_file in _risk_baseline_dir(mode).glob("*.json"):
        try:
            with baseline_file.open() as handle:
                baseline = json.load(handle)
            starting_equity = float(baseline["equity"])
            if (
                baseline.get("snapshot_kind") == "account_inception"
                and baseline.get("trading_mode") == mode
                and baseline.get("account_id") == account_id
                and baseline.get("week_start") == week_start.isoformat()
                and math.isfinite(starting_equity)
                and starting_equity > 0
            ):
                return {
                    "date": week_start.isoformat(),
                    "equity": starting_equity,
                    "kind": "account_inception",
                }
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
    baselines: list[tuple[date, float]] = []
    for snapshot_file in _snapshot_dir(mode).glob("*.json"):
        try:
            with open(snapshot_file) as handle:
                snapshot = json.load(handle)
            snapshot_date = datetime.strptime(str(snapshot["date"]), "%Y-%m-%d").date()
            baseline_equity = float(snapshot["equity"])
            if (
                previous_week_start <= snapshot_date < week_start
                and snapshot.get("trading_mode") == mode
                and snapshot.get("account_id") == account_id
                and snapshot.get("snapshot_kind") == "close"
                and math.isfinite(baseline_equity)
                and baseline_equity > 0
            ):
                baselines.append((snapshot_date, baseline_equity))
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
    if not baselines:
        return None
    baseline_date, baseline_equity = max(baselines, key=lambda row: row[0])
    return {
        "date": baseline_date.isoformat(),
        "equity": baseline_equity,
        "kind": "close_snapshot",
    }


def _estimate_weekly_pnl(
    current_equity: float,
    mode: str = None,
    *,
    account_id: str | None = None,
    session_day: date | None = None,
) -> float | None:
    """Return week-to-date P&L from the same account's tagged baseline."""
    if not math.isfinite(current_equity) or current_equity <= 0:
        return None
    baseline = _find_weekly_equity_baseline(
        mode=mode or "swing", account_id=account_id, session_day=session_day,
    )
    if baseline is None:
        return None
    return (current_equity - baseline["equity"]) / baseline["equity"]


def validate_trade(
    ticker: str,
    action: str,
    qty: int,
    entry_price: float,
    portfolio_state: Optional[dict] = None,
    mode: str = None,
    strategy: str | None = None,
) -> ValidationResult:
    """
    Validate a proposed trade against all hard rules for the given mode.

    Args:
        ticker:           Stock symbol (e.g. "NVDA").
        action:           "buy", "sell", "short", "cover", "hold".
        qty:              Number of shares.
        entry_price:      Expected fill price (used for position size calc).
        portfolio_state:  Pre-fetched state dict. If None, fetches live.
        mode:             "swing", "day", or the dedicated "algo" account route.
                          Reads TRADING_MODE env if not specified.

    Returns:
        ValidationResult with approved=True/False and a reason string.
    """
    if mode is None:
        mode = os.environ.get("TRADING_MODE", "swing")
    mode_config = get_mode_config(mode)
    risk = mode_config["risk"]
    ticker_sector, ticker_max_pct, tactical_tickers, universe = _get_universe_maps(mode)

    action = action.lower()

    if mode == "algo" and action in {"buy", "short"} and strategy in {"multi_factor", "event_driven"}:
        return ValidationResult(
            False,
            "Multi Factor and Event Driven are confirmation-only and cannot own entries.",
            "satellite_confirmation_only",
        )

    if action == "hold":
        return ValidationResult(approved=True, reason="Hold — no order to validate")

    if action in ("sell", "cover"):
        if qty <= 0:
            return ValidationResult(False, "Exit quantity must be positive.", "invalid_exit_quantity")
        try:
            if portfolio_state is None:
                live_positions = {
                    row["symbol"]: row for row in _api("positions", mode=mode)
                }
            else:
                live_positions = portfolio_state["positions"]
            held_qty = float((live_positions.get(ticker.upper()) or {}).get("qty", 0))
        except Exception as exc:
            return ValidationResult(False, f"Could not verify exit position: {exc}", "position_unavailable")
        if action == "sell" and (held_qty <= 0 or qty > held_qty):
            return ValidationResult(False, "Sell exceeds the verified long position.", "oversell")
        if action == "cover" and (held_qty >= 0 or qty > abs(held_qty)):
            return ValidationResult(False, "Cover exceeds the verified short position.", "overcover")
        return ValidationResult(True, "Verified position-reducing exit")

    if action not in ("buy", "short"):
        return ValidationResult(approved=False, reason=f"Unknown action '{action}'", rule="unknown_action")

    if action == "short":
        if mode != "algo" or not risk.get("allow_short_positions", False):
            return ValidationResult(False, "New short entries are disabled for this mode.", "short_disabled")
        if portfolio_state is None:
            try:
                portfolio_state = get_portfolio_state(mode=mode)
            except Exception as exc:
                return ValidationResult(False, f"Could not verify shortability and portfolio state: {exc}", "portfolio_fetch_failed")
        if not portfolio_state.get("shortability_available", False):
            return ValidationResult(False, "Alpaca shortability data is unavailable; refusing the short.", "shortability_unavailable")
        if ticker.upper() not in set(portfolio_state.get("easy_to_borrow_symbols") or []):
            return ValidationResult(False, f"{ticker.upper()} is not confirmed easy to borrow by Alpaca.", "not_easy_to_borrow")

    try:
        entry_price = float(entry_price)
    except (TypeError, ValueError):
        entry_price = 0.0
    if qty <= 0 or not math.isfinite(entry_price) or entry_price <= 0:
        return ValidationResult(
            approved=False,
            reason="Entry quantity and reference price must both be positive.",
            rule="invalid_entry_parameters",
        )
    if ticker.upper() not in ticker_sector:
        return ValidationResult(
            False,
            f"{ticker.upper()} is outside the {mode} risk catalog; sector limits are unavailable.",
            "outside_risk_catalog",
        )

    if mode == "day":
        try:
            schedule = get_day_schedule(
                flatten_by=risk.get("flatten_by", "15:45"),
                flatten_lead_minutes=risk.get("flatten_lead_minutes", 15),
                entry_cutoff_minutes=risk.get("entry_cutoff_minutes", 15),
            )
        except Exception as exc:
            return ValidationResult(False, f"Day market calendar unavailable: {exc}", "day_calendar_unavailable")
        if schedule is None or not schedule.entries_allowed(datetime.now(MARKET_TZ)):
            return ValidationResult(False, "Day entry window has closed or the market is shut.", "day_entry_cutoff")
        try:
            recovery_ok = day_recovery_confirmed(
                get_account_for_mode("day").account_id,
                session_open_at=schedule.opens_at,
            )
        except Exception:
            recovery_ok = False
        if not recovery_ok:
            return ValidationResult(
                False, "Today's Day opening cleanup has not verified a flat account.",
                "day_recovery_unconfirmed",
            )

    if portfolio_state is None:
        try:
            portfolio_state = get_portfolio_state(mode=mode)
        except Exception as e:
            return ValidationResult(
                approved=False,
                reason=f"Could not fetch portfolio state: {e}",
                rule="portfolio_fetch_failed",
            )

    equity = portfolio_state["equity"]
    trade_value = qty * entry_price

    if not portfolio_state.get("pending_exposure_available", True):
        return ValidationResult(
            False, "Open buy exposure is unavailable; new entries are blocked.",
            "pending_exposure_unavailable",
        )

    # ── Rule 1: Leveraged ETFs ──────────────────────────────────────────────
    # Allowed in day mode (they're in the day universe). Banned in swing mode.
    if ticker.upper() in LEVERAGED_ETFS and not risk["allow_leveraged_etfs"]:
        msg = f"BLOCKED: {ticker} is a leveraged ETF — not allowed in {mode} mode."
        log.warning(msg)
        return ValidationResult(approved=False, reason=msg, rule="no_leveraged_etfs")

    # ── Rule 2: Moonshots hard-blocked in all modes ─────────────────────────
    if ticker.upper() in MOONSHOTS:
        msg = f"BLOCKED: {ticker} is on the moonshot blocklist (removed in V2)."
        log.warning(msg)
        return ValidationResult(approved=False, reason=msg, rule="moonshot_blocked")

    # ── Rule 3: Daily loss circuit breaker ─────────────────────────────────
    daily_pnl = portfolio_state["daily_pnl_pct"]
    if daily_pnl <= -risk["daily_loss_limit"]:
        msg = (
            f"BLOCKED: Daily circuit breaker active — down "
            f"{abs(daily_pnl)*100:.1f}% today (limit {risk['daily_loss_limit']*100:.0f}%). "
            "No new entries today."
        )
        log.warning(msg)
        return ValidationResult(approved=False, reason=msg, rule="daily_circuit_breaker")

    # ── Rule 4: No new buys if portfolio down no_buy_if_down_pct ───────────
    if action == "buy" and daily_pnl <= -risk["no_buy_if_down_pct"]:
        msg = (
            f"BLOCKED: Portfolio down {abs(daily_pnl)*100:.1f}% today — "
            f"no new buys when down {risk['no_buy_if_down_pct']*100:.0f}%+ for the day."
        )
        log.warning(msg)
        return ValidationResult(approved=False, reason=msg, rule="no_buy_down_pct")

    # ── Rule 5: Weekly loss circuit breaker ────────────────────────────────
    weekly_pnl = portfolio_state["weekly_pnl_pct"]
    if weekly_pnl is None or not math.isfinite(float(weekly_pnl)):
        return ValidationResult(
            False, "Weekly loss baseline is missing or stale; new entries are blocked.",
            "weekly_baseline_unavailable",
        )
    if weekly_pnl <= -risk["weekly_loss_limit"]:
        msg = (
            f"BLOCKED: Weekly circuit breaker active — down "
            f"{abs(weekly_pnl)*100:.1f}% this week (limit {risk['weekly_loss_limit']*100:.0f}%). "
            "No new entries this week."
        )
        log.warning(msg)
        return ValidationResult(approved=False, reason=msg, rule="weekly_circuit_breaker")

    existing_position = portfolio_state["positions"].get(ticker.upper())
    existing_qty = float((existing_position or {}).get("qty", 0) or 0)
    if action == "buy" and existing_qty < 0:
        return ValidationResult(False, "Close the short position before opening a long position.", "opposing_position_open")
    if action == "short" and existing_qty > 0:
        return ValidationResult(False, "Close the long position before opening a short position.", "opposing_position_open")
    pending_buy = ticker.upper() in (portfolio_state.get("pending_buy_value_by_ticker") or {})
    pending_short = ticker.upper() in (portfolio_state.get("pending_short_value_by_ticker") or {})
    if action == "buy" and pending_short:
        return ValidationResult(False, "An opening short order is already pending for this ticker.", "opposing_order_pending")
    if action == "short" and pending_buy:
        return ValidationResult(False, "An opening long order is already pending for this ticker.", "opposing_order_pending")
    if abs(existing_qty) <= 1e-9 and ((action == "buy" and pending_buy) or (action == "short" and pending_short)):
        return ValidationResult(False, "An opening order is already pending for this ticker.", "entry_order_pending")
    is_new_position = abs(existing_qty) <= 1e-9 and not (pending_buy or pending_short)

    # ── Rule 6: Max trades / new entries per day ────────────────────────────
    if mode == "algo":
        # Algo's quota counts buys or shorts that open a flat ticker. Scale-ins
        # and exits do not use entry slots; Rule 9 still caps total size.
        if action in {"buy", "short"} and is_new_position:
            entry_count = portfolio_state.get(
                "entry_count_today", portfolio_state.get("trade_count_today")
            )
            count_available = portfolio_state.get("entry_count_available", entry_count is not None)
            if not count_available or entry_count is None:
                msg = "BLOCKED: Algo new-entry count unavailable; refusing an uncounted entry."
                log.warning(msg)
                return ValidationResult(approved=False, reason=msg, rule="entry_count_unavailable")
            if entry_count >= risk["max_trades_per_day"]:
                msg = (
                    f"BLOCKED: Algo max {risk['max_trades_per_day']} new entries/day reached "
                    f"({entry_count} already today)."
                )
                log.warning(msg)
                return ValidationResult(approved=False, reason=msg, rule="max_trades_per_day")
    elif not portfolio_state.get("entry_count_available", True) or portfolio_state.get("trade_count_today") is None:
        return ValidationResult(
            False, "Daily entry count is unavailable; refusing an uncounted entry.",
            "entry_count_unavailable",
        )
    elif portfolio_state["trade_count_today"] >= risk["max_trades_per_day"]:
        msg = (
            f"BLOCKED: Max {risk['max_trades_per_day']} trades/day reached "
            f"({portfolio_state['trade_count_today']} already today)."
        )
        log.warning(msg)
        return ValidationResult(approved=False, reason=msg, rule="max_trades_per_day")

    # ── Rule 7: Max open positions ──────────────────────────────────────────
    if is_new_position and portfolio_state["open_position_count"] >= risk["max_open_positions"]:
        msg = (
            f"BLOCKED: Max {risk['max_open_positions']} open positions reached "
            f"({portfolio_state['open_position_count']} open). Close a position first."
        )
        log.warning(msg)
        return ValidationResult(approved=False, reason=msg, rule="max_open_positions")

    # ── Rule 8: Min cash reserve ────────────────────────────────────────────
    cash_available = float(portfolio_state.get("available_cash", portfolio_state["cash"]))
    cash_after = cash_available + trade_value if action == "short" else cash_available - trade_value
    cash_pct_after = (cash_after / equity) if equity else 0
    if action == "buy" and cash_pct_after < risk["min_cash_pct"]:
        msg = (
            f"BLOCKED: Trade would reduce cash to {cash_pct_after*100:.1f}% "
            f"(minimum {risk['min_cash_pct']*100:.0f}%). "
            f"Max trade value: ${cash_available - equity * risk['min_cash_pct']:,.0f}."
        )
        log.warning(msg)
        return ValidationResult(approved=False, reason=msg, rule="min_cash_reserve")

    # ── Rule 9: Max position size (per-ticker or mode default) ─────────────
    effective_max_pct = ticker_max_pct.get(ticker.upper(), risk["max_position_pct"])
    current_ticker_value = abs(float((existing_position or {}).get("market_value", 0) or 0))
    pending_value_map = (
        portfolio_state.get("pending_short_value_by_ticker", {}) if action == "short"
        else portfolio_state.get("pending_buy_value_by_ticker", {})
    )
    existing_value = current_ticker_value + float(pending_value_map.get(ticker.upper(), 0) or 0)
    new_total_value = existing_value + trade_value
    position_pct = new_total_value / equity if equity else 0

    if position_pct > effective_max_pct:
        msg = (
            f"BLOCKED: {ticker} position would reach {position_pct*100:.1f}% of equity "
            f"(max {effective_max_pct*100:.0f}%). "
            f"Max additional: ${equity * effective_max_pct - existing_value:,.0f}."
        )
        log.warning(msg)
        return ValidationResult(approved=False, reason=msg, rule="max_position_size")

    if action == "short":
        short_positions = {
            symbol for symbol, position in portfolio_state["positions"].items()
            if float((position or {}).get("qty", 0) or 0) < 0
        }
        short_positions.update(
            symbol for symbol in (portfolio_state.get("pending_short_value_by_ticker") or {})
            if symbol not in portfolio_state["positions"]
        )
        if is_new_position and ticker.upper() not in short_positions and len(short_positions) >= int(risk.get("max_short_positions", 0)):
            return ValidationResult(False, "Algo maximum short position count reached.", "max_short_positions")
        short_exposure = sum(
            abs(float((position or {}).get("market_value", 0) or 0))
            for position in portfolio_state["positions"].values()
            if float((position or {}).get("qty", 0) or 0) < 0
        ) + sum(float(value or 0) for value in (portfolio_state.get("pending_short_value_by_ticker") or {}).values())
        short_exposure += trade_value
        max_short_exposure = equity * float(risk.get("max_short_exposure_pct", 0))
        if short_exposure > max_short_exposure:
            return ValidationResult(
                False,
                f"Short exposure would reach {short_exposure / equity:.1%} of equity "
                f"(max {float(risk.get('max_short_exposure_pct', 0)):.0%}).",
                "max_short_exposure_pct",
            )

    # ── Rule 10: Max sector allocation ─────────────────────────────────────
    sector = ticker_sector.get(ticker.upper(), "other")
    if sector != "other":
        sector_cfg = universe.get(sector, {})
        sector_cap = sector_cfg.get("max_sector_pct", risk["max_sector_pct"])
        current_sector_value = portfolio_state["sector_alloc"].get(sector, 0)
        new_sector_value = current_sector_value + trade_value
        sector_pct = new_sector_value / equity if equity else 0

        if sector_pct > sector_cap:
            msg = (
                f"BLOCKED: {sector} sector would reach {sector_pct*100:.1f}% "
                f"(cap {sector_cap*100:.0f}%). "
                f"Current sector: ${current_sector_value:,.0f}."
            )
            log.warning(msg)
            return ValidationResult(approved=False, reason=msg, rule="max_sector_pct")

    # ── Rule 11: Tactical bucket cap ────────────────────────────────────────
    # In day mode max_tactical_pct=1.0, so this rule never fires.
    if tactical_tickers and risk["max_tactical_pct"] < 1.0:
        if ticker.upper() in tactical_tickers or ticker.upper() in MOONSHOTS:
            max_tactical_positions = risk.get("max_tactical_positions")
            if max_tactical_positions is not None:
                tactical_symbols = {
                    symbol.upper()
                    for symbol, position in portfolio_state["positions"].items()
                    if (symbol.upper() in tactical_tickers or symbol.upper() in MOONSHOTS)
                    and abs(float(position.get("qty", 0) or 0)) > 0
                }
                tactical_symbols.update(
                    symbol.upper()
                    for symbol, value in (portfolio_state.get("pending_buy_value_by_ticker") or {}).items()
                    if (symbol.upper() in tactical_tickers or symbol.upper() in MOONSHOTS)
                    and float(value or 0) > 0
                )
                tactical_symbols.update(
                    symbol.upper()
                    for symbol, value in (portfolio_state.get("pending_short_value_by_ticker") or {}).items()
                    if (symbol.upper() in tactical_tickers or symbol.upper() in MOONSHOTS)
                    and float(value or 0) > 0
                )
                if ticker.upper() not in tactical_symbols and len(tactical_symbols) >= max_tactical_positions:
                    msg = (
                        f"BLOCKED: Tactical position limit of {max_tactical_positions} reached "
                        f"({len(tactical_symbols)} active or pending)."
                    )
                    log.warning(msg)
                    return ValidationResult(False, msg, "max_tactical_positions")

            tactical_positions = [
                v for k, v in portfolio_state["positions"].items()
                if k in tactical_tickers or k in MOONSHOTS
            ]
            total_tactical = sum(abs(float(p.get("market_value", 0) or 0)) for p in tactical_positions)
            total_tactical += sum(
                value for symbol, value in
                (portfolio_state.get("pending_buy_value_by_ticker") or {}).items()
                if symbol in tactical_tickers or symbol in MOONSHOTS
            )
            total_tactical += sum(
                value for symbol, value in
                (portfolio_state.get("pending_short_value_by_ticker") or {}).items()
                if symbol in tactical_tickers or symbol in MOONSHOTS
            )
            new_total = total_tactical + trade_value
            tactical_pct = new_total / equity if equity else 0

            if tactical_pct > risk["max_tactical_pct"]:
                msg = (
                    f"BLOCKED: High-risk / tactical allocation would reach {tactical_pct*100:.1f}% "
                    f"(max {risk['max_tactical_pct']*100:.0f}%)."
                )
                log.warning(msg)
                return ValidationResult(approved=False, reason=msg, rule="max_tactical_pct")

    # ── All rules passed ────────────────────────────────────────────────────
    approved_msg = (
        f"APPROVED: {action.upper()} {qty} {ticker} @ ${entry_price:.2f} "
        f"(${trade_value:,.0f} = {trade_value/equity*100:.1f}% of equity). "
        f"Daily P&L: {daily_pnl*100:+.2f}%, Cash after: {cash_pct_after*100:.1f}%."
    )
    log.info(approved_msg)
    return ValidationResult(approved=True, reason=approved_msg)


def print_status(portfolio_state: dict, mode: str = None) -> None:
    """Pretty-print current risk status to stdout."""
    if mode is None:
        mode = os.environ.get("TRADING_MODE", "swing")
    mode_config = get_mode_config(mode)
    risk = mode_config["risk"]
    ticker_sector, _, _, universe = _get_universe_maps(mode)

    eq = portfolio_state["equity"]
    print(f"{'='*60}")
    print(f"  SWARM TRADER — RISK MANAGER STATUS")
    print(f"  Mode: {mode.upper()} — {mode_config['label']}")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")
    print(f"  Equity:          ${eq:>12,.2f}")
    print(f"  Cash:            ${portfolio_state['cash']:>12,.2f}  ({portfolio_state['cash_pct']*100:.1f}%)")
    reserved = portfolio_state.get("pending_buy_reservation", 0)
    if reserved:
        print(f"  Pending buys:    ${reserved:>12,.2f} reserved")
    print(f"  Daily P&L:        {portfolio_state['daily_pnl_pct']*100:>+11.2f}%")
    weekly_pnl = portfolio_state.get("weekly_pnl_pct")
    weekly_display = f"{weekly_pnl*100:+.2f}%" if weekly_pnl is not None else "unavailable"
    print(f"  Weekly P&L:       {weekly_display:>12}")
    print(f"  Open Positions:  {portfolio_state['open_position_count']:>13}  / {risk['max_open_positions']} max")
    if mode == "algo":
        entry_count = portfolio_state.get("entry_count_today")
        count_label = "New Entries Today"
        count_display = "unavailable" if entry_count is None or not portfolio_state.get("entry_count_available", True) else str(entry_count)
    else:
        entry_count = portfolio_state["trade_count_today"]
        count_label = "Trades Today"
        count_display = "unavailable" if entry_count is None or not portfolio_state.get("entry_count_available", True) else str(entry_count)
    print(f"  {count_label:<18}{count_display:>13}  / {risk['max_trades_per_day']} max")
    print()

    daily_cb = portfolio_state["daily_pnl_pct"] <= -risk["daily_loss_limit"]
    no_buy_cb = portfolio_state["daily_pnl_pct"] <= -risk["no_buy_if_down_pct"]
    weekly_cb = weekly_pnl is None or weekly_pnl <= -risk["weekly_loss_limit"]
    cash_ok = portfolio_state.get("available_cash_pct", portfolio_state["cash_pct"]) >= risk["min_cash_pct"]

    print(f"  CIRCUIT BREAKERS:")
    print(f"    Daily  (-{risk['daily_loss_limit']*100:.0f}%):  {'🔴 ACTIVE' if daily_cb else '🟢 OK'}")
    print(f"    No-buy (-{risk['no_buy_if_down_pct']*100:.0f}%):  {'🔴 ACTIVE' if no_buy_cb else '🟢 OK'}")
    weekly_status = "⚪ UNAVAILABLE" if weekly_pnl is None else ("🔴 ACTIVE" if weekly_cb else "🟢 OK")
    print(f"    Weekly (-{risk['weekly_loss_limit']*100:.0f}%): {weekly_status}")
    print(f"    Cash   (>{risk['min_cash_pct']*100:.0f}%):   {'🟢 OK' if cash_ok else '🔴 LOW'}")
    if mode == "day":
        flatten_by = risk.get("flatten_by", "15:45")
        print(f"    Flatten EOD ({flatten_by} ET): enabled")
    print()

    if portfolio_state["sector_alloc"]:
        print(f"  SECTOR ALLOCATIONS:")
        for sector, value in sorted(portfolio_state["sector_alloc"].items(), key=lambda x: -x[1]):
            pct = value / eq * 100 if eq else 0
            cap = universe.get(sector, {}).get("max_sector_pct", risk["max_sector_pct"]) * 100
            flag = "⚠️" if pct > cap * 0.9 else "  "
            print(f"    {flag} {sector:<20} ${value:>10,.0f}  ({pct:>5.1f}% / {cap:.0f}% cap)")
    print()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Swarm Trader Risk Manager")
    parser.add_argument("--status", action="store_true", help="Show current risk status")
    parser.add_argument(
        "--mode",
        choices=["swing", "day", "algo"],
        default=None,
        help="Trading mode (overrides TRADING_MODE env, default: swing)",
    )
    args = parser.parse_args()

    mode = args.mode or os.environ.get("TRADING_MODE", "swing")

    if args.status:
        try:
            state = get_portfolio_state(mode=mode)
            print_status(state, mode=mode)
        except Exception as e:
            print(f"Error fetching portfolio state: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        parser.print_help()
