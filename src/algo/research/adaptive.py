"""Short-window parameter discovery and walk-forward verification.

The optimizer selects only signal parameters. Each fold fits on a recent
training window, then evaluates its winner on the immediately following
unseen window using the existing Algo portfolio replay engine.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import itertools
import json
import math
import os
from pathlib import Path
import random
import statistics
from concurrent.futures import ProcessPoolExecutor, as_completed
from zoneinfo import ZoneInfo

from src.algo.backtest import (
    DAILY_WARMUP_DAYS, multi_factor_warmup_days,
    read_borrow_availability,
)
from src.algo.market_store import DEFAULT_DB, SQLiteBarStore
from src.algo.strategies.registry import (
    ALGO_DEFAULT_PARAMETERS, PARAMETER_KEYS, validate_parameter_profile,
)
from src.algo.strategies.universe import configured_universe
from src.market_data import data_tier, regular_feed

from .files import _atomic_json
from .settings import (
    ADJUSTMENT, CORE_RESEARCH_CONTROLS, DEFAULT_OUTPUT_DIR, FEED,
    STRATEGY_FLAVORS,
)
from src.algo.walk_forward import walk_forward_windows


ET = ZoneInfo("America/New_York")
DEFAULT_FITNESS_WEIGHTS = {
    "sharpe": 0.25,
    "sortino": 0.15,
    "return": 0.25,
    "win_rate": 0.10,
    "profit_factor": 0.15,
    "drawdown": 0.10,
}

# Discrete, bounded, signal-only domains. Execution and account risk remain
# fixed during the search. Users can replace or extend these with a JSON file.
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
    "opening_range_breakout": {
        "opening_range_bars": [3, 6, 9, 12],
        "post_range_delay_bars": [1, 2, 3],
        "minimum_volume_ratio": [1.1, 1.4, 1.8, 2.2],
        "breakout_buffer_pct": [0.0, 0.001, 0.0025],
    },
    "intraday_trend": {
        "fast_period": [5, 6, 8, 10, 12], "slow_period": [15, 18, 20, 24, 30],
        "confirmation_bars": [1, 2, 3], "minimum_slope_pct": [0.0, 0.0005, 0.001, 0.002],
    },
    "intraday_momentum": {
        "lookback": [5, 8, 12, 16, 20],
        "minimum_return": [0.008, 0.012, 0.02, 0.03, 0.04],
        "minimum_short_return": [0.008, 0.012, 0.02, 0.03],
        "minimum_volume_ratio": [1.0, 1.2, 1.5, 1.8],
        "minimum_short_volume_ratio": [1.0, 1.2, 1.5, 1.8],
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
    if strategy in {"intraday_trend", "intraday_momentum"}:
        parameters["enabled"] = True
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
) -> dict:
    from src.algo.walk_forward import _run_portfolio

    profile = _profile_for(strategy, parameters)
    return _run_portfolio(
        symbols=symbols, intraday=bars, spy_daily=spy_daily,
        start=start.isoformat(), end=end.isoformat(), feed=FEED,
        adjustment=args.adjustment, capital=float(capital or args.capital),
        slippage_bps=args.slippage_bps, spread_bps=args.spread_bps,
        commission_per_share=args.commission_per_share,
        short_borrow_annual_pct=args.short_borrow_annual_pct / 100,
        unknown_borrow_penalty_bps=args.unknown_borrow_penalty_bps,
        borrow_availability=args.borrow_availability_data,
        profile=profile["parameters"], strategies=profile["strategies"],
        controls=profile["controls"], event_calendar=None,
        research_runtime_overrides={strategy: True}
        if strategy in {"intraday_trend", "intraday_momentum"} else None,
    )


def _run_search_with_bars(
    *, strategy: str, method: str, count: int, seed: int,
    space: dict[str, list], symbols: list[str], bars: dict,
    spy_daily: list[dict], start: date, end: date, args,
    weights: dict[str, float], min_trades: int,
    parent_parameters: dict | None = None,
) -> dict:
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
    while queue or (method == "bayes" and len(evaluated) < count and bayes_attempts < count * 40):
        if queue:
            candidate = queue.pop(0)
        else:
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
            candidate = {"name": f"bayes_{len(evaluated):03d}", "parameters": parameters}
        report = _run_replay(
            strategy=strategy, parameters=candidate["parameters"], symbols=symbols,
            bars=bars, spy_daily=spy_daily, start=start, end=end, args=args,
        )
        metrics = _metrics(report, capital=args.capital)
        fitness, components = _fitness(metrics, weights)
        enough_trades = int(metrics.get("closed_trade_count") or 0) >= min_trades
        positive_after_costs = float(metrics.get("net_return_pct") or 0) > 0
        eligible = enough_trades and positive_after_costs
        row = {
            "name": candidate["name"], "parameters": candidate["parameters"],
            "metrics": metrics, "fitness": round(fitness, 8),
            "fitness_components": {key: round(value, 8) for key, value in components.items()},
            "eligible": eligible, "minimum_trade_count_met": enough_trades,
            "positive_after_costs": positive_after_costs,
        }
        evaluated.append(row)
        history.append({"parameters": candidate["parameters"], "fitness": fitness})
        del report
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


def _fit_window_ladder(
    *, strategy: str, method: str, count: int, seed: int, space: dict[str, list],
    symbols: list[str], bars: dict, spy_daily: list[dict], train_sessions: list[date],
    args, weights: dict[str, float], min_trades: int, window_options: list[int],
) -> dict:
    """Search one or more trailing train windows, falling back until improvement."""
    ordered = _ordered_window_options(window_options)
    results = []
    attempts = []
    for window_days in ordered:
        if len(train_sessions) < window_days:
            attempts.append({"window_days": window_days, "skipped": "insufficient_training_sessions"})
            continue
        fit_start = train_sessions[-window_days]
        fit_end = train_sessions[-1]
        search = _run_search_with_bars(
            strategy=strategy, method=method, count=count,
            seed=seed + window_days * 73, space=space, symbols=symbols,
            bars=bars, spy_daily=spy_daily, start=fit_start, end=fit_end,
            args=args, weights=weights, min_trades=min_trades,
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
    store = SQLiteBarStore(db)
    daily = store.bars(
        ["SPY"], feed=FEED, timeframe="1Day",
        start=(start or date(2015, 1, 1)).isoformat(), end=end.isoformat(),
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


def _prepare_args(args) -> None:
    if args.strategy not in DEFAULT_SEARCH_SPACES:
        raise ValueError(f"unsupported adaptive strategy: {args.strategy}")
    if data_tier() != "algo_trader_plus" or regular_feed() != FEED:
        raise ValueError("adaptive Algo research requires Plus SIP data")
    args.symbols_resolved = _symbols(args.symbols)
    args.db = str(args.db or DEFAULT_DB)
    args.adjustment = args.adjustment or ADJUSTMENT
    args.fitness_weights_data = _normalize_weights(args.fitness_weights)
    args.search_space_data = _search_space(args.strategy, args.search_space)
    args.borrow_availability_data = read_borrow_availability(args.borrow_availability)
    if args.search_method not in {"grid", "random", "bayes"}:
        raise ValueError("search method must be grid, random, or bayes")
    if args.candidates < 1 or args.candidates > 512:
        raise ValueError("candidate count must be between 1 and 512")
    if args.min_trades < 0:
        raise ValueError("minimum trade count cannot be negative")
    if hasattr(args, "window_days") and args.window_days < 2:
        raise ValueError("window-days must be at least two trading sessions")
    for name in ("minimum_oos_sharpe", "maximum_average_decay"):
        if hasattr(args, name) and not math.isfinite(getattr(args, name)):
            raise ValueError(f"{name.replace('_', '-')} must be finite")
    for name in ("capital", "slippage_bps", "spread_bps", "commission_per_share", "short_borrow_annual_pct", "unknown_borrow_penalty_bps"):
        value = getattr(args, name)
        if isinstance(value, bool) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be finite and non-negative")
    if args.capital <= 0:
        raise ValueError("capital must be positive")


def run_discovery(args) -> dict:
    """Run short-window discovery only and persist detailed plus summary JSON."""
    _prepare_args(args)
    end = date.fromisoformat(args.end) if args.end else datetime.now(ET).date() - timedelta(days=1)
    sessions = _trading_sessions(args.db, end, adjustment=args.adjustment)
    if args.start:
        sessions = [session for session in sessions if session >= date.fromisoformat(args.start)]
    window = args.window_days
    if window < 2 or len(sessions) < window:
        raise ValueError(f"need {window} completed SPY sessions for discovery; found {len(sessions)}")
    selected_sessions = sessions[-window:]
    start, end = selected_sessions[0], selected_sessions[-1]
    parent_parameters, parent_metadata = _load_positive_parent(
        args.output_dir, args.strategy, end=end,
    )
    bars, spy = _load_window_data(
        db=args.db, symbols=args.symbols_resolved, start=start, end=end,
        adjustment=args.adjustment, strategy=args.strategy,
    )
    report = _run_search_with_bars(
        strategy=args.strategy, method=args.search_method, count=args.candidates,
        seed=args.seed, space=args.search_space_data, symbols=args.symbols_resolved,
        bars=bars, spy_daily=spy, start=start, end=end, args=args,
        weights=args.fitness_weights_data, min_trades=args.min_trades,
        parent_parameters=parent_parameters,
    )
    report.update({
        "schema": "swarm_algo_adaptive_discovery_v1", "stage": "short_window_discovery",
        "created_at": datetime.now(timezone.utc).isoformat(), "research_only": True,
        "live_configuration_changed": False, "trade_or_order_actions": 0,
        "window": {"start": start.isoformat(), "end": end.isoformat(), "completed_sessions": window},
        "parent_candidate": parent_metadata,
        "symbols": args.symbols_resolved, "database": str(Path(args.db).resolve()),
        "data_tier": data_tier(), "feed": FEED, "adjustment": args.adjustment,
        "limitations": [
            "The short-window winner is a discovery result, not evidence of persistent profitability.",
            "Parameter domains are bounded and discrete; execution costs and risk limits are held fixed.",
        ],
    })
    report["baseline_comparison"] = _baseline_improvement(report)
    return _write_report(args.output_dir, args.strategy, "discovery", report)


def run_window_ladder_discovery(
    args, window_options: list[int], *, pipeline_version: str, data_version: str | None = None,
) -> dict:
    """Try recent windows from preferred to neighboring lengths until a real improvement appears."""
    options = list(dict.fromkeys(int(value) for value in window_options))
    if any(value < 2 for value in options):
        raise ValueError("adaptive discovery windows must be at least two sessions")

    canonical = Path(args.output_dir) / args.strategy / "latest-discovery.json"
    prior, _ = _read_json_file(canonical)
    preferred = None
    if prior and prior.get("strategy") == args.strategy:
        prior_window = (prior.get("adaptive_window_search") or {}).get("selected_window_days")
        if prior_window in options and prior.get("selection_status") == "eligible_positive_candidate":
            preferred = int(prior_window)
        elif prior.get("window", {}).get("completed_sessions") in options:
            preferred = int(prior["window"]["completed_sessions"])
    ordered = _ordered_window_options(options, preferred)
    end = date.fromisoformat(args.end) if args.end else datetime.now(ET).date() - timedelta(days=1)
    attempts = []
    reports = []
    for window_days in ordered:
        trial_output = Path(args.output_dir) / args.strategy / "window-search" / f"{end.isoformat()}-{window_days}"
        trial_output.mkdir(parents=True, exist_ok=True)
        previous_canonical, _ = _read_json_file(canonical)
        if previous_canonical:
            _atomic_json(trial_output / args.strategy / "latest-discovery.json", previous_canonical)
        trial_args = argparse.Namespace(**vars(args))
        trial_args.output_dir = str(trial_output)
        trial_args.window_days = window_days
        report = run_discovery(trial_args)
        detail, _ = _read_json_file(Path(report["latest_detail_artifact"]))
        if not detail:
            raise ValueError(f"adaptive discovery detail is missing for {args.strategy}, {window_days} sessions")
        comparison = detail.get("baseline_comparison") or {}
        selected = detail.get("selected_candidate") or {}
        baseline = comparison.get("baseline_candidate") or {}
        attempt = {
            "window_days": window_days,
            "selection_status": detail.get("selection_status"),
            "selected_candidate": selected.get("name"),
            "selected_fitness": comparison.get("selected_fitness"),
            "baseline_fitness": comparison.get("baseline_fitness"),
            "fitness_delta": comparison.get("fitness_delta"),
            "selected_net_return_pct": (selected.get("metrics") or {}).get("net_return_pct"),
            "baseline_net_return_pct": (baseline.get("metrics") or {}).get("net_return_pct"),
            "improved": bool(comparison.get("improved")),
            "detail_artifact": report["latest_detail_artifact"],
        }
        attempts.append(attempt)
        reports.append((report, detail, attempt))
        if attempt["improved"]:
            break

    successful = next((item for item in reports if item[2]["improved"]), None)
    if successful:
        _, detail, chosen_attempt = successful
    else:
        _, detail, chosen_attempt = max(
            reports,
            key=lambda item: float(item[2].get("baseline_fitness") or -math.inf),
        )
        baseline = (detail.get("baseline_comparison") or {}).get("baseline_candidate")
        if baseline:
            detail["selected_candidate"] = baseline
            comparison = detail.setdefault("baseline_comparison", {})
            comparison.update({
                "selected_fitness": comparison.get("baseline_fitness", baseline.get("fitness", 0.0)),
                "fitness_delta": 0.0,
                "improved": False,
            })
        detail["selection_status"] = "no_adaptive_improvement"
        detail["adaptive_improvement"] = False

    detail["adaptive_window_search"] = {
        "pipeline_version": pipeline_version,
        "data_version": data_version,
        "configured_window_days": options,
        "candidate_count": args.candidates,
        "search_method": args.search_method,
        "attempt_order": ordered,
        "attempts": attempts,
        "selected_window_days": chosen_attempt["window_days"],
        "improvement_found": bool(successful),
        "fallback_used": len(attempts) > 1,
        "selection_rule": "require a sample-qualified positive-after-costs candidate with higher fitness than baseline",
    }
    detail["adaptive_improvement"] = bool(successful)
    return _write_report(args.output_dir, args.strategy, "discovery", detail)


def _read_json_file(path: Path) -> tuple[dict | None, bytes | None]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
        return (value if isinstance(value, dict) else None), raw
    except (OSError, json.JSONDecodeError):
        return None, None


def _load_positive_parent(output_dir: str, strategy: str, *, end: date) -> tuple[dict | None, dict]:
    path = Path(output_dir) / strategy / "latest-discovery.json"
    try:
        parent = json.loads(path.read_text())
        parent_end = date.fromisoformat(str(parent.get("window", {}).get("end", "")))
        selected = parent.get("selected_candidate") or {}
        metrics = selected.get("metrics") or {}
        valid = (
            parent.get("schema") == "swarm_algo_adaptive_discovery_v1"
            and parent.get("strategy") == strategy
            and parent.get("selection_status") == "eligible_positive_candidate"
            and parent_end < end
            and bool(selected.get("eligible"))
            and float(metrics.get("net_return_pct") or 0) > 0
        )
        if not valid:
            return None, {"used": False, "reason": "no prior positive candidate strictly before this window"}
        parameters = validate_parameter_profile({strategy: selected["parameters"]})[strategy]
        return parameters, {
            "used": True, "artifact": str(path.resolve()),
            "window_end": parent_end.isoformat(),
            "candidate": selected.get("name"),
        }
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None, {"used": False, "reason": "no valid prior discovery artifact"}


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
    seed = args.seed + index * 1009
    train_sessions = _trading_sessions(args.db, train_end, train_start, args.adjustment)
    fitted = _fit_window_ladder(
        strategy=args.strategy, method=args.search_method, count=args.candidates,
        seed=seed, space=args.search_space_data, symbols=args.symbols_resolved,
        bars=bars, spy_daily=spy, train_sessions=train_sessions, args=args,
        weights=args.fitness_weights_data, min_trades=args.min_trades,
        window_options=args.train_window_options,
    )
    discovery = fitted["search"]
    selected = fitted["selected_candidate"]
    selected_parameters = selected["parameters"]
    capital_before = float(args.capital)
    test_report = _run_replay(
        strategy=args.strategy, parameters=selected_parameters,
        symbols=args.symbols_resolved, bars=bars, spy_daily=spy,
        start=test_start, end=test_end, args=args, capital=capital_before,
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
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    sessions = _trading_sessions(args.db, end, start, args.adjustment)
    if len(sessions) < args.train_days + args.test_days:
        raise ValueError("date range does not contain enough sessions for a train and test window")
    if args.step_days < args.test_days:
        raise ValueError("step-days must be at least test-days so stitched OOS segments do not overlap")
    holdout = args.holdout_days
    if (
        args.train_days < 2 or args.test_days < 1 or args.step_days < 1
        or args.workers < 1 or args.minimum_folds < 1
    ):
        raise ValueError("train/test/step days, workers, and minimum folds must be positive")
    if holdout < 0 or (holdout and holdout > len(sessions) - args.train_days - args.test_days):
        raise ValueError("holdout-days must leave room for at least one train and test window")
    development_sessions = sessions[:-holdout] if holdout else sessions
    windows = walk_forward_windows(
        development_sessions, train_days=args.train_days,
        test_days=args.test_days, step_days=args.step_days,
    )
    if len(windows) < args.minimum_folds:
        raise ValueError(f"need at least {args.minimum_folds} walk-forward folds; found {len(windows)}")
    fold_jobs = [(index, window, args) for index, window in enumerate(windows)]
    workers = min(args.workers, len(fold_jobs), os.cpu_count() or args.workers)
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
    locked_holdout = None
    if holdout:
        holdout_sessions = sessions[-holdout:]
        train_sessions = sessions[-holdout - args.train_days:-holdout]
        train_start, train_end = train_sessions[0], train_sessions[-1]
        hold_start, hold_end = holdout_sessions[0], holdout_sessions[-1]
        bars, spy = _load_window_data(
            db=args.db, symbols=args.symbols_resolved, start=train_start, end=hold_end,
            adjustment=args.adjustment, strategy=args.strategy,
        )
        final_fit = _fit_window_ladder(
            strategy=args.strategy, method=args.search_method, count=args.candidates,
            seed=args.seed + 987_654, space=args.search_space_data,
            symbols=args.symbols_resolved, bars=bars, spy_daily=spy,
            train_sessions=train_sessions, args=args,
            weights=args.fitness_weights_data, min_trades=args.min_trades,
            window_options=window_options,
        )
        selected = final_fit["selected_candidate"]
        hold_report = _run_replay(
            strategy=args.strategy, parameters=selected["parameters"],
            symbols=args.symbols_resolved, bars=bars, spy_daily=spy,
            start=hold_start, end=hold_end, args=args, capital=args.capital,
        )
        hold_metrics = _metrics(hold_report, capital=args.capital)
        hold_fitness, _ = _fitness(hold_metrics, args.fitness_weights_data)
        locked_holdout = {
            "training_window": {"start": train_start.isoformat(), "end": train_end.isoformat()},
            "holdout_window": {"start": hold_start.isoformat(), "end": hold_end.isoformat(), "sessions": holdout},
            "selected_candidate": selected["name"], "train_parameters": selected["parameters"],
            "fit_window_days": final_fit["fit_window_days"],
            "fit_window_start": final_fit["fit_window_start"],
            "fit_window_end": final_fit["fit_window_end"],
            "adaptive_improvement": final_fit["adaptive_improvement"],
            "window_attempts": final_fit["window_attempts"],
            "train_fitness": selected["fitness"], "train_metrics": selected["metrics"],
            "oos_fitness": round(hold_fitness, 8), "oos_metrics": hold_metrics,
            "fitness_decay": round(float(selected["fitness"]) - hold_fitness, 8),
            "daily_equity": hold_report.get("daily_equity", []),
            "optimization_used_holdout_data": False,
        }
        locked_holdout["pass"] = bool(
            locked_holdout["adaptive_improvement"]
            and hold_metrics.get("daily_sharpe_annualized") is not None
            and hold_metrics["daily_sharpe_annualized"] >= args.minimum_oos_sharpe
            and locked_holdout["fitness_decay"] <= args.maximum_average_decay
        )
        del bars, spy, hold_report
    overall_pass = walk_forward_pass and (locked_holdout["pass"] if locked_holdout else True)
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
        "data": {"start": start.isoformat(), "end": end.isoformat(), "symbols": args.symbols_resolved,
                 "feed": FEED, "adjustment": args.adjustment, "data_tier": data_tier(),
                 "database": str(Path(args.db).resolve()),
                 "data_version": getattr(args, "data_version", None)},
        "window_config": {"train_days": args.train_days, "test_days": args.test_days,
                          "train_window_options": window_options,
                          "step_days": args.step_days, "locked_holdout_days": holdout},
        "worker_count": workers,
        "folds": folds,
        "stitched_oos_daily_equity": curve,
        "locked_holdout": locked_holdout,
        "limitations": [
            "The search uses bounded discrete signal parameters; execution and risk settings are held fixed.",
            "Each fold begins with the stitched equity from the prior OOS segment; training searches use fixed starting capital.",
            "A configured locked holdout is searched against only its preceding training window and evaluated once.",
            "The universe is fixed across history; this is not a profitability guarantee or live promotion.",
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
            "locked_holdout_passed": locked_holdout["pass"] if locked_holdout else None,
            "walk_forward_pass": walk_forward_pass,
            "overall_pass": overall_pass,
        },
        "thresholds": {"minimum_oos_sharpe": args.minimum_oos_sharpe,
                       "maximum_average_decay": args.maximum_average_decay,
                       "minimum_folds": args.minimum_folds},
    }
    report = _write_report(args.output_dir, args.strategy, "walk-forward", detail, summary=summary)
    return report


def _write_report(output_dir: str, strategy: str, label: str, detail: dict, *, summary: dict | None = None) -> dict:
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = Path(output_dir) / strategy
    directory.mkdir(parents=True, exist_ok=True)
    detail_path = directory / f"{now}-{label}.json"
    latest_path = directory / f"latest-{label}.json"
    detail["detail_artifact"] = str(detail_path.resolve())
    detail["latest_detail_artifact"] = str(latest_path.resolve())
    _atomic_json(detail_path, detail)
    _atomic_json(latest_path, detail)
    result = {
        "status": "completed", "stage": detail.get("stage"), "strategy": strategy,
        "detail_artifact": str(detail_path.resolve()),
        "latest_detail_artifact": str(latest_path.resolve()),
    }
    if summary is not None:
        summary["detail_artifact"] = str(detail_path.resolve())
        summary_path = directory / f"{now}-summary.json"
        summary_latest = directory / f"latest-{label}-summary.json"
        _atomic_json(summary_path, summary)
        _atomic_json(summary_latest, summary)
        _atomic_json(directory / "latest-summary.json", summary)
        result["summary_artifact"] = str(summary_path.resolve())
        result["summary"] = summary
    else:
        summary = {
            "schema": "swarm_algo_adaptive_discovery_summary_v1",
            "stage": detail.get("stage"), "strategy": strategy,
            "window": detail.get("window"), "candidate_count": detail.get("candidate_count_evaluated"),
            "selection_status": detail.get("selection_status"),
            "selected_candidate": detail.get("selected_candidate"),
            "fitness": detail.get("selected_candidate", {}).get("fitness"),
            "detail_artifact": str(detail_path.resolve()),
        }
        summary_path = directory / f"{now}-summary.json"
        summary_latest = directory / f"latest-{label}-summary.json"
        _atomic_json(summary_path, summary)
        _atomic_json(summary_latest, summary)
        _atomic_json(directory / "latest-summary.json", summary)
        result["summary_artifact"] = str(summary_path.resolve())
        result["summary"] = summary
    return result


def add_cli_commands(commands) -> None:
    discovery = commands.add_parser(
        "adaptive-search", help="discover short-window regime-fitted parameter candidates",
    )
    _add_adaptive_args(discovery)
    discovery.add_argument("--window-days", type=int, default=60,
                           help="number of latest completed trading sessions used for discovery")
    discovery.set_defaults(handler=run_discovery)

    verification = commands.add_parser(
        "adaptive-walk-forward", help="verify short-window fitting across rolling unseen windows",
    )
    _add_adaptive_args(verification, include_range=False)
    verification.add_argument("--start", required=True)
    verification.add_argument("--end", required=True)
    verification.add_argument("--train-days", type=int, default=60,
                              help="recent completed sessions per rolling fit window")
    verification.add_argument(
        "--train-window-options", type=int, nargs="+",
        help="try these trailing training-window lengths in sessions, starting from the middle value",
    )
    verification.add_argument("--test-days", type=int, default=20,
                              help="immediately following unseen sessions per fold")
    verification.add_argument("--step-days", type=int, default=20,
                              help="advance between folds; must be at least test-days")
    verification.add_argument("--holdout-days", type=int, default=0,
                              help="reserve final sessions from all walk-forward fitting")
    verification.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 1))
    verification.add_argument("--minimum-folds", type=int, default=3)
    verification.add_argument("--minimum-oos-sharpe", type=float, default=0.0)
    verification.add_argument("--maximum-average-decay", type=float, default=0.35)
    verification.set_defaults(handler=run_adaptive_walk_forward)


def _add_adaptive_args(parser: argparse.ArgumentParser, *, include_range: bool = True) -> None:
    parser.add_argument("--strategy", required=True, choices=sorted(DEFAULT_SEARCH_SPACES))
    parser.add_argument("--symbols", help="comma-separated universe; defaults to configured Algo universe")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--adjustment", choices=["raw", "split", "dividend", "all"], default=ADJUSTMENT)
    parser.add_argument("--search-method", choices=["grid", "random", "bayes"], default="random")
    parser.add_argument("--candidates", type=int, default=16, help="maximum parameter sets, including baseline")
    parser.add_argument("--seed", type=int, default=41)
    parser.add_argument("--search-space", help="JSON file mapping tunable parameter names to discrete candidate values")
    parser.add_argument("--fitness-weights", default=json.dumps(DEFAULT_FITNESS_WEIGHTS))
    parser.add_argument("--min-trades", type=int, default=3)
    parser.add_argument("--capital", type=float, default=100_000)
    parser.add_argument("--slippage-bps", type=float, default=5)
    parser.add_argument("--spread-bps", type=float, default=3)
    parser.add_argument("--commission-per-share", type=float, default=0.005)
    parser.add_argument("--short-borrow-annual-pct", type=float, default=3)
    parser.add_argument("--unknown-borrow-penalty-bps", type=float, default=25)
    parser.add_argument("--borrow-availability")
    if include_range:
        parser.add_argument("--start")
        parser.add_argument("--end")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR / "adaptive"))
