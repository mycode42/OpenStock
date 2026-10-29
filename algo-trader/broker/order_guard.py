"""Checks every order before it leaves the process: no single order may exceed MAX_ORDER_USD.

Paper and live accounts are checked the same way.
"""

from __future__ import annotations

MAX_ORDER_USD = 100_000.0


def _number(value, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"order {name} must be a number") from exc


def check_order(payload: dict, *, multiplier: float = 1.0) -> None:
    """Raise ValueError if the order's notional value exceeds the per-order cap.

    The value is quantity x limit price x ``multiplier`` (100 for options contracts), or the
    notional when the order is sized in dollars. An order with neither has no value to check.
    ``multiplier`` converts a per-share price into notional value.
    """
    value = None
    if payload.get("limit_price") not in (None, ""):
        quantity = abs(_number(payload.get("qty") or 0, "quantity"))
        price = _number(payload["limit_price"], "limit price")
        if quantity <= 0 or price <= 0:
            raise ValueError("order needs a positive quantity and limit price")
        value = quantity * price * multiplier
    elif payload.get("notional") not in (None, ""):
        value = abs(_number(payload["notional"], "notional"))
    if value is not None and value > MAX_ORDER_USD:
        raise ValueError(f"order value ${value:,.2f} exceeds the ${MAX_ORDER_USD:,.0f} per-order limit")
