"""The Cython indicators must match the pure-Python functions.

RSI and efficiency ratio match exactly. Population std sums in a different order in the compiled
version, so it is compared within floating-point rounding.
"""

import random

import pytest

from strategies import _indicators_cy as cy
from strategies import indicators as ind

pytestmark = pytest.mark.skipif(cy is None, reason="indicator extension not built")


def _series(seed: int, n: int) -> list[float]:
    rng = random.Random(seed)
    value = 100.0
    out = []
    for _ in range(n):
        value *= 1.0 + rng.gauss(0, 0.02)
        out.append(value)
    return out


@pytest.mark.parametrize("seed", range(25))
def test_rsi_matches_python_exactly(seed):
    values = _series(seed, 60)
    for period in (2, 14, 30):
        assert cy.rsi(values, period) == ind._rsi_py(values, period)


@pytest.mark.parametrize("seed", range(25))
def test_efficiency_ratio_matches_python_exactly(seed):
    values = _series(seed, 60)
    for period in (5, 20):
        assert cy.efficiency_ratio(values, period) == ind._efficiency_ratio_py(values, period)


@pytest.mark.parametrize("seed", range(25))
def test_population_std_matches_python_exactly(seed):
    values = _series(seed, 40)
    assert cy.population_std(values) == pytest.approx(ind._population_std_py(values), rel=1e-12, abs=0.0)


def test_short_and_empty_inputs_return_none_like_python():
    assert cy.rsi([1.0, 2.0], 14) is None
    assert ind._rsi_py([1.0, 2.0], 14) is None
    assert cy.population_std([]) is None
    assert cy.efficiency_ratio([1.0], 5) is None


def test_flat_series_edge_cases_match():
    flat = [5.0] * 30
    assert cy.rsi(flat, 14) == ind._rsi_py(flat, 14) == 50.0
    assert cy.efficiency_ratio(flat, 10) == ind._efficiency_ratio_py(flat, 10) == 0.0
