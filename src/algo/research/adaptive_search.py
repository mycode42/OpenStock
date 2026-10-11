"""Candidate scoring, parameter search, replay preparation, and fit-window selection."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
import itertools
import json
import logging
import math
from pathlib import Path
import random
import statistics
from concurrent.futures import ProcessPoolExecutor
from zoneinfo import ZoneInfo

from src.algo.backtest import DAILY_WARMUP_DAYS, AlgoPortfolioBacktest, multi_factor_warmup_days
from src.algo.market_store import SQLiteBarStore
from src.algo.cpu import available_cpu_count
from src.algo.backtest.execution import build_replay_timeline
from src.algo.strategies.multi_factor.features import aggregate_intraday_bars
from src.algo.strategies.registry import ALGO_DEFAULT_PARAMETERS, PARAMETER_KEYS, validate_parameter_profile
from src.algo.strategies.universe import configured_universe
from .replay_logging import quiet_coordinator_rejections
from .settings import ADJUSTMENT, CORE_RESEARCH_CONTROLS, FEED, RESEARCH_START, RESEARCH_WARMUP_DAYS

ET = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)
_CANDIDATE_REPLAY_CONTEXT: dict | None = None

DEFAULT_FITNESS_WEIGHTS = {
    "sharpe": 0.25,
    "sortino": 0.15,
    "return": 0.25,
    "win_rate": 0.10,
    "profit_factor": 0.15,
    "drawdown": 0.10,
}

DEFAULT_SEARCH_SPACES: dict[str, dict[str, list]] = {
    "mean_reversion": {
        "signal_model": ["opening_dip", "bollinger"],
        "minimum_dip_pct": [0.010, 0.014, 0.020],
        "minimum_bounce_pct": [0.001, 0.002, 0.003],
        "minimum_volume_ratio": [1.0, 1.3, 1.7],
        "entry_window_minutes": [30, 60, 90],
        "window": [15, 20, 30], "band_width": [1.8, 2.0, 2.4],
        "entry_rsi": [25, 30, 35], "exit_rsi": [55, 58, 62],
    },
    "trend_following": {
        "average_type": ["sma", "ema"], "fast_period": [10, 15, 20, 25, 30],
        "slow_period": [40, 50, 60, 70, 80], "minimum_slope_pct": [0.0, 0.001, 0.0025, 0.005],
    },
    "momentum": {
        "lookbacks": [[5, 10, 20], [5, 10, 20, 40], [10, 20, 40], [10, 20, 60]],
        "top_n": [2, 3, 4], "minimum_score_spread": [0.02, 0.04, 0.06, 0.08],
    },
    "swing_mean_reversion": {
        "lookback_days": [10, 15, 20, 25], "stretch_pct": [0.05, 0.07, 0.08, 0.10, 0.12],
        "minimum_stretch_days": [2, 3, 4], "use_rsi_confirmation": [True, False],
        "long_rsi_max": [25, 30, 35], "short_rsi_min": [65, 70, 75],
    },
    "multi_factor": {
        "rebalance_frequency": ["daily", "weekly"], "top_n": [1, 2, 3, 4],
        "minimum_score": [0.0, 0.05, 0.1, 0.2],
        "direction_mode": ["long_only", "regime_dependent", "long_short"],
        "factor_weights": [
            {"momentum_1m": 0.30, "momentum_3m": 0.30, "momentum_6m": 0.20, "low_volatility": 0.20},
            {"momentum_1m": 0.20, "momentum_3m": 0.30, "momentum_6m": 0.30, "low_volatility": 0.20},
            {"momentum_1m": 0.10, "momentum_3m": 0.25, "momentum_6m": 0.45, "low_volatility": 0.20},
            {"momentum_1m": 0.0, "momentum_3m": 0.0, "momentum_6m": 1.0},
            {"momentum_1m": 0.0, "momentum_3m": 0.50, "momentum_6m": 0.30, "low_volatility": 0.20},
        ],
    },
}

def _metrics(report: dict, *, capital: float) -> dict:
    curve = report.get("daily_equity") or []
    returns = _daily_returns(curve, capital)
    downside = [value for value in returns if value < 0]
    downside_deviation = math.sqrt(sum(value * value for value in downside) / len(downside)) if downside else 0.0
    mean_return = statistics.mean(returns) if returns else 0.0
    sortino = math.sqrt(252) * mean_return / downside_deviation if downside_deviation > 0 else None
    raw = {
        key: report.get(key) for key in (
            "net_return_pct", "max_drawdown_pct", "closed_trade_count", "entry_count",
            "win_rate_pct", "profit_factor", "daily_sharpe_annualized", "final_equity",
            "excess_return_vs_buy_hold_pct_points", "open_position_count",
        )
    }
    raw["sortino_annualized"] = round(sortino, 4) if sortino is not None else None
    return raw

def _daily_returns(curve: list[dict], initial_equity: float) -> list[float]:
    returns = []
    previous = float(initial_equity)
    for row in curve:
        current = float(row.get("equity", 0) or 0)
        if previous > 0 and current > 0:
            returns.append(current / previous - 1)
        previous = current
    return returns

def _fitness(metrics: dict, weights: dict[str, float]) -> tuple[float, dict[str, float]]:
    sharpe = metrics.get("daily_sharpe_annualized")
    sortino = metrics.get("sortino_annualized")
    pf = metrics.get("profit_factor")
    try:
        pf_value = float(pf)
    except (TypeError, ValueError):
        pf_value = 0.0
    if not math.isfinite(pf_value):
        pf_component = 1.0 if pf_value > 0 else -1.0
    elif pf_value <= 0:
        pf_component = -1.0
    else:
        pf_component = math.tanh(math.log(pf_value))
    components = {
        "sharpe": max(-1.0, min(1.0, float(sharpe or 0) / 5.0)),
        "sortino": max(-1.0, min(1.0, float(sortino or 0) / 5.0)),
        "return": math.tanh(float(metrics.get("net_return_pct") or 0) / 10.0),
        "win_rate": (float(metrics["win_rate_pct"]) / 100.0 - 0.5) * 2.0
        if metrics.get("win_rate_pct") is not None else 0.0,
        "profit_factor": pf_component,
        "drawdown": -min(1.0, max(0.0, float(metrics.get("max_drawdown_pct") or 0) / 100.0)),
    }
    return sum(weights[name] * components[name] for name in weights), components

def _normalize_weights(value: str | dict | None) -> dict[str, float]:
    if value is None:
        weights = dict(DEFAULT_FITNESS_WEIGHTS)
    elif isinstance(value, dict):
        weights = dict(value)
    else:
        raw = value
        if raw.lstrip().startswith("{"):
            weights = json.loads(raw)
        else:
            candidate = Path(raw)
            if candidate.is_file():
                weights = json.loads(candidate.read_text())
            else:
                weights = json.loads(raw)
    if not isinstance(weights, dict) or set(weights) - set(DEFAULT_FITNESS_WEIGHTS):
        raise ValueError("fitness weights must be a JSON object using sharpe, sortino, return, win_rate, profit_factor, and drawdown")
    result = {key: float(weights.get(key, 0.0)) for key in DEFAULT_FITNESS_WEIGHTS}
    if any(not math.isfinite(item) or item < 0 for item in result.values()) or sum(result.values()) <= 0:
        raise ValueError("fitness weights must be finite non-negative values with a positive total")
    total = sum(result.values())
    return {key: item / total for key, item in result.items()}

def _search_space(strategy: str, path: str | None) -> dict[str, list]:
    space = deepcopy(DEFAULT_SEARCH_SPACES[strategy])
    if path:
        supplied = json.loads(Path(path).read_text())
        if not isinstance(supplied, dict):
            raise ValueError("search-space JSON must map parameter names to non-empty value arrays")
        space.update(supplied)
    if not space or any(not isinstance(values, list) or not values for values in space.values()):
        raise ValueError("each search-space parameter must have a non-empty list of candidate values")
    # Searchable strategy parameters are broader than the fields explicitly
    # overridden in ALGO_DEFAULT_PARAMETERS (notably the intraday signal knobs).
    allowed = set(PARAMETER_KEYS[strategy])
    unknown = set(space) - allowed
    if unknown:
        raise ValueError(f"search space includes unsupported {strategy} parameters: {', '.join(sorted(unknown))}")
    return space

def _make_parameters(strategy: str, values: dict) -> dict:
    parameters = deepcopy(ALGO_DEFAULT_PARAMETERS[strategy])
    parameters.update(deepcopy(values))
    return validate_parameter_profile({strategy: parameters})[strategy]

def _random_values(space: dict[str, list], rng: random.Random) -> dict:
    return {key: deepcopy(rng.choice(values)) for key, values in space.items()}

def _flatten(value, prefix="") -> dict[str, object]:
    if isinstance(value, dict):
        output = {}
        for key, child in value.items():
            output.update(_flatten(child, f"{prefix}.{key}" if prefix else str(key)))
        return output
    if isinstance(value, (list, tuple)):
        return {prefix: list(value)}
    return {prefix: value}

def _distance(left: dict, right: dict, tuned_parameters: set[str] | None = None) -> float:
    first, second = _flatten(left), _flatten(right)
    keys = sorted(set(first) | set(second))
    if tuned_parameters is not None:
        keys = [key for key in keys if key.split(".", 1)[0] in tuned_parameters]
    if not keys:
        return 0.0
    distances = []
    for key in keys:
        a, b = first.get(key), second.get(key)
        if a == b:
            distances.append(0.0)
        elif isinstance(a, (int, float)) and not isinstance(a, bool) and isinstance(b, (int, float)) and not isinstance(b, bool):
            scale = max(abs(float(a)), abs(float(b)), 1e-12)
            distances.append(min(1.0, abs(float(a) - float(b)) / scale))
        else:
            distances.append(1.0)
    return statistics.mean(distances)

def _bayes_proposal(space: dict[str, list], history: list[dict], seen: set[str], rng: random.Random) -> dict:
    pool = []
    pool_keys = set()
    for _ in range(160):
        proposal = _random_values(space, rng)
        key = json.dumps(proposal, sort_keys=True, separators=(",", ":"))
        if key not in seen and key not in pool_keys:
            pool.append(proposal)
            pool_keys.add(key)
    if not pool:
        return {}
    if len(history) < 4:
        return pool[0]

    def acquisition(candidate: dict) -> float:
        weights = [math.exp(-6.0 * _distance(candidate, row["parameters"], set(space))) for row in history]
        total = sum(weights)
        predicted = sum(weight * row["fitness"] for weight, row in zip(weights, history)) / total if total else 0.0
        uncertainty = 1.0 / math.sqrt(max(total, 1e-9))
        return predicted + 0.15 * uncertainty

    return max(pool, key=acquisition)

def _candidate_parameters(
    strategy: str, method: str, count: int, seed: int, space: dict[str, list],
    parent_parameters: dict | None = None,
) -> list[dict]:
    """Produce a bounded parameter bank for grid and random searches."""
    if count < 1 or count > 512:
        raise ValueError("candidate count must be between 1 and 512")
    baseline = deepcopy(ALGO_DEFAULT_PARAMETERS[strategy])
    output = [{"name": "baseline", "parameters": validate_parameter_profile({strategy: baseline})[strategy]}]
    seen = {json.dumps(baseline, sort_keys=True, separators=(",", ":"))}
    rng = random.Random(seed)
    if parent_parameters is not None and len(output) < count:
        try:
            parent = validate_parameter_profile({strategy: parent_parameters})[strategy]
        except ValueError:
            parent = None
        if parent is not None:
            key = json.dumps(parent, sort_keys=True, separators=(",", ":"))
            if key not in seen:
                seen.add(key)
                output.append({"name": "previous_positive", "parameters": parent})
            # Reserve part of today's budget for one-parameter derivatives of
            # yesterday's positive candidate; the remaining budget explores.
            derivative_limit = min(count, max(len(output), count // 2))
            for parameter, values in space.items():
                for value in values:
                    if len(output) >= derivative_limit:
                        break
                    if parent.get(parameter) == value:
                        continue
                    variation = deepcopy(parent)
                    variation[parameter] = deepcopy(value)
                    try:
                        parameters = _make_parameters(strategy, variation)
                    except ValueError:
                        continue
                    key = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
                    if key in seen:
                        continue
                    seen.add(key)
                    output.append({"name": f"parent_{len(output):03d}", "parameters": parameters})
                if len(output) >= derivative_limit:
                    break
    if method == "grid":
        combinations = itertools.product(*(space[key] for key in space))
        for row in combinations:
            values = dict(zip(space, row))
            try:
                parameters = _make_parameters(strategy, values)
            except ValueError:
                continue
            key = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
            if key in seen:
                continue
            seen.add(key)
            output.append({"name": f"grid_{len(output):03d}", "parameters": parameters})
            if len(output) >= count:
                break
    elif method == "random":
        attempts = 0
        while len(output) < count and attempts < count * 40:
            attempts += 1
            try:
                parameters = _make_parameters(strategy, _random_values(space, rng))
            except ValueError:
                continue
            key = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
            if key in seen:
                continue
            seen.add(key)
            output.append({"name": f"random_{len(output):03d}", "parameters": parameters})
    elif method != "bayes":
        raise ValueError("search method must be grid, random, or bayes")
    return output[:count]

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

def _ordered_window_options(options: list[int], preferred: int | None = None) -> list[int]:
    """Start from the previous winner or the middle option, then try neighbors."""
    if not options:
        raise ValueError("at least one adaptive window length is required")
    unique = sorted(set(int(value) for value in options))
    first = preferred if preferred in unique else unique[len(unique) // 2]
    return [first, *sorted((value for value in unique if value != first), key=lambda value: (abs(value - first), value))]

def _load_window_data(
    *, db: str, symbols: list[str], start: date, end: date, adjustment: str,
    strategy: str,
) -> tuple[dict[str, list[dict]], list[dict]]:
    store = SQLiteBarStore(db)
    warmup = max(DAILY_WARMUP_DAYS, multi_factor_warmup_days() if strategy == "multi_factor" else 0)
    warm_start = (start - timedelta(days=warmup)).isoformat()
    bars = store.bars(
        symbols, feed=FEED, timeframe="5Min", start=warm_start,
        end=end.isoformat(), adjustment=adjustment,
    )
    spy = store.bars(
        ["SPY"], feed=FEED, timeframe="1Day", start=warm_start,
        end=end.isoformat(), adjustment=adjustment,
    ).get("SPY", [])
    return bars, spy

def _profile_for(strategy: str, parameters: dict) -> dict:
    controls = deepcopy(CORE_RESEARCH_CONTROLS)
    if strategy == "multi_factor":
        per_strategy = controls.setdefault("max_entries_per_strategy_per_day", {})
        per_strategy["multi_factor"] = min(int(per_strategy.get("multi_factor", 2)), 2)
    return {
        "name": "adaptive_candidate", "parameters": {strategy: parameters},
        "strategies": [strategy], "controls": controls,
    }

def _run_replay(
    *, strategy: str, parameters: dict, symbols: list[str], bars: dict,
    spy_daily: list[dict], start: date, end: date, args,
    capital: float | None = None,
    preaggregated_daily_bars: dict[str, list[dict]] | None = None,
    precomputed_timeline: dict[datetime, dict[str, dict]] | None = None,
) -> dict:
    profile = _profile_for(strategy, parameters)
    replay = AlgoPortfolioBacktest(
        symbols=symbols, intraday_bars=bars, spy_daily_bars=spy_daily,
        start=start.isoformat(), end=end.isoformat(), feed=FEED,
        data_adjustment=args.adjustment, initial_capital=float(capital or args.capital),
        slippage_bps=args.slippage_bps, spread_bps=args.spread_bps,
        commission_per_share=args.commission_per_share,
        short_borrow_annual_pct=args.short_borrow_annual_pct / 100,
        unknown_borrow_penalty_bps=args.unknown_borrow_penalty_bps,
        borrow_availability=args.borrow_availability_data,
        profile=profile["parameters"], strategies=profile["strategies"],
        research_controls=profile["controls"], force_close_at_end=True,
        preaggregated_daily_bars=preaggregated_daily_bars,
        precomputed_timeline=precomputed_timeline,
    )
    with quiet_coordinator_rejections():
        return replay.run()

def _candidate_replay_worker_init(context: dict) -> None:
    """Install read-only replay inputs once in each candidate worker."""
    global _CANDIDATE_REPLAY_CONTEXT
    _CANDIDATE_REPLAY_CONTEXT = context
    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
        handler.close()
    root_logger.addHandler(logging.NullHandler())
    root_logger.setLevel(logging.CRITICAL)
    logging.getLogger("strategy_runner").setLevel(logging.CRITICAL)

def _evaluate_search_candidate(candidate: dict, context: dict) -> dict:
    report = _run_replay(
        strategy=context["strategy"], parameters=candidate["parameters"],
        symbols=context["symbols"], bars=context["bars"],
        spy_daily=context["spy_daily"], start=context["start"], end=context["end"],
        args=context["args"],
        preaggregated_daily_bars=context["preaggregated_daily_bars"],
        precomputed_timeline=context["precomputed_timeline"],
    )
    metrics = _metrics(report, capital=context["args"].capital)
    fitness, components = _fitness(metrics, context["weights"])
    enough_trades = int(metrics.get("closed_trade_count") or 0) >= context["min_trades"]
    positive_after_costs = float(metrics.get("net_return_pct") or 0) > 0
    del report
    return {
        "name": candidate["name"], "parameters": candidate["parameters"],
        "metrics": metrics, "fitness": round(fitness, 8), "_fitness_raw": fitness,
        "fitness_components": {key: round(value, 8) for key, value in components.items()},
        "eligible": enough_trades and positive_after_costs,
        "minimum_trade_count_met": enough_trades,
        "positive_after_costs": positive_after_costs,
    }

def _evaluate_candidate_in_worker(candidate: dict) -> dict:
    if _CANDIDATE_REPLAY_CONTEXT is None:
        raise RuntimeError("candidate replay worker was not initialized")
    return _evaluate_search_candidate(candidate, _CANDIDATE_REPLAY_CONTEXT)

def _run_search_with_bars(
    *, strategy: str, method: str, count: int, seed: int,
    space: dict[str, list], symbols: list[str], bars: dict,
    spy_daily: list[dict], start: date, end: date, args,
    weights: dict[str, float], min_trades: int,
    parent_parameters: dict | None = None,
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
    }
    candidates = _candidate_parameters(
        strategy, method, count, seed, space, parent_parameters=parent_parameters,
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
                if key in seen:
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
    parent_parameters: dict | None = None,
    preaggregated_daily_bars: dict[str, list[dict]] | None = None,
    search_func=None,
) -> dict:
    """Refine on training data for bounded rounds; verification remains separate."""
    if iterations < 1:
        raise ValueError("search iterations must be positive")

    candidates = []
    iteration_summaries = []
    incumbent = None
    for iteration in range(1, iterations + 1):
        active_parent = incumbent["parameters"] if incumbent else parent_parameters
        search = (search_func or _run_search_with_bars)(
            strategy=strategy, method=method, count=count,
            seed=seed + (iteration - 1) * 104_729, space=space, symbols=symbols,
            bars=bars, spy_daily=spy_daily, start=start, end=end, args=args,
            weights=weights, min_trades=min_trades,
            parent_parameters=active_parent,
            progress_label=f"iteration {iteration}/{iterations}",
            preaggregated_daily_bars=preaggregated_daily_bars,
        )
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
