"""Research-only signal variants; live strategy defaults remain unchanged."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from src.strategies.indicators import closes, moving_average
from src.strategies.registry import evaluate as evaluate_strategy
from src.strategies.signals import Signal


ET = ZoneInfo("America/New_York")
RULE_PARAMETERS = {
    "trend_following": {"fast_period", "slow_period"},
    "momentum": {"lookback", "minimum_return", "minimum_volume_ratio"},
    "mean_reversion": {"window", "band_width", "entry_rsi", "exit_rsi"},
    "opening_range_breakout": {"opening_range_bars", "minimum_volume_ratio"},
}


def _parameters(name: str, values: dict) -> dict:
    return {key: value for key, value in values.items() if key in RULE_PARAMETERS[name]}


def _opening_dip(ticker: str, bars: list[dict], parameters: dict) -> Signal | None:
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

    dip_pct = 1 - close / opened
    if dip_pct < parameters.get("minimum_dip_pct", 0.01):
        return None
    if close <= previous_close:
        return None

    higher_low_bars = int(parameters.get("higher_low_bars", 1))
    if higher_low_bars:
        recent_lows = lows[-(higher_low_bars + 1):]
        if len(recent_lows) != higher_low_bars + 1 or any(
            right <= left for left, right in zip(recent_lows, recent_lows[1:])
        ):
            return None
    local_low = min(lows[-3:])
    bounce_pct = close / local_low - 1 if local_low > 0 else 0
    if bounce_pct < parameters.get("minimum_bounce_pct", 0.001):
        return None

    prior_volumes = [float(bar.get("v", 0) or 0) for bar in bars[-21:-1]]
    current_volume = float(bars[-1].get("v", 0) or 0)
    average_volume = sum(prior_volumes) / len(prior_volumes) if prior_volumes else 0
    volume_ratio = current_volume / average_volume if average_volume > 0 else 0
    if volume_ratio < parameters.get("minimum_volume_ratio", 1.2):
        return None
    return Signal(
        ticker, "buy", "mean_reversion",
        f"opening dip {dip_pct:.1%}; bounce {bounce_pct:.1%}; volume {volume_ratio:.2f}x",
        min(0.95, 0.6 + dip_pct),
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
    if prior_fast is None or prior_slow is None or prior_fast > prior_slow:
        return None

    confirmed = []
    for size in range(prior_size + 1, len(values) + 1):
        fast_now = moving_average(values[:size], fast)
        slow_now = moving_average(values[:size], slow)
        if fast_now is None or slow_now is None or fast_now <= slow_now:
            return None
        confirmed.append(fast_now)
    slope_pct = confirmed[-1] / prior_fast - 1 if prior_fast > 0 else 0
    if slope_pct < parameters.get("minimum_slope_pct", 0):
        return None
    return Signal(
        ticker, "buy", "trend_following",
        f"{fast}/{slow} crossover held for {confirmation_bars} bars; slope {slope_pct:.2%}",
        0.8,
    )


def evaluate_research_signal(
    name: str, ticker: str, bars: list[dict], parameters: dict | None = None,
) -> Signal | None:
    """Apply optional, bounded research filters to the production rule signals."""
    params = dict(parameters or {})
    rule_params = _parameters(name, params)

    if name == "mean_reversion" and params.get("signal_model", "bollinger") == "opening_dip":
        return _opening_dip(ticker, bars, params)

    if name == "trend_following":
        confirmation = int(params.get("confirmation_bars", 1))
        if confirmation > 1 or params.get("minimum_slope_pct", 0) > 0:
            return _confirmed_trend(ticker, bars, params, confirmation)

    if name == "opening_range_breakout":
        range_bars = int(params.get("opening_range_bars", 6))
        delay_bars = int(params.get("post_range_delay_bars", 0))
        if len(bars) <= range_bars + delay_bars:
            return None
        buffer_pct = float(params.get("breakout_buffer_pct", 0))
        if buffer_pct > 0:
            try:
                opening_high = max(float(bar["h"]) for bar in bars[:range_bars])
                latest_close = float(bars[-1]["c"])
            except (KeyError, TypeError, ValueError):
                return None
            if opening_high <= 0 or latest_close <= opening_high * (1 + buffer_pct):
                return None

    signal = evaluate_strategy(name, "algo", ticker, bars, 0, parameters=rule_params)
    if signal is None or signal.action != "buy" or name != "momentum":
        return signal
    max_extension = float(params.get("max_extension_pct", 0))
    if max_extension > 0:
        values = closes(bars)
        average = moving_average(values, 20)
        if average is None or values[-1] / average - 1 > max_extension:
            return None
    return signal
