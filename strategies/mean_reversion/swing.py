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

from strategies.indicators import adx, efficiency_ratio, population_std, rsi
from strategies.signals import Signal
from strategies.swing_common import SwingSignal, clean_daily_bars, entry_session, position_entry_price


@dataclass(frozen=True)
class SwingMeanReversionConfig:
    enabled: bool = True
    direction_mode: str = "both"
    lookback_days: int = 15
    lookback_mode: str = "fixed"
    stretch_pct: float = 0.06
    entry_distance_mode: str = "fixed_pct"
    stretch_zscore: float = 1.75
    volatility_lookback_days: int = 20
    regime_filter: str = "none"
    efficiency_ratio_period: int = 20
    efficiency_ratio_max: float = 0.35
    adx_period: int = 14
    adx_max: float = 20.0
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
        if self.lookback_mode not in {"fixed", "volatility_adaptive"}:
            raise ValueError("swing_mean_reversion lookback_mode must be fixed or volatility_adaptive")
        if self.entry_distance_mode not in {"fixed_pct", "standardized"}:
            raise ValueError("swing_mean_reversion entry_distance_mode must be fixed_pct or standardized")
        if self.regime_filter not in {"none", "er", "adx", "combined"}:
            raise ValueError("swing_mean_reversion regime_filter must be none, er, adx, or combined")
        integer_bounds = {
            "lookback_days": (10, 30), "minimum_stretch_days": (2, 5),
            "volatility_lookback_days": (10, 40), "efficiency_ratio_period": (10, 40),
            "adx_period": (7, 28),
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
            "stretch_zscore": (0.75, 3.5), "efficiency_ratio_max": (0.10, 0.60),
            "adx_max": (10, 35),
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


def _effective_lookback(values: list[float], end: int, config: SwingMeanReversionConfig) -> int:
    if config.lookback_mode != "volatility_adaptive":
        return config.lookback_days
    prefix = values[:end + 1]
    returns = [prefix[index] / prefix[index - 1] - 1 for index in range(1, len(prefix))]
    recent = population_std(returns[-config.volatility_lookback_days:])
    long_window = max(config.lookback_days * 3, config.volatility_lookback_days * 2)
    baseline = population_std(returns[-long_window:])
    if recent is None or baseline is None or baseline <= 0:
        return config.lookback_days
    ratio = recent / baseline
    if ratio <= 1.0:
        return config.lookback_days
    return max(10, min(config.lookback_days, int(round(config.lookback_days / ratio))))


def _entry_distance(
    values: list[float], end: int, lookback: int, config: SwingMeanReversionConfig,
) -> float | None:
    reference = sum(values[end - lookback + 1:end + 1]) / lookback
    if reference <= 0:
        return None
    stretch = values[end] / reference - 1
    if config.entry_distance_mode == "fixed_pct":
        return stretch
    deviation = population_std(values[end - lookback + 1:end + 1])
    if deviation is None or deviation <= 0:
        return None
    return (values[end] - reference) / deviation


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
    required = max(
        config.lookback_days + config.slope_lookback_days,
        config.volatility_lookback_days + config.lookback_days + 1
        if config.lookback_mode == "volatility_adaptive" else 0,
    )
    if len(rows) < required:
        return None

    current = values[-1]
    lookback = _effective_lookback(values, len(values) - 1, config)
    reference = mean(values[-lookback:])
    prior_reference = mean(values[-(lookback + config.slope_lookback_days):-config.slope_lookback_days])
    if reference <= 0 or prior_reference <= 0:
        return None
    reference_slope = reference / prior_reference - 1
    stretch = current / reference - 1
    current_distance = _entry_distance(values, len(values) - 1, lookback, config)

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

    if config.regime_filter in {"er", "combined"}:
        er = efficiency_ratio(values, config.efficiency_ratio_period)
        if er is None or er > config.efficiency_ratio_max:
            return None
    if config.regime_filter in {"adx", "combined"}:
        trend_strength = adx([row[1] for row in rows], config.adx_period)
        if trend_strength is None or trend_strength > config.adx_max:
            return None

    start_index = len(rows) - config.minimum_stretch_days
    recent_stretches = []
    for index in range(start_index, len(rows)):
        local_lookback = _effective_lookback(values, index, config)
        historical_closes = values[max(0, index - local_lookback + 1):index + 1]
        if len(historical_closes) < local_lookback:
            return None
        distance = _entry_distance(values, index, local_lookback, config)
        if distance is None:
            return None
        recent_stretches.append(distance)
    threshold = config.stretch_pct if config.entry_distance_mode == "fixed_pct" else config.stretch_zscore
    long_persistent = all(value <= -threshold for value in recent_stretches)
    short_persistent = all(value >= threshold for value in recent_stretches)
    previous = values[-2]
    reversal_up = current > previous
    reversal_down = current < previous
    long_rsi_ok = relative_strength is None or relative_strength <= config.long_rsi_max
    short_rsi_ok = relative_strength is None or relative_strength >= config.short_rsi_min

    if allow_long and long_persistent and reversal_up and long_rsi_ok:
        target = current + (reference - current) * config.mean_target_fraction
        distance_label = (
            f"{current_distance:+.2f} standard deviations from the mean"
            if config.entry_distance_mode == "standardized" and current_distance is not None
            else f"{stretch:.1%} below the mean"
        )
        return _make_signal(
            ticker, "buy",
            f"daily stretch {distance_label}; "
            f"persisted {config.minimum_stretch_days} sessions, reversal confirmed, "
            f"reference slope {reference_slope:+.1%}",
            config=config, take_profit=target,
        )
    if allow_short and short_persistent and reversal_down and short_rsi_ok:
        target = current - (current - reference) * config.mean_target_fraction
        distance_label = (
            f"{current_distance:+.2f} standard deviations from the mean"
            if config.entry_distance_mode == "standardized" and current_distance is not None
            else f"{stretch:.1%} above the mean"
        )
        return _make_signal(
            ticker, "short",
            f"daily stretch {distance_label}; "
            f"persisted {config.minimum_stretch_days} sessions, reversal confirmed, "
            f"reference slope {reference_slope:+.1%}",
            config=config, take_profit=target,
        )
    return None


__all__ = ["SwingMeanReversionConfig", "SwingSignal", "signal"]
