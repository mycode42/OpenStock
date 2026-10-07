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

