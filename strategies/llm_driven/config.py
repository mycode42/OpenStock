"""Conservative daily-horizon config for the LLM-signal strategy."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math


@dataclass(frozen=True)
class LLMStrategyConfig:
    enabled: bool = False
    allow_long: bool = True
    allow_short: bool = False
    disable_in_volatile: bool = True
    minimum_confidence: int = 75
    reaction_window_days: int = 3
    minimum_gap_pct: float = 0.02
    maximum_gap_pct: float = 0.08
    minimum_volume_ratio: float = 1.5
    minimum_macro_move_pct: float = 0.004
    reaction_stop_pct: float = 0.025
    stop_pct: float = 0.04
    max_position_pct: float = 0.04
    max_event_risk_pct: float = 0.0016
    risk_multiplier: float = 0.5
    follow_through_sessions: int = 2
    cooldown_days_per_ticker: int = 5

    @classmethod
    def from_dict(cls, values: dict | None = None) -> "LLMStrategyConfig":
        if values is not None and not isinstance(values, dict):
            raise ValueError("llm_driven profile must be a JSON object")
        supplied = dict(values or {})
        unknown = set(supplied) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"Unsupported llm_driven parameter(s): {', '.join(sorted(unknown))}")
        config = cls(**supplied)
        if any(not isinstance(getattr(config, key), bool) for key in ("enabled", "allow_long", "allow_short", "disable_in_volatile")):
            raise ValueError("llm_driven enable flags must be booleans")
        integer_bounds = {
            "reaction_window_days": (1, 5), "follow_through_sessions": (1, 3),
            "cooldown_days_per_ticker": (1, 30),
            "minimum_confidence": (0, 100),
        }
        for key, (low, high) in integer_bounds.items():
            value = getattr(config, key)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"llm_driven {key} must be an integer between {low} and {high}")
        number_bounds = {
            "minimum_gap_pct": (0.005, 0.10), "maximum_gap_pct": (0.01, 0.20),
            "minimum_volume_ratio": (1.0, 10.0), "minimum_macro_move_pct": (0.001, 0.05),
            "reaction_stop_pct": (0.01, 0.08), "stop_pct": (0.02, 0.10),
            "max_position_pct": (0.001, 0.05),
            "max_event_risk_pct": (0.0001, 0.01), "risk_multiplier": (0.1, 1.0),
        }
        for key, (low, high) in number_bounds.items():
            value = getattr(config, key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"llm_driven {key} must be a finite number")
            if not low <= value <= high:
                raise ValueError(f"llm_driven {key} must be between {low} and {high}")
        if config.minimum_gap_pct >= config.maximum_gap_pct:
            raise ValueError("llm_driven gap bounds must satisfy minimum_gap_pct < maximum_gap_pct")
        return config

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def risk_limited_weight(self) -> float:
        return min(self.max_position_pct, self.max_event_risk_pct / self.stop_pct)


__all__ = ["LLMStrategyConfig"]
