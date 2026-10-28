from datetime import date, datetime, timedelta, timezone

import pytest

import json

from options.assignment import simulate_early_assignment
from options.config import load_options_config
from options.models import GreeksSnapshot, OptionContract, OptionLeg, OptionSignal
from options.research.dataset import ResearchObservation
from options.research.validation import make_purged_walk_forward
from options.risk import PortfolioRiskManager, PositionExposure, summarize_portfolio
from options.structures import collar, vertical_spread


EXPIRATION = date(2027, 1, 15)


def _contract(symbol: str, right: str, strike: float) -> OptionContract:
    return OptionContract(
        symbol=symbol, underlying="SPY", expiration=EXPIRATION,
        right=right, strike=strike, exercise_style="american",
    )


def test_vertical_debit_spread_has_bounded_loss_and_aggregated_greeks():
    long_call = OptionLeg(
        _contract("SPY270115C00100000", "call", 100), "buy",
        bid=4.0, ask=5.0,
        greeks=GreeksSnapshot(delta=0.65, gamma=0.02, theta=-0.04, vega=0.12),
    )
    short_call = OptionLeg(
        _contract("SPY270115C00105000", "call", 105), "sell",
        bid=1.0, ask=2.0,
        greeks=GreeksSnapshot(delta=0.35, gamma=0.015, theta=-0.03, vega=0.10),
    )

    spread = vertical_spread(long_call, short_call)

    assert spread.structure_type == "vertical_debit"
    assert spread.net_debit_per_share == pytest.approx(3.0)
    assert spread.risk.max_loss == pytest.approx(300.0)
    assert spread.risk.max_profit == pytest.approx(200.0)
    assert spread.greeks.delta == pytest.approx(30.0)
    assert spread.greeks.gamma == pytest.approx(0.5)
    assert spread.greeks.theta == pytest.approx(-1.0)
    assert spread.greeks.vega == pytest.approx(2.0)


def test_portfolio_risk_aggregates_equity_and_option_greeks():
    summary = summarize_portfolio(
        combined_equity=100_000,
        options_equity=25_000,
        positions=[
            PositionExposure("SPY", 500, delta_shares=10, gamma=0.01, theta_per_day=-2,
                             vega_per_volatility=50, max_loss=100, margin=200,
                             assignment_shares=100, source="equity"),
            PositionExposure("SPY", 500, delta_shares=-4, gamma=0.02, theta_per_day=1,
                             vega_per_volatility=-20, max_loss=250, margin=300,
                             assignment_shares=100, source="option"),
        ],
        stress_spot_shocks=(0.05, 0.10),
        stress_iv_shock=0.10,
    )

    assert summary.delta_by_underlying["SPY"] == pytest.approx(6)
    assert summary.delta_notional_by_underlying["SPY"] == pytest.approx(3_000)
    assert summary.vega_per_vol_point_by_underlying["SPY"] == pytest.approx(0.3)
    assert summary.theta_per_day_by_underlying["SPY"] == pytest.approx(-1)
    assert summary.max_loss_at_risk == pytest.approx(350)
    assert summary.estimated_margin == pytest.approx(500)
    assert summary.assignment_shares == 200


def test_american_short_call_assignment_heuristic_accounts_for_dividend():
    call = OptionLeg(
        _contract("SPY270115C00095000", "call", 95), "sell",
        bid=4.98, ask=5.02,
    )

    event = simulate_early_assignment(
        call, contracts=1, underlying_price=100, as_of=date(2026, 12, 1),
        expected_dividend_per_share=0.25,
    )

    assert event is not None
    assert event.assigned_contracts == 1
    assert event.resulting_share_change == -100
    assert event.reason == "short_call_ex_dividend_early_exercise"


def test_rolling_walk_forward_purges_maximum_lookback_dte_and_embargo():
    first = datetime(2020, 1, 1, tzinfo=timezone.utc)
    observations = tuple(
        ResearchObservation(
            as_of=first + timedelta(days=index), underlying="SPY", spot=100 + index / 100,
            regime="range_bound" if index % 2 else "trending_down",
            volatility_state="high_vol" if 500 <= index < 510 else "normal",
            feed="historical", events=(), equity_signals={}, event_recommendations={},
            dividends={}, source_line=index + 1,
        )
        for index in range(600)
    )

    folds, split = make_purged_walk_forward(
        observations,
        strategy="vrp",
        strategy_section={"realized_lookback_sessions": 30, "max_dte": 40},
        config={
            "strategy_defaults": {"vrp": {"max_dte": 35}},
            "walk_forward": {
                "rolling_train_sessions": 100, "oos_sessions": 20, "step_sessions": 20,
                "minimum_oos_periods": 4, "embargo_sessions": 5,
            },
        },
    )

    assert len(folds) >= 4
    assert split["purge_sessions"] == 45
    assert all(fold.oos_start - fold.train_end == 45 for fold in folds)
    assert all(folds[index].oos_end <= folds[index + 1].oos_start for index in range(len(folds) - 1))
    assert len(observations) - folds[-1].oos_end < 20  # no full OOS window is left unused


def _european_contract(symbol: str, right: str, strike: float) -> OptionContract:
    return OptionContract(
        symbol=symbol, underlying="SPY", expiration=EXPIRATION,
        right=right, strike=strike, exercise_style="european",
    )


def _risk_config(tmp_path, **overrides) -> dict:
    # Missing override path keeps the test on packaged defaults, not local user config.
    risk = dict(load_options_config(tmp_path / "missing.json")["risk"])
    risk.update({
        "max_contracts_per_trade": 10,
        "max_risk_per_trade_pct": 0.05,
        "max_total_defined_risk_pct": 1.0,
        "max_margin_pct": 1.0,
        "max_delta_notional_pct": 10.0,
        "max_vega_per_vol_point_pct": 10.0,
        "max_gamma_1pct_loss_pct": 10.0,
        "max_theta_loss_per_day_pct": 10.0,
        "max_assignment_shares": 10_000,
        "stress_gate_loss_pct": 1.0,
    })
    risk.update(overrides)
    return risk


def _covered_call_signal(contracts: int = 3) -> OptionSignal:
    put = OptionLeg(
        _european_contract("SPY270115P00095000", "put", 95), "buy",
        bid=2.0, ask=2.2, greeks=GreeksSnapshot(delta=-0.2, gamma=0.01, theta=-0.02, vega=0.1),
    )
    call = OptionLeg(
        _european_contract("SPY270115C00105000", "call", 105), "sell",
        bid=1.0, ask=1.2, greeks=GreeksSnapshot(delta=0.3, gamma=-0.01, theta=-0.02, vega=-0.1),
    )
    structure = collar(put, call, shares=100, share_cost_basis=100.0)
    structure.metadata["underlying_spot"] = 100.0
    return OptionSignal(
        strategy="options_test", underlying="SPY", structure=structure,
        desired_contracts=contracts, confidence=0.8, reason="covered call test", regime="range_bound",
    )


def _manager(tmp_path, **overrides) -> PortfolioRiskManager:
    return PortfolioRiskManager({**_risk_config(tmp_path), **overrides})


def test_open_short_call_is_not_double_counted_as_covered_shares(tmp_path):
    manager = _manager(tmp_path)
    held_shares = PositionExposure("SPY", 100, delta_shares=300, source="broker_equity_position")
    open_short_call = PositionExposure(
        "SPY", 100, delta_shares=-50, short_call_shares=100, source="broker_option_position",
    )
    kwargs = dict(
        combined_equity=1_000_000, options_equity=1_000_000,
        equity_positions=[], options_equity_positions=[held_shares],
        option_positions=[open_short_call], existing_option_structures=1,
    )

    decision = manager.size_and_validate(_covered_call_signal(contracts=3), **kwargs)

    # 300 shares minus one open call's 100 shares leave room for two covered calls, not three.
    assert decision.approved is True
    assert decision.contracts == 2

    no_spare_shares = manager.size_and_validate(
        _covered_call_signal(contracts=1),
        **{**kwargs, "options_equity_positions": [PositionExposure("SPY", 100, delta_shares=100)]},
    )
    assert no_spare_shares.approved is False
    assert "shares required to cover" in no_spare_shares.reason


def test_candidate_covered_call_counts_its_short_call_shares(tmp_path):
    manager = _manager(tmp_path)
    exposure = manager._candidate_exposure(_covered_call_signal(contracts=2).structure, 2)

    assert exposure.short_call_shares == 200


def test_stress_gate_uses_configured_limit(tmp_path):
    kwargs = dict(
        combined_equity=1_000_000, options_equity=1_000_000,
        equity_positions=[], options_equity_positions=[PositionExposure("SPY", 100, delta_shares=300)],
        option_positions=[], existing_option_structures=0,
    )
    tight = _manager(tmp_path, stress_gate_loss_pct=0.000001)
    decision = tight.size_and_validate(_covered_call_signal(), **kwargs)

    assert decision.approved is False
    assert "stress loss" in decision.reason


def test_stress_config_rejects_invalid_shocks(tmp_path):
    bad = tmp_path / "options.json"
    bad.write_text(json.dumps({"risk": {"stress_spot_shocks": [0.10, 0.05]}}), encoding="utf-8")
    with pytest.raises(ValueError, match="stress_spot_shocks"):
        load_options_config(bad)

    bad.write_text(json.dumps({"risk": {"stress_gate_loss_pct": 0}}), encoding="utf-8")
    with pytest.raises(ValueError, match="stress_gate_loss_pct"):
        load_options_config(bad)
