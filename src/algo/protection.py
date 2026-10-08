"""Read-only evidence checks for replacement Algo broker protection."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


ACTIVE_ORDER_STATUSES = frozenset({
    "new", "held", "accepted", "pending_new", "partially_filled", "accepted_for_bidding",
})


def _amount(value: object) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _remaining(order: dict) -> Decimal | None:
    quantity = _amount(order.get("qty"))
    filled = _amount(order.get("filled_qty") or 0)
    if quantity is None or filled is None or quantity <= 0 or filled < 0 or filled > quantity:
        return None
    return quantity - filled


def recovery_oco_order(nested_orders: list[dict], state: dict, position_qty: Decimal) -> dict | None:
    """Return an active, exact full-size OCO parent with its live stop child.

    Alpaca represents an OCO take-profit as the parent and its stop-loss as a
    child in nested order responses. Persisted submission status is not proof.
    """
    recovery = state.get("protection_recovery") or {}
    if not isinstance(recovery, dict):
        return None
    client_id = recovery.get("client_order_id")
    entry_id = state.get("entry_order_id")
    ticker = str(state.get("ticker") or "").upper()
    if not isinstance(client_id, str) or not client_id or not entry_id or not ticker:
        return None
    if recovery.get("entry_order_id") and recovery["entry_order_id"] != entry_id:
        return None
    quantity = _amount(position_qty)
    stop = _amount(state.get("stop_price"))
    target = _amount(state.get("take_profit"))
    if quantity is None or quantity <= 0 or stop is None or target is None or not 0 < stop < target:
        return None
    if recovery.get("qty") is not None and _amount(recovery["qty"]) != quantity:
        return None
    if recovery.get("stop_price") is not None and _amount(recovery["stop_price"]) != stop:
        return None
    if recovery.get("take_profit") is not None and _amount(recovery["take_profit"]) != target:
        return None

    matches = [
        order for order in nested_orders
        if isinstance(order, dict) and order.get("client_order_id") == client_id
    ]
    if len(matches) != 1:
        return None
    parent = matches[0]
    parent_id = str(parent.get("id") or "")
    if (
        not parent_id
        or (recovery.get("order_id") and str(recovery["order_id"]) != parent_id)
        or str(parent.get("symbol") or "").upper() != ticker
        or str(parent.get("side") or "").lower() != "sell"
        or str(parent.get("type") or "").lower() != "limit"
        or str(parent.get("order_class") or "").lower() != "oco"
        or str(parent.get("status") or "").lower() not in ACTIVE_ORDER_STATUSES
        or _remaining(parent) != quantity
        or _amount(parent.get("limit_price")) != target
    ):
        return None

    legs = parent.get("legs")
    if not isinstance(legs, list) or len(legs) != 1 or not isinstance(legs[0], dict):
        return None
    child = legs[0]
    if (
        (child.get("symbol") and str(child["symbol"]).upper() != ticker)
        or (child.get("parent_order_id") and str(child["parent_order_id"]) != parent_id)
        or str(child.get("side") or "").lower() != "sell"
        or str(child.get("type") or "").lower() not in {"stop", "stop_limit"}
        or str(child.get("status") or "").lower() not in ACTIVE_ORDER_STATUSES
        or _remaining(child) != quantity
        or _amount(child.get("stop_price")) != stop
    ):
        return None
    return parent


def verified_recovery_oco(nested_orders: list[dict], state: dict, position_qty: Decimal) -> bool:
    """Whether a replacement OCO currently covers the whole broker holding."""
    return recovery_oco_order(nested_orders, state, position_qty) is not None


__all__ = ["ACTIVE_ORDER_STATUSES", "recovery_oco_order", "verified_recovery_oco"]
