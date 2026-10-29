"""A regime change blocks new factor entries but never exits a ranked holding."""

from datetime import date, timedelta

import pytest

from strategies.multi_factor import MultiFactorConfig, SignalGenerator


def _daily_closes(*, count: int = 140, end: date = date(2026, 10, 2), growth: float = 0.001):
    bars = []
    day = end - timedelta(days=count * 7 // 5 + 5)
    close = 100.0
    while len(bars) < count:
        if day.weekday() < 5:
            close *= 1 + growth
            bars.append({"date": day.isoformat(), "c": close})
        day += timedelta(days=1)
    return bars


def _config(**overrides) -> MultiFactorConfig:
    values = dict(
        top_n=1,
        min_universe=3,
        min_names_per_side=1,
        momentum_lookbacks={"momentum_1m": 5},
        volatility_lookback=10,
        factor_weights={"momentum_1m": 1},
    )
    values.update(overrides)
    return MultiFactorConfig(**values)


BARS = {
    "AAA": _daily_closes(growth=0.01),
    "BBB": _daily_closes(growth=0.002),
    "CCC": _daily_closes(growth=-0.002),
}
HELD = {"AAA": {"qty": 10}, "CCC": {"qty": -10}}


@pytest.mark.parametrize("regime", ["volatile", "trending_down"])
def test_held_ranked_names_are_not_sold_when_the_regime_blocks_their_side(regime):
    config = _config(direction_mode="regime_dependent") if regime != "volatile" else _config()
    signals, diagnostics = SignalGenerator(config).generate(
        BARS, symbols=list(BARS), as_of=date(2026, 10, 1),
        session=date(2026, 10, 2), held_positions=HELD, regime=regime,
    )
    by_symbol = {signal.ticker: signal for signal in signals}

    assert not any(signal.action in {"buy", "short"} for signal in signals)
    assert by_symbol["AAA"].action == "hold"
    assert by_symbol["CCC"].action == "hold"
    assert diagnostics["volatile_regime_disabled"] is (regime == "volatile")


def test_held_name_that_leaves_the_ranking_still_exits_under_a_volatile_regime():
    bars = {**BARS, "AAA": _daily_closes(growth=-0.02)}
    signals, _ = SignalGenerator(_config()).generate(
        bars, symbols=list(bars), as_of=date(2026, 10, 1),
        session=date(2026, 10, 2), held_positions={"AAA": {"qty": 10}}, regime="volatile",
    )
    by_symbol = {signal.ticker: signal for signal in signals}

    assert by_symbol["AAA"].action == "sell"
    assert "AAA" in by_symbol


def test_neutral_regime_keeps_the_same_entries_as_before():
    signals, _ = SignalGenerator(_config()).generate(
        BARS, symbols=list(BARS), as_of=date(2026, 10, 1),
        session=date(2026, 10, 2), held_positions={}, regime="range_bound",
    )
    by_symbol = {signal.ticker: signal for signal in signals}

    assert by_symbol["AAA"].action == "buy"
    assert by_symbol["CCC"].action == "short"
