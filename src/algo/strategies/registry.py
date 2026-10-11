"""Strategy-family registry and default rule profiles."""

from __future__ import annotations

from dataclasses import replace

from src.algo.strategies import mean_reversion, momentum, trend_following
from src.algo.strategies.catalog import STRATEGY_CATALOG, STRATEGY_IDS
from src.algo.strategies.mean_reversion import swing as swing_mean_reversion
from src.algo.strategies.momentum import swing as swing_momentum
from src.algo.strategies.trend_following import swing as swing_trend_following
from src.algo.strategies.event_driven.config import EventStrategyConfig
from src.algo.strategies.multi_factor.config import DEFAULT_CONFIG as MULTI_FACTOR_DEFAULT_CONFIG
from src.algo.strategies.satellite_overlay import SatelliteOverlayConfig
from src.algo.strategies.mean_reversion.swing import SwingMeanReversionConfig
from src.algo.strategies.momentum.swing import SwingMomentumConfig
from src.algo.strategies.trend_following.swing import SwingTrendConfig
from src.config import get_algo_runtime_settings


RULE_MODULES = {
    "mean_reversion": mean_reversion,
    "trend_following": trend_following,
    "momentum": momentum,
    "swing_mean_reversion": swing_mean_reversion,
}
ALGO_RULE_MODULES = {
    "trend_following": swing_trend_following,
    "swing_mean_reversion": swing_mean_reversion,
    "intraday_trend": trend_following,
    "intraday_momentum": momentum,
}
# Enable the conservative, risk-capped event satellite in Algo mode. The
# strategy class itself remains disabled by default for standalone use.
EVENT_DEFAULT_CONFIG = EventStrategyConfig(enabled=True).to_dict()
SWING_MEAN_REVERSION_DEFAULT_CONFIG = SwingMeanReversionConfig().to_dict()
DAY_DEFAULT_PARAMETERS = {
    "trend_following": {"fast_period": 9, "slow_period": 21, "allow_long": True, "allow_short": True},
    "momentum": {"lookback": 12, "minimum_return": 0.015, "minimum_volume_ratio": 1.2, "allow_long": True, "allow_short": True},
}

ALGO_DEFAULT_PARAMETERS = {
    "trend_following": SwingTrendConfig().to_dict(),
    "momentum": SwingMomentumConfig().to_dict(),
    # Explicit research-only swing names keep the bank's horizon labels
    # unambiguous while the live Algo runner retains its existing identifiers.
    "swing_trend_following": SwingTrendConfig().to_dict(),
    "swing_momentum": SwingMomentumConfig().to_dict(),
    "mean_reversion": {
        "signal_model": "opening_dip",
        "minimum_dip_pct": 0.014,
        "minimum_bounce_pct": 0.002,
        "minimum_volume_ratio": 1.3,
        "entry_window_minutes": 90,
        "higher_low_bars": 1,
        "minimum_rip_pct": 0.014,
        "minimum_fade_pct": 0.002,
        "lower_high_bars": 1,
        "allow_long": True,
        "allow_short": True,
    },
    "multi_factor": MULTI_FACTOR_DEFAULT_CONFIG.to_dict(),
    "event_driven": EVENT_DEFAULT_CONFIG,
    "swing_mean_reversion": SWING_MEAN_REVERSION_DEFAULT_CONFIG,
    "intraday_trend": {"enabled": True, **DAY_DEFAULT_PARAMETERS["trend_following"]},
    "intraday_momentum": {"enabled": True, **DAY_DEFAULT_PARAMETERS["momentum"]},
}

_registered_strategy_ids = set(ALGO_DEFAULT_PARAMETERS) - {"satellite_overlay"}
if _registered_strategy_ids != STRATEGY_IDS:
    missing = sorted(_registered_strategy_ids - STRATEGY_IDS)
    unconfigured = sorted(STRATEGY_IDS - _registered_strategy_ids)
    raise RuntimeError(
        f"Strategy catalog and parameter registry differ: missing={missing}, unconfigured={unconfigured}"
    )

PARAMETER_KEYS = {
    "trend_following": set(SwingTrendConfig.__dataclass_fields__),
    "momentum": set(SwingMomentumConfig.__dataclass_fields__),
    "swing_trend_following": set(SwingTrendConfig.__dataclass_fields__),
    "swing_momentum": set(SwingMomentumConfig.__dataclass_fields__),
    "mean_reversion": {
        "window", "band_width", "entry_rsi", "exit_rsi", "signal_model",
        "minimum_dip_pct", "minimum_volume_ratio", "entry_window_minutes",
        "minimum_bounce_pct", "higher_low_bars", "short_entry_rsi", "short_exit_rsi",
        "minimum_rip_pct", "minimum_fade_pct", "lower_high_bars", "allow_long", "allow_short",
    },
    "multi_factor": set(MULTI_FACTOR_DEFAULT_CONFIG.to_dict()),
    "event_driven": set(EVENT_DEFAULT_CONFIG),
    "swing_mean_reversion": set(SWING_MEAN_REVERSION_DEFAULT_CONFIG),
    "intraday_trend": {
        "enabled", "fast_period", "slow_period", "confirmation_bars", "minimum_slope_pct", "allow_long", "allow_short",
    },
    "intraday_momentum": {
        "enabled", "lookback", "minimum_return", "minimum_volume_ratio", "max_extension_pct",
        "minimum_short_return", "minimum_short_volume_ratio", "allow_long", "allow_short",
    },
    "satellite_overlay": set(SatelliteOverlayConfig.__dataclass_fields__),
}


def validate_parameter_profile(profile: dict) -> dict:
    """Validate research-only signal parameters; portfolio risk stays fixed."""
    if not isinstance(profile, dict):
        raise ValueError("Strategy profile must be a JSON object")
    normalized = {}
    for strategy, values in profile.items():
        if strategy not in PARAMETER_KEYS or not isinstance(values, dict):
            raise ValueError(f"Unknown or malformed strategy profile: {strategy}")
        unknown = set(values) - PARAMETER_KEYS[strategy]
        if unknown:
            raise ValueError(f"Unsupported {strategy} parameter(s): {', '.join(sorted(unknown))}")
        if strategy == "multi_factor":
            from src.algo.strategies.multi_factor.config import MultiFactorConfig
            normalized[strategy] = MultiFactorConfig.from_dict(values).to_dict()
            continue
        if strategy == "event_driven":
            normalized[strategy] = EventStrategyConfig.from_dict(values).to_dict()
            continue
        if strategy == "swing_mean_reversion":
            normalized[strategy] = SwingMeanReversionConfig.from_dict(values).to_dict()
            continue
        if strategy == "trend_following":
            normalized[strategy] = SwingTrendConfig.from_dict(values).to_dict()
            continue
        if strategy == "momentum":
            normalized[strategy] = SwingMomentumConfig.from_dict(values).to_dict()
            continue
        if strategy == "swing_trend_following":
            normalized[strategy] = SwingTrendConfig.from_dict(values).to_dict()
            continue
        if strategy == "swing_momentum":
            normalized[strategy] = SwingMomentumConfig.from_dict(values).to_dict()
            continue
        if strategy == "satellite_overlay":
            normalized[strategy] = SatelliteOverlayConfig.from_dict(values).to_dict()
            continue
        normalized[strategy] = dict(values)

    def number(strategy: str, key: str, default: float, low: float, high: float, *, integer: bool = False):
        value = normalized.get(strategy, {}).get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{strategy} {key} must be numeric")
        if integer and not isinstance(value, int):
            raise ValueError(f"{strategy} {key} must be an integer")
        if not low <= value <= high:
            raise ValueError(f"{strategy} {key} must be between {low} and {high}")
        return value

    number("intraday_trend", "fast_period", 9, 1, 200, integer=True)
    number("intraday_trend", "slow_period", 21, 1, 200, integer=True)
    if normalized.get("intraday_trend", {}).get("fast_period", 9) >= normalized.get("intraday_trend", {}).get("slow_period", 21):
        raise ValueError("intraday_trend fast_period must be less than slow_period")
    number("intraday_trend", "confirmation_bars", 1, 1, 10, integer=True)
    number("intraday_trend", "minimum_slope_pct", 0, 0, 0.1)
    number("intraday_momentum", "lookback", 12, 1, 200, integer=True)
    number("intraday_momentum", "minimum_return", 0.015, 0, 1)
    number("intraday_momentum", "minimum_volume_ratio", 1.2, 0.1, 20)
    number("intraday_momentum", "max_extension_pct", 0, 0, 1)
    number("intraday_momentum", "minimum_short_return", normalized.get("intraday_momentum", {}).get("minimum_return", 0.015), 0, 1)
    number("intraday_momentum", "minimum_short_volume_ratio", normalized.get("intraday_momentum", {}).get("minimum_volume_ratio", 1.2), 0.1, 20)
    for key in ("allow_long", "allow_short"):
        for strategy in ("intraday_trend", "intraday_momentum"):
            if key in normalized.get(strategy, {}) and not isinstance(normalized[strategy][key], bool):
                raise ValueError(f"{strategy} {key} must be a boolean")
    number("mean_reversion", "window", 20, 5, 200, integer=True)
    number("mean_reversion", "band_width", 2.0, 0.1, 10)
    entry_rsi = number("mean_reversion", "entry_rsi", 30, 0, 100)
    exit_rsi = number("mean_reversion", "exit_rsi", 58, 0, 100)
    if entry_rsi > exit_rsi:
        raise ValueError("mean_reversion RSI values must satisfy 0 <= entry_rsi <= exit_rsi <= 100")
    short_entry_rsi = number("mean_reversion", "short_entry_rsi", 68, 0, 100)
    short_exit_rsi = number("mean_reversion", "short_exit_rsi", 42, 0, 100)
    if short_exit_rsi > short_entry_rsi:
        raise ValueError("mean_reversion RSI values must satisfy short_exit_rsi <= short_entry_rsi")
    model = normalized.get("mean_reversion", {}).get("signal_model", "bollinger")
    if not isinstance(model, str) or model not in {"bollinger", "opening_dip"}:
        raise ValueError("mean_reversion signal_model must be bollinger or opening_dip")
    number("mean_reversion", "minimum_dip_pct", 0, 0, 0.5)
    number("mean_reversion", "minimum_volume_ratio", 1.2, 0, 20)
    number("mean_reversion", "entry_window_minutes", 90, 5, 390, integer=True)
    number("mean_reversion", "minimum_bounce_pct", 0.001, 0, 0.1)
    number("mean_reversion", "higher_low_bars", 1, 0, 10, integer=True)
    number("mean_reversion", "minimum_rip_pct", normalized.get("mean_reversion", {}).get("minimum_dip_pct", 0), 0, 0.5)
    number("mean_reversion", "minimum_fade_pct", normalized.get("mean_reversion", {}).get("minimum_bounce_pct", 0.001), 0, 0.1)
    number("mean_reversion", "lower_high_bars", normalized.get("mean_reversion", {}).get("higher_low_bars", 1), 0, 10, integer=True)
    for key in ("allow_long", "allow_short"):
        if key in normalized.get("mean_reversion", {}) and not isinstance(normalized["mean_reversion"][key], bool):
            raise ValueError(f"mean_reversion {key} must be a boolean")
    return normalized


def evaluate(
    name: str, mode: str, ticker: str, bars: list[dict], position_qty: float = 0,
    parameters: dict | None = None,
    *, runtime_enabled_override: bool | None = None,
):
    """Call a strategy module with defaults matched to its bar interval."""
    if mode == "algo" and name == "momentum":
        # Momentum is cross-sectional and must be ranked once per universe.
        return None
    if mode == "algo" and name in {"trend_following", "swing_mean_reversion"}:
        module = ALGO_RULE_MODULES[name]
        values = {**ALGO_DEFAULT_PARAMETERS[name], **(parameters or {})}
        return module.signal(ticker, bars, position_qty=position_qty, parameters=values)
    if mode == "algo" and name in {"intraday_trend", "intraday_momentum"}:
        flag = STRATEGY_CATALOG[name].runtime_setting
        runtime_enabled = runtime_enabled_override
        if runtime_enabled is None and flag:
            runtime_enabled = getattr(get_algo_runtime_settings(), flag)
        if not runtime_enabled:
            return None
        values = {**ALGO_DEFAULT_PARAMETERS[name], **(parameters or {})}
        if not values.get("enabled", False):
            return None
        module = ALGO_RULE_MODULES[name]
        call_values = {key: value for key, value in values.items() if key != "enabled"}
        signal = module.signal(ticker, bars, position_qty=position_qty, **call_values)
        return replace(signal, strategy=name) if signal is not None else None

    module = RULE_MODULES.get(name)
    if module is None:
        return None
    kwargs = {}
    if mode == "day":
        if name == "trend_following":
            kwargs = dict(DAY_DEFAULT_PARAMETERS[name])
        elif name in {"momentum", "mean_reversion"}:
            kwargs = dict(DAY_DEFAULT_PARAMETERS.get(name, ALGO_DEFAULT_PARAMETERS.get(name, {})))
    elif mode == "algo":
        if name == "mean_reversion":
            kwargs = dict(ALGO_DEFAULT_PARAMETERS[name])
    kwargs.update(parameters or {})
    if kwargs:
        return module.signal(ticker, bars, position_qty=position_qty, **kwargs)
    return module.signal(ticker, bars, position_qty=position_qty)


__all__ = [
    "ALGO_DEFAULT_PARAMETERS", "PARAMETER_KEYS", "RULE_MODULES", "evaluate",
    "validate_parameter_profile",
]
