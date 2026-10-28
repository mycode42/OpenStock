"""Acceptance rules for research candidates against an incumbent and a benchmark.

A candidate is accepted only when it beats the incumbent and the benchmark on
out-of-sample sessions, net of costs, and its Deflated Sharpe Ratio clears the
configured threshold. The DSR deflates the observed Sharpe ratio by the expected
maximum Sharpe of N evaluated candidates (Bailey and Lopez de Prado, 2014), so
the bar rises with every candidate ever evaluated for the strategy. There is no
separate holdout: every shared session is out-of-sample for the rules here.

Inputs are per-session return series keyed by ISO date. Nothing here reads the
live system, and nothing here is promoted by itself; the caller decides.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import NormalDist, mean, stdev

EULER_MASCHERONI = 0.5772156649015329


@dataclass(frozen=True)
class AcceptancePolicy:
    cost_bps_per_turnover: float = 10.0
    min_oos_sessions: int = 60
    min_deflated_sharpe: float = 0.90
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


def expected_max_sharpe(trials: int, variance: float) -> float:
    """Expected maximum of ``trials`` Sharpe estimates whose true Sharpe is zero.

    Euler-Mascheroni approximation from Bailey and Lopez de Prado (2014):
    E[max] = sqrt(V) * ((1 - g) * Z^-1(1 - 1/N) + g * Z^-1(1 - 1/(N e))), where
    g is the Euler-Mascheroni constant, V the variance of the Sharpe estimates,
    and Z^-1 the standard normal quantile. One trial has no selection bias, so
    the expected maximum is zero there.
    """
    if trials <= 1 or variance <= 0:
        return 0.0
    inverse = NormalDist().inv_cdf
    return math.sqrt(variance) * (
        (1.0 - EULER_MASCHERONI) * inverse(1.0 - 1.0 / trials)
        + EULER_MASCHERONI * inverse(1.0 - 1.0 / (trials * math.e))
    )


def deflated_sharpe_ratio(values: list[float], *, trials: int) -> dict:
    """Probability that the true per-session Sharpe beats the best-of-``trials`` luck.

    With per-session Sharpe ``SR`` over ``T`` sessions, skewness ``g3`` and
    non-excess kurtosis ``g4``, the variance of the Sharpe estimate is
    V = (1 - g3*SR + (g4 - 1)/4 * SR^2) / (T - 1). The deflated Sharpe ratio is
    DSR = Phi((SR - SR0) * sqrt(T - 1) / sqrt(1 - g3*SR + (g4 - 1)/4 * SR^2)),
    where SR0 = expected_max_sharpe(N, V) is the Sharpe a zero-skill search
    over N evaluated candidates is expected to reach by chance. DSR is None
    when the sample is too short or the shape term is not positive.
    """
    count = len(values)
    if count < 3:
        return {"deflated_sharpe": None, "reason": "fewer than three sessions", "observations": count, "trials": trials}
    average = mean(values)
    spread = stdev(values)
    if spread == 0:
        return {"deflated_sharpe": None, "reason": "zero return dispersion", "observations": count, "trials": trials}
    sharpe = average / spread
    central = [value - average for value in values]
    second = sum(value ** 2 for value in central) / count
    if second <= 0:
        return {"deflated_sharpe": None, "reason": "zero return dispersion", "observations": count, "trials": trials}
    skew = sum(value ** 3 for value in central) / count / second ** 1.5
    kurtosis = sum(value ** 4 for value in central) / count / second ** 2
    shape = 1.0 - skew * sharpe + (kurtosis - 1.0) / 4.0 * sharpe ** 2
    if shape <= 0:
        return {"deflated_sharpe": None, "reason": "non-positive Sharpe variance term", "observations": count, "trials": trials}
    variance = shape / (count - 1)
    benchmark = expected_max_sharpe(trials, variance)
    z_score = (sharpe - benchmark) * math.sqrt(count - 1) / math.sqrt(shape)
    return {
        "deflated_sharpe": NormalDist().cdf(z_score),
        "sharpe_per_session": sharpe,
        "expected_max_sharpe_per_session": benchmark,
        "sharpe_estimate_variance": variance,
        "skew": skew,
        "kurtosis": kurtosis,
        "observations": count,
        "trials": trials,
    }


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


def _shared_excess(
    candidate: dict[str, float],
    incumbent: dict[str, float],
    benchmark: dict[str, float],
    *, policy: AcceptancePolicy,
    candidate_turnover: dict[str, float] | None,
    incumbent_turnover: dict[str, float] | None,
) -> tuple[dict, dict, list[str]]:
    cand = net_excess(candidate, benchmark, turnover=candidate_turnover, cost_bps=policy.cost_bps_per_turnover)
    inc = net_excess(incumbent, benchmark, turnover=incumbent_turnover, cost_bps=policy.cost_bps_per_turnover)
    return cand, inc, sorted(set(cand) & set(inc))


def evaluate_search(
    candidate: dict[str, float],
    incumbent: dict[str, float],
    benchmark: dict[str, float],
    *,
    trials: int,
    policy: AcceptancePolicy = AcceptancePolicy(),
    candidate_turnover: dict[str, float] | None = None,
    incumbent_turnover: dict[str, float] | None = None,
) -> CandidateResult:
    """Acceptance rules on every shared session.

    Decisions: ``search_passed`` or ``reject``.
    """
    cand, inc, days = _shared_excess(
        candidate, incumbent, benchmark, policy=policy,
        candidate_turnover=candidate_turnover, incumbent_turnover=incumbent_turnover,
    )
    cand_oos = {day: cand[day] for day in days}
    inc_oos = {day: inc[day] for day in days}
    margin = {day: cand[day] - inc[day] for day in days}

    reasons: list[str] = []
    metrics = {
        "oos_sessions": len(days),
        "trials": trials,
        "mean_excess_oos_bps": round(mean(cand_oos.values()) * 10_000, 3) if cand_oos else None,
        "margin_over_incumbent_bps": round(mean(margin.values()) * 10_000, 3) if margin else None,
    }
    if len(days) < policy.min_oos_sessions:
        return CandidateResult("reject", [f"only {len(days)} out-of-sample sessions"], metrics)

    deflated = deflated_sharpe_ratio(list(cand_oos.values()), trials=trials)
    dsr = deflated["deflated_sharpe"]
    t_value = t_statistic(list(cand_oos.values()))
    metrics.update({
        "t_stat": round(t_value, 3) if t_value is not None else None,
        "deflated_sharpe": round(dsr, 4) if dsr is not None else None,
        "min_deflated_sharpe": policy.min_deflated_sharpe,
        "sharpe_per_session": round(deflated.get("sharpe_per_session") or 0.0, 6),
        "expected_max_sharpe_per_session": round(deflated.get("expected_max_sharpe_per_session") or 0.0, 6),
    })
    if dsr is None or dsr < policy.min_deflated_sharpe:
        reasons.append(
            f"deflated Sharpe {metrics['deflated_sharpe']} below minimum {policy.min_deflated_sharpe} "
            f"for {trials} evaluated candidates",
        )
    if mean(cand_oos.values()) <= 0:
        reasons.append("candidate does not beat the benchmark out of sample")
    if mean(margin.values()) * 10_000 < policy.min_margin_over_incumbent_bps:
        reasons.append("margin over incumbent below minimum")

    folds = purged_fold_means(cand_oos, policy.folds, policy.purge_sessions)
    positive = sum(value > 0 for value in folds) / len(folds) if folds else 0.0
    metrics["positive_fold_fraction"] = round(positive, 3)
    if positive < policy.min_positive_fold_fraction:
        reasons.append(f"only {positive:.0%} of folds positive")

    cand_dd = max_drawdown([cand_oos[day] for day in days])
    inc_dd = max_drawdown([inc_oos[day] for day in days])
    if inc_dd > 0 and cand_dd > policy.max_drawdown_ratio * inc_dd:
        reasons.append("drawdown of excess exceeds the incumbent's by more than allowed")
    metrics["drawdown_ratio"] = round(cand_dd / inc_dd, 3) if inc_dd > 0 else None

    if reasons:
        return CandidateResult("reject", reasons, metrics)
    return CandidateResult("search_passed", ["passed acceptance rules"], metrics)


__all__ = [
    "AcceptancePolicy", "CandidateResult", "deflated_sharpe_ratio",
    "evaluate_search", "expected_max_sharpe", "max_drawdown", "net_excess",
    "purged_fold_means", "t_statistic",
]
