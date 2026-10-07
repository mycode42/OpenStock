"""Pure rule-based mean-reversion entry and exit signals."""

from __future__ import annotations

from src.strategies.indicators import closes, moving_average, population_std, rsi
from src.strategies.signals import Signal


def signal(
    ticker: str,
    bars: list[dict],
    *,
    position_qty: float = 0,
    window: int = 20,
    band_width: float = 2.0,
    entry_rsi: float = 32.0,
    exit_rsi: float = 58.0,
) -> Signal | None:
    """Buy a confirmed lower-band reversal; exit at the mean or RSI recovery."""
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
    if close <= lower_band and close > previous and relative_strength <= entry_rsi:
        return Signal(
            ticker, "buy", "mean_reversion",
            f"lower-band reversal; RSI {relative_strength:.1f}", 0.75,
        )
    return None
