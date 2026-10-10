from datetime import date, timedelta

import pytest

from risk_manager import validate_trade
from src.algo.backtest import multi_factor_warmup_days
from src.algo.state_store import validate_portfolio
from src.algo.walk_forward import _read_profiles
from src.algo.strategies.multi_factor.diagnostics import information_coefficient, turnover
from src.config import STRATEGY_CONFIG, get_mode_config
from src.algo.strategies.multi_factor import (
    FactorRanker,
    MultiFactorConfig,
    MultiFactorSignal,
    SignalGenerator,
)
from src.algo.strategies.multi_factor.features import FactorCalculator
from src.algo.strategies.registry import validate_parameter_profile
from src.algo.strategies.regime import rank_strategies
from src.algo.strategies.runner import (
    _make_trade, _multi_factor_rebalance_signals, _multi_factor_satellite_signals,
)


def _daily_closes(symbol: str, *, count: int = 140, end: date = date(2026, 10, 2), growth: float = 0.001):
    bars = []
    day = end - timedelta(days=count * 2)
    close = 100.0
    while len(bars) < count:
        if day.weekday() < 5:
            close *= 1 + growth
            bars.append({"date": day.isoformat(), "c": close})
        day += timedelta(days=1)
    return bars


def test_factor_calculator_uses_only_data_available_as_of():
    config = MultiFactorConfig(
        min_universe=2,
        momentum_lookbacks={"momentum_1m": 5},
        volatility_lookback=10,
        factor_weights={"momentum_1m": 1},
    )
    calculator = FactorCalculator(config)
    as_of = date(2026, 1, 30)
    history = [
        {"date": (as_of - timedelta(days=days)).isoformat(), "c": 100 + days}
        for days in range(20, -1, -1)
    ]
    future = [{"date": "2026-02-02", "c": 10000}]

    before = calculator.calculate({"AAA": history}, symbols=["AAA"], as_of=as_of)
    after = calculator.calculate({"AAA": history + future}, symbols=["AAA"], as_of=as_of)

    assert before == after


def test_ranker_zscores_and_combines_configured_factors():
    ranker = FactorRanker(MultiFactorConfig(
        min_universe=3,
        factor_weights={"momentum_1m": 0.5, "low_volatility": 0.5},
    ))
    ranked = ranker.rank({
        "AAA": {"momentum_1m": 1.0, "low_volatility": 0.3},
        "BBB": {"momentum_1m": 0.0, "low_volatility": 0.2},
        "CCC": {"momentum_1m": -1.0, "low_volatility": 0.1},
    })

    assert [row["symbol"] for row in ranked] == ["AAA", "BBB", "CCC"]
    assert ranked[0]["factor_zscores"]["momentum_1m"] > 0
    assert ranked[0]["factor_zscores"]["low_volatility"] > 0
    assert ranked[0]["rank"] == 1


def test_signal_generator_emits_targets_and_sells_held_names_outside_ranked_universe():
    config = MultiFactorConfig(
        top_n=1,
        min_universe=3,
        momentum_lookbacks={"momentum_1m": 5},
        volatility_lookback=10,
        factor_weights={"momentum_1m": 1},
    )
    bars = {
        "AAA": _daily_closes("AAA", growth=0.01),
        "BBB": _daily_closes("BBB", growth=0.002),
        "CCC": _daily_closes("CCC", growth=-0.002),
    }

    signals, diagnostics = SignalGenerator(config).generate(
        bars, symbols=list(bars), as_of=date(2026, 10, 1),
        session=date(2026, 10, 2), held_positions={"OLD"},
    )
    by_symbol = {signal.ticker: signal for signal in signals}

    assert diagnostics["minimum_universe_met"] is True
    assert by_symbol["AAA"].action == "buy"
    assert by_symbol["AAA"].target_weight == pytest.approx(0.02)
    assert by_symbol["OLD"].action == "sell"
    assert all(signal.direction != "short" for signal in signals)


def test_regime_dependent_factor_satellite_confirms_weakest_names_bearishly():
    config = MultiFactorConfig(
        top_n=1,
        min_universe=3,
        momentum_lookbacks={"momentum_1m": 5},
        volatility_lookback=10,
        factor_weights={"momentum_1m": 1},
    )
    bars = {
        "AAA": _daily_closes("AAA", growth=0.01),
        "BBB": _daily_closes("BBB", growth=0.002),
        "CCC": _daily_closes("CCC", growth=-0.002),
    }

    signals, diagnostics = SignalGenerator(config).generate(
        bars, symbols=list(bars), as_of=date(2026, 10, 1),
        session=date(2026, 10, 2), regime="trending_down",
    )
    by_symbol = {signal.ticker: signal for signal in signals}

    assert diagnostics["selected_long_count"] == 0
    assert diagnostics["selected_short_count"] == 1
    assert by_symbol["CCC"].action == "short"
    assert by_symbol["CCC"].direction == "short"


def test_multi_factor_stays_available_as_a_bearish_satellite():
    spy = [
        {"t": f"2026-09-{index + 1:02d}T20:00:00Z", "c": 200 - index}
        for index in range(25)
    ]
    regime = rank_strategies(spy, mode="algo")

    assert regime["regime"] == "trending_down"
    assert "multi_factor" not in regime["active"]
    assert "multi_factor" in regime["satellites"]


def test_factor_satellite_direction_modes_are_explicit():
    assert MultiFactorConfig(direction_mode="long_only").direction_mode == "long_only"
    assert MultiFactorConfig(direction_mode="long_short").direction_mode == "long_short"
    with pytest.raises(ValueError, match="direction_mode"):
        MultiFactorConfig(direction_mode="auto")


def test_intraday_tilt_only_scales_an_existing_daily_factor_target():
    generator = SignalGenerator(MultiFactorConfig(enable_intraday_tilt=True))
    daily_target = MultiFactorSignal(
        ticker="AAPL", strategy="multi_factor", action="buy", direction="long",
        score=1.2, rank=1, target_weight=0.02, as_of="2026-10-01",
        rebalance_key="2026-W40", factor_values={}, factor_zscores={}, reason="daily rank",
    )
    adjusted = generator.apply_intraday_size_tilt(
        [daily_target],
        {"AAPL": [{"c": 100}, {"c": 100}, {"c": 99.5}]},
    )

    assert len(adjusted) == 1
    assert adjusted[0].action == "buy"
    assert adjusted[0].ticker == "AAPL"
    assert adjusted[0].target_weight == pytest.approx(0.01)
    assert generator.apply_intraday_size_tilt([], {"AAPL": [{"c": 100}]}) == []


@pytest.mark.parametrize("action", ["buy", "short"])
def test_generated_trade_proposal_still_passes_through_risk_manager(action):
    signal = {
        "ticker": "AAPL", "action": action, "strategy": "multi_factor",
        "reason": "weekly factor rank 1", "target_weight": 0.2,
    }
    risk = get_mode_config("algo")["risk"]
    trade = _make_trade(signal, 200, 100_000, {}, risk)
    assert trade is not None

    state = {
        "equity": 100_000, "cash": 100_000, "available_cash": 100_000,
        "daily_pnl_pct": 0, "weekly_pnl_pct": 0,
        "positions": {}, "sector_alloc": {}, "open_position_count": 0,
        "entry_count_today": 0, "entry_count_available": True,
        "pending_buy_value_by_ticker": {}, "pending_exposure_available": True,
        "shortability_available": True, "easy_to_borrow_symbols": ["AAPL"],
    }
    result = validate_trade(
        ticker=trade["ticker"], action=trade["action"], qty=trade["qty"],
        entry_price=trade["entry_price"], portfolio_state=state, mode="algo",
    )

    assert result.approved, result.reason
    assert trade["qty"] * trade["entry_price"] <= 100_000 * min(
        STRATEGY_CONFIG["max_strategy_position_pct"], risk["max_position_pct"],
    )


def test_value_factor_uses_only_fundamentals_available_by_as_of_date():
    config = MultiFactorConfig(
        min_universe=2,
        momentum_lookbacks={},
        volatility_lookback=10,
        factor_weights={"value_earnings_yield": 1},
    )
    calculator = FactorCalculator(config)
    fundamentals = {
        "AAA": [
            {"available_at": "2026-01-30T20:00:00Z", "earnings_yield": 0.05},
            {"available_at": "2026-02-02T14:00:00Z", "earnings_yield": 0.50},
        ],
    }

    values = calculator.calculate(
        {}, symbols=["AAA"], as_of=date(2026, 1, 30),
        fundamentals_by_symbol=fundamentals,
    )

    assert values == {"AAA": {"value_earnings_yield": 0.05}}


def test_multi_factor_profile_is_validated_with_the_algo_registry():
    profile = validate_parameter_profile({"multi_factor": {"top_n": 3}})
    assert profile["multi_factor"]["top_n"] == 3
    short_profile = validate_parameter_profile({"multi_factor": {"direction_mode": "long_short"}})
    assert short_profile["multi_factor"]["direction_mode"] == "long_short"


def test_warmup_covers_the_longest_configured_momentum_horizon():
    assert multi_factor_warmup_days() == 260
    assert multi_factor_warmup_days({
        "multi_factor": {
            "momentum_lookbacks": {"momentum_6m": 252},
            "factor_weights": {"momentum_6m": 1},
        },
    }) >= 373


def test_multi_factor_position_can_be_persisted_as_a_strategy_owner():
    validate_portfolio({
        "strategy_states": {
            "multi_factor": {
                "AAPL": {"phase": "open", "strategy": "multi_factor", "ticker": "AAPL"},
            },
        },
        "position_owners": {"AAPL": "multi_factor"},
    })


def test_multi_factor_walk_forward_example_profiles_are_isolated_and_valid():
    profiles = _read_profiles("autoresearch/multi-factor-profiles.example.json")

    assert [profile["name"] for profile in profiles] == [
        "baseline", "top_three", "daily_rebalance",
    ]
    assert all(profile["strategies"] == ["multi_factor"] for profile in profiles)


def test_factor_diagnostics_calculate_spearman_ic_and_target_turnover():
    assert information_coefficient(
        {"AAA": 1, "BBB": 2, "CCC": 3},
        {"AAA": -0.1, "BBB": 0.0, "CCC": 0.1},
    ) == pytest.approx(1.0)
    assert turnover({"AAA", "BBB"}, {"BBB", "CCC"}) == 0.5


def test_live_rebalance_ranks_cross_sectional_input_and_persists_weekly_key():
    symbols = [f"S{index:02d}" for index in range(20)]
    daily_bars = {
        symbol: _daily_closes(symbol, growth=(index + 1) * 0.0005)
        for index, symbol in enumerate(symbols)
    }
    states = {}
    session = date(2026, 10, 2)

    signals, diagnostics = _multi_factor_rebalance_signals(
        states, daily_bars, symbols, {}, session,
    )
    repeated, repeated_diagnostics = _multi_factor_rebalance_signals(
        states, daily_bars, symbols, {}, session,
    )

    assert diagnostics["minimum_universe_met"] is True
    assert sum(signal.action == "buy" for signal in signals.values()) == 3
    assert all(
        states["multi_factor"][symbol]["last_rebalance_period"] == diagnostics["rebalance_key"]
        for symbol in symbols
    )
    assert repeated == {}
    assert repeated_diagnostics is None


def test_bearish_factor_short_recommendation_persists_for_live_overlay():
    symbols = [f"S{index:02d}" for index in range(20)]
    daily_bars = {
        symbol: _daily_closes(symbol, growth=(index - 10) * 0.0003)
        for index, symbol in enumerate(symbols)
    }
    states = {}
    session = date(2026, 10, 2)
    signals, diagnostics = _multi_factor_rebalance_signals(
        states, daily_bars, symbols, {}, session, regime="trending_down",
    )

    assert diagnostics["selected_short_count"] > 0
    assert all(signal.action == "short" for signal in signals.values())
    confirmations = _multi_factor_satellite_signals(states, symbols, session)
    assert confirmations
    assert all(signal["action"] == "short" and signal["direction"] == "short" for signal in confirmations.values())


@pytest.mark.parametrize("values", [
    {"factor_weights": {"momentum_1m": float("nan")}},
    {"factor_weights": []},
    {"minimum_score": float("inf")},
    {"momentum_lookbacks": None},
])
def test_multi_factor_config_rejects_malformed_or_non_finite_values(values):
    with pytest.raises(ValueError):
        MultiFactorConfig.from_dict(values)
