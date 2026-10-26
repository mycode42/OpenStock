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
        "event_driven": ("min_llm_confidence",),
        "directional_overlay": (
            "long_delta", "short_delta", "min_signal_confidence",
            "max_spread_pct", "max_risk_per_trade_pct", "profit_target_pct",
            "stop_loss_pct",
        ),
        "long_volatility": (
            "min_realized_volatility", "threshold_risk_multiplier", "otm_delta",
            "iv_percentile_ceiling", "max_spread_pct", "max_premium_per_trade_usd",
            "profit_target_pct", "stop_loss_pct", "max_risk_per_trade_pct",
        ),
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
    vrp = config["vrp"]
    vrp_underlyings = vrp.get("underlyings")
    if not isinstance(vrp_underlyings, list) or not vrp_underlyings or any(
        not isinstance(symbol, str) or not symbol or symbol != symbol.upper()
        for symbol in vrp_underlyings
    ) or len(set(vrp_underlyings)) != len(vrp_underlyings):
        raise ValueError("vrp.underlyings must be a non-empty list of unique uppercase symbols")
    if len(vrp_underlyings) > 10:
        raise ValueError("vrp.underlyings is limited to ten symbols")
    if (
        isinstance(vrp.get("max_entries_per_day"), bool)
        or not isinstance(vrp.get("max_entries_per_day"), int)
        or vrp["max_entries_per_day"] < 1
    ):
        raise ValueError("vrp.max_entries_per_day must be a positive integer")
    event = config["event_driven"]
    for field in ("min_dte", "max_dte", "target_dte", "max_symbols_per_cycle", "min_open_interest"):
        value = event[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"event_driven.{field} must be a non-negative integer")
    if event["max_dte"] < event["min_dte"]:
        raise ValueError("event-driven DTE range is inverted")
    if event["min_dte"] < 1:
        raise ValueError("event-driven minimum DTE must be at least one day")
    if not event["min_dte"] <= event["target_dte"] <= event["max_dte"]:
        raise ValueError("event-driven target_dte must be within its DTE range")
    if event["min_llm_confidence"] > 1:
        raise ValueError("event-driven min_llm_confidence must be in [0, 1]")
    directional = config["directional_overlay"]
    if not isinstance(directional.get("enabled"), bool):
        raise ValueError("directional_overlay.enabled must be a boolean")
    underlyings = directional.get("underlyings")
    if underlyings is None and isinstance(directional.get("underlying"), str):
        underlyings = [directional["underlying"]]
    if not isinstance(underlyings, list) or not underlyings or any(
        not isinstance(symbol, str) or not symbol or symbol != symbol.upper()
        for symbol in underlyings
    ) or len(set(underlyings)) != len(underlyings):
        raise ValueError("directional_overlay.underlyings must be a non-empty list of unique uppercase symbols")
    if len(underlyings) > 50:
        raise ValueError("directional_overlay.underlyings is limited to fifty symbols")
    for field in ("min_dte", "max_dte", "target_dte", "min_open_interest",
                  "max_signal_age_seconds", "max_entries_per_day", "max_holding_days",
                  "close_dte"):
        value = directional[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"directional_overlay.{field} must be a non-negative integer")
    if (
        directional["min_dte"] < 1
        or directional["max_dte"] < directional["min_dte"]
        or not directional["min_dte"] <= directional["target_dte"] <= directional["max_dte"]
    ):
        raise ValueError("directional_overlay DTE window or target is invalid")
    if (
        directional["max_signal_age_seconds"] < 1
        or directional["max_entries_per_day"] < 1
        or directional["max_holding_days"] < 1
    ):
        raise ValueError("directional_overlay signal age, holding, and daily entry limits must be positive")
    if directional["close_dte"] >= directional["max_dte"]:
        raise ValueError("directional_overlay.close_dte must be below max_dte")
    if not 0 < directional["short_delta"] < directional["long_delta"] < 1:
        raise ValueError("directional_overlay deltas must satisfy 0 < short_delta < long_delta < 1")
    if directional["min_signal_confidence"] > 1:
        raise ValueError("directional_overlay.min_signal_confidence must be in [0, 1]")
    if not 0 < directional["max_spread_pct"] <= 1:
        raise ValueError("directional_overlay.max_spread_pct must be in (0, 1]")
    if not 0 < directional["max_risk_per_trade_pct"] <= 1:
        raise ValueError("directional_overlay.max_risk_per_trade_pct must be in (0, 1]")
    if directional["profit_target_pct"] <= 0 or not 0 < directional["stop_loss_pct"] <= 1:
        raise ValueError("directional_overlay profit target and stop loss are invalid")
    long_vol = config["long_volatility"]
    if not isinstance(long_vol.get("enabled"), bool) or not isinstance(long_vol.get("close_on_regime_exit"), bool):
        raise ValueError("long_volatility enabled and close_on_regime_exit must be booleans")
    if not isinstance(long_vol.get("bootstrap_iv_history_from_theta", True), bool):
        raise ValueError("long_volatility.bootstrap_iv_history_from_theta must be a boolean")
    if long_vol.get("mode") not in {"alongside", "exclusive"}:
        raise ValueError("long_volatility.mode must be alongside or exclusive")
    if long_vol.get("activation_mode") not in {"regime_only", "regime_or_realized_vol"}:
        raise ValueError("long_volatility.activation_mode is unsupported")
    if long_vol.get("structure") not in {"strangle", "straddle"}:
        raise ValueError("long_volatility.structure must be strangle or straddle")
    allowed_vol_regimes = long_vol.get("allowed_regimes")
    if not isinstance(allowed_vol_regimes, list) or "volatile" not in {
        str(value).lower() for value in allowed_vol_regimes
    }:
        raise ValueError("long_volatility.allowed_regimes must include volatile")
    underlyings = long_vol.get("underlyings")
    if not isinstance(underlyings, list) or not underlyings or any(
        not isinstance(symbol, str) or not symbol or symbol != symbol.upper()
        for symbol in underlyings
    ) or len(set(underlyings)) != len(underlyings):
        raise ValueError("long_volatility.underlyings must be a non-empty list of unique uppercase symbols")
    if len(underlyings) > 10:
        raise ValueError("long_volatility.underlyings is limited to ten symbols")
    style_map = long_vol.get("exercise_style_by_underlying", {})
    if not isinstance(style_map, dict) or any(style not in {"american", "european"} for style in style_map.values()):
        raise ValueError("long_volatility.exercise_style_by_underlying must map symbols to option styles")
    for field in ("min_dte", "max_dte", "target_dte", "iv_percentile_lookback_sessions",
                  "iv_rank_min_samples", "realized_lookback_sessions", "min_open_interest",
                  "close_dte", "max_holding_days", "max_entries_per_day"):
        value = long_vol[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"long_volatility.{field} must be a non-negative integer")
    if (
        long_vol["min_dte"] < 1 or long_vol["max_dte"] < long_vol["min_dte"]
        or not long_vol["min_dte"] <= long_vol["target_dte"] <= long_vol["max_dte"]
    ):
        raise ValueError("long_volatility DTE window or target is invalid")
    if long_vol["iv_rank_min_samples"] < 1 or long_vol["iv_percentile_lookback_sessions"] < long_vol["iv_rank_min_samples"]:
        raise ValueError("long_volatility IV history window must cover its minimum sample count")
    atm_delta_tolerance = long_vol.get("atm_delta_tolerance", 0.20)
    if (
        isinstance(atm_delta_tolerance, bool)
        or not isinstance(atm_delta_tolerance, (int, float))
        or not math.isfinite(atm_delta_tolerance)
        or not 0 < atm_delta_tolerance <= 0.5
    ):
        raise ValueError("long_volatility.atm_delta_tolerance must be in (0, 0.5]")
    if long_vol["max_holding_days"] < 1 or long_vol["max_entries_per_day"] < 1:
        raise ValueError("long_volatility holding and entry limits must be positive")
    if long_vol["close_dte"] >= long_vol["max_dte"]:
        raise ValueError("long_volatility.close_dte must be below max_dte")
    if not 0 < long_vol["otm_delta"] < 0.5:
        raise ValueError("long_volatility.otm_delta must be in (0, 0.5)")
    if not 0 < long_vol["iv_percentile_ceiling"] <= 100:
        raise ValueError("long_volatility.iv_percentile_ceiling must be in (0, 100]")
    if not 0 < long_vol["max_spread_pct"] <= 1 or long_vol["max_premium_per_trade_usd"] <= 0:
        raise ValueError("long_volatility spread and premium limits must be positive")
    if long_vol["profit_target_pct"] <= 0 or not 0 < long_vol["stop_loss_pct"] <= 1:
        raise ValueError("long_volatility profit target and stop loss are invalid")
    if not 0 < long_vol["max_risk_per_trade_pct"] <= 1:
        raise ValueError("long_volatility.max_risk_per_trade_pct must be in (0, 1]")
    if long_vol["threshold_risk_multiplier"] > 2:
        raise ValueError("long_volatility.threshold_risk_multiplier must be at most 2")
    iv_rv_ratio = long_vol.get("max_iv_to_realized_ratio")
    if iv_rv_ratio is not None and (
        isinstance(iv_rv_ratio, bool) or not isinstance(iv_rv_ratio, (int, float))
        or not math.isfinite(iv_rv_ratio) or iv_rv_ratio <= 0
    ):
        raise ValueError("long_volatility.max_iv_to_realized_ratio must be null or positive")
    if (
        (long_vol["activation_mode"] == "regime_or_realized_vol" or iv_rv_ratio is not None)
        and long_vol["realized_lookback_sessions"] < 2
    ):
        raise ValueError("long_volatility realized-volatility lookback must be at least two sessions")
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
