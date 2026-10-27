"""Route weights choose eligible strategies; risk_scale alone sets position size."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta

import pytest

from portfolio import config as portfolio_config
from strategies.regime import rank_strategies


def _spy_bars(count: int = 160, drift: float = 0.6) -> list[dict]:
    start = date(2025, 1, 1)
    rows = []
    price = 100.0
    for index in range(count):
        move = drift + ((index % 7) - 3) * 0.08
        opening = price
        price = max(1.0, price + move)
        rows.append({
            "t": (start + timedelta(days=index)).isoformat(),
            "o": opening, "h": max(opening, price) + 0.5,
            "l": min(opening, price) - 0.5, "c": price, "v": 100_000 + index,
        })
    return rows


@pytest.fixture
def default_regime_config(monkeypatch, tmp_path):
    """The shipped defaults, with the live override file ignored."""
    monkeypatch.setattr(portfolio_config, "ALGO_STRATEGY_CONFIG_PATH", tmp_path / "absent.json")
    return portfolio_config.get_algo_regime_config()


def _rank(config: dict, *, route: dict | None = None, scales: dict | None = None, bars=None) -> dict:
    return rank_strategies(
        bars if bars is not None else _spy_bars(),
        mode="algo",
        engine="rule",
        feature_set="technical",
        strategy_policy="configured",
        strategy_regime_weights=route if route is not None else config["strategy_regime_weights"],
        strategy_enabled={name: True for name in config["strategy_enabled"]},
        strategy_risk_scales=scales if scales is not None else config["strategy_risk_scales"],
        volatile_policy_risk_multiplier=config["volatile_policy_risk_multiplier"],
        volatility_risk_scales=config["volatility_risk_scales"],
        intraday_trend_enabled=False,
        intraday_momentum_enabled=False,
    )


def test_editing_a_route_weight_changes_eligibility_but_not_size(default_regime_config):
    route = deepcopy(default_regime_config["strategy_regime_weights"])
    base = _rank(default_regime_config)
    assert base["regime"] in {"trending_up", "trending_down"}
    assert "swing_momentum" in base["active"]

    route["swing_momentum"] = {name: (0.3 if value else 0) for name, value in route["swing_momentum"].items()}
    edited = _rank(default_regime_config, route=route)

    assert "swing_momentum" in edited["active"]
    assert edited["strategy_risk_multipliers"]["swing_momentum"] == base["strategy_risk_multipliers"]["swing_momentum"]

    route["swing_momentum"] = {name: 0 for name in route["swing_momentum"]}
    removed = _rank(default_regime_config, route=route)
    assert "swing_momentum" not in removed["active"]
    assert "swing_momentum" not in removed["strategy_risk_multipliers"]


def test_risk_multiplier_is_volatility_scale_times_strategy_risk_scale(default_regime_config):
    result = _rank(default_regime_config)
    budget = result["risk_budget_multiplier"]

    assert result["strategy_risk_multipliers"]
    for name, multiplier in result["strategy_risk_multipliers"].items():
        expected = min(1.5, budget * default_regime_config["strategy_risk_scales"][name])
        assert multiplier == pytest.approx(expected, abs=1e-4), name


def test_missing_risk_scale_defaults_to_one_and_scales_size_linearly(default_regime_config):
    unscaled = _rank(default_regime_config, scales={})
    halved = _rank(
        default_regime_config,
        scales={"swing_momentum": 0.5},
    )
    assert unscaled["strategy_risk_multipliers"]["swing_momentum"] == pytest.approx(
        unscaled["risk_budget_multiplier"], abs=1e-4,
    )
    assert halved["strategy_risk_multipliers"]["swing_momentum"] == pytest.approx(
        unscaled["strategy_risk_multipliers"]["swing_momentum"] * 0.5, abs=1e-4,
    )


def test_migrated_default_scales_equal_the_route_weights_they_replace(default_regime_config):
    route = default_regime_config["strategy_regime_weights"]
    scales = default_regime_config["strategy_risk_scales"]
    assert set(scales) == set(route)
    for name, regimes in route.items():
        active_weights = {float(value) for value in regimes.values() if float(value) > 0}
        if name == "swing_multi_factor":
            # Its route is 0.75 in trends and 0.65 in ranges; one scalar must pick one.
            # The conservative 0.65 is the deliberate size change this migration makes.
            assert active_weights == {0.65, 0.75}
            assert scales[name] == pytest.approx(0.65)
        else:
            assert active_weights, name
            assert scales[name] == pytest.approx(active_weights.pop()), name


def test_risk_scale_outside_the_supported_range_is_rejected(default_regime_config):
    with pytest.raises(ValueError, match="risk_scale"):
        _rank(default_regime_config, scales={"swing_momentum": 0})
    with pytest.raises(ValueError, match="risk_scale"):
        _rank(default_regime_config, scales={"swing_momentum": 2.0})
