"""Small, deterministic diagnostics used by adaptive research reports."""

from __future__ import annotations

import math
import random
import statistics


def _grid_distance(left: dict, right: dict, search_space: dict[str, list]) -> tuple[int, int]:
    changed = 0
    step_distance = 0
    for name in search_space:
        a, b = _parameter_value(left, name), _parameter_value(right, name)
        if a == b:
            continue
        changed += 1
        values = search_space[name]
        try:
            step_distance += abs(values.index(a) - values.index(b))
        except ValueError:
            step_distance += 1
    return changed, step_distance


def _parameter_value(parameters: dict, name: str):
    if name in parameters:
        return parameters[name]
    value = parameters
    for part in name.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def choose_plateau_candidate(
    candidates: list[dict], search_space: dict[str, list], *,
    relative_tolerance: float = 0.10, absolute_tolerance: float = 0.02,
) -> tuple[dict, dict]:
    """Choose a central near-best training candidate and describe its support."""
    pool = [row for row in candidates if isinstance(row, dict) and isinstance(row.get("parameters"), dict)]
    if not pool:
        raise ValueError("parameter plateau selection requires evaluated candidates")
    eligible = [row for row in pool if row.get("eligible")]
    if eligible:
        pool = eligible
    best = max(pool, key=lambda row: float(row.get("fitness") or 0.0))
    best_fitness = float(best.get("fitness") or 0.0)
    tolerance = max(absolute_tolerance, abs(best_fitness) * relative_tolerance)
    plateau = [
        row for row in pool
        if float(row.get("fitness") or 0.0) >= best_fitness - tolerance
    ]

    def centrality(row: dict) -> tuple[float, float, float]:
        distances = [
            _grid_distance(row["parameters"], other["parameters"], search_space)[1]
            for other in plateau
        ]
        return (
            statistics.mean(distances) if distances else 0.0,
            -float(row.get("fitness") or 0.0),
            float((row.get("metrics") or {}).get("max_drawdown_pct") or 0.0),
        )

    selected = min(plateau, key=centrality)
    adjacent = [
        row for row in plateau if row is not selected
        and _grid_distance(selected["parameters"], row["parameters"], search_space)[0] == 1
        and _grid_distance(selected["parameters"], row["parameters"], search_space)[1] == 1
    ]
    result = {
        "supported": len(plateau) >= 2 and bool(adjacent),
        "candidate_count": len(pool),
        "near_best_candidate_count": len(plateau),
        "adjacent_candidate_count": len(adjacent),
        "best_fitness": round(best_fitness, 8),
        "selected_fitness": round(float(selected.get("fitness") or 0.0), 8),
        "fitness_tolerance": round(tolerance, 8),
        "near_best_candidate_names": [str(row.get("name", "unknown")) for row in plateau],
        "selection": "central_candidate_within_training_fitness_plateau",
    }
    return selected, result


def walk_forward_efficiency(train_fitness: float, oos_fitness: float) -> float | None:
    """OOS/IS fitness ratio; undefined when the in-sample score is non-positive."""
    if not math.isfinite(train_fitness) or not math.isfinite(oos_fitness) or train_fitness <= 0:
        return None
    return round(oos_fitness / train_fitness, 6)


def summarize_fold_consistency(folds: list[dict], *, minimum_positive_fraction: float) -> dict:
    returns = [
        float((fold.get("oos_metrics") or {}).get("net_return_pct"))
        for fold in folds
        if (fold.get("oos_metrics") or {}).get("net_return_pct") is not None
    ]
    positive = sum(value > 0 for value in returns)
    fraction = positive / len(returns) if returns else 0.0
    return {
        "fold_count": len(returns),
        "positive_fold_count": positive,
        "positive_fold_fraction": round(fraction, 6),
        "median_oos_return_pct": round(statistics.median(returns), 6) if returns else None,
        "worst_oos_return_pct": round(min(returns), 6) if returns else None,
        "minimum_positive_fraction": minimum_positive_fraction,
        "consistency_gate_met": bool(returns and fraction >= minimum_positive_fraction),
    }


def bootstrap_daily_equity(
    equity_curve: list[dict], *, iterations: int = 1000, block_size: int = 5,
    seed: int = 41,
) -> dict:
    """Block-bootstrap daily returns, preserving short-range serial dependence."""
    if iterations < 0 or block_size < 1:
        raise ValueError("bootstrap iterations must be non-negative and block size positive")
    if iterations == 0:
        return {
            "status": "disabled", "observation_count": max(0, len(equity_curve) - 1),
            "iterations": 0, "block_size_sessions": block_size,
        }
    points = []
    previous = None
    for row in equity_curve:
        try:
            equity = float(row.get("equity"))
        except (TypeError, ValueError):
            continue
        if equity <= 0:
            continue
        if previous is not None:
            points.append(equity / previous - 1.0)
        previous = equity
    if len(points) < max(20, block_size * 2):
        return {
            "status": "insufficient_daily_returns", "observation_count": len(points),
            "iterations": 0, "block_size_sessions": block_size,
        }
    rng = random.Random(seed)
    count = len(points)
    block = min(block_size, count)
    outcomes = []
    for _ in range(iterations):
        sample = []
        while len(sample) < count:
            start = rng.randrange(0, count - block + 1)
            sample.extend(points[start:start + block])
        sample = sample[:count]
        equity = 1.0
        peak = 1.0
        drawdown = 0.0
        for value in sample:
            equity *= max(0.0, 1.0 + value)
            peak = max(peak, equity)
            if peak:
                drawdown = max(drawdown, (peak - equity) / peak)
        outcomes.append((equity - 1.0, drawdown))
    returns = sorted(value[0] * 100 for value in outcomes)
    drawdowns = sorted(value[1] * 100 for value in outcomes)

    def percentile(values: list[float], fraction: float) -> float:
        return round(values[min(len(values) - 1, int((len(values) - 1) * fraction))], 4)

    return {
        "status": "completed", "observation_count": count,
        "iterations": iterations, "block_size_sessions": block,
        "net_return_pct_p05": percentile(returns, 0.05),
        "net_return_pct_p50": percentile(returns, 0.50),
        "net_return_pct_p95": percentile(returns, 0.95),
        "max_drawdown_pct_p50": percentile(drawdowns, 0.50),
        "max_drawdown_pct_p95": percentile(drawdowns, 0.95),
        "probability_of_loss_pct": round(100 * sum(value < 0 for value in returns) / len(returns), 2),
    }


def bootstrap_trade_sequence(
    trade_pnls: list[float], *, initial_capital: float = 100_000.0,
    iterations: int = 1000, block_size: int = 5, seed: int = 41,
) -> dict:
    """Block-bootstrap after-cost trade P&L sequences to stress path dependence."""
    if initial_capital <= 0 or iterations < 0 or block_size < 1:
        raise ValueError("trade bootstrap requires positive capital/block size and non-negative iterations")
    if iterations == 0:
        return {"status": "disabled", "observation_count": len(trade_pnls), "iterations": 0}
    values = []
    for raw in trade_pnls:
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(value):
            values.append(value)
    if len(values) < max(10, block_size * 2):
        return {
            "status": "insufficient_closed_trades", "observation_count": len(values),
            "iterations": 0, "block_size_trades": block_size,
        }
    rng = random.Random(seed)
    count = len(values)
    block = min(block_size, count)
    outcomes = []
    for _ in range(iterations):
        sample = []
        while len(sample) < count:
            start = rng.randrange(0, count - block + 1)
            sample.extend(values[start:start + block])
        sample = sample[:count]
        equity = peak = initial_capital
        drawdown = 0.0
        for pnl in sample:
            equity = max(0.0, equity + pnl)
            peak = max(peak, equity)
            if peak > 0:
                drawdown = max(drawdown, (peak - equity) / peak)
        outcomes.append((equity / initial_capital - 1.0, drawdown))
    returns = sorted(value[0] * 100 for value in outcomes)
    drawdowns = sorted(value[1] * 100 for value in outcomes)

    def percentile(items: list[float], fraction: float) -> float:
        return round(items[min(len(items) - 1, int((len(items) - 1) * fraction))], 4)

    return {
        "status": "completed", "observation_count": count,
        "iterations": iterations, "block_size_trades": block,
        "net_return_pct_p05": percentile(returns, 0.05),
        "net_return_pct_p50": percentile(returns, 0.50),
        "net_return_pct_p95": percentile(returns, 0.95),
        "max_drawdown_pct_p50": percentile(drawdowns, 0.50),
        "max_drawdown_pct_p95": percentile(drawdowns, 0.95),
        "probability_of_loss_pct": round(100 * sum(value < 0 for value in returns) / len(returns), 2),
    }


def simulate_ewma_allocation_shadow(
    daily_strategy_pnl: list[dict], *, initial_capital: float = 100_000.0,
    learning_rate: float = 2.0, log_weight_decay: float = 0.02,
    reward_clip: float = 0.01, minimum_multiplier: float = 0.50,
    maximum_multiplier: float = 1.50, minimum_regime_observations: int = 20,
) -> dict:
    """Simulate delayed, regime-conditioned expert weights on after-cost sleeves.

    The multiplier is applied to the next session's strategy contribution. This
    is a linear shadow allocation over observed sleeve P&L, not a fill-level
    portfolio replay. Each rule-based regime learns its own weights; a new
    regime stays at neutral weights until it has enough observations.
    """
    if (
        initial_capital <= 0 or learning_rate < 0 or not 0 <= log_weight_decay <= 1
        or minimum_regime_observations < 1
    ):
        raise ValueError("invalid shadow allocation configuration")
    if not 0 < minimum_multiplier <= 1 <= maximum_multiplier:
        raise ValueError("allocation multipliers must bracket one")
    rows = sorted(daily_strategy_pnl, key=lambda row: str(row.get("date", "")))
    strategies = sorted({
        str(strategy)
        for row in rows
        for strategy in (row.get("pnl_by_strategy") or {})
    })
    if not rows or not strategies:
        return {"status": "insufficient_strategy_pnl", "daily_observations": len(rows)}
    logs_by_regime: dict[str, dict[str, float]] = {}
    observations_by_regime: dict[str, int] = {}
    base_equity = adjusted_equity = float(initial_capital)
    base_peak = adjusted_peak = float(initial_capital)
    base_drawdown = adjusted_drawdown = 0.0
    weights_path = []
    base_curve = []
    adjusted_curve = []
    latest_regime = "unknown"
    for row in rows:
        p_and_l = row.get("pnl_by_strategy") or {}
        regime = str(row.get("regime") or "unknown")
        latest_regime = regime
        logs = logs_by_regime.setdefault(regime, {strategy: 0.0 for strategy in strategies})
        observations = observations_by_regime.get(regime, 0)
        weights = _bounded_weights(
            logs, minimum=minimum_multiplier, maximum=maximum_multiplier,
        )
        if observations < minimum_regime_observations:
            weights = {strategy: 1.0 for strategy in strategies}
        base_pnl = sum(float(p_and_l.get(strategy, 0.0) or 0.0) for strategy in strategies)
        adjusted_pnl = sum(
            weights[strategy] * float(p_and_l.get(strategy, 0.0) or 0.0)
            for strategy in strategies
        )
        base_equity_before = base_equity
        base_equity += base_pnl
        adjusted_equity += adjusted_pnl
        base_peak = max(base_peak, base_equity)
        adjusted_peak = max(adjusted_peak, adjusted_equity)
        base_drawdown = max(base_drawdown, (base_peak - base_equity) / base_peak if base_peak else 0.0)
        adjusted_drawdown = max(
            adjusted_drawdown,
            (adjusted_peak - adjusted_equity) / adjusted_peak if adjusted_peak else 0.0,
        )
        base_curve.append({"date": row.get("date"), "equity": round(base_equity, 2)})
        adjusted_curve.append({"date": row.get("date"), "equity": round(adjusted_equity, 2)})
        weights_path.append({
            "date": row.get("date"), "regime": regime,
            "weights_used": {key: round(value, 4) for key, value in weights.items()},
        })
        observations += 1
        observations_by_regime[regime] = observations
        for strategy in strategies:
            reward = float(p_and_l.get(strategy, 0.0) or 0.0) / max(base_equity_before, 1.0)
            reward = max(-reward_clip, min(reward_clip, reward))
            logs[strategy] = (
                (1.0 - log_weight_decay) * logs[strategy]
                + learning_rate * reward
            )
        bounded = _bounded_weights(logs, minimum=minimum_multiplier, maximum=maximum_multiplier)
        logs_by_regime[regime] = {key: math.log(value) for key, value in bounded.items()}
    final_weights_by_regime = {
        regime: (
            _bounded_weights(logs, minimum=minimum_multiplier, maximum=maximum_multiplier)
            if observations_by_regime.get(regime, 0) >= minimum_regime_observations
            else {strategy: 1.0 for strategy in strategies}
        )
        for regime, logs in logs_by_regime.items()
    }
    return {
        "status": "completed", "policy": "bounded_regime_conditioned_exponential_weights_after_costs",
        "research_only": True, "live_configuration_changed": False,
        "daily_observations": len(rows), "strategies": strategies,
        "regime_observations": observations_by_regime,
        "parameters": {
            "learning_rate": learning_rate, "log_weight_decay": log_weight_decay,
            "reward_clip_pct_of_equity": reward_clip * 100,
            "minimum_multiplier": minimum_multiplier,
            "maximum_multiplier": maximum_multiplier,
            "minimum_regime_observations": minimum_regime_observations,
        },
        "baseline": _curve_summary(base_curve, initial_capital, base_drawdown),
        "shadow": _curve_summary(adjusted_curve, initial_capital, adjusted_drawdown),
        "final_weights": final_weights_by_regime.get(
            latest_regime, {strategy: 1.0 for strategy in strategies},
        ),
        "final_weights_by_regime": final_weights_by_regime,
        "recent_weight_path": weights_path[-20:],
        "limitation": "Weights learn separately within each recorded regime and remain neutral for the warm-up sample; shadow P&L scales observed sleeves linearly and does not replay changed fills or shared-capacity interactions.",
    }


def _bounded_weights(log_weights: dict[str, float], *, minimum: float, maximum: float) -> dict[str, float]:
    if not log_weights:
        return {}
    raw = {key: math.exp(max(-20.0, min(20.0, value))) for key, value in log_weights.items()}
    target = float(len(raw))
    weights = {key: min(maximum, max(minimum, value)) for key, value in raw.items()}
    for _ in range(8):
        total = sum(weights.values())
        if total <= 0:
            break
        scale = target / total
        updated = {key: min(maximum, max(minimum, value * scale)) for key, value in weights.items()}
        if all(abs(updated[key] - weights[key]) < 1e-9 for key in weights):
            weights = updated
            break
        weights = updated
    return weights


def _curve_summary(curve: list[dict], initial_capital: float, drawdown: float) -> dict:
    final = float(curve[-1]["equity"]) if curve else initial_capital
    return {
        "initial_equity": round(initial_capital, 2), "final_equity": round(final, 2),
        "net_return_pct": round((final / initial_capital - 1.0) * 100, 4),
        "max_drawdown_pct": round(drawdown * 100, 4),
        "daily_equity": curve,
    }
