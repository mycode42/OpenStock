"""American option pricing and Greeks for backtests and fallback surfaces.

The Cox-Ross-Rubinstein tree supports early exercise. A real volatility surface,
borrow curve, and discrete dividend schedule should replace the flat inputs when
those data are available.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math

try:
    from . import _pricing_cy
except ImportError:  # pragma: no cover - used when Cython extensions are unavailable
    _pricing_cy = None


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def black_scholes_price(
    spot: float, strike: float, years: float, rate: float, volatility: float,
    right: str, *, dividend_yield: float = 0.0,
) -> float:
    """European Black-Scholes price; useful for European contracts and checks."""
    if _pricing_cy is not None:
        return _pricing_cy.european_price(
            spot, strike, years, rate, volatility, right,
            dividend_yield=dividend_yield,
        )
    if min(spot, strike) <= 0 or years <= 0:
        return max(spot - strike, 0.0) if right == "call" else max(strike - spot, 0.0)
    if volatility <= 0:
        forward = spot * math.exp((rate - dividend_yield) * years)
        discounted = math.exp(-rate * years)
        return discounted * (max(forward - strike, 0.0) if right == "call" else max(strike - forward, 0.0))
    root_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate - dividend_yield + 0.5 * volatility**2) * years) / (volatility * root_t)
    d2 = d1 - volatility * root_t
    if right == "call":
        return spot * math.exp(-dividend_yield * years) * _normal_cdf(d1) - strike * math.exp(-rate * years) * _normal_cdf(d2)
    if right == "put":
        return strike * math.exp(-rate * years) * _normal_cdf(-d2) - spot * math.exp(-dividend_yield * years) * _normal_cdf(-d1)
    raise ValueError("right must be call or put")


def _american_tree(
    spot: float, strike: float, years: float, rate: float, volatility: float,
    right: str, *, dividend_yield: float, steps: int,
) -> tuple[float, float, float]:
    """CRR tree value, plus delta and gamma read from the first two lattice levels.

    Lattice Greeks come from the same tree that prices the option, so they do not
    depend on bumping the spot. Bumping a tree with a 0.1% spot step produces
    gamma that is almost entirely discretization noise.
    """
    if min(spot, strike) <= 0 or years <= 0:
        intrinsic = max(spot - strike, 0.0) if right == "call" else max(strike - spot, 0.0)
        sign = (1.0 if spot > strike else 0.0) if right == "call" else (-1.0 if spot < strike else 0.0)
        return intrinsic, sign, 0.0
    if right not in {"call", "put"}:
        raise ValueError("right must be call or put")
    if steps < 2 or steps > 2000:
        raise ValueError("steps must be between 2 and 2000")
    volatility = max(float(volatility), 1e-8)
    dt = years / steps
    up = math.exp(volatility * math.sqrt(dt))
    down = 1.0 / up
    growth = math.exp((rate - dividend_yield) * dt)
    probability = min(1.0, max(0.0, (growth - down) / (up - down)))
    discount = math.exp(-rate * dt)
    values = []
    for j in range(steps + 1):
        terminal_spot = spot * (up**j) * (down ** (steps - j))
        values.append(max(terminal_spot - strike, 0.0) if right == "call" else max(strike - terminal_spot, 0.0))
    level1: list[float] = []
    level2: list[float] = []
    for i in range(steps - 1, -1, -1):
        next_values = []
        for j in range(i + 1):
            continuation = discount * (probability * values[j + 1] + (1.0 - probability) * values[j])
            node_spot = spot * (up**j) * (down ** (i - j))
            exercise = max(node_spot - strike, 0.0) if right == "call" else max(strike - node_spot, 0.0)
            next_values.append(max(continuation, exercise))
        values = next_values
        if i == 2:
            level2 = list(values)
        elif i == 1:
            level1 = list(values)
    delta = (level1[1] - level1[0]) / (spot * (up - down))
    gamma = 0.0
    if level2:
        delta_up = (level2[2] - level2[1]) / (spot * up * up - spot)
        delta_down = (level2[1] - level2[0]) / (spot - spot * down * down)
        gamma = (delta_up - delta_down) / (0.5 * spot * (up * up - down * down))
    return float(values[0]), float(delta), float(gamma)


def american_binomial_price(
    spot: float, strike: float, years: float, rate: float, volatility: float,
    right: str, *, dividend_yield: float = 0.0, steps: int = 200,
) -> float:
    """CRR tree with early exercise at each node (continuous yield approximation)."""
    return _american_tree(
        spot, strike, years, rate, volatility, right,
        dividend_yield=dividend_yield, steps=steps,
    )[0]


def _european_delta_gamma_vega(
    spot: float, strike: float, years: float, rate: float, volatility: float,
    right: str, *, dividend_yield: float,
) -> tuple[float, float, float]:
    """Closed-form Black-Scholes delta, gamma, and vega per unit volatility."""
    if min(spot, strike) <= 0 or years <= 0 or volatility <= 0:
        itm = spot > strike if right == "call" else spot < strike
        sign = 1.0 if right == "call" else -1.0
        return (sign if itm else 0.0), 0.0, 0.0
    root_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate - dividend_yield + 0.5 * volatility**2) * years) / (volatility * root_t)
    discount_q = math.exp(-dividend_yield * years)
    density = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
    delta = discount_q * (_normal_cdf(d1) if right == "call" else _normal_cdf(d1) - 1.0)
    gamma = discount_q * density / (spot * volatility * root_t)
    vega = spot * discount_q * density * root_t
    return delta, gamma, vega


@dataclass(frozen=True)
class ModelGreeks:
    price: float
    delta: float
    gamma: float
    theta: float
    vega: float
    rho: float
    implied_volatility: float


def model_greeks(
    spot: float, strike: float, years: float, rate: float, volatility: float,
    right: str, *, dividend_yield: float = 0.0, american: bool = True,
) -> ModelGreeks:
    """Model Greeks per underlying share.

    European delta, gamma, and vega are closed form. American delta and gamma come
    from the lattice, and American vega uses a volatility bump on the same tree.
    Theta is a per-calendar-day figure from a central difference in time. Rho uses
    a central difference in rate.
    """
    if not american and _pricing_cy is not None:
        return ModelGreeks(*_pricing_cy.european_model_greeks(
            spot, strike, years, rate, volatility, right,
            dividend_yield=dividend_yield,
        ))
    price_fn = american_binomial_price if american else black_scholes_price
    kwargs = {"dividend_yield": dividend_yield}
    if american:
        kwargs["steps"] = 160
        price, delta, gamma = _american_tree(
            spot, strike, years, rate, volatility, right, dividend_yield=dividend_yield, steps=160,
        )
    else:
        price = price_fn(spot, strike, years, rate, volatility, right, **kwargs)
        delta, gamma, _ = _european_delta_gamma_vega(
            spot, strike, years, rate, volatility, right, dividend_yield=dividend_yield,
        )
    if american:
        vega_up = price_fn(spot, strike, years, rate, volatility + 0.01, right, **kwargs)
        vega_down = price_fn(spot, strike, years, rate, max(1e-6, volatility - 0.01), right, **kwargs)
        vega = (vega_up - vega_down) / 0.02
    else:
        vega = _european_delta_gamma_vega(
            spot, strike, years, rate, volatility, right, dividend_yield=dividend_yield,
        )[2]
    d_t = min(1.0 / 365.0, years / 2.0)
    # Time-decay per calendar day: -dP/dT over a 2*d_t window, scaled from years to days.
    theta = 0.0 if years <= d_t else (
        price_fn(spot, strike, years - d_t, rate, volatility, right, **kwargs)
        - price_fn(spot, strike, years + d_t, rate, volatility, right, **kwargs)
    ) / (2.0 * d_t) / 365.0
    d_r = 0.0001
    rho = (
        price_fn(spot, strike, years, rate + d_r, volatility, right, **kwargs)
        - price_fn(spot, strike, years, rate - d_r, volatility, right, **kwargs)
    ) / (2.0 * d_r)
    return ModelGreeks(price, delta, gamma, theta, vega, rho, volatility)


def implied_volatility(
    market_price: float, spot: float, strike: float, years: float, rate: float,
    right: str, *, dividend_yield: float = 0.0, american: bool = True,
    low: float = 0.005, high: float = 5.0, tolerance: float = 1e-5,
) -> float:
    """Bisection IV solver; returns a decimal volatility, e.g. 0.25."""
    if not american and _pricing_cy is not None:
        return _pricing_cy.european_implied_volatility(
            market_price, spot, strike, years, rate, right,
            dividend_yield=dividend_yield, low=low, high=high,
            tolerance=tolerance,
        )
    if not all(math.isfinite(v) for v in (market_price, spot, strike, years, rate)) or market_price < 0 or spot <= 0 or strike <= 0 or years <= 0:
        raise ValueError("invalid inputs for implied volatility")
    price_fn = american_binomial_price if american else black_scholes_price
    kwargs = {"dividend_yield": dividend_yield}
    if american:
        kwargs["steps"] = 120
    intrinsic = max(spot - strike, 0.0) if right == "call" else max(strike - spot, 0.0)
    if market_price + tolerance < intrinsic:
        raise ValueError("market price is below intrinsic value")
    if price_fn(spot, strike, years, rate, high, right, **kwargs) < market_price:
        raise ValueError("implied volatility exceeds solver bounds")
    for _ in range(80):
        mid = (low + high) / 2.0
        model = price_fn(spot, strike, years, rate, mid, right, **kwargs)
        if abs(model - market_price) <= tolerance:
            return mid
        if model < market_price:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


def years_to_expiration(as_of: date, expiration: date) -> float:
    return max(0.0, (expiration - as_of).days / 365.0)
