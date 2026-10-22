"""Candidate scoring, parameter search, replay preparation, and fit-window selection."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import logging
import multiprocessing as mp
import random
from concurrent.futures import ProcessPoolExecutor
from zoneinfo import ZoneInfo

from autoresearch.cpu import available_cpu_count
from autoresearch.backtest.execution import build_replay_timeline
from market.store import SQLiteBarStore
from strategies.multi_factor.features import aggregate_intraday_bars
from strategies.registry import PARAMETER_KEYS
from strategies.universe import configured_universe
from ..artifacts.candidate_archive import candidate_id
from ..artifacts.replay_logging import quiet_coordinator_rejections
from ..settings import ADJUSTMENT, FEED, RESEARCH_START, RESEARCH_WARMUP_DAYS

from .adaptive_candidates import (
    _bayes_proposal, _candidate_parameters, _distance, _flatten,
    _make_parameters, _planned_ablation_variants, _random_values, _search_space,
)

from .adaptive_evaluation import (
    _apply_multi_factor_ic_improvement_gate, _candidate_replay_worker_init,
    _evaluate_candidate_in_worker, _evaluate_search_candidate,
    _load_forward_window_data, _load_window_data, _ordered_window_options,
    _profile_for, _run_replay,
)

from .adaptive_scoring import (
    _cost_model_version, _daily_returns, _fitness, _metrics,
    _metrics_by_regime, _normalize_weights,
)

from .adaptive_config import (
    ADAPTIVE_REGIMES, DEFAULT_FITNESS_WEIGHTS, DEFAULT_SEARCH_SPACES,
    MULTI_FACTOR_EQUAL_ABLATION_WEIGHTS, MULTI_FACTOR_MIN_NET_IC_IMPROVEMENT,
)

ET = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)

__all__ = [
    "ADAPTIVE_REGIMES",
    "DEFAULT_FITNESS_WEIGHTS",
    "DEFAULT_SEARCH_SPACES",
    "MULTI_FACTOR_EQUAL_ABLATION_WEIGHTS",
    "MULTI_FACTOR_MIN_NET_IC_IMPROVEMENT",
    "PARAMETER_KEYS",
    "_apply_multi_factor_ic_improvement_gate",
    "_baseline_improvement",
    "_bayes_proposal",
    "_candidate_parameters",
    "_candidate_replay_worker_init",
    "_cost_model_version",
    "_daily_returns",
    "_distance",
    "_evaluate_candidate_in_worker",
    "_evaluate_search_candidate",
    "_fitness",
    "_flatten",
    "_fit_window_ladder",
    "_load_forward_window_data",
    "_load_window_data",
    "_make_parameters",
    "_metrics",
    "_metrics_by_regime",
    "_normalize_weights",
    "_ordered_window_options",
    "_planned_ablation_variants",
    "_profile_for",
    "_random_values",
    "_run_iterative_discovery_search",
    "_run_replay",
    "_run_search_with_bars",
    "_search_space",
    "_symbols",
    "_trading_sessions",
    "quiet_coordinator_rejections",
]


def _baseline_improvement(search: dict) -> dict:
    """Return whether the selected result improves on baseline after costs."""
    candidates = search.get("candidates") or []
    baseline = next((row for row in candidates if row.get("name") == "baseline"), None)
    selected = search.get("selected_candidate") or {}
    baseline_fitness = float((baseline or {}).get("fitness") or 0.0)
    selected_fitness = float(selected.get("fitness") or 0.0)
    improved = bool(
        baseline is not None
        and selected.get("name") != "baseline"
        and selected.get("eligible")
        and selected_fitness > baseline_fitness + 1e-9
    )
    return {
        "improved": improved,
        "baseline_candidate": baseline,
        "baseline_fitness": baseline_fitness,
        "selected_fitness": selected_fitness,
        "fitness_delta": round(selected_fitness - baseline_fitness, 8),
    }


def _run_search_with_bars(
    *, strategy: str, method: str, count: int, seed: int,
    space: dict[str, list], symbols: list[str], bars: dict,
    spy_daily: list[dict], start: date, end: date, args,
    weights: dict[str, float], min_trades: int,
    parent_parameters: dict | list[dict] | None = None,
    progress_label: str | None = None,
    preaggregated_daily_bars: dict[str, list[dict]] | None = None,
    precomputed_timeline: dict[datetime, dict[str, dict]] | None = None,
) -> dict:
    # Candidates in this search share the same input bars and replay window.
    # Prepare their data-derived inputs once instead of rebuilding them per run.
    if preaggregated_daily_bars is None:
        preaggregated_daily_bars = aggregate_intraday_bars(bars)
    if precomputed_timeline is None:
        precomputed_timeline = build_replay_timeline(symbols, bars, start, end)
    worker_count = min(
        max(1, int(getattr(args, "candidate_workers", 1))),
        available_cpu_count(),
    )
    replay_context = {
        "strategy": strategy, "symbols": symbols, "bars": bars,
        "spy_daily": spy_daily, "start": start, "end": end, "args": args,
        "weights": weights, "min_trades": min_trades,
        "preaggregated_daily_bars": preaggregated_daily_bars,
        "precomputed_timeline": precomputed_timeline,
        "regime_cache": getattr(args, "regime_cache", {}),
    }
    candidates = _candidate_parameters(
        strategy, method, count, seed, space, parent_parameters=parent_parameters,
        excluded_candidate_ids=getattr(args, "excluded_candidate_ids", None),
    )
    evaluated: list[dict] = []
    history: list[dict] = []
    seen = {
        json.dumps(row["parameters"], sort_keys=True, separators=(",", ":"))
        for row in candidates
    }
    queue = list(candidates)
    rng = random.Random(seed)
    bayes_attempts = 0
    executor = None
    try:
        while queue or (method == "bayes" and len(evaluated) < count and bayes_attempts < count * 40):
            had_queued_candidates = bool(queue)
            batch = []
            while queue and len(batch) < worker_count:
                batch.append(queue.pop(0))
            # Bayesian proposals use completed results from the previous batch.
            # New proposals within this batch are unique, but do not wait for
            # another candidate's replay, keeping the CPU workers busy.
            while (
                not had_queued_candidates and not queue and method == "bayes"
                and len(evaluated) + len(batch) < count
                and len(batch) < worker_count and bayes_attempts < count * 40
            ):
                bayes_attempts += 1
                values = _bayes_proposal(space, history, seen, rng)
                if not values:
                    break
                try:
                    parameters = _make_parameters(strategy, values)
                except ValueError:
                    continue
                key = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
                excluded = getattr(args, "excluded_candidate_ids", ()) or ()
                if key in seen or candidate_id(strategy, parameters) in excluded:
                    continue
                seen.add(key)
                batch.append({
                    "name": f"bayes_{len(evaluated) + len(batch):03d}",
                    "parameters": parameters,
                })
            if not batch:
                break

            if executor is None and worker_count > 1 and len(batch) > 1:
                executor = ProcessPoolExecutor(
                    max_workers=worker_count,
                    initializer=_candidate_replay_worker_init,
                    initargs=(replay_context,),
                    mp_context=mp.get_context("spawn"),
                )
                logger.info(
                    "Adaptive search %s%s: evaluating candidates in batches of up to %d processes",
                    strategy, f" ({progress_label})" if progress_label else "", worker_count,
                )
            if executor is None:
                rows = [_evaluate_search_candidate(candidate, replay_context) for candidate in batch]
            else:
                rows = list(executor.map(_evaluate_candidate_in_worker, batch))
            for candidate, row in zip(batch, rows):
                raw_fitness = row.pop("_fitness_raw")
                evaluated.append(row)
                history.append({"parameters": candidate["parameters"], "fitness": raw_fitness})
            logger.info(
                "Adaptive search %s%s: completed %d/%d candidates",
                strategy, f" ({progress_label})" if progress_label else "", len(evaluated), count,
            )
    finally:
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
    _apply_multi_factor_ic_improvement_gate(evaluated, strategy)
    eligible_rows = [row for row in evaluated if row["eligible"]]
    sample_eligible = [row for row in evaluated if row["minimum_trade_count_met"]]
    pool = eligible_rows or sample_eligible or evaluated
    selected = max(pool, key=lambda row: (row["fitness"], -float(row["metrics"].get("max_drawdown_pct") or 0)))
    return {
        "strategy": strategy, "method": method, "candidate_count_requested": count,
        "candidate_count_evaluated": len(evaluated), "min_trades_for_selection": min_trades,
        "selection_status": (
            "eligible_positive_candidate" if eligible_rows else
            "no_positive_candidate" if sample_eligible else
            "fallback_low_sample"
        ),
        "fitness_weights": weights, "selected_candidate": selected,
        "leaderboard": sorted(evaluated, key=lambda row: row["fitness"], reverse=True),
        "candidates": evaluated,
    }

def _run_iterative_discovery_search(
    *, strategy: str, method: str, count: int, iterations: int, seed: int,
    space: dict[str, list], symbols: list[str], bars: dict,
    spy_daily: list[dict], start: date, end: date, args,
    weights: dict[str, float], min_trades: int,
    parent_parameters: dict | list[dict] | None = None,
    excluded_candidate_ids: set[str] | list[str] | tuple[str, ...] = (),
    preaggregated_daily_bars: dict[str, list[dict]] | None = None,
    search_func=None,
) -> dict:
    """Refine on training data for bounded rounds; verification remains separate."""
    if iterations < 1:
        raise ValueError("search iterations must be positive")

    candidates = []
    iteration_summaries = []
    incumbent = None
    excluded = set(excluded_candidate_ids)
    for iteration in range(1, iterations + 1):
        active_parent = (
            [incumbent["parameters"], *(parent_parameters if isinstance(parent_parameters, list) else ([parent_parameters] if parent_parameters else []))]
            if incumbent else parent_parameters
        )
        args.excluded_candidate_ids = excluded
        search_runner = search_func or _run_search_with_bars
        search_options = dict(
            strategy=strategy, method=method, count=count,
            seed=seed + (iteration - 1) * 104_729, space=space, symbols=symbols,
            bars=bars, spy_daily=spy_daily, start=start, end=end, args=args,
            weights=weights, min_trades=min_trades,
            parent_parameters=active_parent,
            progress_label=f"iteration {iteration}/{iterations}",
            preaggregated_daily_bars=preaggregated_daily_bars,
        )
        search = search_runner(**search_options)
        selected = search["selected_candidate"]
        comparison = _baseline_improvement(search)
        selected_name = f"iteration_{iteration:02d}:{selected.get('name', 'unknown')}"
        logger.info(
            "Adaptive refinement %s iteration %d/%d: %s fitness %.4f (baseline delta %+.4f)%s",
            strategy, iteration, iterations, selected.get("name"),
            float(selected.get("fitness") or 0.0), comparison["fitness_delta"],
            "; incumbent updated" if comparison["improved"] and (
                incumbent is None or float(selected["fitness"]) > float(incumbent["fitness"])
            ) else "",
        )
        iteration_summaries.append({
            "iteration": iteration,
            "candidate_count_evaluated": search["candidate_count_evaluated"],
            "parent_candidate_used": bool(active_parent),
            "selected_candidate": selected_name,
            "selected_fitness": selected.get("fitness"),
            "baseline_fitness": comparison["baseline_fitness"],
            "fitness_delta_vs_baseline": comparison["fitness_delta"],
            "improved_vs_baseline": comparison["improved"],
        })
        for row in search["candidates"]:
            excluded.add(candidate_id(strategy, row["parameters"]))
            original_name = str(row.get("name", "candidate"))
            candidates.append({
                **row,
                "name": "baseline" if iteration == 1 and original_name == "baseline"
                else f"iteration_{iteration:02d}:{original_name}",
                "search_iteration": iteration,
            })
        if (
            comparison["improved"]
            and (incumbent is None or float(selected["fitness"]) > float(incumbent["fitness"]))
        ):
            incumbent = selected

    eligible_rows = [row for row in candidates if row["eligible"]]
    sample_eligible = [row for row in candidates if row["minimum_trade_count_met"]]
    pool = eligible_rows or sample_eligible or candidates
    selected = max(
        pool,
        key=lambda row: (
            row["fitness"], -float(row["metrics"].get("max_drawdown_pct") or 0),
        ),
    )
    return {
        "strategy": strategy, "method": method,
        "candidate_count_requested": count * iterations,
        "candidate_count_per_iteration": count,
        "search_iterations": iterations,
        "candidate_count_evaluated": len(candidates),
        "min_trades_for_selection": min_trades,
        "selection_status": (
            "eligible_positive_candidate" if eligible_rows else
            "no_positive_candidate" if sample_eligible else
            "fallback_low_sample"
        ),
        "fitness_weights": weights, "selected_candidate": selected,
        "leaderboard": sorted(candidates, key=lambda row: row["fitness"], reverse=True),
        "candidates": candidates,
        "iteration_summaries": iteration_summaries,
    }

def _fit_window_ladder(
    *, strategy: str, method: str, count: int, seed: int, space: dict[str, list],
    symbols: list[str], bars: dict, spy_daily: list[dict], train_sessions: list[date],
    args, weights: dict[str, float], min_trades: int, window_options: list[int],
    preaggregated_daily_bars: dict[str, list[dict]] | None = None,
    search_func=None,
) -> dict:
    """Search one or more trailing train windows, falling back until improvement."""
    if preaggregated_daily_bars is None:
        preaggregated_daily_bars = aggregate_intraday_bars(bars)
    ordered = _ordered_window_options(window_options)
    results = []
    attempts = []
    for window_days in ordered:
        if len(train_sessions) < window_days:
            attempts.append({"window_days": window_days, "skipped": "insufficient_training_sessions"})
            continue
        fit_start = train_sessions[-window_days]
        fit_end = train_sessions[-1]
        search = (search_func or _run_search_with_bars)(
            strategy=strategy, method=method, count=count,
            seed=seed + window_days * 73, space=space, symbols=symbols,
            bars=bars, spy_daily=spy_daily, start=fit_start, end=fit_end,
            args=args, weights=weights, min_trades=min_trades,
            preaggregated_daily_bars=preaggregated_daily_bars,
        )
        comparison = _baseline_improvement(search)
        selected = search["selected_candidate"]
        baseline = comparison.get("baseline_candidate") or selected
        attempt = {
            "window_days": window_days,
            "fit_start": fit_start.isoformat(), "fit_end": fit_end.isoformat(),
            "selection_status": search["selection_status"],
            "selected_candidate": selected.get("name"),
            "selected_fitness": comparison["selected_fitness"],
            "baseline_fitness": comparison["baseline_fitness"],
            "fitness_delta": comparison["fitness_delta"],
            "improved": comparison["improved"],
        }
        attempts.append(attempt)
        results.append({
            "window_days": window_days, "fit_start": fit_start,
            "fit_end": fit_end,
            "search": search, "baseline": baseline, "comparison": comparison,
        })
        # A single configured window preserves the CLI's original behavior.
        if len(ordered) == 1 or comparison["improved"]:
            break

    if not results:
        raise ValueError("adaptive fit has no training window with enough sessions")
    improved = next((result for result in results if result["comparison"]["improved"]), None)
    if improved:
        chosen = improved
        selected = chosen["search"]["selected_candidate"]
    elif len(ordered) == 1:
        chosen = results[0]
        selected = chosen["search"]["selected_candidate"]
    else:
        chosen = max(results, key=lambda result: result["comparison"]["baseline_fitness"])
        selected = chosen["baseline"]

    return {
        "search": chosen["search"], "selected_candidate": selected,
        "fit_window_days": chosen["window_days"],
        "fit_window_start": chosen["fit_start"].isoformat(),
        "fit_window_end": chosen["fit_end"].isoformat(),
        "window_attempts": attempts, "adaptive_improvement": bool(improved),
        "selection_status": (
            chosen["search"]["selection_status"] if improved or len(ordered) == 1
            else "no_adaptive_improvement"
        ),
    }

def _symbols(value: str | None) -> list[str]:
    if value:
        symbols = list(dict.fromkeys(part.strip().upper() for part in value.split(",") if part.strip()))
    else:
        symbols = configured_universe("algo")["tickers"]
    if not symbols:
        raise ValueError("the Algo research universe is empty")
    return symbols

def _trading_sessions(db: str, end: date, start: date | None = None, adjustment: str = ADJUSTMENT) -> list[date]:
    lookup_start = start or (RESEARCH_START - timedelta(days=RESEARCH_WARMUP_DAYS))
    store = SQLiteBarStore(db)
    daily = store.bars(
        ["SPY"], feed=FEED, timeframe="1Day",
        start=lookup_start.isoformat(), end=end.isoformat(),
        adjustment=adjustment,
    ).get("SPY", [])
    result = set()
    for bar in daily:
        raw = str(bar.get("date") or bar.get("t") or "")
        try:
            stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            day = stamp.astimezone(ET).date() if stamp.tzinfo else date.fromisoformat(raw[:10])
        except (TypeError, ValueError):
            try:
                day = date.fromisoformat(raw[:10])
            except ValueError:
                continue
        if day <= end and (start is None or day >= start):
            result.add(day)
    return sorted(result)
