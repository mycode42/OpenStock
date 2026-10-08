"""Strategy-family registry and default rule profiles."""

from __future__ import annotations

from src.strategies import mean_reversion, momentum, opening_range_breakout, trend_following


RULE_MODULES = {
    "mean_reversion": mean_reversion,
    "trend_following": trend_following,
    "momentum": momentum,
    "opening_range_breakout": opening_range_breakout,
}

ALGO_DEFAULT_PARAMETERS = {
    "trend_following": {"fast_period": 9, "slow_period": 21},
    "momentum": {"lookback": 12, "minimum_return": 0.015, "minimum_volume_ratio": 1.2},
    "mean_reversion": {"window": 20, "band_width": 2.0, "entry_rsi": 30, "exit_rsi": 58},
    "opening_range_breakout": {"opening_range_bars": 6, "minimum_volume_ratio": 1.25},
}

PARAMETER_KEYS = {
    "trend_following": {
        "fast_period", "slow_period", "confirmation_bars", "minimum_slope_pct",
    },
    "momentum": {
        "lookback", "minimum_return", "minimum_volume_ratio", "max_extension_pct",
    },
    "mean_reversion": {
        "window", "band_width", "entry_rsi", "exit_rsi", "signal_model",
        "minimum_dip_pct", "minimum_volume_ratio", "entry_window_minutes",
        "minimum_bounce_pct", "higher_low_bars",
    },
    "opening_range_breakout": {
        "opening_range_bars", "minimum_volume_ratio", "post_range_delay_bars",
        "breakout_buffer_pct",
    },
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

    fast = number("trend_following", "fast_period", 9, 1, 200, integer=True)
    slow = number("trend_following", "slow_period", 21, 1, 200, integer=True)
    if fast >= slow:
        raise ValueError("trend_following periods must satisfy 1 <= fast_period < slow_period <= 200")
    number("trend_following", "confirmation_bars", 1, 1, 10, integer=True)
    number("trend_following", "minimum_slope_pct", 0, 0, 0.1)
    number("momentum", "lookback", 12, 1, 200, integer=True)
    number("momentum", "minimum_return", 0.015, 0, 1)
    number("momentum", "minimum_volume_ratio", 1.2, 0.1, 20)
    number("momentum", "max_extension_pct", 0, 0, 1)
    number("mean_reversion", "window", 20, 5, 200, integer=True)
    number("mean_reversion", "band_width", 2.0, 0.1, 10)
    entry_rsi = number("mean_reversion", "entry_rsi", 30, 0, 100)
    exit_rsi = number("mean_reversion", "exit_rsi", 58, 0, 100)
    if entry_rsi > exit_rsi:
        raise ValueError("mean_reversion RSI values must satisfy 0 <= entry_rsi <= exit_rsi <= 100")
    model = normalized.get("mean_reversion", {}).get("signal_model", "bollinger")
    if not isinstance(model, str) or model not in {"bollinger", "opening_dip"}:
        raise ValueError("mean_reversion signal_model must be bollinger or opening_dip")
    number("mean_reversion", "minimum_dip_pct", 0, 0, 0.5)
    number("mean_reversion", "minimum_volume_ratio", 1.2, 0, 20)
    number("mean_reversion", "entry_window_minutes", 90, 5, 390, integer=True)
    number("mean_reversion", "minimum_bounce_pct", 0.001, 0, 0.1)
    number("mean_reversion", "higher_low_bars", 1, 0, 10, integer=True)
    number("opening_range_breakout", "opening_range_bars", 6, 1, 12, integer=True)
    number("opening_range_breakout", "minimum_volume_ratio", 1.25, 0.1, 20)
    number("opening_range_breakout", "post_range_delay_bars", 0, 0, 12, integer=True)
    number("opening_range_breakout", "breakout_buffer_pct", 0, 0, 0.05)
    return normalized


def evaluate(
    name: str, mode: str, ticker: str, bars: list[dict], position_qty: float = 0,
    parameters: dict | None = None,
):
    """Call a strategy module with defaults matched to its bar interval."""
    module = RULE_MODULES.get(name)
    if module is None:
        return None
    kwargs = {}
    if name == "opening_range_breakout":
        if mode not in {"day", "algo"}:
            return None
        if mode == "algo":
            kwargs = dict(ALGO_DEFAULT_PARAMETERS[name])
        kwargs.update(parameters or {})
        return module.signal(ticker, bars, position_qty=position_qty, **kwargs)
    if mode in {"day", "algo"}:
        if name == "trend_following":
            kwargs = dict(ALGO_DEFAULT_PARAMETERS[name])
        elif name in {"momentum", "mean_reversion"}:
            kwargs = dict(ALGO_DEFAULT_PARAMETERS[name])
    kwargs.update(parameters or {})
    if kwargs:
        return module.signal(ticker, bars, position_qty=position_qty, **kwargs)
    return module.signal(ticker, bars, position_qty=position_qty)


__all__ = [
    "ALGO_DEFAULT_PARAMETERS", "PARAMETER_KEYS", "RULE_MODULES", "evaluate",
    "validate_parameter_profile",
]
