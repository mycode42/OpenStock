"""Position sizing and stop helpers shared by live scans and backtests."""

from __future__ import annotations

import math

from portfolio.config import STRATEGY_CONFIG


def _is_protective_sell_order(order: dict, position_qty: float = 1) -> bool:
    """Identify the exit-side broker protection for a long or short holding."""
    expected_side = "buy" if position_qty < 0 else "sell"
    if str(order.get("side", "")).lower() != expected_side:
        return False
    kind = str(order.get("type", "")).lower()
    order_class = str(order.get("order_class", "")).lower()
    return bool(
        kind in {"stop", "stop_limit", "trailing_stop"}
        or order.get("parent_order_id")
        or order_class in {"bracket", "oco"}
    )


def _positive_price_level(value: object) -> float | None:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if math.isfinite(price) and price > 0 else None


def _ticker_max_position_pct(ticker: str, universe: dict | None) -> float | None:
    """Return the configured per-name cap for a ticker in the active universe."""
    symbol = str(ticker).upper()
    limit = None
    for group in (universe or {}).values():
        if not isinstance(group, dict):
            continue
        tickers = {str(value).upper() for value in group.get("tickers", [])}
        raw_limit = group.get("max_per_stock_pct")
        if symbol in tickers and raw_limit is not None:
            candidate = float(raw_limit)
            limit = candidate if limit is None else min(limit, candidate)
    return limit


def _make_trade(
    signal: dict,
    price: float,
    equity: float,
    position: dict,
    mode_risk: dict,
    *,
    mode_universe: dict | None = None,
) -> dict | None:
    action = signal["action"]
    if action in {"sell", "cover"}:
        quantity = int(abs(float(position.get("qty", 0))))
        if quantity <= 0:
            return None
        return {
            "ticker": signal["ticker"],
            "action": action,
            "qty": quantity,
            "entry_price": price,
            "reasoning": signal["reason"],
            "strategy": signal["strategy"],
            "timeframe": signal.get("timeframe"),
            "direction": "close_short" if action == "cover" else "close_long",
        }

    strategy = str(signal.get("strategy", ""))
    configured_stop = mode_risk.get("stop_loss_pct_by_strategy", {}).get(strategy)
    if signal.get("stop_model_override"):
        stop_pct = float(signal.get("stop_pct") or 0.0)
    else:
        stop_pct = float(configured_stop or signal.get("stop_pct") or mode_risk["stop_loss_pct"])
    if equity <= 0 or price <= 0 or stop_pct <= 0:
        return None
    try:
        risk_multiplier = float(signal.get("risk_multiplier", 1.0))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(risk_multiplier) or not 0.01 <= risk_multiplier <= 1.5:
        return None
    risk_dollars = equity * float(mode_risk.get("risk_per_trade_pct", STRATEGY_CONFIG["risk_per_trade_pct"])) * risk_multiplier
    max_position_pct = min(
        float(mode_risk.get("max_strategy_position_pct", STRATEGY_CONFIG["max_strategy_position_pct"])),
        float(mode_risk["max_position_pct"]),
    )
    ticker_limit = _ticker_max_position_pct(signal["ticker"], mode_universe)
    if ticker_limit is not None:
        max_position_pct = min(max_position_pct, ticker_limit)
    suggested_weight = signal.get("target_weight")
    strategy_limit = mode_risk.get("max_position_pct_by_strategy", {}).get(strategy)
    if isinstance(strategy_limit, (int, float)) and strategy_limit > 0:
        max_position_pct = min(max_position_pct, float(strategy_limit))
    if isinstance(suggested_weight, (int, float)) and suggested_weight > 0:
        max_position_pct = min(max_position_pct, float(suggested_weight))
    max_position_dollars = equity * max_position_pct
    risk_quantity = int(risk_dollars / (price * stop_pct))
    position_quantity = int(max_position_dollars / price)
    quantity = min(risk_quantity, position_quantity)
    if quantity <= 0:
        return None
    if action not in {"buy", "short"}:
        return None
    stop_price = round(price * (1 + stop_pct), 2) if action == "short" else round(price * (1 - stop_pct), 2)
    requested_target = _positive_price_level(signal.get("take_profit_price"))
    target_is_valid = requested_target is not None and (
        requested_target < price if action == "short" else requested_target > price
    )
    if signal.get("take_profit_price") is not None and not target_is_valid:
        return None
    target_price = requested_target if target_is_valid else (
        round(price - (stop_price - price) * float(mode_risk.get("take_profit_r_multiple", STRATEGY_CONFIG["take_profit_r_multiple"])), 2)
        if action == "short" else
        round(price + (price - stop_price) * float(mode_risk.get("take_profit_r_multiple", STRATEGY_CONFIG["take_profit_r_multiple"])), 2)
    )
    trailing_stop = mode_risk.get("trailing_stop_pct_by_strategy", {}).get(strategy)
    trailing_activation = mode_risk.get("trailing_activation_pct_by_strategy", {}).get(strategy)
    return {
        "ticker": signal["ticker"],
        "action": action,
        "qty": quantity,
        "entry_price": price,
        "stop_price": stop_price,
        "take_profit": target_price,
        "trailing_stop_pct": trailing_stop,
        "trailing_activation_pct": trailing_activation,
        "reasoning": signal["reason"],
        "strategy": signal["strategy"],
        "timeframe": signal.get("timeframe"),
        "direction": "long" if action == "buy" else "short",
        "strategy_group": signal.get("strategy_group", "swing" if signal["strategy"] in {"swing_trend_following", "swing_momentum", "swing_mean_reversion"} else "intraday"),
    }


def _update_trailing_stop(
    state: dict,
    *,
    strategy: str,
    qty: float,
    entry_price: object,
    current_price: float,
    mode_risk: dict,
) -> float | None:
    """Maintain a persisted, direction-aware software trail inside the hard stop."""
    entry = _positive_price_level(entry_price)
    if entry is None or qty == 0 or current_price <= 0:
        return _positive_price_level(state.get("stop_price"))
    stop_pct = mode_risk.get("stop_loss_pct_by_strategy", {}).get(strategy)
    trail_pct = mode_risk.get("trailing_stop_pct_by_strategy", {}).get(strategy)
    activation_pct = mode_risk.get("trailing_activation_pct_by_strategy", {}).get(strategy)
    try:
        hard_pct = float(stop_pct)
        trail_pct = float(trail_pct)
        activation_pct = float(activation_pct)
    except (TypeError, ValueError):
        return _positive_price_level(state.get("stop_price"))
    if not all(math.isfinite(value) and value > 0 for value in (hard_pct, trail_pct, activation_pct)):
        return _positive_price_level(state.get("stop_price"))

    is_short = qty < 0
    hard_stop = entry * (1 + hard_pct) if is_short else entry * (1 - hard_pct)
    existing_stop = _positive_price_level(state.get("stop_price"))
    stop = min(existing_stop, hard_stop) if is_short and existing_stop else (
        max(existing_stop, hard_stop) if existing_stop else hard_stop
    )
    best_key = "trailing_trough_price" if is_short else "trailing_peak_price"
    best = _positive_price_level(state.get(best_key)) or entry
    best = min(best, current_price) if is_short else max(best, current_price)
    state[best_key] = round(best, 4)
    favorable = (entry - best) / entry if is_short else (best - entry) / entry
    if favorable >= activation_pct:
        trailing = best * (1 + trail_pct) if is_short else best * (1 - trail_pct)
        stop = min(stop, trailing) if is_short else max(stop, trailing)
    state["stop_price"] = round(stop, 2)
    return state["stop_price"]

