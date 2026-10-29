import pytest

from options.research import search
from options.research.search import Candidate


GRID = {"min_iv_minus_rv": [0.03, 0.05, 0.07], "wing_delta_max": [0.05, 0.10]}
CONFIG = {
    "search": {
        "maximum_candidates": 6,
        "mode": "surrogate",
        "grids": {"vrp": GRID},
        "regime_sets": [["range_bound"], ["trending_up"]],
    },
}
BASE_CONFIG = {"vrp": {"allowed_regimes": ["range_bound"]}}


def _pool():
    baseline = Candidate("base", {"min_iv_minus_rv": 0.05, "wing_delta_max": 0.05}, ("range_bound",), (0, 1, 0), is_baseline=True)
    grid_candidates = [
        Candidate("c0", {"min_iv_minus_rv": 0.03, "wing_delta_max": 0.10}, ("range_bound",), (0, 0, 1)),
        Candidate("c1", {"min_iv_minus_rv": 0.07, "wing_delta_max": 0.05}, ("trending_up",), (1, 2, 0)),
        Candidate("c2", {"min_iv_minus_rv": 0.05, "wing_delta_max": 0.10}, ("trending_up",), (1, 1, 1)),
        Candidate("c3", {"min_iv_minus_rv": 0.07, "wing_delta_max": 0.10}, ("range_bound",), (0, 2, 1)),
    ]
    # A champion restored from a previous approval has parameters but no grid position.
    champion = Candidate("champion", {"min_iv_minus_rv": 0.07, "wing_delta_max": 0.10}, ("trending_up",), (), pinned=True)
    return [baseline, *grid_candidates, champion]


def _evaluator(candidate):
    return {"risk_adjusted_score": round(sum(candidate.grid_indices or (0,)) / 10, 3)}


@pytest.mark.parametrize("compiled", [True, False], ids=["cython", "python"])
def test_pinned_champion_without_grid_position_is_scored_in_both_paths(monkeypatch, compiled):
    if compiled and search._search_cy is None:
        pytest.skip("compiled surrogate extension is not built")
    if not compiled:
        monkeypatch.setattr(search, "_search_cy", None)

    rows = search.evaluate_candidates(
        "vrp", CONFIG, BASE_CONFIG, _evaluator,
        include_candidate_objects=True, candidate_pool_override=_pool(),
    )

    ids = [row["candidate"]["candidate_id"] for row in rows]
    assert len(rows) == CONFIG["search"]["maximum_candidates"]
    assert "champion" in ids
    assert len(set(ids)) == len(ids)
