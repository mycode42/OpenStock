#!/usr/bin/env python3
"""
Market Pulse Check — Mid-morning moves on universe stocks.

Shows price changes for all stocks in the active trading mode universe.
Used for quick pulse checks during the trading day.

Usage:
  poetry run python check_moves.py                # Use resolved mode
  poetry run python check_moves.py --mode swing   # Force swing mode
  poetry run python check_moves.py --mode day     # Force day mode
"""

import argparse
import json
import os
import urllib.request
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

# Import mode config system
from src.config import get_mode_config, resolve_mode

API_KEY = os.getenv("ALPACA_API_KEY", "")
API_SECRET = os.getenv("ALPACA_API_SECRET", "")

if not API_KEY or not API_SECRET:
    raise EnvironmentError("ALPACA_API_KEY and ALPACA_API_SECRET must be set in .env")

def main():
    parser = argparse.ArgumentParser(description="Check market moves for universe stocks")
    parser.add_argument("--mode", choices=["swing", "day"], help="Trading mode (default: auto-resolve)")
    args = parser.parse_args()

    # Resolve mode
    mode = args.mode if args.mode else resolve_mode()
    if mode == "auto":
        mode = "swing"  # Default fallback

    # Get universe tickers
    config = get_mode_config(mode)
    all_tickers = []
    for sector in config["universe"].values():
        all_tickers.extend(sector["tickers"])
    all_tickers = sorted(set(all_tickers))  # Dedupe and sort

    # Build URL with all universe tickers
    symbols = ",".join(all_tickers)
    url = f"https://data.alpaca.markets/v2/stocks/snapshots?symbols={symbols}"

    req = urllib.request.Request(url)
    req.add_header("APCA-API-KEY-ID", API_KEY)
    req.add_header("APCA-API-SECRET-KEY", API_SECRET)

    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read().decode())

    now = datetime.now().strftime("%I:%M %p PT | %A %B %d, %Y")
    print("=" * 70)
    print(f"UNIVERSE PULSE CHECK [{mode.upper()}] — {now}")
    print("=" * 70)
    print(f"{'Symbol':<8} {'Prev Close':>12} {'Current':>12} {'Change':>10} {'Change%':>10}")
    print("-" * 70)

    moves = []
    for sym in all_tickers:
        if sym in data:
            prev = data[sym].get('prevDailyBar', {}).get('c')
            curr = data[sym].get('latestQuote', {}).get('bp')
            if prev and curr:
                change = curr - prev
                pct = (change / prev) * 100
                sign = "+" if change >= 0 else ""
                print(f"{sym:<8} {prev:>12.2f} {curr:>12.2f} {sign}{change:>9.2f} {sign}{pct:>9.2f}%")
                if abs(pct) > 3:
                    moves.append((sym, pct, curr, prev))
            else:
                print(f"{sym:<8} {'N/A':>12} {'N/A':>12}")
        else:
            print(f"{sym:<8} {'N/A':>12} {'N/A':>12}")

    print("-" * 70)
    if moves:
        print("\n⚠️  SIGNIFICANT MOVES (>3%):")
        for sym, pct, curr, prev in moves:
            direction = "UP" if pct > 0 else "DOWN"
            print(f"  {sym}: {direction} {abs(pct):.2f}% (${prev:.2f} → ${curr:.2f})")
        print(f"\n📊 MATERIAL ACTION DETECTED — Message Kenny & Zo recommended")
    else:
        print("\n✓ No significant moves (>3%) detected")
        print("\n📭 MARKETS QUIET — Skip notification per cron instructions")
    print("=" * 70)

if __name__ == "__main__":
    main()