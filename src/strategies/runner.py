"""Alpaca scanner shared by one-shot reports and the continuous portfolio.

Examples:
  poetry run python -m src.strategies.runner --mode swing
  poetry run python -m src.strategies.runner --mode day --execute --dry-run
  poetry run python -m src.strategies.runner --mode swing --execute
The long-lived strategy portfolio is hosted by src.algo.daemon --mode algo.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

from src.accounts import get_account_for_mode
from src.config import STRATEGY_CONFIG, get_mode_config
from src.strategies import pairs
from src.strategies.regime import rank_strategies
from src.strategies.regime_advisor import apply_regime_advisory
from src.strategies.registry import evaluate as evaluate_strategy
from src.strategies.state_machine import RuleStrategyStateMachine
from src.strategies.universe import select_universe


LOG = logging.getLogger("strategy_runner")
DATA_BASE = "https://data.alpaca.markets/v2"
TRADE_BASE = "https://paper-api.alpaca.markets/v2"
ET = ZoneInfo("America/New_York")
PAIR_DATA_SYMBOLS = sorted({symbol for pair in pairs.DEFAULT_PAIRS for symbol in pair})


def _request(endpoint: str, *, mode: str, base: str = DATA_BASE, params: dict | None = None):
    response = requests.get(
        f"{base}/{endpoint}",
        headers=get_account_for_mode(mode).headers,
        params=params,
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def _fetch_bars(
    symbols: list[str], *, mode: str, timeframe: str, start: str, end: str,
    limit: int, feed: str = "iex",
) -> dict[str, list[dict]]:
    if not symbols:
        return {}
    output: dict[str, list[dict]] = {}
    for offset in range(0, len(symbols), 50):
        chunk = symbols[offset:offset + 50]
        payload = _request(
            "stocks/bars",
            mode=mode,
            params={
                "symbols": ",".join(chunk),
                "timeframe": timeframe,
                "start": start,
                "end": end,
                "limit": limit,
                "sort": "asc",
                "feed": feed,
            },
        )
        for ticker, bars in (payload.get("bars") or {}).items():
            if isinstance(bars, list):
                output[ticker] = bars
    return output


def _fetch_latest_quotes(
    symbols: list[str], *, mode: str, feed: str = "iex",
) -> dict[str, dict]:
    if not symbols:
        return {}
    output: dict[str, dict] = {}
    if mode in {"swing", "day"} and feed == "iex":
        # Preserve the existing Swing/Day IEX last-trade marks. Algo uses
        # quote-first marks so its extended-hours limit prices use the BBO.
        for offset in range(0, len(symbols), 50):
            chunk = symbols[offset:offset + 50]
            payload = _request(
                "stocks/trades/latest",
                mode=mode,
                params={"symbols": ",".join(chunk), "feed": feed},
            )
            for ticker, trade in (payload.get("trades") or {}).items():
                try:
                    price = float(trade["p"])
                    timestamp = datetime.fromisoformat(trade["t"].replace("Z", "+00:00"))
                except (KeyError, TypeError, ValueError):
                    continue
                if price > 0:
                    output[ticker] = {"price": price, "timestamp": timestamp}
    for offset in range(0, len(symbols), 50):
        chunk = symbols[offset:offset + 50]
        try:
            payload = _request(
                "stocks/quotes/latest",
                mode=mode,
                params={"symbols": ",".join(chunk), "feed": feed},
            )
        except requests.RequestException:
            continue
        for ticker, quote in (payload.get("quotes") or {}).items():
            try:
                bid = float(quote.get("bp", 0) or 0)
                ask = float(quote.get("ap", 0) or 0)
                timestamp = datetime.fromisoformat(quote["t"].replace("Z", "+00:00"))
            except (KeyError, TypeError, ValueError):
                continue
            if bid <= 0 or ask < bid:
                continue
            previous = output.get(ticker)
            if previous is None or timestamp > previous["timestamp"]:
                output[ticker] = {
                    "price": (bid + ask) / 2,
                    "bid": bid,
                    "ask": ask,
                    "timestamp": timestamp,
                }
    return output


def _completed_daily(bars: list[dict], today: str) -> list[dict]:
    return [bar for bar in bars if str(bar.get("t", ""))[:10] < today]


def _completed_intraday(bars: list[dict], *, now: datetime, minutes: int = 5) -> list[dict]:
    """Drop the currently forming bar so signals only use closed candles."""
    now_utc = now.astimezone(timezone.utc)
    completed = []
    for bar in bars:
        try:
            timestamp = datetime.fromisoformat(str(bar["t"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        if timestamp + timedelta(minutes=minutes) <= now_utc:
            completed.append(bar)
    return completed


def _current_portfolio(mode: str) -> tuple[dict, dict[str, dict], list[dict]]:
    account = _request("account", mode=mode, base=TRADE_BASE)
    positions_raw = _request("positions", mode=mode, base=TRADE_BASE)
    open_orders = _request("orders", mode=mode, base=TRADE_BASE, params={"status": "open", "limit": 500})
    return account, {position["symbol"]: position for position in positions_raw}, open_orders


def _make_trade(signal: dict, price: float, equity: float, position: dict, mode_risk: dict) -> dict | None:
    action = signal["action"]
    if action == "sell":
        quantity = int(abs(float(position.get("qty", 0))))
        if quantity <= 0:
            return None
        return {
            "ticker": signal["ticker"],
            "action": action,
            "qty": quantity,
            "entry_price": price,
            "reasoning": signal["reason"],
            "strategy": signal["strategy"],
        }

    stop_pct = float(signal.get("stop_pct") or mode_risk["stop_loss_pct"])
    if equity <= 0 or price <= 0 or stop_pct <= 0:
        return None
    risk_dollars = equity * float(STRATEGY_CONFIG["risk_per_trade_pct"])
    max_position_dollars = equity * min(
        float(STRATEGY_CONFIG["max_strategy_position_pct"]),
        float(mode_risk["max_position_pct"]),
    )
    risk_quantity = int(risk_dollars / (price * stop_pct))
    position_quantity = int(max_position_dollars / price)
    quantity = min(risk_quantity, position_quantity)
    if quantity <= 0:
        return None
    stop_price = round(price * (1 - stop_pct), 2)
    target_price = round(price + (price - stop_price) * float(STRATEGY_CONFIG["take_profit_r_multiple"]), 2)
    return {
        "ticker": signal["ticker"],
        "action": action,
        "qty": quantity,
        "entry_price": price,
        "stop_price": stop_price,
        "take_profit": target_price,
        "reasoning": signal["reason"],
        "strategy": signal["strategy"],
    }


def execute_trades(trades: list[dict], *, mode: str, dry_run: bool = False) -> dict:
    repo_root = Path(__file__).resolve().parents[2]
    command = [sys.executable, str(repo_root / "execute_trades.py"), "--mode", mode]
    if dry_run:
        command.append("--dry-run")
    completed = subprocess.run(
        command,
        input=json.dumps({"trades": trades}),
        capture_output=True,
        text=True,
        cwd=repo_root,
        timeout=90,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"execute_trades.py exited {completed.returncode}: {completed.stderr[-1000:]}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("execute_trades.py returned invalid JSON") from exc


def scan(
    mode: str = "swing",
    *,
    max_tickers: int | None = None,
    universe_result: dict | None = None,
    excluded_symbols: set[str] | None = None,
    position_owners: dict[str, str] | None = None,
    strategy_states: dict | None = None,
    session_name: str | None = None,
    trading_date: str | None = None,
    extended_hours: bool = False,
    data_feed: str = "iex",
    overnight_tradable_tickers: set[str] | None = None,
) -> dict:
    if mode not in {"swing", "day", "algo"}:
        raise ValueError("Strategy scan mode must be swing, day, or algo")
    mode_config = get_mode_config(mode)
    now = datetime.now(ET)
    today = trading_date or now.date().isoformat()
    target = max_tickers or int(STRATEGY_CONFIG["universe_target"])
    universe = universe_result or select_universe(mode, max_tickers=target)
    selected_symbols = universe["tickers"]
    excluded = {str(symbol).upper() for symbol in (excluded_symbols or set())}
    track_owners = position_owners is not None
    owner_map = position_owners if position_owners is not None else {}
    held_symbol_set = {str(symbol).upper() for symbol in owner_map} if track_owners else set()
    if strategy_states is not None:
        for by_symbol in strategy_states.values():
            for symbol, state in by_symbol.items():
                if state.get("phase") in {"entry_pending", "open", "exit_pending"}:
                    held_symbol_set.add(str(symbol).upper())
    held_symbols = sorted(held_symbol_set - excluded)
    strategy_symbols = list(dict.fromkeys(
        [symbol for symbol in selected_symbols if symbol not in excluded] + held_symbols
    ))
    symbols_for_bars = list(dict.fromkeys(strategy_symbols + PAIR_DATA_SYMBOLS + ["SPY"]))

    now_utc = datetime.now(timezone.utc)
    daily_start = (now_utc - timedelta(days=220)).date().isoformat()
    daily_bars = _fetch_bars(
        symbols_for_bars,
        mode=mode,
        timeframe="1Day",
        start=daily_start,
        end=now_utc.isoformat(),
        limit=10000,
    )
    daily_bars = {ticker: _completed_daily(bars, today) for ticker, bars in daily_bars.items()}

    if mode in {"day", "algo"}:
        intraday_bars = {}
        if mode == "algo" and session_name:
            if session_name == "overnight":
                bar_day = now.date() if now.hour >= 20 else now.date() - timedelta(days=1)
                start_at = datetime.combine(bar_day, datetime.min.time(), tzinfo=ET).replace(hour=20)
            elif session_name == "premarket":
                start_at = datetime.fromisoformat(f"{today}T04:00:00").replace(tzinfo=ET)
            else:
                start_at = datetime.fromisoformat(f"{today}T09:30:00").replace(tzinfo=ET)
            if now >= start_at:
                intraday_bars = _fetch_bars(
                    strategy_symbols,
                    mode=mode,
                    timeframe="5Min",
                    start=start_at.isoformat(),
                    end=now.isoformat(),
                    limit=10000,
                    feed=data_feed,
                )
        elif now >= now.replace(hour=9, minute=30, second=0, microsecond=0):
            session_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
            intraday_bars = _fetch_bars(
                strategy_symbols,
                mode=mode,
                timeframe="5Min",
                start=session_open.isoformat(),
                end=now.isoformat(),
                limit=10000,
            )
        bars_by_symbol = {
            ticker: _completed_intraday(bars, now=now)
            for ticker, bars in intraday_bars.items()
        }
    else:
        bars_by_symbol = daily_bars

    regime = rank_strategies(daily_bars.get("SPY", []), mode=mode)
    if mode == "algo":
        regime = apply_regime_advisory(regime)
    account, positions, open_orders = _current_portfolio(mode)
    pending_by_symbol: dict[str, list[dict]] = {}
    for order in open_orders:
        ticker = str(order.get("symbol", "")).upper()
        if ticker:
            pending_by_symbol.setdefault(ticker, []).append(order)
    lifecycle = None
    if strategy_states is not None:
        lifecycle = RuleStrategyStateMachine(strategy_states, owner_map)
        lifecycle.reconcile(positions, open_orders)
    equity = float(account.get("equity", 0) or 0)
    latest_quotes = _fetch_latest_quotes(strategy_symbols, mode=mode, feed=data_feed)
    freshness_limit = int(STRATEGY_CONFIG["quote_stale_seconds"])
    rank_score = {
        entry["strategy"]: entry.get("selection_score", entry["score"])
        for entry in regime.get("ranked", [])
    }
    generated = []

    for ticker in strategy_symbols:
        position = positions.get(ticker, {})
        position_qty = float(position.get("qty", 0) or 0)
        symbol_bars = bars_by_symbol.get(ticker, [])
        options = []
        owner = owner_map.get(ticker)
        if track_owners and position_qty > 0 and owner is None:
            # On the continuous shared account, don't let a newly active
            # strategy take over a holding whose entry strategy is unknown.
            continue
        candidates = [owner] if owner else regime.get("active", [])
        protective_state = (strategy_states or {}).get(owner, {}).get(ticker, {}) if owner else {}
        quote = latest_quotes.get(ticker)
        if owner and position_qty > 0 and quote:
            quote_age = (datetime.now(timezone.utc) - quote["timestamp"]).total_seconds()
            current_price = float(quote["price"])
            stop_price = protective_state.get("stop_price")
            target_price = protective_state.get("take_profit")
            protective_reason = None
            if 0 <= quote_age <= int(STRATEGY_CONFIG["quote_stale_seconds"]):
                if stop_price is not None and current_price <= float(stop_price):
                    protective_reason = "software stop-loss reached"
                elif target_price is not None and current_price >= float(target_price):
                    protective_reason = "software take-profit reached"
            if protective_reason:
                signal_bar_time = symbol_bars[-1].get("t") if symbol_bars else None
                if lifecycle is None or lifecycle.allows(owner, ticker, "sell", signal_bar_time):
                    options.append({
                        "ticker": ticker,
                        "action": "sell",
                        "strategy": owner,
                        "reason": protective_reason,
                        "strength": 1.0,
                        "rank_score": rank_score.get(owner, 0),
                        "signal_bar_time": signal_bar_time,
                        "software_protection": True,
                    })
        for strategy_name in candidates:
            if (
                strategy_name == "opening_range_breakout"
                and session_name is not None
                and session_name != "regular"
                and owner != strategy_name
            ):
                continue
            signal_bar_time = symbol_bars[-1].get("t") if symbol_bars else None
            if lifecycle:
                lifecycle.observe(strategy_name, ticker, signal_bar_time)
            signal = evaluate_strategy(strategy_name, mode, ticker, symbol_bars, position_qty)
            if signal is None or (owner and signal.action != "sell"):
                continue
            item = signal.to_dict()
            item["rank_score"] = rank_score.get(strategy_name, 0)
            item["signal_bar_time"] = signal_bar_time
            if lifecycle and not lifecycle.allows(
                strategy_name, ticker, signal.action, item["signal_bar_time"],
            ):
                continue
            options.append(item)
        if not options:
            continue
        # One action per ticker per scan prevents conflicting strategy orders.
        chosen = max(options, key=lambda item: (item["rank_score"], item["strength"]))
        ticker_orders = pending_by_symbol.get(ticker, [])
        pending_entry = any(str(order.get("side", "")).lower() == "buy" for order in ticker_orders)
        pending_exit = any(
            str(order.get("side", "")).lower() == "sell"
            and str(order.get("type", "market")).lower() not in {"stop", "stop_limit", "limit", "trailing_stop"}
            for order in ticker_orders
        )
        if (chosen["action"] == "buy" and ticker_orders) or pending_entry or pending_exit:
            chosen["suppressed_reason"] = "a conflicting Alpaca order is already open for this symbol"
            generated.append(chosen)
            continue
        if regime.get("regime") == "trending_down" and chosen["action"] == "buy":
            chosen["suppressed_reason"] = "long entries are disabled in the downtrend regime"
            generated.append(chosen)
            continue
        generated.append(chosen)

    trades = []
    now_utc = datetime.now(timezone.utc)
    for item in generated:
        if item.get("suppressed_reason"):
            continue
        quote = latest_quotes.get(item["ticker"])
        if quote is None:
            item["suppressed_reason"] = "latest Alpaca quote unavailable"
            continue
        price = float(quote["price"])
        quote_time = quote["timestamp"]
        age = (now_utc - quote_time).total_seconds()
        if age < 0 or age > freshness_limit:
            item["suppressed_reason"] = f"latest Alpaca quote is stale ({age:.0f}s)"
            continue
        if extended_hours:
            spread_pct = (float(quote["ask"]) - float(quote["bid"])) / price if price else 1.0
            if item["action"] == "buy" and spread_pct > float(STRATEGY_CONFIG["extended_hours_max_spread_pct"]):
                item["suppressed_reason"] = f"extended-hours spread is too wide ({spread_pct:.1%})"
                continue
            if session_name == "overnight" and item["ticker"] not in (overnight_tradable_tickers or set()):
                item["suppressed_reason"] = "Alpaca does not mark this asset overnight_tradable"
                continue
        execution_price = float(quote["ask"] if item["action"] == "buy" else quote["bid"]) if extended_hours else price
        trade = _make_trade(item, execution_price, equity, positions.get(item["ticker"], {}), mode_config["risk"])
        if trade:
            if extended_hours:
                trade.update({
                    "order_type": "limit",
                    "limit_price": execution_price,
                    "extended_hours": True,
                })
            identity = f"{mode}:{today}:{item['strategy']}:{item['ticker']}:{item['action']}:{item.get('signal_bar_time') or ''}"
            trade["client_order_id"] = f"rule-{today.replace('-', '')}-{hashlib.sha256(identity.encode()).hexdigest()[:12]}"
            trade["signal_bar_time"] = item.get("signal_bar_time")
            item_orders = pending_by_symbol.get(item["ticker"], [])
            protective_exit = any(
                str(order.get("side", "")).lower() == "sell"
                and (
                    str(order.get("type", "")).lower() in {"stop", "stop_limit", "limit", "trailing_stop"}
                    or order.get("parent_order_id")
                    or str(order.get("order_class", "")).lower() in {"bracket", "oco"}
                )
                for order in item_orders
            )
            if trade["action"] == "sell" and protective_exit:
                trade["cancel_open_orders"] = True
            trades.append(trade)
            item["entry_price"] = price
            item["qty"] = trade["qty"]
        else:
            item["suppressed_reason"] = "position or strategy sizing produced zero shares"

    pair_setups = pairs.candidates(
        daily_bars,
        minimum_correlation=float(STRATEGY_CONFIG["pairs_min_correlation"]),
        entry_z=float(STRATEGY_CONFIG["pairs_entry_z"]),
    )
    pair_output = []
    for pair_setup in pair_setups:
        pair_setup_data = pair_setup.to_dict()
        pair_setup_data["execution_enabled"] = False
        pair_setup_data["execution_note"] = "signal only: paired orders are not atomic in the current executor"
        pair_output.append(pair_setup_data)

    return {
        "timestamp": now.isoformat(),
        "trading_mode": mode,
        "universe": universe,
        "market_regime": regime,
        "signals": generated,
        "pairs": pair_output,
        "trades": trades,
        "pair_execution_enabled": False,
        "portfolio_positions": sorted(positions),
        "position_details": positions,
        "pending_symbols": sorted(pending_by_symbol),
        "session": session_name,
        "extended_hours": extended_hours,
        "open_orders": open_orders,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan the dynamic universe with pure rule-based strategies")
    parser.add_argument("--mode", choices=["swing", "day", "algo"], default="swing")
    parser.add_argument("--max-tickers", type=int, default=None)
    parser.add_argument("--execute", action="store_true", help="Submit generated trades through execute_trades.py")
    parser.add_argument("--dry-run", action="store_true", help="Validate generated trades without sending orders")
    args = parser.parse_args()
    if args.dry_run and not args.execute:
        parser.error("--dry-run requires --execute")

    result = scan(args.mode, max_tickers=args.max_tickers)
    if args.execute and result["trades"]:
        result["execution"] = execute_trades(result["trades"], mode=args.mode, dry_run=args.dry_run)
    elif args.execute:
        result["execution"] = {"status": "no_trades", "results": []}
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [strategy_runner] %(levelname)s %(message)s")
    sys.exit(main())
