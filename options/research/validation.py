"""Purged rolling walk-forward and final-holdout split construction."""

from __future__ import annotations

from dataclasses import dataclass

from options.research.dataset import ResearchObservation


@dataclass(frozen=True)
class WalkForwardFold:
    number: int
    train_start: int
    train_end: int
    oos_start: int
    oos_end: int
    purge_sessions: int


def longest_strategy_lookback_sessions(strategy: str, section: dict, config: dict) -> int:
    if strategy == "vrp":
        return int(section.get("realized_lookback_sessions", 20))
    if strategy == "long_volatility":
        lookbacks = [int(section.get("iv_percentile_lookback_sessions", 252))]
        if (
            section.get("activation_mode") == "regime_or_realized_vol"
            or section.get("max_iv_to_realized_ratio") is not None
        ):
            lookbacks.append(int(section.get("realized_lookback_sessions", 20)))
        return max(lookbacks)
    if strategy == "llm_driven":
        # The frozen LLM recommendation is a point-in-time dataset input; the
        # options expression does not inspect a rolling calendar history.
        return 0
    return int(config.get("walk_forward", {}).get("directional_input_lookback_sessions", 252))


def make_purged_walk_forward(
    observations: tuple[ResearchObservation, ...],
    *,
    strategy: str,
    strategy_section: dict,
    config: dict,
) -> tuple[list[WalkForwardFold], tuple[ResearchObservation, ...], dict]:
    settings = config.get("walk_forward", {})
    train_size = int(settings.get("rolling_train_sessions", 252))
    test_size = int(settings.get("oos_sessions", 63))
    step = int(settings.get("step_sessions", test_size))
    min_periods = int(settings.get("minimum_oos_periods", 4))
    holdout_size = int(settings.get("final_holdout_sessions", 252))
    embargo = int(settings.get("embargo_sessions", 5))
    if min(train_size, test_size, step, min_periods, holdout_size) <= 0 or embargo < 0:
        raise ValueError("walk-forward session counts must be positive and embargo non-negative")
    if step < test_size:
        raise ValueError("step_sessions must be at least oos_sessions so OOS periods are independent and non-overlapping")
    lookback = longest_strategy_lookback_sessions(strategy, strategy_section, config)
    max_dte = max(
        int(strategy_section.get("max_dte", 0)),
        int(config.get("strategy_defaults", {}).get(strategy, {}).get("max_dte", 0)),
    )
    purge = max(lookback, max_dte) + embargo
    holdout_start = len(observations) - holdout_size
    folds = []
    test_start = train_size + purge
    while test_start + test_size <= holdout_start:
        train_end = test_start - purge
        train_start = train_end - train_size
        if train_start >= 0:
            folds.append(WalkForwardFold(
                number=len(folds) + 1,
                train_start=train_start,
                train_end=train_end,
                oos_start=test_start,
                oos_end=test_start + test_size,
                purge_sessions=purge,
            ))
        test_start += step
    if len(folds) < min_periods:
        needed = train_size + purge + (min_periods - 1) * step + test_size + holdout_size
        raise ValueError(
            f"insufficient history for {min_periods} purged rolling OOS periods: "
            f"need at least {needed} sessions, have {len(observations)}"
        )
    if holdout_start < 0:
        raise ValueError("dataset is shorter than the untouched final holdout")
    holdout = observations[holdout_start:]
    labels = set()
    regime_labels = set()
    high_vol_labels = {
        str(value).strip().lower().replace("-", "_")
        for value in settings.get("high_volatility_labels", ["high_vol", "high_volatility", "elevated", "crisis", "risk_off"])
    }
    high_vol_rows = 0
    high_vol_streak = 0
    longest_high_vol_streak = 0
    for item in holdout:
        regime = item.regime.strip().lower().replace("-", "_")
        volatility = item.volatility_state.strip().lower().replace("-", "_")
        if regime and regime != "unknown":
            labels.add(regime)
            if regime not in high_vol_labels and regime not in {"normal", "low_vol", "low_volatility"}:
                regime_labels.add(regime)
        if volatility and volatility != "unknown":
            labels.add(volatility)
        if regime in high_vol_labels or volatility in high_vol_labels:
            high_vol_rows += 1
            high_vol_streak += 1
            longest_high_vol_streak = max(longest_high_vol_streak, high_vol_streak)
        else:
            high_vol_streak = 0
    required_label_count = int(settings.get("minimum_holdout_regime_labels", 2))
    required_high_vol_sessions = int(settings.get("minimum_high_volatility_holdout_sessions", 5))
    coverage = {
        "observed_labels": sorted(labels),
        "observed_market_regimes": sorted(regime_labels),
        "required_distinct_market_regimes": required_label_count,
        "distinct_market_regime_count": len(regime_labels),
        "high_volatility_rows": high_vol_rows,
        "required_high_volatility_sessions": required_high_vol_sessions,
        "longest_consecutive_high_volatility_period": longest_high_vol_streak,
        "meets_minimum_coverage": len(regime_labels) >= required_label_count and longest_high_vol_streak >= required_high_vol_sessions,
    }
    return folds, holdout, {
        "rolling_train_sessions": train_size,
        "oos_sessions": test_size,
        "step_sessions": step,
        "purge_sessions": purge,
        "lookback_sessions": lookback,
        "maximum_dte_sessions": max_dte,
        "embargo_sessions": embargo,
        "minimum_oos_periods": min_periods,
        "actual_oos_periods": len(folds),
        "holdout_sessions": len(holdout),
        "holdout_start": holdout[0].as_of.isoformat() if holdout else None,
        "holdout_regime_coverage": coverage,
    }


__all__ = ["WalkForwardFold", "longest_strategy_lookback_sessions", "make_purged_walk_forward"]
