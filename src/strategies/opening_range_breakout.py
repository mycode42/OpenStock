"""Opening range breakout rules over regular-session five-minute bars."""

from __future__ import annotations

from src.strategies.signals import Signal


def signal(
    ticker: str,
    bars: list[dict],
    *,
    position_qty: float = 0,
    opening_range_bars: int = 6,
    minimum_volume_ratio: float = 1.25,
) -> Signal | None:
    """Use the first 30 minutes as range; buy a later close above its high."""
    if opening_range_bars < 1 or len(bars) <= opening_range_bars:
        return None
    opening = bars[:opening_range_bars]
    later = bars[opening_range_bars:]
    try:
        opening_high = max(float(bar["h"]) for bar in opening)
        opening_low = min(float(bar["l"]) for bar in opening)
        latest_close = float(later[-1]["c"])
        latest_volume = float(later[-1].get("v", 0) or 0)
        baseline = sum(float(bar.get("v", 0) or 0) for bar in opening) / opening_range_bars
    except (KeyError, TypeError, ValueError):
        return None
    ratio = latest_volume / baseline if baseline > 0 else 0
    if position_qty > 0 and latest_close < opening_low:
        return Signal(ticker, "sell", "opening_range_breakout", "price closed below opening range low", 0.7)
    if position_qty <= 0 and latest_close > opening_high and ratio >= minimum_volume_ratio:
        return Signal(
            ticker, "buy", "opening_range_breakout",
            f"close above 30-minute range high; volume {ratio:.2f}x opening average", 0.8,
        )
    return None

