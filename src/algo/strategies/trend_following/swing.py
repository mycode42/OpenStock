"""Patient, bidirectional trend following on completed daily bars."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math

from src.algo.strategies.swing_common import (
    SwingSignal, clean_daily_bars, held_sessions, position_entry_price,
)


@dataclass(frozen=True)
class SwingTrendConfig:
    enabled: bool = True
    average_type: str = "sma"
    fast_period: int = 20
    slow_period: int = 50
    slope_lookback_days: int = 5
    minimum_slope_pct: float = 0.002
    direction_mode: str = "both"
    minimum_hold_sessions: int = 3
    maximum_holding_sessions: int = 15
    hard_stop_pct: float = 0.08
    trailing_stop_pct: float = 0.07
    trailing_activation_pct: float = 0.05
    cooldown_days: int = 5
    max_positions: int = 4
    max_entries_per_day: int = 1
    max_position_pct: float = 0.04
    risk_multiplier: float = 0.75

    @classmethod
    def from_dict(cls, values: dict | None = None) -> "SwingTrendConfig":
        if values is not None and not isinstance(values, dict):
            raise ValueError("trend_following settings must be an object")
        supplied = dict(values or {})
        unknown = set(supplied) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"Unsupported trend_following setting(s): {', '.join(sorted(unknown))}")
        config = cls(**supplied)
        config.validate()
        return config

    def to_dict(self) -> dict:
        return asdict(self)

    def validate(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("trend_following enabled must be a boolean")
        if self.average_type not in {"sma", "ema"}:
            raise ValueError("trend_following average_type must be sma or ema")
        if self.direction_mode not in {"both", "long_only", "short_only"}:
            raise ValueError("trend_following direction_mode must be both, long_only, or short_only")
        if self.fast_period >= self.slow_period:
            raise ValueError("trend_following fast_period must be less than slow_period")
        for name, low, high in (
            ("fast_period", 5, 40), ("slow_period", 30, 100),
            ("slope_lookback_days", 3, 15), ("minimum_hold_sessions", 2, 5),
            ("maximum_holding_sessions", 10, 20), ("cooldown_days", 3, 10),
            ("max_positions", 3, 5), ("max_entries_per_day", 1, 2),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"trend_following {name} must be an integer between {low} and {high}")
        if self.maximum_holding_sessions <= self.minimum_hold_sessions:
            raise ValueError("trend_following periods and holding limits are inconsistent")
        for name, low, high in (
            ("minimum_slope_pct", 0.0, 0.03), ("hard_stop_pct", 0.04, 0.15),
            ("trailing_stop_pct", 0.03, 0.15), ("trailing_activation_pct", 0.01, 0.12),
            ("max_position_pct", 0.01, 0.05), ("risk_multiplier", 0.1, 1.0),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"trend_following {name} must be between {low} and {high}")


def _average(values: list[float], period: int, average_type: str) -> float | None:
    if len(values) < period:
        return None
    if average_type == "sma":
        return sum(values[-period:]) / period
    alpha = 2 / (period + 1)
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = alpha * value + (1 - alpha) * result
    return result


def signal(
    ticker: str, bars: list[dict], *, position_qty: float = 0,
    parameters: dict | None = None, entry_at: str | None = None,
    entry_price: float | None = None,
) -> SwingSignal | None:
    config = SwingTrendConfig.from_dict(parameters)
    if not config.enabled:
        return None
    rows = clean_daily_bars(bars)
    values = [row[2] for row in rows]
    if len(values) < config.slow_period + config.slope_lookback_days:
        return None
    fast = _average(values, config.fast_period, config.average_type)
    slow = _average(values, config.slow_period, config.average_type)
    prior_slow = _average(values[:-config.slope_lookback_days], config.slow_period, config.average_type)
    if fast is None or slow is None or prior_slow is None or prior_slow <= 0:
        return None
    slope = slow / prior_slow - 1
    bullish = fast > slow and slope >= config.minimum_slope_pct
    bearish = fast < slow and slope <= -config.minimum_slope_pct
    current = values[-1]
    ticker = ticker.upper()

    if position_qty:
        entry = position_entry_price(entry_price)
        held = held_sessions(rows, entry_at)
        if entry is None or held is None:
            return None
        is_short = position_qty < 0
        hard_stop = entry * (1 + config.hard_stop_pct) if is_short else entry * (1 - config.hard_stop_pct)
        action = "cover" if is_short else "sell"
        if (current >= hard_stop if is_short else current <= hard_stop):
            reason = f"daily trend hard stop; held {held} sessions"
        else:
            favorable = (entry - min(values[-held - 1:])) / entry if is_short else (max(values[-held - 1:]) - entry) / entry
            trail = None
            if favorable >= config.trailing_activation_pct:
                trail = min(values[-held - 1:]) * (1 + config.trailing_stop_pct) if is_short else max(values[-held - 1:]) * (1 - config.trailing_stop_pct)
            trailing_hit = trail is not None and (current >= trail if is_short else current <= trail)
            reversed_trend = bearish if not is_short else bullish
            timed_out = held >= config.maximum_holding_sessions
            if held < config.minimum_hold_sessions:
                return None
            if trailing_hit:
                reason = f"daily trend trailing stop; held {held} sessions"
            elif reversed_trend:
                reason = f"daily {config.fast_period}/{config.slow_period} trend reversed; slope {slope:+.2%}"
            elif timed_out:
                reason = f"maximum {config.maximum_holding_sessions}-session trend hold reached"
            else:
                return None
        return SwingSignal(ticker, action, "trend_following", reason, stop_pct=config.hard_stop_pct,
                           target_weight=config.max_position_pct, risk_multiplier=config.risk_multiplier)

    if config.direction_mode == "long_only":
        bearish = False
    elif config.direction_mode == "short_only":
        bullish = False
    if bullish:
        return SwingSignal(
            ticker, "buy", "trend_following",
            f"daily {config.average_type.upper()} {config.fast_period}/{config.slow_period} bullish; slow-average slope {slope:+.2%}",
            stop_pct=config.hard_stop_pct, target_weight=config.max_position_pct,
            risk_multiplier=config.risk_multiplier,
        )
    if bearish:
        return SwingSignal(
            ticker, "short", "trend_following",
            f"daily {config.average_type.upper()} {config.fast_period}/{config.slow_period} bearish; slow-average slope {slope:+.2%}",
            stop_pct=config.hard_stop_pct, target_weight=config.max_position_pct,
            risk_multiplier=config.risk_multiplier,
        )
    return None


__all__ = ["SwingTrendConfig", "signal"]
