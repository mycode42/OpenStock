"""Dataset loading and command handlers for Algo replay."""

from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import requests

from .config import REGIME_WARMUP_DAYS, ET
from .config import multi_factor_warmup_days, validate_research_controls, validate_strategy_filter
from market.store import SQLiteBarStore, fetch_alpaca_bars
from strategies.event_driven import EventCalendar
from strategies.registry import validate_parameter_profile
from portfolio.config import candidate_pool_tickers
from portfolio.config import get_algo_regime_config
from market.regime import load_fred_regime_series
from market.data import data_tier, regular_feed


def _split_symbols(value: str) -> list[str]:
    symbols = list(dict.fromkeys(item.strip().upper() for item in value.split(",") if item.strip()))
    if not symbols:
        raise argparse.ArgumentTypeError("Provide at least one comma-separated symbol")
    return symbols

def _regular_session_bars(bars_by_symbol: dict[str, list[dict]]) -> tuple[dict[str, list[dict]], int, int]:
    """Keep only valid regular-session bars; the replay has no extended-hours model."""
    filtered: dict[str, list[dict]] = {}
    outside_session = invalid_ohlc = 0
    for symbol, bars in bars_by_symbol.items():
        kept = []
        for bar in bars:
            try:
                timestamp = datetime.fromisoformat(str(bar["t"]).replace("Z", "+00:00"))
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                local_time = timestamp.astimezone(ET).time().replace(tzinfo=None)
                if not time(9, 30) <= local_time < time(16, 0):
                    outside_session += 1
                    continue
                opening, high, low, close = (float(bar[key]) for key in ("o", "h", "l", "c"))
                if (
                    not all(math.isfinite(value) and value > 0 for value in (opening, high, low, close))
                    or high < max(opening, close, low)
                    or low > min(opening, close, high)
                ):
                    invalid_ohlc += 1
                    continue
            except (KeyError, TypeError, ValueError, OverflowError):
                invalid_ohlc += 1
                continue
            kept.append(bar)
        if kept:
            filtered[symbol] = kept
    return filtered, outside_session, invalid_ohlc

def _read_profile(path: str | None) -> dict:
    if not path:
        return {}
    profile = json.loads(Path(path).read_text())
    return validate_parameter_profile(profile)

def read_borrow_availability(path: str | None) -> dict[tuple[str, str], bool]:
    """Read optional point-in-time borrow data with date,symbol,easy_to_borrow columns."""
    if not path:
        return {}
    output: dict[tuple[str, str], bool] = {}
    with Path(path).open(newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"date", "symbol", "easy_to_borrow"}
        if not required.issubset(set(reader.fieldnames or [])):
            raise ValueError("Borrow CSV requires date,symbol,easy_to_borrow columns")
        for row in reader:
            day = date.fromisoformat(str(row["date"]).strip()).isoformat()
            ticker = str(row["symbol"]).strip().upper()
            value = str(row["easy_to_borrow"]).strip().lower()
            if not ticker or value not in {"true", "false", "1", "0", "yes", "no", "easy", "hard"}:
                raise ValueError("Borrow CSV has an invalid symbol or easy_to_borrow value")
            output[(ticker, day)] = value in {"true", "1", "yes", "easy"}
    return output

def _read_strategy_filter(value: str | None) -> list[str] | None:
    if not value:
        return None
    return validate_strategy_filter([item.strip() for item in value.split(",") if item.strip()])

def _read_research_controls(args) -> dict:
    controls = {}
    for cli_name, key in (
        ("research_max_entries_per_day", "max_entries_per_day"),
        ("research_max_open_positions", "max_open_positions"),
        ("research_max_positions_per_strategy", "max_positions_per_strategy"),
        ("research_cooldown_minutes", "cooldown_minutes"),
        ("research_max_momentum_entries_per_day", None),
    ):
        value = getattr(args, cli_name, None)
        if value is None:
            continue
        if key is not None:
            controls[key] = value
        else:
            controls["max_entries_per_strategy_per_day"] = {"swing_momentum": value}
    return validate_research_controls(controls)

def fetch_dataset(args) -> dict:
    symbols = _split_symbols(args.symbols)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    if end < start:
        raise ValueError("End date must not be before start date")
    feed = args.feed or regular_feed()
    tier = data_tier()
    store = SQLiteBarStore(args.db)
    selected_strategies = _read_strategy_filter(getattr(args, "strategies", None))
    needs_factor_warmup = selected_strategies is None or "swing_multi_factor" in selected_strategies
    needs_split_adjustment = needs_factor_warmup or selected_strategies is None or "swing_event_driven" in selected_strategies
    adjustment = args.adjustment or ("split" if needs_split_adjustment else "raw")
    warmup_days = multi_factor_warmup_days(_read_profile(getattr(args, "profile", None))) if needs_factor_warmup else 0
    context_symbols = list(dict.fromkeys(
        candidate_pool_tickers()
        + ["HYG", "LQD"]
    ))
    all_symbols = list(dict.fromkeys(symbols + context_symbols))
    regime_warmup_days = max(REGIME_WARMUP_DAYS, warmup_days)
    data_start = start - timedelta(days=regime_warmup_days)
    request_start = f"{data_start.isoformat()}T00:00:00Z"
    request_end = f"{end.isoformat()}T23:59:59Z"
    intraday = fetch_alpaca_bars(
        all_symbols, timeframe="5Min", start=request_start, end=request_end,
        feed=feed, adjustment=adjustment,
    )
    intraday, outside_session_bars, invalid_ohlc_bars = _regular_session_bars(intraday)
    intraday_rows = store.write_bars(
        intraday, feed=feed, timeframe="5Min", adjustment=adjustment,
        data_tier=tier, requested_start=request_start, requested_end=request_end,
    )
    daily_start = start - timedelta(days=REGIME_WARMUP_DAYS)
    daily_from = f"{daily_start.isoformat()}T00:00:00Z"
    daily = fetch_alpaca_bars(
        ["SPY"], timeframe="1Day", start=daily_from, end=request_end,
        feed=feed, adjustment=adjustment,
    )
    daily_rows = store.write_bars(
        daily, feed=feed, timeframe="1Day", adjustment=adjustment,
        data_tier=tier, requested_start=daily_from, requested_end=request_end,
    )
    return {
        "database": str(Path(args.db).resolve()), "data_tier": tier, "feed": feed,
        "adjustment": adjustment, "intraday_rows": intraday_rows,
        "intraday_warmup_days": warmup_days,
        "regime_context_warmup_days": regime_warmup_days,
        "regime_context_symbols": context_symbols,
        "discarded_outside_session_bars": outside_session_bars,
        "discarded_invalid_ohlc_bars": invalid_ohlc_bars,
        "spy_daily_rows": daily_rows, "symbols": symbols,
    }

def run_backtest(args) -> dict:
    from . import AlgoPortfolioBacktest

    symbols = _split_symbols(args.symbols)
    feed = args.feed or regular_feed()
    store = SQLiteBarStore(args.db)
    selected_strategies = _read_strategy_filter(getattr(args, "strategies", None))
    needs_factor_warmup = selected_strategies is None or "swing_multi_factor" in selected_strategies
    needs_split_adjustment = needs_factor_warmup or selected_strategies is None or "swing_event_driven" in selected_strategies
    adjustment = args.adjustment or ("split" if needs_split_adjustment else "raw")
    profile = _read_profile(args.profile)
    warmup_days = multi_factor_warmup_days(profile) if needs_factor_warmup else 0
    context_symbols = list(dict.fromkeys(
        candidate_pool_tickers()
        + ["HYG", "LQD"]
    ))
    all_symbols = list(dict.fromkeys(symbols + context_symbols))
    regime_warmup_days = max(REGIME_WARMUP_DAYS, warmup_days)
    data_start = (date.fromisoformat(args.start) - timedelta(days=regime_warmup_days)).isoformat()
    intraday = store.bars(
        all_symbols, feed=feed, timeframe="5Min", start=data_start,
        end=args.end, adjustment=adjustment,
    )
    daily_start = (date.fromisoformat(args.start) - timedelta(days=REGIME_WARMUP_DAYS)).isoformat()
    daily = store.bars(
        ["SPY"], feed=feed, timeframe="1Day", start=daily_start,
        end=args.end, adjustment=adjustment,
    ).get("SPY", [])
    missing = [ticker for ticker in symbols if not intraday.get(ticker)]
    if missing:
        raise ValueError(f"No stored 5-minute {feed} bars for: {', '.join(missing)}")
    macro_series, macro_diagnostics = load_fred_regime_series(store, refresh=True)
    replay = AlgoPortfolioBacktest(
        symbols=symbols, intraday_bars=intraday, spy_daily_bars=daily,
        start=args.start, end=args.end, feed=feed,
        data_adjustment=adjustment, initial_capital=args.capital,
        slippage_bps=args.slippage_bps, profile=profile,
        spread_bps=getattr(args, "spread_bps", 3.0),
        commission_per_share=getattr(args, "commission_per_share", 0.005),
        short_borrow_annual_pct=getattr(args, "short_borrow_annual_pct", 3.0) / 100,
        unknown_borrow_penalty_bps=getattr(args, "unknown_borrow_penalty_bps", 25.0),
        borrow_availability=read_borrow_availability(getattr(args, "borrow_availability", None)),
        strategies=selected_strategies,
        research_controls=_read_research_controls(args),
        event_calendar=(EventCalendar.from_csv(args.event_calendar) if getattr(args, "event_calendar", None) else None),
        regime_macro_series=macro_series,
        regime_config={**get_algo_regime_config(), "macro_data_status": macro_diagnostics.get("status")},
    )
    report = replay.run()
    report["data_tier_at_run"] = data_tier()
    report["database"] = str(Path(args.db).resolve())
    report["multi_factor_warmup_days"] = warmup_days
    report["regime_context_warmup_days"] = regime_warmup_days
    report["regime_context_symbols"] = context_symbols
    report["regime_macro_data"] = macro_diagnostics
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True))
        report["report_file"] = str(output.resolve())
    return report

