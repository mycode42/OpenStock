"""Symmetric moving-average crossover rules; negative quantity is short."""

from __future__ import annotations

from strategies.indicators import closes, moving_average
from strategies.signals import Signal


def signal(
    ticker: str,
    bars: list[dict],
    *,
    position_qty: float = 0,
    fast_period: int = 20,
    slow_period: int = 50,
    allow_long: bool = True,
    allow_short: bool = False,
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
    bearish_cross = fast_now < slow_now and fast_before >= slow_before
    bullish_cross = fast_now > slow_now and fast_before <= slow_before
    if position_qty > 0 and bearish_cross:
        return Signal(ticker, "sell", "trend_following", "fast moving average crossed below slow average", 0.8)
    if position_qty < 0 and bullish_cross:
        return Signal(ticker, "cover", "trend_following", "fast moving average crossed above slow average", 0.8)
    if position_qty == 0 and allow_long and bullish_cross:
        return Signal(ticker, "buy", "trend_following", "fast moving average crossed above slow average", 0.8)
    if position_qty == 0 and allow_short and bearish_cross:
        return Signal(ticker, "short", "trend_following", "fast moving average crossed below slow average", 0.8)
    return None
