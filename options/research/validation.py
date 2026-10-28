"""Purged rolling walk-forward split construction. Every session after warm-up is out-of-sample for some fold."""

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
) -> tuple[list[WalkForwardFold], dict]:
    settings = config.get("walk_forward", {})
    train_size = int(settings.get("rolling_train_sessions", 252))
    test_size = int(settings.get("oos_sessions", 63))
    step = int(settings.get("step_sessions", test_size))
    min_periods = int(settings.get("minimum_oos_periods", 4))
    embargo = int(settings.get("embargo_sessions", 5))
    if min(train_size, test_size, step, min_periods) <= 0 or embargo < 0:
        raise ValueError("walk-forward session counts must be positive and embargo non-negative")
    if step < test_size:
        raise ValueError("step_sessions must be at least oos_sessions so OOS periods are independent and non-overlapping")
    lookback = longest_strategy_lookback_sessions(strategy, strategy_section, config)
    max_dte = max(
        int(strategy_section.get("max_dte", 0)),
        int(config.get("strategy_defaults", {}).get(strategy, {}).get("max_dte", 0)),
    )
    purge = max(lookback, max_dte) + embargo
    folds = []
    test_start = train_size + purge
    while test_start + test_size <= len(observations):
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
        needed = train_size + purge + (min_periods - 1) * step + test_size
        raise ValueError(
            f"insufficient history for {min_periods} purged rolling OOS periods: "
            f"need at least {needed} sessions, have {len(observations)}"
        )
    return folds, {
        "rolling_train_sessions": train_size,
        "oos_sessions": test_size,
        "step_sessions": step,
        "purge_sessions": purge,
        "lookback_sessions": lookback,
        "maximum_dte_sessions": max_dte,
        "embargo_sessions": embargo,
        "minimum_oos_periods": min_periods,
        "actual_oos_periods": len(folds),
        "oos_start": observations[folds[0].oos_start].as_of.isoformat(),
        "oos_end": observations[folds[-1].oos_end - 1].as_of.isoformat(),
    }


__all__ = ["WalkForwardFold", "longest_strategy_lookback_sessions", "make_purged_walk_forward"]
