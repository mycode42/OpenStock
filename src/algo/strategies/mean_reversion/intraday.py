"""Pure rule-based mean-reversion entry and exit signals."""

from __future__ import annotations

from src.algo.strategies.indicators import (
    adx, closes, efficiency_ratio, moving_average, population_std, rsi,
)
from src.algo.strategies.signals import Signal


def signal(
    ticker: str,
    bars: list[dict],
    *,
    position_qty: float = 0,
    window: int = 20,
    band_width: float = 2.0,
    entry_rsi: float = 32.0,
    exit_rsi: float = 58.0,
    short_entry_rsi: float = 68.0,
    short_exit_rsi: float = 42.0,
    allow_long: bool = True,
    allow_short: bool = False,
    regime_filter: str = "none",
    efficiency_ratio_period: int = 20,
    efficiency_ratio_max: float = 0.35,
    adx_period: int = 14,
    adx_max: float = 20.0,
    threshold_mode: str = "fixed",
    volatility_lookback: int = 20,
    high_volatility_ratio: float = 1.25,
) -> Signal | None:
    """Trade confirmed reversals from either band; negative quantity is short."""
    values = closes(bars)
    if len(values) < window + 2:
        return None
    sample = values[-window:]
    mean = moving_average(sample, window)
    deviation = population_std(sample)
    relative_strength = rsi(values, 14)
    if mean is None or deviation is None or relative_strength is None:
        return None
    if regime_filter not in {"none", "er", "adx", "combined"}:
        raise ValueError("mean_reversion regime_filter must be none, er, adx, or combined")
    if threshold_mode not in {"fixed", "volatility_regime_adjusted"}:
        raise ValueError("mean_reversion threshold_mode must be fixed or volatility_regime_adjusted")
    if not position_qty:
        if regime_filter in {"er", "combined"}:
            er = efficiency_ratio(values, efficiency_ratio_period)
            if er is None or er > efficiency_ratio_max:
                return None
        if regime_filter in {"adx", "combined"}:
            trend_strength = adx(bars, adx_period)
            if trend_strength is None or trend_strength > adx_max:
                return None
    threshold_multiplier = 1.0
    if threshold_mode == "volatility_regime_adjusted":
        returns = [values[index] / values[index - 1] - 1 for index in range(1, len(values))]
        recent_vol = population_std(returns[-volatility_lookback:])
        long_lookback = max(window * 4, volatility_lookback * 2)
        baseline_vol = population_std(returns[-long_lookback:]) if len(returns) >= long_lookback else None
        if recent_vol is not None and baseline_vol is not None and baseline_vol > 0:
            volatility_ratio = recent_vol / baseline_vol
            if volatility_ratio >= high_volatility_ratio:
                threshold_multiplier = min(1.5, volatility_ratio)
    close = values[-1]
    previous = values[-2]
    effective_band_width = band_width * threshold_multiplier
    lower_band = mean - effective_band_width * deviation
    if position_qty > 0:
        if close >= mean or relative_strength >= exit_rsi:
            return Signal(ticker, "sell", "mean_reversion", "price reverted to its moving average", 0.7)
        return None
    if position_qty < 0:
        if close <= mean or relative_strength <= short_exit_rsi:
            return Signal(ticker, "cover", "mean_reversion", "price reverted to its moving average", 0.7)
        return None
    if allow_long and close <= lower_band and close > previous and relative_strength <= entry_rsi:
        return Signal(
            ticker, "buy", "mean_reversion",
            f"lower-band reversal; RSI {relative_strength:.1f}", 0.75,
        )
    upper_band = mean + effective_band_width * deviation
    if allow_short and close >= upper_band and close < previous and relative_strength >= short_entry_rsi:
        return Signal(
            ticker, "short", "mean_reversion",
            f"upper-band rejection; RSI {relative_strength:.1f}", 0.75,
        )
    return None
