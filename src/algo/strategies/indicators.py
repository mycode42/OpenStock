"""Small dependency-free indicators shared by the pure strategy functions."""

from __future__ import annotations

import math


def closes(bars: list[dict]) -> list[float]:
    values = []
    for bar in bars:
        try:
            value = float(bar["c"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            values.append(value)
    return values


def moving_average(values: list[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def population_std(values: list[float]) -> float | None:
    if not values:
        return None
    mean = sum(values) / len(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))


def rsi(values: list[float], period: int = 14) -> float | None:
    if period <= 0 or len(values) < period + 1:
        return None
    changes = [values[i] - values[i - 1] for i in range(len(values) - period, len(values))]
    gains = sum(max(change, 0.0) for change in changes) / period
    losses = sum(max(-change, 0.0) for change in changes) / period
    if losses == 0:
        return 100.0 if gains else 50.0
    relative_strength = gains / losses
    return 100 - 100 / (1 + relative_strength)


def efficiency_ratio(values: list[float], period: int = 20) -> float | None:
    """Kaufman efficiency ratio over the last completed ``period`` moves."""
    if period <= 0 or len(values) < period + 1:
        return None
    path = sum(abs(values[index] - values[index - 1]) for index in range(len(values) - period, len(values)))
    if path <= 0:
        return 0.0
    return min(1.0, abs(values[-1] - values[-1 - period]) / path)


def _ohlc_rows(bars: list[dict]) -> list[tuple[float, float, float]]:
    rows = []
    for bar in bars:
        try:
            high, low, close = float(bar["h"]), float(bar["l"]), float(bar["c"])
        except (KeyError, TypeError, ValueError):
            continue
        if all(math.isfinite(value) and value > 0 for value in (high, low, close)) and high >= low:
            rows.append((high, low, close))
    return rows


def _directional_rows(bars: list[dict]) -> list[tuple[float, float, float]]:
    rows = _ohlc_rows(bars)
    result = []
    for index, (high, low, close) in enumerate(rows):
        if index == 0:
            continue
        previous_high, previous_low, previous_close = rows[index - 1]
        true_range = max(high - low, abs(high - previous_close), abs(low - previous_close))
        upward = high - previous_high
        downward = previous_low - low
        plus_dm = upward if upward > downward and upward > 0 else 0.0
        minus_dm = downward if downward > upward and downward > 0 else 0.0
        result.append((true_range, plus_dm, minus_dm))
    return result


def average_true_range(bars: list[dict], period: int = 14) -> float | None:
    """Wilder ATR from completed OHLC bars."""
    if period <= 0:
        return None
    rows = _directional_rows(bars)
    if len(rows) < period:
        return None
    value = sum(row[0] for row in rows[:period]) / period
    for row in rows[period:]:
        value = (value * (period - 1) + row[0]) / period
    return value


def adx(bars: list[dict], period: int = 14) -> float | None:
    """Wilder ADX from completed OHLC bars."""
    if period <= 0:
        return None
    rows = _directional_rows(bars)
    if len(rows) < period * 2 - 1:
        return None
    smoothed_tr = sum(row[0] for row in rows[:period])
    smoothed_plus = sum(row[1] for row in rows[:period])
    smoothed_minus = sum(row[2] for row in rows[:period])
    dx_values = []
    for index in range(period - 1, len(rows)):
        if index >= period:
            true_range, plus_dm, minus_dm = rows[index]
            smoothed_tr = smoothed_tr - smoothed_tr / period + true_range
            smoothed_plus = smoothed_plus - smoothed_plus / period + plus_dm
            smoothed_minus = smoothed_minus - smoothed_minus / period + minus_dm
        if smoothed_tr <= 0:
            dx_values.append(0.0)
            continue
        plus_di = 100.0 * smoothed_plus / smoothed_tr
        minus_di = 100.0 * smoothed_minus / smoothed_tr
        denominator = plus_di + minus_di
        dx_values.append(100.0 * abs(plus_di - minus_di) / denominator if denominator else 0.0)
    if len(dx_values) < period:
        return None
    result = sum(dx_values[:period]) / period
    for value in dx_values[period:]:
        result = (result * (period - 1) + value) / period
    return result

