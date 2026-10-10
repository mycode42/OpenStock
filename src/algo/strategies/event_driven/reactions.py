"""Daily-bar earnings and macro reactions with bounded swing exits."""

from __future__ import annotations

from datetime import datetime
import math
from statistics import mean

from src.algo.strategies.event_driven.config import EventStrategyConfig
from src.algo.strategies.event_driven.models import ScheduledEvent
from src.algo.strategies.swing_common import SwingSignal, clean_daily_bars


class EventSignal(SwingSignal):
    """A risk-sized satellite signal carrying its chosen exit horizon."""

    def __init__(self, *args, exit_mode: str, maximum_holding_sessions: int, **kwargs) -> None:
        if exit_mode not in {"reaction", "follow_through"}:
            raise ValueError("event exit_mode must be reaction or follow_through")
        super().__init__(*args, strategy_group="swing_satellite", **kwargs)
        object.__setattr__(self, "exit_mode", exit_mode)
        object.__setattr__(self, "maximum_holding_sessions", maximum_holding_sessions)

    def to_dict(self) -> dict:
        return {
            **super().to_dict(),
            "exit_mode": self.exit_mode,
            "maximum_holding_sessions": self.maximum_holding_sessions,
        }


def daily_event_reaction(
    event: ScheduledEvent,
    ticker: str,
    bars: list[dict],
    *,
    as_of: datetime,
    config: EventStrategyConfig,
) -> EventSignal | None:
    """Use only a completed daily event-session bar known by ``as_of``."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        return None
    sessions = clean_daily_bars(bars)
    if len({row[0] for row in sessions}) != len(sessions):
        # Intraday candles are deliberately rejected; this module has no
        # five-minute event entry path.
        return None
    visible = [row for row in sessions if row[0] < as_of.astimezone(event.reaction_start_at.tzinfo).date()]
    event_day = event.reaction_start_at.date()
    event_index = next((index for index, row in enumerate(visible) if row[0] == event_day), None)
    if event_index is None:
        return None
    _, bar, close = visible[event_index]
    try:
        opened = float(bar["o"])
        volume = float(bar.get("v", 0) or 0)
        prior_volumes = [float(row[1].get("v", 0) or 0) for row in visible[max(0, event_index - 20):event_index]]
        prior_volume = mean([value for value in prior_volumes if math.isfinite(value) and value > 0])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(value) and value > 0 for value in (opened, close)):
        return None
    if prior_volume <= 0 or volume / prior_volume < config.minimum_volume_ratio:
        return None

    exit_mode = "follow_through"
    maximum_holding = config.follow_through_sessions
    if event.is_earnings:
        if event_index == 0:
            return None
        previous_close = visible[event_index - 1][2]
        gap = opened / previous_close - 1 if previous_close > 0 else 0
        if not config.minimum_gap_pct <= abs(gap) <= config.maximum_gap_pct:
            return None
        event_direction = "long" if gap > 0 else "short"
        session_move = close / opened - 1
        if session_move * gap >= 0 and abs(session_move) >= config.minimum_macro_move_pct:
            direction = event_direction
            reason = f"earnings gap {gap:+.1%} held through daily close; follow-through"
        elif session_move * gap < 0 and abs(session_move) >= config.minimum_macro_move_pct:
            direction = "short" if event_direction == "long" else "long"
            exit_mode = "reaction"
            maximum_holding = 1
            reason = f"earnings gap {gap:+.1%} faded by daily close; reaction fade"
        else:
            return None
    else:
        move = close / opened - 1
        if abs(move) < config.minimum_macro_move_pct:
            return None
        direction = "long" if move > 0 else "short"
        reason = f"{event.event_type.upper()} daily reaction {move:+.1%}; follow-through"

    if (direction == "long" and not config.allow_long) or (direction == "short" and not config.allow_short):
        return None
    if direction == "long" and close < (opened + close) / 2:
        return None
    if direction == "short" and close > (opened + close) / 2:
        return None
    strength = min(0.9, 0.62 + abs(close / opened - 1))
    return EventSignal(
        ticker=ticker.upper(), action="buy" if direction == "long" else "short",
        strategy="event_driven", reason=f"{reason}; daily volume {volume / prior_volume:.2f}x",
        strength=strength,
        stop_pct=config.reaction_stop_pct if exit_mode == "reaction" else config.stop_pct,
        target_weight=config.risk_limited_weight, risk_multiplier=config.risk_multiplier,
        event_id=event.event_id, exit_mode=exit_mode,
        maximum_holding_sessions=maximum_holding,
    )


__all__ = ["EventSignal", "daily_event_reaction"]
