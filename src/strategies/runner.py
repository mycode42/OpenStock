"""Alpaca scanner shared by Swing/Day reports and the continuous Algo portfolio.

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
import math
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
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
from src.market_data import feeds_for_session, historical_delay_seconds
from src.strategies.registry import evaluate as evaluate_strategy
from src.strategies.state_machine import RuleStrategyStateMachine
from src.strategies.universe import select_universe


LOG = logging.getLogger("strategy_runner")
DATA_BASE = "https://data.alpaca.markets/v2"
TRADE_BASE = "https://paper-api.alpaca.markets/v2"
ET = ZoneInfo("America/New_York")
PAIR_DATA_SYMBOLS = sorted({symbol for pair in pairs.DEFAULT_PAIRS for symbol in pair})
# Free-plan BOATS historical data is available only when the requested end is
# at least 15 minutes old. Keep a small margin for clock differences.
MAX_SPY_DAILY_AGE_DAYS = 5


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
        page_token = None
        seen_tokens: set[str] = set()
        while True:
            params = {
                "symbols": ",".join(chunk),
                "timeframe": timeframe,
                "start": start,
                "end": end,
                "limit": limit,
                "sort": "asc",
                "feed": feed,
            }
            if page_token:
                params["page_token"] = page_token
            payload = _request("stocks/bars", mode=mode, params=params)
            if not isinstance(payload, dict):
                raise ValueError("Alpaca historical bars response is invalid")
            bars_payload = payload.get("bars") or {}
            if not isinstance(bars_payload, dict):
                raise ValueError("Alpaca historical bars response is invalid")
            for ticker, bars in bars_payload.items():
                if isinstance(bars, list):
                    output.setdefault(ticker, []).extend(bars)
            page_token = payload.get("next_page_token")
            if not page_token:
                break
            if not isinstance(page_token, str) or page_token in seen_tokens:
                raise ValueError("Alpaca historical bars pagination token repeated or invalid")
            seen_tokens.add(page_token)
    return output


def _fetch_latest_quotes(
    symbols: list[str], *, mode: str, feed: str = "iex", errors: list[str] | None = None,
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
                    timestamp = datetime.fromisoformat(str(trade["t"]).replace("Z", "+00:00"))
                except (KeyError, TypeError, ValueError):
                    continue
                if math.isfinite(price) and price > 0:
                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(tzinfo=timezone.utc)
                    output[ticker] = {"price": price, "timestamp": timestamp}
    for offset in range(0, len(symbols), 50):
        chunk = symbols[offset:offset + 50]
        try:
            payload = _request(
                "stocks/quotes/latest",
                mode=mode,
                params={"symbols": ",".join(chunk), "feed": feed},
            )
            if not isinstance(payload, dict):
                raise ValueError("Alpaca latest quotes response is invalid")
            quotes = payload.get("quotes") or {}
            if not isinstance(quotes, dict):
                raise ValueError("Alpaca latest quotes response is invalid")
        except (requests.RequestException, ValueError) as exc:
            if errors is not None:
                errors.append(f"latest quotes {feed}: {type(exc).__name__}: {exc}")
            continue
        for ticker, quote in quotes.items():
            if not isinstance(quote, dict):
                continue
            try:
                bid = float(quote.get("bp", 0) or 0)
                ask = float(quote.get("ap", 0) or 0)
                timestamp = datetime.fromisoformat(str(quote["t"]).replace("Z", "+00:00"))
            except (KeyError, TypeError, ValueError):
                continue
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            if not all(math.isfinite(value) for value in (bid, ask)) or bid <= 0 or ask < bid:
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


def _latest_bar_age_seconds(bars: list[dict], now: datetime) -> float | None:
    if not bars:
        return None
    try:
        timestamp = datetime.fromisoformat(str(bars[-1]["t"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return (now.astimezone(timezone.utc) - timestamp).total_seconds()


def _current_portfolio(mode: str) -> tuple[dict, dict[str, dict], list[dict]]:
    account = _request("account", mode=mode, base=TRADE_BASE)
    positions_raw = _request("positions", mode=mode, base=TRADE_BASE)
    open_orders = _request("orders", mode=mode, base=TRADE_BASE, params={"status": "open", "limit": 500})
    return account, {position["symbol"]: position for position in positions_raw}, open_orders


def _is_protective_sell_order(order: dict) -> bool:
    """Identify broker protection legs without treating standalone limits as brackets."""
    if str(order.get("side", "")).lower() != "sell":
        return False
    kind = str(order.get("type", "")).lower()
    order_class = str(order.get("order_class", "")).lower()
    return bool(
        kind in {"stop", "stop_limit", "trailing_stop"}
        or order.get("parent_order_id")
        or order_class in {"bracket", "oco"}
    )


def _positive_price_level(value: object) -> float | None:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if math.isfinite(price) and price > 0 else None


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
    data_feed: str | None = None,
    quote_feed: str | None = None,
    overnight_tradable_tickers: set[str] | None = None,
) -> dict:
    if mode not in {"swing", "day", "algo"}:
        raise ValueError("Strategy scan mode must be swing, day, or algo")
    if mode == "algo" and (position_owners is None or strategy_states is None):
        raise ValueError("Algo scans require the coordinator's position owners and strategy states")
    if mode == "algo" and session_name:
        default_bar_feed, default_quote_feed = feeds_for_session(session_name)
        # Overnight historical bars only come from BOATS. Quote feed varies by
        # plan: Basic's indicative overnight feed versus Plus BOATS quotes.
        data_feed = default_bar_feed if session_name == "overnight" else (data_feed or default_bar_feed)
        quote_feed = quote_feed or default_quote_feed
    else:
        data_feed = data_feed or "iex"
        quote_feed = quote_feed or data_feed
    mode_config = get_mode_config(mode)
    now = datetime.now(ET)
    today = trading_date or now.date().isoformat()
    target = max_tickers or int(STRATEGY_CONFIG["universe_target"])
    universe = universe_result if universe_result is not None else select_universe(mode, max_tickers=target)
    selected_symbols = [str(symbol).upper() for symbol in universe.get("tickers", [])]
    excluded = {str(symbol).upper() for symbol in (excluded_symbols or set())}
    track_owners = position_owners is not None
    owner_map = position_owners if position_owners is not None else {}
    scan_errors: list[str] = []
    entry_pause_reasons: list[str] = []
    if mode == "algo" and not selected_symbols:
        entry_pause_reasons.append("the Algo universe is empty")
    if mode == "algo" and session_name == "overnight" and not bool(
        STRATEGY_CONFIG.get("overnight_entries_enabled", True)
    ):
        entry_pause_reasons.append("overnight entries are disabled by configuration")

    # Broker holdings are fetched and reconciled before choosing scan symbols.
    # This lets a recovered owner receive quote-based protection in this scan.
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
    unowned_positions = sorted(
        ticker for ticker, position in positions.items()
        if float(position.get("qty", 0) or 0) > 0 and ticker not in owner_map
    ) if track_owners else []
    if mode == "algo" and unowned_positions:
        entry_pause_reasons.append("broker positions have no verified strategy owner")

    held_symbol_set = {str(symbol).upper() for symbol in owner_map} if track_owners else set()
    if strategy_states is not None:
        for by_symbol in strategy_states.values():
            for symbol, state in by_symbol.items():
                if state.get("phase") in {"entry_pending", "open", "exit_pending"}:
                    held_symbol_set.add(str(symbol).upper())
    held_symbols = sorted(held_symbol_set if mode == "algo" else held_symbol_set - excluded)
    strategy_symbols = list(dict.fromkeys(
        [symbol for symbol in selected_symbols if symbol not in excluded] + held_symbols
    ))
    symbols_for_bars = list(dict.fromkeys(strategy_symbols + PAIR_DATA_SYMBOLS + ["SPY"]))

    now_utc = datetime.now(timezone.utc)
    daily_start = (now_utc - timedelta(days=220)).date().isoformat()
    historical_status = "ok"
    historical_bar_end: str | None = None
    try:
        daily_bars = _fetch_bars(
            symbols_for_bars,
            mode=mode,
            timeframe="1Day",
            start=daily_start,
            end=now_utc.isoformat(),
            limit=10000,
        )
        daily_bars = {ticker: _completed_daily(bars, today) for ticker, bars in daily_bars.items()}
    except Exception as exc:
        if mode != "algo":
            raise
        daily_bars = {}
        historical_status = "degraded"
        scan_errors.append(f"daily historical bars: {type(exc).__name__}: {exc}")
        entry_pause_reasons.append("daily historical bars are unavailable")

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
            delay_seconds = historical_delay_seconds(data_feed, session_name)
            bar_end = now - timedelta(seconds=delay_seconds) if delay_seconds else now
            historical_bar_end = bar_end.isoformat()
            if bar_end > start_at:
                try:
                    intraday_bars = _fetch_bars(
                        strategy_symbols,
                        mode=mode,
                        timeframe="5Min",
                        start=start_at.isoformat(),
                        end=historical_bar_end,
                        limit=10000,
                        feed=data_feed,
                    )
                except Exception as exc:
                    historical_status = "degraded"
                    scan_errors.append(f"intraday historical bars ({data_feed}): {type(exc).__name__}: {exc}")
                    entry_pause_reasons.append("intraday historical bars are unavailable")
            elif historical_status == "ok":
                historical_status = "warming_up"
                entry_pause_reasons.append("waiting for completed intraday historical bars")
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
        try:
            bars_by_symbol = {
                ticker: _completed_intraday(bars, now=now)
                for ticker, bars in intraday_bars.items()
            }
        except Exception as exc:
            if mode != "algo":
                raise
            bars_by_symbol = {}
            historical_status = "degraded"
            scan_errors.append(f"intraday bar validation: {type(exc).__name__}: {exc}")
            entry_pause_reasons.append("intraday historical bars are invalid")
        if mode == "algo" and strategy_symbols and not any(bars_by_symbol.values()) and historical_status == "ok":
            historical_status = "warming_up"
            entry_pause_reasons.append("no completed intraday historical bars are available yet")
    else:
        bars_by_symbol = daily_bars

    delay_seconds = historical_delay_seconds(data_feed, session_name or "") if mode == "algo" else 0
    signal_bar_max_age_seconds = (35 if delay_seconds else 15) * 60
    fresh_signal_bar_symbols = set()
    if mode == "algo":
        fresh_signal_bar_symbols = {
            ticker for ticker, bars in bars_by_symbol.items()
            if (age := _latest_bar_age_seconds(bars, now)) is not None
            and 0 <= age <= signal_bar_max_age_seconds
        }
        if selected_symbols and not fresh_signal_bar_symbols.intersection(selected_symbols):
            entry_pause_reasons.append("the Algo universe has no recent completed signal bars")
            if historical_status == "ok":
                historical_status = "stale"

    spy_last_completed_date: str | None = None
    spy_daily_age_days: int | None = None
    if mode == "algo":
        if daily_bars.get("SPY"):
            try:
                spy_last_completed_date = str(daily_bars["SPY"][-1]["t"])[:10]
                spy_daily_age_days = (date.fromisoformat(today) - date.fromisoformat(spy_last_completed_date)).days
            except (KeyError, TypeError, ValueError):
                spy_daily_age_days = None
        if spy_daily_age_days is None or not 1 <= spy_daily_age_days <= MAX_SPY_DAILY_AGE_DAYS:
            entry_pause_reasons.append("the latest completed SPY daily bar is stale or invalid")
            if historical_status == "ok":
                historical_status = "stale"

    try:
        regime = rank_strategies(daily_bars.get("SPY", []), mode=mode)
    except Exception as exc:
        if mode != "algo":
            raise
        regime = {"regime": "unknown", "reason": "SPY regime calculation failed", "ranked": [], "active": []}
        scan_errors.append(f"SPY regime calculation: {type(exc).__name__}: {exc}")
        entry_pause_reasons.append("SPY regime calculation failed")
    if mode == "algo":
        try:
            regime = apply_regime_advisory(regime)
        except Exception as exc:
            # The optional local advisor must never interrupt deterministic
            # strategy exits or quote-driven protection.
            scan_errors.append(f"optional regime advisor: {type(exc).__name__}: {exc}")
            regime["regime_advisory"] = {"status": "fallback", "error": str(exc)}
        if regime.get("regime") == "unknown":
            entry_pause_reasons.append("deterministic SPY regime is unavailable")
            if historical_status == "ok":
                historical_status = "degraded"
    equity = float(account.get("equity", 0) or 0)
    quote_errors: list[str] = []
    latest_quotes = _fetch_latest_quotes(
        strategy_symbols, mode=mode, feed=quote_feed,
        errors=quote_errors if mode == "algo" else None,
    )
    freshness_limit = int(STRATEGY_CONFIG["quote_stale_seconds"])
    quote_checked_at = datetime.now(timezone.utc)
    fresh_quote_symbols = {
        ticker for ticker, quote in latest_quotes.items()
        if 0 <= (quote_checked_at - quote["timestamp"]).total_seconds() <= freshness_limit
    }
    owned_positions_without_fresh_quotes = sorted(
        ticker for ticker, position in positions.items()
        if ticker in owner_map and float(position.get("qty", 0) or 0) > 0
        and ticker not in fresh_quote_symbols
    ) if track_owners else []
    owned_positions_without_software_stops = sorted(
        ticker for ticker, position in positions.items()
        if ticker in owner_map and float(position.get("qty", 0) or 0) > 0
        and _positive_price_level((strategy_states or {}).get(owner_map[ticker], {}).get(ticker, {}).get("stop_price")) is None
    ) if track_owners else []
    if mode == "algo":
        if quote_errors:
            scan_errors.extend(quote_errors)
            entry_pause_reasons.append("latest quote requests failed")
        if owned_positions_without_fresh_quotes:
            entry_pause_reasons.append("owned positions lack fresh protective quotes")
        if owned_positions_without_software_stops:
            entry_pause_reasons.append("owned positions lack valid software stop levels")
        if selected_symbols and not fresh_quote_symbols.intersection(selected_symbols):
            entry_pause_reasons.append("the Algo universe has no fresh quotes")
    quote_status = (
        "idle" if not strategy_symbols else
        "degraded" if quote_errors or owned_positions_without_fresh_quotes else
        "unavailable" if not fresh_quote_symbols else "ok"
    )
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
            # A long holding can only exit near the bid. Use that executable
            # side for both stop and take-profit thresholds.
            current_price = float(quote.get("bid", quote["price"]))
            stop_price = _positive_price_level(protective_state.get("stop_price"))
            target_price = _positive_price_level(protective_state.get("take_profit"))
            protective_reason = None
            if 0 <= quote_age <= int(STRATEGY_CONFIG["quote_stale_seconds"]):
                if stop_price is not None and current_price <= stop_price:
                    protective_reason = "software stop-loss reached"
                elif target_price is not None and current_price >= target_price:
                    protective_reason = "software take-profit reached"
            if protective_reason:
                signal_bar_time = symbol_bars[-1].get("t") if symbol_bars else None
                if lifecycle is None or lifecycle.allows_protective_exit(owner, ticker):
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
            try:
                signal = evaluate_strategy(strategy_name, mode, ticker, symbol_bars, position_qty)
            except Exception as exc:
                if mode != "algo":
                    raise
                scan_errors.append(f"{strategy_name} signal {ticker}: {type(exc).__name__}: {exc}")
                entry_pause_reasons.append("one or more Algo signal rules failed")
                continue
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
        chosen = max(
            options,
            key=lambda item: (bool(item.get("software_protection")), item["rank_score"], item["strength"]),
        )
        ticker_orders = pending_by_symbol.get(ticker, [])
        pending_entry = any(str(order.get("side", "")).lower() == "buy" for order in ticker_orders)
        protective_exit_order = any(_is_protective_sell_order(order) for order in ticker_orders)
        pending_exit = any(
            str(order.get("side", "")).lower() == "sell"
            and not _is_protective_sell_order(order)
            for order in ticker_orders
        )
        if mode == "algo" and chosen["action"] == "sell" and protective_exit_order:
            # A broker bracket/OCO already owns the exit quantity. Alpaca may
            # cancel its stop and profit legs at different times; replacing a
            # rule exit by cancelling both can leave the holding without a
            # stop even during regular hours. Let the broker exit stand.
            chosen["suppressed_reason"] = (
                "broker protective order remains active; wait for its stop or target"
            )
            generated.append(chosen)
            continue
        if (chosen["action"] == "buy" and ticker_orders) or pending_entry or pending_exit:
            chosen["suppressed_reason"] = "a conflicting Alpaca order is already open for this symbol"
            generated.append(chosen)
            continue
        if regime.get("regime") == "trending_down" and chosen["action"] == "buy":
            chosen["suppressed_reason"] = "long entries are disabled in the downtrend regime"
            generated.append(chosen)
            continue
        if mode == "algo" and not chosen.get("software_protection") and ticker not in fresh_signal_bar_symbols:
            chosen["suppressed_reason"] = "latest completed signal bar is stale or unavailable"
            generated.append(chosen)
            continue
        generated.append(chosen)

    entries_paused = bool(entry_pause_reasons)
    if entries_paused:
        reason = "; ".join(dict.fromkeys(entry_pause_reasons))
        for item in generated:
            if item["action"] == "buy" and not item.get("suppressed_reason"):
                item["suppressed_reason"] = reason

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
            signal_kind = "protective" if item.get("software_protection") else "rule"
            identity = f"{mode}:{today}:{item['strategy']}:{item['ticker']}:{item['action']}:{signal_kind}:{item.get('signal_bar_time') or ''}"
            trade["client_order_id"] = f"rule-{today.replace('-', '')}-{hashlib.sha256(identity.encode()).hexdigest()[:12]}"
            trade["signal_bar_time"] = item.get("signal_bar_time")
            item_orders = pending_by_symbol.get(item["ticker"], [])
            protective_exit = any(_is_protective_sell_order(order) for order in item_orders)
            if trade["action"] == "sell" and protective_exit:
                trade["cancel_open_orders"] = True
            trades.append(trade)
            item["entry_price"] = price
            item["qty"] = trade["qty"]
        else:
            item["suppressed_reason"] = "position or strategy sizing produced zero shares"

    try:
        pair_setups = pairs.candidates(
            daily_bars,
            minimum_correlation=float(STRATEGY_CONFIG["pairs_min_correlation"]),
            entry_z=float(STRATEGY_CONFIG["pairs_entry_z"]),
        )
    except Exception as exc:
        if mode != "algo":
            raise
        pair_setups = []
        scan_errors.append(f"signal-only pair scan: {type(exc).__name__}: {exc}")
    pair_output = []
    try:
        for pair_setup in pair_setups:
            pair_setup_data = pair_setup.to_dict()
            pair_setup_data["execution_enabled"] = False
            pair_setup_data["execution_note"] = "signal only: paired orders are not atomic in the current executor"
            pair_output.append(pair_setup_data)
    except Exception as exc:
        if mode != "algo":
            raise
        pair_output = []
        scan_errors.append(f"signal-only pair report: {type(exc).__name__}: {exc}")

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
        "entries_paused": entries_paused,
        "entry_pause_reasons": list(dict.fromkeys(entry_pause_reasons)),
        "scan_errors": scan_errors,
        "unowned_positions": unowned_positions,
        "data_status": {
            "historical_bars": historical_status,
            "bar_feed": data_feed,
            "bar_end": historical_bar_end,
            "quote_feed": quote_feed,
            "quotes": quote_status,
            "fresh_quote_count": len(fresh_quote_symbols),
            "requested_quote_count": len(strategy_symbols),
            "owned_positions_without_fresh_quotes": owned_positions_without_fresh_quotes,
            "owned_positions_without_software_stops": owned_positions_without_software_stops,
            "historical_delay_seconds": delay_seconds,
            "fresh_signal_bar_count": len(fresh_signal_bar_symbols),
            "signal_bar_max_age_seconds": signal_bar_max_age_seconds if mode == "algo" else None,
            "spy_last_completed_date": spy_last_completed_date,
            "spy_daily_age_days": spy_daily_age_days,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan the dynamic universe with pure rule-based strategies")
    parser.add_argument("--mode", choices=["swing", "day"], default="swing")
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
