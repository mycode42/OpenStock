"""Read-only evidence checks for replacement Algo broker protection."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


ACTIVE_ORDER_STATUSES = frozenset({
    "new", "held", "accepted", "pending_new", "partially_filled", "accepted_for_bidding",
})


def to_finite_decimal(value: object) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def normalize_broker_price(value: object) -> Decimal | None:
    """Apply the same cent rounding that the Algo executor sends to Alpaca."""
    price = to_finite_decimal(value)
    if price is None or price <= 0:
        return None
    try:
        rounded = Decimal(str(round(float(price), 2)))
    except (OverflowError, TypeError, ValueError):
        return None
    return rounded if rounded.is_finite() and rounded > 0 else None


def broker_prices_match(actual: object, expected: object) -> bool:
    """Compare prices using the executor's two-decimal broker submission value."""
    actual_price = to_finite_decimal(actual)
    broker_expected = normalize_broker_price(expected)
    if actual_price is None or broker_expected is None:
        return False
    return actual_price == broker_expected


def remaining_order_quantity(order: dict) -> Decimal | None:
    quantity = to_finite_decimal(order.get("qty"))
    filled = to_finite_decimal(order.get("filled_qty") or 0)
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
    signed_quantity = to_finite_decimal(position_qty)
    quantity = abs(signed_quantity) if signed_quantity is not None else None
    is_short = signed_quantity is not None and signed_quantity < 0
    exit_side = "buy" if is_short else "sell"
    stop = normalize_broker_price(state.get("stop_price"))
    target = normalize_broker_price(state.get("take_profit"))
    valid_levels = stop < target if stop is not None and target is not None and not is_short else (
        stop > target if stop is not None and target is not None else False
    )
    if quantity is None or quantity <= 0 or stop is None or target is None or not valid_levels:
        return None
    if recovery.get("qty") is not None and to_finite_decimal(recovery["qty"]) != quantity:
        return None
    if recovery.get("stop_price") is not None and not broker_prices_match(
        recovery["stop_price"], state.get("stop_price"),
    ):
        return None
    if recovery.get("take_profit") is not None and not broker_prices_match(
        recovery["take_profit"], state.get("take_profit"),
    ):
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
        or str(parent.get("side") or "").lower() != exit_side
        or str(parent.get("type") or "").lower() != "limit"
        or str(parent.get("order_class") or "").lower() != "oco"
        or str(parent.get("status") or "").lower() not in ACTIVE_ORDER_STATUSES
        or remaining_order_quantity(parent) != quantity
        or not broker_prices_match(parent.get("limit_price"), target)
    ):
        return None

    legs = parent.get("legs")
    if not isinstance(legs, list) or len(legs) != 1 or not isinstance(legs[0], dict):
        return None
    child = legs[0]
    if (
        (child.get("symbol") and str(child["symbol"]).upper() != ticker)
        or (child.get("parent_order_id") and str(child["parent_order_id"]) != parent_id)
        or str(child.get("side") or "").lower() != exit_side
        or str(child.get("type") or "").lower() not in {"stop", "stop_limit"}
        or str(child.get("status") or "").lower() not in ACTIVE_ORDER_STATUSES
        or remaining_order_quantity(child) != quantity
        or not broker_prices_match(child.get("stop_price"), stop)
    ):
        return None
    return parent


def verified_recovery_oco(nested_orders: list[dict], state: dict, position_qty: Decimal) -> bool:
    """Whether a replacement OCO currently covers the whole broker holding."""
    return recovery_oco_order(nested_orders, state, position_qty) is not None


__all__ = ["ACTIVE_ORDER_STATUSES", "broker_prices_match", "normalize_broker_price", "recovery_oco_order", "remaining_order_quantity", "to_finite_decimal", "verified_recovery_oco"]
