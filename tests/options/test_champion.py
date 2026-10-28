from pathlib import Path

import pytest

from options.research.champion import (
    champion_candidate, decide_promotion, load_state, pool_with_champion, record_decision,
)
from options.research.search import Candidate, _candidate, _select_budget
from options.research.validation import FreshHoldoutUnavailable, make_purged_walk_forward


def _record(params, regimes=("range_bound",)):
    return {"parameters": params, "allowed_regimes": list(regimes)}


def test_decide_promotion_requires_approval():
    assert decide_promotion(approved=False, candidate_holdout_score=2.0, incumbent_holdout_score=None) == (
        False, "candidate did not pass the approval gate")


def test_decide_promotion_first_approved_candidate_becomes_champion():
    promoted, reason = decide_promotion(approved=True, candidate_holdout_score=0.4, incumbent_holdout_score=None)
    assert promoted and reason == "first approved candidate"


def test_decide_promotion_needs_holdout_to_beat_incumbent_by_margin():
    assert decide_promotion(approved=True, candidate_holdout_score=0.5, incumbent_holdout_score=0.4, margin=0.0)[0]
    assert not decide_promotion(approved=True, candidate_holdout_score=0.4, incumbent_holdout_score=0.4, margin=0.0)[0]
    assert not decide_promotion(approved=True, candidate_holdout_score=0.45, incumbent_holdout_score=0.4, margin=0.1)[0]


def test_champion_candidate_keeps_identity_and_is_pinned():
    pinned = champion_candidate(_record({"lookback": 10}))
    assert pinned.candidate_id == _candidate({"lookback": 10}, ["range_bound"], ()).candidate_id
    assert pinned.pinned and not pinned.is_baseline


def test_pool_with_champion_inserts_once_after_baseline():
    baseline = _candidate({}, ["range_bound"], (), baseline=True)
    other = _candidate({"lookback": 5}, ["range_bound"], (0,))
    pool = pool_with_champion([baseline, other], {"candidate_id": "x", **_record({"lookback": 10})})
    assert pool[0].is_baseline and pool[1].pinned
    same = _candidate({"lookback": 10}, ["range_bound"], (1,))
    pool2 = pool_with_champion([baseline, same], {"candidate_id": "x", **_record({"lookback": 10})})
    assert [item.pinned for item in pool2].count(True) == 1


def test_pool_without_champion_is_unchanged():
    baseline = _candidate({}, ["range_bound"], (), baseline=True)
    assert pool_with_champion([baseline], None) == [baseline]
    assert pool_with_champion([baseline], {"candidate_id": None}) == [baseline]


def test_select_budget_always_keeps_pinned_champion():
    pool = [_candidate({}, ["range_bound"], (), baseline=True)]
    pool += [_candidate({"lookback": i}, ["range_bound"], (i,)) for i in range(1, 20)]
    pool = pool_with_champion(pool, {"candidate_id": "x", **_record({"lookback": 99})})
    selected = _select_budget(pool, 4)
    assert any(item.pinned for item in selected) and any(item.is_baseline for item in selected)


def _decide(db: Path, *, run_id: str, promoted: bool, cand_id: str = "abc", params=None, end="2026-10-27"):
    cand = Candidate(cand_id, params or {"lookback": 5}, ("range_bound",), ())
    record_decision(
        strategy="vrp", underlying="SPY", run_id=run_id, decided_at=f"{end}T20:00:00+00:00",
        holdout_start="2026-08-01", holdout_end=end, candidate=cand,
        candidate_holdout_score=0.5, incumbent_candidate_id=None, incumbent_holdout_score=None,
        promoted=promoted, reason="test", path=db,
    )


def test_state_is_none_before_first_decision(tmp_path: Path):
    assert load_state("vrp", "SPY", tmp_path / "h.sqlite3") is None


def test_rejected_decision_rotates_holdout_but_keeps_no_champion(tmp_path: Path):
    db = tmp_path / "h.sqlite3"
    _decide(db, run_id="r1", promoted=False, end="2026-10-20")
    state = load_state("vrp", "SPY", db)
    assert state["candidate_id"] is None and state["holdout_end"] == "2026-10-20"


def test_promotion_persists_champion_and_round_trips_parameters(tmp_path: Path):
    db = tmp_path / "h.sqlite3"
    _decide(db, run_id="r1", promoted=True, cand_id="abc", params={"lookback": 5})
    _decide(db, run_id="r2", promoted=False, cand_id="def", params={"lookback": 7}, end="2026-11-10")
    state = load_state("vrp", "SPY", db)
    assert state["candidate_id"] == "abc"
    assert state["parameters"] == {"lookback": 5}
    assert state["holdout_end"] == "2026-11-10"  # rotated even though not promoted


def _observations(count: int):
    from datetime import datetime, timedelta
    from types import SimpleNamespace
    start = datetime(2026, 1, 1)
    return tuple(
        SimpleNamespace(as_of=start + timedelta(days=i), regime="range_bound", volatility_state="normal")
        for i in range(count)
    )


def test_fresh_holdout_is_only_sessions_after_previous_holdout():
    obs = _observations(400)
    config = {"walk_forward": {"rolling_train_sessions": 60, "oos_sessions": 20, "step_sessions": 20,
                               "minimum_oos_periods": 2, "final_holdout_sessions": 63, "embargo_sessions": 2}}
    section = {}
    _, holdout, split = make_purged_walk_forward(obs, strategy="vrp", strategy_section=section, config=config)
    assert len(holdout) == 63
    _, holdout2, split2 = make_purged_walk_forward(
        obs, strategy="vrp", strategy_section=section, config=config,
        holdout_after="2026-01-31", minimum_fresh_holdout_sessions=42,
    )
    assert all(item.as_of.date().isoformat() > "2026-01-31" for item in holdout2)
    assert len(holdout2) == 63 and split2["holdout_after"] == "2026-01-31"


def test_fresh_holdout_refuses_too_few_new_sessions():
    obs = _observations(100)
    config = {"walk_forward": {"rolling_train_sessions": 20, "oos_sessions": 10, "step_sessions": 10,
                               "minimum_oos_periods": 2, "final_holdout_sessions": 63, "embargo_sessions": 1}}
    with pytest.raises(FreshHoldoutUnavailable):
        make_purged_walk_forward(
            obs, strategy="vrp", strategy_section={}, config=config,
            holdout_after=obs[-10].as_of.date().isoformat(), minimum_fresh_holdout_sessions=42,
        )
