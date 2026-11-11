"""Momentum paper book: quarterly rebalance of the target basket on its own paper account.

Dry run by default. ``--submit`` places orders, and only once per quarter (state is kept in
data/research/momentum-paper/state.json). Exits and trims are marketable limits: a sell at the bid less the
marketable band. Buys are passive limits at the bid, so they do not cross the spread; an unfilled buy expires
with the day and the next rebalance corrects the book. Every decision is logged with the quote it was priced
from.

Credentials: ALPACA_MOMENTUM_API_KEY, ALPACA_MOMENTUM_API_SECRET, ALPACA_MOMENTUM_BASE_URL (paper only).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from broker.endpoints import _base
from broker.order_guard import check_order
from equity_research.momentum_target import daily_closes, fill_recent_sessions, target_basket
from execution.broker import get_latest_quote
from portfolio.config import get_algo_execution_settings
from runtime.paths import ROOT

STATE_DIR = ROOT / "data" / "research" / "momentum-paper"
STATE_PATH = STATE_DIR / "state.json"
LOG_PATH = STATE_DIR / "decisions.jsonl"
MAX_BASKET_AGE_DAYS = 4


def _load_environment() -> None:
    from dotenv import load_dotenv

    load_dotenv(ROOT.parent / ".env", override=False)


def base_url() -> str:
    # Refuses any non-paper endpoint unless the live opt-in is set (same rule as the other Alpaca clients).
    return _base("ALPACA_MOMENTUM_BASE_URL")


def headers() -> dict:
    key = (os.getenv("ALPACA_MOMENTUM_API_KEY") or "").strip()
    secret = (os.getenv("ALPACA_MOMENTUM_API_SECRET") or "").strip()
    if not key or not secret:
        raise RuntimeError("ALPACA_MOMENTUM_API_KEY and ALPACA_MOMENTUM_API_SECRET must be set")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def market_is_open(base: str, auth: dict) -> bool:
    response = requests.get(f"{base}/clock", headers=auth, timeout=10)
    response.raise_for_status()
    return bool(response.json().get("is_open"))


def rebalance_due(today: date, state: dict) -> bool:
    """Once per quarter: in the first week of the quarter, or at once if this book has never rebalanced."""
    if state.get("last_quarter") == quarter_key(today):
        return False
    return not state.get("last_rebalance") or (today.month - 1) % 3 == 0 and today.day <= 7


def quarter_key(day: date) -> str:
    return f"{day.year}-Q{(day.month - 1) // 3 + 1}"


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def plan_rebalance(
    basket: list[dict], equity: float, positions: dict[str, dict], prices: dict[str, float],
    quotes: dict[str, dict],
) -> list[dict]:
    """Orders that move the book to equal weights across the basket. Sells are listed before buys."""
    band = float(get_algo_execution_settings()["marketable_limit_band_bps"]) / 10_000.0
    target = {row["symbol"]: float(row["weight"]) for row in basket}
    intents: list[dict] = []
    for symbol, position in positions.items():
        held = int(float(position["qty"]))
        if held <= 0 or symbol in target:
            continue
        bid = quotes[symbol]["bid"]
        intents.append({"symbol": symbol, "side": "sell", "qty": held, "reason": "exit: left the basket",
                        "limit_price": round(bid * (1 - band), 2), "priced_from": quotes[symbol]})
    for symbol, weight in target.items():
        price = prices[symbol]
        desired = math.floor(equity * weight / price)
        held = int(float(positions.get(symbol, {}).get("qty", 0)))
        diff = desired - held
        if diff < 0:
            intents.append({"symbol": symbol, "side": "sell", "qty": -diff, "reason": "trim to target weight",
                            "limit_price": round(quotes[symbol]["bid"] * (1 - band), 2),
                            "priced_from": quotes[symbol]})
        elif diff > 0:
            intents.append({"symbol": symbol, "side": "buy", "qty": diff, "reason": "add to target weight",
                            "limit_price": round(quotes[symbol]["bid"], 2), "priced_from": quotes[symbol]})
    return intents


def fetch_book(base: str, auth: dict) -> tuple[float, dict[str, dict]]:
    account = requests.get(f"{base}/account", headers=auth, timeout=10)
    account.raise_for_status()
    positions = requests.get(f"{base}/positions", headers=auth, timeout=10)
    positions.raise_for_status()
    equity = float(account.json()["equity"])
    return equity, {row["symbol"]: row for row in positions.json()}


def submit(base: str, auth: dict, intent: dict) -> dict:
    payload = {
        "symbol": intent["symbol"], "qty": str(intent["qty"]), "side": intent["side"], "type": "limit",
        "limit_price": str(intent["limit_price"]), "time_in_force": "day",
        "client_order_id": f"mom-{uuid.uuid4().hex}",
    }
    check_order(payload)
    response = requests.post(f"{base}/orders", headers=auth, json=payload, timeout=10)
    response.raise_for_status()
    return {"order_id": response.json().get("id"), "client_order_id": payload["client_order_id"], "payload": payload}


def log(entry: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), **entry}) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Momentum paper book rebalance")
    parser.add_argument("--submit", action="store_true", help="place orders (default: dry run)")
    parser.add_argument("--force", action="store_true", help="rebalance even if this quarter is already done")
    parser.add_argument("--scheduled", action="store_true",
                        help="run from the timer: submit only when due and the market is open, otherwise exit quietly")
    args = parser.parse_args()
    if args.scheduled:
        args.submit = True
    _load_environment()
    today = datetime.now(timezone.utc).date()
    database = ROOT / "data" / "algo_market.sqlite3"
    series = daily_closes(database)
    # Daily bars can lag the 5-minute bars; close the gap from the 5-minute bars so the basket uses today's prices.
    fill_recent_sessions(database, series, since=today - timedelta(days=10))
    basket = target_basket(series)
    newest = max(row["as_of"] for row in basket)
    if (today - date.fromisoformat(newest)).days > MAX_BASKET_AGE_DAYS:
        raise RuntimeError(f"basket prices are stale: newest bar {newest}")
    base, auth = base_url(), headers()
    quarter = quarter_key(today)
    state = load_state()
    if args.scheduled and not (args.force or rebalance_due(today, state)):
        print(f"quarter {quarter}: not due (last {state.get('last_rebalance')}); nothing to do")
        return 0
    if args.submit and not market_is_open(base, auth):
        if args.scheduled:
            print("market is closed; the rebalance waits for the next run")
            return 0
        raise RuntimeError("market is closed: orders are submitted only with fresh quotes")
    if state.get("last_quarter") == quarter and not args.force:
        print(f"quarter {quarter} already rebalanced on {state.get('last_rebalance')}; nothing to do")
        return 0
    equity, positions = fetch_book(base, auth)
    symbols = sorted({row["symbol"] for row in basket} | set(positions))
    prices = {row["symbol"]: row["close"] for row in basket}
    quotes = {}
    closes = {row["symbol"]: row["close"] for row in basket}
    for symbol in symbols:
        try:
            bid = get_latest_quote(symbol, "bid", auth)
            ask = get_latest_quote(symbol, "ask", auth)
            indicative = False
        except ValueError:
            # Quotes are stale when the market is closed. A dry run prices off the last close and says so;
            # --submit never does, so orders are only priced from fresh quotes.
            if args.submit:
                raise
            bid = ask = float(closes.get(symbol) or positions.get(symbol, {}).get("current_price") or 0)
            indicative = True
        quotes[symbol] = {"bid": bid, "ask": ask, "mid": round((bid + ask) / 2, 4),
                          "quote_at": datetime.now(timezone.utc).isoformat(), "indicative": indicative}
        prices[symbol] = quotes[symbol]["mid"]  # size on the live midpoint, not the database close
    intents = plan_rebalance(basket, equity, positions, prices, quotes)
    print(f"equity {equity:,.2f}, quarter {quarter}, {len(intents)} order(s) planned")
    for intent in intents:
        print(f"  {intent['side']:4} {intent['qty']:>6} {intent['symbol']:6} limit {intent['limit_price']:>9}  {intent['reason']}")
    if not args.submit:
        print("dry run: nothing submitted (use --submit)")
        return 0
    for intent in intents:
        result = submit(base, auth, intent)
        log({"quarter": quarter, "basket": basket, "intent": {k: v for k, v in intent.items() if k != "priced_from"},
             "priced_from": intent["priced_from"], "result": result})
    state.update({"last_quarter": quarter, "last_rebalance": today.isoformat()})
    save_state(state)
    print(f"submitted {len(intents)} order(s) for {quarter}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
