#!/usr/bin/env python3
"""
Fast Alpaca Portfolio Check — No LLM, just data.

Usage:
  poetry run python check_portfolio.py                    # Summary
  poetry run python check_portfolio.py --mode swing       # Swing mode categories
  poetry run python check_portfolio.py --mode day         # Day mode categories
  poetry run python check_portfolio.py --telegram         # Telegram-formatted
  poetry run python check_portfolio.py --json             # JSON output
"""

import argparse
import json
import os
import sys
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

import requests

from src.config import get_mode_config, resolve_mode

API_BASE = "https://paper-api.alpaca.markets/v2"
HEADERS = {
    "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY", ""),
    "APCA-API-SECRET-KEY": os.environ.get("ALPACA_API_SECRET", ""),
}


def api(endpoint):
    r = requests.get(f"{API_BASE}/{endpoint}", headers=HEADERS)
    r.raise_for_status()
    return r.json()


def main():
    parser = argparse.ArgumentParser(description="Fast Alpaca portfolio check")
    parser.add_argument("--mode", choices=["swing", "day"], default=None,
                        help="Trading mode (default: resolved from trading_mode.json / env)")
    parser.add_argument("--telegram", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    mode = resolve_mode(cli_mode=args.mode)
    mode_config = get_mode_config(mode)
    universe = mode_config["universe"]

    # Build category map: {cat_key: [tickers]} and flat set of all tickers
    category_map = {cat_key: cat_data["tickers"] for cat_key, cat_data in universe.items()}
    all_universe = {t for tickers in category_map.values() for t in tickers}

    account = api("account")
    positions = api("positions")

    equity = float(account["equity"])
    cash = float(account["cash"])
    last_equity = float(account.get("last_equity", equity))
    daily_pl = equity - last_equity
    daily_pl_pct = (daily_pl / last_equity * 100) if last_equity else 0

    # Process positions
    pos_data = []
    for p in positions:
        sym = p["symbol"]
        qty = float(p["qty"])
        market_value = float(p["market_value"])
        unrealized_pl = float(p["unrealized_pl"])
        unrealized_plpc = float(p["unrealized_plpc"]) * 100
        current_price = float(p["current_price"])
        avg_entry = float(p["avg_entry_price"])
        in_universe = sym in all_universe
        category = next((k for k, v in category_map.items() if sym in v), "other")

        pos_data.append({
            "symbol": sym,
            "qty": qty,
            "market_value": market_value,
            "unrealized_pl": unrealized_pl,
            "unrealized_plpc": unrealized_plpc,
            "current_price": current_price,
            "avg_entry": avg_entry,
            "in_universe": in_universe,
            "category": category,
            "weight": (market_value / equity * 100) if equity else 0,
        })

    pos_data.sort(key=lambda x: x["market_value"], reverse=True)

    # Big movers (>5% swing)
    big_movers = [p for p in pos_data if abs(p["unrealized_plpc"]) > 5]

    # Out-of-universe positions
    out_of_universe = [p for p in pos_data if not p["in_universe"] and p["market_value"] > 100]

    if args.json:
        print(json.dumps({
            "mode": mode,
            "equity": equity,
            "cash": cash,
            "daily_pl": daily_pl,
            "daily_pl_pct": daily_pl_pct,
            "positions": pos_data,
            "big_movers": [p["symbol"] for p in big_movers],
            "out_of_universe": [p["symbol"] for p in out_of_universe],
            "timestamp": datetime.now().isoformat(),
        }, indent=2))
        return

    if args.telegram:
        output_telegram(mode, mode_config, equity, cash, daily_pl, daily_pl_pct,
                        pos_data, big_movers, out_of_universe, category_map)
    else:
        output_terminal(mode, mode_config, equity, cash, daily_pl, daily_pl_pct,
                        pos_data, big_movers, out_of_universe)


def output_telegram(mode, mode_config, equity, cash, daily_pl, daily_pl_pct,
                    positions, big_movers, out_of_universe, category_map):
    pl_emoji = "📈" if daily_pl >= 0 else "📉"
    print(f"💰 Apex Fund — Portfolio Check [{mode.upper()}]")
    print(f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M PST')}")
    print()
    print(f"Portfolio: ${equity:,.2f}")
    print(f"Cash: ${cash:,.2f} ({cash/equity*100:.1f}%)")
    print(f"{pl_emoji} Daily P/L: ${daily_pl:+,.2f} ({daily_pl_pct:+.2f}%)")
    print()

    # Top 5 by value
    print("📊 Top 5 Holdings:")
    for p in positions[:5]:
        pl_sign = "+" if p["unrealized_pl"] >= 0 else ""
        print(f"  • {p['symbol']}: ${p['market_value']:,.2f} ({p['weight']:.1f}%) — {pl_sign}${p['unrealized_pl']:,.2f}")
    print()

    # Category breakdown using active mode categories
    categories: dict[str, float] = {}
    for p in positions:
        cat = p["category"]
        categories[cat] = categories.get(cat, 0) + p["market_value"]

    print("🏷️ Allocation:")
    for cat_key, cat_data in mode_config["universe"].items():
        if cat_key in categories:
            pct = categories[cat_key] / equity * 100
            label = cat_data["label"]
            cap_pct = cat_data.get("max_sector_pct", 0) * 100
            print(f"  • {label}: ${categories[cat_key]:,.2f} ({pct:.1f}% / {cap_pct:.0f}% cap)")
    if "other" in categories:
        pct = categories["other"] / equity * 100
        print(f"  • Other: ${categories['other']:,.2f} ({pct:.1f}%)")
    print()

    if big_movers:
        print("🚨 Big Movers (>5% swing):")
        for p in big_movers:
            print(f"  • {p['symbol']}: {p['unrealized_plpc']:+.1f}%")
        print()

    if out_of_universe:
        print("⚠️ Out of Universe:")
        for p in out_of_universe:
            print(f"  • {p['symbol']}: ${p['market_value']:,.2f} ({p['weight']:.1f}%)")


def output_terminal(mode, mode_config, equity, cash, daily_pl, daily_pl_pct,
                    positions, big_movers, out_of_universe):
    print(f"{'='*60}")
    print(f"  APEX FUND — PORTFOLIO CHECK  [{mode.upper()} — {mode_config['label']}]")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M PST')}")
    print(f"{'='*60}")
    print(f"  Equity:   ${equity:>12,.2f}")
    print(f"  Cash:     ${cash:>12,.2f}  ({cash/equity*100:.1f}%)")
    print(f"  Daily P/L: ${daily_pl:>+11,.2f}  ({daily_pl_pct:+.2f}%)")
    print(f"{'='*60}")
    print()

    print(f"  {'Symbol':<8} {'Qty':>6} {'Value':>12} {'Weight':>7} {'P/L':>10} {'P/L%':>7} {'Cat':<14}")
    print(f"  {'-'*66}")
    for p in positions:
        if p["market_value"] < 1:
            continue
        print(
            f"  {p['symbol']:<8} {p['qty']:>6.0f} ${p['market_value']:>10,.2f}"
            f" {p['weight']:>6.1f}% ${p['unrealized_pl']:>+9,.2f}"
            f" {p['unrealized_plpc']:>+6.1f}% {p['category']:<14}"
        )

    if big_movers:
        print(f"\n  🚨 BIG MOVERS (>5% swing):")
        for p in big_movers:
            print(f"    {p['symbol']}: {p['unrealized_plpc']:+.1f}%")

    if out_of_universe:
        print(f"\n  ⚠️  OUT OF UNIVERSE ({mode.upper()} mode):")
        for p in out_of_universe:
            print(f"    {p['symbol']}: ${p['market_value']:,.2f} ({p['weight']:.1f}%)")
    print()


def run_monitoring():
    """Run performance snapshot + alert checks after portfolio check."""
    try:
        from performance_tracker_v2 import take_snapshot
        take_snapshot()
    except Exception as e:
        print(f"⚠️ Performance snapshot failed: {e}", file=sys.stderr)

    try:
        from trade_alerts import check_concentration, check_cash, check_drawdown, check_trading_frequency, log_alerts, get_portfolio as alerts_get_portfolio
        account, positions = alerts_get_portfolio()
        alerts = []
        if account and positions:
            alerts.extend(check_concentration(account, positions))
            alerts.extend(check_cash(account))
        alerts.extend(check_drawdown())
        alerts.extend(check_trading_frequency())
        if alerts:
            log_alerts(alerts)
            print("\n🚨 ALERTS:")
            for a in alerts:
                print(f"  {a['message']}")
    except Exception as e:
        print(f"⚠️ Alert check failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    result = main()
    run_monitoring()
    sys.exit(result or 0)
