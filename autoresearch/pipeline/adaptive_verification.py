"""Chronological rolling-fit verification and stitched out-of-sample scoring."""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import math
import multiprocessing as mp
from pathlib import Path
import statistics

from autoresearch.backtest import MIN_REGIME_DAILY_BARS, strategy_warmup_days
from autoresearch.backtest.execution import build_replay_timeline
from market.store import SQLiteBarStore
from autoresearch.cpu import available_cpu_count
from strategies.multi_factor.features import aggregate_intraday_bars
from strategies.registry import ALGO_DEFAULT_PARAMETERS
from market.data import data_tier

from .adaptive_common import _prepare_args, _write_report
from ..artifacts.artifact_cache import stable_hash
from .adaptive_search import _daily_returns, _distance, _fitness, _fit_window_ladder, _flatten, _load_window_data, _metrics, _profile_for, _run_replay, _trading_sessions
from ..validation.robustness import (
    bootstrap_daily_equity, choose_plateau_candidate, summarize_fold_consistency,
    walk_forward_efficiency,
)
from ..settings import (
    FEED, MULTI_FACTOR_MIN_UNIVERSE_VALUES,
    ROBUSTNESS_BOOTSTRAP_BLOCK_SESSIONS, ROBUSTNESS_BOOTSTRAP_ITERATIONS,
    STRATEGY_FLAVORS,
)

def walk_forward_windows(
    trading_dates: list[date], *, train_days: int, test_days: int, step_days: int,
    purge_sessions: int = 0,
) -> list[dict[str, str | int]]:
    """Build rolling train/test windows separated by a forward-outcome purge."""
    if min(train_days, test_days, step_days) < 1 or purge_sessions < 0:
        raise ValueError("train_days, test_days, and step_days must be positive")
    if step_days < test_days:
        raise ValueError("step_days must be at least test_days so verification folds do not overlap")
    dates = sorted(set(trading_dates))
    windows = []
    test_index = train_days + purge_sessions
    while test_index < len(dates):
        train_end_index = test_index - purge_sessions
        train = dates[max(0, train_end_index - train_days):train_end_index]
        test = dates[test_index:test_index + test_days]
        if not train or not test:
            break
        purged = dates[train_end_index:test_index]
        windows.append({
            "train_start": train[0].isoformat(), "train_end": train[-1].isoformat(),
            "test_start": test[0].isoformat(), "test_end": test[-1].isoformat(),
            "train_session_count": len(train), "purge_session_count": len(purged),
            "purge_start": purged[0].isoformat() if purged else None,
            "purge_end": purged[-1].isoformat() if purged else None,
            "test_session_count": len(test),
        })
        test_index += step_days
    return windows

def _run_adaptive_fold(payload: tuple[int, dict, argparse.Namespace]) -> tuple[int, dict]:
    index, window, args = payload
    args.regime_cache = {}
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
    plateau_candidate, plateau = choose_plateau_candidate(
        discovery.get("leaderboard") or [], args.search_space_data,
    )
    selected = (
        plateau_candidate
        if fitted.get("adaptive_improvement") and plateau_candidate.get("eligible")
        else fitted["selected_candidate"]
    )
    baseline = next(
        (row for row in discovery.get("candidates", []) if row.get("name") == "baseline"),
        None,
    )
    adaptive_improvement = bool(
        plateau.get("supported") and baseline is not None
        and selected.get("name") != "baseline"
        and selected.get("eligible")
        and float(selected.get("fitness") or 0.0) > float(baseline.get("fitness") or 0.0)
    )
    selected_parameters = selected["parameters"]
    capital_before = float(args.capital)
    test_timeline = build_replay_timeline(
        args.symbols_resolved, bars, test_start, test_end,
    )
    test_report = _run_replay(
        strategy=args.strategy, parameters=selected_parameters,
        symbols=args.symbols_resolved, bars=bars, spy_daily=spy,
        start=test_start, end=test_end, args=args, capital=capital_before,
        preaggregated_daily_bars=preaggregated_daily_bars,
        precomputed_timeline=test_timeline, regime_cache=args.regime_cache,
    )
    train_metrics = selected["metrics"]
    oos_metrics = _metrics(test_report, capital=capital_before)
    oos_fitness, oos_components = _fitness(oos_metrics, args.fitness_weights_data)
    execution_sensitivity = {}
    multipliers = list(getattr(args, "cost_sensitivity_multipliers", []) or [])
    delay_multiplier = max(multipliers, default=1.5)
    scenarios = [(multiplier, 0) for multiplier in multipliers]
    if multipliers:
        scenarios.append((delay_multiplier, 1))
    for multiplier, delay_bars in scenarios:
        stress_args = deepcopy(args)
        stress_args.slippage_bps = float(args.slippage_bps) * multiplier
        stress_args.spread_bps = float(args.spread_bps) * multiplier
        stress_args.commission_per_share = float(args.commission_per_share) * multiplier
        stress_args.short_borrow_annual_pct = float(args.short_borrow_annual_pct) * multiplier
        stress_args.unknown_borrow_penalty_bps = float(args.unknown_borrow_penalty_bps) * multiplier
        stress_args.execution_delay_bars = delay_bars
        replay = _run_replay(
            strategy=args.strategy, parameters=selected_parameters,
            symbols=args.symbols_resolved, bars=bars, spy_daily=spy,
            start=test_start, end=test_end, args=stress_args,
            capital=capital_before, preaggregated_daily_bars=preaggregated_daily_bars,
            precomputed_timeline=test_timeline, regime_cache=args.regime_cache,
        )
        key = f"cost_{multiplier:g}x" + (f"_delay_{delay_bars}_bars" if delay_bars else "")
        execution_sensitivity[key] = {
            "cost_multiplier": multiplier,
            "execution_delay_bars": delay_bars,
            "metrics": _metrics(replay, capital=capital_before),
        }
        del replay
    min_universe_sensitivity = None
    if args.strategy == "swing_multi_factor":
        min_universe_sensitivity = {}
        for min_universe in MULTI_FACTOR_MIN_UNIVERSE_VALUES:
            parameters = deepcopy(ALGO_DEFAULT_PARAMETERS["swing_multi_factor"])
            parameters["min_universe"] = min_universe
            sensitivity_report = _run_replay(
                strategy=args.strategy, parameters=parameters,
                symbols=args.symbols_resolved, bars=bars, spy_daily=spy,
                start=test_start, end=test_end, args=args, capital=capital_before,
                preaggregated_daily_bars=preaggregated_daily_bars,
                precomputed_timeline=test_timeline, regime_cache=args.regime_cache,
            )
            min_universe_sensitivity[str(min_universe)] = {
                "oos_metrics": _metrics(sensitivity_report, capital=capital_before),
                "oos_trade_pnls": [
                    float(trade.get("pnl", 0) or 0)
                    for trade in sensitivity_report.get("closed_trades", [])
                ],
                "oos_daily_equity": sensitivity_report.get("daily_equity", []),
            }
            del sensitivity_report
    result = {
        "fold": index + 1, **window,
        "selected_candidate": selected["name"],
        "selection_status": fitted["selection_status"],
        "adaptive_improvement": adaptive_improvement,
        "training_parameter_plateau": plateau,
        "fit_window_days": fitted["fit_window_days"],
        "fit_window_start": fitted["fit_window_start"],
        "fit_window_end": fitted["fit_window_end"],
        "window_attempts": fitted["window_attempts"],
        "train_parameters": selected_parameters,
        "train_controls": _profile_for(args.strategy, selected_parameters)["controls"],
        "train_fitness": selected["fitness"], "train_metrics": train_metrics,
        "oos_fitness": round(oos_fitness, 8),
        "walk_forward_efficiency": walk_forward_efficiency(
            float(selected.get("fitness") or 0.0), oos_fitness,
        ),
        "oos_fitness_components": {key: round(value, 8) for key, value in oos_components.items()},
        "oos_metrics": oos_metrics,
        "oos_trade_pnls": [float(trade.get("pnl", 0) or 0) for trade in test_report.get("closed_trades", [])],
        "fitness_decay": round(float(selected["fitness"]) - oos_fitness, 8),
        "execution_sensitivity": execution_sensitivity,
        "min_universe_sensitivity": min_universe_sensitivity,
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
    warmup_days = strategy_warmup_days(args.strategy)
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
    if getattr(args, "purge_sessions", 0) < 0:
        raise ValueError("purge-sessions must be a non-negative session count")
    if not 0 <= float(getattr(args, "minimum_positive_fold_fraction", 0.60)) <= 1:
        raise ValueError("minimum-positive-fold-fraction must be in [0, 1]")
    if len(sessions) < args.train_days + args.test_days:
        raise ValueError("research range does not contain enough sessions for a train and test window")
    windows = walk_forward_windows(
        sessions, train_days=args.train_days,
        test_days=args.test_days, step_days=args.step_days,
        purge_sessions=int(getattr(args, "purge_sessions", 0)),
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
        # This executor can be created inside a strategy worker. Spawn clean
        # interpreters so fold workers do not inherit the outer pool's queues.
        with ProcessPoolExecutor(
            max_workers=workers, mp_context=mp.get_context("spawn"),
        ) as pool:
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
    fold_consistency = summarize_fold_consistency(
        folds,
        minimum_positive_fraction=float(getattr(args, "minimum_positive_fold_fraction", 0.60)),
    )
    bootstrap = bootstrap_daily_equity(
        curve,
        iterations=int(getattr(args, "bootstrap_iterations", ROBUSTNESS_BOOTSTRAP_ITERATIONS)),
        block_size=ROBUSTNESS_BOOTSTRAP_BLOCK_SESSIONS,
        seed=args.seed,
    )
    min_universe_sensitivity = None
    if args.strategy == "swing_multi_factor":
        min_universe_sensitivity = {
            "method": "paired rolling OOS replay using current default Multi Factor parameters; only min_universe varies",
            "current_setting": ALGO_DEFAULT_PARAMETERS["swing_multi_factor"]["min_universe"],
            "thresholds": {},
        }
        sensitivity_summaries = {}
        for min_universe in MULTI_FACTOR_MIN_UNIVERSE_VALUES:
            sensitivity_folds = []
            for fold in folds:
                row = (fold.get("min_universe_sensitivity") or {}).get(str(min_universe))
                if row is None:
                    continue
                sensitivity_folds.append({
                    "fold": fold["fold"],
                    "oos_daily_equity": row.get("oos_daily_equity", []),
                    "oos_trade_pnls": row.get("oos_trade_pnls", []),
                })
            _, sensitivity_metrics = _stitch(sensitivity_folds, args.capital)
            sensitivity_summaries[min_universe] = sensitivity_metrics
        current_metrics = sensitivity_summaries.get(
            min_universe_sensitivity["current_setting"], {},
        )
        for min_universe, sensitivity_metrics in sensitivity_summaries.items():
            baseline_return = float(current_metrics.get("net_return_pct") or 0.0)
            baseline_drawdown = float(current_metrics.get("max_drawdown_pct") or 0.0)
            min_universe_sensitivity["thresholds"][str(min_universe)] = {
                **sensitivity_metrics,
                "delta_net_return_percentage_points_vs_current": round(
                    float(sensitivity_metrics.get("net_return_pct") or 0.0) - baseline_return, 4,
                ),
                "delta_max_drawdown_percentage_points_vs_current": round(
                    float(sensitivity_metrics.get("max_drawdown_pct") or 0.0) - baseline_drawdown, 4,
                ),
            }
    decay_values = [float(row["fitness_decay"]) for row in folds]
    average_decay = statistics.mean(decay_values) if decay_values else 0.0
    wfe_values = [
        float(row["walk_forward_efficiency"])
        for row in folds if row.get("walk_forward_efficiency") is not None
    ]
    wfe_positive_count = sum(value > 0 for value in wfe_values)
    wfe_positive_fraction = wfe_positive_count / len(folds) if folds else 0.0
    wfe_summary = {
        "defined_fold_count": len(wfe_values),
        "positive_fold_count": wfe_positive_count,
        "positive_fold_fraction_of_all_folds": round(wfe_positive_fraction, 6),
        "mean": round(statistics.mean(wfe_values), 6) if wfe_values else None,
        "median": round(statistics.median(wfe_values), 6) if wfe_values else None,
        "minimum_positive_fraction": float(getattr(args, "minimum_positive_fold_fraction", 0.60)),
        "consistency_gate_met": bool(
            folds and wfe_positive_fraction >= float(
                getattr(args, "minimum_positive_fold_fraction", 0.60)
            )
        ),
    }
    stability = _parameter_stability(folds, set(args.search_space_data))
    sharpe_pass = oos["daily_sharpe_annualized"] is not None and oos["daily_sharpe_annualized"] >= args.minimum_oos_sharpe
    decay_pass = average_decay <= args.maximum_average_decay
    fold_pass = len(folds) >= args.minimum_folds
    improvement_folds = sum(bool(row.get("adaptive_improvement")) for row in folds)
    improvement_pass = improvement_folds > 0
    plateau_pass = all(
        not row.get("adaptive_improvement")
        or (row.get("training_parameter_plateau") or {}).get("supported")
        for row in folds
    )
    walk_forward_pass = bool(
        sharpe_pass and decay_pass and fold_pass and improvement_pass
        and fold_consistency["consistency_gate_met"] and wfe_summary["consistency_gate_met"]
        and plateau_pass
    )
    overall_pass = walk_forward_pass
    detail = {
        "schema": "swarm_algo_adaptive_walk_forward_detail_v1",
        "stage": "short_window_walk_forward_verification",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "adaptive_policy_version": getattr(args, "adaptive_policy_version", None),
        "factor_weight_timeline_sha256": stable_hash(
            getattr(args, "factor_weight_timeline", None) or []
        ),
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
                          "minimum_folds": args.minimum_folds,
                          "purge_sessions": int(getattr(args, "purge_sessions", 0)),
                          "minimum_positive_fold_fraction": float(getattr(args, "minimum_positive_fold_fraction", 0.60)),
                          "cost_sensitivity_multipliers": list(getattr(args, "cost_sensitivity_multipliers", []) or []),
                          "bootstrap_iterations": int(getattr(args, "bootstrap_iterations", ROBUSTNESS_BOOTSTRAP_ITERATIONS)),
                          "rolling_training_range": {
                              "start": windows[0]["train_start"],
                              "first_oos_start": windows[0]["test_start"],
                              "last_oos_end": windows[-1]["test_end"],
                          }},
        "worker_count": workers,
        "folds": folds,
        "stitched_oos_daily_equity": curve,
        "robustness": {
            "fold_consistency": fold_consistency,
            "walk_forward_efficiency": wfe_summary,
            "block_bootstrap": bootstrap,
            "block_bootstrap_method": f"{ROBUSTNESS_BOOTSTRAP_BLOCK_SESSIONS}-session moving-block resampling of stitched OOS daily returns",
            "cost_sensitivity_scenarios": list(getattr(args, "cost_sensitivity_multipliers", []) or []),
            "latency_scenario": "highest configured cost multiplier plus one additional 5-minute bar before fill",
        },
        "min_universe_sensitivity": min_universe_sensitivity,
        "limitations": [
            "The search uses bounded discrete signal parameters; execution and risk settings are held fixed.",
            "Each fold begins with the stitched equity from the prior OOS segment; training searches use fixed starting capital.",
            "The rolling evaluation begins in 2020 and uses the current fixed universe; this is not a profitability guarantee or live promotion.",
            f"Each rolling fit is separated from its OOS test by a {int(getattr(args, 'purge_sessions', 0))}-session forward-outcome purge; feature lookbacks use prior history and do not require an embargo of the same length.",
            "Historical walk-forward results are reused across research cycles and are not a pristine independent holdout; immutable forward challenger versions are evaluated only after staging.",
        ],
    }
    summary = {
        "schema": "swarm_algo_adaptive_walk_forward_summary_v1",
        "created_at": detail["created_at"], "strategy": args.strategy,
        "adaptive_policy_version": detail["adaptive_policy_version"],
        "detail_artifact": None, "fold_count": len(folds),
        "stitched_oos": oos, "average_fitness_decay": round(average_decay, 8),
        "min_universe_sensitivity": min_universe_sensitivity,
        "median_fitness_decay": round(statistics.median(decay_values), 8) if decay_values else 0.0,
        "parameter_stability": stability,
        "fold_consistency": fold_consistency,
        "walk_forward_efficiency": wfe_summary,
        "block_bootstrap": bootstrap,
        "plateau_supported_folds": sum(
            bool((row.get("training_parameter_plateau") or {}).get("supported"))
            for row in folds
        ),
        "checks": {
            "minimum_fold_count_met": fold_pass,
            "adaptive_improvement_observed": improvement_pass,
            "adaptive_improvement_folds": improvement_folds,
            "stitched_oos_sharpe_threshold_met": sharpe_pass,
            "average_decay_threshold_met": decay_pass,
            "positive_fold_consistency_met": fold_consistency["consistency_gate_met"],
            "walk_forward_efficiency_consistency_met": wfe_summary["consistency_gate_met"],
            "parameter_plateau_gate_met": plateau_pass,
            "walk_forward_pass": walk_forward_pass,
            "overall_pass": overall_pass,
        },
        "thresholds": {"minimum_oos_sharpe": args.minimum_oos_sharpe,
                       "maximum_average_decay": args.maximum_average_decay,
                       "minimum_folds": args.minimum_folds,
                       "minimum_positive_fold_fraction": float(getattr(args, "minimum_positive_fold_fraction", 0.60)),
                       "minimum_positive_wfe_fraction": float(getattr(args, "minimum_positive_fold_fraction", 0.60)),
                       "purge_sessions": int(getattr(args, "purge_sessions", 0))},
    }
    report = _write_report(args.output_dir, args.strategy, "walk-forward", detail, summary=summary)
    return report
