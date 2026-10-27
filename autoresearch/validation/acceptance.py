"""Acceptance rules for research candidates against an incumbent and a benchmark.

A candidate is accepted only when it beats the incumbent and the benchmark on
out-of-sample sessions, net of costs, with a t-statistic that clears a bar which
rises with the number of trials searched. The holdout is scored once and must
also pass; a candidate without holdout evidence stays pending.

Inputs are per-session return series keyed by ISO date. Nothing here reads the
live system, and nothing here is promoted by itself; the caller decides.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import mean, stdev


@dataclass(frozen=True)
class AcceptancePolicy:
    cost_bps_per_turnover: float = 10.0
    min_oos_sessions: int = 60
    min_t_stat: float = 2.0
    min_margin_over_incumbent_bps: float = 5.0
    min_positive_fold_fraction: float = 0.6
    folds: int = 5
    purge_sessions: int = 5
    max_drawdown_ratio: float = 1.2


@dataclass
class CandidateResult:
    decision: str
    reasons: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


def net_excess(
    returns: dict[str, float], benchmark: dict[str, float],
    *, turnover: dict[str, float] | None = None, cost_bps: float = 0.0,
) -> dict[str, float]:
    """Daily return minus benchmark return, minus turnover costs, on shared dates."""
    shared = sorted(set(returns) & set(benchmark))
    out = {}
    for day in shared:
        cost = (turnover or {}).get(day, 0.0) * cost_bps / 10_000.0
        out[day] = returns[day] - benchmark[day] - cost
    return out


def t_statistic(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    spread = stdev(values)
    if spread == 0:
        return None
    return mean(values) / (spread / math.sqrt(len(values)))


def deflated_t_threshold(trials: int, floor: float) -> float:
    """Raise the significance bar with the number of candidates tried.

    With N independent trials, the best of N noise draws reaches roughly
    sqrt(2 ln N) standard errors. The floor keeps single-trial runs honest.
    """
    if trials <= 1:
        return floor
    return max(floor, math.sqrt(2.0 * math.log(trials)))


def purged_fold_means(series: dict[str, float], folds: int, purge: int) -> list[float]:
    """Mean excess per contiguous fold, dropping ``purge`` sessions around each boundary."""
    days = sorted(series)
    if folds < 1 or len(days) < folds * 2:
        return []
    size = len(days) // folds
    means = []
    for index in range(folds):
        start = index * size
        stop = len(days) if index == folds - 1 else start + size
        low = start + (purge if index > 0 else 0)
        high = stop - (purge if index < folds - 1 else 0)
        values = [series[day] for day in days[low:high]]
        if values:
            means.append(mean(values))
    return means


def max_drawdown(values: list[float]) -> float:
    """Worst peak-to-trough drop of the cumulative sum of per-session values."""
    peak = 0.0
    equity = 0.0
    worst = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return worst


def evaluate_candidate(
    candidate: dict[str, float],
    incumbent: dict[str, float],
    benchmark: dict[str, float],
    *,
    holdout_start: str,
    trials: int,
    policy: AcceptancePolicy = AcceptancePolicy(),
    candidate_turnover: dict[str, float] | None = None,
    incumbent_turnover: dict[str, float] | None = None,
) -> CandidateResult:
    """Decide whether a candidate may replace the incumbent.

    Decisions: ``accept``, ``reject``, or ``holdout_pending`` (passed search
    rules, but no holdout evidence yet).
    """
    cand = net_excess(candidate, benchmark, turnover=candidate_turnover, cost_bps=policy.cost_bps_per_turnover)
    inc = net_excess(incumbent, benchmark, turnover=incumbent_turnover, cost_bps=policy.cost_bps_per_turnover)
    shared = sorted(set(cand) & set(inc))
    search_days = [day for day in shared if day < holdout_start]
    holdout_days = [day for day in shared if day >= holdout_start]
    cand_oos = {day: cand[day] for day in search_days}
    inc_oos = {day: inc[day] for day in search_days}
    margin = {day: cand[day] - inc[day] for day in search_days}

    reasons: list[str] = []
    metrics = {
        "oos_sessions": len(search_days),
        "holdout_sessions": len(holdout_days),
        "trials": trials,
        "mean_excess_oos_bps": round(mean(cand_oos.values()) * 10_000, 3) if cand_oos else None,
        "margin_over_incumbent_bps": round(mean(margin.values()) * 10_000, 3) if margin else None,
    }
    if len(search_days) < policy.min_oos_sessions:
        return CandidateResult("reject", [f"only {len(search_days)} out-of-sample sessions"], metrics)

    t_value = t_statistic(list(cand_oos.values()))
    threshold = deflated_t_threshold(trials, policy.min_t_stat)
    metrics.update({"t_stat": round(t_value, 3) if t_value is not None else None, "t_threshold": round(threshold, 3)})
    if t_value is None or t_value < threshold:
        reasons.append(f"t-statistic {metrics['t_stat']} below deflated threshold {metrics['t_threshold']}")
    if mean(cand_oos.values()) <= 0:
        reasons.append("candidate does not beat the benchmark out of sample")
    if mean(margin.values()) * 10_000 < policy.min_margin_over_incumbent_bps:
        reasons.append("margin over incumbent below minimum")

    folds = purged_fold_means(cand_oos, policy.folds, policy.purge_sessions)
    positive = sum(value > 0 for value in folds) / len(folds) if folds else 0.0
    metrics["positive_fold_fraction"] = round(positive, 3)
    if positive < policy.min_positive_fold_fraction:
        reasons.append(f"only {positive:.0%} of folds positive")

    cand_dd = max_drawdown([cand_oos[day] for day in search_days])
    inc_dd = max_drawdown([inc_oos[day] for day in search_days])
    if inc_dd > 0 and cand_dd > policy.max_drawdown_ratio * inc_dd:
        reasons.append("drawdown of excess exceeds the incumbent's by more than allowed")
    metrics["drawdown_ratio"] = round(cand_dd / inc_dd, 3) if inc_dd > 0 else None

    if reasons:
        return CandidateResult("reject", reasons, metrics)
    if len(holdout_days) < policy.min_oos_sessions:
        return CandidateResult("holdout_pending", ["passed search; holdout not yet long enough"], metrics)
    holdout_mean = mean(cand[day] for day in holdout_days)
    metrics["mean_excess_holdout_bps"] = round(holdout_mean * 10_000, 3)
    if holdout_mean <= 0:
        return CandidateResult("reject", ["holdout excess is not positive"], metrics)
    return CandidateResult("accept", ["passed search rules and holdout"], metrics)


__all__ = [
    "AcceptancePolicy", "CandidateResult", "deflated_t_threshold", "evaluate_candidate",
    "max_drawdown", "net_excess", "purged_fold_means", "t_statistic",
]
