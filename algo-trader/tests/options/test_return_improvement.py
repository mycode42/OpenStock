from options.research.automation import _return_improvement


def test_relative_improvement_when_baseline_is_positive():
    assert _return_improvement(0.10, 0.15) == "+50.0%"


def test_gap_in_points_when_baseline_is_not_positive():
    assert _return_improvement(-0.02, 0.01) == "n/a (baseline -2.00%; +3.00 pts)"


def test_missing_metrics_are_not_reported_as_improvement():
    assert _return_improvement(None, 0.10) == "n/a"
