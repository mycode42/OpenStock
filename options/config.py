"""Validated options sleeve settings, with a JSON override outside code."""

from __future__ import annotations

from copy import deepcopy
import json
import math
import os
from pathlib import Path

from runtime.paths import DATA_DIR


DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.default.json")
USER_CONFIG_PATH = DATA_DIR / "options" / "config.json"


def _merge(base: dict, override: dict) -> dict:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def load_options_config(path: str | Path | None = None) -> dict:
    """Load packaged defaults, then a user JSON file and supported env overrides."""
    config = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    override_path = Path(path) if path else Path(os.getenv("ALPACA_OPTIONS_CONFIG", USER_CONFIG_PATH))
    if override_path.exists():
        override = json.loads(override_path.read_text(encoding="utf-8"))
        if not isinstance(override, dict):
            raise ValueError("options config must be a JSON object")
        config = _merge(config, override)
    if os.getenv("ALPACA_OPTIONS_FEED"):
        config["market_data_feed"] = os.environ["ALPACA_OPTIONS_FEED"].strip().lower()
    if config.get("market_data_feed") not in {"opra", "indicative"}:
        raise ValueError("market_data_feed must be opra or indicative")
    for section, fields in {
        "risk": ("max_risk_per_trade_pct", "max_total_defined_risk_pct", "max_daily_loss_pct", "max_delta_notional_pct", "max_vega_per_vol_point_pct", "max_gamma_1pct_loss_pct", "max_theta_loss_per_day_pct", "max_margin_pct"),
        "vrp": ("min_iv_minus_rv", "short_delta_min", "short_delta_max", "wing_delta_max"),
        "event_driven": ("min_move_edge_pct", "min_llm_confidence"),
    }.items():
        for field in fields:
            value = config[section][field]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{section}.{field} must be a finite non-negative number")
    if config["risk"]["max_risk_per_trade_pct"] <= 0 or config["risk"]["max_contracts_per_trade"] < 1:
        raise ValueError("risk budget and max_contracts_per_trade must be positive")
    if config["vrp"]["short_delta_min"] > config["vrp"]["short_delta_max"]:
        raise ValueError("VRP short delta range is inverted")
    if config["vrp"]["max_dte"] < config["vrp"]["min_dte"]:
        raise ValueError("VRP DTE range is inverted")
    weights = config.get("regime_risk_multipliers", {})
    if not isinstance(weights, dict) or any(
        not isinstance(key, str) or not key
        or isinstance(value, bool) or not isinstance(value, (int, float))
        or not math.isfinite(value) or not 0 <= value <= 2
        for key, value in weights.items()
    ):
        raise ValueError("regime_risk_multipliers must map regime names to finite weights in [0, 2]")
    return config


__all__ = ["DEFAULT_CONFIG_PATH", "USER_CONFIG_PATH", "load_options_config"]
