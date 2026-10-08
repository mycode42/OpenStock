"""Small validated configuration surface for the event strategy."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math


@dataclass(frozen=True)
class EventStrategyConfig:
    enabled: bool = False
    disable_in_volatile: bool = True
    opening_range_bars: int = 3
    minimum_gap_pct: float = 0.02
    maximum_gap_pct: float = 0.08
    minimum_volume_ratio: float = 1.5
    breakout_buffer_pct: float = 0.001
    minimum_macro_move_pct: float = 0.004
    reaction_window_minutes: int = 90
    stop_pct: float = 0.02
    max_position_pct: float = 0.025
    max_event_risk_pct: float = 0.0005
    cooldown_days_per_ticker: int = 5
    cooldown_hours_portfolio: int = 24
    max_entries_per_week: int = 3

    @classmethod
    def from_dict(cls, values: dict | None = None) -> "EventStrategyConfig":
        if values is not None and not isinstance(values, dict):
            raise ValueError("event_driven profile must be a JSON object")
        supplied = dict(values or {})
        allowed = set(cls.__dataclass_fields__)
        unknown = set(supplied) - allowed
        if unknown:
            raise ValueError(f"Unsupported event_driven parameter(s): {', '.join(sorted(unknown))}")
        config = cls(**supplied)
        if not isinstance(config.enabled, bool) or not isinstance(config.disable_in_volatile, bool):
            raise ValueError("event_driven enabled flags must be booleans")
        integer_bounds = {
            "opening_range_bars": (2, 6), "reaction_window_minutes": (30, 120),
            "cooldown_days_per_ticker": (0, 30), "cooldown_hours_portfolio": (0, 168),
            "max_entries_per_week": (1, 5),
        }
        for key, (low, high) in integer_bounds.items():
            value = getattr(config, key)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"event_driven {key} must be an integer between {low} and {high}")
        number_bounds = {
            "minimum_gap_pct": (0.005, 0.10), "maximum_gap_pct": (0.01, 0.20),
            "minimum_volume_ratio": (1.0, 10.0), "breakout_buffer_pct": (0, 0.02),
            "minimum_macro_move_pct": (0.001, 0.05), "stop_pct": (0.005, 0.10),
            "max_position_pct": (0.001, 0.10), "max_event_risk_pct": (0.0001, 0.01),
        }
        for key, (low, high) in number_bounds.items():
            value = getattr(config, key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"event_driven {key} must be a finite number")
            if not low <= value <= high:
                raise ValueError(f"event_driven {key} must be between {low} and {high}")
        if config.minimum_gap_pct >= config.maximum_gap_pct:
            raise ValueError("event_driven gap bounds must satisfy minimum_gap_pct < maximum_gap_pct")
        return config

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def risk_limited_weight(self) -> float:
        return min(self.max_position_pct, self.max_event_risk_pct / self.stop_pct)
