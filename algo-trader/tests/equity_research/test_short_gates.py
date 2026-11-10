from types import SimpleNamespace

from equity_research.backtest.signals import ReplaySignalsMixin


def _stub(gates, regime="trending_up"):
    return SimpleNamespace(
        parameters_for_regime=lambda strategy, regime_name: {"short_entry_gates": gates},
        _current_regime={"regime": regime},
    )


def _short(strength=0.7, **features):
    return {"action": "short", "strategy": "weekly_momentum", "strength": strength,
            "features": sorted(features.items())}


def test_each_gate_rejects_a_short_that_fails_it():
    check = ReplaySignalsMixin._short_entry_rejection
    assert check(_stub({"min_strength": 0.8}), "weekly_momentum", _short(0.7, ret_70=-0.1)) == "short_below_minimum_strength"
    assert check(_stub({"min_ret_70": -0.05}), "weekly_momentum", _short(ret_70=-0.02)) == "short_insufficient_decline"
    assert check(_stub({"min_ret_70": -0.05}), "weekly_momentum", _short()) == "short_insufficient_decline"
    assert check(_stub({"min_stretch_70": 0.03}), "weekly_momentum", _short(stretch_70=0.01)) == "short_insufficient_stretch"
    assert check(_stub({"allowed_regimes": ["range_bound"]}), "weekly_momentum", _short(ret_70=-0.1)) == "short_regime_not_allowed"


def test_a_short_that_passes_every_gate_is_allowed_and_no_gates_means_no_change():
    check = ReplaySignalsMixin._short_entry_rejection
    gates = {"min_strength": 0.6, "min_ret_70": -0.05, "min_stretch_70": 0.0, "allowed_regimes": ["trending_up"]}
    assert check(_stub(gates), "weekly_momentum", _short(0.7, ret_70=-0.1, stretch_70=0.02)) is None
    assert check(_stub({}), "weekly_momentum", _short()) is None
