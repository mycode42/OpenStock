"""Shared pure signal pipeline for live Algo scans and research replay."""

from __future__ import annotations

import time
from copy import deepcopy
from datetime import datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from strategies.catalog import STRATEGY_CATALOG, canonical_strategy_id
from strategies.indicators import adx, closes, efficiency_ratio, moving_average
from strategies.registry import ALGO_DEFAULT_PARAMETERS, evaluate as evaluate_strategy
from strategies.signals import Signal
from portfolio.config import (
    ALGO_STRATEGY_DEFAULTS_PATH, get_algo_runtime_settings,
    get_algo_strategy_settings, get_mode_config,
)
from strategies.swing_common import SwingSignal
from strategies.mean_reversion.swing import SwingMeanReversionConfig
from strategies.momentum.swing import SwingMomentumConfig
from strategies.trend_following.swing import SwingTrendConfig


ET = ZoneInfo("America/New_York")
INTRADAY_MAX_HOLDING_SESSIONS = 3


def _et_date(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ET)
    return parsed.astimezone(ET).date()


def _sessions_after_entry(entry_at, now_at) -> int | None:
    """Weekdays after the entry session up to and including the current session. Holidays count as sessions."""
    from datetime import timedelta

    try:
        entry_day = _et_date(entry_at)
        current_day = _et_date(now_at)
    except (TypeError, ValueError):
        return None
    count = 0
    day = entry_day
    while day < current_day:
        day += timedelta(days=1)
        if day.weekday() < 5:
            count += 1
    return count
RULE_PARAMETERS = {
    "swing_trend_following": set(SwingTrendConfig.__dataclass_fields__),
    "swing_momentum": set(SwingMomentumConfig.__dataclass_fields__),
    "intraday_trend_following": {"enabled", "fast_period", "slow_period", "confirmation_bars", "minimum_slope_pct", "allow_long", "allow_short"},
    "intraday_momentum": {"enabled", "lookback", "minimum_return", "minimum_volume_ratio", "minimum_short_return",
                           "minimum_short_volume_ratio", "max_extension_pct", "allow_long", "allow_short"},
    "intraday_mean_reversion": {
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


def _intraday_strategy_signal(signal: Signal | None, name: str) -> SwingSignal | None:
    if signal is None:
        return None
    settings = get_algo_strategy_settings(name)
    return SwingSignal(
        signal.ticker, signal.action, name, signal.reason, signal.strength,
        stop_pct=0.01, strategy_group="intraday_core",
        target_weight=settings["max_position_pct"],
        risk_multiplier=settings["risk_multiplier"],
    )


WEEKLY_RULE_NAMES = frozenset({"weekly_trend_following", "weekly_momentum", "weekly_mean_reversion"})
INTRADAY_TREND_MOMENTUM_AND_WEEKLY = frozenset({"intraday_trend_following", "intraday_momentum", "weekly_momentum", "weekly_trend_following"})
for _weekly in WEEKLY_RULE_NAMES:
    RULE_PARAMETERS[_weekly] = set(ALGO_DEFAULT_PARAMETERS[_weekly]) | {"maximum_holding_sessions"}


def _weekly_strategy_signal(signal: Signal | None, name: str, params: dict) -> SwingSignal | None:
    """Relabel a weekly signal: its own group, and the configured hard stop."""
    if signal is None:
        return None
    settings = get_algo_strategy_settings(name)
    return SwingSignal(
        signal.ticker, signal.action, name, signal.reason, signal.strength,
        stop_pct=float(params.get("hard_stop_pct", 0.04)), strategy_group="weekly_core",
        target_weight=settings["max_position_pct"],
        risk_multiplier=settings["risk_multiplier"],
    )


# The reversion reference is the average close of the last 21 five-minute bars.
REVERSION_REFERENCE_BARS = 21
REVERSION_SLOPE_LOOKBACK_BARS = 5


def _bar_time(bar: dict) -> datetime:
    parsed = datetime.fromisoformat(str(bar["t"]).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=ET) if parsed.tzinfo is None else parsed


def _average_reversion(
    ticker: str, bars: list[dict], parameters: dict, *, position_qty: float = 0,
    strategy: str = "intraday_mean_reversion",
) -> Signal | None:
    """Dips below and spikes above the reference average close, with a slope filter.

    The intraday rule uses the 21-bar average of five-minute closes. The weekly
    rule passes its own `reference_bars` and `slope_lookback_bars` (hourly bars).
    """
    if len(bars) < 3:
        return None
    reference_bars = int(parameters.get("reference_bars", REVERSION_REFERENCE_BARS))
    slope_bars = int(parameters.get("slope_lookback_bars", REVERSION_SLOPE_LOOKBACK_BARS))
    needed = reference_bars + slope_bars + 1
    if len(bars) < needed:
        return None
    try:
        closes_all = [float(bar["c"]) for bar in bars]
        close = closes_all[-1]
        previous_close = closes_all[-2]
        lows = [float(bar["l"]) for bar in bars]
        highs = [float(bar["h"]) for bar in bars]
    except (KeyError, TypeError, ValueError):
        return None
    # Reference: mean of the closes before the current bar. Slope: the same mean `slope_bars` bars earlier.
    reference = sum(closes_all[-1 - reference_bars:-1]) / reference_bars
    earlier = sum(closes_all[-1 - slope_bars - reference_bars:-1 - slope_bars]) / reference_bars
    if reference <= 0 or close <= 0 or earlier <= 0:
        return None
    # Slope filter, as the swing mean reversion has it: a steep move of the reference is a trend, not a stretch to fade.
    reference_slope = reference / earlier - 1
    if abs(reference_slope) > float(parameters.get("max_reference_slope_pct", 0.005)):
        return None

    if position_qty > 0 and close >= reference:
        return Signal(ticker, "sell", strategy, f"price back at the {reference_bars}-bar average", 0.7)
    if position_qty < 0 and close <= reference:
        return Signal(ticker, "cover", strategy, f"price back at the {reference_bars}-bar average", 0.7)
    if position_qty != 0:
        return None

    action = None
    if parameters.get("allow_long", True):
        dip_pct = 1 - close / reference
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
        rip_pct = close / reference - 1
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

    prior_volumes = [float(bar.get("v", 0) or 0) for bar in bars[-(reference_bars + 1):-1]]
    current_volume = float(bars[-1].get("v", 0) or 0)
    average_volume = sum(prior_volumes) / len(prior_volumes) if prior_volumes else 0
    volume_ratio = current_volume / average_volume if average_volume > 0 else 0
    if volume_ratio < parameters.get("minimum_volume_ratio", 1.2):
        return None
    direction, move_pct, reaction_pct, confirmation = action
    return Signal(
        ticker, direction, strategy,
        f"{'dip' if direction == 'buy' else 'spike'} {move_pct:.1%} from the {reference_bars}-bar average; "
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
            ticker, action, "swing_trend_following",
            f"{fast}/{slow} {direction} crossover held for {confirmation_bars} bars; slope {slope_pct:.2%}",
            0.8,
        )
    return None


_STOP_SETTINGS_CHECK_SECONDS = 1.0
_stop_settings_checked: list = [0.0, None]  # [monotonic time of last file check, file key]


def _algo_stop_settings() -> dict:
    """Algo stop-distance settings, re-read when the strategy config file changes.

    Signal evaluation runs once per bar and candidate. Files are checked at most
    once per second, so a config edit takes effect within about a second.
    """
    now = time.monotonic()
    if now - _stop_settings_checked[0] >= _STOP_SETTINGS_CHECK_SECONDS or _stop_settings_checked[1] is None:
        key = tuple(
            (path.stat().st_mtime_ns if path.exists() else None)
            for path in (ALGO_STRATEGY_DEFAULTS_PATH,)
        )
        _stop_settings_checked[0] = now
        _stop_settings_checked[1] = key
    return _cached_stop_settings(_stop_settings_checked[1])


@lru_cache(maxsize=8)
def _cached_stop_settings(_key: tuple) -> dict:
    return deepcopy(get_mode_config("algo")["risk"])


_RULE_PARAMETER_MEMO: dict = {}


def _merged_rule_parameters(name: str, parameters: dict | None, algo_risk: dict) -> tuple[dict, dict]:
    params = {**ALGO_DEFAULT_PARAMETERS.get(name, {}), **(parameters or {})}
    # Stop distances are live risk settings, not research-tunable signal
    # parameters. Apply the horizon-specific Algo values after profile merge.
    if name in algo_risk.get("stop_loss_pct_by_strategy", {}):
        params["hard_stop_pct"] = algo_risk["stop_loss_pct_by_strategy"][name]
    if name in algo_risk.get("trailing_stop_pct_by_strategy", {}):
        params["trailing_stop_pct"] = algo_risk["trailing_stop_pct_by_strategy"][name]
    if name in algo_risk.get("trailing_activation_pct_by_strategy", {}):
        params["trailing_activation_pct"] = algo_risk["trailing_activation_pct_by_strategy"][name]
    return params, _parameters(name, params)


def evaluate_rule_signal(
    name: str, ticker: str, bars: list[dict], parameters: dict | None = None,
    *, position_qty: float = 0, entry_at: str | None = None,
    entry_price: float | None = None, position_target: float | None = None,
    runtime_enabled_override: bool | None = None,
) -> Signal | None:
    """Apply configured rule filters, then delegate to the production signal module."""
    name = canonical_strategy_id(name)
    if name not in RULE_PARAMETERS:
        return None
    # Intraday holdings are closed after a fixed number of sessions, like the swing exits. There is no end-of-day flatten.
    # Weekly holdings use their own maximum_holding_sessions.
    holding_limit = None
    if name.startswith("intraday_"):
        holding_limit = INTRADAY_MAX_HOLDING_SESSIONS
    elif name in WEEKLY_RULE_NAMES:
        holding_limit = int((parameters or ALGO_DEFAULT_PARAMETERS.get(name, {})).get("maximum_holding_sessions", 10))
    if holding_limit is not None and position_qty and entry_at and bars:
        held = _sessions_after_entry(entry_at, bars[-1].get("t"))
        if held is not None and held >= holding_limit:
            action = "sell" if position_qty > 0 else "cover"
            return Signal(
                ticker, action, name,
                f"held {holding_limit} sessions after entry; maximum holding reached",
                0.9,
            )
    runtime_enabled = None
    if name in {"intraday_trend_following", "intraday_momentum"}:
        runtime_enabled = runtime_enabled_override
        runtime_setting = STRATEGY_CATALOG[name].runtime_setting
        if runtime_enabled is None and runtime_setting:
            runtime_enabled = getattr(get_algo_runtime_settings(), runtime_setting)
        if position_qty:
            # Holdings keep their own exit rules when entries are switched off.
            runtime_enabled = True
        elif not runtime_enabled:
            return None
    algo_risk = _algo_stop_settings()
    # Replays pass the same parameter object for every bar, so the merged rule
    # parameters are built once per candidate instead of once per bar.
    memo_key = (name, id(parameters), id(algo_risk))
    memo = _RULE_PARAMETER_MEMO.get(memo_key)
    if memo is not None and memo[0] is parameters and memo[1] is algo_risk:
        params, rule_params = memo[2], memo[3]
    else:
        params, rule_params = _merged_rule_parameters(name, parameters, algo_risk)
        if len(_RULE_PARAMETER_MEMO) > 256:
            _RULE_PARAMETER_MEMO.clear()
        _RULE_PARAMETER_MEMO[memo_key] = (parameters, algo_risk, params, rule_params)
    if name in algo_risk.get("stop_loss_pct_by_strategy", {}):
        params["hard_stop_pct"] = algo_risk["stop_loss_pct_by_strategy"][name]
    if name in algo_risk.get("trailing_stop_pct_by_strategy", {}):
        params["trailing_stop_pct"] = algo_risk["trailing_stop_pct_by_strategy"][name]
    if name in algo_risk.get("trailing_activation_pct_by_strategy", {}):
        params["trailing_activation_pct"] = algo_risk["trailing_activation_pct_by_strategy"][name]

    if name == "swing_mean_reversion":
        from strategies.mean_reversion.swing import signal as swing_signal
        # The signal validates and caches the settings itself, so they are not rebuilt per bar.
        return swing_signal(
            ticker, bars, position_qty=position_qty, parameters=rule_params,
            entry_at=entry_at, entry_price=entry_price, position_target=position_target,
        )

    if name == "swing_trend_following":
        from strategies.trend_following.swing import signal as swing_trend_signal
        config = _trend_config(rule_params)
        return swing_trend_signal(
            ticker, bars, position_qty=position_qty, parameters=config,
            entry_at=entry_at, entry_price=entry_price,
        )

    if name == "swing_momentum":
        # The swing momentum implementation ranks the full universe in one pass.
        return None

    if name in {"intraday_mean_reversion", "weekly_mean_reversion"} and params.get("signal_model", "bollinger") == "average_24h":
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
        return _average_reversion(ticker, bars, params, position_qty=position_qty, strategy=name)

    if name == "intraday_trend_following":
        confirmation = int(params.get("confirmation_bars", 1))
        if confirmation > 1 or params.get("minimum_slope_pct", 0) > 0:
            # The five-minute rule remains explicitly opt-in at both config
            # and runner level; a disabled call cannot emit a signal.
            if not position_qty and not params.get("enabled", False):
                return None
            legacy_params = {key: value for key, value in params.items() if key != "enabled"}
            signal = _confirmed_trend(ticker, bars, legacy_params, confirmation)
            return _intraday_strategy_signal(signal, name)

    evaluation_kwargs = {"parameters": rule_params}
    if runtime_enabled is not None:
        evaluation_kwargs["runtime_enabled_override"] = runtime_enabled
    signal = evaluate_strategy(name, "algo", ticker, bars, position_qty, **evaluation_kwargs)
    if signal is None or name not in INTRADAY_TREND_MOMENTUM_AND_WEEKLY:
        return signal
    if name in WEEKLY_RULE_NAMES:
        enabled = runtime_enabled_override if runtime_enabled_override is not None else params.get("enabled", False)
    else:
        enabled = params.get("enabled", False)
    if not position_qty and not enabled:
        return None
    max_extension = float(params.get("max_extension_pct", 0)) if name in {"intraday_momentum", "weekly_momentum"} else 0
    if max_extension > 0:
        values = closes(bars)
        average = moving_average(values, 20)
        extension = values[-1] / average - 1 if average else None
        if extension is None or abs(extension) > max_extension:
            return None
    if name in WEEKLY_RULE_NAMES:
        return _weekly_strategy_signal(signal, name, params)
    return _intraday_strategy_signal(signal, name)
