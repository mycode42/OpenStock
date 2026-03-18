#!/usr/bin/env python3
"""
Portfolio Rebalancer — Sell positions not in the active mode's universe.

Finds all current positions outside the active mode's universe and sells them
at market. Defaults to dry-run so you can preview before executing.

Usage:
  poetry run python rebalance.py                        # Dry run, uses resolved mode
  poetry run python rebalance.py --mode swing           # Dry run, swing mode
  poetry run python rebalance.py --mode day --execute   # Execute day mode rebalance
  poetry run python rebalance.py --mode swing --execute # Execute swing mode rebalance
"""

import argparse
import json
import os
import sys
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

from src.config import get_mode_config, resolve_mode

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_API_SECRET = os.getenv("ALPACA_API_SECRET", "")
ALPACA_BASE_URL = "https://paper-api.alpaca.markets/v2"

if not ALPACA_API_KEY or not ALPACA_API_SECRET:
    raise EnvironmentError("ALPACA_API_KEY and ALPACA_API_SECRET must be set in .env")

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_API_SECRET,
    "Content-Type": "application/json",
}


def get_positions():
    resp = requests.get(f"{ALPACA_BASE_URL}/positions", headers=HEADERS, timeout=10)
    resp.raise_for_status()
    return {p["symbol"]: p for p in resp.json()}


def place_sell_order(ticker, qty):
    order = {
        "symbol": ticker,
        "qty": str(qty),
        "side": "sell",
        "type": "market",
        "time_in_force": "day",
    }
    resp = requests.post(f"{ALPACA_BASE_URL}/orders", headers=HEADERS, json=order, timeout=10)
    if resp.status_code in (200, 201):
        return resp.status_code, resp.json()
    return resp.status_code, resp.text


def main():
    parser = argparse.ArgumentParser(description="Rebalance portfolio to active mode universe")
    parser.add_argument(
        "--mode",
        choices=["swing", "day"],
        default=None,
        help="Trading mode (default: resolved from trading_mode.json / env)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually place orders (default: dry run — preview only)",
    )
    args = parser.parse_args()

    mode = resolve_mode(cli_mode=args.mode)
    mode_config = get_mode_config(mode)
    mode_label = mode_config["label"]
    universe = mode_config["universe"]

    # Build set of all tickers in the active universe
    universe_tickers: set[str] = set()
    for cat_data in universe.values():
        universe_tickers.update(cat_data["tickers"])

    dry_run = not args.execute

    print(f"{'='*60}")
    print(f"  PORTFOLIO REBALANCER")
    print(f"  Mode: {mode.upper()} — {mode_label}")
    print(f"  Universe ({len(universe_tickers)} tickers): {', '.join(sorted(universe_tickers))}")
    print(f"  Action: {'EXECUTE' if args.execute else 'DRY RUN (add --execute to trade)'}")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")
    print()

    positions = get_positions()

    # Find positions outside the active universe
    out_of_universe = {
        sym: pos for sym, pos in positions.items()
        if sym not in universe_tickers
    }

    if not out_of_universe:
        print(f"✅ All positions are within the {mode.upper()} universe. Nothing to rebalance.")
        return 0

    print(f"Found {len(out_of_universe)} position(s) outside {mode.upper()} universe:")
    print()

    results = []

    for ticker, pos in sorted(out_of_universe.items()):
        total_shares = int(float(pos.get("qty", 0)))
        current_price = float(pos.get("current_price", 0))
        market_value = float(pos.get("market_value", 0))
        unrealized_pl = float(pos.get("unrealized_pl", 0))

        if total_shares <= 0:
            print(f"  ⏭️  {ticker}: Zero shares, skipping")
            continue

        pl_sign = "+" if unrealized_pl >= 0 else ""
        print(
            f"  📉 {ticker}: {total_shares} shares @ ${current_price:,.2f}"
            f" = ${market_value:,.2f} ({pl_sign}${unrealized_pl:,.2f} P&L)"
        )

        if dry_run:
            print(f"     → DRY RUN: would sell all {total_shares} shares")
            results.append({"ticker": ticker, "qty": total_shares, "success": True, "dry_run": True})
        else:
            status_code, response = place_sell_order(ticker, total_shares)
            if status_code in (200, 201):
                order_id = response.get("id", "?")
                order_status = response.get("status", "?")
                print(f"     → ✅ Order placed! ID: {order_id} | Status: {order_status}")
                results.append({"ticker": ticker, "qty": total_shares, "success": True, "order_id": order_id})
            else:
                print(f"     → ❌ Order failed: {response}")
                results.append({"ticker": ticker, "qty": total_shares, "success": False, "error": str(response)})

        print()

    print("=" * 60)
    successful = [r for r in results if r.get("success")]
    failed = [r for r in results if not r.get("success")]
    dry_runs = [r for r in results if r.get("dry_run")]

    if dry_run:
        print(f"DRY RUN: {len(dry_runs)} position(s) would be fully sold")
        print("Run with --execute to place orders.")
    else:
        print(f"✅ {len(successful)} order(s) placed, ❌ {len(failed)} failed")
        if successful:
            print(f"Capital will be freed at next market fill.")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
