"""Strategy-family registry and default rule profiles."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

from strategies import mean_reversion, momentum, trend_following
from strategies.catalog import STRATEGY_CATALOG, STRATEGY_IDS, canonical_strategy_id
from strategies.mean_reversion import swing as swing_mean_reversion
from strategies.momentum import swing as swing_momentum
from strategies.trend_following import swing as swing_trend_following
from strategies.event_driven.config import EventStrategyConfig
from strategies.multi_factor.config import DEFAULT_CONFIG as MULTI_FACTOR_DEFAULT_CONFIG
from strategies.mean_reversion.swing import SwingMeanReversionConfig
from strategies.momentum.swing import SwingMomentumConfig
from strategies.trend_following.swing import SwingTrendConfig
from portfolio.config import get_algo_runtime_settings


RULE_MODULES = {
    # Keep the old names available to the legacy per-symbol/day evaluator.
    "mean_reversion": mean_reversion,
    "trend_following": trend_following,
    "momentum": momentum,
    "intraday_mean_reversion": mean_reversion,
    "swing_trend_following": trend_following,
    "swing_momentum": momentum,
    "swing_mean_reversion": swing_mean_reversion,
}
ALGO_RULE_MODULES = {
    "swing_trend_following": swing_trend_following,
    "swing_mean_reversion": swing_mean_reversion,
    "intraday_trend_following": trend_following,
    "intraday_momentum": momentum,
}
# Enable the conservative, risk-capped event strategy in Algo mode. The
# strategy class itself remains disabled by default for standalone use.
EVENT_DEFAULT_CONFIG = EventStrategyConfig(enabled=True).to_dict()
SWING_MEAN_REVERSION_DEFAULT_CONFIG = SwingMeanReversionConfig().to_dict()
DAY_DEFAULT_PARAMETERS = {
    "trend_following": {"fast_period": 9, "slow_period": 21, "allow_long": True, "allow_short": True},
    "momentum": {"lookback": 12, "minimum_return": 0.015, "minimum_volume_ratio": 1.2, "allow_long": True, "allow_short": True},
}

ALGO_DEFAULT_PARAMETERS = {
    "swing_trend_following": SwingTrendConfig().to_dict(),
    "swing_momentum": SwingMomentumConfig().to_dict(),
    "intraday_mean_reversion": {
        "signal_model": "opening_dip",
        "window": 20,
        "band_width": 2.0,
        "entry_rsi": 32.0,
        "exit_rsi": 58.0,
        "short_entry_rsi": 68.0,
        "short_exit_rsi": 42.0,
        "regime_filter": "none",
        "efficiency_ratio_period": 20,
        "efficiency_ratio_max": 0.35,
        "adx_period": 14,
        "adx_max": 20.0,
        "threshold_mode": "fixed",
        "volatility_lookback": 20,
        "high_volatility_ratio": 1.25,
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
    "swing_multi_factor": MULTI_FACTOR_DEFAULT_CONFIG.to_dict(),
    "swing_event_driven": EVENT_DEFAULT_CONFIG,
    "swing_mean_reversion": SWING_MEAN_REVERSION_DEFAULT_CONFIG,
    "intraday_trend_following": {"enabled": True, **DAY_DEFAULT_PARAMETERS["trend_following"]},
    "intraday_momentum": {"enabled": True, **DAY_DEFAULT_PARAMETERS["momentum"]},
}
BASE_ALGO_DEFAULT_PARAMETERS = deepcopy(ALGO_DEFAULT_PARAMETERS)
REGIME_PARAMETER_PROFILES: dict[str, dict[str, dict]] = {}
ACTIVE_PROFILE_METADATA: dict = {}

_registered_strategy_ids = set(ALGO_DEFAULT_PARAMETERS)
if _registered_strategy_ids != STRATEGY_IDS:
    missing = sorted(_registered_strategy_ids - STRATEGY_IDS)
    unconfigured = sorted(STRATEGY_IDS - _registered_strategy_ids)
    raise RuntimeError(
        f"Strategy catalog and parameter registry differ: missing={missing}, unconfigured={unconfigured}"
    )

PARAMETER_KEYS = {
    "swing_trend_following": set(SwingTrendConfig.__dataclass_fields__),
    "swing_momentum": set(SwingMomentumConfig.__dataclass_fields__),
    "intraday_mean_reversion": {
        "window", "band_width", "entry_rsi", "exit_rsi", "signal_model",
        "minimum_dip_pct", "minimum_volume_ratio", "entry_window_minutes",
        "minimum_bounce_pct", "higher_low_bars", "short_entry_rsi", "short_exit_rsi",
        "minimum_rip_pct", "minimum_fade_pct", "lower_high_bars", "allow_long", "allow_short",
        "regime_filter", "efficiency_ratio_period", "efficiency_ratio_max",
        "adx_period", "adx_max", "threshold_mode", "volatility_lookback",
        "high_volatility_ratio",
    },
    "swing_multi_factor": set(MULTI_FACTOR_DEFAULT_CONFIG.to_dict()),
    "swing_event_driven": set(EVENT_DEFAULT_CONFIG),
    "swing_mean_reversion": set(SWING_MEAN_REVERSION_DEFAULT_CONFIG),
    "intraday_trend_following": {
        "enabled", "fast_period", "slow_period", "confirmation_bars", "minimum_slope_pct", "allow_long", "allow_short",
    },
    "intraday_momentum": {
        "enabled", "lookback", "minimum_return", "minimum_volume_ratio", "max_extension_pct",
        "minimum_short_return", "minimum_short_volume_ratio", "allow_long", "allow_short",
    },
}


def validate_parameter_profile(profile: dict) -> dict:
    """Validate research-only signal parameters; portfolio risk stays fixed."""
    if not isinstance(profile, dict):
        raise ValueError("Strategy profile must be a JSON object")
    normalized = {}
    for supplied_strategy, values in profile.items():
        # Old profiles carried confirmation overlay knobs. Ignore that retired
        # section while preserving the strategy parameters beside it.
        if supplied_strategy == "satellite_overlay":
            if not isinstance(values, dict):
                raise ValueError("satellite_overlay must be an object")
            continue
        strategy = canonical_strategy_id(supplied_strategy)
        if strategy in normalized:
            raise ValueError(f"Profile contains both legacy and canonical IDs for {strategy}")
        if strategy not in PARAMETER_KEYS or not isinstance(values, dict):
            raise ValueError(f"Unknown or malformed strategy profile: {supplied_strategy}")
        unknown = set(values) - PARAMETER_KEYS[strategy]
        if unknown:
            raise ValueError(f"Unsupported {strategy} parameter(s): {', '.join(sorted(unknown))}")
        if strategy == "swing_multi_factor":
            from strategies.multi_factor.config import MultiFactorConfig
            normalized[strategy] = MultiFactorConfig.from_dict(values).to_dict()
            continue
        if strategy == "swing_event_driven":
            normalized[strategy] = EventStrategyConfig.from_dict(values).to_dict()
            continue
        if strategy == "swing_mean_reversion":
            normalized[strategy] = SwingMeanReversionConfig.from_dict(values).to_dict()
            continue
        if strategy == "swing_trend_following":
            normalized[strategy] = SwingTrendConfig.from_dict(values).to_dict()
            continue
        if strategy == "swing_momentum":
            normalized[strategy] = SwingMomentumConfig.from_dict(values).to_dict()
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

    number("intraday_trend_following", "fast_period", 9, 1, 200, integer=True)
    number("intraday_trend_following", "slow_period", 21, 1, 200, integer=True)
    if normalized.get("intraday_trend_following", {}).get("fast_period", 9) >= normalized.get("intraday_trend_following", {}).get("slow_period", 21):
        raise ValueError("intraday_trend fast_period must be less than slow_period")
    number("intraday_trend_following", "confirmation_bars", 1, 1, 10, integer=True)
    number("intraday_trend_following", "minimum_slope_pct", 0, 0, 0.1)
    number("intraday_momentum", "lookback", 12, 1, 200, integer=True)
    number("intraday_momentum", "minimum_return", 0.015, 0, 1)
    number("intraday_momentum", "minimum_volume_ratio", 1.2, 0.1, 20)
    number("intraday_momentum", "max_extension_pct", 0, 0, 1)
    number("intraday_momentum", "minimum_short_return", normalized.get("intraday_momentum", {}).get("minimum_return", 0.015), 0, 1)
    number("intraday_momentum", "minimum_short_volume_ratio", normalized.get("intraday_momentum", {}).get("minimum_volume_ratio", 1.2), 0.1, 20)
    for key in ("allow_long", "allow_short"):
        for strategy in ("intraday_trend_following", "intraday_momentum"):
            if key in normalized.get(strategy, {}) and not isinstance(normalized[strategy][key], bool):
                raise ValueError(f"{strategy} {key} must be a boolean")
    number("intraday_mean_reversion", "window", 20, 5, 200, integer=True)
    number("intraday_mean_reversion", "band_width", 2.0, 0.1, 10)
    entry_rsi = number("intraday_mean_reversion", "entry_rsi", 30, 0, 100)
    exit_rsi = number("intraday_mean_reversion", "exit_rsi", 58, 0, 100)
    if entry_rsi > exit_rsi:
        raise ValueError("mean_reversion RSI values must satisfy 0 <= entry_rsi <= exit_rsi <= 100")
    short_entry_rsi = number("intraday_mean_reversion", "short_entry_rsi", 68, 0, 100)
    short_exit_rsi = number("intraday_mean_reversion", "short_exit_rsi", 42, 0, 100)
    if short_exit_rsi > short_entry_rsi:
        raise ValueError("mean_reversion RSI values must satisfy short_exit_rsi <= short_entry_rsi")
    model = normalized.get("intraday_mean_reversion", {}).get("signal_model", "bollinger")
    if not isinstance(model, str) or model not in {"bollinger", "opening_dip"}:
        raise ValueError("mean_reversion signal_model must be bollinger or opening_dip")
    number("intraday_mean_reversion", "minimum_dip_pct", 0, 0, 0.5)
    number("intraday_mean_reversion", "minimum_volume_ratio", 1.2, 0, 20)
    number("intraday_mean_reversion", "entry_window_minutes", 90, 5, 390, integer=True)
    number("intraday_mean_reversion", "minimum_bounce_pct", 0.001, 0, 0.1)
    number("intraday_mean_reversion", "higher_low_bars", 1, 0, 10, integer=True)
    number("intraday_mean_reversion", "minimum_rip_pct", normalized.get("intraday_mean_reversion", {}).get("minimum_dip_pct", 0), 0, 0.5)
    number("intraday_mean_reversion", "minimum_fade_pct", normalized.get("intraday_mean_reversion", {}).get("minimum_bounce_pct", 0.001), 0, 0.1)
    number("intraday_mean_reversion", "lower_high_bars", normalized.get("intraday_mean_reversion", {}).get("higher_low_bars", 1), 0, 10, integer=True)
    for key in ("allow_long", "allow_short"):
        if key in normalized.get("intraday_mean_reversion", {}) and not isinstance(normalized["intraday_mean_reversion"][key], bool):
            raise ValueError(f"mean_reversion {key} must be a boolean")
    mean_reversion_values = normalized.get("intraday_mean_reversion", {})
    if mean_reversion_values.get("regime_filter", "none") not in {"none", "er", "adx", "combined"}:
        raise ValueError("mean_reversion regime_filter must be none, er, adx, or combined")
    if mean_reversion_values.get("threshold_mode", "fixed") not in {"fixed", "volatility_regime_adjusted"}:
        raise ValueError("mean_reversion threshold_mode must be fixed or volatility_regime_adjusted")
    number("intraday_mean_reversion", "efficiency_ratio_period", 20, 5, 100, integer=True)
    number("intraday_mean_reversion", "efficiency_ratio_max", 0.35, 0.05, 0.8)
    number("intraday_mean_reversion", "adx_period", 14, 5, 50, integer=True)
    number("intraday_mean_reversion", "adx_max", 20, 5, 50)
    number("intraday_mean_reversion", "volatility_lookback", 20, 5, 100, integer=True)
    number("intraday_mean_reversion", "high_volatility_ratio", 1.25, 1.0, 3.0)
    return normalized


def refresh_active_parameter_profile() -> dict:
    """Reload promoted parameter manifests into the shared registry."""
    from portfolio.profile_store import effective_profile_metadata

    metadata = effective_profile_metadata()
    # Promoted profiles created before the ID cleanup remain usable.
    for section in ("parameters", "regime_profiles", "profile_ids"):
        values = metadata.get(section)
        if not isinstance(values, dict):
            continue
        normalized_values = {}
        for key, value in values.items():
            if section == "profile_ids" and ":" in key:
                strategy, regime = key.split(":", 1)
                normalized_key = f"{canonical_strategy_id(strategy)}:{regime}"
            else:
                normalized_key = canonical_strategy_id(key)
            if normalized_key in normalized_values and normalized_values[normalized_key] != value:
                raise ValueError(f"Conflicting legacy and canonical profile data for {normalized_key}")
            normalized_values[normalized_key] = value
        metadata[section] = normalized_values
    effective = deepcopy(BASE_ALGO_DEFAULT_PARAMETERS)
    for strategy, values in (metadata.get("parameters") or {}).items():
        if strategy not in effective:
            continue
        merged = {**effective[strategy], **values}
        effective[strategy] = validate_parameter_profile({strategy: merged})[strategy]

    regimes: dict[str, dict[str, dict]] = {}
    for strategy, by_regime in (metadata.get("regime_profiles") or {}).items():
        if strategy not in effective:
            continue
        for regime, values in by_regime.items():
            merged = {**effective[strategy], **values}
            regimes.setdefault(strategy, {})[regime] = validate_parameter_profile({strategy: merged})[strategy]

    ALGO_DEFAULT_PARAMETERS.clear()
    ALGO_DEFAULT_PARAMETERS.update(effective)
    REGIME_PARAMETER_PROFILES.clear()
    REGIME_PARAMETER_PROFILES.update(regimes)
    ACTIVE_PROFILE_METADATA.clear()
    ACTIVE_PROFILE_METADATA.update(metadata)
    return metadata


def parameters_for_regime(strategy: str, regime: str | None = None) -> dict:
    """Return the effective strategy parameters for the detected regime."""
    strategy = canonical_strategy_id(strategy)
    base = deepcopy(ALGO_DEFAULT_PARAMETERS[strategy])
    selected = (REGIME_PARAMETER_PROFILES.get(strategy) or {}).get(str(regime or ""))
    return {**base, **(selected or {})}


def profile_id_for_regime(strategy: str, regime: str | None = None) -> str:
    strategy = canonical_strategy_id(strategy)
    metadata = ACTIVE_PROFILE_METADATA
    ids = metadata.get("profile_ids") or {}
    regime_key = f"{strategy}:{regime}" if regime else ""
    return str(ids.get(regime_key) or ids.get(strategy) or "base")


refresh_active_parameter_profile()


def evaluate(
    name: str, mode: str, ticker: str, bars: list[dict], position_qty: float = 0,
    parameters: dict | None = None,
    *, runtime_enabled_override: bool | None = None,
):
    """Call a strategy module with defaults matched to its bar interval."""
    if mode == "algo":
        name = canonical_strategy_id(name)
    if mode == "algo" and name == "swing_momentum":
        # Momentum is cross-sectional and must be ranked once per universe.
        return None
    if mode == "algo" and name == "intraday_mean_reversion":
        values = {**ALGO_DEFAULT_PARAMETERS[name], **(parameters or {})}
        signal_keys = {
            "window", "band_width", "entry_rsi", "exit_rsi", "short_entry_rsi",
            "short_exit_rsi", "allow_long", "allow_short", "regime_filter",
            "efficiency_ratio_period", "efficiency_ratio_max", "adx_period", "adx_max",
            "threshold_mode", "volatility_lookback", "high_volatility_ratio",
        }
        signal = mean_reversion.intraday.signal(
            ticker, bars, position_qty=position_qty,
            **{key: value for key, value in values.items() if key in signal_keys},
        )
        return replace(signal, strategy=name) if signal is not None else None
    if mode == "algo" and name in {"swing_trend_following", "swing_mean_reversion"}:
        module = ALGO_RULE_MODULES[name]
        values = {**ALGO_DEFAULT_PARAMETERS[name], **(parameters or {})}
        return module.signal(ticker, bars, position_qty=position_qty, parameters=values)
    if mode == "algo" and name in {"intraday_trend_following", "intraday_momentum"}:
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
        if name == "intraday_mean_reversion":
            kwargs = dict(ALGO_DEFAULT_PARAMETERS[name])
    kwargs.update(parameters or {})
    if kwargs:
        return module.signal(ticker, bars, position_qty=position_qty, **kwargs)
    return module.signal(ticker, bars, position_qty=position_qty)


__all__ = [
    "ALGO_DEFAULT_PARAMETERS", "PARAMETER_KEYS", "RULE_MODULES", "evaluate",
    "validate_parameter_profile",
]
