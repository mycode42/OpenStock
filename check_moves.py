#!/usr/bin/env python3
import json
import os
import urllib.request

from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("ALPACA_API_KEY", "")
API_SECRET = os.getenv("ALPACA_API_SECRET", "")

if not API_KEY or not API_SECRET:
    raise EnvironmentError("ALPACA_API_KEY and ALPACA_API_SECRET must be set in .env")

url = "https://data.alpaca.markets/v2/stocks/snapshots?symbols=NVDA,MSFT,GOOGL,AAPL,META,AVGO,TSLA,AMD"
req = urllib.request.Request(url)
req.add_header("APCA-API-KEY-ID", API_KEY)
req.add_header("APCA-API-SECRET-KEY", API_SECRET)

with urllib.request.urlopen(req) as response:
    data = json.loads(response.read().decode())

print("=" * 70)
print("MID-MORNING PULSE CHECK — 10:30 AM PT | Monday March 9, 2026")
print("=" * 70)
print(f"{'Symbol':<8} {'Prev Close':>12} {'Current':>12} {'Change':>10} {'Change%':>10}")
print("-" * 70)

moves = []
for sym in ['NVDA', 'MSFT', 'GOOGL', 'AAPL', 'META', 'AVGO', 'TSLA', 'AMD']:
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
    print("\n✓ No significant moves (>3%) detected since Friday's close")
    print("\n📭 MARKETS QUIET — Skip notification per cron instructions")
print("=" * 70)
