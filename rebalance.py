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
import hashlib
import sys
import requests
from datetime import datetime
from decimal import Decimal, InvalidOperation
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

load_dotenv()

from src.config import get_mode_config, resolve_mode
from src.accounts import AlpacaAccount, verify_account_for_mode
from execute_trades import account_execution_lock, _list_open_orders, _submit_broker_order
from src.market_calendar import get_day_schedule

ET = ZoneInfo("America/New_York")

def get_positions(account: AlpacaAccount):
    resp = requests.get(f"{account.base_url}/positions", headers=account.headers, timeout=10)
    resp.raise_for_status()
    positions = resp.json()
    if not isinstance(positions, list):
        raise ValueError("Alpaca positions response was not a list")
    result = {}
    for position in positions:
        if not isinstance(position, dict) or not isinstance(position.get("symbol"), str):
            raise ValueError("Alpaca position omitted its symbol")
        symbol = position["symbol"].upper()
        if not symbol or symbol in result:
            raise ValueError("Alpaca positions response contained an empty or duplicate symbol")
        result[symbol] = position
    return result


def get_open_order_symbols(account: AlpacaAccount) -> set[str]:
    """Return symbols with any open broker order, across every result page."""
    symbols = set()
    for order in _list_open_orders(account.headers):
        if not isinstance(order, dict) or not isinstance(order.get("symbol"), str):
            raise ValueError("Open Alpaca order omitted its symbol")
        symbol = order["symbol"].upper()
        if not symbol:
            raise ValueError("Open Alpaca order had an empty symbol")
        symbols.add(symbol)
        legs = order.get("legs") or []
        if not isinstance(legs, list):
            raise ValueError("Open Alpaca order had malformed legs")
        for leg in legs:
            if not isinstance(leg, dict):
                raise ValueError("Open Alpaca order had a malformed leg")
            if leg.get("symbol"):
                symbols.add(str(leg["symbol"]).upper())
    return symbols


def _long_quantity(position: dict) -> Decimal:
    try:
        quantity = Decimal(str(position.get("qty", "0")))
    except (InvalidOperation, TypeError) as exc:
        raise ValueError(f"Invalid quantity for {position.get('symbol')}") from exc
    if not quantity.is_finite():
        raise ValueError(f"Nonfinite quantity for {position.get('symbol')}")
    return quantity


def _client_order_id(mode: str, account_id: str, ticker: str, qty: Decimal) -> str:
    """Reuse the same broker ID for a repeated same-day rebalance intent."""
    session_day = datetime.now(ET).date().isoformat()
    intent = f"{mode}:{account_id}:{session_day}:{ticker}:{qty}"
    digest = hashlib.sha256(intent.encode("utf-8")).hexdigest()[:36]
    return f"rebalance-{digest}"


def place_sell_order(mode: str, ticker: str, expected_qty: Decimal) -> dict:
    """Recheck the account and exact holding under the account write lock."""
    try:
        with account_execution_lock(mode):
            if mode == "day":
                schedule = get_day_schedule(flatten_by="15:45", mode="day")
                if schedule is None or not schedule.market_open(datetime.now(ET)):
                    return {"success": False, "error": "Day rebalance requires regular market hours"}
            account, _ = verify_account_for_mode(mode)
            position = get_positions(account).get(ticker)
            if position is None:
                return {"success": False, "error": "Position is no longer held; preview again"}
            live_qty = _long_quantity(position)
            if live_qty <= 0 or live_qty != expected_qty:
                return {"success": False, "error": "Position quantity changed; preview again"}
            if ticker in get_open_order_symbols(account):
                return {"success": False, "error": "Open order exists for this symbol; resolve it before rebalancing"}
            client_order_id = _client_order_id(mode, account.account_id, ticker, live_qty)
            order = {
                "symbol": ticker,
                "qty": str(live_qty),
                "side": "sell",
                "type": "market",
                "time_in_force": "day",
                "client_order_id": client_order_id,
            }
            return _submit_broker_order(order, "market", mode=mode)
    except (requests.RequestException, ValueError, RuntimeError, TimeoutError, TypeError) as exc:
        return {"success": False, "error": f"Rebalance preflight unavailable: {exc}"}


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
    if mode == "auto":
        mode = "swing"
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

    try:
        account, _ = verify_account_for_mode(mode)
        positions = get_positions(account)
    except (requests.RequestException, ValueError, RuntimeError) as exc:
        print(f"❌ Cannot verify {mode.upper()} account for rebalance: {exc}", file=sys.stderr)
        return 1

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
        raw_qty = str(pos.get("qty", "0"))
        try:
            total_shares = _long_quantity(pos)
        except ValueError as exc:
            print(f"  ❌ {ticker}: {exc}")
            results.append({"ticker": ticker, "qty": raw_qty, "success": False, "error": str(exc)})
            continue
        current_price = float(pos.get("current_price", 0))
        market_value = float(pos.get("market_value", 0))
        unrealized_pl = float(pos.get("unrealized_pl", 0))

        if total_shares <= 0:
            print(f"  ⏭️  {ticker}: No long shares to sell, skipping")
            continue

        pl_sign = "+" if unrealized_pl >= 0 else ""
        print(
            f"  📉 {ticker}: {raw_qty} shares @ ${current_price:,.2f}"
            f" = ${market_value:,.2f} ({pl_sign}${unrealized_pl:,.2f} P&L)"
        )

        if dry_run:
            print(f"     → DRY RUN: would sell all {raw_qty} shares")
            results.append({"ticker": ticker, "qty": raw_qty, "success": True, "dry_run": True})
        else:
            response = place_sell_order(mode, ticker, total_shares)
            if response.get("success"):
                order_id = response.get("order_id", "?")
                order_status = response.get("broker_status", "?")
                print(f"     → ✅ Order placed! ID: {order_id} | Status: {order_status}")
                results.append({"ticker": ticker, "qty": raw_qty, "success": True, "order_id": order_id})
            else:
                error = response.get("error", "Unknown broker error")
                print(f"     → ❌ Order failed: {error}")
                results.append({"ticker": ticker, "qty": raw_qty, "success": False, "error": str(error)})

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

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
