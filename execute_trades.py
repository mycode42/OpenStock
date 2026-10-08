#!/usr/bin/env python3
"""
Trade Executor — Takes Agent's JSON decisions and executes via Alpaca.

Input: JSON on stdin or --file with format:
{
  "trades": [
    {"ticker": "NVDA", "action": "buy", "qty": 50, "reasoning": "..."},
    {"ticker": "NVDA", "action": "buy", "qty": 20, "order_type": "bracket", "stop_price": 900, "take_profit": 1050, "reasoning": "..."},
    {"ticker": "NVDA", "action": "buy", "qty": 10, "order_type": "limit", "limit_price": 920, "reasoning": "..."},
    {"ticker": "NVDA", "action": "sell", "qty": 10, "order_type": "stop", "stop_price": 880, "reasoning": "stop-loss on existing"},
    {"ticker": "NVDA", "action": "sell", "qty": 10, "order_type": "oco", "stop_price": 880, "take_profit": 1050, "reasoning": "exit bracket on existing"},
    {"ticker": "NVDA", "action": "sell", "qty": 10, "order_type": "trailing_stop", "trail_percent": 2.0, "reasoning": "lock in gains"},
    {"ticker": "NVDA", "action": "short", "qty": 10, "stop_price": 920, "take_profit": 850, "reasoning": "breakdown below VWAP"}
  ]
}

Order types: market (default), limit, bracket, stop, oco, trailing_stop
Actions: buy, sell, hold, short, cover

Usage:
  echo '{"trades":[...]}' | poetry run python execute_trades.py
  poetry run python execute_trades.py --file decisions.json
  poetry run python execute_trades.py --file decisions.json --dry-run
  poetry run python execute_trades.py --flatten               # Sell all positions at market
  poetry run python execute_trades.py --flatten --dry-run     # Preview flatten
  poetry run python execute_trades.py --mode day --file decisions.json
"""

import argparse
import fcntl
import hashlib
import json
import logging
import math
import os
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [execute_trades] %(levelname)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("execute_trades")

from src.accounts import get_account_for_mode, verify_account_for_mode
from src.day_recovery import day_recovery_confirmed, record_day_recovery
from src.market_calendar import get_day_schedule

API_BASE = "https://paper-api.alpaca.markets/v2"
DATA_BASE = "https://data.alpaca.markets/v2"
MARKET_TZ = ZoneInfo("America/New_York")
LOCK_DIR = Path(__file__).parent / "data" / "locks"

# Module-level mode — set once in main(), used by helpers
_active_mode: str = "swing"


def _headers(mode: str = None) -> dict:
    """Get API headers for the currently active trading mode."""
    return get_account_for_mode(mode or _active_mode).headers

from src.config import ALGO_SERVICE_CONFIG, get_mode_config, DEFAULT_TARGET_MULTIPLIER

# V2 risk manager — required for every non-hold order
try:
    from risk_manager import (
        validate_trade as rm_validate_trade,
        get_portfolio_state as rm_get_portfolio_state,
        stage_approved_trade as rm_stage_approved_trade,
    )
    RISK_MANAGER_AVAILABLE = True
    log.info("V2 risk manager loaded")
except ImportError:
    RISK_MANAGER_AVAILABLE = False
    log.error("risk_manager.py not found — all orders will be blocked")


def get_account():
    r = requests.get(f"{API_BASE}/account", headers=_headers(), timeout=10)
    r.raise_for_status()
    return r.json()


def get_positions():
    r = requests.get(f"{API_BASE}/positions", headers=_headers(), timeout=10)
    r.raise_for_status()
    return {p["symbol"]: p for p in r.json()}


def get_latest_price(ticker: str) -> float:
    """Fetch an Alpaca IEX trade for risk sizing when a decision omits price."""
    response = requests.get(
        f"{DATA_BASE}/stocks/{ticker}/trades/latest",
        headers=_headers(),
        params={"feed": "iex"},
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    trade = payload.get("trade") or (payload.get("trades") or {}).get(ticker) or {}
    price = float(trade.get("p", 0) or 0)
    if price <= 0:
        raise ValueError(f"No positive latest Alpaca trade available for {ticker}")
    try:
        timestamp = datetime.fromisoformat(str(trade["t"]).replace("Z", "+00:00"))
        age = (datetime.now(timestamp.tzinfo) - timestamp).total_seconds()
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Latest Alpaca trade timestamp unavailable for {ticker}") from exc
    if age < 0 or age > 90:
        raise ValueError(f"Latest Alpaca trade for {ticker} is stale ({age:.0f}s)")
    return price


def get_latest_ask(ticker: str, *, mode: str) -> float:
    """Require a fresh broker-data ask before sizing a live market buy."""
    response = requests.get(
        f"{DATA_BASE}/stocks/{ticker}/quotes/latest",
        headers=_headers(mode),
        params={"feed": "iex"},
        timeout=10,
    )
    response.raise_for_status()
    quote = (response.json() or {}).get("quote") or {}
    try:
        ask = float(quote["ap"])
        timestamp = datetime.fromisoformat(str(quote["t"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Latest Alpaca ask unavailable for {ticker}") from exc
    if not math.isfinite(ask) or ask <= 0 or timestamp.tzinfo is None:
        raise ValueError(f"Latest Alpaca ask invalid for {ticker}")
    age = (datetime.now(timestamp.tzinfo) - timestamp).total_seconds()
    if age < 0 or age > 90:
        raise ValueError(f"Latest Alpaca ask for {ticker} is stale ({age:.0f}s)")
    return ask


def get_daily_pnl(account: dict) -> float:
    """Return today's P&L as a fraction of starting equity (negative = loss)."""
    equity = float(account.get("equity", 0))
    last_equity = float(account.get("last_equity", equity))
    if last_equity <= 0:
        return 0.0
    return (equity - last_equity) / last_equity


@contextmanager
def account_execution_lock(mode: str, timeout_seconds: float = 10.0):
    """Serialize validation, order submission, and flattening per account."""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK_DIR / f"{mode}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(fd, 0o600)
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Could not acquire {mode} account execution lock")
                time.sleep(0.1)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _list_open_orders(headers: dict) -> list[dict]:
    """Read every open account order, including pending entries and stop legs."""
    orders: list[dict] = []
    cursor = None
    for _ in range(100):
        params = {"status": "open", "limit": 500, "direction": "asc"}
        if cursor:
            params["after_order_id"] = cursor
        response = requests.get(f"{API_BASE}/orders", headers=headers, params=params, timeout=10)
        response.raise_for_status()
        page = response.json()
        if not isinstance(page, list):
            raise ValueError("Alpaca open orders response was not a list")
        orders.extend(page)
        if len(page) < 500:
            return orders
        cursor = str(page[-1].get("id") or "")
        if not cursor:
            raise ValueError("Alpaca open order page omitted its cursor")
    raise RuntimeError("Alpaca open order list exceeded 100 pages")


def _list_positions(headers: dict) -> list[dict]:
    response = requests.get(f"{API_BASE}/positions", headers=headers, timeout=10)
    response.raise_for_status()
    positions = response.json()
    if not isinstance(positions, list):
        raise ValueError("Alpaca positions response was not a list")
    return positions


def _open_order_ids(orders: list[dict]) -> list[str]:
    ids = []
    for order in orders:
        order_id = str(order.get("id") or "")
        if not order_id:
            raise ValueError("Alpaca open order has no ID")
        ids.append(order_id)
    return ids


def _cancel_orders(headers: dict, orders: list[dict]) -> tuple[list[str], list[str]]:
    cancelled, errors = [], []
    for order_id in _open_order_ids(orders):
        try:
            response = requests.delete(f"{API_BASE}/orders/{order_id}", headers=headers, timeout=10)
            # 404/422 can mean an order filled or finished during cancellation.
            # A fresh open-order query below decides whether it is truly gone.
            if response.status_code in (200, 202, 204, 404, 422):
                cancelled.append(order_id)
            else:
                errors.append(f"Cancel {order_id}: HTTP {response.status_code}")
        except requests.RequestException as exc:
            errors.append(f"Cancel {order_id}: {exc}")
    return cancelled, errors


def _position_qty(position: dict) -> Decimal:
    try:
        quantity = Decimal(str(position.get("qty", "0")))
    except InvalidOperation as exc:
        raise ValueError(f"Invalid position quantity for {position.get('symbol')}") from exc
    if not quantity.is_finite():
        raise ValueError(f"Nonfinite position quantity for {position.get('symbol')}")
    return quantity


def _submit_flatten_order(headers: dict, position: dict) -> dict:
    symbol = str(position["symbol"]).upper()
    quantity = _position_qty(position)
    if not quantity:
        raise ValueError(f"Cannot flatten zero-quantity {symbol} position")
    client_order_id = f"flat-{uuid.uuid4().hex}"
    order = {
        "symbol": symbol,
        "qty": str(abs(quantity)),
        "side": "sell" if quantity > 0 else "buy",
        "type": "market",
        "time_in_force": "day",
        "client_order_id": client_order_id,
    }
    try:
        response = requests.post(f"{API_BASE}/orders", headers=headers, json=order, timeout=10)
    except requests.RequestException as exc:
        # Do not retry an ambiguous POST: it may have reached Alpaca.
        return {"ticker": symbol, "status": "failed", "client_order_id": client_order_id,
                "error": f"Close submission outcome unknown: {exc}"}
    if response.status_code not in (200, 201):
        return {"ticker": symbol, "status": "failed", "client_order_id": client_order_id,
                "error": f"HTTP {response.status_code}: {response.text[:200]}"}
    data = response.json()
    if not data.get("id"):
        return {"ticker": symbol, "status": "failed", "client_order_id": client_order_id,
                "error": "Close order response omitted its Alpaca ID"}
    return {
        "ticker": symbol,
        "side": order["side"],
        "qty": order["qty"],
        "status": "submitted",
        "submission_status": "submitted",
        "broker_status": data.get("status"),
        "order_id": data["id"],
        "client_order_id": client_order_id,
    }


def _get_order_status(headers: dict, order_id: str) -> str:
    response = requests.get(f"{API_BASE}/orders/{order_id}", headers=headers, timeout=10)
    response.raise_for_status()
    return str(response.json().get("status") or "").lower()


def _lookup_client_order(headers: dict, client_order_id: str) -> dict | None:
    response = requests.get(
        f"{API_BASE}/orders:by_client_order_id",
        headers=headers,
        params={"client_order_id": client_order_id},
        timeout=10,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Alpaca client order lookup returned invalid data")
    return data


def _get_nested_order(headers: dict, order_id: str) -> dict:
    response = requests.get(
        f"{API_BASE}/orders/{order_id}",
        headers=headers,
        params={"nested": "true"},
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or str(data.get("id") or "") != str(order_id):
        raise ValueError("Exact nested Alpaca order lookup returned a different order")
    return data


def _prices_equal(left: object, right: object) -> bool:
    try:
        a, b = Decimal(str(left)), Decimal(str(right))
        return a.is_finite() and b.is_finite() and a == b
    except (InvalidOperation, TypeError, ValueError):
        return False


def _replay_matches_order(order: dict, broker: dict, headers: dict) -> bool:
    """Check every material instruction before accepting an idempotent replay."""
    if (
        broker.get("client_order_id") != order.get("client_order_id")
        or str(broker.get("time_in_force") or "").lower() != str(order.get("time_in_force") or "").lower()
    ):
        return False
    if "extended_hours" in order and broker.get("extended_hours") is not order["extended_hours"]:
        return False
    for field in ("stop_price", "trail_percent"):
        if field in order and not _prices_equal(broker.get(field), order[field]):
            return False

    order_class = str(order.get("order_class") or "simple").lower()
    if order_class not in {"bracket", "oco"}:
        return True
    nested = _get_nested_order(headers, str(broker["id"]))
    if (
        str(nested.get("order_class") or "").lower() != order_class
        or str(nested.get("symbol") or "").upper() != str(order["symbol"]).upper()
        or str(nested.get("side") or "").lower() != str(order["side"]).lower()
    ):
        return False
    stop_requested = (order.get("stop_loss") or {}).get("stop_price")
    target_requested = (order.get("take_profit") or {}).get("limit_price")
    legs = nested.get("legs") or []
    if not isinstance(legs, list):
        return False
    if order_class == "oco":
        return (
            _prices_equal(nested.get("limit_price"), target_requested)
            and len(legs) == 1
            and isinstance(legs[0], dict)
            and str(legs[0].get("type") or "").lower() in {"stop", "stop_limit"}
            and _prices_equal(legs[0].get("stop_price"), stop_requested)
        )
    stops = [
        leg for leg in legs
        if isinstance(leg, dict) and str(leg.get("type") or "").lower() in {"stop", "stop_limit"}
    ]
    targets = [
        leg for leg in legs
        if isinstance(leg, dict) and str(leg.get("type") or "").lower() == "limit"
    ]
    return (
        len(stops) == 1 and len(targets) == 1
        and _prices_equal(stops[0].get("stop_price"), stop_requested)
        and _prices_equal(targets[0].get("limit_price"), target_requested)
    )


def _submit_broker_order(order: dict, order_type: str, mode: str | None = None) -> dict:
    """Submit once and use the client ID to resolve duplicate/ambiguous POSTs."""
    mode = mode or _active_mode
    if mode == "algo":
        _verify_algo_execution_lease()
    headers = _headers(mode)
    client_order_id = order.get("client_order_id")
    replayed = False
    try:
        response = requests.post(f"{API_BASE}/orders", headers=headers, json=order, timeout=10)
    except requests.RequestException as exc:
        if not client_order_id:
            return {"success": False, "error": f"Submission outcome unknown: {exc}"}
        try:
            data = _lookup_client_order(headers, client_order_id)
        except (requests.RequestException, ValueError) as lookup_exc:
            return {"success": False, "error": f"Submission outcome unknown; lookup failed: {lookup_exc}"}
        if data is None:
            return {"success": False, "error": f"Submission outcome unknown; retry the same client order ID: {exc}"}
        replayed = True
    else:
        if response.status_code in (200, 201):
            try:
                data = response.json()
            except ValueError as exc:
                if not client_order_id:
                    return {"success": False, "error": f"Submission outcome unknown: {exc}"}
                try:
                    data = _lookup_client_order(headers, client_order_id)
                except (requests.RequestException, ValueError) as lookup_exc:
                    return {"success": False, "error": f"Submission outcome unknown; lookup failed: {lookup_exc}"}
                if data is None:
                    return {"success": False, "error": "Submission outcome unknown; broker lookup found no order"}
                replayed = True
        elif client_order_id and response.status_code in (409, 422):
            try:
                data = _lookup_client_order(headers, client_order_id)
            except (requests.RequestException, ValueError) as exc:
                return {"success": False, "error": f"Duplicate lookup failed: {exc}"}
            if data is None:
                return {"success": False, "error": f"HTTP {response.status_code}: {response.text[:200]}"}
            replayed = True
        else:
            return {"success": False, "error": f"HTTP {response.status_code}: {response.text[:200]}"}

    if not isinstance(data, dict) or not data.get("id"):
        return {"success": False, "error": "Alpaca order response omitted its ID"}
    try:
        identity_matches = (
            str(data.get("symbol", "")).upper() == str(order["symbol"]).upper()
            and str(data.get("side", "")).lower() == str(order["side"]).lower()
            and Decimal(str(data.get("qty") or 0)) == Decimal(str(order["qty"]))
            and str(data.get("type", "")).lower() == str(order["type"]).lower()
            and str(data.get("order_class") or "simple").lower() == str(order.get("order_class") or "simple").lower()
            and (
                order.get("limit_price") is None
                or Decimal(str(data.get("limit_price") or 0)) == Decimal(str(order["limit_price"]))
            )
        )
    except (InvalidOperation, TypeError, ValueError):
        identity_matches = False
    if not identity_matches:
        return {
            "success": False, "order_id": data["id"], "broker_status": data.get("status"),
            "error": "Client order ID belongs to a different or unverifiable broker order",
        }
    if replayed:
        try:
            replay_matches = _replay_matches_order(order, data, headers)
        except (requests.RequestException, ValueError, KeyError) as exc:
            return {
                "success": False, "order_id": data["id"], "broker_status": data.get("status"),
                "error": f"Could not verify replayed broker order instructions: {exc}",
            }
        if not replay_matches:
            return {
                "success": False, "order_id": data["id"], "broker_status": data.get("status"),
                "error": "Client order ID belongs to different broker order instructions",
            }
    broker_status = str(data.get("status") or "").lower()
    if not broker_status:
        return {"success": False, "error": "Alpaca order response omitted its status"}
    result = {
        "success": broker_status not in {"rejected", "canceled", "expired"},
        "order_id": data["id"],
        "broker_status": broker_status,
        "order_type": order_type,
        "filled_qty": data.get("filled_qty"),
        "filled_avg_price": data.get("filled_avg_price"),
        "filled_at": data.get("filled_at"),
        "idempotent_replay": replayed,
    }
    if not result["success"]:
        result["error"] = f"Broker order is {broker_status}; use a new decision ID if retrying intentionally"
    return result


def _verify_algo_execution_lease() -> None:
    """Permit Algo writes only from the currently leased coordinator process."""
    from src.algo.state_store import AlgoStateStore

    state_path = Path(ALGO_SERVICE_CONFIG["state_file"])
    if not state_path.is_absolute():
        state_path = Path(__file__).parent / state_path
    token = os.environ.get("SWARM_ALGO_COORDINATOR_TOKEN")
    if not AlgoStateStore(state_path).verify_coordinator_token(token, os.getppid()):
        raise PermissionError("Live Algo orders require the active coordinator lease")


def _flatten_all_unlocked(
    dry_run: bool = False,
    mode: str = "day",
    poll_attempts: int = 10,
    poll_interval: float = 1.0,
) -> dict:
    """Cancel all account orders, close residual positions, verify both are gone.

    Must be called while holding the account execution lock. If a close POST
    has an ambiguous result, this run only reconciles; the next scheduled run
    can retry after reading fresh broker state.
    """
    headers = _headers(mode)
    results: list[dict] = []
    errors: list[str] = []
    cancelled_ids: list[str] = []
    positions: list[dict] = []
    open_orders: list[dict] = []
    initial_count = 0

    def report(status: str) -> dict:
        remaining = []
        for position in positions:
            try:
                quantity = _position_qty(position)
            except ValueError:
                quantity = "invalid"
            if quantity != 0:
                remaining.append({"ticker": str(position.get("symbol") or ""), "qty": str(quantity)})
        open_ids = [str(order.get("id") or "unknown") for order in open_orders]
        verified_flat = not remaining and not open_ids and status in ("flattened", "already_flat")
        recovery_recorded = False
        if verified_flat and mode == "day" and not dry_run:
            try:
                record_day_recovery(get_account_for_mode("day").account_id)
                recovery_recorded = True
            except Exception as exc:
                errors.append(f"Could not persist Day recovery receipt: {exc}")
        return {
            "timestamp": datetime.now(MARKET_TZ).isoformat(),
            "trading_mode": mode,
            "mode": "dry_run" if dry_run else "live",
            "status": status,
            "success": verified_flat and (mode != "day" or recovery_recorded),
            "verified_flat": verified_flat,
            "day_recovery_recorded": recovery_recorded,
            "positions_closed": max(0, initial_count - len(remaining)),
            "positions_remaining": remaining,
            "open_orders_remaining": open_ids,
            "orders_cancelled": cancelled_ids,
            "results": results,
            "errors": errors,
        }

    try:
        if not dry_run:
            if mode == "algo":
                _verify_algo_execution_lease()
            verify_account_for_mode(mode)
        positions = _list_positions(headers)
        open_orders = _list_open_orders(headers)
        initial_count = sum(_position_qty(p) != 0 for p in positions)
        if dry_run:
            results.extend({
                "ticker": p["symbol"],
                "side": "sell" if _position_qty(p) > 0 else "buy",
                "qty": str(abs(_position_qty(p))),
                "status": "would_flatten",
            } for p in positions if _position_qty(p) != 0)
            return report("dry_run")

        if mode == "day" and any(_position_qty(p) != 0 for p in positions):
            # The guard also runs after the bell. A Day holding cannot be
            # market-closed then, so keep its existing stop/order protection
            # intact and alert. Checking only after cancellation would strip
            # that protection while leaving the holding overnight.
            risk = get_mode_config(mode)["risk"]
            schedule = get_day_schedule(
                flatten_by=risk.get("flatten_by", "15:45"),
                flatten_lead_minutes=risk.get("flatten_lead_minutes", 15),
                entry_cutoff_minutes=risk.get("entry_cutoff_minutes", 15),
            )
            if schedule is None or not schedule.market_open(datetime.now(MARKET_TZ)):
                errors.append("Day market is closed; existing position orders were left intact")
                return report("failed")

        # Open buys can create a new position after liquidation; protective
        # sell legs can block a close. Cancel every order in this dedicated
        # mode account, then wait for broker reconciliation before closing.
        for attempt in range(poll_attempts):
            if not open_orders:
                break
            cancelled, cancel_errors = _cancel_orders(headers, open_orders)
            cancelled_ids.extend(cancelled)
            errors.extend(cancel_errors)
            if attempt + 1 < poll_attempts:
                time.sleep(poll_interval)
            open_orders = _list_open_orders(headers)
        if open_orders:
            errors.append("Open orders remained after cancellation retries")
            positions = _list_positions(headers)
            return report("failed")

        # Cancellations can fill during the race, so read positions again.
        positions = _list_positions(headers)
        if not any(_position_qty(p) != 0 for p in positions):
            return report("already_flat" if initial_count == 0 else "flattened")

        if mode == "day":
            risk = get_mode_config(mode)["risk"]
            schedule = get_day_schedule(
                flatten_by=risk.get("flatten_by", "15:45"),
                flatten_lead_minutes=risk.get("flatten_lead_minutes", 15),
                entry_cutoff_minutes=risk.get("entry_cutoff_minutes", 15),
            )
            if schedule is None or not schedule.market_open(datetime.now(MARKET_TZ)):
                errors.append("Day market is closed; a market close cannot be verified before the bell")
                return report("failed")

        for position in positions:
            if _position_qty(position) != 0:
                results.append(_submit_flatten_order(headers, position))

        for attempt in range(poll_attempts):
            if attempt:
                time.sleep(poll_interval)
            positions = _list_positions(headers)
            open_orders = _list_open_orders(headers)
            if not any(_position_qty(p) != 0 for p in positions) and not open_orders:
                return report("flattened")
            for result in results:
                order_id = result.get("order_id")
                if order_id:
                    try:
                        result["broker_status"] = _get_order_status(headers, order_id)
                    except requests.RequestException as exc:
                        errors.append(f"Could not confirm close order {order_id}: {exc}")
        errors.append("Broker did not confirm zero positions and zero open orders")
        return report("failed")
    except (requests.RequestException, ValueError, KeyError, TimeoutError) as exc:
        errors.append(str(exc))
        return report("failed")


def flatten_all(
    dry_run: bool = False,
    mode: str = None,
    poll_attempts: int = 10,
    poll_interval: float = 1.0,
) -> dict:
    """Idempotent account-wide liquidation for a dedicated trading mode."""
    mode = mode or _active_mode
    with account_execution_lock(mode):
        return _flatten_all_unlocked(dry_run, mode, poll_attempts, poll_interval)


def place_order(
    ticker, action, qty,
    order_type="market",
    stop_price=None, take_profit=None,
    limit_price=None, trail_percent=None,
    entry_price=None, stop_pct=None, client_order_id=None,
    extended_hours=False,
):
    """Place an order. Supports market, bracket, limit, stop, oco, trailing_stop.

    Auto-enforces bracket on buy/short orders if stop_pct is provided and
    stop_price is missing. In day mode, stop_pct is not passed so no auto-bracket.

    Order types:
        market          — immediate fill (default)
        limit           — enter at specific price (requires limit_price)
        bracket         — entry + stop-loss + take-profit atomic (requires stop_price + take_profit)
        stop            — standalone stop order on existing position (requires stop_price)
        oco             — exit-only: stop + take-profit on existing position (requires stop_price + take_profit)
        trailing_stop   — trailing stop that rises with price (requires trail_percent)
    """
    if action not in {"buy", "sell", "short", "cover"}:
        return {"success": False, "error": f"Unsupported trade action: {action}"}
    if order_type not in {"market", "limit", "bracket", "stop", "oco", "trailing_stop"}:
        return {"success": False, "error": f"Unsupported order type: {order_type}"}
    if action == "short":
        return {"success": False, "error": "New short entries are disabled"}
    if action == "cover" and order_type not in {"market", "limit"}:
        return {"success": False, "error": "Cover exits support market or limit orders only"}
    if order_type == "oco" and action != "sell":
        return {"success": False, "error": "OCO protection requires a position-reducing sell"}
    if action != "buy" and (
        order_type == "bracket"
        or (stop_price is not None and take_profit is not None and order_type != "oco")
    ):
        return {"success": False, "error": "Entry brackets require a buy; use OCO for an existing long position"}
    if order_type == "market" and limit_price is not None:
        return {"success": False, "error": "Market orders cannot include a limit price"}
    if order_type in {"stop", "trailing_stop"} and (
        take_profit is not None or limit_price is not None
        or (order_type == "stop" and trail_percent is not None)
        or (order_type == "trailing_stop" and stop_price is not None)
    ):
        return {"success": False, "error": "Stop and trailing-stop orders cannot include bracket or limit fields"}
    if order_type == "oco" and (limit_price is not None or trail_percent is not None):
        return {"success": False, "error": "OCO take-profit and stop prices must use their dedicated fields"}
    if _active_mode == "day" and action == "buy":
        try:
            risk = get_mode_config("day")["risk"]
            schedule = get_day_schedule(
                flatten_by=risk.get("flatten_by", "15:45"),
                flatten_lead_minutes=risk.get("flatten_lead_minutes", 15),
                entry_cutoff_minutes=risk.get("entry_cutoff_minutes", 15),
            )
        except Exception as exc:
            return {"success": False, "error": f"Day calendar unavailable: {exc}"}
        if schedule is None or not schedule.entries_allowed(datetime.now(MARKET_TZ)):
            return {"success": False, "error": "Day entry cutoff has passed or market is closed"}
        if not day_recovery_confirmed(
            get_account_for_mode("day").account_id,
            session_open_at=schedule.opens_at,
        ):
            return {"success": False, "error": "Today's Day opening cleanup has not verified a flat account"}

    side = "buy" if action in ("buy", "cover") else "sell"

    # Alpaca extended sessions accept simple limit DAY orders only. Keep this
    # branch ahead of bracket construction so strategy stop/target metadata
    # remains software-managed and never becomes an unsupported bracket.
    if extended_hours:
        if _active_mode != "algo":
            return {"success": False, "error": "Extended-hours orders are enabled only for Algo mode"}
        if action not in {"buy", "sell"} or order_type != "limit" or limit_price is None or float(limit_price) <= 0:
            return {"success": False, "error": "Extended-hours orders require a buy/sell limit order with a positive limit price"}
        order = {
            "symbol": ticker,
            "qty": str(int(qty)),
            "side": side,
            "type": "limit",
            "time_in_force": "day",
            "limit_price": str(round(float(limit_price), 2)),
            "extended_hours": True,
        }
        if client_order_id:
            order["client_order_id"] = str(client_order_id)[:48]
        return {**_submit_broker_order(order, "limit"), "extended_hours": True}

    if _active_mode == "algo" and order_type == "oco":
        # A replacement OCO is a new claim on the entire live holding.
        # Verify the broker position and all symbol orders immediately before
        # its POST, while the canonical executor holds the account lock.
        try:
            schedule = get_day_schedule(flatten_by="15:45", mode="algo")
            if schedule is None or not schedule.market_open(datetime.now(MARKET_TZ)):
                return {"success": False, "error": "Algo OCO repair requires regular market hours"}
            headers = _headers("algo")
            matches = [
                row for row in _list_positions(headers)
                if str(row.get("symbol", "")).upper() == str(ticker).upper()
            ]
            if len(matches) != 1 or _position_qty(matches[0]) != Decimal(str(qty)):
                return {"success": False, "error": "Algo OCO repair quantity no longer matches the broker holding"}
            open_symbol_orders = [
                row for row in _list_open_orders(headers)
                if str(row.get("symbol", "")).upper() == str(ticker).upper()
            ]
            if open_symbol_orders:
                return {"success": False, "error": "Algo OCO repair found an open broker order for this symbol"}
        except (requests.RequestException, ValueError, InvalidOperation) as exc:
            return {"success": False, "error": f"Algo OCO repair preflight unavailable: {exc}"}

    # Auto-calculate bracket prices if stop_pct is provided and prices are missing
    if action == "buy" and order_type not in ("limit", "stop", "trailing_stop", "oco"):
        ref_price = entry_price or limit_price or 0
        if ref_price > 0 and stop_pct is not None:
            if stop_price is None:
                stop_price = round(ref_price * (1 - stop_pct), 2)
            if take_profit is None and stop_price is not None:
                stop_dist = abs(ref_price - float(stop_price))
                take_profit = round(ref_price + stop_dist * DEFAULT_TARGET_MULTIPLIER, 2)

    if order_type in {"market", "limit", "bracket"}:
        if (stop_price is None) != (take_profit is None):
            return {"success": False, "error": "Entry protection requires both a stop and a take-profit price"}
        if trail_percent is not None:
            return {"success": False, "error": "Entry bracket orders cannot include a trailing-stop field"}

    required_fields = {
        "bracket": (stop_price, take_profit),
        "oco": (stop_price, take_profit),
        "limit": (limit_price,),
        "stop": (stop_price,),
        "trailing_stop": (trail_percent,),
    }
    if order_type in required_fields and any(value is None for value in required_fields[order_type]):
        return {"success": False, "error": f"{order_type} order is missing a required price"}

    use_bracket = action == "buy" and order_type in {"market", "limit", "bracket"} and (
        order_type == "bracket"
        or (stop_price is not None and take_profit is not None)
    )

    if use_bracket:
        entry_limit = limit_price is not None
        if entry_limit:
            try:
                numeric_limit = float(limit_price)
            except (TypeError, ValueError):
                return {"success": False, "error": "Bracket entry limit price must be positive and finite"}
            if not math.isfinite(numeric_limit) or numeric_limit <= 0:
                return {"success": False, "error": "Bracket entry limit price must be positive and finite"}
        order = {
            "symbol": ticker,
            "qty": str(int(qty)),
            "side": side,
            "type": "limit" if entry_limit else "market",
            "time_in_force": "gtc",
            "order_class": "bracket",
            "stop_loss": {"stop_price": str(round(float(stop_price), 2))},
            "take_profit": {"limit_price": str(round(float(take_profit), 2))},
        }
        if entry_limit:
            order["limit_price"] = str(round(float(limit_price), 2))
    elif order_type == "oco" and stop_price is not None and take_profit is not None:
        order = {
            "symbol": ticker,
            "qty": str(int(qty)),
            "side": side,
            "type": "limit",
            "time_in_force": "gtc",
            "order_class": "oco",
            "stop_loss": {"stop_price": str(round(float(stop_price), 2))},
            "take_profit": {"limit_price": str(round(float(take_profit), 2))},
        }
    elif order_type == "limit" and limit_price is not None:
        order = {
            "symbol": ticker,
            "qty": str(int(qty)),
            "side": side,
            "type": "limit",
            "time_in_force": "day",
            "limit_price": str(round(float(limit_price), 2)),
        }
    elif order_type == "stop" and stop_price is not None:
        order = {
            "symbol": ticker,
            "qty": str(int(qty)),
            "side": side,
            "type": "stop",
            "time_in_force": "gtc",
            "stop_price": str(round(float(stop_price), 2)),
        }
    elif order_type == "trailing_stop" and trail_percent is not None:
        order = {
            "symbol": ticker,
            "qty": str(int(qty)),
            "side": side,
            "type": "trailing_stop",
            "time_in_force": "gtc",
            "trail_percent": str(round(float(trail_percent), 2)),
        }
    else:
        order = {
            "symbol": ticker,
            "qty": str(int(qty)),
            "side": side,
            "type": "market",
            "time_in_force": "day",
        }

    if client_order_id:
        order["client_order_id"] = str(client_order_id)[:48]

    result = _submit_broker_order(order, order_type)
    if use_bracket:
        result["order_class"] = "bracket"
        result["stop_price"] = stop_price
        result["take_profit"] = take_profit
    elif order_type == "oco":
        result["order_class"] = "oco"
    return result


def cancel_open_orders_for_symbol(ticker: str, mode: str = None) -> dict:
    """Cancel and confirm all symbol orders before a strategy market exit."""
    mode = mode or _active_mode
    headers = _headers(mode)
    cancelled: list[str] = []
    errors: list[str] = []
    last_matched: list[dict] = []
    try:
        for attempt in range(6):
            matched = [
                order for order in _list_open_orders(headers)
                if str(order.get("symbol", "")).upper() == ticker.upper()
            ]
            if not matched:
                return {"success": True, "cancelled": cancelled}
            last_matched = matched
            if mode == "algo" and any(
                str(order.get("side", "")).lower() == "sell"
                and (
                    str(order.get("type", "")).lower() in {"stop", "stop_limit", "trailing_stop"}
                    or str(order.get("order_class", "")).lower() in {"bracket", "oco"}
                    or order.get("parent_order_id")
                )
                for order in matched
            ):
                return {
                    "success": False,
                    "error": (
                        f"Cannot cancel {ticker} broker protective orders for a software exit: "
                        f"{[(order.get('id'), order.get('status')) for order in matched]}"
                    ),
                    "cancelled": cancelled,
                }
            # Alpaca may hold a routed order in pending_cancel until its
            # venue acknowledges it. More DELETEs cannot complete that state.
            to_cancel = [
                order for order in matched
                if str(order.get("status", "")).lower() != "pending_cancel"
            ]
            if to_cancel:
                latest_cancelled, latest_errors = _cancel_orders(headers, to_cancel)
                cancelled.extend(latest_cancelled)
                errors.extend(latest_errors)
            if attempt < 5:
                time.sleep(0.5)
        return {
            "success": False,
            "error": (
                f"Open {ticker} orders remained after cancellation: "
                f"{[(order.get('id'), order.get('status')) for order in last_matched]}; "
                f"cancel errors: {errors}"
            ),
            "cancelled": cancelled,
        }
    except (requests.RequestException, ValueError) as exc:
        return {"success": False, "error": str(exc), "cancelled": cancelled}


def validate_trade_legacy(ticker, action, qty, positions, portfolio_value, daily_loss_limit=0.03, max_trade_pct=0.15):
    """Legacy position-level validation (sell/cover checks + portfolio-level circuit breaker)."""
    pos = positions.get(ticker, {})
    current_price = float(pos.get("current_price", 0))
    current_shares = float(pos.get("qty", 0))

    if action == "sell" and current_shares <= 0:
        return False, f"No long position in {ticker} to sell"
    if action == "sell" and qty > current_shares:
        return False, f"Sell quantity {qty} exceeds the {current_shares:g} shares currently held in {ticker}"
    if action == "cover" and current_shares >= 0:
        return False, f"No short position in {ticker} to cover"
    if action == "cover" and qty > abs(current_shares):
        return False, f"Cover quantity {qty} exceeds the {abs(current_shares):g} short shares in {ticker}"
    if action == "short":
        return False, "New short entries are disabled"

    daily_pnl_pct = 0.0  # circuit breaker is now handled by V2 risk manager
    if action in ("buy", "short") and daily_pnl_pct <= -daily_loss_limit:
        return False, (
            f"Circuit breaker: down {abs(daily_pnl_pct)*100:.1f}% today "
            f"(limit {daily_loss_limit*100:.0f}%). No new entries until tomorrow."
        )

    if action in ("buy", "short") and current_price > 0:
        trade_value = qty * current_price
        max_value = portfolio_value * max_trade_pct
        if trade_value > max_value:
            return False, (
                f"Trade value ${trade_value:,.0f} exceeds max ${max_value:,.0f} "
                f"({max_trade_pct*100:.0f}% of portfolio)"
            )

    return True, ""


def main():
    parser = argparse.ArgumentParser(description="Execute Agent's trade decisions")
    parser.add_argument("--file", type=str, help="JSON file with trade decisions")
    parser.add_argument("--dry-run", action="store_true", help="Validate but don't execute")
    parser.add_argument(
        "--flatten",
        action="store_true",
        help="Market-sell all open positions (end-of-day flatten). Ignores --file.",
    )
    parser.add_argument(
        "--flatten-if-due",
        action="store_true",
        help="Day guard: flatten at the broker-calendar deadline, otherwise no-op.",
    )
    parser.add_argument(
        "--mode",
        choices=["swing", "day", "algo"],
        default=None,
        help="Trading mode (overrides TRADING_MODE env, default: swing)",
    )
    args = parser.parse_args()

    # Resolve mode: CLI flag > env var > default
    mode = args.mode or os.environ.get("TRADING_MODE", "swing")
    if args.flatten_if_due and mode != "day":
        parser.error("--flatten-if-due requires --mode day")
    if args.flatten and args.flatten_if_due:
        parser.error("Choose --flatten or --flatten-if-due")
    with account_execution_lock(mode):
        return _run_main(args, mode)


def _run_main(args: argparse.Namespace, mode: str) -> int:
    mode_config = get_mode_config(mode)
    mode_risk = mode_config["risk"]

    # Set module-level mode so all API helpers route to the correct account
    global _active_mode
    _active_mode = mode

    acct = get_account_for_mode(mode)
    log.info(f"Trading mode: {mode.upper()} — {mode_config['label']} (account: {acct.name})")

    # Confirm the credentials resolve to the configured, active paper account
    # before any live order. Keep the broker payload for the risk snapshot.
    broker_account = None
    if not args.dry_run:
        if mode == "algo":
            _verify_algo_execution_lease()
        _, broker_account = verify_account_for_mode(mode)

    if args.flatten_if_due:
        try:
            schedule = get_day_schedule(
                flatten_by=mode_risk.get("flatten_by", "15:45"),
                flatten_lead_minutes=mode_risk.get("flatten_lead_minutes", 15),
                entry_cutoff_minutes=mode_risk.get("entry_cutoff_minutes", 15),
            )
            if schedule is not None and not schedule.flatten_due(datetime.now(MARKET_TZ)):
                result = {
                    "timestamp": datetime.now(MARKET_TZ).isoformat(),
                    "trading_mode": mode,
                    "status": "not_due",
                    "flatten_at": schedule.flatten_at.isoformat(),
                    "success": True,
                }
                print(json.dumps(result, indent=2))
                return 0
        except Exception as exc:
            print(json.dumps({"trading_mode": mode, "status": "failed", "errors": [f"Day calendar unavailable: {exc}"]}, indent=2))
            return 2
        result = _flatten_all_unlocked(dry_run=args.dry_run, mode=mode)
        print(json.dumps(result, indent=2))
        return 0 if args.dry_run or result["success"] else 2

    # Handle flatten command separately (works in both modes)
    if args.flatten:
        result = _flatten_all_unlocked(dry_run=args.dry_run, mode=mode)
        print(json.dumps(result, indent=2))
        return 0 if args.dry_run or result["success"] else 2

    # Read decisions
    if args.file:
        with open(args.file) as f:
            decisions = json.load(f)
    else:
        decisions = json.load(sys.stdin)

    trades = decisions.get("trades", [])
    if not trades:
        print(json.dumps({"status": "no_trades", "message": "No trades to execute"}))
        return 0
    decision_id = decisions.get("decision_id") or decisions.get("timestamp") or hashlib.sha256(
        json.dumps(decisions, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    decision_digest = hashlib.sha256(str(decision_id).encode("utf-8")).hexdigest()[:16]

    # Get current state
    account = broker_account or get_account()
    positions = get_positions()
    portfolio_value = float(account.get("equity", 0))
    daily_pnl_pct = get_daily_pnl(account)

    daily_loss_limit = mode_risk["daily_loss_limit"]
    max_trade_pct = mode_risk["max_position_pct"]
    stop_loss_pct = mode_risk["stop_loss_pct"]

    if daily_pnl_pct <= -daily_loss_limit:
        log.warning(
            f"Circuit breaker ACTIVE: down {abs(daily_pnl_pct)*100:.1f}% today "
            f"(limit {daily_loss_limit*100:.0f}%). New buy/short entries blocked."
        )

    # Pre-fetch V2 portfolio state once (shared across all trade validations)
    rm_portfolio_state = None
    if RISK_MANAGER_AVAILABLE:
        try:
            rm_portfolio_state = rm_get_portfolio_state(mode=mode)
            log.info("V2 risk manager portfolio state loaded")
        except Exception as e:
            log.warning(f"Could not pre-fetch V2 portfolio state: {e}. Will validate per-trade.")

    results = []
    submitted_count = 0
    would_execute_count = 0
    seen_client_order_ids: set[str] = set()

    # The risk manager counts and validates entries against staged portfolio
    # state. Do not truncate this list: exits later in the batch must still be
    # allowed after the day's entry quota is full.
    for trade_index, trade in enumerate(trades):
        ticker = trade["ticker"]
        action = trade["action"].lower()
        qty = int(trade.get("qty", 0))
        reasoning = trade.get("reasoning", "")
        order_type = trade.get("order_type", "market")
        stop_price = trade.get("stop_price")
        take_profit = trade.get("take_profit")
        limit_price = trade.get("limit_price")
        trail_percent = trade.get("trail_percent")
        extended_hours = bool(trade.get("extended_hours", False))
        entry_price = trade.get("entry_price")
        strategy = trade.get("strategy", "unspecified")
        strategy_group = trade.get("strategy_group")
        client_order_id = str(trade.get("client_order_id") or (
            f"sw-{mode[:2]}-{datetime.now(MARKET_TZ).date():%Y%m%d}-{trade_index}-{decision_digest}"
        ))[:48]

        if client_order_id in seen_client_order_ids:
            results.append({
                "ticker": ticker, "action": action, "qty": qty,
                "status": "blocked", "reason": "Duplicate client order ID in decision batch",
                "rule": "duplicate_client_order_id", "strategy": strategy,
                "client_order_id": client_order_id,
            })
            continue
        seen_client_order_ids.add(client_order_id)

        if action == "hold" or qty <= 0:
            results.append({"ticker": ticker, "action": action, "status": "skipped", "reason": "Hold or zero qty"})
            continue

        if not RISK_MANAGER_AVAILABLE:
            reason = "V2 risk manager unavailable; order blocked"
            log.error(reason)
            results.append({
                "ticker": ticker, "action": action, "qty": qty,
                "status": "blocked", "reason": reason, "strategy": strategy,
            })
            continue

        # ── V2 Risk Manager validation ───────────────────────────────────────
        if action in ("buy", "short") and RISK_MANAGER_AVAILABLE:
            pos = positions.get(ticker, {})
            try:
                hinted_price = float(entry_price or 0)
            except (TypeError, ValueError):
                hinted_price = 0.0
            if not math.isfinite(hinted_price) or hinted_price <= 0:
                hinted_price = 0.0
            try:
                limit_reference = float(limit_price or 0)
            except (TypeError, ValueError):
                limit_reference = 0.0
            if not math.isfinite(limit_reference) or limit_reference <= 0:
                limit_reference = 0.0
            has_buy_limit = order_type == "limit" or (
                order_type == "bracket" and limit_price is not None
            )
            if not args.dry_run and action == "buy" and has_buy_limit:
                # A buy limit caps the actual fill price. Use that ceiling
                # even when the signal supplies a much lower stale mark.
                if limit_reference <= 0:
                    results.append({
                        "ticker": ticker, "action": action, "qty": qty,
                        "status": "blocked", "reason": "Live buy limit price is missing or invalid",
                        "rule": "invalid_buy_limit", "strategy": strategy,
                    })
                    continue
                ref_price = max(hinted_price, limit_reference)
                entry_price = limit_reference
            elif not args.dry_run and action == "buy":
                try:
                    live_ask = get_latest_ask(ticker, mode=mode)
                except Exception as exc:
                    results.append({
                        "ticker": ticker, "action": action, "qty": qty,
                        "status": "blocked", "reason": f"Fresh broker ask unavailable: {exc}",
                        "rule": "fresh_ask_unavailable", "strategy": strategy,
                    })
                    continue
                ref_price = max(hinted_price, live_ask)
                entry_price = ref_price
            else:
                ref_price = max(
                    hinted_price, limit_reference, float(pos.get("current_price", 0) or 0),
                )
                if ref_price <= 0:
                    try:
                        ref_price = get_latest_price(ticker)
                    except Exception as exc:
                        ref_price = 0.0
                        log.warning("No reference price for %s: %s", ticker, exc)
            rm_result = rm_validate_trade(
                ticker=ticker,
                action=action,
                qty=qty,
                entry_price=ref_price,
                portfolio_state=rm_portfolio_state,
                mode=mode,
            )
            if not rm_result.approved:
                log.warning(f"V2 risk manager BLOCKED {action} {ticker}: {rm_result.reason}")
                results.append({
                    "ticker": ticker, "action": action, "qty": qty,
                    "status": "blocked",
                    "reason": rm_result.reason,
                    "rule": rm_result.rule,
                    "strategy": strategy,
                })
                continue
            log.info(f"V2 risk manager approved {action} {ticker}")
        elif action in ("sell", "cover") and RISK_MANAGER_AVAILABLE:
            # The central validator explicitly allows exits; route them through
            # it too so every submitted signal uses the same policy boundary.
            rm_result = rm_validate_trade(
                ticker=ticker,
                action=action,
                qty=qty,
                entry_price=float(entry_price or 0),
                portfolio_state=rm_portfolio_state,
                mode=mode,
            )
            if not rm_result.approved:
                results.append({
                    "ticker": ticker, "action": action, "qty": qty,
                    "status": "blocked", "reason": rm_result.reason,
                    "rule": rm_result.rule, "strategy": strategy,
                })
                continue
        elif action not in ("sell", "cover"):
            reason = f"Unsupported action '{action}'"
            results.append({
                "ticker": ticker, "action": action, "qty": qty,
                "status": "blocked", "reason": reason, "strategy": strategy,
            })
            continue

        # ── Auto-calculate bracket for swing mode (mandatory stops) ──────────
        # In day mode: brackets are not required — stops managed intraday / via EOD flatten.
        # If the trade JSON explicitly includes stop_price, it will still be used.
        if mode == "swing" and action in ("buy", "cover", "short") and order_type not in ("limit", "stop", "trailing_stop", "oco"):
            pos = positions.get(ticker, {})
            ref = entry_price or limit_price or float(pos.get("current_price", 0))
            if ref > 0:
                if stop_price is None:
                    if action in ("buy", "cover"):
                        stop_price = round(ref * (1 - stop_loss_pct), 2)
                    else:  # short
                        stop_price = round(ref * (1 + stop_loss_pct), 2)
                if take_profit is None and stop_price is not None:
                    stop_dist = abs(ref - float(stop_price))
                    if action in ("buy", "cover"):
                        take_profit = round(ref + stop_dist * DEFAULT_TARGET_MULTIPLIER, 2)
                    else:
                        take_profit = round(ref - stop_dist * DEFAULT_TARGET_MULTIPLIER, 2)

            if action == "buy" and stop_price is not None:
                log.info(
                    f"BUY {ticker}: bracket order with stop=${stop_price:.2f} "
                    f"(-{stop_loss_pct*100:.1f}%), target=${take_profit:.2f}"
                )

        # Legacy validate_trade (sell/cover position checks)
        valid, reason = validate_trade_legacy(
            ticker, action, qty, positions, portfolio_value,
            daily_loss_limit=daily_loss_limit,
            max_trade_pct=max_trade_pct,
        )
        if not valid:
            log.warning(f"Legacy validator BLOCKED {action} {ticker}: {reason}")
            results.append({"ticker": ticker, "action": action, "qty": qty, "status": "blocked", "reason": reason, "strategy": strategy})
            continue

        if args.dry_run:
            dry_entry = {
                "ticker": ticker, "action": action, "qty": qty,
                "status": "would_execute", "reasoning": reasoning,
                "order_type": order_type,
                "extended_hours": extended_hours,
                "strategy": strategy,
                "client_order_id": client_order_id,
            }
            if strategy_group:
                dry_entry["strategy_group"] = strategy_group
            if stop_price is not None:
                dry_entry["stop_price"] = stop_price
            if take_profit is not None:
                dry_entry["take_profit"] = take_profit
            if limit_price is not None:
                dry_entry["limit_price"] = limit_price
            if trail_percent is not None:
                dry_entry["trail_percent"] = trail_percent
            if trade.get("cancel_open_orders"):
                dry_entry["cancel_open_orders"] = True
            results.append(dry_entry)
            would_execute_count += 1
            if action == "buy" and RISK_MANAGER_AVAILABLE and rm_portfolio_state is not None:
                rm_portfolio_state = rm_stage_approved_trade(
                    rm_portfolio_state, ticker, action, qty,
                    float(ref_price),
                    mode=mode,
                )
        else:
            if action in {"sell", "cover"} and (
                trade.get("cancel_open_orders") or order_type in {"market", "limit"}
            ):
                cancel_result = cancel_open_orders_for_symbol(ticker, mode=mode)
                if not cancel_result.get("success"):
                    results.append({
                        "ticker": ticker, "action": action, "qty": qty,
                        "status": "failed", "reason": cancel_result.get("error"),
                        "strategy": strategy, "client_order_id": client_order_id,
                    })
                    continue
                if cancel_result.get("cancelled"):
                    log.info("Cancelled %d open order(s) for %s before strategy exit", len(cancel_result["cancelled"]), ticker)
                # An order may fill while being cancelled. Never use the
                # earlier position quantity to submit a reducing order.
                try:
                    positions = get_positions()
                except Exception as exc:
                    results.append({
                        "ticker": ticker, "action": action, "qty": qty,
                        "status": "failed", "reason": f"Could not reconcile exit position: {exc}",
                        "strategy": strategy, "client_order_id": client_order_id,
                    })
                    continue
                valid, reason = validate_trade_legacy(
                    ticker, action, qty, positions, portfolio_value,
                    daily_loss_limit=daily_loss_limit,
                    max_trade_pct=max_trade_pct,
                )
                if not valid:
                    results.append({
                        "ticker": ticker, "action": action, "qty": qty,
                        "status": "blocked", "reason": reason,
                        "strategy": strategy, "client_order_id": client_order_id,
                    })
                    continue
            # Pass stop_pct only in swing mode so place_order() can auto-calc if needed
            result = place_order(
                ticker, action, qty,
                order_type=order_type,
                stop_price=stop_price, take_profit=take_profit,
                limit_price=limit_price, trail_percent=trail_percent,
                entry_price=entry_price,
                stop_pct=stop_loss_pct if mode == "swing" else None,
                client_order_id=client_order_id,
                extended_hours=extended_hours,
            )
            status = "submitted" if result["success"] else "failed"
            results.append({
                "ticker": ticker, "action": action, "qty": qty,
                "status": status, "submission_status": status,
                "reasoning": reasoning, "strategy": strategy,
                "strategy_group": strategy_group,
                "client_order_id": client_order_id,
                **result,
            })
            if result["success"]:
                if not result.get("idempotent_replay"):
                    submitted_count += 1
                if action == "buy" and not result.get("idempotent_replay") and RISK_MANAGER_AVAILABLE and rm_portfolio_state is not None:
                    rm_portfolio_state = rm_stage_approved_trade(
                        rm_portfolio_state, ticker, action, qty,
                        float(ref_price),
                        mode=mode,
                    )

    for row in results:
        row.setdefault("submission_status", row.get("status"))
        row.setdefault("broker_status", None)

    output = {
        "timestamp": datetime.now().isoformat(),
        "trading_mode": mode,
        "account_id": (
            str(broker_account.get("id") or broker_account.get("account_number") or "")
            if broker_account else None
        ),
        "mode": "dry_run" if args.dry_run else "live",
        "daily_pnl_pct": round(daily_pnl_pct * 100, 2),
        "circuit_breaker_active": daily_pnl_pct <= -daily_loss_limit,
        "total_trades": len(trades),
        "submitted": submitted_count,
        "filled_at_submission": sum(
            r.get("broker_status") == "filled" for r in results
        ),
        "would_execute": would_execute_count,
        "blocked": len([r for r in results if r.get("status") == "blocked"]),
        "results": results,
    }

    print(json.dumps(output, indent=2))

    # Auto-log to trade journal
    if not args.dry_run:
        try:
            from trade_journal import append_trades
            logged = append_trades(output)
            print(f"📓 Logged {logged} trades to journal", file=sys.stderr)
        except Exception as e:
            print(f"⚠️ Journal logging failed: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
