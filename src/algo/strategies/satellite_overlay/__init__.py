"""Bounded satellite confirmations for the Algo rule portfolio."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math


CORE_STRATEGIES = frozenset({
    "mean_reversion", "swing_mean_reversion", "momentum",
    "opening_range_breakout", "trend_following",
    "swing_momentum", "swing_trend_following",
    "intraday_trend", "intraday_momentum",
})
SATELLITE_STRATEGIES = frozenset({"multi_factor", "event_driven"})


@dataclass(frozen=True)
class SatelliteOverlayConfig:
    multi_factor_enabled: bool = True
    event_driven_enabled: bool = True
    multi_factor_strength_bonus: float = 0.08
    event_driven_strength_bonus: float = 0.10
    multi_factor_risk_bonus: float = 0.10
    event_driven_risk_bonus: float = 0.10
    max_risk_multiplier: float = 1.25

    @classmethod
    def from_dict(cls, values: dict | None = None) -> "SatelliteOverlayConfig":
        if values is not None and not isinstance(values, dict):
            raise ValueError("satellite_overlay must be an object")
        supplied = dict(values or {})
        unknown = set(supplied) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"Unsupported satellite_overlay setting(s): {', '.join(sorted(unknown))}")
        config = cls(**supplied)
        if not isinstance(config.multi_factor_enabled, bool) or not isinstance(config.event_driven_enabled, bool):
            raise ValueError("satellite_overlay enable flags must be booleans")
        for key in (
            "multi_factor_strength_bonus", "event_driven_strength_bonus",
            "multi_factor_risk_bonus", "event_driven_risk_bonus",
        ):
            value = getattr(config, key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"satellite_overlay {key} must be finite")
            if not 0 <= value <= 0.25:
                raise ValueError(f"satellite_overlay {key} must be between 0 and 0.25")
        if (
            isinstance(config.max_risk_multiplier, bool)
            or not isinstance(config.max_risk_multiplier, (int, float))
            or not math.isfinite(config.max_risk_multiplier)
            or not 1 <= config.max_risk_multiplier <= 1.5
        ):
            raise ValueError("satellite_overlay max_risk_multiplier must be between 1 and 1.5")
        return config

    def to_dict(self) -> dict:
        return asdict(self)


def apply_satellite_confirmations(
    signal: dict,
    *,
    factor_signal: dict | None = None,
    event_signal: dict | None = None,
    config: SatelliteOverlayConfig | None = None,
) -> dict:
    """Return a core signal with any aligned satellite evidence attached.

    The position remains attributed to the core strategy. A confirmation adds
    priority and a small capped risk-budget multiplier; normal position caps
    and the risk manager still govern the resulting order.
    """
    settings = config or SatelliteOverlayConfig()
    result = dict(signal)
    action = result.get("action")
    direction = "long" if action == "buy" else "short" if action == "short" else None
    if direction is None or result.get("strategy") not in CORE_STRATEGIES:
        return result

    confirmations: list[str] = []
    strength_bonus = 0.0
    risk_bonus = 0.0
    reasons: list[str] = []

    if settings.multi_factor_enabled and factor_signal:
        factor_action = factor_signal.get("action")
        factor_direction = factor_signal.get("direction", "long" if factor_action == "buy" else "flat")
        factor_confirms = (
            direction == "long" and factor_action in {"buy", "hold"} and factor_direction == "long"
        ) or (
            direction == "short" and factor_action == "short" and factor_direction == "short"
        )
        if factor_confirms:
            confirmations.append("multi_factor")
            strength_bonus += settings.multi_factor_strength_bonus
            risk_bonus += settings.multi_factor_risk_bonus
            rank = factor_signal.get("rank")
            score = factor_signal.get("score")
            reasons.append(f"factor rank {rank}" + (f" score {float(score):.2f}" if isinstance(score, (int, float)) else ""))

    if settings.event_driven_enabled and event_signal and event_signal.get("action") == action:
        confirmations.append("event_driven")
        strength_bonus += settings.event_driven_strength_bonus
        risk_bonus += settings.event_driven_risk_bonus
        reasons.append(str(event_signal.get("reason") or "scheduled event reaction"))
        if event_signal.get("event_id"):
            result["event_id"] = event_signal["event_id"]
        result["event_confirmation"] = dict(event_signal)

    if not confirmations:
        return result

    result["strength"] = min(1.0, float(result.get("strength", 0.0)) + strength_bonus)
    try:
        base_risk_multiplier = float(result.get("risk_multiplier", 1.0))
    except (TypeError, ValueError):
        return result
    if not math.isfinite(base_risk_multiplier) or base_risk_multiplier <= 0:
        return result
    result["risk_multiplier"] = min(
        settings.max_risk_multiplier,
        base_risk_multiplier * (1.0 + risk_bonus),
    )
    result["rank_score"] = float(result.get("rank_score", 0.0)) + strength_bonus
    result["satellite_confirmations"] = confirmations
    result["satellite_reasons"] = reasons
    result["reason"] = f"{result.get('reason', '')}; confirmed by {', '.join(confirmations)}"
    return result
