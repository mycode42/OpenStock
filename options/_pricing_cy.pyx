# cython: language_level=3

"""Compiled European Black-Scholes kernels used by options.pricing."""

from libc.math cimport erf, exp, fabs, isfinite, log, sqrt


cdef double _normal_cdf(double value) noexcept:
    return 0.5 * (1.0 + erf(value / sqrt(2.0)))


cdef double _black_scholes_price(
    double spot,
    double strike,
    double years,
    double rate,
    double volatility,
    int is_call,
    double dividend_yield,
) noexcept:
    cdef double forward, discounted, root_t, d1, d2
    if spot <= 0.0 or strike <= 0.0 or years <= 0.0:
        if is_call:
            return max(spot - strike, 0.0)
        return max(strike - spot, 0.0)
    if volatility <= 0.0:
        forward = spot * exp((rate - dividend_yield) * years)
        discounted = exp(-rate * years)
        if is_call:
            return discounted * max(forward - strike, 0.0)
        return discounted * max(strike - forward, 0.0)
    root_t = sqrt(years)
    d1 = (log(spot / strike) + (rate - dividend_yield + 0.5 * volatility**2) * years) / (volatility * root_t)
    d2 = d1 - volatility * root_t
    if is_call:
        return spot * exp(-dividend_yield * years) * _normal_cdf(d1) - strike * exp(-rate * years) * _normal_cdf(d2)
    return strike * exp(-rate * years) * _normal_cdf(-d2) - spot * exp(-dividend_yield * years) * _normal_cdf(-d1)


cdef int _right_code(object right) except -1:
    if right == "call":
        return 1
    if right == "put":
        return 0
    raise ValueError("right must be call or put")


cpdef double european_price(
    double spot,
    double strike,
    double years,
    double rate,
    double volatility,
    object right,
    double dividend_yield=0.0,
):
    return _black_scholes_price(
        spot, strike, years, rate, volatility, _right_code(right), dividend_yield,
    )


cpdef tuple european_model_greeks(
    double spot,
    double strike,
    double years,
    double rate,
    double volatility,
    object right,
    double dividend_yield=0.0,
):
    cdef int is_call = _right_code(right)
    cdef double price, d_s, up, down, delta, gamma, d_vol
    cdef double vol_up, vol_down, vega, d_t, theta, d_r, rho
    price = _black_scholes_price(spot, strike, years, rate, volatility, is_call, dividend_yield)
    d_s = max(0.01, spot * 0.001)
    up = _black_scholes_price(spot + d_s, strike, years, rate, volatility, is_call, dividend_yield)
    down = _black_scholes_price(max(1e-6, spot - d_s), strike, years, rate, volatility, is_call, dividend_yield)
    delta = (up - down) / (2.0 * d_s)
    gamma = (up - 2.0 * price + down) / (d_s**2)
    d_vol = 0.01
    vol_up = _black_scholes_price(spot, strike, years, rate, volatility + d_vol, is_call, dividend_yield)
    vol_down = _black_scholes_price(spot, strike, years, rate, max(1e-6, volatility - d_vol), is_call, dividend_yield)
    vega = (vol_up - vol_down) / (2.0 * d_vol)
    d_t = min(1.0 / 365.0, years / 2.0)
    if years <= d_t:
        theta = 0.0
    else:
        theta = (
            _black_scholes_price(spot, strike, years - d_t, rate, volatility, is_call, dividend_yield)
            - _black_scholes_price(spot, strike, years + d_t, rate, volatility, is_call, dividend_yield)
        ) / 2.0
    d_r = 0.0001
    rho = (
        _black_scholes_price(spot, strike, years, rate + d_r, volatility, is_call, dividend_yield)
        - _black_scholes_price(spot, strike, years, rate - d_r, volatility, is_call, dividend_yield)
    ) / (2.0 * d_r)
    return price, delta, gamma, theta, vega, rho, volatility


cpdef double european_implied_volatility(
    double market_price,
    double spot,
    double strike,
    double years,
    double rate,
    object right,
    double dividend_yield=0.0,
    double low=0.005,
    double high=5.0,
    double tolerance=1e-5,
):
    cdef int is_call = _right_code(right)
    cdef double intrinsic, mid, model
    if (
        not isfinite(market_price) or not isfinite(spot) or not isfinite(strike)
        or not isfinite(years) or not isfinite(rate) or market_price < 0.0
        or spot <= 0.0 or strike <= 0.0 or years <= 0.0
    ):
        raise ValueError("invalid inputs for implied volatility")
    if is_call:
        intrinsic = max(spot - strike, 0.0)
    else:
        intrinsic = max(strike - spot, 0.0)
    if market_price + tolerance < intrinsic:
        raise ValueError("market price is below intrinsic value")
    if _black_scholes_price(spot, strike, years, rate, high, is_call, dividend_yield) < market_price:
        raise ValueError("implied volatility exceeds solver bounds")
    for _ in range(80):
        mid = (low + high) / 2.0
        model = _black_scholes_price(spot, strike, years, rate, mid, is_call, dividend_yield)
        if fabs(model - market_price) <= tolerance:
            return mid
        if model < market_price:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0
