"""Chronological research-only profile selection and out-of-sample replay."""

from __future__ import annotations

import argparse
from bisect import bisect_left
from concurrent.futures import ProcessPoolExecutor, as_completed
from copy import deepcopy
import gc
import json
import math
import os
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from src.algo.backtest import (
    AlgoPortfolioBacktest, DAILY_WARMUP_DAYS, MIN_REGIME_DAILY_BARS,
    multi_factor_warmup_days, read_borrow_availability,
    validate_research_controls, validate_strategy_filter,
)
from src.algo.market_store import DEFAULT_DB, SQLiteBarStore
from src.market_data import data_tier, regular_feed
from src.strategies.event_driven import EventCalendar, EventStrategyConfig
from src.strategies.registry import validate_parameter_profile
from src.strategies.satellite_overlay import SatelliteOverlayConfig


ET = ZoneInfo("America/New_York")
METRICS = (
    "net_return_pct", "max_drawdown_pct", "closed_trade_count", "win_rate_pct",
    "profit_factor", "daily_sharpe_annualized", "final_equity",
    "benchmark_net_return_pct", "excess_return_vs_buy_hold_pct_points",
    "open_position_count",
    "entry_count", "average_entries_per_session", "median_entries_per_session",
    "max_entries_per_session", "entry_count_by_strategy", "signal_event_count_by_strategy",
    "candidate_setup_count", "candidate_ticker_day_count",
    "candidate_setup_count_by_strategy", "average_candidate_ticker_days_per_session",
    "strategy_metrics", "average_holding_minutes", "median_holding_minutes",
    "average_calendar_holding_hours", "max_consecutive_losses", "time_in_market_pct",
    "multi_factor_diagnostics", "event_strategy_diagnostics", "satellite_confirmation_candidates",
    "satellite_confirmed_entries",
    "strategy_correlation_report",
)

NEIGHBOR_THRESHOLDS = {
    "trend_following": {"minimum_slope_pct": (0.001, 0.0, 0.03)},
    "momentum": {"minimum_score_spread": (0.01, 0.01, 0.50), "minimum_average_dollar_volume": (1_000_000, 1_000_000, 1_000_000_000)},
    "mean_reversion": {
        "minimum_dip_pct": (0.001, 0.0, 0.5), "minimum_bounce_pct": (0.0005, 0.0, 0.1),
        "minimum_rip_pct": (0.001, 0.0, 0.5), "minimum_fade_pct": (0.0005, 0.0, 0.1),
        "minimum_volume_ratio": (0.1, 0.1, 20.0),
    },
    "opening_range_breakout": {"minimum_volume_ratio": (0.1, 0.1, 20.0), "breakout_buffer_pct": (0.001, 0.0, 0.05)},
    "swing_mean_reversion": {"stretch_pct": (0.005, 0.02, 0.20), "max_reference_slope_pct": (0.002, 0.005, 0.10)},
    "multi_factor": {"minimum_score": (0.1, -10.0, 10.0)},
    "event_driven": {
        "minimum_gap_pct": (0.005, 0.005, 0.10), "minimum_volume_ratio": (0.1, 1.0, 10.0),
        "minimum_macro_move_pct": (0.001, 0.001, 0.05),
    },
}


def walk_forward_windows(
    trading_dates: list[date], *, train_days: int, test_days: int, step_days: int,
) -> list[dict[str, str | int]]:
    """Return rolling, chronological windows; the final test window may be shorter."""
    if min(train_days, test_days, step_days) < 1:
        raise ValueError("train_days, test_days, and step_days must be positive")
    if step_days < test_days:
        raise ValueError("step_days must be at least test_days so out-of-sample folds do not overlap")
    dates = sorted(set(trading_dates))
    windows = []
    test_index = train_days
    while test_index < len(dates):
        train = dates[max(0, test_index - train_days):test_index]
        test = dates[test_index:test_index + test_days]
        if not train or not test:
            break
        windows.append({
            "train_start": train[0].isoformat(), "train_end": train[-1].isoformat(),
            "test_start": test[0].isoformat(), "test_end": test[-1].isoformat(),
            "train_session_count": len(train), "test_session_count": len(test),
        })
        test_index += step_days
    return windows


def _trim_sessions_without_regime_warmup(
    trading_dates: list[date], spy_daily_dates: list[date], *, required_bars: int = MIN_REGIME_DAILY_BARS,
) -> tuple[list[date], int]:
    """Skip leading replay sessions until historical SPY regime warmup exists."""
    if required_bars < 0:
        raise ValueError("required_bars must be non-negative")
    dates = sorted(set(trading_dates))
    daily_dates = sorted(set(spy_daily_dates))
    offset = next((
        index for index, session in enumerate(dates)
        if bisect_left(daily_dates, session) >= required_bars
    ), len(dates))
    return dates[offset:], offset


def _read_profiles(path: str) -> list[dict]:
    payload = json.loads(Path(path).read_text())
    if not isinstance(payload, list) or not payload:
        raise ValueError("Profiles file must contain a non-empty JSON array")
    output = []
    names = set()
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValueError("Each profile requires a string name")
        name = item["name"].strip()
        if not name or name in names:
            raise ValueError("Profile names must be non-empty and unique")
        names.add(name)
        strategies = validate_strategy_filter(item.get("strategies"))
        parameters = validate_parameter_profile(item.get("parameters", {}))
        controls = validate_research_controls(item.get("controls"))
        output.append({
            "name": name, "strategies": strategies,
            "parameters": parameters, "controls": controls,
        })
    if not any(profile["name"] == "baseline" for profile in output):
        output.insert(0, {
            "name": "baseline", "strategies": None,
            "parameters": {}, "controls": {},
        })
    return output


def _metrics(report: dict) -> dict:
    return {key: report.get(key) for key in METRICS}


def _selection_score(report: dict) -> float:
    # A simple, visible train-only score. A drawdown penalty discourages selecting
    # a high-return profile that took materially more risk in the fit window.
    return float(report["net_return_pct"]) - 0.5 * float(report["max_drawdown_pct"])


def parameter_neighbors(parameters: dict) -> list[dict]:
    """Create one-at-a-time lower/upper neighbors for every tuned threshold."""
    neighbors = []
    for strategy, thresholds in NEIGHBOR_THRESHOLDS.items():
        values = parameters.get(strategy)
        if not isinstance(values, dict):
            continue
        for key, (step, minimum, maximum) in thresholds.items():
            center = values.get(key)
            if isinstance(center, bool) or not isinstance(center, (int, float)):
                continue
            for direction, candidate_value in (
                ("lower", max(minimum, float(center) - step)),
                ("upper", min(maximum, float(center) + step)),
            ):
                if math.isclose(candidate_value, float(center), abs_tol=1e-12):
                    continue
                candidate = deepcopy(parameters)
                candidate[strategy][key] = int(candidate_value) if isinstance(center, int) else candidate_value
                try:
                    normalized = validate_parameter_profile(candidate)
                except ValueError:
                    continue
                neighbors.append({
                    "parameter_key": f"{strategy}.{key}",
                    "direction": direction,
                    "value": normalized[strategy][key],
                    "parameters": normalized,
                })
    return neighbors


def evaluate_promotion_gate(folds: list[dict], *, minimum_test_trades_per_fold: int) -> dict:
    """Aggregate only frozen test outcomes; never changes selected parameters."""
    if minimum_test_trades_per_fold < 1:
        raise ValueError("minimum_test_trades_per_fold must be positive")
    fold_count = len(folds)
    center = [fold.get("out_of_sample", {}) for fold in folds]
    positive_net = sum(float(row.get("net_return_pct") or 0) > 0 for row in center)
    positive_excess = sum(float(row.get("excess_return_vs_buy_hold_pct_points") or 0) > 0 for row in center)
    trade_counts = [int(row.get("closed_trade_count") or 0) for row in center]
    enough_trades = bool(trade_counts) and all(count >= minimum_test_trades_per_fold for count in trade_counts)
    majority = fold_count >= 3 and positive_net > fold_count / 2
    majority_excess = fold_count >= 3 and positive_excess > fold_count / 2
    selected_profiles = [fold.get("selected_profile") for fold in folds]
    same_selected_profile = (
        fold_count >= 3 and all(isinstance(name, str) and name for name in selected_profiles)
        and len(set(selected_profiles)) == 1
    )

    by_parameter: dict[str, dict[str, list[dict]]] = {}
    for fold in folds:
        for row in fold.get("neighbor_tests", []):
            by_parameter.setdefault(row["parameter_key"], {"lower": [], "upper": []})[row["direction"]].append(row)
    stability = {}
    for key, sides in by_parameter.items():
        side_results = {}
        for side in ("lower", "upper"):
            rows = sides[side]
            supported_folds = sum(
                float(row.get("net_return_pct") or 0) > 0
                and float(row.get("excess_return_vs_buy_hold_pct_points") or 0) > 0
                and int(row.get("closed_trade_count") or 0) >= minimum_test_trades_per_fold
                for row in rows
            )
            side_results[side] = {
                "positive_test_folds": supported_folds,
                "tested_folds": len(rows),
                "all_folds_meet_minimum_trade_count": (
                    len(rows) == fold_count and all(
                        int(row.get("closed_trade_count") or 0) >= minimum_test_trades_per_fold
                        for row in rows
                    )
                ),
                "supported": len(rows) == fold_count and supported_folds > fold_count / 2,
            }
        stability[key] = {
            **side_results,
            "stable": bool(side_results["lower"]["supported"] or side_results["upper"]["supported"]),
        }
    unstable = sorted(key for key, result in stability.items() if not result["stable"])
    stability_pass = not unstable
    checks = {
        "at_least_three_non_overlapping_test_folds": fold_count >= 3,
        "same_selected_config_in_every_fold": same_selected_profile,
        "positive_after_costs_in_majority_of_folds": majority,
        "positive_excess_over_buy_and_hold_in_majority_of_folds": majority_excess,
        "minimum_trade_count_met_in_every_fold": enough_trades,
        "neighbor_parameter_stability": stability_pass,
    }
    return {
        "promotable": all(checks.values()),
        "checks": checks,
        "fold_count": fold_count,
        "selected_profiles": selected_profiles,
        "minimum_test_trades_per_fold": minimum_test_trades_per_fold,
        "positive_net_return_folds": positive_net,
        "positive_excess_return_folds": positive_excess,
        "test_trade_counts": trade_counts,
        "unstable_parameters": unstable,
        "parameter_stability": stability,
        "limitations": [
            "Promotion eligibility is research evidence only; it never updates live settings.",
            "Neighbor variants are fixed before reading each fold's test outcomes and are never refit on a test fold.",
        ],
    }


def _enabled_satellites(profile: dict) -> set[str]:
    overlay = SatelliteOverlayConfig.from_dict(profile["parameters"].get("satellite_overlay"))
    event_config = EventStrategyConfig.from_dict(profile["parameters"].get("event_driven"))
    selected = profile["strategies"]
    includes = lambda name: selected is None or name in selected
    enabled = set()
    if overlay.multi_factor_enabled and includes("multi_factor"):
        enabled.add("multi_factor")
    if overlay.event_driven_enabled and event_config.enabled and includes("event_driven"):
        enabled.add("event_driven")
    return enabled


def _run_portfolio(
    *, symbols: list[str], intraday: dict, spy_daily: list[dict], start: str, end: str,
    feed: str, adjustment: str, capital: float, slippage_bps: float,
    spread_bps: float, commission_per_share: float, short_borrow_annual_pct: float,
    unknown_borrow_penalty_bps: float, borrow_availability: dict[tuple[str, str], bool],
    profile: dict, strategies: list[str] | None, controls: dict,
    event_calendar: EventCalendar | None = None,
) -> dict:
    return AlgoPortfolioBacktest(
        symbols=symbols, intraday_bars=intraday, spy_daily_bars=spy_daily,
        start=start, end=end, feed=feed, data_adjustment=adjustment,
        initial_capital=capital, slippage_bps=slippage_bps, profile=profile,
        strategies=strategies, research_controls=controls, event_calendar=event_calendar,
        spread_bps=spread_bps, commission_per_share=commission_per_share,
        short_borrow_annual_pct=short_borrow_annual_pct,
        unknown_borrow_penalty_bps=unknown_borrow_penalty_bps,
        borrow_availability=borrow_availability, force_close_at_end=True,
    ).run()


def _run_fold_job(payload: tuple[int, dict, list[str], list[dict], list[dict], argparse.Namespace]) -> tuple[int, dict]:
    """Evaluate one independent fold in a worker process."""
    index, window, symbols, profiles, spy_daily, args = payload
    store = SQLiteBarStore(args.db)
    cost_args = {
        "spread_bps": args.spread_bps,
        "commission_per_share": args.commission_per_share,
        "short_borrow_annual_pct": args.short_borrow_annual_pct / 100,
        "unknown_borrow_penalty_bps": args.unknown_borrow_penalty_bps,
        "borrow_availability": args.borrow_availability_data,
    }
    event_calendar = EventCalendar.from_csv(args.event_calendar) if getattr(args, "event_calendar", None) else None
    warmup_days = max((
        multi_factor_warmup_days(profile["parameters"])
        for profile in profiles
        if profile["strategies"] is None or "multi_factor" in profile["strategies"]
    ), default=0)
    train_start = (
        date.fromisoformat(window["train_start"]) - timedelta(days=warmup_days)
    ).isoformat() if warmup_days else window["train_start"]
    intraday = store.bars(
        symbols, feed=args.feed, timeframe="5Min",
        start=train_start, end=window["train_end"],
        adjustment=args.adjustment,
    )
    train_runs = []
    for candidate in profiles:
        report = _run_portfolio(
            symbols=symbols, intraday=intraday, spy_daily=spy_daily,
            start=window["train_start"], end=window["train_end"], feed=args.feed,
            adjustment=args.adjustment, capital=args.capital,
            slippage_bps=args.slippage_bps, **cost_args, profile=candidate["parameters"],
            strategies=candidate["strategies"], controls=candidate["controls"],
            event_calendar=event_calendar,
        )
        enabled_satellites = _enabled_satellites(candidate)
        confirmed_entries = report.get("satellite_confirmed_entries", {})
        satellite_sample_eligible = all(
            int(confirmed_entries.get(name, 0)) >= getattr(args, "min_satellite_confirmed_entries", 3)
            for name in enabled_satellites
        )
        train_runs.append({
            "name": candidate["name"], "parameters": candidate["parameters"],
            "strategies": candidate["strategies"], "controls": candidate["controls"],
            "score": round(_selection_score(report), 6), "metrics": _metrics(report),
            "enabled_satellites": sorted(enabled_satellites),
            "satellite_sample_eligible": satellite_sample_eligible,
            "frequency_eligible": (
                report["average_candidate_ticker_days_per_session"]
                <= args.max_candidate_ticker_days_per_session
            ),
            "eligible": (
                report["closed_trade_count"] >= args.min_train_trades
                and report["max_drawdown_pct"] <= args.max_train_drawdown_pct
                and report["average_candidate_ticker_days_per_session"]
                <= args.max_candidate_ticker_days_per_session
                and satellite_sample_eligible
            ),
        })
        del report
    eligible = [item for item in train_runs if item["eligible"]]
    sample_and_frequency_eligible = [
        item for item in train_runs
        if item["frequency_eligible"] and item["satellite_sample_eligible"]
    ]
    baseline = next(item for item in train_runs if item["name"] == "baseline")
    if eligible:
        selected = max(eligible, key=lambda item: (item["score"], -item["metrics"]["max_drawdown_pct"]))
        selected_reason = "passed training trade-count, drawdown, candidate-frequency, and satellite-sample gates"
    elif sample_and_frequency_eligible:
        selected = max(sample_and_frequency_eligible, key=lambda item: (item["score"], -item["metrics"]["max_drawdown_pct"]))
        selected_reason = "no profile passed all gates; best-scoring sample- and frequency-compliant profile selected"
    else:
        selected = baseline
        selected_reason = "no profile met sample and candidate-frequency targets; baseline fallback"
    del intraday
    gc.collect()

    test_start = (
        date.fromisoformat(window["test_start"]) - timedelta(days=warmup_days)
    ).isoformat() if warmup_days else window["test_start"]
    intraday = store.bars(
        symbols, feed=args.feed, timeframe="5Min",
        start=test_start, end=window["test_end"],
        adjustment=args.adjustment,
    )
    test_report = _run_portfolio(
        symbols=symbols, intraday=intraday, spy_daily=spy_daily,
        start=window["test_start"], end=window["test_end"], feed=args.feed,
        adjustment=args.adjustment, capital=args.capital,
        slippage_bps=args.slippage_bps, **cost_args, profile=selected["parameters"],
        strategies=selected["strategies"], controls=selected["controls"],
        event_calendar=event_calendar,
    )
    neighbor_tests = []
    for neighbor in parameter_neighbors(selected["parameters"]):
        neighbor_report = _run_portfolio(
            symbols=symbols, intraday=intraday, spy_daily=spy_daily,
            start=window["test_start"], end=window["test_end"], feed=args.feed,
            adjustment=args.adjustment, capital=args.capital,
            slippage_bps=args.slippage_bps, **cost_args,
            profile=neighbor["parameters"], strategies=selected["strategies"],
            controls=selected["controls"], event_calendar=event_calendar,
        )
        neighbor_tests.append({
            "parameter_key": neighbor["parameter_key"],
            "direction": neighbor["direction"],
            "value": neighbor["value"],
            "net_return_pct": neighbor_report["net_return_pct"],
            "benchmark_net_return_pct": neighbor_report["benchmark_net_return_pct"],
            "excess_return_vs_buy_hold_pct_points": neighbor_report["excess_return_vs_buy_hold_pct_points"],
            "closed_trade_count": neighbor_report["closed_trade_count"],
        })
        del neighbor_report
    gc.collect()
    result = {
        **window, "selected_profile": selected["name"],
        "selected_parameters": selected["parameters"],
        "selected_strategies": selected["strategies"],
        "selected_controls": selected["controls"],
        "selected_reason": selected_reason,
        "training_frequency_target_met": selected["frequency_eligible"],
        "eligible_training_profiles": len(eligible),
        "frequency_eligible_training_profiles": sum(item["frequency_eligible"] for item in train_runs),
        "satellite_sample_eligible_training_profiles": sum(item["satellite_sample_eligible"] for item in train_runs),
        "training_candidates": train_runs,
        "out_of_sample": _metrics(test_report),
        "neighbor_tests": neighbor_tests,
    }
    del intraday, test_report
    gc.collect()
    return index, result


def run_walk_forward(args) -> dict:
    symbols = list(dict.fromkeys(s.strip().upper() for s in args.symbols.split(",") if s.strip()))
    if not symbols:
        raise ValueError("Provide at least one comma-separated symbol")
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    if end < start:
        raise ValueError("End date must not be before start date")
    if args.min_train_trades < 0 or args.max_train_drawdown_pct < 0:
        raise ValueError("Training eligibility limits must be non-negative")
    min_satellite_samples = getattr(args, "min_satellite_confirmed_entries", 3)
    if isinstance(min_satellite_samples, bool) or min_satellite_samples < 0:
        raise ValueError("min_satellite_confirmed_entries must be non-negative")
    args.min_satellite_confirmed_entries = min_satellite_samples
    frequency_limit = getattr(args, "max_candidate_ticker_days_per_session", 3.0)
    if not math.isfinite(frequency_limit) or frequency_limit <= 0:
        raise ValueError("max_candidate_ticker_days_per_session must be finite and positive")
    args.max_candidate_ticker_days_per_session = frequency_limit
    minimum_test_trades = getattr(args, "min_test_trades_per_fold", 3)
    if isinstance(minimum_test_trades, bool) or not isinstance(minimum_test_trades, int) or minimum_test_trades < 1:
        raise ValueError("min_test_trades_per_fold must be a positive integer")
    args.min_test_trades_per_fold = minimum_test_trades
    for name in ("slippage_bps", "spread_bps", "commission_per_share", "short_borrow_annual_pct", "unknown_borrow_penalty_bps"):
        value = getattr(args, name)
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be finite and non-negative")
    args.borrow_availability_data = read_borrow_availability(getattr(args, "borrow_availability", None))

    profiles = _read_profiles(args.profiles)
    event_profiles = []
    for profile in profiles:
        overlay = SatelliteOverlayConfig.from_dict(profile["parameters"].get("satellite_overlay"))
        event_config = EventStrategyConfig.from_dict(profile["parameters"].get("event_driven"))
        selected = profile["strategies"]
        event_selected = selected is None or "event_driven" in selected
        if event_selected and overlay.event_driven_enabled and event_config.enabled:
            event_profiles.append(profile["name"])
    event_calendar = EventCalendar.from_csv(args.event_calendar) if getattr(args, "event_calendar", None) else None
    if event_profiles and event_calendar is None:
        raise ValueError(
            "Event Driven overlay profiles require --event-calendar: "
            + ", ".join(event_profiles)
        )
    needs_factor_adjustment = any(
        profile["strategies"] is None or "multi_factor" in profile["strategies"]
        for profile in profiles
    )
    args.adjustment = args.adjustment or ("split" if needs_factor_adjustment else "raw")

    store = SQLiteBarStore(args.db)
    coverage = store.coverage()
    symbols_with_bars = {
        row["symbol"] for row in coverage
        if row["feed"] == args.feed and row["adjustment"] == args.adjustment
        and row["timeframe"] == "5Min"
        and row["first"][:10] <= args.end and row["last"][:10] >= args.start
    }
    symbols_without_bars = [symbol for symbol in symbols if symbol not in symbols_with_bars]
    if not symbols_with_bars.intersection(symbols):
        raise ValueError(f"No stored 5-minute {args.feed} bars for any supplied symbol")
    daily_start = (start - timedelta(days=DAILY_WARMUP_DAYS)).isoformat()
    spy_daily = store.bars(
        ["SPY"], feed=args.feed, timeframe="1Day", start=daily_start, end=args.end,
        adjustment=args.adjustment,
    ).get("SPY", [])
    spy_daily_dates = sorted({
        datetime.fromisoformat(str(bar["t"]).replace("Z", "+00:00")).astimezone(ET).date()
        for bar in spy_daily
    })
    trading_dates = [session for session in spy_daily_dates if start <= session <= end]
    requested_trading_session_count = len(trading_dates)
    trading_dates, warmup_sessions_skipped = _trim_sessions_without_regime_warmup(
        trading_dates, spy_daily_dates,
    )
    windows = walk_forward_windows(
        trading_dates, train_days=args.train_days,
        test_days=args.test_days, step_days=args.step_days,
    )
    if len(windows) < 3:
        raise ValueError(
            f"The promotion gate requires at least 3 non-overlapping test folds; found {len(windows)}. "
            f"Increase the date range or reduce --train-days/--test-days (currently {len(trading_dates)} sessions)."
        )
    workers = getattr(args, "workers", 1)
    if workers < 1:
        raise ValueError("workers must be at least 1")
    workers_used = min(workers, len(windows), os.cpu_count() or workers)
    jobs = [
        (index, window, symbols, profiles, spy_daily, args)
        for index, window in enumerate(windows)
    ]
    folds_by_index = {}
    if workers_used == 1:
        for job in jobs:
            index, result = _run_fold_job(job)
            folds_by_index[index] = result
    else:
        print(
            f"Evaluating {len(jobs)} walk-forward folds with {workers_used} worker processes",
            file=sys.stderr, flush=True,
        )
        with ProcessPoolExecutor(max_workers=workers_used) as executor:
            futures = {executor.submit(_run_fold_job, job): job[0] for job in jobs}
            for completed, future in enumerate(as_completed(futures), start=1):
                index, result = future.result()
                folds_by_index[index] = result
                print(
                    f"Completed walk-forward fold {completed}/{len(jobs)} "
                    f"({result['test_start']}..{result['test_end']})",
                    file=sys.stderr, flush=True,
                )
    folds = [folds_by_index[index] for index in range(len(windows))]
    promotion_gate = evaluate_promotion_gate(
        folds, minimum_test_trades_per_fold=args.min_test_trades_per_fold,
    )

    test_returns = [fold["out_of_sample"]["net_return_pct"] for fold in folds]
    excess_returns = [fold["out_of_sample"]["excess_return_vs_buy_hold_pct_points"] for fold in folds]
    return {
        "simulator": "swarm_algo_walk_forward_v3",
        "data_tier_at_run": data_tier(), "feed": args.feed,
        "data_adjustment": args.adjustment, "worker_count": workers_used,
        "symbols": symbols,
        "symbols_without_bars_in_period": symbols_without_bars,
        "universe_model": "fixed supplied symbol list for every train and test window",
        "start": args.start, "end": args.end,
        "multi_factor_warmup_days": max((
            multi_factor_warmup_days(profile["parameters"])
            for profile in profiles
            if profile["strategies"] is None or "multi_factor" in profile["strategies"]
        ), default=0),
        "trading_session_count": requested_trading_session_count,
        "walk_forward_session_count": len(trading_dates),
        "initial_sessions_skipped_for_regime_warmup": warmup_sessions_skipped,
        "regime_warmup_daily_bars": MIN_REGIME_DAILY_BARS,
        "candidate_profile_count": len(profiles),
        "event_calendar": {
            "path": str(Path(args.event_calendar).resolve()) if getattr(args, "event_calendar", None) else None,
            **(event_calendar.diagnostics() if event_calendar else {"loaded_events": 0}),
            "event_enabled_profiles": event_profiles,
        },
        "selection_rule": (
            "Each fold ranks the fixed candidate profiles using that fold's training data only, then evaluates "
            "the selected profile and predeclared one-parameter neighbors on the next frozen test fold. "
            "Test outcomes are never used to tune a later fold. Promotion additionally requires positive "
            "after-costs and buy-and-hold excess returns in a majority of at least three test folds, "
            "minimum test trades in every fold, and non-spiky neighboring thresholds."
        ),
        "candidate_frequency_target": {
            "max_candidate_ticker_days_per_session": frequency_limit,
            "minimum_confirmed_entries_per_enabled_satellite": min_satellite_samples,
            "selection_frequency_target_met_folds": sum(
                bool(fold["training_frequency_target_met"]) for fold in folds
            ),
        },
        "fold_summary": {
            "count": len(folds), "positive_out_of_sample_folds": sum(value > 0 for value in test_returns),
            "positive_excess_over_buy_hold_folds": sum(value > 0 for value in excess_returns),
            "mean_out_of_sample_return_pct": round(statistics.mean(test_returns), 4),
            "median_out_of_sample_return_pct": round(statistics.median(test_returns), 4),
            "mean_excess_over_buy_hold_pct_points": round(statistics.mean(excess_returns), 4),
        },
        "promotion_gate": promotion_gate,
        "transaction_cost_model": {
            "spread_bps": args.spread_bps,
            "slippage_bps_each_side": args.slippage_bps,
            "commission_per_share_each_side": args.commission_per_share,
            "short_borrow_annual_pct": args.short_borrow_annual_pct,
            "unknown_borrow_adverse_fill_penalty_bps": args.unknown_borrow_penalty_bps,
            "historical_borrow_availability_rows": len(args.borrow_availability_data),
        },
        "folds": folds,
        "limitations": [
            "Profiles are selected from a supplied, bounded candidate list; this does not generate new parameters.",
            "Each fold is replayed as a flat portfolio with fresh capital; positions and risk state do not carry across fold boundaries.",
            "Universe membership is fixed for all folds; historical point-in-time selections are not available.",
            "Supplied symbols without bars in the requested period are recorded and contribute no signals; later-listed symbols can only enter from their available history.",
            "LLM advice and overnight sessions are omitted; replay uses Plus SIP regular-session bars.",
            "Explicit hard-to-borrow rows are skipped; missing historical borrow data receives the configured unknown-borrow adverse fill penalty.",
            "This report cannot update live settings and is not a performance forecast.",
            "Each fold selects from training data only; test folds are frozen and never fed into a later fit.",
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Chronological Algo profile selection and out-of-sample replay")
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--profiles", required=True, help="JSON array of named signal parameter profiles")
    parser.add_argument("--event-calendar", help="point-in-time historical event calendar CSV for Event Driven overlay profiles")
    parser.add_argument("--feed", choices=["sip"], default=regular_feed())
    parser.add_argument("--adjustment", choices=["raw", "split", "dividend", "all"], default=None)
    parser.add_argument("--train-days", type=int, default=30, help="number of prior trading sessions per fit window")
    parser.add_argument("--test-days", type=int, default=10, help="number of later trading sessions per holdout")
    parser.add_argument("--step-days", type=int, default=10, help="advance in trading sessions between holdouts")
    parser.add_argument("--min-train-trades", type=int, default=10)
    parser.add_argument("--max-train-drawdown-pct", type=float, default=10)
    parser.add_argument(
        "--min-satellite-confirmed-entries", type=int, default=3,
        help="minimum training confirmations required for every enabled satellite",
    )
    parser.add_argument(
        "--max-candidate-ticker-days-per-session", type=float, default=3.0,
        help="training-only frequency ceiling for unique candidate tickers per session",
    )
    parser.add_argument("--capital", type=float, default=100_000)
    parser.add_argument("--slippage-bps", type=float, default=5)
    parser.add_argument("--spread-bps", type=float, default=3, help="assumed full quoted spread; half is charged on each side")
    parser.add_argument("--commission-per-share", type=float, default=0.005)
    parser.add_argument("--short-borrow-annual-pct", type=float, default=3.0)
    parser.add_argument("--unknown-borrow-penalty-bps", type=float, default=25)
    parser.add_argument("--borrow-availability", help="CSV with date,symbol,easy_to_borrow columns")
    parser.add_argument("--min-test-trades-per-fold", type=int, default=3)
    parser.add_argument(
        "--workers", type=int, default=min(6, os.cpu_count() or 1),
        help="independent fold worker processes (default: up to 6)",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_walk_forward(args)
    except (ValueError, OSError, requests.RequestException) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True))
        report["report_file"] = str(output.resolve())
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
