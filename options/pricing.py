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


def american_binomial_price(
    spot: float, strike: float, years: float, rate: float, volatility: float,
    right: str, *, dividend_yield: float = 0.0, steps: int = 200,
) -> float:
    """CRR tree with early exercise at each node (continuous yield approximation)."""
    if min(spot, strike) <= 0 or years <= 0:
        return max(spot - strike, 0.0) if right == "call" else max(strike - spot, 0.0)
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
    for i in range(steps - 1, -1, -1):
        next_values = []
        for j in range(i + 1):
            continuation = discount * (probability * values[j + 1] + (1.0 - probability) * values[j])
            node_spot = spot * (up**j) * (down ** (i - j))
            exercise = max(node_spot - strike, 0.0) if right == "call" else max(strike - node_spot, 0.0)
            next_values.append(max(continuation, exercise))
        values = next_values
    return float(values[0])


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
    """Central finite-difference Greeks per underlying share."""
    if not american and _pricing_cy is not None:
        return ModelGreeks(*_pricing_cy.european_model_greeks(
            spot, strike, years, rate, volatility, right,
            dividend_yield=dividend_yield,
        ))
    price_fn = american_binomial_price if american else black_scholes_price
    kwargs = {"dividend_yield": dividend_yield}
    if american:
        kwargs["steps"] = 160
    price = price_fn(spot, strike, years, rate, volatility, right, **kwargs)
    d_s = max(0.01, spot * 0.001)
    up = price_fn(spot + d_s, strike, years, rate, volatility, right, **kwargs)
    down = price_fn(max(1e-6, spot - d_s), strike, years, rate, volatility, right, **kwargs)
    delta = (up - down) / (2.0 * d_s)
    gamma = (up - 2.0 * price + down) / (d_s**2)
    d_vol = 0.01
    vol_up = price_fn(spot, strike, years, rate, volatility + d_vol, right, **kwargs)
    vol_down = price_fn(spot, strike, years, rate, max(1e-6, volatility - d_vol), right, **kwargs)
    vega = (vol_up - vol_down) / (2.0 * d_vol)
    d_t = min(1.0 / 365.0, years / 2.0)
    theta = 0.0 if years <= d_t else (
        price_fn(spot, strike, years - d_t, rate, volatility, right, **kwargs)
        - price_fn(spot, strike, years + d_t, rate, volatility, right, **kwargs)
    ) / 2.0
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
