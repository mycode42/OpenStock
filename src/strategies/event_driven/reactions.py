"""Simple post-event price/volume confirmation rules."""

from __future__ import annotations

from datetime import datetime
import math

from src.strategies.event_driven.config import EventStrategyConfig
from src.strategies.event_driven.models import ScheduledEvent
from src.strategies.signals import Signal


def post_event_reaction(
    event: ScheduledEvent,
    ticker: str,
    bars: list[dict],
    *,
    as_of: datetime,
    previous_close: float | None,
    config: EventStrategyConfig,
) -> Signal | None:
    """Return a long continuation only after a confirmed range break.

    ``bars`` must contain completed bars through ``as_of``. The first
    ``opening_range_bars`` after the explicit reaction start define the range;
    an entry is considered only on a later bar during the event window.
    """
    if not bars:
        return None
    if event.is_earnings and (
        previous_close is None or not math.isfinite(previous_close) or previous_close <= 0
    ):
        return None
    try:
        timed = [
            (datetime.fromisoformat(str(bar["t"]).replace("Z", "+00:00")), bar)
            for bar in bars
        ]
        if any(timestamp.tzinfo is None for timestamp, _ in timed):
            return None
        event_bars = [
            bar for timestamp, bar in timed
            if event.reaction_start_at <= timestamp <= as_of
        ]
        if len(event_bars) <= config.opening_range_bars:
            return None
        opening_range = event_bars[:config.opening_range_bars]
        current = event_bars[-1]
        opening_price = float(opening_range[0]["o"])
        close = float(current["c"])
        high = max(float(bar["h"]) for bar in opening_range)
        volumes = [float(bar.get("v", 0) or 0) for bar in event_bars[:-1]]
        current_volume = float(current.get("v", 0) or 0)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not all(math.isfinite(value) for value in (opening_price, close, high, current_volume)):
        return None
    if opening_price <= 0 or close <= 0 or high <= 0 or not volumes:
        return None
    average_volume = sum(volumes) / len(volumes)
    volume_ratio = current_volume / average_volume if average_volume > 0 else 0
    if volume_ratio < config.minimum_volume_ratio:
        return None

    if event.is_earnings:
        gap_pct = opening_price / previous_close - 1
        if not config.minimum_gap_pct <= gap_pct <= config.maximum_gap_pct:
            return None
        reason = f"earnings gap continuation {gap_pct:.1%}; volume {volume_ratio:.2f}x"
        strength = min(0.95, 0.65 + gap_pct)
    else:
        move_pct = close / opening_price - 1
        if move_pct < config.minimum_macro_move_pct:
            return None
        reason = f"{event.event_type.upper()} post-event breakout {move_pct:.1%}; volume {volume_ratio:.2f}x"
        strength = min(0.9, 0.62 + move_pct)

    breakout_level = high * (1 + config.breakout_buffer_pct)
    if close <= breakout_level:
        return None
    vwap = current.get("vw")
    if vwap is not None:
        try:
            if close < float(vwap):
                return None
        except (TypeError, ValueError):
            return None
    return Signal(
        ticker=ticker.upper(), action="buy", strategy="event_driven", reason=reason,
        strength=strength, stop_pct=config.stop_pct, target_weight=config.risk_limited_weight,
        event_id=event.event_id,
    )
