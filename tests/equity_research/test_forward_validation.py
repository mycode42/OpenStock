"""Forward cycle selection rules: search-window winner, promotion cap, trial counting."""

import json
from datetime import date, timedelta

from equity_research.artifacts.candidate_archive import candidate_id
from equity_research.profile_versions import cumulative_trial_count
from equity_research.validation import forward_validation as fv

STRATEGY = "swing_momentum"
DAYS = [f"2026-01-{day:02d}" if day <= 31 else f"2026-02-{day - 31:02d}" for day in range(1, 61)]


def _series(search_return: float, holdout_return: float) -> dict:
    """Candidate returns: the first two thirds are search, the last third is holdout."""
    split = (len(DAYS) * 2) // 3
    candidate = {
        day: (search_return if index < split else holdout_return)
        for index, day in enumerate(DAYS)
    }
    flat = {day: 0.0 for day in DAYS}
    return {"candidate": candidate, "baseline": dict(flat), "benchmark": dict(flat)}


def _trial(candidate: str, strategy: str, series: dict, fitness: float = 0.0) -> dict:
    return {
        "candidate_id": candidate, "strategy": strategy, "regime": "range_bound",
        "discovery_fitness": fitness, "status": "promotion_pending",
        "latest_simulation": {"return_series": series},
    }


def test_cohort_winner_is_chosen_on_search_window_results_only():
    best_on_search = _trial("search-best", STRATEGY, _series(0.002, 0.0))
    best_only_on_holdout = _trial("holdout-only", STRATEGY, _series(0.001, 0.05))

    assert fv._cohort_winner([best_on_search, best_only_on_holdout]) is best_on_search
    assert fv._cohort_winner([best_only_on_holdout, best_on_search]) is best_on_search
    assert fv._cohort_winner([]) is None


def test_holdout_results_do_not_change_the_search_ranking():
    first = _trial("a", STRATEGY, _series(0.002, -0.05))
    second = _trial("b", STRATEGY, _series(0.001, 0.05))
    assert fv._cohort_winner([first, second]) is first
    assert fv._search_return(first["latest_simulation"]["return_series"]) > fv._search_return(
        second["latest_simulation"]["return_series"],
    )


def test_promotion_cap_defaults_allow_one_and_skip_the_rest_with_reason():
    queue = [
        _trial("weak", STRATEGY, _series(0.0005, 0.0)),
        _trial("strong", STRATEGY, _series(0.002, 0.0)),
        _trial("other-strategy", "swing_mean_reversion", _series(0.001, 0.0)),
    ]
    selected = fv._select_promotions(queue, max_per_cycle=1, max_per_strategy=1)

    assert [trial["candidate_id"] for trial in selected] == ["strong"]
    skipped = {trial["candidate_id"]: trial for trial in queue if trial["status"] == "skipped"}
    assert set(skipped) == {"weak", "other-strategy"}
    assert all("cap" in trial["reason"] for trial in skipped.values())


def test_per_strategy_cap_allows_one_promotion_per_strategy_when_the_cycle_cap_is_higher():
    queue = [
        _trial("momentum-a", STRATEGY, _series(0.002, 0.0)),
        _trial("momentum-b", STRATEGY, _series(0.0015, 0.0)),
        _trial("reversion", "swing_mean_reversion", _series(0.001, 0.0)),
    ]
    selected = fv._select_promotions(queue, max_per_cycle=5, max_per_strategy=1)

    assert sorted(trial["candidate_id"] for trial in selected) == ["momentum-a", "reversion"]
    assert [trial["candidate_id"] for trial in queue if trial["status"] == "skipped"] == ["momentum-b"]


def test_discovery_evaluations_come_from_every_window_attempt(tmp_path):
    attempt_a = tmp_path / "attempt-a.json"
    attempt_b = tmp_path / "attempt-b.json"
    params_a = {"lookback": 20}
    params_b = {"lookback": 40}
    attempt_a.write_text(json.dumps({
        "candidates": [
            {"name": "baseline", "parameters": {"lookback": 10}},
            {"name": "iteration_01:x", "parameters": params_a},
        ],
    }))
    attempt_b.write_text(json.dumps({
        "evaluated_candidate_ids": [candidate_id(STRATEGY, params_b)],
    }))
    final = tmp_path / "final.json"
    final.write_text(json.dumps({
        "adaptive_window_search": {"attempts": [
            {"detail_artifact": str(attempt_a)}, {"detail_artifact": str(attempt_b)},
        ]},
        "candidates": [],
    }))

    identifiers = fv._discovery_candidate_ids(STRATEGY, {"detail_artifact": str(final)})

    assert identifiers == {candidate_id(STRATEGY, params_a), candidate_id(STRATEGY, params_b)}


def test_recorded_trial_count_never_shrinks_when_artifacts_disappear(tmp_path):
    database = tmp_path / "research.sqlite3"
    detail = tmp_path / "discovery.json"
    params = [{"lookback": value} for value in (10, 20, 30)]
    detail.write_text(json.dumps({
        "candidates": [{"name": "iteration_01:x", "parameters": item} for item in params],
    }))
    discovery = {"strategies": {STRATEGY: {"status": "completed", "detail_artifact": str(detail)}}}

    fv._record_trial_evidence(database, discovery, {}, [])
    first = cumulative_trial_count(database, STRATEGY)
    detail.unlink()
    fv._record_trial_evidence(database, discovery, {}, [])
    after = cumulative_trial_count(database, STRATEGY)

    assert first == 3
    assert after == 3


def test_acceptance_decision_reports_its_stage():
    series = _series(0.002, 0.0)
    search = fv._acceptance_decision(series, trials=1, final=False)
    final = fv._acceptance_decision(series, trials=1)

    assert search["stage"] == "search"
    assert search["decision"] in {"search_passed", "reject"}
    assert final["stage"] == "final"
    assert fv._acceptance_decision(None, trials=1, final=False)["decision"] == "reject"


# End-to-end cycle with replay, calendar, and regime lookups stubbed out. Every
# path is a temporary file; nothing touches data/research.

SESSIONS = [date(2026, 1, 2) + timedelta(days=index) for index in range(120)]


def _fake_report(seed: int, edge: float) -> dict:
    import random

    rng = random.Random(seed)
    equity = 100_000.0
    daily = [{"date": SESSIONS[0].isoformat(), "equity": equity}]
    benchmark = {}
    for session in SESSIONS[1:]:
        benchmark_return = random.Random(f"bench-{session}").gauss(0, 0.01)
        benchmark[session.isoformat()] = benchmark_return
        equity *= 1.0 + benchmark_return + edge + rng.gauss(0, 0.004)
        daily.append({"date": session.isoformat(), "equity": equity})
    return {"daily_equity": daily, "benchmark_returns": benchmark, "edge": edge}


def _pending_trial(index: int, strategy: str) -> dict:
    return {
        "trial_id": f"trial-{index}", "candidate_id": f"cand-{index}", "strategy": strategy,
        "regime": "range_bound", "parameters": {"probe": index}, "status": "validating",
        "staged_on": "2026-01-01", "staged_at": "2026-01-01T00:00:00+00:00",
        "latest_simulation": None, "cohort_id": None,
    }


def test_cycle_caps_promotions_and_pauses_a_pair_under_an_active_holdout(tmp_path, monkeypatch):
    import json as _json

    from equity_research.settings import LIVE_RESEARCH_STRATEGIES
    from equity_research.validation import final_holdout as holdout
    from strategies.registry import ALGO_DEFAULT_PARAMETERS

    strategies = [name for name in LIVE_RESEARCH_STRATEGIES if name in ALGO_DEFAULT_PARAMETERS][:3]
    assert len(strategies) == 3, strategies
    first, second, held = strategies
    root = tmp_path / "research"
    root.mkdir()
    state_path = root / "forward-validation.json"
    promoted_path = root / "promoted-profile.json"
    database = root / "research.sqlite3"
    state_path.write_text(_json.dumps({
        "schema": "swarm_algo_forward_validation_v1",
        "active_trials": {
            "a": _pending_trial(1, first), "b": _pending_trial(2, first),
            "c": _pending_trial(3, second), "d": _pending_trial(4, held),
        },
        "history": [],
    }))
    promoted_path.write_text("{}")

    # Freeze every cohort, then keep only the held strategy's pair under holdout.
    holdout.initialize_cohort_holdouts(database=database, frozen_on=date(2025, 12, 31))
    for name in (first, second):
        holdout.supersede_cohort_holdouts(database=database, reason="test setup", strategy=name)
    assert holdout.active_holdout_pairs(database) >= {(held, "range_bound")}

    edges = {"a": 0.004, "b": 0.0035, "c": 0.0045, "d": 0.005}
    by_parameter = {1: "a", 2: "b", 3: "c", 4: "d"}

    def fake_simulate(*, strategy, regime, parameters, candidate, **_):
        label = by_parameter[parameters["probe"]]
        return _fake_report(parameters["probe"], edges[label] if candidate else 0.0)

    def fake_metrics(report, strategy, regime):
        return {
            "closed_trades": 40, "distinct_exit_sessions": 30, "target_regime_entry_sessions": 20,
            "net_realized_pnl_after_simulated_costs": 500.0 if report["edge"] else 100.0,
            "marked_pnl_after_estimated_exit_costs": 400.0 if report["edge"] else 50.0,
            "final_equity": 100_500.0,
        }

    monkeypatch.setattr(fv, "FORWARD_VALIDATION_PATH", state_path)
    monkeypatch.setattr(fv, "PROMOTED_PROFILE_PATH", promoted_path)
    monkeypatch.setattr(fv, "refresh_active_parameter_profile", lambda: None)
    monkeypatch.setattr(fv, "_forward_session_data_version", lambda *args, **kwargs: "test-data")
    monkeypatch.setattr(fv, "_current_regime", lambda *args, **kwargs: "range_bound")
    monkeypatch.setattr(fv, "_trading_sessions", lambda *args, **kwargs: [SESSIONS[0]])
    monkeypatch.setattr(fv, "_simulate", fake_simulate)
    monkeypatch.setattr(fv, "_target_metrics", fake_metrics)

    result = fv.run_forward_validation_cycle(
        discovery={"strategies": {}}, verification={}, store=None, database=str(root / "market.sqlite3"),
        symbols=["SPY"], end=SESSIONS[-1], data_version="test-data", allow_stage=False,
        ledger_path=database,
    )

    assert result["status"] == "completed", result
    assert len(result["promoted"]) == 1
    assert result["promotion_paused_by_final_holdout"] is True
    assert f"{held}:range_bound" in result["final_holdout_paused_pairs"]
    assert f"{first}:range_bound" not in result["final_holdout_paused_pairs"]
    # Three eligible candidates (a, b, c): one promotes, the other two are skipped.
    assert len(result["promotion_skipped_candidate_ids"]) == 2
    state = _json.loads(state_path.read_text())
    skipped = [row for row in state["history"] if row.get("status") == "skipped"]
    assert skipped and all("cap" in row["reason"] for row in skipped)
    paused = [row for row in state["active_trials"].values() if row.get("promotion_paused")]
    assert [row["strategy"] for row in paused] == [held]
    assert "final holdout" in paused[0]["reason"]
    assert result["promotion_cap"] == {
        "max_promotions_per_cycle": 1, "max_promotions_per_strategy_per_cycle": 1,
    }
