#!/usr/bin/env python3
"""
Trade Alerts — Monitors for anomalies in Agent's trading behavior.

All thresholds pulled from active mode config — no hardcoded values.

Usage:
  poetry run python trade_alerts.py --check              # Check all alerts
  poetry run python trade_alerts.py --check --mode day   # Day mode thresholds
  poetry run python trade_alerts.py --check --telegram   # Output for Telegram
  poetry run python trade_alerts.py --audit decisions.json  # Audit before execution

Alerts:
  1. Position concentration: any single position > max_position_pct
  2. Sector concentration: any category > max_sector_pct
  3. Rapid trading: > max_trades_per_day trades in a day
  4. Cash depletion: cash < min_cash_pct of equity
  5. Drawdown: equity down >10% from peak (configurable)
  6. Trade size: individual trade >10% of portfolio (pre-execution check)
"""

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

import requests

from src.config import get_mode_config, resolve_mode
from src.accounts import verify_account_for_mode
from performance_tracker_v2 import load_snapshots

ALERTS_LOG = Path(__file__).parent / "data" / "alerts.jsonl"
JOURNAL_PATH = Path(__file__).parent / "data" / "trade_journal.jsonl"

# Drawdown alert threshold (not in mode config — applies to both modes)
MAX_DRAWDOWN_PCT = 10.0
# Trade size alert threshold (pre-execution check, as % of portfolio)
MAX_TRADE_SIZE_PCT = 10.0


def get_portfolio(mode: str = "swing"):
    account_route, account = verify_account_for_mode(mode)
    response = requests.get(
        f"{account_route.base_url}/positions",
        headers=account_route.headers,
        timeout=10,
    )
    response.raise_for_status()
    return account, response.json()


def check_concentration(account, positions, mode: str = "swing"):
    """Check position and sector concentration using mode risk limits."""
    alerts = []
    equity = float(account.get("equity", 0))
    if equity <= 0:
        return alerts

    risk = get_mode_config(mode)["risk"]
    universe = get_mode_config(mode)["universe"]
    category_map = {cat_key: cat_data["tickers"] for cat_key, cat_data in universe.items()}

    max_position_pct = risk["max_position_pct"] * 100  # convert to %

    # Position concentration
    for p in positions:
        sym = p["symbol"]
        mv = float(p.get("market_value", 0))
        pct = mv / equity * 100

        if pct > max_position_pct:
            alerts.append({
                "level": "warning",
                "type": "position_concentration",
                "message": f"🚨 {sym} is {pct:.1f}% of portfolio (max {max_position_pct:.0f}%)",
                "ticker": sym,
                "value": pct,
                "threshold": max_position_pct,
            })

    # Sector concentration — use per-sector caps from mode universe
    categories: dict[str, float] = {}
    for p in positions:
        sym = p["symbol"]
        cat = next((k for k, v in category_map.items() if sym in v), "other")
        categories[cat] = categories.get(cat, 0) + float(p.get("market_value", 0))

    for cat, value in categories.items():
        pct = value / equity * 100
        # Use per-sector cap if defined, else fall back to mode max_sector_pct
        sector_cap = universe.get(cat, {}).get("max_sector_pct", risk["max_sector_pct"]) * 100
        if pct > sector_cap:
            cat_label = universe.get(cat, {}).get("label", cat)
            alerts.append({
                "level": "warning",
                "type": "sector_concentration",
                "message": f"⚠️ {cat_label} sector is {pct:.1f}% of portfolio (max {sector_cap:.0f}%)",
                "category": cat,
                "value": pct,
                "threshold": sector_cap,
            })

    return alerts


def check_cash(account, mode: str = "swing"):
    """Check cash levels against mode's min_cash_pct."""
    equity = float(account.get("equity", 0))
    cash = float(account.get("cash", 0))
    if equity <= 0:
        return []

    risk = get_mode_config(mode)["risk"]
    min_cash_pct = risk["min_cash_pct"] * 100  # convert to %

    cash_pct = cash / equity * 100
    if cash_pct < min_cash_pct:
        return [{
            "level": "warning",
            "type": "low_cash",
            "message": f"💸 Cash at {cash_pct:.1f}% (${cash:,.2f}) — below {min_cash_pct:.0f}% threshold",
            "value": cash_pct,
            "threshold": min_cash_pct,
        }]
    return []


def check_drawdown(mode: str = "swing", account_id: str | None = None):
    """Check drawdown from the selected account's equity snapshots."""
    if account_id is None:
        _, broker = verify_account_for_mode(mode)
        account_id = str(broker.get("id") or broker.get("account_number") or "")
    if not account_id:
        raise ValueError("Broker account ID is unavailable for drawdown check")
    snapshots = load_snapshots(mode=mode, account_id=account_id)
    if len(snapshots) < 2:
        return []

    peak = max(float(s["equity"]) for s in snapshots)
    current = float(snapshots[-1]["equity"])
    if peak <= 0:
        return []
    drawdown = (peak - current) / peak * 100

    if drawdown > MAX_DRAWDOWN_PCT:
        return [{
            "level": "critical",
            "type": "drawdown",
            "message": f"🔥 Portfolio down {drawdown:.1f}% from peak ${peak:,.2f} → ${current:,.2f}",
            "value": drawdown,
            "peak": peak,
            "current": current,
            "threshold": MAX_DRAWDOWN_PCT,
        }]
    return []


def check_trading_frequency(mode: str = "swing", account_id: str | None = None):
    """Check if too many trades in a single day against mode's max_trades_per_day."""
    if not JOURNAL_PATH.exists():
        return []

    risk = get_mode_config(mode)["risk"]
    if account_id is None:
        _, broker = verify_account_for_mode(mode)
        account_id = str(broker.get("id") or broker.get("account_number") or "")
    if not account_id:
        raise ValueError("Broker account ID is unavailable for frequency check")
    max_daily_trades = risk["max_trades_per_day"]

    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    count = 0
    with open(JOURNAL_PATH) as f:
        for line in f:
            try:
                entry = json.loads(line.strip())
                if (
                    entry.get("timestamp", "")[:10] == today
                    and entry.get("trading_mode") == mode
                    and entry.get("account_id") == account_id
                    and entry.get("status") in ("submitted", "executed", "accepted", "new", "pending_new", "filled", "partially_filled")
                ):
                    count += 1
            except json.JSONDecodeError:
                continue

    if count > max_daily_trades:
        return [{
            "level": "warning",
            "type": "high_frequency",
            "message": f"⚡ {count} trades today — exceeds {max_daily_trades} daily threshold ({mode} mode)",
            "value": count,
            "threshold": max_daily_trades,
        }]
    return []


def audit_decisions(decisions_file, mode: str = "swing"):
    """Pre-execution audit of trade decisions."""
    if decisions_file == "-":
        decisions = json.load(sys.stdin)
    else:
        with open(decisions_file) as f:
            decisions = json.load(f)

    account, positions = get_portfolio(mode=mode)

    equity = float(account.get("equity", 0))
    alerts = []
    trades = decisions.get("trades", [])

    for trade in trades:
        ticker = trade.get("ticker", "?")
        qty = int(trade.get("qty", 0))
        action = trade.get("action", "")

        # Estimate trade value
        pos = next((p for p in positions if p["symbol"] == ticker), None)
        price = float(pos.get("current_price", 0)) if pos else 0

        if price > 0 and equity > 0:
            trade_value = qty * price
            trade_pct = trade_value / equity * 100
            if trade_pct > MAX_TRADE_SIZE_PCT:
                alerts.append({
                    "level": "warning",
                    "type": "large_trade",
                    "message": f"⚠️ {action.upper()} {ticker} x{qty} = ${trade_value:,.0f} ({trade_pct:.1f}% of portfolio)",
                    "ticker": ticker,
                    "value": trade_pct,
                    "threshold": MAX_TRADE_SIZE_PCT,
                })

    return alerts


def log_alerts(alerts, mode: str = "swing"):
    """Append alerts to persistent log."""
    ALERTS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(ALERTS_LOG, "a") as f:
        for alert in alerts:
            alert["timestamp"] = datetime.now().isoformat()
            alert["trading_mode"] = mode
            f.write(json.dumps(alert) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Swarm Trader Alerts")
    parser.add_argument("--check", action="store_true", help="Run all alert checks")
    parser.add_argument("--audit", type=str, help="Audit trade decisions before execution")
    parser.add_argument("--telegram", action="store_true", help="Telegram format")
    parser.add_argument("--mode", choices=["swing", "day"], default=None,
                        help="Trading mode (default: resolved from trading_mode.json / env)")
    args = parser.parse_args()

    mode = resolve_mode(cli_mode=args.mode)
    if mode == "auto":
        mode = "swing"
    alerts = []

    try:
        if args.audit:
            alerts = audit_decisions(args.audit, mode=mode)
        elif args.check:
            account, positions = get_portfolio(mode=mode)
            alerts.extend(check_concentration(account, positions, mode=mode))
            alerts.extend(check_cash(account, mode=mode))
            account_id = str(account.get("id") or account.get("account_number") or "")
            alerts.extend(check_drawdown(mode=mode, account_id=account_id))
            alerts.extend(check_trading_frequency(mode=mode, account_id=account_id))
        else:
            parser.print_help()
            return 0
    except (requests.RequestException, ValueError, KeyError, OSError) as exc:
        print(f"Could not complete {mode.upper()} alert check: {exc}", file=sys.stderr)
        return 1

    if alerts:
        log_alerts(alerts, mode=mode)

        if args.telegram:
            critical = [a for a in alerts if a.get("level") == "critical"]
            warnings = [a for a in alerts if a.get("level") == "warning"]
            print(f"🚨 Swarm Alert [{mode.upper()}] — {len(alerts)} issue{'s' if len(alerts) != 1 else ''}")
            for a in critical:
                print(f"  {a['message']}")
            for a in warnings:
                print(f"  {a['message']}")
        else:
            print(f"{'='*50}")
            print(f"  SWARM ALERTS [{mode.upper()}] — {len(alerts)} issue{'s' if len(alerts) != 1 else ''}")
            print(f"{'='*50}")
            for a in alerts:
                level = "🔴 CRITICAL" if a["level"] == "critical" else "🟡 WARNING"
                print(f"  {level}: {a['message']}")
            print()
    else:
        if args.telegram:
            print(f"✅ No alerts — all within {mode.upper()} mode thresholds")
        else:
            print(f"✅ All clear — no alerts triggered ({mode.upper()} mode)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
