"""Pure rule-based mean-reversion entry and exit signals."""

from __future__ import annotations

from src.algo.strategies.indicators import closes, moving_average, population_std, rsi
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
    close = values[-1]
    previous = values[-2]
    lower_band = mean - band_width * deviation
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
    upper_band = mean + band_width * deviation
    if allow_short and close >= upper_band and close < previous and relative_strength >= short_entry_rsi:
        return Signal(
            ticker, "short", "mean_reversion",
            f"upper-band rejection; RSI {relative_strength:.1f}", 0.75,
        )
    return None
