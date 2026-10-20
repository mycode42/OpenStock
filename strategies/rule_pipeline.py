"""Shared pure signal pipeline for live Algo scans and research replay."""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

from strategies.catalog import STRATEGY_CATALOG
from strategies.indicators import adx, closes, efficiency_ratio, moving_average
from strategies.registry import ALGO_DEFAULT_PARAMETERS, evaluate as evaluate_strategy
from strategies.signals import Signal
from portfolio.config import get_algo_runtime_settings
from strategies.swing_common import SwingSignal
from strategies.mean_reversion.swing import SwingMeanReversionConfig
from strategies.momentum.swing import SwingMomentumConfig
from strategies.trend_following.swing import SwingTrendConfig


ET = ZoneInfo("America/New_York")
RULE_PARAMETERS = {
    "trend_following": set(SwingTrendConfig.__dataclass_fields__),
    "momentum": set(SwingMomentumConfig.__dataclass_fields__),
    "swing_trend_following": set(SwingTrendConfig.__dataclass_fields__),
    "swing_momentum": set(SwingMomentumConfig.__dataclass_fields__),
    "intraday_trend": {"enabled", "fast_period", "slow_period", "confirmation_bars", "minimum_slope_pct", "allow_long", "allow_short"},
    "intraday_momentum": {"enabled", "lookback", "minimum_return", "minimum_volume_ratio", "minimum_short_return",
                           "minimum_short_volume_ratio", "max_extension_pct", "allow_long", "allow_short"},
    "mean_reversion": {
        "window", "band_width", "entry_rsi", "exit_rsi", "short_entry_rsi",
        "short_exit_rsi", "allow_long", "allow_short", "regime_filter",
        "efficiency_ratio_period", "efficiency_ratio_max", "adx_period", "adx_max",
        "threshold_mode", "volatility_lookback", "high_volatility_ratio",
    },
    "swing_mean_reversion": set(SwingMeanReversionConfig.__dataclass_fields__),
}


def _parameters(name: str, values: dict) -> dict:
    return {key: value for key, value in values.items() if key in RULE_PARAMETERS[name]}


@lru_cache(maxsize=128)
def _cached_trend_config(items: tuple[tuple[str, type, object], ...]) -> SwingTrendConfig:
    return SwingTrendConfig.from_dict({key: value for key, _, value in items})


def _trend_config(parameters: dict) -> SwingTrendConfig:
    items = tuple(sorted(
        ((key, type(value), value) for key, value in parameters.items()),
        key=lambda item: item[0],
    ))
    try:
        hash(items)
    except TypeError:
        # Preserve the config validator's normal error path for malformed,
        # unhashable parameter values.
        return SwingTrendConfig.from_dict(parameters)
    return _cached_trend_config(items)


def _intraday_satellite(signal: Signal | None, name: str) -> SwingSignal | None:
    if signal is None:
        return None
    runtime_settings = get_algo_runtime_settings()
    return SwingSignal(
        signal.ticker, signal.action, name, signal.reason, signal.strength,
        stop_pct=0.01, strategy_group="intraday_satellite",
        target_weight=runtime_settings.intraday_satellite_position_pct,
        risk_multiplier=runtime_settings.intraday_satellite_risk_multiplier,
    )


def _opening_dip(
    ticker: str, bars: list[dict], parameters: dict, *, position_qty: float = 0,
) -> Signal | None:
    if len(bars) < 3:
        return None
    try:
        opened = float(bars[0]["o"])
        close = float(bars[-1]["c"])
        previous_close = float(bars[-2]["c"])
        lows = [float(bar["l"]) for bar in bars]
        current_time = datetime.fromisoformat(str(bars[-1]["t"]).replace("Z", "+00:00"))
        first_time = datetime.fromisoformat(str(bars[0]["t"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=ET)
    if first_time.tzinfo is None:
        first_time = first_time.replace(tzinfo=ET)
    elapsed_minutes = (current_time - first_time).total_seconds() / 60
    if elapsed_minutes < 0 or elapsed_minutes > parameters.get("entry_window_minutes", 90):
        return None
    if opened <= 0 or close <= 0:
        return None

    if position_qty > 0 and close >= opened:
        return Signal(ticker, "sell", "mean_reversion", "opening dip recovered to the session open", 0.7)
    if position_qty < 0 and close <= opened:
        return Signal(ticker, "cover", "mean_reversion", "opening rip faded to the session open", 0.7)
    if position_qty != 0:
        return None

    action = None
    if parameters.get("allow_long", True):
        dip_pct = 1 - close / opened
        higher_low_bars = int(parameters.get("higher_low_bars", 1))
        recent_lows = lows[-(higher_low_bars + 1):] if higher_low_bars else []
        higher_lows = not higher_low_bars or (
            len(recent_lows) == higher_low_bars + 1
            and all(right > left for left, right in zip(recent_lows, recent_lows[1:]))
        )
        local_low = min(lows[-3:])
        bounce_pct = close / local_low - 1 if local_low > 0 else 0
        if (
            dip_pct >= parameters.get("minimum_dip_pct", 0.01)
            and close > previous_close and higher_lows
            and bounce_pct >= parameters.get("minimum_bounce_pct", 0.001)
        ):
            action = ("buy", dip_pct, bounce_pct, "higher low")

    if action is None and parameters.get("allow_short", False):
        highs = [float(bar["h"]) for bar in bars]
        rip_pct = close / opened - 1
        lower_high_bars = int(parameters.get("lower_high_bars", parameters.get("higher_low_bars", 1)))
        recent_highs = highs[-(lower_high_bars + 1):] if lower_high_bars else []
        lower_highs = not lower_high_bars or (
            len(recent_highs) == lower_high_bars + 1
            and all(right < left for left, right in zip(recent_highs, recent_highs[1:]))
        )
        local_high = max(highs[-3:])
        fade_pct = 1 - close / local_high if local_high > 0 else 0
        minimum_rip = parameters.get("minimum_rip_pct", parameters.get("minimum_dip_pct", 0.01))
        if (
            rip_pct >= minimum_rip and close < previous_close and lower_highs
            and fade_pct >= parameters.get("minimum_fade_pct", parameters.get("minimum_bounce_pct", 0.001))
        ):
            action = ("short", rip_pct, fade_pct, "lower high")
    if action is None:
        return None

    prior_volumes = [float(bar.get("v", 0) or 0) for bar in bars[-21:-1]]
    current_volume = float(bars[-1].get("v", 0) or 0)
    average_volume = sum(prior_volumes) / len(prior_volumes) if prior_volumes else 0
    volume_ratio = current_volume / average_volume if average_volume > 0 else 0
    if volume_ratio < parameters.get("minimum_volume_ratio", 1.2):
        return None
    direction, move_pct, reaction_pct, confirmation = action
    return Signal(
        ticker, direction, "mean_reversion",
        f"opening {'dip' if direction == 'buy' else 'rip'} {move_pct:.1%}; "
        f"{confirmation} confirmation {reaction_pct:.1%}; volume {volume_ratio:.2f}x",
        min(0.95, 0.6 + move_pct),
    )


def _confirmed_trend(
    ticker: str, bars: list[dict], parameters: dict, confirmation_bars: int,
) -> Signal | None:
    values = closes(bars)
    fast = int(parameters.get("fast_period", 9))
    slow = int(parameters.get("slow_period", 21))
    prior_size = len(values) - confirmation_bars
    if prior_size < slow:
        return None
    prior_fast = moving_average(values[:prior_size], fast)
    prior_slow = moving_average(values[:prior_size], slow)
    if prior_fast is None or prior_slow is None:
        return None
    minimum_slope = float(parameters.get("minimum_slope_pct", 0))
    for direction, was_bullish, action, enabled in (
        ("long", prior_fast <= prior_slow, "buy", parameters.get("allow_long", True)),
        ("short", prior_fast >= prior_slow, "short", parameters.get("allow_short", False)),
    ):
        if not enabled or not was_bullish:
            continue
        fast_values = []
        confirmed = True
        for size in range(prior_size + 1, len(values) + 1):
            fast_now = moving_average(values[:size], fast)
            slow_now = moving_average(values[:size], slow)
            if fast_now is None or slow_now is None:
                confirmed = False
                break
            aligned = fast_now > slow_now if direction == "long" else fast_now < slow_now
            if not aligned:
                confirmed = False
                break
            fast_values.append(fast_now)
        if not confirmed or not fast_values:
            continue
        slope_pct = fast_values[-1] / prior_fast - 1 if prior_fast > 0 else 0
        if (direction == "long" and slope_pct < minimum_slope) or (direction == "short" and slope_pct > -minimum_slope):
            continue
        return Signal(
            ticker, action, "trend_following",
            f"{fast}/{slow} {direction} crossover held for {confirmation_bars} bars; slope {slope_pct:.2%}",
            0.8,
        )
    return None


def evaluate_rule_signal(
    name: str, ticker: str, bars: list[dict], parameters: dict | None = None,
    *, position_qty: float = 0, entry_at: str | None = None,
    entry_price: float | None = None, position_target: float | None = None,
    runtime_enabled_override: bool | None = None,
) -> Signal | None:
    """Apply configured rule filters, then delegate to the production signal module."""
    if name not in RULE_PARAMETERS:
        return None
    runtime_enabled = None
    if name in {"intraday_trend", "intraday_momentum"}:
        runtime_enabled = runtime_enabled_override
        runtime_setting = STRATEGY_CATALOG[name].runtime_setting
        if runtime_enabled is None and runtime_setting:
            runtime_enabled = getattr(get_algo_runtime_settings(), runtime_setting)
        if not runtime_enabled:
            return None
    params = {**ALGO_DEFAULT_PARAMETERS.get(name, {}), **(parameters or {})}
    # Stop distances are live risk settings, not research-tunable signal
    # parameters. Apply the horizon-specific Algo values after profile merge.
    from portfolio.config import ALGO_MODE_CONFIG
    algo_risk = ALGO_MODE_CONFIG["risk"]
    if name in algo_risk.get("stop_loss_pct_by_strategy", {}):
        params["hard_stop_pct"] = algo_risk["stop_loss_pct_by_strategy"][name]
    if name in algo_risk.get("trailing_stop_pct_by_strategy", {}):
        params["trailing_stop_pct"] = algo_risk["trailing_stop_pct_by_strategy"][name]
    if name in algo_risk.get("trailing_activation_pct_by_strategy", {}):
        params["trailing_activation_pct"] = algo_risk["trailing_activation_pct_by_strategy"][name]
    rule_params = _parameters(name, params)

    if name == "swing_mean_reversion":
        config = SwingMeanReversionConfig.from_dict(rule_params)
        from strategies.mean_reversion.swing import signal as swing_signal
        return swing_signal(
            ticker, bars, position_qty=position_qty, parameters=config.to_dict(),
            entry_at=entry_at, entry_price=entry_price, position_target=position_target,
        )

    if name in {"trend_following", "swing_trend_following"}:
        from strategies.trend_following.swing import signal as swing_trend_signal
        config = _trend_config(rule_params)
        return swing_trend_signal(
            ticker, bars, position_qty=position_qty, parameters=config,
            entry_at=entry_at, entry_price=entry_price,
        )

    if name in {"momentum", "swing_momentum"}:
        # The swing momentum implementation ranks the full universe in one pass.
        return None

    if name == "mean_reversion" and params.get("signal_model", "bollinger") == "opening_dip":
        if not position_qty:
            regime_filter = params.get("regime_filter", "none")
            values = closes(bars)
            if regime_filter in {"er", "combined"}:
                er = efficiency_ratio(values, int(params.get("efficiency_ratio_period", 20)))
                if er is None or er > float(params.get("efficiency_ratio_max", 0.35)):
                    return None
            if regime_filter in {"adx", "combined"}:
                trend_strength = adx(bars, int(params.get("adx_period", 14)))
                if trend_strength is None or trend_strength > float(params.get("adx_max", 20.0)):
                    return None
        return _opening_dip(ticker, bars, params, position_qty=position_qty)

    if name == "intraday_trend":
        confirmation = int(params.get("confirmation_bars", 1))
        if confirmation > 1 or params.get("minimum_slope_pct", 0) > 0:
            # The five-minute rule remains explicitly opt-in at both config
            # and runner level; a disabled call cannot emit a signal.
            if not params.get("enabled", False):
                return None
            legacy_params = {key: value for key, value in params.items() if key != "enabled"}
            signal = _confirmed_trend(ticker, bars, legacy_params, confirmation)
            return _intraday_satellite(signal, name)

    evaluation_kwargs = {"parameters": rule_params}
    if runtime_enabled is not None:
        evaluation_kwargs["runtime_enabled_override"] = runtime_enabled
    signal = evaluate_strategy(name, "algo", ticker, bars, position_qty, **evaluation_kwargs)
    if signal is None or name not in {"intraday_trend", "intraday_momentum"}:
        return signal
    if not params.get("enabled", False):
        return None
    max_extension = float(params.get("max_extension_pct", 0)) if name == "intraday_momentum" else 0
    if max_extension > 0:
        values = closes(bars)
        average = moving_average(values, 20)
        extension = values[-1] / average - 1 if average else None
        if extension is None or abs(extension) > max_extension:
            return None
    return _intraday_satellite(signal, name)
