"""Candidates must beat the benchmark and incumbent out of sample, and pass a holdout."""

import random
from datetime import date, timedelta

from autoresearch.validation.acceptance import (
    AcceptancePolicy, deflated_t_threshold, evaluate_candidate, purged_fold_means,
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

    result = evaluate_candidate(
        candidate, incumbent, benchmark, holdout_start=days[300], trials=3,
        policy=AcceptancePolicy(cost_bps_per_turnover=0.0),
    )

    assert result.decision == "accept", result.reasons


def test_noise_is_rejected_once_the_trial_count_raises_the_bar():
    days = _days(400)
    benchmark = _series(days, 0.0, 0.01, seed=2)
    incumbent = _series(days, 0.0, 0.01, seed=3)
    candidate = {day: benchmark[day] + value for day, value in _series(days, 0.00005, 0.01, seed=4).items()}

    result = evaluate_candidate(
        candidate, incumbent, benchmark, holdout_start=days[300], trials=500,
        policy=AcceptancePolicy(cost_bps_per_turnover=0.0),
    )

    assert result.decision == "reject"
    assert any("t-statistic" in reason for reason in result.reasons)


def test_candidate_without_holdout_evidence_is_pending_not_accepted():
    days = _days(200)
    benchmark = _series(days, 0.0, 0.01, seed=5)
    incumbent = {day: benchmark[day] for day in days}
    candidate = {day: benchmark[day] + 0.002 + random.Random(9 + i).gauss(0, 0.002) for i, day in enumerate(days)}

    result = evaluate_candidate(
        candidate, incumbent, benchmark, holdout_start="2099-01-01", trials=1,
        policy=AcceptancePolicy(cost_bps_per_turnover=0.0),
    )

    assert result.decision == "holdout_pending"


def test_deflated_threshold_rises_with_trials_and_respects_the_floor():
    assert deflated_t_threshold(1, 2.0) == 2.0
    assert deflated_t_threshold(100, 2.0) > deflated_t_threshold(10, 2.0) >= 2.0


def test_purged_folds_drop_boundary_sessions_and_cover_the_sample():
    days = _days(100)
    series = {day: 1.0 for day in days}
    means = purged_fold_means(series, folds=5, purge=2)
    assert len(means) == 5
    assert all(value == 1.0 for value in means)


def test_point_in_time_membership_uses_only_the_prior_close():
    from autoresearch.backtest.membership import PointInTimeMembership
    from datetime import date as _date

    membership = PointInTimeMembership({
        "2026-10-20": ["AAA", "BBB"],
        "2026-10-21": ["CCC"],
    })

    assert membership.symbols_for(_date(2026, 10, 20)) == []
    assert membership.symbols_for(_date(2026, 10, 21)) == ["AAA", "BBB"]
    assert membership.symbols_for(_date(2026, 10, 22)) == ["CCC"]


def test_forward_gate_accepts_a_candidate_with_a_real_edge_and_rejects_noise():
    from autoresearch.validation.forward_validation import _acceptance_decision

    days = _days(150)
    benchmark = {day: random.Random(11 + i).gauss(0, 0.01) for i, day in enumerate(days)}
    baseline = {day: benchmark[day] + 0.0001 for day in days}
    edge = {day: benchmark[day] + 0.001 + random.Random(31 + i).gauss(0, 0.002) for i, day in enumerate(days)}
    noise = {day: benchmark[day] + random.Random(71 + i).gauss(0, 0.01) for i, day in enumerate(days)}

    good = _acceptance_decision({"candidate": edge, "baseline": baseline, "benchmark": benchmark}, trials=2)
    bad = _acceptance_decision({"candidate": noise, "baseline": baseline, "benchmark": benchmark}, trials=2)

    assert good["decision"] == "accept", good["reasons"]
    assert bad["decision"] == "reject"
    assert _acceptance_decision(None, trials=1)["decision"] == "reject"
