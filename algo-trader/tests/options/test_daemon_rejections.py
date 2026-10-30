"""Each rejected options candidate is logged with its reason; approved ones are not."""

from options.daemon import _rejection_lines


def _row(approved, reason=None, underlying="SPY"):
    return {
        "approved": approved,
        "reason": reason,
        "signal": {"underlying": underlying, "strategy": "vrp", "structure_type": "iron_condor"},
    }


def test_rejected_candidates_are_listed_with_their_reason_and_approved_ones_are_not():
    report = {"decisions": [_row(True, None, "AAPL"), _row(False, "VRP daily entry limit reached", "SPY")]}
    lines = _rejection_lines(report)
    assert len(lines) == 1
    assert "underlying=SPY" in lines[0]
    assert "reason=VRP daily entry limit reached" in lines[0]


def test_a_rejection_without_a_reason_says_so_and_long_lists_are_capped():
    assert "no reason recorded" in _rejection_lines({"decisions": [_row(False)]})[0]
    many = {"decisions": [_row(False, f"r{i}") for i in range(12)]}
    lines = _rejection_lines(many)
    assert len(lines) == 11
    assert lines[-1].startswith("Options candidate rejections truncated: 2 more")
