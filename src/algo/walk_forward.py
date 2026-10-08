"""Chronological research-only profile selection and out-of-sample replay."""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
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
    AlgoPortfolioBacktest, DAILY_WARMUP_DAYS, multi_factor_warmup_days,
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
    "open_position_count",
    "entry_count", "average_entries_per_session", "median_entries_per_session",
    "max_entries_per_session", "entry_count_by_strategy", "signal_event_count_by_strategy",
    "candidate_setup_count", "candidate_ticker_day_count",
    "candidate_setup_count_by_strategy", "average_candidate_ticker_days_per_session",
    "strategy_metrics", "average_holding_minutes", "median_holding_minutes",
    "average_calendar_holding_hours", "max_consecutive_losses", "time_in_market_pct",
    "multi_factor_diagnostics", "event_strategy_diagnostics", "satellite_confirmation_candidates",
    "satellite_confirmed_entries",
)


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
    profile: dict, strategies: list[str] | None, controls: dict,
    event_calendar: EventCalendar | None = None,
) -> dict:
    return AlgoPortfolioBacktest(
        symbols=symbols, intraday_bars=intraday, spy_daily_bars=spy_daily,
        start=start, end=end, feed=feed, data_adjustment=adjustment,
        initial_capital=capital, slippage_bps=slippage_bps, profile=profile,
        strategies=strategies, research_controls=controls, event_calendar=event_calendar,
    ).run()


def _run_fold_job(payload: tuple[int, dict, list[str], list[dict], list[dict], argparse.Namespace]) -> tuple[int, dict]:
    """Evaluate one independent fold in a worker process."""
    index, window, symbols, profiles, spy_daily, args = payload
    store = SQLiteBarStore(args.db)
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
            slippage_bps=args.slippage_bps, profile=candidate["parameters"],
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
        slippage_bps=args.slippage_bps, profile=selected["parameters"],
        strategies=selected["strategies"], controls=selected["controls"],
        event_calendar=event_calendar,
    )
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
    trading_dates = sorted({
        datetime.fromisoformat(str(bar["t"]).replace("Z", "+00:00")).astimezone(ET).date()
        for bar in spy_daily
    })
    trading_dates = [session for session in trading_dates if start <= session <= end]
    windows = walk_forward_windows(
        trading_dates, train_days=args.train_days,
        test_days=args.test_days, step_days=args.step_days,
    )
    if not windows:
        raise ValueError(
            f"Need more sessions than the {args.train_days}-session training window; "
            f"found {len(trading_dates)}"
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

    test_returns = [fold["out_of_sample"]["net_return_pct"] for fold in folds]
    return {
        "simulator": "swarm_algo_walk_forward_v2",
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
        "trading_session_count": len(trading_dates),
        "candidate_profile_count": len(profiles),
        "event_calendar": {
            "path": str(Path(args.event_calendar).resolve()) if getattr(args, "event_calendar", None) else None,
            **(event_calendar.diagnostics() if event_calendar else {"loaded_events": 0}),
            "event_enabled_profiles": event_profiles,
        },
        "selection_rule": (
            "Eligible profiles meet minimum training trade count, maximum training drawdown, and "
            f"average candidate ticker-days <= {frequency_limit} per session, plus at least "
            f"{min_satellite_samples} confirmed entries for each enabled satellite. Select the eligible "
            "profile with the highest training net_return_pct - 0.5 * max_drawdown_pct. If none pass all "
            "gates, select the best-scoring sample- and frequency-compliant profile; otherwise fall back to baseline."
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
            "mean_out_of_sample_return_pct": round(statistics.mean(test_returns), 4),
            "median_out_of_sample_return_pct": round(statistics.median(test_returns), 4),
        },
        "folds": folds,
        "limitations": [
            "Profiles are selected from a supplied, bounded candidate list; this does not generate new parameters.",
            "Each fold is replayed as a flat portfolio with fresh capital; positions and risk state do not carry across fold boundaries.",
            "Universe membership is fixed for all folds; historical point-in-time selections are not available.",
            "Supplied symbols without bars in the requested period are recorded and contribute no signals; later-listed symbols can only enter from their available history.",
            "LLM advice and overnight sessions are omitted; replay uses Plus SIP regular-session bars.",
            "This report cannot update live settings and is not a performance forecast.",
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
