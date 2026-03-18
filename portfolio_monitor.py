#!/usr/bin/env python3
"""
Portfolio Monitor — Cassius V2 stop-loss enforcer and health check.

Designed to run on cron (e.g., 9:30 AM and 3:30 PM ET).  Checks every open
position against its hard stop and trailing stop from the active trading mode.
Auto-sells anything that breaches either stop.  In day mode, auto-flattens all
positions if it's past the flatten_by time.  Compares daily performance to SPY.

Usage:
  poetry run python portfolio_monitor.py              # Full check + auto-sell
  poetry run python portfolio_monitor.py --dry-run   # Report only, no sells
  poetry run python portfolio_monitor.py --mode day  # Day trading mode
  poetry run python portfolio_monitor.py --mode day --dry-run
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import requests
import yfinance as yf

from src.config import get_mode_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [portfolio_monitor] %(levelname)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("portfolio_monitor")

API_BASE = "https://paper-api.alpaca.markets/v2"
HEADERS = {
    "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY", ""),
    "APCA-API-SECRET-KEY": os.environ.get("ALPACA_API_SECRET", ""),
    "Content-Type": "application/json",
}


# ---------------------------------------------------------------------------
# Alpaca helpers
# ---------------------------------------------------------------------------

def _get(endpoint: str) -> dict | list:
    r = requests.get(f"{API_BASE}/{endpoint}", headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()


def get_account() -> dict:
    return _get("account")


def get_positions() -> list[dict]:
    return _get("positions")


def place_market_sell(symbol: str, qty: int, reason: str, dry_run: bool) -> dict:
    """Submit a market sell order (or log it in dry-run mode)."""
    if dry_run:
        msg = f"DRY-RUN: Would sell {qty} {symbol} — {reason}"
        log.info(msg)
        return {"status": "dry_run", "symbol": symbol, "qty": qty, "reason": reason}

    order = {
        "symbol": symbol,
        "qty": str(qty),
        "side": "sell",
        "type": "market",
        "time_in_force": "day",
    }
    r = requests.post(f"{API_BASE}/orders", headers=HEADERS, json=order, timeout=10)
    if r.status_code in (200, 201):
        data = r.json()
        log.info(f"SOLD {qty} {symbol} — {reason}. Order ID: {data.get('id')}")
        return {"status": "sold", "symbol": symbol, "qty": qty, "reason": reason, "order_id": data.get("id")}
    else:
        log.error(f"Failed to sell {symbol}: HTTP {r.status_code} — {r.text[:200]}")
        return {"status": "failed", "symbol": symbol, "qty": qty, "reason": reason, "error": r.text[:200]}


# ---------------------------------------------------------------------------
# Price data helpers
# ---------------------------------------------------------------------------

def get_intraday_high(symbol: str) -> float | None:
    """Fetch today's intraday high from yfinance (1m data, last session)."""
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="1d", interval="1m")
        if hist.empty:
            return None
        return float(hist["High"].max())
    except Exception as e:
        log.warning(f"Could not get intraday high for {symbol}: {e}")
        return None


def get_spy_daily_return() -> float | None:
    """Get SPY's return for today using yfinance."""
    try:
        spy = yf.Ticker("SPY")
        hist = spy.history(period="2d", interval="1d")
        if len(hist) < 2:
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
    if avg_entry <= 0 or current_price <= 0:
        return False, ""

    pct_change = (current_price - avg_entry) / avg_entry
    if pct_change <= -stop_loss_pct:
        return True, (
            f"Hard stop triggered: {pct_change*100:.1f}% from entry "
            f"(entry ${avg_entry:.2f}, current ${current_price:.2f}, "
            f"threshold -{stop_loss_pct*100:.1f}%)"
        )
    return False, ""


def check_trailing_stop(position: dict, intraday_high: float | None, trailing_stop_pct: float) -> tuple[bool, str]:
    """
    Check if position has breached the trailing stop from today's intraday high.

    Returns (triggered, reason_string).
    """
    if intraday_high is None:
        return False, ""

    current_price = float(position.get("current_price", 0))
    if intraday_high <= 0 or current_price <= 0:
        return False, ""

    drop_from_high = (current_price - intraday_high) / intraday_high
    if drop_from_high <= -trailing_stop_pct:
        return True, (
            f"Trailing stop triggered: {drop_from_high*100:.1f}% from today's high "
            f"(high ${intraday_high:.2f}, current ${current_price:.2f}, "
            f"threshold -{trailing_stop_pct*100:.1f}%)"
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

    mode_config = get_mode_config(mode)
    mode_risk = mode_config["risk"]
    stop_loss_pct = mode_risk["stop_loss_pct"]
    trailing_stop_pct = mode_risk["trailing_stop_pct"]

    log.info(f"Portfolio monitor starting (mode={mode}, dry_run={dry_run})")

    account = get_account()
    positions = get_positions()

    equity = float(account.get("equity", 0))
    cash = float(account.get("cash", 0))
    last_equity = float(account.get("last_equity", equity))
    daily_pnl = equity - last_equity
    daily_pnl_pct = (daily_pnl / last_equity) if last_equity else 0

    spy_return = get_spy_daily_return()

    actions = []
    warnings = []
    long_positions = [p for p in positions if float(p.get("qty", 0)) > 0]

    # ── Day mode: auto-flatten if past flatten_by time ───────────────────────
    if mode_risk.get("flatten_eod", False):
        flatten_time = mode_risk.get("flatten_by", "15:45")
        h, m = map(int, flatten_time.split(":"))
        now = datetime.now()
        if now.hour > h or (now.hour == h and now.minute >= m):
            log.info(f"Day mode: past {flatten_time} ET — flattening all positions")
            try:
                from execute_trades import flatten_all
                flatten_result = flatten_all(dry_run=dry_run)
                for r in flatten_result.get("results", []):
                    actions.append({**r, "stop_type": "eod_flatten"})
            except Exception as e:
                log.error(f"EOD flatten failed: {e}")

            _print_summary(
                equity=equity, cash=cash, daily_pnl=daily_pnl, daily_pnl_pct=daily_pnl_pct,
                spy_return=spy_return, positions=long_positions, actions=actions, warnings=warnings,
                dry_run=dry_run, mode=mode, stop_loss_pct=stop_loss_pct, trailing_stop_pct=trailing_stop_pct,
            )
            return {
                "timestamp": datetime.now().isoformat(),
                "trading_mode": mode,
                "mode": "dry_run" if dry_run else "live",
                "equity": equity,
                "daily_pnl_pct": round(daily_pnl_pct * 100, 2),
                "spy_return_pct": round(spy_return * 100, 2) if spy_return is not None else None,
                "positions_checked": len(long_positions),
                "stops_triggered": len(actions),
                "eod_flatten": True,
                "actions": actions,
                "warnings": warnings,
            }

    # ── Per-position stop checks ─────────────────────────────────────────────
    for pos in long_positions:
        symbol = pos["symbol"]
        qty = int(float(pos["qty"]))

        intraday_high = get_intraday_high(symbol)

        hard_triggered, hard_reason = check_hard_stop(pos, stop_loss_pct)
        if hard_triggered:
            result = place_market_sell(symbol, qty, hard_reason, dry_run)
            actions.append({**result, "stop_type": "hard_stop"})
            continue

        trail_triggered, trail_reason = check_trailing_stop(pos, intraday_high, trailing_stop_pct)
        if trail_triggered:
            result = place_market_sell(symbol, qty, trail_reason, dry_run)
            actions.append({**result, "stop_type": "trailing_stop"})
            continue

        # Warn if approaching stops (within 20% of threshold)
        avg_entry = float(pos.get("avg_entry_price", 0))
        current_price = float(pos.get("current_price", 0))
        warn_buffer = stop_loss_pct * 0.2  # warn at 80% of stop depth

        if avg_entry > 0 and current_price > 0:
            pct_from_entry = (current_price - avg_entry) / avg_entry
            if pct_from_entry <= -(stop_loss_pct - warn_buffer):
                warnings.append(
                    f"⚠️  {symbol}: approaching hard stop at "
                    f"{pct_from_entry*100:.1f}% from entry (stop at -{stop_loss_pct*100:.1f}%)"
                )

        if intraday_high and intraday_high > 0 and current_price > 0:
            trail_warn = trailing_stop_pct * 0.2
            pct_from_high = (current_price - intraday_high) / intraday_high
            if pct_from_high <= -(trailing_stop_pct - trail_warn):
                warnings.append(
                    f"⚠️  {symbol}: approaching trailing stop at "
                    f"{pct_from_high*100:.1f}% from today's high (stop at -{trailing_stop_pct*100:.1f}%)"
                )

    _print_summary(
        equity=equity, cash=cash, daily_pnl=daily_pnl, daily_pnl_pct=daily_pnl_pct,
        spy_return=spy_return, positions=long_positions, actions=actions, warnings=warnings,
        dry_run=dry_run, mode=mode, stop_loss_pct=stop_loss_pct, trailing_stop_pct=trailing_stop_pct,
    )

    return {
        "timestamp": datetime.now().isoformat(),
        "trading_mode": mode,
        "mode": "dry_run" if dry_run else "live",
        "equity": equity,
        "daily_pnl_pct": round(daily_pnl_pct * 100, 2),
        "spy_return_pct": round(spy_return * 100, 2) if spy_return is not None else None,
        "positions_checked": len(long_positions),
        "stops_triggered": len(actions),
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
    trailing_stop_pct: float,
) -> None:
    """Pretty-print the monitor summary to stdout."""
    mode_config = get_mode_config(mode)
    mode_tag = " [DRY RUN]" if dry_run else ""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    print(f"{'='*65}")
    print(f"  CASSIUS V2 — PORTFOLIO MONITOR{mode_tag}")
    print(f"  Mode: {mode.upper()} — {mode_config['label']}")
    print(f"  Stops: -{stop_loss_pct*100:.1f}% hard  /  -{trailing_stop_pct*100:.1f}% trailing")
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
        print(f"  {'Symbol':<8} {'Qty':>5} {'Price':>9} {'Entry':>9} {'From Entry':>11} {'Value':>12} {'Intraday Hi':>13}")
        print(f"  {'-'*71}")
        warn_pct = stop_loss_pct * 0.8  # flag approaching threshold
        for pos in sorted(positions, key=lambda x: float(x.get("market_value", 0)), reverse=True):
            sym = pos["symbol"]
            qty = int(float(pos["qty"]))
            price = float(pos.get("current_price", 0))
            entry = float(pos.get("avg_entry_price", 0))
            value = float(pos.get("market_value", 0))
            plpc = float(pos.get("unrealized_plpc", 0)) * 100
            intra_high = get_intraday_high(sym)
            high_str = f"${intra_high:>9.2f}" if intra_high else "     N/A"
            flag = " 🔴" if plpc / 100 <= -stop_loss_pct else (" ⚠️" if plpc / 100 <= -warn_pct else "")
            print(f"  {sym:<8} {qty:>5} ${price:>8.2f} ${entry:>8.2f} {plpc:>+10.1f}% ${value:>10,.0f} {high_str}{flag}")
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
        print("  All positions within stop thresholds.")
        print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cassius V2 Portfolio Monitor")
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
