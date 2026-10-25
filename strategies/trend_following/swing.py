"""Patient, bidirectional trend following on completed daily bars."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math

from strategies.indicators import average_true_range
from strategies.swing_common import (
    CachedDailyBarHistory, SwingSignal, clean_daily_bars, held_sessions,
    position_entry_price,
)


@dataclass(frozen=True)
class SwingTrendConfig:
    enabled: bool = True
    average_type: str = "sma"
    stop_mode: str = "fixed_pct"
    fast_period: int = 20
    slow_period: int = 50
    slope_lookback_days: int = 5
    minimum_slope_pct: float = 0.002
    direction_mode: str = "both"
    minimum_hold_sessions: int = 3
    maximum_holding_sessions: int = 15
    hard_stop_pct: float = 0.08
    atr_period: int = 14
    atr_stop_multiple: float = 2.5
    atr_min_stop_pct: float = 0.04
    atr_max_stop_pct: float = 0.15
    trailing_stop_pct: float = 0.07
    trailing_activation_pct: float = 0.05
    cooldown_days: int = 5
    max_positions: int = 7
    max_entries_per_day: int = 2
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
        if self.average_type not in {"sma", "ema", "kama"}:
            raise ValueError("trend_following average_type must be sma, ema, or kama")
        if self.stop_mode not in {"fixed_pct", "atr"}:
            raise ValueError("trend_following stop_mode must be fixed_pct or atr")
        if self.direction_mode not in {"both", "long_only", "short_only"}:
            raise ValueError("trend_following direction_mode must be both, long_only, or short_only")
        if self.fast_period >= self.slow_period:
            raise ValueError("trend_following fast_period must be less than slow_period")
        for name, low, high in (
            ("fast_period", 5, 40), ("slow_period", 30, 100),
            ("atr_period", 7, 28),
            ("slope_lookback_days", 3, 15), ("minimum_hold_sessions", 2, 5),
            ("maximum_holding_sessions", 10, 20), ("cooldown_days", 3, 10),
            ("max_positions", 3, 7), ("max_entries_per_day", 1, 2),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"trend_following {name} must be an integer between {low} and {high}")
        if self.maximum_holding_sessions <= self.minimum_hold_sessions:
            raise ValueError("trend_following periods and holding limits are inconsistent")
        if self.atr_min_stop_pct >= self.atr_max_stop_pct:
            raise ValueError("trend_following atr_min_stop_pct must be below atr_max_stop_pct")
        for name, low, high in (
            ("minimum_slope_pct", 0.0, 0.03), ("hard_stop_pct", 0.04, 0.15),
            ("atr_stop_multiple", 1.0, 5.0), ("atr_min_stop_pct", 0.03, 0.10),
            ("atr_max_stop_pct", 0.08, 0.25),
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
    if average_type == "kama":
        if len(values) < period + 1:
            return None
        fast_smoothing = 2.0 / (2 + 1)
        slow_smoothing = 2.0 / (30 + 1)
        result = sum(values[:period]) / period
        for index in range(period, len(values)):
            change = abs(values[index] - values[index - period])
            volatility = sum(
                abs(values[offset] - values[offset - 1])
                for offset in range(index - period + 1, index + 1)
            )
            efficiency = change / volatility if volatility > 0 else 0.0
            smoothing = (efficiency * (fast_smoothing - slow_smoothing) + slow_smoothing) ** 2
            result += smoothing * (values[index] - result)
        return result
    alpha = 2 / (period + 1)
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = alpha * value + (1 - alpha) * result
    return result


def _signal_uncached(
    ticker: str, bars: list[dict], *, position_qty: float = 0,
    config: SwingTrendConfig, entry_at: str | None = None,
    entry_price: float | None = None,
) -> SwingSignal | None:
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
    stop_pct = config.hard_stop_pct
    if config.stop_mode == "atr":
        atr = average_true_range([row[1] for row in rows], config.atr_period)
        if atr is None or current <= 0:
            return None
        if config.atr_min_stop_pct >= config.atr_max_stop_pct:
            raise ValueError("trend_following ATR stop bounds are inconsistent")
        stop_pct = min(
            config.atr_max_stop_pct,
            max(config.atr_min_stop_pct, atr * config.atr_stop_multiple / current),
        )

    if position_qty:
        entry = position_entry_price(entry_price)
        held = held_sessions(rows, entry_at)
        if entry is None or held is None:
            return None
        is_short = position_qty < 0
        hard_stop = entry * (1 + stop_pct) if is_short else entry * (1 - stop_pct)
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
        return SwingSignal(ticker, action, "swing_trend_following", reason, stop_pct=stop_pct,
                           target_weight=config.max_position_pct, risk_multiplier=config.risk_multiplier,
                           stop_model_override=config.stop_mode == "atr")

    if config.direction_mode == "long_only":
        bearish = False
    elif config.direction_mode == "short_only":
        bullish = False
    if bullish:
        return SwingSignal(
            ticker, "buy", "swing_trend_following",
            f"daily {config.average_type.upper()} {config.fast_period}/{config.slow_period} bullish; slow-average slope {slope:+.2%}",
            stop_pct=stop_pct, target_weight=config.max_position_pct,
            risk_multiplier=config.risk_multiplier,
            stop_model_override=config.stop_mode == "atr",
        )
    if bearish:
        return SwingSignal(
            ticker, "short", "swing_trend_following",
            f"daily {config.average_type.upper()} {config.fast_period}/{config.slow_period} bearish; slow-average slope {slope:+.2%}",
            stop_pct=stop_pct, target_weight=config.max_position_pct,
            risk_multiplier=config.risk_multiplier,
            stop_model_override=config.stop_mode == "atr",
        )
    return None


def signal(
    ticker: str, bars: list[dict], *, position_qty: float = 0,
    parameters: dict | SwingTrendConfig | None = None, entry_at: str | None = None,
    entry_price: float | None = None,
) -> SwingSignal | None:
    config = parameters if isinstance(parameters, SwingTrendConfig) else SwingTrendConfig.from_dict(parameters)
    cache_key = ("swing_trend_following", ticker, config, position_qty, entry_at, entry_price)
    cached_history = bars if isinstance(bars, CachedDailyBarHistory) else None
    if cached_history is not None:
        try:
            hash(cache_key)
        except TypeError:
            cached_history = None
        else:
            found, result = cached_history.cached_signal(cache_key)
            if found:
                return result
    result = _signal_uncached(
        ticker, bars, position_qty=position_qty, config=config,
        entry_at=entry_at, entry_price=entry_price,
    )
    if cached_history is not None:
        cached_history.remember_signal(cache_key, result)
    return result


__all__ = ["SwingTrendConfig", "signal"]
