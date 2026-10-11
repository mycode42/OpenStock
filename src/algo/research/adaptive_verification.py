"""Chronological rolling-fit verification and stitched out-of-sample scoring."""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
import math
from pathlib import Path
import statistics

from src.algo.backtest import DAILY_WARMUP_DAYS, MIN_REGIME_DAILY_BARS, multi_factor_warmup_days
from src.algo.market_store import SQLiteBarStore
from src.algo.cpu import available_cpu_count
from src.algo.strategies.multi_factor.features import aggregate_intraday_bars
from src.market_data import data_tier

from .adaptive_common import _prepare_args, _write_report
from .adaptive_search import _daily_returns, _distance, _fitness, _fit_window_ladder, _flatten, _load_window_data, _metrics, _profile_for, _run_replay, _trading_sessions
from .settings import FEED, STRATEGY_FLAVORS

def walk_forward_windows(
    trading_dates: list[date], *, train_days: int, test_days: int, step_days: int,
) -> list[dict[str, str | int]]:
    """Build non-overlapping chronological windows for rolling verification."""
    if min(train_days, test_days, step_days) < 1:
        raise ValueError("train_days, test_days, and step_days must be positive")
    if step_days < test_days:
        raise ValueError("step_days must be at least test_days so verification folds do not overlap")
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

def _run_adaptive_fold(payload: tuple[int, dict, argparse.Namespace]) -> tuple[int, dict]:
    index, window, args = payload
    train_start = date.fromisoformat(window["train_start"])
    train_end = date.fromisoformat(window["train_end"])
    test_start = date.fromisoformat(window["test_start"])
    test_end = date.fromisoformat(window["test_end"])
    bars, spy = _load_window_data(
        db=args.db, symbols=args.symbols_resolved, start=train_start, end=test_end,
        adjustment=args.adjustment, strategy=args.strategy,
    )
    preaggregated_daily_bars = aggregate_intraday_bars(bars)
    seed = args.seed + test_end.toordinal() + index * 1009
    train_sessions = _trading_sessions(args.db, train_end, train_start, args.adjustment)
    fitted = _fit_window_ladder(
        strategy=args.strategy, method=args.search_method, count=args.candidates,
        seed=seed, space=args.search_space_data, symbols=args.symbols_resolved,
        bars=bars, spy_daily=spy, train_sessions=train_sessions, args=args,
        weights=args.fitness_weights_data, min_trades=args.min_trades,
        window_options=args.train_window_options,
        preaggregated_daily_bars=preaggregated_daily_bars,
    )
    discovery = fitted["search"]
    selected = fitted["selected_candidate"]
    selected_parameters = selected["parameters"]
    capital_before = float(args.capital)
    test_report = _run_replay(
        strategy=args.strategy, parameters=selected_parameters,
        symbols=args.symbols_resolved, bars=bars, spy_daily=spy,
        start=test_start, end=test_end, args=args, capital=capital_before,
        preaggregated_daily_bars=preaggregated_daily_bars,
    )
    train_metrics = selected["metrics"]
    oos_metrics = _metrics(test_report, capital=capital_before)
    oos_fitness, oos_components = _fitness(oos_metrics, args.fitness_weights_data)
    result = {
        "fold": index + 1, **window,
        "selected_candidate": selected["name"],
        "selection_status": fitted["selection_status"],
        "adaptive_improvement": fitted["adaptive_improvement"],
        "fit_window_days": fitted["fit_window_days"],
        "fit_window_start": fitted["fit_window_start"],
        "fit_window_end": fitted["fit_window_end"],
        "window_attempts": fitted["window_attempts"],
        "train_parameters": selected_parameters,
        "train_controls": _profile_for(args.strategy, selected_parameters)["controls"],
        "train_fitness": selected["fitness"], "train_metrics": train_metrics,
        "oos_fitness": round(oos_fitness, 8),
        "oos_fitness_components": {key: round(value, 8) for key, value in oos_components.items()},
        "oos_metrics": oos_metrics,
        "oos_trade_pnls": [float(trade.get("pnl", 0) or 0) for trade in test_report.get("closed_trades", [])],
        "fitness_decay": round(float(selected["fitness"]) - oos_fitness, 8),
        "top_training_candidates": [
            {key: row[key] for key in ("name", "parameters", "fitness", "metrics", "eligible")}
            for row in discovery["leaderboard"][:5]
        ],
        "oos_daily_equity": test_report.get("daily_equity", []),
    }
    del bars, spy, test_report, discovery
    return index, result

def _parameter_stability(folds: list[dict], tuned_parameters: set[str]) -> dict:
    distances = []
    changed = {}
    for previous, current in zip(folds, folds[1:]):
        left = _flatten(previous["train_parameters"])
        right = _flatten(current["train_parameters"])
        for key in sorted(
            key for key in (set(left) | set(right))
            if key.split(".", 1)[0] in tuned_parameters
        ):
            a, b = left.get(key), right.get(key)
            if a == b:
                distance = 0.0
            elif isinstance(a, (int, float)) and not isinstance(a, bool) and isinstance(b, (int, float)) and not isinstance(b, bool):
                scale = max(abs(float(a)), abs(float(b)), 1e-12)
                distance = min(1.0, abs(float(a) - float(b)) / scale)
            else:
                distance = 1.0
            distances.append(distance)
            if distance > 0:
                changed[key] = changed.get(key, 0) + 1
    transitions = max(0, len(folds) - 1)
    return {
        "parameter_transition_count": transitions,
        "mean_normalized_parameter_distance": round(statistics.mean(distances), 6) if distances else 0.0,
        "tuned_parameter_keys": sorted(tuned_parameters),
        "changed_parameter_frequency": {
            key: {"changed_transitions": count, "transition_count": transitions,
                  "fraction": round(count / transitions, 6) if transitions else 0.0}
            for key, count in sorted(changed.items())
        },
        "changed_parameter_keys": sorted(changed),
    }

def _stitch(folds: list[dict], initial_capital: float) -> tuple[list[dict], dict]:
    curve = []
    trade_pnls = []
    equity = float(initial_capital)
    previous_date = None
    for fold in folds:
        scale = equity / float(initial_capital)
        for row in fold.get("oos_daily_equity", []):
            day = row.get("date")
            if previous_date is not None and day <= previous_date:
                continue
            scaled_equity = float(row.get("equity", 0) or 0) * scale
            if scaled_equity > 0:
                curve.append({"date": day, "equity": round(scaled_equity, 2), "fold": fold["fold"]})
                equity = scaled_equity
                previous_date = day
        trade_pnls.extend(float(value) * scale for value in fold.get("oos_trade_pnls", []))
    returns = _daily_returns(curve, initial_capital)
    negative = [value for value in returns if value < 0]
    deviation = statistics.pstdev(returns) if len(returns) > 1 else 0.0
    downside = math.sqrt(sum(value * value for value in negative) / len(negative)) if negative else 0.0
    highwater = float(initial_capital)
    max_drawdown = 0.0
    for row in curve:
        highwater = max(highwater, float(row["equity"]))
        if highwater > 0:
            max_drawdown = max(max_drawdown, (highwater - float(row["equity"])) / highwater)
    wins = [pnl for pnl in trade_pnls if pnl > 0]
    losses = [pnl for pnl in trade_pnls if pnl < 0]
    closed = len(trade_pnls)
    summary = {
        "initial_equity": round(initial_capital, 2),
        "final_equity": round(float(curve[-1]["equity"]), 2) if curve else round(initial_capital, 2),
        "net_return_pct": round((float(curve[-1]["equity"]) / initial_capital - 1) * 100, 4)
        if curve else 0.0,
        "max_drawdown_pct": round(max_drawdown * 100, 4),
        "daily_sharpe_annualized": round(math.sqrt(252) * statistics.mean(returns) / deviation, 4)
        if deviation > 0 and returns else None,
        "daily_sortino_annualized": round(math.sqrt(252) * statistics.mean(returns) / downside, 4)
        if downside > 0 and returns else None,
        "win_rate_pct": round(len(wins) / closed * 100, 4) if closed else None,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 4) if losses else ("inf" if wins else None),
        "closed_trade_count": closed,
        "completed_oos_sessions": len(curve),
    }
    return curve, summary

def run_adaptive_walk_forward(args) -> dict:
    """Fit each recent window, verify immediately out of sample, and stitch results."""
    _prepare_args(args)
    raw_window_options = getattr(args, "train_window_options", None)
    window_options = list(raw_window_options or [args.train_days])
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 2 for value in window_options):
        raise ValueError("train-window-options must contain integer session counts of at least two")
    window_options = sorted(set(window_options))
    args.train_window_options = window_options
    args.train_days = max(int(args.train_days), max(window_options))
    if not args.start or not args.end:
        raise ValueError("adaptive-walk-forward requires --start and --end")
    requested_start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    warmup_days = max(
        DAILY_WARMUP_DAYS,
        multi_factor_warmup_days() if args.strategy == "multi_factor" else 0,
    )
    all_sessions = _trading_sessions(
        args.db, end, start=requested_start - timedelta(days=warmup_days),
        adjustment=args.adjustment,
    )
    sessions = [session for session in all_sessions if session >= requested_start]
    if not sessions:
        raise ValueError("date range does not contain completed SPY sessions")
    prior_session_count = sum(session < sessions[0] for session in all_sessions)
    if prior_session_count < MIN_REGIME_DAILY_BARS:
        first_ready_index = MIN_REGIME_DAILY_BARS
        if len(all_sessions) <= first_ready_index:
            raise ValueError("date range has too few SPY sessions to warm up the regime filter")
        first_ready_session = all_sessions[first_ready_index]
        sessions = [session for session in sessions if session >= first_ready_session]
        if not sessions:
            raise ValueError("date range ends before the regime filter has enough warm-up bars")
    start = sessions[0]
    if args.step_days < args.test_days:
        raise ValueError("step-days must be at least test-days so stitched OOS segments do not overlap")
    if (
        args.train_days < 2 or args.test_days < 1 or args.step_days < 1
        or args.workers < 1 or args.minimum_folds < 1
    ):
        raise ValueError("train/test/step days, workers, and minimum folds must be positive")
    if len(sessions) < args.train_days + args.test_days:
        raise ValueError("2024-onward range does not contain enough sessions for a train and test window")
    windows = walk_forward_windows(
        sessions, train_days=args.train_days,
        test_days=args.test_days, step_days=args.step_days,
    )
    if len(windows) < args.minimum_folds:
        raise ValueError(f"need at least {args.minimum_folds} walk-forward folds; found {len(windows)}")
    fold_jobs = [(index, window, args) for index, window in enumerate(windows)]
    workers = min(args.workers, len(fold_jobs), available_cpu_count())
    results_by_index = {}
    if workers == 1:
        for job in fold_jobs:
            index, result = _run_adaptive_fold(job)
            results_by_index[index] = result
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_run_adaptive_fold, job): job[0] for job in fold_jobs}
            for future in as_completed(futures):
                index, result = future.result()
                results_by_index[index] = result
    folds = [results_by_index[index] for index in range(len(windows))]
    for index, fold in enumerate(folds):
        fold["parameter_change_from_previous_fold"] = None if index == 0 else {
            "normalized_distance": round(_distance(
                folds[index - 1]["train_parameters"], fold["train_parameters"],
                set(args.search_space_data),
            ), 6),
            "changed": _distance(
                folds[index - 1]["train_parameters"], fold["train_parameters"],
                set(args.search_space_data),
            ) > 0,
        }
    curve, oos = _stitch(folds, args.capital)
    decay_values = [float(row["fitness_decay"]) for row in folds]
    average_decay = statistics.mean(decay_values) if decay_values else 0.0
    stability = _parameter_stability(folds, set(args.search_space_data))
    sharpe_pass = oos["daily_sharpe_annualized"] is not None and oos["daily_sharpe_annualized"] >= args.minimum_oos_sharpe
    decay_pass = average_decay <= args.maximum_average_decay
    fold_pass = len(folds) >= args.minimum_folds
    improvement_folds = sum(bool(row.get("adaptive_improvement")) for row in folds)
    improvement_pass = improvement_folds > 0
    walk_forward_pass = bool(sharpe_pass and decay_pass and fold_pass and improvement_pass)
    overall_pass = walk_forward_pass
    detail = {
        "schema": "swarm_algo_adaptive_walk_forward_detail_v1",
        "stage": "short_window_walk_forward_verification",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "adaptive_policy_version": getattr(args, "adaptive_policy_version", None),
        "research_only": True, "live_configuration_changed": False,
        "trade_or_order_actions": 0, "strategy": args.strategy,
        "strategy_flavor": STRATEGY_FLAVORS.get(args.strategy),
        "search": {"method": args.search_method, "candidates_per_fold": args.candidates,
                   "search_space": args.search_space_data, "fitness_weights": args.fitness_weights_data,
                   "minimum_train_trades": args.min_trades},
        "data": {
            "requested_start": requested_start.isoformat(),
            "start": start.isoformat(), "end": end.isoformat(),
            "symbols": args.symbols_resolved,
            "feed": FEED, "adjustment": args.adjustment, "data_tier": data_tier(),
            "database": str(Path(args.db).resolve()),
            "data_version": getattr(args, "data_version", None),
        },
        "window_config": {"train_days": args.train_days, "test_days": args.test_days,
                          "train_window_options": window_options,
                          "step_days": args.step_days,
                          "rolling_training_range": {
                              "start": windows[0]["train_start"],
                              "first_oos_start": windows[0]["test_start"],
                              "last_oos_end": windows[-1]["test_end"],
                          }},
        "worker_count": workers,
        "folds": folds,
        "stitched_oos_daily_equity": curve,
        "limitations": [
            "The search uses bounded discrete signal parameters; execution and risk settings are held fixed.",
            "Each fold begins with the stitched equity from the prior OOS segment; training searches use fixed starting capital.",
            "The rolling evaluation begins in 2024 and uses the current fixed universe; this is not a profitability guarantee or live promotion.",
        ],
    }
    summary = {
        "schema": "swarm_algo_adaptive_walk_forward_summary_v1",
        "created_at": detail["created_at"], "strategy": args.strategy,
        "adaptive_policy_version": detail["adaptive_policy_version"],
        "detail_artifact": None, "fold_count": len(folds),
        "stitched_oos": oos, "average_fitness_decay": round(average_decay, 8),
        "median_fitness_decay": round(statistics.median(decay_values), 8) if decay_values else 0.0,
        "parameter_stability": stability,
        "checks": {
            "minimum_fold_count_met": fold_pass,
            "adaptive_improvement_observed": improvement_pass,
            "adaptive_improvement_folds": improvement_folds,
            "stitched_oos_sharpe_threshold_met": sharpe_pass,
            "average_decay_threshold_met": decay_pass,
            "walk_forward_pass": walk_forward_pass,
            "overall_pass": overall_pass,
        },
        "thresholds": {"minimum_oos_sharpe": args.minimum_oos_sharpe,
                       "maximum_average_decay": args.maximum_average_decay,
                       "minimum_folds": args.minimum_folds},
    }
    report = _write_report(args.output_dir, args.strategy, "walk-forward", detail, summary=summary)
    return report
