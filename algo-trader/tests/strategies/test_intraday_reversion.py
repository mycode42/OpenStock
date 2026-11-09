"""Intraday mean reversion: dips and spikes against the 21-bar average, with a slope filter."""

from strategies.rule_pipeline import evaluate_rule_signal

PARAMS = {"signal_model": "average_24h", "minimum_dip_pct": 0.01, "minimum_rip_pct": 0.01,
          "minimum_bounce_pct": 0.001, "minimum_volume_ratio": 1.2, "allow_long": True, "allow_short": True}


def _bar(index, close, low=None, high=None, volume=1000):
    low = close if low is None else low
    high = close if high is None else high
    minute = 30 + 5 * index
    day = 9 + (minute // 60)
    return {"t": f"2026-11-09T{day:02d}:{minute % 60:02d}:00Z", "o": close, "h": high, "l": low, "c": close, "v": volume}


def _flat_history(level):
    return [_bar(i, level) for i in range(26)]


def test_dip_below_the_21_bar_average_with_a_higher_low_and_volume_is_a_buy():
    bars = _flat_history(100.0)
    bars.append(_bar(26, 97.0, low=96.8, high=97.2))         # the bar before the signal bar
    bars.append(_bar(27, 97.5, low=97.0, high=97.8, volume=3000))  # dip, higher low, close above previous
    signal = evaluate_rule_signal("intraday_mean_reversion", "AAPL", bars, parameters=PARAMS)
    assert signal is not None
    assert signal.action == "buy"
    assert "21-bar average" in signal.reason


def test_spike_above_the_average_is_a_short_when_allowed():
    bars = _flat_history(100.0)
    bars.append(_bar(26, 103.0, low=102.8, high=103.2))
    bars.append(_bar(27, 102.5, low=102.2, high=103.0, volume=3000))  # rip, lower high, close below previous
    signal = evaluate_rule_signal("intraday_mean_reversion", "AAPL", bars, parameters=PARAMS)
    assert signal is not None
    assert signal.action == "short"


def test_a_steep_move_of_the_reference_is_not_faded():
    # The reference drifts from 110 down to 97 over the slope window: a trend, so the slope filter blocks the dip.
    bars = [_bar(i, 110.0 - 0.5 * i) for i in range(20)] + [_bar(i, 97.0) for i in range(20, 28)]
    bars[-1] = _bar(27, 96.5, low=96.0, high=97.0, volume=3000)
    assert evaluate_rule_signal("intraday_mean_reversion", "AAPL", bars, parameters=PARAMS) is None


def test_too_little_history_gives_no_signal():
    bars = [_bar(i, 100.0) for i in range(10)]
    assert evaluate_rule_signal("intraday_mean_reversion", "AAPL", bars, parameters=PARAMS) is None
