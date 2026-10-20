from datetime import date, datetime, timedelta, timezone

import pytest

from options.assignment import simulate_early_assignment
from options.models import GreeksSnapshot, OptionContract, OptionLeg
from options.research.dataset import ResearchObservation
from options.research.validation import make_purged_walk_forward
from options.risk import PositionExposure, summarize_portfolio
from options.structures import vertical_spread


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

    folds, holdout, split = make_purged_walk_forward(
        observations,
        strategy="vrp",
        strategy_section={"realized_lookback_sessions": 30, "max_dte": 40},
        config={
            "strategy_defaults": {"vrp": {"max_dte": 35}},
            "walk_forward": {
                "rolling_train_sessions": 100, "oos_sessions": 20, "step_sessions": 20,
                "minimum_oos_periods": 4, "final_holdout_sessions": 100, "embargo_sessions": 5,
                "minimum_holdout_regime_labels": 2, "minimum_high_volatility_holdout_sessions": 5,
            },
        },
    )

    assert len(folds) >= 4
    assert split["purge_sessions"] == 45
    assert all(fold.oos_start - fold.train_end == 45 for fold in folds)
    assert all(folds[index].oos_end <= folds[index + 1].oos_start for index in range(len(folds) - 1))
    assert len(holdout) == 100
