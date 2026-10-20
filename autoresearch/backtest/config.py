"""Algo replay limits and profile validation."""

from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

from src.config import get_mode_config
from src.algo.strategies.multi_factor.config import MultiFactorConfig
from src.algo.strategies.registry import ALGO_DEFAULT_PARAMETERS
from src.algo.config import ALGO_REGIME_WARMUP_CALENDAR_DAYS

ET = ZoneInfo("America/New_York")

BAR_MINUTES = 5

DAILY_WARMUP_DAYS = 180
MOMENTUM_WARMUP_DAYS = 390
REGIME_WARMUP_DAYS = ALGO_REGIME_WARMUP_CALENDAR_DAYS

MIN_REGIME_DAILY_BARS = 22

FACTOR_WARMUP_DAYS = 260

STRATEGY_NAMES = set(ALGO_DEFAULT_PARAMETERS)

RESEARCH_CONTROL_KEYS = {
    "max_entries_per_day", "max_open_positions", "max_positions_per_strategy",
    "max_entries_per_strategy_per_day", "cooldown_minutes",
    "cooldown_minutes_by_strategy",
}

def multi_factor_warmup_days(profile: dict | None = None) -> int:
    """Calendar-day history needed for the longest configured daily factor."""
    settings = (profile or {}).get("multi_factor") if isinstance(profile, dict) else None
    config = MultiFactorConfig.from_dict(settings)
    return max(FACTOR_WARMUP_DAYS, config.warmup_calendar_days())


def strategy_warmup_days(strategy: str, profile: dict | None = None) -> int:
    """Calendar history needed before replaying strategy signals."""
    if strategy in {"momentum", "swing_momentum"}:
        # Research searches longer-horizon variants even when the live profile
        # is still using its short-horizon baseline.
        return MOMENTUM_WARMUP_DAYS
    if strategy == "multi_factor":
        return max(DAILY_WARMUP_DAYS, multi_factor_warmup_days(profile))
    return DAILY_WARMUP_DAYS

def validate_strategy_filter(strategies: list[str] | None) -> list[str] | None:
    if strategies is None:
        return None
    if not isinstance(strategies, list) or not strategies:
        raise ValueError("strategy filter must be a non-empty list")
    if any(not isinstance(name, str) or name not in STRATEGY_NAMES for name in strategies):
        raise ValueError("strategy filter contains an unknown strategy")
    if len(set(strategies)) != len(strategies):
        raise ValueError("strategy filter names must be unique")
    return list(strategies)

def validate_research_controls(controls: dict | None) -> dict:
    if controls is None:
        return {}
    if not isinstance(controls, dict):
        raise ValueError("research controls must be an object")
    unknown = set(controls) - RESEARCH_CONTROL_KEYS
    if unknown:
        raise ValueError(f"unknown research control(s): {', '.join(sorted(unknown))}")
    normalized = dict(controls)
    live_risk = get_mode_config("algo")["risk"]
    limits = {
        "max_entries_per_day": (1, live_risk["max_trades_per_day"]),
        "max_open_positions": (1, live_risk["max_open_positions"]),
        "max_positions_per_strategy": (1, live_risk["max_positions_per_strategy"]),
        "cooldown_minutes": (0, 390),
    }
    for key, (minimum, maximum) in limits.items():
        if key not in normalized:
            continue
        value = normalized[key]
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"{key} must be an integer between {minimum} and {maximum}")
    per_strategy = normalized.get("max_entries_per_strategy_per_day", {})
    if not isinstance(per_strategy, dict):
        raise ValueError("max_entries_per_strategy_per_day must be an object")
    live_daily_caps = live_risk.get("max_entries_per_strategy_per_day", {})
    for name, value in per_strategy.items():
        maximum = live_daily_caps.get(name, live_risk["max_trades_per_day"])
        if name not in STRATEGY_NAMES or isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise ValueError(f"per-strategy daily entry limits must not exceed the live Algo cap of {maximum}")
    normalized["max_entries_per_strategy_per_day"] = per_strategy
    cooldowns = normalized.get("cooldown_minutes_by_strategy", {})
    if not isinstance(cooldowns, dict):
        raise ValueError("cooldown_minutes_by_strategy must be an object")
    for name, value in cooldowns.items():
        maximum = live_risk.get("cooldown_minutes_by_strategy", {}).get(name, 390)
        if name not in STRATEGY_NAMES or isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
            raise ValueError("strategy cooldowns must use known strategies and stay within the live Algo cap")
    normalized["cooldown_minutes_by_strategy"] = cooldowns
    return normalized
