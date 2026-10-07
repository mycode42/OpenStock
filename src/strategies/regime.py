"""Deterministic SPY regime filter and strategy ranking."""

from __future__ import annotations

import math

from src.strategies.indicators import closes, moving_average


def rank_strategies(spy_daily_bars: list[dict], mode: str = "swing") -> dict:
    values = closes(spy_daily_bars)
    if len(values) < 22:
        return {
            "regime": "unknown",
            "reason": "fewer than 22 completed SPY daily bars",
            "ranked": [],
            "active": [],
        }
    daily_returns = [values[i] / values[i - 1] - 1 for i in range(len(values) - 20, len(values))]
    mean_return = sum(daily_returns) / len(daily_returns)
    volatility = math.sqrt(sum((value - mean_return) ** 2 for value in daily_returns) / len(daily_returns))
    sma20 = moving_average(values, 20)
    return_20d = values[-1] / values[-21] - 1
    if volatility >= 0.025:
        regime = "volatile"
        weights = {}
        reason = f"20-day daily volatility {volatility:.1%} exceeds 2.5%"
    elif sma20 and values[-1] > sma20 and return_20d >= 0.01:
        regime = "trending_up"
        weights = {"trend_following": 1.0, "momentum": 0.9, "opening_range_breakout": 0.82, "mean_reversion": 0.25, "pairs_stat_arb": 0.2}
        reason = f"SPY above 20-day average with {return_20d:.1%} 20-day return"
    elif sma20 and values[-1] < sma20 and return_20d <= -0.01:
        regime = "trending_down"
        weights = {"trend_following": 0.72, "momentum": 0.65, "opening_range_breakout": 0.55, "mean_reversion": 0.15, "pairs_stat_arb": 0.25}
        reason = f"SPY below 20-day average with {return_20d:.1%} 20-day return"
    else:
        regime = "range_bound"
        weights = {"mean_reversion": 1.0, "pairs_stat_arb": 0.68, "trend_following": 0.25, "momentum": 0.2, "opening_range_breakout": 0.15}
        reason = f"SPY 20-day return {return_20d:.1%}; no directional trend"

    available = {"trend_following", "momentum", "mean_reversion"}
    if mode in {"day", "algo"}:
        available.add("opening_range_breakout")
    ranked = sorted(weights.items(), key=lambda item: item[1], reverse=True)
    active = [name for name, score in ranked if score >= 0.55 and name in available]
    return {
        "regime": regime,
        "reason": reason,
        "spy_return_20d": return_20d,
        "spy_volatility_20d": volatility,
        "ranked": [
            {"strategy": name, "score": score, "available": name in available}
            for name, score in ranked
        ],
        "active": active,
    }
