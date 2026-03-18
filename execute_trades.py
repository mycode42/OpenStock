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
import json
import logging
import os
import sys
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [execute_trades] %(levelname)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("execute_trades")

API_BASE = "https://paper-api.alpaca.markets/v2"
DATA_BASE = "https://data.alpaca.markets/v2"
HEADERS = {
    "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY", ""),
    "APCA-API-SECRET-KEY": os.environ.get("ALPACA_API_SECRET", ""),
    "Content-Type": "application/json",
}

from src.config import get_mode_config, DEFAULT_TARGET_MULTIPLIER

# V2 risk manager — validates every BUY before execution
try:
    from risk_manager import validate_trade as rm_validate_trade, get_portfolio_state as rm_get_portfolio_state
    RISK_MANAGER_AVAILABLE = True
    log.info("V2 risk manager loaded")
except ImportError:
    RISK_MANAGER_AVAILABLE = False
    log.warning("risk_manager.py not found — falling back to legacy validation only")


def get_account():
    r = requests.get(f"{API_BASE}/account", headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()


def get_positions():
    r = requests.get(f"{API_BASE}/positions", headers=HEADERS, timeout=10)
    r.raise_for_status()
    return {p["symbol"]: p for p in r.json()}


def get_daily_pnl(account: dict) -> float:
    """Return today's P&L as a fraction of starting equity (negative = loss)."""
    equity = float(account.get("equity", 0))
    last_equity = float(account.get("last_equity", equity))
    if last_equity <= 0:
        return 0.0
    return (equity - last_equity) / last_equity


def flatten_all(dry_run: bool = False) -> dict:
    """Market-sell every open position. Used for end-of-day flatten."""
    r = requests.get(f"{API_BASE}/positions", headers=HEADERS, timeout=10)
    r.raise_for_status()
    positions = r.json()

    if not positions:
        return {"status": "nothing_to_flatten", "positions_closed": 0}

    results = []
    for pos in positions:
        symbol = pos["symbol"]
        qty = int(float(pos.get("qty", 0)))
        if qty == 0:
            continue

        side = "sell" if qty > 0 else "buy"
        abs_qty = abs(qty)

        if dry_run:
            results.append({"ticker": symbol, "side": side, "qty": abs_qty, "status": "would_flatten"})
        else:
            order = {
                "symbol": symbol,
                "qty": str(abs_qty),
                "side": side,
                "type": "market",
                "time_in_force": "day",
            }
            resp = requests.post(f"{API_BASE}/orders", headers=HEADERS, json=order, timeout=10)
            if resp.status_code in (200, 201):
                data = resp.json()
                results.append({
                    "ticker": symbol, "side": side, "qty": abs_qty,
                    "status": "flattened", "order_id": data.get("id"),
                })
            else:
                results.append({
                    "ticker": symbol, "side": side, "qty": abs_qty,
                    "status": "failed", "error": f"HTTP {resp.status_code}: {resp.text[:200]}",
                })

    return {
        "timestamp": datetime.now().isoformat(),
        "mode": "dry_run" if dry_run else "live",
        "positions_closed": len(results),
        "results": results,
    }


def place_order(
    ticker, action, qty,
    order_type="market",
    stop_price=None, take_profit=None,
    limit_price=None, trail_percent=None,
    entry_price=None, stop_pct=None,
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
    side = "buy" if action in ("buy", "cover") else "sell"

    # Auto-calculate bracket prices if stop_pct is provided and prices are missing
    if action in ("buy", "cover", "short") and order_type not in ("limit", "stop", "trailing_stop", "oco"):
        ref_price = entry_price or limit_price or 0
        if ref_price > 0 and stop_pct is not None:
            if stop_price is None:
                if action in ("buy", "cover"):
                    stop_price = round(ref_price * (1 - stop_pct), 2)
                else:  # short
                    stop_price = round(ref_price * (1 + stop_pct), 2)
            if take_profit is None and stop_price is not None:
                stop_dist = abs(ref_price - float(stop_price))
                if action in ("buy", "cover"):
                    take_profit = round(ref_price + stop_dist * DEFAULT_TARGET_MULTIPLIER, 2)
                else:
                    take_profit = round(ref_price - stop_dist * DEFAULT_TARGET_MULTIPLIER, 2)

    if action == "short":
        side = "sell"

    use_bracket = (
        order_type == "bracket"
        or (stop_price is not None and take_profit is not None and order_type not in ("oco",))
    )

    if use_bracket:
        order = {
            "symbol": ticker,
            "qty": str(int(qty)),
            "side": side,
            "type": "market",
            "time_in_force": "gtc",
            "order_class": "bracket",
            "stop_loss": {"stop_price": str(round(float(stop_price), 2))},
            "take_profit": {"limit_price": str(round(float(take_profit), 2))},
        }
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

    r = requests.post(f"{API_BASE}/orders", headers=HEADERS, json=order, timeout=10)
    if r.status_code in (200, 201):
        data = r.json()
        result = {
            "success": True,
            "order_id": data.get("id"),
            "status": data.get("status"),
            "order_type": order_type,
        }
        if use_bracket:
            result["order_class"] = "bracket"
            result["stop_price"] = stop_price
            result["take_profit"] = take_profit
        elif order_type == "oco":
            result["order_class"] = "oco"
        return result
    return {"success": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}


def validate_trade_legacy(ticker, action, qty, positions, portfolio_value, daily_loss_limit=0.03, max_trade_pct=0.15):
    """Legacy position-level validation (sell/cover checks + portfolio-level circuit breaker)."""
    pos = positions.get(ticker, {})
    current_price = float(pos.get("current_price", 0))
    current_shares = float(pos.get("qty", 0))

    if action == "sell" and current_shares <= 0:
        return False, f"No long position in {ticker} to sell"
    if action == "cover" and current_shares >= 0:
        return False, f"No short position in {ticker} to cover"

    daily_pnl_pct = 0.0  # circuit breaker is now handled by V2 risk manager
    if action in ("buy", "short") and daily_pnl_pct <= -daily_loss_limit:
        return False, (
            f"Circuit breaker: down {abs(daily_pnl_pct)*100:.1f}% today "
            f"(limit {daily_loss_limit*100:.0f}%). No new entries until tomorrow."
        )

    if current_price > 0:
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
        "--mode",
        choices=["swing", "day"],
        default=None,
        help="Trading mode (overrides TRADING_MODE env, default: swing)",
    )
    args = parser.parse_args()

    # Resolve mode: CLI flag > env var > default
    mode = args.mode or os.environ.get("TRADING_MODE", "swing")
    mode_config = get_mode_config(mode)
    mode_risk = mode_config["risk"]

    log.info(f"Trading mode: {mode.upper()} — {mode_config['label']}")

    # Handle flatten command separately (works in both modes)
    if args.flatten:
        result = flatten_all(dry_run=args.dry_run)
        print(json.dumps(result, indent=2))
        return 0

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

    # Get current state
    account = get_account()
    positions = get_positions()
    portfolio_value = float(account.get("equity", 0))
    daily_pnl_pct = get_daily_pnl(account)

    daily_loss_limit = mode_risk["daily_loss_limit"]
    max_trades = mode_risk["max_trades_per_day"]
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
    executed = 0

    for trade in trades[:max_trades]:
        ticker = trade["ticker"]
        action = trade["action"].lower()
        qty = int(trade.get("qty", 0))
        reasoning = trade.get("reasoning", "")
        order_type = trade.get("order_type", "market")
        stop_price = trade.get("stop_price")
        take_profit = trade.get("take_profit")
        limit_price = trade.get("limit_price")
        trail_percent = trade.get("trail_percent")
        entry_price = trade.get("entry_price")

        if action == "hold" or qty <= 0:
            results.append({"ticker": ticker, "action": action, "status": "skipped", "reason": "Hold or zero qty"})
            continue

        # ── V2 Risk Manager validation (buy/short entries only) ──────────────
        if action in ("buy", "short") and RISK_MANAGER_AVAILABLE:
            pos = positions.get(ticker, {})
            ref_price = entry_price or limit_price or float(pos.get("current_price", 0))
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
                })
                continue
            log.info(f"V2 risk manager approved {action} {ticker}")

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
            results.append({"ticker": ticker, "action": action, "qty": qty, "status": "blocked", "reason": reason})
            continue

        if args.dry_run:
            dry_entry = {
                "ticker": ticker, "action": action, "qty": qty,
                "status": "would_execute", "reasoning": reasoning,
                "order_type": order_type,
            }
            if stop_price is not None:
                dry_entry["stop_price"] = stop_price
            if take_profit is not None:
                dry_entry["take_profit"] = take_profit
            if limit_price is not None:
                dry_entry["limit_price"] = limit_price
            if trail_percent is not None:
                dry_entry["trail_percent"] = trail_percent
            results.append(dry_entry)
            executed += 1
        else:
            # Pass stop_pct only in swing mode so place_order() can auto-calc if needed
            result = place_order(
                ticker, action, qty,
                order_type=order_type,
                stop_price=stop_price, take_profit=take_profit,
                limit_price=limit_price, trail_percent=trail_percent,
                entry_price=entry_price,
                stop_pct=stop_loss_pct if mode == "swing" else None,
            )
            status = "executed" if result["success"] else "failed"
            results.append({
                "ticker": ticker, "action": action, "qty": qty,
                "status": status, "reasoning": reasoning,
                **result,
            })
            if result["success"]:
                executed += 1

    output = {
        "timestamp": datetime.now().isoformat(),
        "trading_mode": mode,
        "mode": "dry_run" if args.dry_run else "live",
        "daily_pnl_pct": round(daily_pnl_pct * 100, 2),
        "circuit_breaker_active": daily_pnl_pct <= -daily_loss_limit,
        "total_trades": len(trades),
        "executed": executed,
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

    # Auto-snapshot performance
    try:
        from performance_tracker_v2 import take_snapshot
        take_snapshot()
    except Exception as e:
        print(f"⚠️ Performance snapshot failed: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
