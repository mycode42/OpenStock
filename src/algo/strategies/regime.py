"""Deterministic SPY regime filter and strategy ranking."""

from __future__ import annotations

import math

from src.algo.strategies.indicators import closes, moving_average


def rank_strategies(
    spy_daily_bars: list[dict], mode: str = "swing", *,
    event_enabled: bool = False, event_disable_in_volatile: bool = True,
    intraday_trend_enabled: bool = False, intraday_momentum_enabled: bool = False,
) -> dict:
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
        weights = {"event_driven": 0.56} if event_enabled and not event_disable_in_volatile else {}
        reason = f"20-day daily volatility {volatility:.1%} exceeds 2.5%"
    elif sma20 and values[-1] > sma20 and return_20d >= 0.01:
        regime = "trending_up"
        weights = {"trend_following": 1.0, "momentum": 0.9, "opening_range_breakout": 0.82, "multi_factor": 0.8, "event_driven": 0.58, "mean_reversion": 0.25, "swing_mean_reversion": 0.15, "pairs_stat_arb": 0.2}
        reason = f"SPY above 20-day average with {return_20d:.1%} 20-day return"
    elif sma20 and values[-1] < sma20 and return_20d <= -0.01:
        regime = "trending_down"
        # Mean reversion is active in downtrends so its upper-band short
        # rejection can participate; deterministic entry gates still block
        # its long signals in this regime.
        weights = {"trend_following": 0.72, "momentum": 0.65, "opening_range_breakout": 0.55, "multi_factor": 0.62, "event_driven": 0.56, "mean_reversion": 0.58, "swing_mean_reversion": 0.15, "pairs_stat_arb": 0.25}
        reason = f"SPY below 20-day average with {return_20d:.1%} 20-day return"
    else:
        regime = "range_bound"
        weights = {"mean_reversion": 1.0, "swing_mean_reversion": 0.78, "pairs_stat_arb": 0.68, "multi_factor": 0.65, "event_driven": 0.58, "trend_following": 0.4, "momentum": 0.4, "opening_range_breakout": 0.15}
        reason = f"SPY 20-day return {return_20d:.1%}; no directional trend"

    if mode == "algo_research":
        # The daily profile bank uses unprefixed trend/momentum for their
        # intraday rule modules and explicit swing_* names for daily signals.
        swing_trend_weight = weights.get("trend_following", 0.0)
        swing_momentum_weight = weights.get("momentum", 0.0)
        weights["swing_trend_following"] = swing_trend_weight
        weights["swing_momentum"] = swing_momentum_weight
        if regime == "trending_up":
            weights["trend_following"] = 0.62
            weights["momentum"] = 0.58
        elif regime == "trending_down":
            weights["trend_following"] = 0.58
            weights["momentum"] = 0.56
        else:
            weights["trend_following"] = 0.4
            weights["momentum"] = 0.4

    available = {"trend_following", "momentum", "mean_reversion"}
    if mode in {"day", "algo"}:
        available.add("opening_range_breakout")
    if mode == "algo":
        available.add("multi_factor")
        available.add("swing_mean_reversion")
        if event_enabled:
            available.add("event_driven")
        if intraday_trend_enabled:
            weights["intraday_trend"] = 0.3 if regime == "range_bound" else 0.58
            available.add("intraday_trend")
        if intraday_momentum_enabled:
            weights["intraday_momentum"] = 0.3 if regime == "range_bound" else 0.56
            available.add("intraday_momentum")
    if mode == "algo_research":
        available.update({
            "swing_trend_following", "swing_momentum", "swing_mean_reversion",
            "multi_factor",
        })
        if event_enabled:
            available.add("event_driven")
    ranked = sorted(weights.items(), key=lambda item: item[1], reverse=True)
    allowed = [name for name, score in ranked if score >= 0.55 and name in available]
    # Keep satellite eligibility separate from the core set the regime
    # advisor prioritizes. Multi Factor may also use its own bounded entry path.
    satellites = [name for name in allowed if name in {"multi_factor", "event_driven"}]
    active = [name for name in allowed if name not in {"multi_factor", "event_driven"}]
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
        "satellites": satellites,
    }
