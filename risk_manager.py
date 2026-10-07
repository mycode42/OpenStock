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
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(override=True)

import requests

from src.config import get_mode_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [risk_manager] %(levelname)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("risk_manager")

from src.accounts import get_account_for_mode

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


def _get_algo_orders_since(session_start: str, mode: str) -> list[dict]:
    """Fetch all Algo orders since the session start using Alpaca ID cursors."""
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
    """Count today's filled or pending buys that opened a flat ticker.

    Rebuild each ticker's start-of-session share count from current positions
    and Alpaca fill activities, then replay fills and today's submitted buys.
    Scale-ins do not consume the quota; a buy after a completed exit does.
    """
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
        if timestamp.astimezone(MARKET_TZ).date().isoformat() != session_date:
            continue
        if qty <= 0:
            continue
        order_id = str(fill.get("order_id") or fill.get("id") or "")
        today_fills.append((ticker, timestamp, side, qty, order_id))
        if order_id:
            fills_by_order[order_id] = fills_by_order.get(order_id, 0.0) + qty
        signed_qty = qty if side == "buy" else -qty
        filled_delta[ticker] = filled_delta.get(ticker, 0.0) + signed_qty

    today_buy_order_ids: set[str] = set()
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
        if timestamp.astimezone(MARKET_TZ).date().isoformat() != session_date:
            continue

        order_id = str(order.get("id") or "")
        status = str(order.get("status", "")).lower()
        pending = status not in {
            "canceled", "expired", "rejected", "filled", "done_for_day",
            "replaced", "stopped", "suspended", "calculated",
        }
        if side == "buy":
            filled_qty = max(fills_by_order.get(order_id, 0.0), reported_filled)
            if filled_qty <= 0 and not pending:
                continue
            missing_fill_qty = max(0.0, reported_filled - fills_by_order.get(order_id, 0.0))
            filled_delta[ticker] = filled_delta.get(ticker, 0.0) + missing_fill_qty
            projected_qty = max(filled_qty, order_qty if pending else filled_qty)
            events_by_ticker.setdefault(ticker, []).append((timestamp, "buy", projected_qty, True, order_id))
            if order_id:
                today_buy_order_ids.add(order_id)
        elif reported_filled > fills_by_order.get(order_id, 0.0):
            missing_fill_qty = reported_filled - fills_by_order.get(order_id, 0.0)
            filled_delta[ticker] = filled_delta.get(ticker, 0.0) - missing_fill_qty
            filled_at = order.get("filled_at") or order.get("updated_at") or submitted_at
            try:
                filled_timestamp = datetime.fromisoformat(str(filled_at).replace("Z", "+00:00"))
                if filled_timestamp.tzinfo is None:
                    filled_timestamp = filled_timestamp.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                filled_timestamp = timestamp
            events_by_ticker.setdefault(ticker, []).append((filled_timestamp, "sell", missing_fill_qty, False, order_id))

    # Today's buy orders are represented once at submission time above;
    # remaining fills still adjust holdings when the order was submitted
    # before this session (for example, a prior-day protective exit).
    for ticker, timestamp, side, qty, order_id in today_fills:
        if side == "buy" and order_id in today_buy_order_ids:
            continue
        events_by_ticker.setdefault(ticker, []).append((timestamp, side, qty, False, order_id))

    entries = 0
    for ticker, events in events_by_ticker.items():
        current_qty = float((positions.get(ticker) or {}).get("qty", 0) or 0)
        starting_qty = max(0.0, current_qty - filled_delta.get(ticker, 0.0))
        running_qty = starting_qty
        counted_order_ids: set[str] = set()
        for _, side, qty, countable, order_id in sorted(events, key=lambda event: event[0]):
            if side == "buy":
                if countable and running_qty <= 0 and order_id not in counted_order_ids:
                    entries += 1
                    counted_order_ids.add(order_id)
                running_qty += qty
            else:
                running_qty = max(0.0, running_qty - qty)
    return entries


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


def get_portfolio_state(mode: str = None) -> dict:
    """
    Fetch live portfolio state from Alpaca.

    Args:
        mode: "swing", "day", or the dedicated "algo" account route.
              Reads TRADING_MODE env if not specified.

    Returns a dict with:
      equity, cash, cash_pct, daily_pnl_pct, weekly_pnl_pct,
      positions (symbol → position dict),
      sector_alloc (sector → $ value),
      trade_count_today (all nonterminal buy orders),
      entry_count_today (new-position buys for Algo; legacy buy count otherwise),
      open_position_count.
    """
    if mode is None:
        mode = os.environ.get("TRADING_MODE", "swing")
    ticker_sector, _, _, _ = _get_universe_maps(mode)

    account = _api("account", mode=mode)
    positions_raw = _api("positions", mode=mode)

    equity = float(account.get("equity", 0))
    cash = float(account.get("cash", 0))
    last_equity = float(account.get("last_equity", equity))

    daily_pnl_pct = ((equity - last_equity) / last_equity) if last_equity else 0.0
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
        sector_alloc[sector] = sector_alloc.get(sector, 0) + market_value

    is_algo = mode == "algo"
    now_et = datetime.now(MARKET_TZ) if is_algo else None
    session_date = now_et.date().isoformat() if is_algo else datetime.now().strftime("%Y-%m-%d")
    if is_algo:
        session_start = datetime(
            now_et.year, now_et.month, now_et.day, tzinfo=MARKET_TZ,
        ).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        order_limit = 500
    else:
        session_start = f"{session_date}T00:00:00Z"
        order_limit = 100

    entry_count_today = None
    entry_count_available = True
    entry_count_error = None
    try:
        order_endpoint = f"orders?status=all&after={session_start}&limit={order_limit}"
        if is_algo:
            order_endpoint += "&direction=asc"
        orders = (
            _get_algo_orders_since(session_start, mode=mode)
            if is_algo else _api(order_endpoint, mode=mode)
        )
        buy_orders_today = [
            o for o in orders
            if o.get("side") == "buy"
            and (
                _is_market_session_date(o.get("submitted_at", ""), session_date)
                if is_algo else o.get("submitted_at", "")[:10] == session_date
            )
            and o.get("status") not in ("canceled", "expired", "rejected")
        ]
        trade_count_today = len(buy_orders_today)
        if is_algo:
            fills = _get_algo_fill_activities(session_start, mode=mode)
            entry_count_today = _count_algo_new_entries_today(
                orders, fills, positions, session_date,
            )
        else:
            entry_count_today = trade_count_today
    except Exception:
        trade_count_today = 0
        if is_algo:
            entry_count_available = False
            entry_count_error = "Could not read today's Algo order history"
        else:
            entry_count_today = 0

    weekly_pnl_pct = _estimate_weekly_pnl(equity, mode=mode)

    return {
        "equity": equity,
        "cash": cash,
        "cash_pct": cash_pct,
        "daily_pnl_pct": daily_pnl_pct,
        "weekly_pnl_pct": weekly_pnl_pct,
        "positions": positions,
        "sector_alloc": sector_alloc,
        "trade_count_today": trade_count_today,
        "entry_count_today": entry_count_today,
        "entry_count_available": entry_count_available,
        "entry_count_error": entry_count_error,
        "open_position_count": len([p for p in positions.values() if p["qty"] > 0]),
    }


def stage_approved_trade(
    portfolio_state: dict,
    ticker: str,
    action: str,
    qty: int,
    entry_price: float,
    mode: str = None,
) -> dict:
    """Reserve an approved long entry before validating the next batch signal.

    execute_trades.py can receive multiple strategy signals at once. Staging
    successful or dry-run entries prevents each signal from being checked
    against the same pre-batch cash, position, and sector totals.
    """
    state = deepcopy(portfolio_state)
    if action.lower() != "buy" or qty <= 0 or entry_price <= 0:
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
    positions[ticker] = {
        **(existing or {}),
        "symbol": ticker,
        "qty": previous_qty + int(qty),
        "market_value": previous_value + trade_value,
        "current_price": float(entry_price),
        "pct_of_equity": (previous_value + trade_value) / equity if equity else 0,
    }
    cash = float(state.get("cash", 0) or 0) - trade_value
    state["cash"] = cash
    state["cash_pct"] = cash / equity if equity else 0
    is_new_position = existing is None or previous_qty <= 0
    if is_new_position:
        state["open_position_count"] = int(state.get("open_position_count", 0)) + 1
    previous_trade_count = int(state.get("trade_count_today", 0) or 0)
    state["trade_count_today"] = previous_trade_count + 1
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


def _estimate_weekly_pnl(current_equity: float, mode: str = None) -> float:
    """Estimate weekly P&L by reading performance snapshots.
    
    Filters by mode when available to avoid cross-account confusion
    (e.g. swing $100K vs day $150K would show a false -34% drop).
    """
    try:
        # V2 snapshots are already isolated by account/mode. Prefer them so
        # the weekly breaker also works for the dedicated Algo route.
        snapshot_dir = Path(__file__).parent / "snapshots"
        if mode == "algo":
            snapshot_dir = Path(__file__).parent / "data" / "snapshots" / "dip"
        elif mode and mode != "swing":
            snapshot_dir = snapshot_dir / mode
        if snapshot_dir.exists():
            import json
            cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            mode_snapshots = []
            for snapshot_file in sorted(snapshot_dir.glob("*.json")):
                try:
                    with open(snapshot_file) as snapshot_handle:
                        snapshot = json.load(snapshot_handle)
                    if snapshot.get("date", "") >= cutoff and snapshot.get("equity"):
                        mode_snapshots.append(snapshot)
                except Exception:
                    continue
            if len(mode_snapshots) >= 2:
                base_equity = float(mode_snapshots[0]["equity"])
                if base_equity > 0:
                    return (current_equity - base_equity) / base_equity
        if mode == "algo":
            # Never estimate Algo's breaker from a legacy shared-account file.
            return 0.0

        data_path = Path(__file__).parent / "data" / "performance.json"
        if not data_path.exists():
            return 0.0
        import json
        with open(data_path) as f:
            data = json.load(f)
        snapshots = data.get("snapshots", [])
        if len(snapshots) < 2:
            return 0.0
        cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        week_snaps = [s for s in snapshots if s["date"] >= cutoff]
        # Filter by mode if snapshots have mode tags; if none are tagged
        # and the equity delta exceeds 25%, assume cross-account contamination
        # and return 0 (safe default for a fresh or different account).
        if mode:
            tagged = [s for s in week_snaps if s.get("mode") == mode]
            if tagged:
                week_snaps = tagged
            else:
                # No mode-tagged snapshots — check if base equity is wildly different
                # (indicates mixed account data). Threshold: 25% difference.
                if week_snaps:
                    base = week_snaps[0]["equity"]
                    if base > 0 and abs(current_equity - base) / base > 0.25:
                        return 0.0  # Cross-account data, ignore
        if not week_snaps:
            return 0.0
        base_equity = week_snaps[0]["equity"]
        if base_equity <= 0:
            return 0.0
        return (current_equity - base_equity) / base_equity
    except Exception:
        return 0.0


def validate_trade(
    ticker: str,
    action: str,
    qty: int,
    entry_price: float,
    portfolio_state: Optional[dict] = None,
    mode: str = None,
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

    # Only validate entries; exits/holds pass through
    if action in ("sell", "cover", "hold"):
        return ValidationResult(approved=True, reason="Exit/hold — no entry rules apply")

    if action not in ("buy", "short"):
        return ValidationResult(approved=False, reason=f"Unknown action '{action}'", rule="unknown_action")

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
    if weekly_pnl <= -risk["weekly_loss_limit"]:
        msg = (
            f"BLOCKED: Weekly circuit breaker active — down "
            f"{abs(weekly_pnl)*100:.1f}% this week (limit {risk['weekly_loss_limit']*100:.0f}%). "
            "Reduce position sizes by 50% this week."
        )
        log.warning(msg)
        return ValidationResult(approved=False, reason=msg, rule="weekly_circuit_breaker")

    existing_position = portfolio_state["positions"].get(ticker.upper())
    is_new_position = existing_position is None or existing_position.get("qty", 0) == 0

    # ── Rule 6: Max trades / new entries per day ────────────────────────────
    if mode == "algo":
        # Algo's quota counts only buys that open a flat ticker. Scale-ins and
        # exits do not use entry slots; Rule 9 still caps their total size.
        if action == "buy" and is_new_position:
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
                    f"BLOCKED: Algo max {risk['max_trades_per_day']} new buy entries/day reached "
                    f"({entry_count} already today)."
                )
                log.warning(msg)
                return ValidationResult(approved=False, reason=msg, rule="max_trades_per_day")
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
    cash_after = portfolio_state["cash"] - trade_value
    cash_pct_after = (cash_after / equity) if equity else 0
    if cash_pct_after < risk["min_cash_pct"]:
        msg = (
            f"BLOCKED: Trade would reduce cash to {cash_pct_after*100:.1f}% "
            f"(minimum {risk['min_cash_pct']*100:.0f}%). "
            f"Max trade value: ${portfolio_state['cash'] - equity * risk['min_cash_pct']:,.0f}."
        )
        log.warning(msg)
        return ValidationResult(approved=False, reason=msg, rule="min_cash_reserve")

    # ── Rule 9: Max position size (per-ticker or mode default) ─────────────
    effective_max_pct = ticker_max_pct.get(ticker.upper(), risk["max_position_pct"])
    existing_value = (existing_position or {}).get("market_value", 0)
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
            tactical_positions = [
                v for k, v in portfolio_state["positions"].items()
                if k in tactical_tickers or k in MOONSHOTS
            ]
            total_tactical = sum(p.get("market_value", 0) for p in tactical_positions)
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
    print(f"  Daily P&L:        {portfolio_state['daily_pnl_pct']*100:>+11.2f}%")
    print(f"  Weekly P&L:       {portfolio_state['weekly_pnl_pct']*100:>+11.2f}%")
    print(f"  Open Positions:  {portfolio_state['open_position_count']:>13}  / {risk['max_open_positions']} max")
    if mode == "algo":
        entry_count = portfolio_state.get("entry_count_today")
        count_label = "New Entries Today"
        count_display = "unavailable" if entry_count is None or not portfolio_state.get("entry_count_available", True) else str(entry_count)
    else:
        entry_count = portfolio_state["trade_count_today"]
        count_label = "Trades Today"
        count_display = str(entry_count)
    print(f"  {count_label:<18}{count_display:>13}  / {risk['max_trades_per_day']} max")
    print()

    daily_cb = portfolio_state["daily_pnl_pct"] <= -risk["daily_loss_limit"]
    no_buy_cb = portfolio_state["daily_pnl_pct"] <= -risk["no_buy_if_down_pct"]
    weekly_cb = portfolio_state["weekly_pnl_pct"] <= -risk["weekly_loss_limit"]
    cash_ok = portfolio_state["cash_pct"] >= risk["min_cash_pct"]

    print(f"  CIRCUIT BREAKERS:")
    print(f"    Daily  (-{risk['daily_loss_limit']*100:.0f}%):  {'🔴 ACTIVE' if daily_cb else '🟢 OK'}")
    print(f"    No-buy (-{risk['no_buy_if_down_pct']*100:.0f}%):  {'🔴 ACTIVE' if no_buy_cb else '🟢 OK'}")
    print(f"    Weekly (-{risk['weekly_loss_limit']*100:.0f}%): {'🔴 ACTIVE' if weekly_cb else '🟢 OK'}")
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
