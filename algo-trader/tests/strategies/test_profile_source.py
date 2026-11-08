"""One source per parameter: the position target and the research risk config follow the settings file, or a lab run's values."""

import json

import pytest

from portfolio.config import get_algo_configuration, get_mode_config
from strategies import registry


@pytest.fixture(autouse=True)
def restore_registry(monkeypatch):
    monkeypatch.delenv("ALGO_LAB_PARAMETER_OVERRIDES", raising=False)
    yield
    monkeypatch.delenv("ALGO_LAB_PARAMETER_OVERRIDES", raising=False)
    registry.refresh_active_parameter_profile()


def test_live_target_is_the_settings_value_and_unchanged_from_before():
    settings = get_algo_configuration()["strategies"]
    assert registry.ALGO_DEFAULT_PARAMETERS["swing_momentum"]["max_position_pct"] == settings["swing_momentum"]["max_position_pct"] == 0.04
    assert registry.ALGO_DEFAULT_PARAMETERS["swing_multi_factor"]["max_position_pct"] == settings["swing_multi_factor"]["max_position_pct"] == 0.02


def test_a_lab_run_uses_its_profile_for_target_cap_and_stops_and_live_does_not(monkeypatch, tmp_path):
    profile = {
        "max_position_pct": 0.052, "risk_multiplier": 1.05, "hard_stop_pct": 0.072,
        "trailing_stop_pct": 0.054, "trailing_activation_pct": 0.024,
    }
    overrides = tmp_path / "overrides.json"
    overrides.write_text(json.dumps({"swing_momentum": profile}), encoding="utf-8")
    monkeypatch.setenv("ALGO_LAB_PARAMETER_OVERRIDES", str(overrides))
    registry.refresh_active_parameter_profile()
    assert registry.ALGO_DEFAULT_PARAMETERS["swing_momentum"]["max_position_pct"] == 0.052
    risk = get_mode_config("algo")["risk"]
    assert risk["max_position_pct_by_strategy"]["swing_momentum"] == 0.052
    assert risk["risk_multiplier_by_strategy"]["swing_momentum"] == 1.05
    assert risk["stop_loss_pct_by_strategy"]["swing_momentum"] == 0.072
    assert risk["trailing_stop_pct_by_strategy"]["swing_momentum"] == 0.054

    monkeypatch.delenv("ALGO_LAB_PARAMETER_OVERRIDES")
    registry.refresh_active_parameter_profile()
    live = get_mode_config("algo")["risk"]
    assert live["max_position_pct_by_strategy"]["swing_momentum"] == 0.04
    assert live["stop_loss_pct_by_strategy"]["swing_momentum"] == get_algo_configuration()["strategies"]["swing_momentum"]["hard_stop_pct"]
