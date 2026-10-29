import math

import pytest

from options import pricing


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def test_one_day_atm_call_theta_is_per_calendar_day_and_matches_closed_form():
    spot, strike, years, rate, vol = 100.0, 100.0, 1.0 / 365.0, 0.04, 0.30
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * years) / (vol * math.sqrt(years))
    d2 = d1 - vol * math.sqrt(years)
    phi = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
    theta_per_year = -spot * phi * vol / (2.0 * math.sqrt(years)) - rate * strike * math.exp(-rate * years) * _normal_cdf(d2)

    greeks = pricing.model_greeks(spot, strike, years, rate, vol, "call", american=False)

    # Half-day central difference on a one-day option carries a few percent of truncation error.
    assert greeks.theta == pytest.approx(theta_per_year / 365.0, rel=0.05)
    assert greeks.theta == pytest.approx(-0.319, abs=0.02)


def test_python_fallback_matches_compiled_european_greeks(monkeypatch):
    args = (100.0, 95.0, 30 / 365, 0.04, 0.30, "put")
    compiled = pricing.model_greeks(*args, american=False)
    monkeypatch.setattr(pricing, "_pricing_cy", None)
    fallback = pricing.model_greeks(*args, american=False)

    for field in ("price", "delta", "gamma", "theta", "vega", "rho"):
        assert getattr(fallback, field) == pytest.approx(getattr(compiled, field), rel=1e-6, abs=1e-9)


def test_european_gamma_is_smooth_and_matches_finite_difference_on_closed_form():
    spots = [99.0, 99.5, 100.0, 100.5, 101.0]
    gammas = []
    for spot in spots:
        greeks = pricing.model_greeks(spot, 100.0, 30 / 365, 0.04, 0.30, "put", american=False)
        h = 0.01
        reference = (
            pricing.black_scholes_price(spot + h, 100.0, 30 / 365, 0.04, 0.30, "put")
            - 2.0 * pricing.black_scholes_price(spot, 100.0, 30 / 365, 0.04, 0.30, "put")
            + pricing.black_scholes_price(spot - h, 100.0, 30 / 365, 0.04, 0.30, "put")
        ) / (h * h)
        assert greeks.gamma == pytest.approx(reference, rel=1e-3)
        gammas.append(greeks.gamma)
    assert all(later <= earlier for earlier, later in zip(gammas, gammas[1:]))


def test_american_lattice_greeks_are_smooth_across_adjacent_spots():
    spots = [99.0, 99.5, 100.0, 100.5, 101.0]
    results = [
        pricing.model_greeks(spot, 100.0, 30 / 365, 0.04, 0.30, "put", american=True)
        for spot in spots
    ]
    gammas = [item.gamma for item in results]
    deltas = [item.delta for item in results]

    assert all(0.03 < gamma < 0.06 for gamma in gammas)
    assert max(abs(b - a) for a, b in zip(gammas, gammas[1:])) < 0.002
    assert all(later > earlier for earlier, later in zip(deltas, deltas[1:]))
    assert all(-0.6 < delta < -0.4 for delta in deltas)
