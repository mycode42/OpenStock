"""Long-only moving-average crossover rules."""

from __future__ import annotations

from src.strategies.indicators import closes, moving_average
from src.strategies.signals import Signal


def signal(
    ticker: str,
    bars: list[dict],
    *,
    position_qty: float = 0,
    fast_period: int = 20,
    slow_period: int = 50,
) -> Signal | None:
    values = closes(bars)
    if fast_period <= 0 or slow_period <= fast_period or len(values) < slow_period + 1:
        return None
    previous_values = values[:-1]
    fast_now = moving_average(values, fast_period)
    slow_now = moving_average(values, slow_period)
    fast_before = moving_average(previous_values, fast_period)
    slow_before = moving_average(previous_values, slow_period)
    if None in (fast_now, slow_now, fast_before, slow_before):
        return None
    if position_qty > 0 and fast_now < slow_now and fast_before >= slow_before:
        return Signal(ticker, "sell", "trend_following", "fast moving average crossed below slow average", 0.8)
    if position_qty <= 0 and fast_now > slow_now and fast_before <= slow_before:
        return Signal(ticker, "buy", "trend_following", "fast moving average crossed above slow average", 0.8)
    return None

