from equity_research.validation.forward_validation import beats_live

LIVE = {"status": "passed", "lower_quartile_fitness": 0.10, "median_fitness": 0.30,
        "positive_fold_fraction": 0.7, "fold_count": 12, "median_net_return_pct": 1.0, "closed_trade_count": 40}


def _cand(**overrides):
    return {**LIVE, **overrides}


def test_candidate_that_ranks_above_live_is_promoted():
    ok, comparison = beats_live(_cand(lower_quartile_fitness=0.2), LIVE)
    assert ok and comparison["reason"].startswith("ranks above")


def test_lower_quartile_decides_before_median():
    assert not beats_live(_cand(lower_quartile_fitness=0.05, median_fitness=0.9), LIVE)[0]


def test_equal_or_worse_candidate_is_not_promoted():
    assert not beats_live(dict(LIVE), LIVE)[0]
    assert not beats_live(_cand(median_fitness=0.1), LIVE)[0]


def test_missing_live_or_unfinished_evidence_never_wins():
    assert not beats_live(_cand(), None)[0]
    assert not beats_live(_cand(), {**LIVE, "status": "insufficient_sample"})[0]
    assert not beats_live(_cand(median_fitness=None), LIVE)[0]
