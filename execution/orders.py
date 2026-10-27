"""Order validation, Alpaca payload construction, and replay-safe submission."""

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Callable
from zoneinfo import ZoneInfo

import requests

from execution.broker import API_BASE
from market.calendar import extended_session_name


MARKET_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class OrderHooks:
    mode: str
    headers: Callable
    verify_algo_execution_lease: Callable
    lookup_client_order: Callable
    get_nested_order: Callable
    get_mode_config: Callable
    get_market_schedule: Callable
    list_positions: Callable
    position_qty: Callable
    list_open_orders: Callable
    submit_broker_order: Callable


def prices_equal(left: object, right: object) -> bool:
    try:
        a, b = Decimal(str(left)), Decimal(str(right))
        return a.is_finite() and b.is_finite() and a == b
    except (InvalidOperation, TypeError, ValueError):
        return False


def replay_matches_order(
    order: dict,
    broker: dict,
    headers: dict,
    get_nested_order: Callable,
) -> bool:
    """Check every material instruction before accepting an idempotent replay."""
    if broker.get("client_order_id") != order.get("client_order_id") or str(broker.get("time_in_force") or "").lower() != str(order.get("time_in_force") or "").lower() or str(broker.get("position_intent") or "").lower() != str(order.get("position_intent") or "").lower():
        return False
    if "extended_hours" in order and broker.get("extended_hours") is not order["extended_hours"]:
        return False
    for field in ("stop_price", "trail_percent"):
        if field in order and not prices_equal(broker.get(field), order[field]):
            return False

    # Plain orders only: protection is software-managed, so no legs exist.
    return not (order.get("order_class") or order.get("stop_loss") or order.get("take_profit"))


def _is_extended_limit_payload(order: dict) -> bool:
    """Alpaca accepts only plain extended-hours limit orders: DAY or GTC, no bracket or OCO legs."""
    return (
        order.get("extended_hours") is True
        and order.get("type") == "limit"
        and order.get("time_in_force") in {"day", "gtc"}
        and not order.get("order_class")
        and not order.get("stop_loss")
        and not order.get("take_profit")
    )


def submit_broker_order(
    order: dict,
    order_type: str,
    mode: str | None,
    hooks: OrderHooks,
) -> dict:
    """Submit once and use the client ID to resolve duplicate or ambiguous POSTs."""
    mode = mode or hooks.mode
    if mode == "algo":
        hooks.verify_algo_execution_lease()
    headers = hooks.headers(mode)
    client_order_id = order.get("client_order_id")
    replayed = False
    if mode == "algo":
        try:
            now = datetime.now(MARKET_TZ)
            schedule = hooks.get_market_schedule(mode="algo")
            regular_open = schedule is not None and schedule.market_open(now)
            extended_session = None if regular_open else extended_session_name(
                now, lambda on_date: hooks.get_market_schedule(on_date=on_date, mode="algo"),
            )
        except Exception as exc:
            return {
                "success": False,
                "error": f"Algo regular-session verification unavailable: {type(exc).__name__}: {exc}",
            }
        if not regular_open and extended_session is None:
            return {
                "success": False,
                "error": "Algo order submissions are restricted to regular market hours",
            }
        if not regular_open and not _is_extended_limit_payload(order):
            return {
                "success": False,
                "error": "Algo extended-hours orders must be plain limit DAY or GTC orders without broker protection",
            }
    try:
        response = requests.post(f"{API_BASE}/orders", headers=headers, json=order, timeout=10)
    except requests.RequestException as exc:
        if not client_order_id:
            return {"success": False, "error": f"Submission outcome unknown: {exc}"}
        try:
            data = hooks.lookup_client_order(headers, client_order_id)
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
                    data = hooks.lookup_client_order(headers, client_order_id)
                except (requests.RequestException, ValueError) as lookup_exc:
                    return {"success": False, "error": f"Submission outcome unknown; lookup failed: {lookup_exc}"}
                if data is None:
                    return {"success": False, "error": "Submission outcome unknown; broker lookup found no order"}
                replayed = True
        elif client_order_id and response.status_code in (409, 422):
            try:
                data = hooks.lookup_client_order(headers, client_order_id)
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
            and (order.get("limit_price") is None or Decimal(str(data.get("limit_price") or 0)) == Decimal(str(order["limit_price"])))
        )
    except (InvalidOperation, TypeError, ValueError):
        identity_matches = False
    if not identity_matches:
        return {
            "success": False,
            "order_id": data["id"],
            "broker_status": data.get("status"),
            "error": "Client order ID belongs to a different or unverifiable broker order",
        }
    if replayed:
        try:
            replay_matches = replay_matches_order(
                order,
                data,
                headers,
                hooks.get_nested_order,
            )
        except (requests.RequestException, ValueError, KeyError) as exc:
            return {
                "success": False,
                "order_id": data["id"],
                "broker_status": data.get("status"),
                "error": f"Could not verify replayed broker order instructions: {exc}",
            }
        if not replay_matches:
            return {
                "success": False,
                "order_id": data["id"],
                "broker_status": data.get("status"),
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


def place_order(
    ticker,
    action,
    qty,
    hooks: OrderHooks,
    order_type="market",
    stop_price=None,
    take_profit=None,
    limit_price=None,
    trail_percent=None,
    entry_price=None,
    client_order_id=None,
    extended_hours=False,
):
    """Validate an order request, build a plain Alpaca payload, and submit it.

    No bracket, OCO, stop, or trailing-stop order is ever sent. Stop and target
    levels are persisted as software protection and enforced by the daemon.
    """
    mode = hooks.mode
    if action not in {"buy", "sell", "short", "cover"}:
        return {"success": False, "error": f"Unsupported trade action: {action}"}
    if order_type not in {"market", "limit"}:
        return {"success": False, "error": f"Unsupported order type: {order_type}; protection is software-managed"}
    if trail_percent is not None:
        return {"success": False, "error": "Trailing stops are software-managed and never sent to the broker"}
    if action == "short" and not hooks.get_mode_config(mode)["risk"].get("allow_short_positions", False):
        return {"success": False, "error": "New short entries are disabled for this mode"}
    if action == "short" and (stop_price is None or take_profit is None):
        return {"success": False, "error": "Short entries require software stop-loss and take-profit prices"}
    if order_type == "market" and limit_price is not None:
        return {"success": False, "error": "Market orders cannot include a limit price"}

    side = "buy" if action in ("buy", "cover") else "sell"
    if extended_hours:
        if mode != "algo":
            return {"success": False, "error": "Extended-hours orders are enabled only for Algo mode"}
        if action not in {"buy", "sell", "cover"} or order_type != "limit" or limit_price is None or float(limit_price) <= 0:
            return {"success": False, "error": "Extended-hours orders require a buy/sell/cover limit order with a positive limit price"}
        order = {
            "symbol": ticker,
            "qty": str(int(qty)),
            "side": side,
            "type": "limit",
            "time_in_force": "day",
            "limit_price": str(round(float(limit_price), 2)),
            "extended_hours": True,
        }
        order["position_intent"] = {
            "buy": "buy_to_open",
            "short": "sell_to_open",
            "sell": "sell_to_close",
            "cover": "buy_to_close",
        }[action]
        if client_order_id:
            order["client_order_id"] = str(client_order_id)[:48]
        return {**hooks.submit_broker_order(order, "limit"), "extended_hours": True}

    if (stop_price is None) != (take_profit is None):
        return {"success": False, "error": "Entry protection requires both a stop and a take-profit price"}
    if stop_price is not None and take_profit is not None:
        try:
            stop_value = float(stop_price)
            target_value = float(take_profit)
        except (TypeError, ValueError):
            return {"success": False, "error": "Protection prices must be numeric"}
        valid_levels = stop_value > target_value if action in {"short", "cover"} else stop_value < target_value
        if not all(math.isfinite(value) and value > 0 for value in (stop_value, target_value)) or not valid_levels:
            return {"success": False, "error": "Stop and target prices do not match the trade direction"}

    if order_type == "limit":
        if limit_price is None:
            return {"success": False, "error": "limit order is missing a required price"}
        try:
            numeric_limit = float(limit_price)
        except (TypeError, ValueError):
            return {"success": False, "error": "Limit price must be positive and finite"}
        if not math.isfinite(numeric_limit) or numeric_limit <= 0:
            return {"success": False, "error": "Limit price must be positive and finite"}
        order = {
            "symbol": ticker,
            "qty": str(int(qty)),
            "side": side,
            "type": "limit",
            "time_in_force": "day",
            "limit_price": str(round(numeric_limit, 2)),
        }
    else:
        order = {
            "symbol": ticker,
            "qty": str(int(qty)),
            "side": side,
            "type": "market",
            "time_in_force": "day",
        }

    order["position_intent"] = {
        "buy": "buy_to_open",
        "short": "sell_to_open",
        "sell": "sell_to_close",
        "cover": "buy_to_close",
    }[action]
    if client_order_id:
        order["client_order_id"] = str(client_order_id)[:48]

    result = hooks.submit_broker_order(order, order_type)
    if stop_price is not None and take_profit is not None and action in {"buy", "short"}:
        result["stop_price"] = stop_price
        result["take_profit"] = take_profit
    return result
