"""Validated, small factor-model configuration."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field


SUPPORTED_FACTORS = {
    "momentum_1m",
    "momentum_3m",
    "momentum_6m",
    "low_volatility",
    "short_reversal_5d",
    "value_earnings_yield",
}


@dataclass(frozen=True)
class MultiFactorConfig:
    """Cross-sectional long-only defaults for the Algo portfolio."""

    rebalance_frequency: str = "weekly"
    top_n: int = 5
    min_universe: int = 20
    minimum_score: float = 0.0
    winsor_quantile: float = 0.05
    momentum_lookbacks: dict[str, int] = field(default_factory=lambda: {
        "momentum_1m": 21,
        "momentum_3m": 63,
        "momentum_6m": 126,
    })
    volatility_lookback: int = 63
    reversal_lookback: int = 5
    factor_weights: dict[str, float] = field(default_factory=lambda: {
        "momentum_1m": 0.25,
        "momentum_3m": 0.25,
        "momentum_6m": 0.25,
        "low_volatility": 0.25,
    })
    long_only: bool = True

    def __post_init__(self) -> None:
        self.validate()

    def to_dict(self) -> dict:
        return asdict(self)

    def warmup_calendar_days(self) -> int:
        sessions = max(
            [self.volatility_lookback, self.reversal_lookback, *self.momentum_lookbacks.values()],
            default=0,
        )
        return max(260, math.ceil(sessions * 7 / 5) + 20)

    @classmethod
    def from_dict(cls, values: dict | None = None) -> "MultiFactorConfig":
        if values is not None and not isinstance(values, dict):
            raise ValueError("multi-factor settings must be an object")
        values = dict(values or {})
        allowed = set(cls.__dataclass_fields__)
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"Unsupported multi-factor setting(s): {', '.join(sorted(unknown))}")
        defaults = cls()
        normalized = {**defaults.to_dict(), **values}
        return cls(**normalized)

    def validate(self) -> None:
        if self.rebalance_frequency not in {"daily", "weekly"}:
            raise ValueError("rebalance_frequency must be daily or weekly")
        if isinstance(self.top_n, bool) or not isinstance(self.top_n, int) or not 1 <= self.top_n <= 20:
            raise ValueError("top_n must be an integer between 1 and 20")
        if isinstance(self.min_universe, bool) or not isinstance(self.min_universe, int) or not 2 <= self.min_universe <= 100:
            raise ValueError("min_universe must be an integer between 2 and 100")
        if not isinstance(self.long_only, bool) or not self.long_only:
            raise ValueError("The first multi-factor release supports long_only=True")
        if (
            isinstance(self.winsor_quantile, bool)
            or not isinstance(self.winsor_quantile, (int, float))
            or not math.isfinite(self.winsor_quantile)
            or not 0.0 <= self.winsor_quantile < 0.25
        ):
            raise ValueError("winsor_quantile must be in [0, 0.25)")
        if (
            isinstance(self.minimum_score, bool)
            or not isinstance(self.minimum_score, (int, float))
            or not math.isfinite(self.minimum_score)
        ):
            raise ValueError("minimum_score must be a finite number")
        if (
            isinstance(self.volatility_lookback, bool)
            or not isinstance(self.volatility_lookback, int)
            or isinstance(self.reversal_lookback, bool)
            or not isinstance(self.reversal_lookback, int)
            or not 10 <= self.volatility_lookback <= 252
            or not 2 <= self.reversal_lookback <= 252
        ):
            raise ValueError(
                "volatility_lookback must be 10..252 and reversal_lookback must be 2..252 sessions"
            )
        if not isinstance(self.momentum_lookbacks, dict):
            raise ValueError("momentum_lookbacks must be an object")
        if set(self.momentum_lookbacks) - {"momentum_1m", "momentum_3m", "momentum_6m"}:
            raise ValueError("momentum_lookbacks contains an unsupported horizon")
        if any(isinstance(days, bool) or not isinstance(days, int) or not 5 <= days <= 252 for days in self.momentum_lookbacks.values()):
            raise ValueError("momentum lookbacks must be integer session counts between 5 and 252")
        if (
            not isinstance(self.factor_weights, dict)
            or not self.factor_weights
            or set(self.factor_weights) - SUPPORTED_FACTORS
        ):
            raise ValueError("factor_weights must name supported factors")
        if any(
            isinstance(weight, bool) or not isinstance(weight, (int, float))
            or not math.isfinite(weight) or weight < 0
            for weight in self.factor_weights.values()
        ):
            raise ValueError("factor weights must be finite non-negative numbers")
        if not any(weight > 0 for weight in self.factor_weights.values()):
            raise ValueError("at least one factor weight must be positive")


DEFAULT_CONFIG = MultiFactorConfig()
