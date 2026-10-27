"""Factory for one-at-a-time options strategy expression research."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from options.config import DEFAULT_CONFIG_PATH
from strategies.options.directional_overlay import DirectionalOverlayStrategy
from strategies.options.llm_driven import LlmDrivenOptionsStrategy
from strategies.options.long_volatility import LongVolatilitySleeve
from strategies.options.vrp import VolatilityRiskPremiumStrategy


STRATEGIES = {
    "vrp": VolatilityRiskPremiumStrategy,
    "llm_driven": LlmDrivenOptionsStrategy,
    "directional_overlay": DirectionalOverlayStrategy,
    "long_volatility": LongVolatilitySleeve,
}
STRATEGY_IDS = {
    "vrp": "options_vrp",
    "llm_driven": "options_llm_driven",
    "directional_overlay": "options_directional_overlay",
    "long_volatility": "options_long_volatility_sleeve",
}


def live_defaults(path: str | Path = DEFAULT_CONFIG_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def make_strategy(
    name: str,
    *,
    parameters: dict,
    allowed_regimes: tuple[str, ...] | list[str] | None = None,
    base_config: dict | None = None,
):
    if name not in STRATEGIES:
        raise ValueError(f"unsupported options strategy {name!r}; choose {', '.join(STRATEGIES)}")
    defaults = deepcopy(base_config if base_config is not None else live_defaults())
    section = deepcopy(defaults[name])
    section.update(parameters)
    section["enabled"] = True
    if allowed_regimes is not None:
        regime_filter = sorted(set(map(str, allowed_regimes)))
        if name == "vrp":
            section["allowed_regimes"] = regime_filter
        else:
            base_weights = defaults.get("regime_risk_multipliers", {})
            section["regime_risk_multipliers"] = {
                key: float(value) if key in regime_filter else 0.0
                for key, value in base_weights.items()
            }
            # A new regime label may be supplied by a deployment config.
            for key in regime_filter:
                section["regime_risk_multipliers"].setdefault(key, 1.0)
            if name == "long_volatility":
                section["allowed_regimes"] = regime_filter
    return STRATEGIES[name](section), section


def export_section(
    name: str,
    *,
    parameters: dict,
    allowed_regimes: tuple[str, ...] | list[str] | None,
    base_config: dict,
) -> dict:
    _, section = make_strategy(
        name,
        parameters=parameters,
        allowed_regimes=allowed_regimes,
        base_config=base_config,
    )
    return section


__all__ = ["STRATEGIES", "STRATEGY_IDS", "export_section", "live_defaults", "make_strategy"]
