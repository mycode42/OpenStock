from pathlib import Path

from options.research.champion import (
    champion_candidate, decide_promotion, load_state, pool_with_champion, record_decision,
)
from options.research.search import Candidate, _candidate, _select_budget
from runtime.fold_ranking import beats, fold_summary

LIVE = {"status": "passed", "lower_quartile_fitness": 0.1, "median_fitness": 0.3, "positive_fold_fraction": 0.7}


def _record(params, regimes=("range_bound",)):
    return {"parameters": params, "allowed_regimes": list(regimes)}


def test_decide_promotion_requires_approval():
    assert decide_promotion(approved=False, candidate_summary=LIVE, incumbent_summary=None, has_incumbent=False) == (
        False, "candidate did not pass the approval gate")


def test_first_approved_candidate_becomes_champion():
    promoted, reason = decide_promotion(approved=True, candidate_summary=LIVE, incumbent_summary=None, has_incumbent=False)
    assert promoted and reason == "first approved candidate"


def test_candidate_must_rank_above_incumbent_on_same_folds():
    better = {**LIVE, "lower_quartile_fitness": 0.2}
    assert decide_promotion(approved=True, candidate_summary=better, incumbent_summary=LIVE, has_incumbent=True)[0]
    assert not decide_promotion(approved=True, candidate_summary=LIVE, incumbent_summary=LIVE, has_incumbent=True)[0]
    worse = {**LIVE, "median_fitness": 0.1}
    assert not decide_promotion(approved=True, candidate_summary=worse, incumbent_summary=LIVE, has_incumbent=True)[0]


def test_fold_summary_matches_equity_ranking_inputs():
    summary = fold_summary([0.5, -0.2, 0.1, 0.4])
    assert summary["fold_count"] == 4
    assert summary["lower_quartile_fitness"] == -0.2  # index int(3*0.25)=0 on sorted scores
    assert summary["positive_fold_fraction"] == 0.75
    assert fold_summary([])["median_fitness"] is None


def test_beats_is_shared_and_rejects_missing_evidence():
    assert beats({**LIVE, "lower_quartile_fitness": 0.2}, LIVE)[0]
    assert not beats(LIVE, {"status": "insufficient_sample"})[0]
    assert not beats({**LIVE, "median_fitness": None}, LIVE)[0]


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
    assert pool_with_champion([baseline], {"candidate_id": None}) == [baseline]


def test_select_budget_always_keeps_pinned_champion():
    pool = [_candidate({}, ["range_bound"], (), baseline=True)]
    pool += [_candidate({"lookback": i}, ["range_bound"], (i,)) for i in range(1, 20)]
    pool = pool_with_champion(pool, {"candidate_id": "x", **_record({"lookback": 99})})
    selected = _select_budget(pool, 4)
    assert any(item.pinned for item in selected) and any(item.is_baseline for item in selected)


def _decide(db: Path, *, run_id: str, promoted: bool, cand_id: str = "abc", params=None):
    record_decision(
        strategy="vrp", underlying="SPY", run_id=run_id, decided_at="2026-10-28T20:00:00+00:00",
        candidate=Candidate(cand_id, params or {"lookback": 5}, ("range_bound",), ()),
        candidate_summary=LIVE, incumbent_candidate_id=None, incumbent_summary=None,
        promoted=promoted, reason="test", path=db,
    )


def test_state_is_none_before_first_promotion(tmp_path: Path):
    assert load_state("vrp", "SPY", tmp_path / "h.sqlite3") is None


def test_rejected_decision_keeps_no_champion(tmp_path: Path):
    db = tmp_path / "h.sqlite3"
    _decide(db, run_id="r1", promoted=False)
    assert load_state("vrp", "SPY", db) is None


def test_promotion_persists_champion_and_replaces_it(tmp_path: Path):
    db = tmp_path / "h.sqlite3"
    _decide(db, run_id="r1", promoted=True, cand_id="abc", params={"lookback": 5})
    _decide(db, run_id="r2", promoted=False, cand_id="def", params={"lookback": 7})
    state = load_state("vrp", "SPY", db)
    assert state["candidate_id"] == "abc" and state["parameters"] == {"lookback": 5}
    _decide(db, run_id="r3", promoted=True, cand_id="ghi", params={"lookback": 9})
    assert load_state("vrp", "SPY", db)["candidate_id"] == "ghi"
