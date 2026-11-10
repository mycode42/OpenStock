import pytest

from strategies.registry import validate_parameter_profile
from strategies.rule_pipeline import entry_features


def _bars(values, volumes=None):
    volumes = volumes or [100.0] * len(values)
    return [{"t": f"2026-01-{i + 1:02d}T15:00:00+00:00", "c": c, "h": c, "l": c, "v": v}
            for i, (c, v) in enumerate(zip(values, volumes))]


def test_features_measure_the_return_and_stretch_at_the_last_bar():
    values = [100.0] * 70 + [110.0]
    found = dict(entry_features(_bars(values)))
    assert found["ret_70"] == pytest.approx(110.0 / 100.0 - 1)
    average = (100.0 * 69 + 110.0) / 70
    assert found["stretch_70"] == pytest.approx(110.0 / average - 1)


def test_features_are_left_out_when_history_is_short():
    found = dict(entry_features(_bars([100.0] * 10)))
    assert "ret_70" not in found and "stretch_70" not in found
    assert "ret_20" not in found


def test_volume_ratio_compares_the_last_bar_with_the_19_before_it():
    volumes = [100.0] * 19 + [300.0]
    found = dict(entry_features(_bars([100.0] * 20, volumes)))
    assert found["volume_ratio"] == pytest.approx(3.0)


def test_short_gate_settings_are_validated_and_kept_with_the_strategy():
    profile = validate_parameter_profile({
        "weekly_momentum": {"short_entry_gates": {"min_ret_70": -0.05, "allowed_regimes": ["trending_up"]}},
    })
    assert profile["weekly_momentum"]["short_entry_gates"] == {
        "min_ret_70": -0.05, "allowed_regimes": ["trending_up"],
    }
    with pytest.raises(ValueError):
        validate_parameter_profile({"weekly_momentum": {"short_entry_gates": {"min_ret_70": 0.05}}})
    with pytest.raises(ValueError):
        validate_parameter_profile({"weekly_momentum": {"short_entry_gates": {"unknown": 1}}})
