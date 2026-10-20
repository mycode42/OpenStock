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
    "price_volume_momentum",
    "value_earnings_yield",
    "value_book_to_price",
    "value_fcf_yield",
    "quality_profitability",
    "quality_balance_sheet",
}
MOMENTUM_FACTORS = {"momentum_1m", "momentum_3m", "momentum_6m"}
REGIMES = {"unknown", "trending_up", "trending_down", "range_bound", "sideways", "volatile", "risk_off", "bullish", "bearish"}
REGIME_FACTOR_WEIGHT_PROFILES = {
    "trending_up": {
        "momentum_1m": 0.18, "momentum_3m": 0.22, "momentum_6m": 0.24,
        "low_volatility": 0.06, "price_volume_momentum": 0.10,
        "value_earnings_yield": 0.01, "quality_profitability": 0.01,
    },
    "trending_down": {
        "momentum_1m": 0.08, "momentum_3m": 0.10, "momentum_6m": 0.12,
        "low_volatility": 0.35, "price_volume_momentum": 0.05,
        "value_earnings_yield": 0.08, "quality_profitability": 0.08,
        "quality_balance_sheet": 0.16,
    },
    "range_bound": {
        "momentum_1m": 0.08, "momentum_3m": 0.10, "momentum_6m": 0.08,
        "low_volatility": 0.22, "short_reversal_5d": 0.15,
        "price_volume_momentum": 0.05, "value_earnings_yield": 0.10,
        "quality_profitability": 0.08, "quality_balance_sheet": 0.08,
    },
    "risk_off": {
        "momentum_1m": 0.04, "momentum_3m": 0.08, "momentum_6m": 0.10,
        "low_volatility": 0.38, "price_volume_momentum": 0.02,
        "value_earnings_yield": 0.10, "quality_profitability": 0.10,
        "quality_balance_sheet": 0.10,
    },
    "volatile": {
        "momentum_1m": 0.02, "momentum_3m": 0.04, "momentum_6m": 0.08,
        "low_volatility": 0.55, "price_volume_momentum": 0.01,
        "value_earnings_yield": 0.10, "quality_profitability": 0.10,
        "quality_balance_sheet": 0.10,
    },
}


@dataclass(frozen=True)
class MultiFactorConfig:
    """Small cross-sectional ranking configuration for the Algo core strategy."""

    rebalance_frequency: str = "daily"
    top_n: int = 3
    min_universe: int = 20
    minimum_score: float = 0.0
    winsor_quantile: float = 0.05
    momentum_lookbacks: dict[str, int] = field(default_factory=lambda: {
        "momentum_1m": 20,
        "momentum_3m": 60,
        "momentum_6m": 120,
    })
    volatility_lookback: int = 20
    reversal_lookback: int = 5
    liquidity_lookback: int = 20
    minimum_history_sessions: int = 20
    minimum_price: float = 5.0
    minimum_average_dollar_volume: float = 500_000.0
    maximum_bar_age_days: int = 7
    factor_neutralization: str = "none"
    factor_weight_mode: str = "static"
    regime_factor_weights: dict[str, dict[str, float]] = field(default_factory=dict)
    portfolio_weight_method: str = "equal"
    covariance_lookback_sessions: int = 63
    covariance_shrinkage: float = 0.5
    factor_weights: dict[str, float] = field(default_factory=lambda: {
        "momentum_1m": 0.30,
        "momentum_3m": 0.30,
        "momentum_6m": 0.20,
        "low_volatility": 0.20,
    })
    direction_mode: str = "long_short"
    disable_in_volatile: bool = True
    max_position_pct: float = 0.02
    hard_stop_pct: float = 0.06
    risk_multiplier: float = 0.5
    minimum_hold_sessions: int = 3
    maximum_holding_sessions: int = 15
    cooldown_days: int = 5
    max_positions: int = 7
    enable_intraday_tilt: bool = False

    def __post_init__(self) -> None:
        self.validate()

    def to_dict(self) -> dict:
        return asdict(self)

    def warmup_calendar_days(self) -> int:
        sessions = max(
            [
                self.volatility_lookback, self.reversal_lookback,
                self.liquidity_lookback, self.minimum_history_sessions,
                self.covariance_lookback_sessions, *self.momentum_lookbacks.values(),
            ],
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
            raise ValueError("rebalance_frequency must be daily or weekly (weekly is five sessions)")
        if self.direction_mode not in {"long_only", "long_short", "regime_dependent"}:
            raise ValueError("direction_mode must be long_only, long_short, or regime_dependent")
        if not isinstance(self.factor_neutralization, str) or self.factor_neutralization not in {"none", "sector"}:
            raise ValueError("factor_neutralization must be none or sector")
        if self.factor_weight_mode not in {"static", "ridge_walk_forward"}:
            raise ValueError("factor_weight_mode must be static or ridge_walk_forward")
        if not isinstance(self.portfolio_weight_method, str) or self.portfolio_weight_method not in {"equal", "shrinkage_min_variance"}:
            raise ValueError("portfolio_weight_method must be equal or shrinkage_min_variance")
        if not isinstance(self.disable_in_volatile, bool):
            raise ValueError("disable_in_volatile must be a boolean")
        if not isinstance(self.enable_intraday_tilt, bool):
            raise ValueError("enable_intraday_tilt must be a boolean")
        if isinstance(self.top_n, bool) or not isinstance(self.top_n, int) or not 1 <= self.top_n <= 20:
            raise ValueError("top_n must be an integer between 1 and 20")
        if isinstance(self.min_universe, bool) or not isinstance(self.min_universe, int) or not 2 <= self.min_universe <= 100:
            raise ValueError("min_universe must be an integer between 2 and 100")
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
            or isinstance(self.liquidity_lookback, bool)
            or not isinstance(self.liquidity_lookback, int)
            or isinstance(self.minimum_history_sessions, bool)
            or not isinstance(self.minimum_history_sessions, int)
            or isinstance(self.maximum_bar_age_days, bool)
            or not isinstance(self.maximum_bar_age_days, int)
            or not 10 <= self.volatility_lookback <= 252
            or not 2 <= self.reversal_lookback <= 252
            or not 5 <= self.liquidity_lookback <= 252
            or not 5 <= self.minimum_history_sessions <= 252
            or not 1 <= self.maximum_bar_age_days <= 30
        ):
            raise ValueError(
                "lookbacks must be in supported session ranges and maximum_bar_age_days must be 1..30"
            )
        if not isinstance(self.momentum_lookbacks, dict):
            raise ValueError("momentum_lookbacks must be an object")
        if set(self.momentum_lookbacks) - MOMENTUM_FACTORS:
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
        for name in ("minimum_price", "minimum_average_dollar_volume", "covariance_shrinkage"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        if self.minimum_price <= 0 or self.minimum_average_dollar_volume < 0:
            raise ValueError("minimum_price must be positive and minimum_average_dollar_volume non-negative")
        if (
            isinstance(self.covariance_lookback_sessions, bool)
            or not isinstance(self.covariance_lookback_sessions, int)
            or not 10 <= self.covariance_lookback_sessions <= 252
        ):
            raise ValueError("covariance_lookback_sessions must be an integer between 10 and 252")
        if not 0 <= self.covariance_shrinkage <= 1:
            raise ValueError("covariance_shrinkage must be between 0 and 1")
        if not isinstance(self.regime_factor_weights, dict):
            raise ValueError("regime_factor_weights must be an object")
        for regime, weights in self.regime_factor_weights.items():
            if regime not in REGIMES or not isinstance(weights, dict) or not weights:
                raise ValueError("regime_factor_weights must map supported regimes to factor weights")
            if (
                set(weights) - SUPPORTED_FACTORS
                or any(
                    isinstance(weight, bool) or not isinstance(weight, (int, float))
                    or not math.isfinite(weight) or weight < 0
                    for weight in weights.values()
                )
                or not any(weight > 0 for weight in weights.values())
            ):
                raise ValueError(f"invalid factor weights for regime {regime}")
        for name, low, high in (
            ("minimum_hold_sessions", 1, 5), ("maximum_holding_sessions", 5, 20),
            ("cooldown_days", 1, 15), ("max_positions", 3, 7),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{name} must be an integer between {low} and {high}")
        if self.maximum_holding_sessions <= self.minimum_hold_sessions:
            raise ValueError("maximum_holding_sessions must exceed minimum_hold_sessions")
        for name, low, high in (
            ("max_position_pct", 0.005, 0.05), ("hard_stop_pct", 0.02, 0.15),
            ("risk_multiplier", 0.1, 1.0),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"{name} must be between {low} and {high}")


DEFAULT_CONFIG = MultiFactorConfig()
