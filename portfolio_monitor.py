#!/usr/bin/env python3
"""
Portfolio Monitor — Swarm Trader stop-loss enforcer and health check.

Designed to run on cron (e.g., 9:30 AM and 3:30 PM ET). Checks each open
position against its configured hard stop. Auto-sells positions that breach it.
In day mode, auto-flattens all positions after the configured deadline. Compares
daily performance to SPY.

Usage:
  poetry run python portfolio_monitor.py              # Full check + auto-sell
  poetry run python portfolio_monitor.py --dry-run     # Report only, no sells
  poetry run python portfolio_monitor.py --mode day  # Day trading mode
  poetry run python portfolio_monitor.py --mode day --dry-run
"""

import argparse
import hashlib
import json
import logging
import math
import os
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(override=True)

import requests
import yfinance as yf

from src.config import get_mode_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [portfolio_monitor] %(levelname)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("portfolio_monitor")
MARKET_TZ = ZoneInfo("America/New_York")

from src.accounts import get_account_for_mode, verify_account_for_mode
from src.market_calendar import get_day_schedule

API_BASE = "https://paper-api.alpaca.markets/v2"

# Module-level mode — set once when run_monitor() starts, used by helpers
_active_mode: str = "swing"


def _headers() -> dict:
    """Get API headers for the currently active trading mode."""
    return get_account_for_mode(_active_mode).headers


# ---------------------------------------------------------------------------
# Alpaca helpers
# ---------------------------------------------------------------------------

def _get(endpoint: str) -> dict | list:
    r = requests.get(f"{API_BASE}/{endpoint}", headers=_headers(), timeout=10)
    r.raise_for_status()
    return r.json()


def get_account() -> dict:
    return _get("account")


def get_positions() -> list[dict]:
    return _get("positions")


def place_market_close(symbol: str, qty: float, reason: str, dry_run: bool) -> dict:
    """Submit one verified sell/cover that reduces the held position."""
    action = "sell" if qty > 0 else "cover"
    verb = "sell" if action == "sell" else "cover"
    if dry_run:
        msg = f"DRY-RUN: Would {verb} {abs(qty)} {symbol} — {reason}"
        log.info(msg)
        return {"status": "dry_run", "symbol": symbol, "qty": abs(qty), "action": action, "reason": reason}

    from execute_trades import (
        _submit_broker_order, account_execution_lock, cancel_open_orders_for_symbol,
    )

    try:
        with account_execution_lock(_active_mode):
            verify_account_for_mode(_active_mode)
            if _active_mode == "day":
                risk = get_mode_config("day")["risk"]
                schedule = get_day_schedule(
                    flatten_by=risk.get("flatten_by", "15:45"),
                    flatten_lead_minutes=risk.get("flatten_lead_minutes", 15),
                    entry_cutoff_minutes=risk.get("entry_cutoff_minutes", 15),
                )
                now = datetime.now(MARKET_TZ)
                if schedule is None or not schedule.market_open(now) or schedule.flatten_due(now):
                    return {"status": "failed", "symbol": symbol, "qty": abs(qty), "action": action,
                            "reason": reason, "error": "Day stop close requires an open session before the flatten deadline"}
            cancelled = cancel_open_orders_for_symbol(symbol, mode=_active_mode)
            if not cancelled.get("success"):
                return {"status": "failed", "symbol": symbol, "qty": abs(qty), "action": action,
                        "reason": reason, "error": cancelled.get("error")}
            fresh = next((p for p in get_positions() if str(p.get("symbol", "")).upper() == symbol.upper()), None)
            held_qty = Decimal(str((fresh or {}).get("qty", 0)))
            if held_qty == 0:
                return {"status": "already_flat", "symbol": symbol, "qty": 0, "action": action, "reason": reason}
            if (action == "sell" and held_qty < 0) or (action == "cover" and held_qty > 0):
                return {"status": "failed", "symbol": symbol, "qty": abs(qty), "action": action,
                        "reason": reason, "error": "Position direction changed before stop close; refusing the order"}
            close_qty = abs(held_qty)
            event = reason.split(":", 1)[0].strip().lower()
            identity = f"{_active_mode}:{datetime.now(MARKET_TZ).date()}:{symbol}:{action}:{close_qty}:{event}"
            client_order_id = f"stop-{hashlib.sha256(identity.encode()).hexdigest()[:20]}"
            order = {
                "symbol": symbol,
                "qty": format(close_qty, "f"),
                "side": "sell" if action == "sell" else "buy",
                "position_intent": "sell_to_close" if action == "sell" else "buy_to_close",
                "type": "market",
                "time_in_force": "day",
                "client_order_id": client_order_id,
            }
            result = _submit_broker_order(order, "market", mode=_active_mode)
            if not result.get("success"):
                return {"status": "failed", "symbol": symbol, "qty": str(close_qty), "action": action,
                        "reason": reason, "client_order_id": client_order_id, **result}
            log.info("Submitted %s %s %s for %s, order %s", close_qty, symbol, verb, reason, result["order_id"])
            return {"status": "submitted", "symbol": symbol, "qty": str(close_qty), "action": action,
                    "reason": reason, "client_order_id": client_order_id, **result}
    except (requests.RequestException, ValueError, TimeoutError) as exc:
        return {"status": "failed", "symbol": symbol, "qty": abs(qty), "action": action, "reason": reason, "error": str(exc)}


# ---------------------------------------------------------------------------
# Benchmark data helper
# ---------------------------------------------------------------------------


def get_spy_daily_return() -> float | None:
    """Get SPY's return for today using yfinance."""
    try:
        spy = yf.Ticker("SPY")
        hist = spy.history(period="2d", interval="1d")
        if len(hist) < 2:
            log.warning("Yahoo Finance returned fewer than two daily bars for SPY")
            return None
        prev_close = float(hist["Close"].iloc[-2])
        last_close = float(hist["Close"].iloc[-1])
        return (last_close - prev_close) / prev_close
    except Exception as e:
        log.warning(f"Could not fetch SPY return: {e}")
        return None


# ---------------------------------------------------------------------------
# Stop-loss checks
# ---------------------------------------------------------------------------

def check_hard_stop(position: dict, stop_loss_pct: float) -> tuple[bool, str]:
    """
    Check if position has breached the hard stop from avg_entry_price.

    Returns (triggered, reason_string).
    """
    avg_entry = float(position.get("avg_entry_price", 0))
    current_price = float(position.get("current_price", 0))
    if not all(math.isfinite(v) and v > 0 for v in (avg_entry, current_price)):
        return False, ""

    pct_change = (current_price - avg_entry) / avg_entry
    adverse_move = pct_change if float(position.get("qty", 0) or 0) < 0 else -pct_change
    if adverse_move >= stop_loss_pct:
        return True, (
            f"Hard stop triggered: {adverse_move*100:.1f}% adverse move from entry "
            f"(entry ${avg_entry:.2f}, current ${current_price:.2f}, "
            f"threshold {stop_loss_pct*100:.1f}%)"
        )
    return False, ""


# ---------------------------------------------------------------------------
# Main monitor logic
# ---------------------------------------------------------------------------

def run_monitor(dry_run: bool = False, mode: str = None) -> dict:
    """
    Check all positions against stops, sell violators, and print summary.

    Args:
        dry_run: If True, report only — do not execute sells.
        mode:    "swing" or "day". Reads TRADING_MODE env if not specified.

    Returns a result dict with actions taken.
    """
    if mode is None:
        mode = os.environ.get("TRADING_MODE", "swing")

    # Set module-level mode so all API helpers route to the correct account
    global _active_mode
    _active_mode = mode

    mode_config = get_mode_config(mode)
    mode_risk = mode_config["risk"]
    stop_loss_pct = mode_risk["stop_loss_pct"]

    log.info(f"Portfolio monitor starting (mode={mode}, dry_run={dry_run}, account={get_account_for_mode(mode).name})")

    # Day liquidation is time-sensitive. Run it before account reporting,
    # benchmark downloads or other analytics can delay the close.
    if mode_risk.get("flatten_eod", False):
        try:
            schedule = get_day_schedule(
                flatten_by=mode_risk.get("flatten_by", "15:45"),
                flatten_lead_minutes=mode_risk.get("flatten_lead_minutes", 15),
                entry_cutoff_minutes=mode_risk.get("entry_cutoff_minutes", 15),
            )
            flatten_due = schedule is None or schedule.flatten_due(datetime.now(MARKET_TZ))
        except Exception as exc:
            calendar_error = f"Day market calendar unavailable: {exc}"
            log.error(calendar_error)
            return {
                "timestamp": datetime.now(MARKET_TZ).isoformat(),
                "trading_mode": mode,
                "mode": "dry_run" if dry_run else "live",
                "status": "failed",
                "success": False,
                "eod_flatten": False,
                "eod_flatten_attempted": False,
                "actions": [],
                "warnings": [calendar_error],
            }
        if flatten_due:
            from execute_trades import flatten_all
            try:
                flatten_result = flatten_all(dry_run=dry_run, mode=mode)
            except Exception as exc:
                flatten_result = {"status": "failed", "verified_flat": False, "results": [], "errors": [str(exc)]}
            for error in flatten_result.get("errors", []):
                log.error("Day flatten: %s", error)
            return {
                "timestamp": datetime.now(MARKET_TZ).isoformat(),
                "trading_mode": mode,
                "mode": "dry_run" if dry_run else "live",
                "status": "dry_run" if dry_run else flatten_result.get("status", "failed"),
                "success": bool(flatten_result.get("success")) if not dry_run else True,
                "eod_flatten": bool(flatten_result.get("verified_flat")),
                "eod_flatten_attempted": True,
                "flatten_result": flatten_result,
                "actions": [
                    {**row, "stop_type": "eod_flatten"}
                    for row in flatten_result.get("results", [])
                ],
                "warnings": flatten_result.get("errors", []),
            }

    account = get_account()
    positions = get_positions()

    equity = float(account.get("equity", 0))
    cash = float(account.get("cash", 0))
    last_equity = float(account.get("last_equity", equity))
    daily_pnl = equity - last_equity
    daily_pnl_pct = (daily_pnl / last_equity) if last_equity else 0

    actions = []
    warnings = []
    open_positions = [p for p in positions if float(p.get("qty", 0)) != 0]

    position_data_failed = False

    # ── Per-position hard-stop checks ────────────────────────────────────────
    for pos in open_positions:
        symbol = pos["symbol"]
        qty = float(pos["qty"])
        is_short = qty < 0

        prices = (float(pos.get("avg_entry_price", 0)), float(pos.get("current_price", 0)))
        if not math.isfinite(qty) or not all(math.isfinite(v) and v > 0 for v in prices):
            position_data_failed = True
            warnings.append(f"{symbol}: hard stop check unavailable (invalid_position_price_or_quantity)")
            continue
        hard_triggered, hard_reason = check_hard_stop(pos, stop_loss_pct)
        if hard_triggered:
            result = place_market_close(symbol, qty, hard_reason, dry_run)
            actions.append({**result, "stop_type": "hard_stop"})
            continue

        # Warn if approaching stops (within 20% of threshold)
        avg_entry = float(pos.get("avg_entry_price", 0))
        current_price = float(pos.get("current_price", 0))
        warn_buffer = stop_loss_pct * 0.2  # warn at 80% of stop depth

        if avg_entry > 0 and current_price > 0:
            pct_from_entry = (current_price - avg_entry) / avg_entry
            adverse_move = pct_from_entry if is_short else -pct_from_entry
            if adverse_move >= stop_loss_pct - warn_buffer:
                warnings.append(
                    f"⚠️  {symbol}: approaching hard stop at "
                    f"{adverse_move*100:.1f}% adverse from entry (stop at {stop_loss_pct*100:.1f}%)"
                )


    spy_return = get_spy_daily_return()
    _print_summary(
        equity=equity, cash=cash, daily_pnl=daily_pnl, daily_pnl_pct=daily_pnl_pct,
        spy_return=spy_return, positions=open_positions, actions=actions, warnings=warnings,
        dry_run=dry_run, mode=mode, stop_loss_pct=stop_loss_pct,
    )

    action_failed = any(a.get("status") == "failed" for a in actions)
    return {
        "timestamp": datetime.now(MARKET_TZ).isoformat(),
        "trading_mode": mode,
        "mode": "dry_run" if dry_run else "live",
        "equity": equity,
        "daily_pnl_pct": round(daily_pnl_pct * 100, 2),
        "spy_return_pct": round(spy_return * 100, 2) if spy_return is not None else None,
        "positions_checked": len(open_positions),
        "stops_triggered": len(actions),
        "status": "failed" if action_failed else "degraded" if position_data_failed else "ok",
        "success": not action_failed and not position_data_failed,
        "exit_code": 2 if action_failed or position_data_failed else 0,
        "eod_flatten": False,
        "actions": actions,
        "warnings": warnings,
    }


def _print_summary(
    equity: float,
    cash: float,
    daily_pnl: float,
    daily_pnl_pct: float,
    spy_return: float | None,
    positions: list[dict],
    actions: list[dict],
    warnings: list[str],
    dry_run: bool,
    mode: str,
    stop_loss_pct: float,
) -> None:
    """Pretty-print the monitor summary to stdout."""
    mode_config = get_mode_config(mode)
    now = datetime.now(MARKET_TZ).strftime("%Y-%m-%d %H:%M %Z")

    print(f"{'='*65}")
    mode_tag = " [DRY RUN]" if dry_run else ""
    print(f"  SWARM TRADER — PORTFOLIO MONITOR{mode_tag}")
    print(f"  Mode: {mode.upper()} — {mode_config['label']}")
    print(f"  Hard stop: -{stop_loss_pct*100:.1f}% from entry")
    print(f"  {now}")
    print(f"{'='*65}")
    print(f"  Equity:     ${equity:>13,.2f}")
    print(f"  Cash:       ${cash:>13,.2f}  ({cash/equity*100:.1f}% of equity)")

    print(f"  Daily P&L:  ${daily_pnl:>+13,.2f}  ({daily_pnl_pct*100:+.2f}%)")

    if spy_return is not None:
        alpha = daily_pnl_pct - spy_return
        beat = "BEATING" if alpha > 0 else "LAGGING"
        print(f"  SPY Today:   {spy_return*100:>+13.2f}%")
        print(f"  Alpha:       {alpha*100:>+13.2f}%  ({beat} SPY)")
    else:
        print(f"  SPY Today:   (unavailable)")

    print(f"  Positions:  {len(positions):>13}")
    print()

    if positions:
        print(f"  {'Symbol':<8} {'Qty':>5} {'Price':>9} {'Entry':>9} {'From Entry':>11} {'Value':>12}")
        print(f"  {'-'*60}")
        warn_pct = stop_loss_pct * 0.8  # flag approaching threshold
        for pos in sorted(positions, key=lambda x: float(x.get("market_value", 0)), reverse=True):
            sym = pos["symbol"]
            qty = int(float(pos["qty"]))
            price = float(pos.get("current_price", 0))
            entry = float(pos.get("avg_entry_price", 0))
            value = float(pos.get("market_value", 0))
            plpc = float(pos.get("unrealized_plpc", 0)) * 100
            flag = " 🔴" if plpc / 100 <= -stop_loss_pct else (" ⚠️" if plpc / 100 <= -warn_pct else "")
            print(f"  {sym:<8} {qty:>5} ${price:>8.2f} ${entry:>8.2f} {plpc:>+10.1f}% ${value:>10,.0f}{flag}")
    print()

    if actions:
        print(f"  STOP-LOSS ACTIONS TAKEN ({len(actions)}):")
        for a in actions:
            tag = "[DRY-RUN] " if dry_run else ""
            stop_type = a.get("stop_type", "").upper()
            print(f"    {tag}{stop_type}: {a.get('symbol', a.get('ticker', '?'))} — {str(a.get('reason', ''))[:80]}")
        print()

    if warnings:
        print(f"  APPROACHING STOPS:")
        for w in warnings:
            print(f"    {w}")
        print()

    if not actions and not warnings:
        print("  All positions within hard-stop threshold.")
        print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def monitor_exit_code(result: dict) -> int:
    """Return nonzero if a close fails or a position cannot be checked."""
    return 0 if result.get("success") else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Swarm Trader Portfolio Monitor")
    parser.add_argument(
        "--dry-run", "--no-sell",
        action="store_true",
        help="Report only — do not execute sells",
    )
    parser.add_argument(
        "--mode",
        choices=["swing", "day"],
        default=None,
        help="Trading mode (overrides TRADING_MODE env, default: swing)",
    )
    args = parser.parse_args()

    mode = args.mode or os.environ.get("TRADING_MODE", "swing")

    try:
        result = run_monitor(dry_run=args.dry_run, mode=mode)
    except Exception as e:
        log.error(f"Monitor failed: {e}", exc_info=True)
        sys.exit(1)
    print(json.dumps(result, indent=2))
    sys.exit(monitor_exit_code(result))
