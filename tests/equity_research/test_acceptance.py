"""Candidates must beat the benchmark and incumbent out of sample, net of costs, with a deflated Sharpe."""

import random
from datetime import date, timedelta

import pytest

from statistics import NormalDist

from equity_research.validation.acceptance import (
    EULER_MASCHERONI, AcceptancePolicy, deflated_sharpe_ratio,
    evaluate_search, expected_max_sharpe, purged_fold_means,
)


def _days(count: int) -> list[str]:
    start = date(2024, 1, 1)
    return [(start + timedelta(days=index)).isoformat() for index in range(count)]


def _series(days, drift, noise, seed):
    rng = random.Random(seed)
    return {day: drift + rng.gauss(0, noise) for day in days}


def test_real_edge_over_benchmark_and_incumbent_is_accepted():
    days = _days(400)
    benchmark = _series(days, 0.0, 0.01, seed=1)
    incumbent = {day: benchmark[day] + 0.0001 for day in days}
    candidate = {day: benchmark[day] + 0.0009 + random.Random(7 + i).gauss(0, 0.002) for i, day in enumerate(days)}

    result = evaluate_search(
        candidate, incumbent, benchmark, trials=3,
        policy=AcceptancePolicy(cost_bps_per_turnover=0.0),
    )

    assert result.decision == "search_passed", result.reasons


def test_noise_is_rejected_once_the_trial_count_raises_the_bar():
    days = _days(400)
    benchmark = _series(days, 0.0, 0.01, seed=2)
    incumbent = _series(days, 0.0, 0.01, seed=3)
    candidate = {day: benchmark[day] + value for day, value in _series(days, 0.00005, 0.01, seed=4).items()}

    result = evaluate_search(
        candidate, incumbent, benchmark, trials=500,
        policy=AcceptancePolicy(cost_bps_per_turnover=0.0),
    )

    assert result.decision == "reject"
    assert any("deflated Sharpe" in reason for reason in result.reasons)


def test_expected_max_sharpe_is_zero_for_one_trial_and_rises_with_trials():
    assert expected_max_sharpe(1, 1.0) == 0.0
    # Two trials: Euler-Mascheroni term times the normal quantile at 1 - 1/(2e).
    expected_two = EULER_MASCHERONI * NormalDist().inv_cdf(1 - 1 / (2 * 2.718281828459045))
    assert abs(expected_max_sharpe(2, 1.0) - expected_two) < 1e-9
    values = [expected_max_sharpe(n, 1.0) for n in (1, 2, 10, 100, 1000, 10000)]
    assert values == sorted(values)
    assert values[-1] > 3.5


def test_deflated_sharpe_matches_the_closed_form_for_normal_returns():
    rng = random.Random(21)
    values = [rng.gauss(0.002, 0.01) for _ in range(250)]
    result = deflated_sharpe_ratio(values, trials=50)
    sharpe = result["sharpe_per_session"]
    shape = 1.0 - result["skew"] * sharpe + (result["kurtosis"] - 1.0) / 4.0 * sharpe ** 2
    # z = SR * sqrt(T - 1) / sqrt(shape) - E[max z-score of N trials]
    z_score = sharpe * (250 - 1) ** 0.5 / shape ** 0.5 - expected_max_sharpe(50, 1.0)
    assert abs(result["deflated_sharpe"] - NormalDist().cdf(z_score)) < 1e-9
    assert result["skew"] == pytest.approx(0.0, abs=0.35)
    assert result["kurtosis"] == pytest.approx(3.0, abs=0.8)


def test_deflated_sharpe_falls_as_the_number_of_evaluated_candidates_grows():
    rng = random.Random(5)
    values = [rng.gauss(0.003, 0.01) for _ in range(200)]
    alone = deflated_sharpe_ratio(values, trials=1)["deflated_sharpe"]
    many = deflated_sharpe_ratio(values, trials=1000)["deflated_sharpe"]
    assert alone > 0.99
    assert many < alone
    assert deflated_sharpe_ratio([0.001] * 10, trials=5)["deflated_sharpe"] is None


def test_same_edge_passes_search_for_few_trials_and_fails_for_many():
    days = _days(400)
    benchmark = _series(days, 0.0, 0.01, seed=8)
    incumbent = {day: benchmark[day] for day in days}
    candidate = {day: benchmark[day] + 0.0012 + random.Random(40 + i).gauss(0, 0.01) for i, day in enumerate(days)}
    policy = AcceptancePolicy(cost_bps_per_turnover=0.0, min_oos_sessions=20)

    few = evaluate_search(candidate, incumbent, benchmark, trials=1, policy=policy)
    many = evaluate_search(candidate, incumbent, benchmark, trials=5000, policy=policy)

    assert few.decision == "search_passed", few.reasons
    assert many.decision == "reject"


def test_search_gate_never_reads_the_holdout_but_the_final_gate_does():
    days = _days(300)
    benchmark = _series(days, 0.0, 0.01, seed=12)
    incumbent = {day: benchmark[day] for day in days}
    candidate = {
        day: benchmark[day] + 0.002 + random.Random(70 + i).gauss(0, 0.002)
        if i < 200 else benchmark[day] - 0.05
        for i, day in enumerate(days)
    }
    policy = AcceptancePolicy(cost_bps_per_turnover=0.0, min_oos_sessions=20)

    search = evaluate_search(candidate, incumbent, benchmark, trials=1, policy=policy)

    assert search.decision == "reject"
    assert "holdout_sessions" not in search.metrics


def test_purged_folds_drop_boundary_sessions_and_cover_the_sample():
    days = _days(100)
    series = {day: 1.0 for day in days}
    means = purged_fold_means(series, folds=5, purge=2)
    assert len(means) == 5
    assert all(value == 1.0 for value in means)


def test_point_in_time_membership_uses_only_the_prior_close():
    from equity_research.backtest.membership import PointInTimeMembership
    from datetime import date as _date

    membership = PointInTimeMembership({
        "2026-10-20": ["AAA", "BBB"],
        "2026-10-21": ["CCC"],
    })

    assert membership.symbols_for(_date(2026, 10, 20)) == []
    assert membership.symbols_for(_date(2026, 10, 21)) == ["AAA", "BBB"]
    assert membership.symbols_for(_date(2026, 10, 22)) == ["CCC"]


def test_forward_gate_accepts_a_candidate_with_a_real_edge_and_rejects_noise():
    from equity_research.validation.forward_validation import _acceptance_decision

    days = _days(150)
    benchmark = {day: random.Random(11 + i).gauss(0, 0.01) for i, day in enumerate(days)}
    baseline = {day: benchmark[day] + 0.0001 for day in days}
    edge = {day: benchmark[day] + 0.001 + random.Random(31 + i).gauss(0, 0.002) for i, day in enumerate(days)}
    noise = {day: benchmark[day] + random.Random(71 + i).gauss(0, 0.01) for i, day in enumerate(days)}

    good = _acceptance_decision({"candidate": edge, "baseline": baseline, "benchmark": benchmark}, trials=2)
    bad = _acceptance_decision({"candidate": noise, "baseline": baseline, "benchmark": benchmark}, trials=2)

    assert good["decision"] == "search_passed", good["reasons"]
    assert bad["decision"] == "reject"
    assert _acceptance_decision(None, trials=1)["decision"] == "reject"
