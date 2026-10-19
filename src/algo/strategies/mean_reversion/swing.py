"""Patient, daily-bar cyclical mean reversion for the Algo portfolio.

This is intentionally separate from ``mean_reversion``'s intraday open-dip
rule. It needs a persistent multi-day stretch and a daily reversal cue, so an
opening print or a single intraday move cannot trigger it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import math
from statistics import mean
from typing import Any

from src.algo.strategies.indicators import rsi
from src.algo.strategies.signals import Signal
from src.algo.strategies.swing_common import SwingSignal, clean_daily_bars, entry_session, position_entry_price


@dataclass(frozen=True)
class SwingMeanReversionConfig:
    enabled: bool = True
    direction_mode: str = "both"
    lookback_days: int = 15
    stretch_pct: float = 0.06
    minimum_stretch_days: int = 2
    mean_target_fraction: float = 0.80
    use_rsi_confirmation: bool = True
    long_rsi_max: float = 40.0
    short_rsi_min: float = 60.0
    slope_lookback_days: int = 5
    max_reference_slope_pct: float = 0.025
    minimum_hold_sessions: int = 2
    maximum_holding_sessions: int = 10
    hard_stop_pct: float = 0.07
    trailing_stop_pct: float = 0.06
    trailing_activation_pct: float = 0.04
    cooldown_days: int = 5
    max_positions: int = 7
    max_entries_per_day: int = 2
    max_position_pct: float = 0.04
    risk_multiplier: float = 0.75

    @classmethod
    def from_dict(cls, values: dict | None = None) -> "SwingMeanReversionConfig":
        if values is not None and not isinstance(values, dict):
            raise ValueError("swing_mean_reversion settings must be an object")
        supplied = dict(values or {})
        unknown = set(supplied) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"Unsupported swing_mean_reversion setting(s): {', '.join(sorted(unknown))}")
        config = cls(**supplied)
        config.validate()
        return config

    def to_dict(self) -> dict:
        return asdict(self)

    def validate(self) -> None:
        if not isinstance(self.enabled, bool) or not isinstance(self.use_rsi_confirmation, bool):
            raise ValueError("swing_mean_reversion enable flags must be booleans")
        if self.direction_mode not in {"both", "long_only", "short_only"}:
            raise ValueError("swing_mean_reversion direction_mode must be both, long_only, or short_only")
        integer_bounds = {
            "lookback_days": (10, 30), "minimum_stretch_days": (2, 5),
            "slope_lookback_days": (3, 15), "minimum_hold_sessions": (1, 3),
            "maximum_holding_sessions": (5, 15), "cooldown_days": (3, 10),
            "max_positions": (3, 7), "max_entries_per_day": (1, 2),
        }
        for key, (low, high) in integer_bounds.items():
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"swing_mean_reversion {key} must be an integer between {low} and {high}")
        if self.maximum_holding_sessions <= self.minimum_hold_sessions:
            raise ValueError("maximum_holding_sessions must exceed minimum_hold_sessions")
        number_bounds = {
            "stretch_pct": (0.02, 0.20), "mean_target_fraction": (0.25, 1.0),
            "long_rsi_max": (10, 50), "short_rsi_min": (50, 90),
            "max_reference_slope_pct": (0.005, 0.10), "hard_stop_pct": (0.03, 0.15),
            "trailing_stop_pct": (0.03, 0.15), "trailing_activation_pct": (0.01, 0.10),
            "max_position_pct": (0.01, 0.05), "risk_multiplier": (0.5, 1.0),
        }
        for key, (low, high) in number_bounds.items():
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"swing_mean_reversion {key} must be finite")
            if not low <= value <= high:
                raise ValueError(f"swing_mean_reversion {key} must be between {low} and {high}")


def _make_signal(
    ticker: str,
    action: str,
    reason: str,
    *,
    config: SwingMeanReversionConfig,
    take_profit: float | None = None,
) -> SwingSignal:
    return SwingSignal(
        ticker=ticker.upper(), action=action, strategy="swing_mean_reversion",
        reason=reason, strength=0.8, stop_pct=config.hard_stop_pct,
        target_weight=config.max_position_pct, risk_multiplier=config.risk_multiplier,
        take_profit_price=take_profit,
    )


def signal(
    ticker: str,
    bars: list[dict],
    *,
    position_qty: float = 0,
    parameters: dict | None = None,
    entry_at: str | None = None,
    entry_price: float | None = None,
    position_target: float | None = None,
) -> Signal | None:
    """Evaluate completed daily bars without an intraday time-of-day window."""
    config = SwingMeanReversionConfig.from_dict(parameters)
    if not config.enabled:
        return None
    rows = clean_daily_bars(bars)
    values = [row[2] for row in rows]
    required = config.lookback_days + config.slope_lookback_days
    if len(rows) < required:
        return None

    current = values[-1]
    reference = mean(values[-config.lookback_days:])
    prior_reference = mean(values[-required:-config.slope_lookback_days])
    if reference <= 0 or prior_reference <= 0:
        return None
    reference_slope = reference / prior_reference - 1
    stretch = current / reference - 1

    if position_qty:
        entry = position_entry_price(entry_price)
        if entry is None:
            return None
        is_short = position_qty < 0
        hard_stop = entry * (1 + config.hard_stop_pct) if is_short else entry * (1 - config.hard_stop_pct)
        if (current >= hard_stop if is_short else current <= hard_stop):
            action = "cover" if is_short else "sell"
            return _make_signal(
                ticker, action, f"cyclical mean-reversion hard stop at {current:.2f}",
                config=config,
            )

        opened_session = entry_session(entry_at)
        if opened_session is None:
            return None
        post_entry = [row for row in rows if row[0] > opened_session]
        held_sessions = len(post_entry)
        if held_sessions < config.minimum_hold_sessions:
            return None

        target = position_target if position_target and math.isfinite(float(position_target)) else None
        if target is None:
            target = entry + (reference - entry) * config.mean_target_fraction
        if (current <= target if is_short else current >= target):
            action = "cover" if is_short else "sell"
            return _make_signal(
                ticker, action,
                f"reversion target reached at {current:.2f}; reference {reference:.2f}",
                config=config,
            )

        post_entry_closes = [row[2] for row in post_entry]
        if post_entry_closes:
            favorable_move = (
                (entry - min(post_entry_closes)) / entry if is_short
                else (max(post_entry_closes) - entry) / entry
            )
            if favorable_move >= config.trailing_activation_pct:
                trailing_stop = (
                    min(post_entry_closes) * (1 + config.trailing_stop_pct) if is_short
                    else max(post_entry_closes) * (1 - config.trailing_stop_pct)
                )
                if (current >= trailing_stop if is_short else current <= trailing_stop):
                    action = "cover" if is_short else "sell"
                    return _make_signal(
                        ticker, action,
                        f"daily trailing stop reached after {held_sessions} sessions",
                        config=config,
                    )
        if held_sessions >= config.maximum_holding_sessions:
            action = "cover" if is_short else "sell"
            return _make_signal(
                ticker, action, f"maximum {config.maximum_holding_sessions}-session holding period reached",
                config=config,
            )
        return None

    if config.direction_mode == "long_only":
        allow_long, allow_short = True, False
    elif config.direction_mode == "short_only":
        allow_long, allow_short = False, True
    else:
        allow_long = allow_short = True

    # A five-session reference slope filter avoids fading a strong one-way move.
    if abs(reference_slope) > config.max_reference_slope_pct:
        return None
    if config.use_rsi_confirmation:
        relative_strength = rsi(values, 14)
        if relative_strength is None:
            return None
    else:
        relative_strength = None

    start_index = len(rows) - config.minimum_stretch_days
    recent_stretches = []
    for index in range(start_index, len(rows)):
        historical_closes = values[max(0, index - config.lookback_days + 1):index + 1]
        if len(historical_closes) < config.lookback_days:
            return None
        recent_stretches.append(values[index] / mean(historical_closes) - 1)
    long_persistent = all(value <= -config.stretch_pct for value in recent_stretches)
    short_persistent = all(value >= config.stretch_pct for value in recent_stretches)
    previous = values[-2]
    reversal_up = current > previous
    reversal_down = current < previous
    long_rsi_ok = relative_strength is None or relative_strength <= config.long_rsi_max
    short_rsi_ok = relative_strength is None or relative_strength >= config.short_rsi_min

    if allow_long and long_persistent and reversal_up and long_rsi_ok:
        target = current + (reference - current) * config.mean_target_fraction
        return _make_signal(
            ticker, "buy",
            f"daily stretch {stretch:.1%} below {config.lookback_days}-session mean; "
            f"persisted {config.minimum_stretch_days} sessions, reversal confirmed, "
            f"reference slope {reference_slope:+.1%}",
            config=config, take_profit=target,
        )
    if allow_short and short_persistent and reversal_down and short_rsi_ok:
        target = current - (current - reference) * config.mean_target_fraction
        return _make_signal(
            ticker, "short",
            f"daily stretch {stretch:.1%} above {config.lookback_days}-session mean; "
            f"persisted {config.minimum_stretch_days} sessions, reversal confirmed, "
            f"reference slope {reference_slope:+.1%}",
            config=config, take_profit=target,
        )
    return None


__all__ = ["SwingMeanReversionConfig", "SwingSignal", "signal"]
