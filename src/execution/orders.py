"""Order validation, Alpaca payload construction, and replay-safe submission."""

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from src.execution.broker import API_BASE


MARKET_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class OrderHooks:
    mode: str
    headers: Callable
    verify_algo_execution_lease: Callable
    lookup_client_order: Callable
    get_nested_order: Callable
    get_mode_config: Callable
    get_day_schedule: Callable
    list_positions: Callable
    position_qty: Callable
    list_open_orders: Callable
    submit_broker_order: Callable
    default_target_multiplier: float


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

    order_class = str(order.get("order_class") or "simple").lower()
    if order_class not in {"bracket", "oco"}:
        return True
    nested = get_nested_order(headers, str(broker["id"]))
    if str(nested.get("order_class") or "").lower() != order_class or str(nested.get("symbol") or "").upper() != str(order["symbol"]).upper() or str(nested.get("side") or "").lower() != str(order["side"]).lower():
        return False
    stop_requested = (order.get("stop_loss") or {}).get("stop_price")
    target_requested = (order.get("take_profit") or {}).get("limit_price")
    legs = nested.get("legs") or []
    if not isinstance(legs, list):
        return False
    if order_class == "oco":
        return prices_equal(nested.get("limit_price"), target_requested) and len(legs) == 1 and isinstance(legs[0], dict) and str(legs[0].get("type") or "").lower() in {"stop", "stop_limit"} and prices_equal(legs[0].get("stop_price"), stop_requested)
    stops = [leg for leg in legs if isinstance(leg, dict) and str(leg.get("type") or "").lower() in {"stop", "stop_limit"}]
    targets = [leg for leg in legs if isinstance(leg, dict) and str(leg.get("type") or "").lower() == "limit"]
    return len(stops) == 1 and len(targets) == 1 and prices_equal(stops[0].get("stop_price"), stop_requested) and prices_equal(targets[0].get("limit_price"), target_requested)


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
    stop_pct=None,
    client_order_id=None,
    extended_hours=False,
):
    """Validate an order request, build its Alpaca payload, and submit it."""
    mode = hooks.mode
    if action not in {"buy", "sell", "short", "cover"}:
        return {"success": False, "error": f"Unsupported trade action: {action}"}
    if order_type not in {"market", "limit", "bracket", "stop", "oco", "trailing_stop"}:
        return {"success": False, "error": f"Unsupported order type: {order_type}"}
    if action == "short" and not hooks.get_mode_config(mode)["risk"].get("allow_short_positions", False):
        return {"success": False, "error": "New short entries are disabled for this mode"}
    if action == "short" and (stop_price is None or take_profit is None):
        return {"success": False, "error": "Short entries require attached stop-loss and take-profit prices"}
    if action == "short" and order_type not in {"market", "limit", "bracket"}:
        return {"success": False, "error": "Short entries require a bracket-capable entry order"}
    if action == "cover" and order_type not in {"market", "limit", "oco", "stop"}:
        return {"success": False, "error": "Cover exits support market, limit, stop, or OCO orders"}
    if order_type == "oco" and action not in {"sell", "cover"}:
        return {"success": False, "error": "OCO protection requires a position-reducing sell or cover"}
    if action not in {"buy", "short"} and (order_type == "bracket" or (stop_price is not None and take_profit is not None and order_type != "oco")):
        return {"success": False, "error": "Entry brackets require buy/short; use OCO for an existing position"}
    if order_type == "market" and limit_price is not None:
        return {"success": False, "error": "Market orders cannot include a limit price"}
    if order_type in {"stop", "trailing_stop"} and (take_profit is not None or limit_price is not None or (order_type == "stop" and trail_percent is not None) or (order_type == "trailing_stop" and stop_price is not None)):
        return {"success": False, "error": "Stop and trailing-stop orders cannot include bracket or limit fields"}
    if order_type == "oco" and (limit_price is not None or trail_percent is not None):
        return {"success": False, "error": "OCO take-profit and stop prices must use their dedicated fields"}

    if mode == "day" and action in {"buy", "short"}:
        try:
            risk = hooks.get_mode_config("day")["risk"]
            schedule = hooks.get_day_schedule(
                flatten_by=risk.get("flatten_by", "15:45"),
                flatten_lead_minutes=risk.get("flatten_lead_minutes", 15),
                entry_cutoff_minutes=risk.get("entry_cutoff_minutes", 15),
            )
        except Exception as exc:
            return {"success": False, "error": f"Day calendar unavailable: {exc}"}
        if schedule is None or not schedule.entries_allowed(datetime.now(MARKET_TZ)):
            return {"success": False, "error": "Day entry cutoff has passed or market is closed"}

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
        if action == "cover":
            order["position_intent"] = "buy_to_close"
        if client_order_id:
            order["client_order_id"] = str(client_order_id)[:48]
        return {**hooks.submit_broker_order(order, "limit"), "extended_hours": True}

    if order_type in {"bracket", "oco", "market", "limit"} and stop_price is not None and take_profit is not None:
        try:
            stop_value = float(stop_price)
            target_value = float(take_profit)
        except (TypeError, ValueError):
            return {"success": False, "error": "Protection prices must be numeric"}
        valid_levels = stop_value > target_value if action in {"short", "cover"} else stop_value < target_value
        if not all(math.isfinite(value) and value > 0 for value in (stop_value, target_value)) or not valid_levels:
            return {"success": False, "error": "Stop and target prices do not match the trade direction"}

    if mode == "algo" and order_type == "oco":
        try:
            schedule = hooks.get_day_schedule(flatten_by="15:45", mode="algo")
            if schedule is None or not schedule.market_open(datetime.now(MARKET_TZ)):
                return {"success": False, "error": "Algo OCO repair requires regular market hours"}
            headers = hooks.headers("algo")
            matches = [row for row in hooks.list_positions(headers) if str(row.get("symbol", "")).upper() == str(ticker).upper()]
            if len(matches) != 1 or abs(hooks.position_qty(matches[0])) != Decimal(str(qty)):
                return {"success": False, "error": "Algo OCO repair quantity no longer matches the broker holding"}
            open_symbol_orders = [row for row in hooks.list_open_orders(headers) if str(row.get("symbol", "")).upper() == str(ticker).upper()]
            if open_symbol_orders:
                return {"success": False, "error": "Algo OCO repair found an open broker order for this symbol"}
        except (requests.RequestException, ValueError, InvalidOperation) as exc:
            return {"success": False, "error": f"Algo OCO repair preflight unavailable: {exc}"}

    if action in {"buy", "short"} and order_type not in ("limit", "stop", "trailing_stop", "oco"):
        ref_price = entry_price or limit_price or 0
        if ref_price > 0 and stop_pct is not None:
            if stop_price is None:
                stop_price = round(ref_price * (1 + stop_pct), 2) if action == "short" else round(ref_price * (1 - stop_pct), 2)
            if take_profit is None and stop_price is not None:
                stop_dist = abs(ref_price - float(stop_price))
                take_profit = round(ref_price - stop_dist * hooks.default_target_multiplier, 2) if action == "short" else round(ref_price + stop_dist * hooks.default_target_multiplier, 2)

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

    use_bracket = action in {"buy", "short"} and order_type in {"market", "limit", "bracket"} and (order_type == "bracket" or (stop_price is not None and take_profit is not None))
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

    order["position_intent"] = {
        "buy": "buy_to_open",
        "short": "sell_to_open",
        "sell": "sell_to_close",
        "cover": "buy_to_close",
    }[action]
    if client_order_id:
        order["client_order_id"] = str(client_order_id)[:48]

    result = hooks.submit_broker_order(order, order_type)
    if use_bracket:
        result["order_class"] = "bracket"
        result["stop_price"] = stop_price
        result["take_profit"] = take_profit
    elif order_type == "oco":
        result["order_class"] = "oco"
    return result
