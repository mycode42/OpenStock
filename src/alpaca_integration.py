"""Read-only Alpaca paper integration and legacy decision previews.

Broker mutations use execute_trades.py, which owns account verification,
locking, risk validation, and Algo coordinator authorization.

Multi-account support: all functions accept an optional `mode` parameter.
When provided, credentials are routed to the correct account (day vs swing).
When omitted, the current trading mode (from trading_mode.json) is used.
"""

import requests

from src.accounts import get_account_for_mode

# Always paper trading - never use live endpoint
ALPACA_BASE_URL = "https://paper-api.alpaca.markets/v2"


def _get_headers(mode: str = None) -> dict:
    """Get API headers for the appropriate account based on trading mode."""
    account = get_account_for_mode(mode)
    return account.headers


def get_alpaca_account(mode: str = None) -> dict:
    """Fetch account information from Alpaca.

    Args:
        mode: Trading mode ("swing" or "day"). Routes to correct account.
    """
    headers = _get_headers(mode)
    resp = requests.get(f"{ALPACA_BASE_URL}/account", headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_alpaca_positions(mode: str = None) -> list[dict]:
    """Fetch all open positions from Alpaca. Returns list of position dicts.

    Args:
        mode: Trading mode ("swing" or "day"). Routes to correct account.
    """
    headers = _get_headers(mode)
    resp = requests.get(f"{ALPACA_BASE_URL}/positions", headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_alpaca_portfolio_value(account: dict, positions: list[dict]) -> float:
    """Calculate total portfolio value (cash + positions)."""
    cash = float(account.get("cash", 0))
    position_value = sum(float(p.get("market_value", 0)) for p in positions)
    return cash + position_value


def convert_to_portfolio(
    alpaca_positions: list[dict],
    alpaca_account: dict,
    tickers: list[str] | None = None,
) -> dict:
    """Convert Alpaca positions + account into the portfolio format expected by the hedge fund.

    Args:
        alpaca_positions: Raw list of Alpaca position objects
        alpaca_account: Raw Alpaca account object
        tickers: Full list of tickers to analyze (including ones not currently held).
                 If None, uses only tickers from existing positions.

    Returns:
        Portfolio dict compatible with run_hedge_fund()
    """
    cash = float(alpaca_account.get("cash", 0))

    # Build lookup by symbol
    positions_by_symbol: dict[str, dict] = {p["symbol"]: p for p in alpaca_positions}

    # Determine all tickers (existing positions + any additional from args)
    all_tickers = list(positions_by_symbol.keys())
    if tickers:
        for t in tickers:
            if t not in all_tickers:
                all_tickers.append(t)

    positions = {}
    for ticker in all_tickers:
        pos = positions_by_symbol.get(ticker, {})
        qty = int(float(pos.get("qty", 0)))
        avg_price = float(pos.get("avg_entry_price", 0))

        positions[ticker] = {
            "long": qty if qty > 0 else 0,
            "short": abs(qty) if qty < 0 else 0,
            "long_cost_basis": avg_price if qty > 0 else 0.0,
            "short_cost_basis": avg_price if qty < 0 else 0.0,
            "short_margin_used": 0.0,
        }

    realized_gains = {
        ticker: {"long": 0.0, "short": 0.0}
        for ticker in all_tickers
    }

    return {
        "cash": cash,
        "margin_requirement": 0.5,
        "margin_used": 0.0,
        "positions": positions,
        "realized_gains": realized_gains,
    }


def get_daily_pnl(account: dict) -> float:
    """Calculate today's P&L as a fraction of starting equity."""
    equity = float(account.get("equity", 0))
    last_equity = float(account.get("last_equity", equity))
    if last_equity <= 0:
        return 0.0
    return (equity - last_equity) / last_equity


def _place_alpaca_order(*args, **kwargs) -> dict:
    """Legacy direct broker mutation is disabled."""
    raise RuntimeError("Legacy broker mutation is disabled; use execute_trades.py")


def _place_bracket_order(*args, **kwargs) -> dict:
    """Legacy direct broker mutation is disabled."""
    raise RuntimeError("Legacy broker mutation is disabled; use execute_trades.py")


def flatten_positions(
    positions_raw: list[dict],
    dry_run: bool = True,
    tickers: list[str] | None = None,
    mode: str = None,
) -> list[dict]:
    """Preview a flatten from a supplied snapshot.

    Args:
        positions_raw: Raw Alpaca positions list
        dry_run: Must be True. Use execute_trades.py --flatten to trade.
        tickers: If provided, only flatten these tickers. Default: all positions.
        mode: Trading mode for account routing.

    Returns:
        List of result dicts per position flattened
    """
    if not dry_run:
        raise RuntimeError("Legacy flatten route is disabled; use execute_trades.py --flatten")
    results = []
    for pos in positions_raw:
        symbol = pos["symbol"]
        if tickers and symbol not in tickers:
            continue

        qty = int(float(pos.get("qty", 0)))
        if qty == 0:
            continue

        side = "sell" if qty > 0 else "buy"  # longs → sell, shorts → buy to cover
        abs_qty = abs(qty)

        results.append({
            "ticker": symbol,
            "action": "flatten",
            "qty": abs_qty,
            "side": side,
            "success": True,
            "dry_run": True,
        })

    return results


def execute_decisions(
    decisions: dict,
    positions_raw: list[dict],
    account: dict,
    dry_run: bool = True,
    mode: str = None,
) -> list[dict]:
    """Preview trading decisions with V2 risk validation.

    Validation for buy/short orders is delegated to risk_manager.validate_trade().
    No internal safety rail logic — risk_manager is the single source of truth.

    Args:
        decisions: Dict of {ticker: {action, quantity, confidence, reasoning,
                   stop_price (optional), take_profit (optional), order_type (optional)}}
                   If stop_price and take_profit are both provided, a bracket order is placed.
        positions_raw: Raw Alpaca positions list
        account: Raw Alpaca account dict
        dry_run: Must be True. Use execute_trades.py to place orders.
        mode: Trading mode ("swing" or "day"). Resolved from env if not specified.

    Returns:
        List of result dicts with success/failure info per ticker
    """
    if not dry_run:
        raise RuntimeError("Legacy decision execution is disabled; use execute_trades.py")
    from src.config import resolve_mode
    if mode is None:
        mode = resolve_mode()

    # Try to load V2 risk manager
    risk_manager_available = False
    rm_portfolio_state = None
    try:
        from risk_manager import validate_trade as rm_validate_trade, get_portfolio_state as rm_get_portfolio_state
        risk_manager_available = True
        try:
            rm_portfolio_state = rm_get_portfolio_state(mode=mode)
        except Exception:
            rm_portfolio_state = None  # will be fetched per-trade by validate_trade
    except ImportError:
        pass

    positions_by_symbol: dict[str, dict] = {p["symbol"]: p for p in positions_raw}

    results = []

    for ticker, decision in decisions.items():
        action = decision.get("action", "hold")
        qty = int(decision.get("quantity", 0))
        confidence = float(decision.get("confidence", 0))
        reasoning = decision.get("reasoning", "")
        stop_price = decision.get("stop_price")
        take_profit = decision.get("take_profit")
        order_type = decision.get("order_type", "market")
        limit_price = decision.get("limit_price")
        trail_percent = decision.get("trail_percent")

        if action == "hold" or qty <= 0:
            results.append({
                "ticker": ticker,
                "action": action,
                "qty": qty,
                "success": False,
                "reason": "Hold — no trade needed",
                "skipped": True,
            })
            continue

        # V2 risk manager validation for entries
        if action in ("buy", "short") and risk_manager_available:
            pos = positions_by_symbol.get(ticker, {})
            entry_price = float(pos.get("current_price", 0)) or float(limit_price or 0)
            rm_result = rm_validate_trade(
                ticker=ticker,
                action=action,
                qty=qty,
                entry_price=entry_price,
                portfolio_state=rm_portfolio_state,
                mode=mode,
            )
            if not rm_result.approved:
                results.append({
                    "ticker": ticker,
                    "action": action,
                    "qty": qty,
                    "confidence": confidence,
                    "success": False,
                    "reason": rm_result.reason,
                    "rule": rm_result.rule,
                })
                continue

        use_bracket = stop_price is not None and take_profit is not None and order_type != "oco"

        if dry_run:
            dry_result: dict = {
                "ticker": ticker,
                "action": action,
                "qty": qty,
                "confidence": confidence,
                "success": True,
                "dry_run": True,
                "reasoning": reasoning,
                "order_type": order_type,
            }
            if use_bracket:
                dry_result["order_class"] = "bracket"
                dry_result["stop_price"] = stop_price
                dry_result["take_profit_price"] = take_profit
            elif order_type == "oco" and stop_price is not None and take_profit is not None:
                dry_result["order_class"] = "oco"
                dry_result["stop_price"] = stop_price
                dry_result["take_profit_price"] = take_profit
            elif order_type == "limit" and limit_price is not None:
                dry_result["limit_price"] = limit_price
            elif order_type == "stop" and stop_price is not None:
                dry_result["stop_price"] = stop_price
            elif order_type == "trailing_stop" and trail_percent is not None:
                dry_result["trail_percent"] = trail_percent
            results.append(dry_result)
    return results


def get_open_orders(status: str = "open", mode: str = None) -> list[dict]:
    """Fetch all open orders from Alpaca.

    Args:
        status: Order status filter — "open", "closed", or "all"
        mode: Trading mode for account routing.

    Returns:
        List of order dicts from Alpaca
    """
    headers = _get_headers(mode)
    resp = requests.get(
        f"{ALPACA_BASE_URL}/orders",
        headers=headers,
        params={"status": status, "limit": 100},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def get_order(order_id: str, mode: str = None) -> dict:
    """Fetch a single order by ID.

    Returns:
        Full order dict (status, filled_qty, etc.) or error dict
    """
    headers = _get_headers(mode)
    resp = requests.get(f"{ALPACA_BASE_URL}/orders/{order_id}", headers=headers, timeout=10)
    if resp.status_code == 200:
        return resp.json()
    return {"success": False, "reason": f"Alpaca API error {resp.status_code}: {resp.text[:200]}"}


def cancel_order(*args, **kwargs) -> dict:
    """Legacy direct broker mutation is disabled."""
    raise RuntimeError("Legacy broker mutation is disabled; use execute_trades.py")


def cancel_all_orders(*args, **kwargs) -> dict:
    """Legacy direct broker mutation is disabled."""
    raise RuntimeError("Legacy broker mutation is disabled; use execute_trades.py")


def _place_limit_order(*args, **kwargs) -> dict:
    """Legacy direct broker mutation is disabled."""
    raise RuntimeError("Legacy broker mutation is disabled; use execute_trades.py")


def _place_stop_order(*args, **kwargs) -> dict:
    """Legacy direct broker mutation is disabled."""
    raise RuntimeError("Legacy broker mutation is disabled; use execute_trades.py")


def _place_trailing_stop(*args, **kwargs) -> dict:
    """Legacy direct broker mutation is disabled."""
    raise RuntimeError("Legacy broker mutation is disabled; use execute_trades.py")


def _place_oco_order(*args, **kwargs) -> dict:
    """Legacy direct broker mutation is disabled."""
    raise RuntimeError("Legacy broker mutation is disabled; use execute_trades.py")


def format_positions_summary(positions_raw: list[dict], account: dict) -> str:
    """Format a human-readable summary of current positions."""
    cash = float(account.get("cash", 0))
    portfolio_value = get_alpaca_portfolio_value(account, positions_raw)
    lines = [
        f"Portfolio Value: ${portfolio_value:,.2f}",
        f"Cash: ${cash:,.2f}",
        f"Positions ({len(positions_raw)}):",
    ]
    for pos in sorted(positions_raw, key=lambda p: abs(float(p.get("market_value", 0))), reverse=True):
        symbol = pos["symbol"]
        qty = float(pos.get("qty", 0))
        market_value = float(pos.get("market_value", 0))
        unrealized_pl = float(pos.get("unrealized_pl", 0))
        pl_sign = "+" if unrealized_pl >= 0 else ""
        lines.append(
            f"  {symbol}: {qty:.0f} shares  ${market_value:,.2f}  ({pl_sign}{unrealized_pl:,.2f} P&L)"
        )
    return "\n".join(lines)
