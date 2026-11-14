"""Fixed strategy settings used by options research replays."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyConfiguration:
    parameters: dict
    allowed_regimes: tuple[str, ...]


def fixed_strategy_configuration(
    strategy: str,
    base_config: dict,
    *,
    parameters: dict | None = None,
) -> StrategyConfiguration:
    """Build the fixed settings profile without creating a search pool."""
    strategy_config = base_config.get(strategy, {})
    allowed_regimes = strategy_config.get("allowed_regimes")
    if allowed_regimes is None:
        allowed_regimes = [
            name for name, multiplier in base_config.get("regime_risk_multipliers", {}).items()
            if float(multiplier) > 0
        ]
    return StrategyConfiguration(
        dict(parameters or {}),
        tuple(sorted({str(regime) for regime in allowed_regimes})),
    )
