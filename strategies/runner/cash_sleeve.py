"""Plan parking of idle cash in a cash-proxy ETF; a pure function with no broker calls.

The sleeve buys the proxy only while gross exposure is below target and cash is
above the reserve, and sells the proxy only when cash has fallen below the
reserve, so entries are never starved. It is disabled by default. Live use needs
the proxy to be attributed to a reserved owner in reconciliation; otherwise the
holding would pause entries as an unattributed position.
"""

from __future__ import annotations

import math

from portfolio.config import CASH_SLEEVE as CASH_SLEEVE_DEFAULTS


def plan_cash_sleeve(
    *, equity: float, cash: float, invested_value: float, proxy_price: float,
    proxy_qty: int, config: dict | None = None,
) -> dict:
    """Return {"action": "buy" | "sell" | "hold", "qty": int, "reason": str}."""
    settings = {**CASH_SLEEVE_DEFAULTS, **(config or {})}
    hold = {"action": "hold", "qty": 0}
    if not settings["enabled"]:
        return {**hold, "reason": "cash sleeve disabled"}
    if equity <= 0 or proxy_price <= 0 or not math.isfinite(proxy_price):
        return {**hold, "reason": "equity or proxy price unavailable"}
    reserve = settings["reserve_pct"] * equity
    proxy_value = proxy_qty * proxy_price
    if cash < reserve:
        needed = math.ceil((reserve - cash) / proxy_price)
        qty = min(int(proxy_qty), needed)
        if qty > 0:
            return {"action": "sell", "qty": qty, "reason": "cash below reserve; free cash for entries"}
        return {**hold, "reason": "cash below reserve and no proxy held"}
    gross_room = settings["target_gross_pct"] * equity - invested_value
    proxy_room = settings["max_proxy_pct"] * equity - proxy_value
    spare_cash = cash - reserve
    budget = min(spare_cash, gross_room, proxy_room)
    if budget < settings["min_trade_value"]:
        return {**hold, "reason": "idle cash below minimum trade value or exposure at target"}
    qty = int(budget // proxy_price)
    if qty <= 0:
        return {**hold, "reason": "proxy price exceeds idle budget"}
    return {"action": "buy", "qty": qty, "reason": "park idle cash in cash proxy"}
