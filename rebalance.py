#!/usr/bin/env python3
"""
Mordecai Portfolio Rebalance — Sell non-universe positions

Sells ASTS, COP, MRVL, RTX, XLE down to 10% residual positions.
Orders queue for Monday market open (placed Saturday).

Safety: Paper trading only, keeps 10% of each position.
"""

import os
import requests
import json
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_API_SECRET = os.getenv("ALPACA_API_SECRET", "")
ALPACA_BASE_URL = "https://paper-api.alpaca.markets/v2"

if not ALPACA_API_KEY or not ALPACA_API_SECRET:
    raise EnvironmentError(
        "ALPACA_API_KEY and ALPACA_API_SECRET must be set in .env"
    )

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_API_SECRET,
    "Content-Type": "application/json",
}

# Tickers to sell (outside Mordecai universe)
SELL_TICKERS = ["ASTS", "COP", "MRVL", "RTX", "XLE"]
MIN_KEEP_PCT = 0.10  # Keep at least 10%


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
    return resp.status_code, resp.json() if resp.status_code in (200, 201) else resp.text


def main():
    print(f"🔄 Mordecai Portfolio Rebalance — {datetime.now().strftime('%Y-%m-%d %H:%M PST')}")
    print(f"📋 Selling: {', '.join(SELL_TICKERS)} (keeping {MIN_KEEP_PCT*100:.0f}% of each)")
    print()

    positions = get_positions()
    results = []

    for ticker in SELL_TICKERS:
        pos = positions.get(ticker)
        if not pos:
            print(f"⏭️  {ticker}: No position found, skipping")
            continue

        total_shares = int(float(pos.get("qty", 0)))
        current_price = float(pos.get("current_price", 0))
        market_value = float(pos.get("market_value", 0))
        unrealized_pl = float(pos.get("unrealized_pl", 0))

        if total_shares <= 0:
            print(f"⏭️  {ticker}: Zero shares, skipping")
            continue

        # Keep at least 10% (minimum 1 share)
        min_keep = max(1, int(total_shares * MIN_KEEP_PCT))
        sell_qty = total_shares - min_keep

        if sell_qty <= 0:
            print(f"⏭️  {ticker}: Only {total_shares} share(s), can't sell and keep 10%")
            continue

        sell_value = sell_qty * current_price
        print(f"📉 {ticker}: Selling {sell_qty} of {total_shares} shares (keeping {min_keep})")
        print(f"   Current price: ${current_price:,.2f} | Sell value: ~${sell_value:,.2f} | P&L: ${unrealized_pl:,.2f}")

        status_code, response = place_sell_order(ticker, sell_qty)

        if status_code in (200, 201):
            order_id = response.get("id", "?")
            order_status = response.get("status", "?")
            print(f"   ✅ Order placed! ID: {order_id} | Status: {order_status}")
            results.append({"ticker": ticker, "qty": sell_qty, "success": True, "order_id": order_id})
        else:
            print(f"   ❌ Order failed! {response}")
            results.append({"ticker": ticker, "qty": sell_qty, "success": False, "error": str(response)})

        print()

    # Summary
    successful = [r for r in results if r["success"]]
    failed = [r for r in results if not r["success"]]

    print("=" * 60)
    print(f"✅ {len(successful)} orders placed, ❌ {len(failed)} failed")
    if successful:
        total_freed = sum(r["qty"] for r in successful)
        print(f"📊 Total shares being sold: {total_freed}")
        print(f"💰 Capital will be freed at Monday market open")
    print("=" * 60)

    return results


if __name__ == "__main__":
    main()
