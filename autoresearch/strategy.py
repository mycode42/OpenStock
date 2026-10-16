# EXPERIMENT: lower_day_target
# HYPOTHESIS: A moderately closer profit target may let more winning intraday trades realize gains before the day-end flatten, improving win rate and risk-adjusted returns while retaining a favorable reward-to-risk ratio.
# CHANGE: Lowered TARGET_MULTIPLIER from 3.8 to 3.2

"""
Pure-Python deterministic trading strategy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from typing import Literal
from zoneinfo import ZoneInfo


EXPERIMENT_NAME = "lower_day_target"
EXPERIMENT_HYPOTHESIS = (
    "A moderately closer profit target may let more winning intraday trades "
    "realize gains before the day-end flatten, improving win rate and "
    "risk-adjusted returns while retaining a favorable reward-to-risk ratio."
)
EXPERIMENT_CHANGE = "Lowered TARGET_MULTIPLIER from 3.8 to 3.2"

RSI_PERIOD = 16
RSI_OVERSOLD = 25
RSI_VERY_OVERSOLD = 28
RSI_STRONG_OVERSOLD = 31
RSI_MODERATE_OVERSOLD = 38
RSI_OVERBOUGHT = 63
RSI_NEUTRAL_LOW = 48
RSI_NEUTRAL_HIGH = 53

VWAP_NEAR_BAND_PCT = 0.90
VWAP_EXTENDED_PCT = 1.50
VOLUME_CONFIRM_RATIO = 1.15
VOLUME_STRONG_RATIO = 2.50

STOP_PCT = 0.0105
TARGET_MULTIPLIER = 3.2
MAX_POSITION_SIZE_PCT = 0.15
MIN_CONFIDENCE = 54.0

CONF_WEIGHT_RSI = 0.25
CONF_WEIGHT_VWAP = 0.40
CONF_WEIGHT_VOLUME = 0.20
CONF_WEIGHT_MACD = 0.15

MACD_FAST = 14
MACD_SLOW = 26
MACD_SIGNAL_PERIOD = 9

REGIME_MULTIPLIER: dict[str, float] = {
    "trending_up": 1.00,
    "trending_down": 1.00,
    "range_bound": 1.00,
    "volatile": 0.65,
    "unknown": 0.70,
}

NO_TRADE_OPEN_MINUTES = 5
MARKET_CLOSE_CUTOFF = time(15, 25)
MARKET_TZ = ZoneInfo("America/New_York")
MODE = "day"

SMA_FAST = 20
SMA_SLOW = 50
SWING_RSI_PERIOD = 14
SWING_RSI_OVERSOLD = 35
SWING_RSI_OVERBOUGHT = 70
SWING_STOP_PCT = 0.03
SWING_TARGET_MULTIPLIER = 2.5
SWING_MIN_CONFIDENCE = 50.0
SWING_TREND_STRENGTH_MIN = 0.5
SWING_VOLUME_CONFIRM = 1.2

SWING_CONF_WEIGHT_TREND = 0.35
SWING_CONF_WEIGHT_RSI = 0.25
SWING_CONF_WEIGHT_MOMENTUM = 0.25
SWING_CONF_WEIGHT_VOLUME = 0.15


@dataclass
class Signal:
    ticker: str
    direction: Literal["long", "short"]
    confidence: float
    entry_price: float
    stop_price: float
    target_price: float
    reasoning: str
    indicators: dict = field(default_factory=dict)


def _calc_rsi(closes: list[float], period: int = RSI_PERIOD) -> float | None:
    if len(closes) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    gain = sum(gains[-period:]) / period
    loss = sum(losses[-period:]) / period
    if loss < 1e-10:
        return 100.0
    return round(100.0 - 100.0 / (1.0 + gain / loss), 2)


def _calc_vwap(bars: list[dict]) -> float | None:
    pv = 0.0
    volume = 0.0
    for bar in bars:
        typical = (float(bar["h"]) + float(bar["l"]) + float(bar["c"])) / 3.0
        vol = float(bar.get("v", 0))
        pv += typical * vol
        volume += vol
    return pv / volume if volume >= 1 else None


def _calc_ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(value * alpha + result[-1] * (1.0 - alpha))
    return result


def _calc_macd(
    closes: list[float],
) -> tuple[float | None, float | None, float | None]:
    if len(closes) < MACD_SLOW + MACD_SIGNAL_PERIOD:
        return None, None, None
    fast = _calc_ema(closes, MACD_FAST)
    slow = _calc_ema(closes, MACD_SLOW)
    line = [fast[i] - slow[i] for i in range(min(len(fast), len(slow)))]
    if len(line) < MACD_SIGNAL_PERIOD:
        return None, None, None
    signal = _calc_ema(line, MACD_SIGNAL_PERIOD)
    value = line[-1]
    sig = signal[-1]
    return value, sig, value - sig


def _parse_bar_time_et(value: str) -> time | None:
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(MARKET_TZ).time()
    except Exception:
        return None


def _is_tradeable_time(value: str) -> bool:
    current = _parse_bar_time_et(value)
    if current is None:
        return True
    if current < time(9, 30 + NO_TRADE_OPEN_MINUTES):
        return False
    return current < MARKET_CLOSE_CUTOFF


def _ticker_signal(
    ticker: str,
    bars: list[dict],
    market_context: dict,
    regime_mult: float,
) -> Signal | None:
    if len(bars) < 5:
        return None

    closes = [float(bar["c"]) for bar in bars]
    highs = [float(bar["h"]) for bar in bars]
    lows = [float(bar["l"]) for bar in bars]
    volumes = [float(bar.get("v", 0)) for bar in bars]

    price = closes[-1]
    day_open = float(bars[0]["o"])
    day_high = max(highs)
    day_low = min(lows)

    rsi = _calc_rsi(closes)
    vwap = _calc_vwap(bars)
    macd_value, macd_signal, macd_hist = _calc_macd(closes)

    average_volume = market_context.get(f"{ticker}_avg_volume_20d")
    cumulative_volume = sum(volumes)
    volume_ratio = (
        cumulative_volume / average_volume
        if average_volume and average_volume > 0
        else None
    )

    vwap_deviation = None
    if vwap and vwap > 0:
        vwap_deviation = (price - vwap) / vwap * 100.0

    bull = 0.0
    bear = 0.0

    if rsi is not None:
        if rsi < RSI_OVERSOLD:
            bull += CONF_WEIGHT_RSI * 100.0
        elif rsi < RSI_VERY_OVERSOLD:
            bull += CONF_WEIGHT_RSI * 90.0
        elif rsi < RSI_STRONG_OVERSOLD:
            bull += CONF_WEIGHT_RSI * 80.0
        elif rsi < RSI_MODERATE_OVERSOLD:
            bull += CONF_WEIGHT_RSI * 70.0
        elif rsi > RSI_OVERBOUGHT:
            bear += CONF_WEIGHT_RSI * 100.0
        elif rsi < RSI_NEUTRAL_LOW:
            bull += CONF_WEIGHT_RSI * 60.0
        elif rsi > RSI_NEUTRAL_HIGH:
            bear += CONF_WEIGHT_RSI * 60.0

    if vwap_deviation is not None:
        deviation = abs(vwap_deviation)
        score = CONF_WEIGHT_VWAP * min(100.0, 60.0 + deviation * 10.0)
        if vwap_deviation > VWAP_NEAR_BAND_PCT:
            bull += score
        elif vwap_deviation < -VWAP_NEAR_BAND_PCT:
            bear += score

    if volume_ratio is not None:
        if volume_ratio >= VOLUME_STRONG_RATIO:
            volume_score = 100.0
        elif volume_ratio >= VOLUME_CONFIRM_RATIO:
            volume_score = 70.0
        elif volume_ratio >= 0.80:
            volume_score = 55.0
        else:
            volume_score = 10.0
        if bull >= bear:
            bull += CONF_WEIGHT_VOLUME * volume_score
        else:
            bear += CONF_WEIGHT_VOLUME * volume_score

    if macd_hist is not None and macd_value is not None:
        if macd_hist > 0 and macd_value > 0:
            bull += CONF_WEIGHT_MACD * 80.0
        elif macd_hist < 0 and macd_value < 0:
            bear += CONF_WEIGHT_MACD * 80.0

    regime = market_context.get("regime", "unknown")
    if regime == "trending_up" and bull > bear:
        bull *= 1.10
    elif regime == "trending_down" and bear > bull:
        bear *= 1.10
    elif regime == "range_bound":
        if rsi is not None and rsi < RSI_MODERATE_OVERSOLD:
            bull *= 1.15
        elif rsi is not None and rsi > RSI_OVERBOUGHT:
            bear *= 1.15

    spy_change = float(market_context.get("spy_change_pct") or 0.0)
    if spy_change > 0.4 and bull > bear:
        bull *= 1.05
    elif spy_change < -0.4 and bear > bull:
        bear *= 1.05

    qqq_change = float(market_context.get("qqq_change_pct") or 0.0)
    if qqq_change > 0.4 and bull > bear:
        bull *= 1.05
    elif qqq_change < -0.4 and bear > bull:
        bear *= 1.05

    if bull > bear:
        direction: Literal["long", "short"] = "long"
        raw_confidence = bull
    elif bear > bull:
        direction = "short"
        raw_confidence = bear
    else:
        return None

    confidence = min(raw_confidence * regime_mult, 95.0)
    if confidence < MIN_CONFIDENCE:
        return None

    if direction == "long":
        default_stop = round(price * (1.0 - STOP_PCT), 4)
        natural_stop = round(day_low * 0.999, 4)
        stop_price = max(default_stop, natural_stop)
        stop_distance = price - stop_price
        if stop_distance <= 0:
            stop_price = default_stop
            stop_distance = price - stop_price
        target_price = round(price + stop_distance * TARGET_MULTIPLIER, 4)
    else:
        default_stop = round(price * (1.0 + STOP_PCT), 4)
        natural_stop = round(day_high * 1.001, 4)
        stop_price = min(default_stop, natural_stop)
        stop_distance = stop_price - price
        if stop_distance <= 0:
            stop_price = default_stop
            stop_distance = stop_price - price
        target_price = round(price - stop_distance * TARGET_MULTIPLIER, 4)

    if direction == "long" and target_price <= price:
        return None
    if direction == "short" and target_price >= price:
        return None

    rsi_text = f"RSI={rsi:.1f}" if rsi is not None else "RSI=n/a"
    vwap_text = (
        f"VWAP_dev={vwap_deviation:+.2f}%"
        if vwap_deviation is not None
        else "VWAP=n/a"
    )
    volume_text = (
        f"vol={volume_ratio:.1f}x" if volume_ratio is not None else "vol=n/a"
    )
    macd_text = (
        f"MACD_hist={macd_hist:+.4f}"
        if macd_hist is not None
        else "MACD=n/a"
    )

    return Signal(
        ticker=ticker,
        direction=direction,
        confidence=confidence,
        entry_price=price,
        stop_price=stop_price,
        target_price=target_price,
        reasoning=(
            f"{direction.upper()} | {rsi_text} {vwap_text} {volume_text} "
            f"{macd_text} | regime={regime} conf={confidence:.0f}"
        ),
        indicators={
            "rsi": rsi,
            "vwap": vwap,
            "price_vs_vwap_pct": vwap_deviation,
            "volume_ratio": volume_ratio,
            "macd_val": macd_value,
            "macd_sig": macd_signal,
            "macd_hist": macd_hist,
            "day_high": day_high,
            "day_low": day_low,
            "day_open": day_open,
            "current_price": price,
        },
    )


def _calc_sma(closes: list[float], period: int) -> float | None:
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def _calc_roc(closes: list[float], period: int = 10) -> float | None:
    if len(closes) < period + 1:
        return None
    return (closes[-1] - closes[-period - 1]) / closes[-period - 1] * 100.0


def _swing_ticker_signal(
    ticker: str,
    bars: list[dict],
    market_context: dict,
) -> Signal | None:
    if len(bars) < SMA_SLOW + 5:
        return None

    closes = [float(bar["c"]) for bar in bars]
    highs = [float(bar["h"]) for bar in bars]
    lows = [float(bar["l"]) for bar in bars]
    volumes = [float(bar.get("v", 0)) for bar in bars]
    price = closes[-1]

    fast = _calc_sma(closes, SMA_FAST)
    slow = _calc_sma(closes, SMA_SLOW)
    rsi = _calc_rsi(closes, SWING_RSI_PERIOD)
    roc = _calc_roc(closes)

    if fast is None or slow is None:
        return None

    recent_volume = sum(volumes[-5:]) / 5
    average_volume = sum(volumes[-20:]) / 20
    volume_ratio = recent_volume / average_volume if average_volume > 0 else None

    spread = (fast - slow) / slow * 100.0
    price_fast = (price - fast) / fast * 100.0
    bull = bear = 0.0

    if spread > SWING_TREND_STRENGTH_MIN:
        bull += SWING_CONF_WEIGHT_TREND * min(100.0, 50.0 + abs(spread) * 15.0)
    elif spread < -SWING_TREND_STRENGTH_MIN:
        bear += SWING_CONF_WEIGHT_TREND * min(100.0, 50.0 + abs(spread) * 15.0)

    if rsi is not None:
        if rsi < SWING_RSI_OVERSOLD:
            bull += SWING_CONF_WEIGHT_RSI * 90.0
        elif rsi > SWING_RSI_OVERBOUGHT:
            bear += SWING_CONF_WEIGHT_RSI * 90.0
        elif rsi < 45:
            bull += SWING_CONF_WEIGHT_RSI * 50.0
        elif rsi > 55:
            bear += SWING_CONF_WEIGHT_RSI * 50.0

    if roc is not None:
        if roc > 2.0:
            bull += SWING_CONF_WEIGHT_MOMENTUM * min(100.0, 40.0 + roc * 8.0)
        elif roc < -2.0:
            bear += SWING_CONF_WEIGHT_MOMENTUM * min(100.0, 40.0 + abs(roc) * 8.0)

    if volume_ratio is not None:
        score = (
            min(100.0, 50.0 + (volume_ratio - 1.0) * 40.0)
            if volume_ratio >= SWING_VOLUME_CONFIRM
            else 30.0
        )
        if bull >= bear:
            bull += SWING_CONF_WEIGHT_VOLUME * score
        else:
            bear += SWING_CONF_WEIGHT_VOLUME * score

    if spread > SWING_TREND_STRENGTH_MIN and -1.5 < price_fast < 0.5:
        bull *= 1.15
    elif spread < -SWING_TREND_STRENGTH_MIN and -0.5 < price_fast < 1.5:
        bear *= 1.15

    if bull > bear:
        direction: Literal["long", "short"] = "long"
        confidence = bull
    elif bear > bull:
        direction = "short"
        confidence = bear
    else:
        return None

    confidence = min(confidence, 95.0)
    if confidence < SWING_MIN_CONFIDENCE:
        return None

    recent_low = min(lows[-10:])
    recent_high = max(highs[-10:])

    if direction == "long":
        default_stop = round(price * (1.0 - SWING_STOP_PCT), 4)
        natural_stop = round(recent_low * 0.995, 4)
        stop_price = max(default_stop, natural_stop)
        distance = price - stop_price
        if distance <= 0:
            stop_price = default_stop
            distance = price - stop_price
        target_price = round(price + distance * SWING_TARGET_MULTIPLIER, 4)
    else:
        default_stop = round(price * (1.0 + SWING_STOP_PCT), 4)
        natural_stop = round(recent_high * 1.005, 4)
        stop_price = min(default_stop, natural_stop)
        distance = stop_price - price
        if distance <= 0:
            stop_price = default_stop
            distance = stop_price - price
        target_price = round(price - distance * SWING_TARGET_MULTIPLIER, 4)

    if direction == "long" and target_price <= price:
        return None
    if direction == "short" and target_price >= price:
        return None

    return Signal(
        ticker=ticker,
        direction=direction,
        confidence=confidence,
        entry_price=price,
        stop_price=stop_price,
        target_price=target_price,
        reasoning=(
            f"SWING {direction.upper()} | SMA{SMA_FAST}={fast:.2f}/"
            f"SMA{SMA_SLOW}={slow:.2f} spread={spread:+.1f}% | "
            f"RSI={rsi:.1f} ROC={roc:+.1f}% conf={confidence:.0f}"
            if rsi is not None and roc is not None
            else f"SWING {direction.upper()} | conf={confidence:.0f}"
        ),
        indicators={
            "sma_fast": fast,
            "sma_slow": slow,
            "sma_spread_pct": spread,
            "rsi": rsi,
            "roc_10": roc,
            "volume_ratio": volume_ratio,
            "price_vs_sma_fast_pct": price_fast,
            "recent_high": recent_high,
            "recent_low": recent_low,
            "current_price": price,
        },
    )


def _generate_day_signals(
    bars_df: dict[str, list[dict]],
    market_context: dict,
) -> list[Signal]:
    current_time = market_context.get("current_bar_time", "")
    if current_time and not _is_tradeable_time(current_time):
        return []

    regime = market_context.get("regime", "unknown")
    multiplier = REGIME_MULTIPLIER.get(regime, 0.70)
    signals = []

    for ticker, bars in bars_df.items():
        try:
            signal = _ticker_signal(ticker, bars, market_context, multiplier)
            if signal is not None:
                signals.append(signal)
        except Exception:
            continue
    return signals


def _generate_swing_signals(
    bars_df: dict[str, list[dict]],
    market_context: dict,
) -> list[Signal]:
    signals = []
    for ticker, bars in bars_df.items():
        try:
            signal = _swing_ticker_signal(ticker, bars, market_context)
            if signal is not None:
                signals.append(signal)
        except Exception:
            continue
    return signals


def generate_signals(
    bars_df: dict[str, list[dict]],
    market_context: dict,
) -> list[Signal]:
    if not isinstance(bars_df, dict):
        return []
    mode = market_context.get("mode", MODE)
    if mode == "swing":
        return _generate_swing_signals(bars_df, market_context)
    return _generate_day_signals(bars_df, market_context)