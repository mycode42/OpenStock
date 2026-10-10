"""Price and volume confirmed long and short momentum rules."""

from __future__ import annotations

from src.algo.strategies.indicators import closes, moving_average
from src.algo.strategies.signals import Signal


def signal(
    ticker: str,
    bars: list[dict],
    *,
    position_qty: float = 0,
    lookback: int = 20,
    minimum_return: float = 0.04,
    minimum_volume_ratio: float = 1.2,
    minimum_short_return: float | None = None,
    minimum_short_volume_ratio: float | None = None,
    allow_long: bool = True,
    allow_short: bool = False,
) -> Signal | None:
    values = closes(bars)
    if len(values) < max(lookback + 1, 21):
        return None
    current = values[-1]
    average = moving_average(values, 20)
    if average is None:
        return None
    recent = [float(bar.get("v", 0) or 0) for bar in bars[-20:]]
    prior_volume = recent[:-1]
    current_volume = recent[-1] if recent else 0
    average_volume = sum(prior_volume) / len(prior_volume) if prior_volume else 0
    volume_ratio = current_volume / average_volume if average_volume > 0 else 0
    momentum_return = current / values[-lookback - 1] - 1
    if position_qty > 0 and (current < average or momentum_return < 0):
        return Signal(ticker, "sell", "momentum", "momentum or 20-bar trend turned negative", 0.65)
    if position_qty < 0 and (current > average or momentum_return > 0):
        return Signal(ticker, "cover", "momentum", "downside momentum or 20-bar trend turned positive", 0.65)
    short_return = minimum_return if minimum_short_return is None else minimum_short_return
    short_volume = minimum_volume_ratio if minimum_short_volume_ratio is None else minimum_short_volume_ratio
    if position_qty == 0 and allow_long and current > average and momentum_return >= minimum_return and volume_ratio >= minimum_volume_ratio:
        return Signal(
            ticker, "buy", "momentum",
            f"{lookback}-bar return {momentum_return:.1%}; volume {volume_ratio:.2f}x average",
            min(0.95, 0.6 + momentum_return),
        )
    if position_qty == 0 and allow_short and current < average and momentum_return <= -short_return and volume_ratio >= short_volume:
        return Signal(
            ticker, "short", "momentum",
            f"{lookback}-bar return {momentum_return:.1%}; volume {volume_ratio:.2f}x average",
            min(0.95, 0.6 + abs(momentum_return)),
        )
    return None
