"""The tail-limited entry path must produce exactly the same signals as the full-history path."""

import random
from datetime import date, timedelta

import pytest

from strategies.mean_reversion import swing

PARAMETER_SETS = [
    {"stretch_pct": 0.02, "minimum_stretch_days": 2, "max_reference_slope_pct": 0.10},
    {"stretch_pct": 0.03, "minimum_stretch_days": 3, "max_reference_slope_pct": 0.10, "lookback_days": 12},
    {"stretch_pct": 0.02, "minimum_stretch_days": 2, "max_reference_slope_pct": 0.10, "lookback_days": 25,
     "entry_distance_mode": "standardized", "stretch_zscore": 0.75},
    {"stretch_pct": 0.02, "minimum_stretch_days": 2, "max_reference_slope_pct": 0.10,
     "regime_filter": "er", "efficiency_ratio_period": 12, "efficiency_ratio_max": 0.60},
    {"stretch_pct": 0.02, "minimum_stretch_days": 2, "max_reference_slope_pct": 0.10,
     "use_rsi_confirmation": True, "long_rsi_max": 50, "short_rsi_min": 50},
]


def _bars(seed: int, n: int) -> list[dict]:
    """Oscillating prices around a drift, so stretches and reversals occur often enough to test."""
    rng = random.Random(seed)
    start, out = date(2024, 1, 1), []
    period = rng.uniform(6, 14)
    for index in range(n):
        value = 100.0 * (1 + 0.09 * __import__("math").sin(index / period)) * (1 + rng.gauss(0, 0.004))
        out.append({
            "date": (start + timedelta(days=index)).isoformat(),
            "o": value, "h": value * 1.01, "l": value * 0.99, "c": value, "v": 1_000_000,
        })
    return out


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("parameters", PARAMETER_SETS)
def test_tail_path_matches_full_history(seed, parameters):
    bars = _bars(seed, 260)
    for end in (200, 230, 260):
        window = bars[:end]
        try:
            swing._TAIL_OPTIMIZATION = False
            full = swing.signal("TST", window, parameters=parameters)
            full_position = swing.signal("TST", window, parameters=parameters, position_qty=10, entry_at=window[-30]["date"], entry_price=window[-30]["c"])
        finally:
            swing._TAIL_OPTIMIZATION = True
        tail = swing.signal("TST", window, parameters=parameters)
        tail_position = swing.signal("TST", window, parameters=parameters, position_qty=10, entry_at=window[-30]["date"], entry_price=window[-30]["c"])
        assert tail == full
        assert tail_position == full_position


def test_equivalence_cases_actually_produce_signals():
    """Guards the test itself: without real signals the equality checks above prove nothing."""
    fired = sum(
        swing.signal("TST", _bars(seed, 260)[:end], parameters=parameters) is not None
        for seed in range(12) for parameters in PARAMETER_SETS for end in (200, 230, 260)
    )
    assert fired >= 5
