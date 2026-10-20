"""Shared Algo Trader settings and compatibility accessors."""

from src.algo.config import (
    ALGO_FACTOR_SECTOR_BY_SYMBOL,
    ALGO_MODE_CONFIG,
    ALGO_PORTFOLIO_COORDINATOR_CONFIG,
    ALGO_PORTFOLIO_UNIVERSE,
    ALGO_RUNTIME_CONFIG,
    ALGO_RUNTIME_SETTINGS,
    ALGO_SERVICE_CONFIG,
    AlgoRuntimeSettings,
)

_ALGO_RUNTIME_DEFAULTS = ALGO_RUNTIME_SETTINGS.as_legacy_dict()

# Runtime tuning values that are shared by the live coordinator and research.
STRATEGY_CONFIG = {
    "portfolio_scan_seconds": 60,
    "universe_retry_seconds": 300,
    "quote_stale_seconds": 90,
    "extended_hours_max_spread_pct": 0.02,
    "regime_llm_advisor_enabled": True,
    "regime_llm_default_model": "gpt-6-luna",
    "regime_llm_timeout_seconds": 10,
    "risk_per_trade_pct": 0.005,
    "max_strategy_position_pct": 0.05,
    "take_profit_r_multiple": 2.0,
    **ALGO_RUNTIME_CONFIG,
}


def get_algo_runtime_settings() -> AlgoRuntimeSettings:
    """Return validated effective settings, including compatibility overrides."""
    values = ALGO_RUNTIME_SETTINGS.as_legacy_dict()
    for config in (ALGO_RUNTIME_CONFIG, STRATEGY_CONFIG):
        values.update({
            key: config[key] for key, default in _ALGO_RUNTIME_DEFAULTS.items()
            if key in config and config[key] != default
        })
    return AlgoRuntimeSettings(**values)


def resolve_mode(cli_mode: str | None = None) -> str:
    """Resolve the only supported account route: the Algo paper account."""
    if cli_mode is not None and cli_mode.lower() != "algo":
        raise ValueError("Only the algo account route is supported")
    return "algo"


def get_mode_config(mode: str | None = None) -> dict:
    """Return Algo settings; reject removed Swing and Day account routes."""
    resolve_mode(mode)
    return ALGO_MODE_CONFIG
