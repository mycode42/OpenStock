"""Purged, point-in-time research for the small Algo multi-factor model.

This module fits Ridge weights from cross-sectional factor z-scores and
forward returns. It never writes strategy configuration or submits orders.
"""

from __future__ import annotations

import math
import statistics
from bisect import bisect_right
from datetime import date

import numpy as np

from src.algo.strategies.multi_factor.config import MultiFactorConfig
from src.algo.strategies.multi_factor.diagnostics import information_coefficient
from src.algo.strategies.multi_factor.ranking import FactorRanker


FACTORS = ("momentum_1m", "momentum_3m", "momentum_6m", "low_volatility")
FORWARD_SESSIONS = 21
FACTOR_RESEARCH_START = date(2024, 1, 1)
FACTOR_TRAINING_SAMPLES = 12
RIDGE_ALPHA = 1.0


def _as_date(value: object) -> date | None:
    try:
        return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _feature_panel(
    daily_by_symbol: dict[str, list[dict]],
    *,
    session_dates: list[date],
    min_universe: int = 20,
) -> list[dict]:
    """Create monthly, as-of cross-sections with non-overlapping 21-day labels."""
    series: dict[str, tuple[list[date], list[float], list[float]]] = {}
    for symbol, rows in daily_by_symbol.items():
        if symbol.upper() == "SPY":
            continue
        clean = []
        for row in rows:
            session = _as_date(row.get("date", row.get("t")))
            try:
                close = float(row.get("c", row.get("close")))
            except (TypeError, ValueError):
                continue
            if session is not None and math.isfinite(close) and close > 0:
                clean.append((session, close))
        clean.sort()
        dates = [row[0] for row in clean]
        closes = [row[1] for row in clean]
        returns = [0.0] + [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
        if dates:
            series[symbol.upper()] = (dates, closes, returns)

    config = MultiFactorConfig(min_universe=min_universe)
    ranker = FactorRanker(config)
    first_index = max(126, max(config.volatility_lookback, *config.momentum_lookbacks.values()))
    observations = []
    # A 21-session step makes the 21-session forward-return labels non-overlapping.
    for index in range(first_index, len(session_dates) - FORWARD_SESSIONS, FORWARD_SESSIONS):
        as_of = session_dates[index]
        factor_values: dict[str, dict[str, float]] = {}
        future_returns: dict[str, float] = {}
        label_end = session_dates[index + FORWARD_SESSIONS]
        for symbol, (dates, closes, returns) in series.items():
            local_index = bisect_right(dates, as_of) - 1
            if local_index < 126 or dates[local_index] != as_of:
                continue
            future_index = bisect_right(dates, label_end) - 1
            if future_index < 0 or dates[future_index] != label_end:
                continue
            values = {
                name: closes[local_index] / closes[local_index - lookback] - 1
                for name, lookback in config.momentum_lookbacks.items()
            }
            recent_returns = returns[max(1, local_index - config.volatility_lookback + 1):local_index + 1]
            if len(recent_returns) < config.volatility_lookback:
                continue
            values["low_volatility"] = -statistics.pstdev(recent_returns)
            if all(math.isfinite(value) for value in values.values()):
                factor_values[symbol] = values
                future_returns[symbol] = closes[future_index] / closes[local_index] - 1

        ranked = ranker.rank(factor_values)
        zscores = {row["symbol"]: row["factor_zscores"] for row in ranked}
        if len(zscores) < min_universe:
            continue
        eligible_returns = {symbol: future_returns[symbol] for symbol in zscores if symbol in future_returns}
        if len(eligible_returns) < min_universe:
            continue
        observations.append({
            "date": as_of,
            "label_end": label_end,
            "zscores": zscores,
            "returns": eligible_returns,
        })
    return observations


def _training_arrays(observations: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    rows = []
    labels = []
    for observation in observations:
        returns = observation["returns"]
        center = statistics.mean(returns.values())
        deviation = statistics.pstdev(returns.values())
        if not math.isfinite(deviation) or deviation <= 0:
            continue
        for symbol, factors in observation["zscores"].items():
            if symbol not in returns or any(name not in factors for name in FACTORS):
                continue
            rows.append([float(factors[name]) for name in FACTORS])
            labels.append((returns[symbol] - center) / deviation)
    if not rows:
        return np.empty((0, len(FACTORS))), np.empty((0,))
    return np.asarray(rows, dtype=float), np.asarray(labels, dtype=float)


def _fit_weights(observations: list[dict], alpha: float) -> tuple[dict[str, float], bool]:
    matrix, target = _training_arrays(observations)
    if len(target) < len(FACTORS) * 4:
        raise ValueError("Insufficient purged cross-sectional observations to fit factor weights")
    gram = matrix.T @ matrix
    coefficients = np.linalg.solve(
        gram + np.eye(len(FACTORS)) * float(alpha) * len(target),
        matrix.T @ target,
    )
    positive = np.maximum(coefficients, 0.0)
    used_fallback = float(positive.sum()) <= 1e-12
    if used_fallback:
        positive = np.ones(len(FACTORS), dtype=float)
    positive /= positive.sum()
    return {name: round(float(weight), 8) for name, weight in zip(FACTORS, positive)}, used_fallback


def _scores(observation: dict, weights: dict[str, float]) -> dict[str, float]:
    return {
        symbol: sum(float(weights[name]) * float(factors[name]) for name in FACTORS)
        for symbol, factors in observation["zscores"].items()
        if symbol in observation["returns"] and all(name in factors for name in FACTORS)
    }


def _ic_summary(observations: list[dict], weights: dict[str, float]) -> dict:
    values = []
    for observation in observations:
        predictions = _scores(observation, weights)
        value = information_coefficient(predictions, observation["returns"])
        if value is not None:
            values.append(value)
    return {
        "sample_count": len(values),
        "mean_spearman_ic": round(statistics.mean(values), 6) if values else None,
        "median_spearman_ic": round(statistics.median(values), 6) if values else None,
        "positive_sample_pct": round(100 * sum(value > 0 for value in values) / len(values), 2) if values else None,
    }


def train_factor_model(daily_by_symbol: dict[str, list[dict]], *, min_universe: int = 20) -> dict:
    """Evaluate recent factor weights with a trailing, purged walk-forward fit.

    Factor observations are limited to 2024 onward. Each prediction uses only
    the previous 12 observations whose 21-session labels ended before the
    prediction date. Earlier prices are indicator warm-up only.
    """
    if "SPY" not in {symbol.upper() for symbol in daily_by_symbol}:
        raise ValueError("SPY daily bars are required to define the market-session calendar")
    spy_rows = next(rows for symbol, rows in daily_by_symbol.items() if symbol.upper() == "SPY")
    session_dates = sorted({
        session for row in spy_rows
        if (session := _as_date(row.get("date", row.get("t")))) is not None
    })
    observations = [
        row for row in _feature_panel(
            daily_by_symbol, session_dates=session_dates, min_universe=min_universe,
        )
        if row["date"] >= FACTOR_RESEARCH_START
    ]
    if len(observations) <= FACTOR_TRAINING_SAMPLES:
        raise ValueError(
            "Need more than 12 monthly factor observations from 2024 onward; "
            f"found {len(observations)}"
        )

    equal_weights = {name: 1 / len(FACTORS) for name in FACTORS}
    adaptive_observations = []
    for index, observation in enumerate(observations):
        # Labels must finish before the prediction date; retain only the
        # latest year of eligible observations to follow recent regimes.
        prior = [
            row for row in observations[:index]
            if row["label_end"] < observation["date"]
        ][-FACTOR_TRAINING_SAMPLES:]
        if len(prior) < FACTOR_TRAINING_SAMPLES:
            continue
        weights, _ = _fit_weights(prior, RIDGE_ALPHA)
        adaptive_observations.append({
            **observation,
            "_scores": _scores(observation, weights),
            "_weights": weights,
        })
    if len(adaptive_observations) < 3:
        raise ValueError(
            "Need at least three rolling factor evaluations after the 12-observation warm-up"
        )

    adaptive_values = [
        value for row in adaptive_observations
        if (value := information_coefficient(row["_scores"], row["returns"])) is not None
    ]
    adaptive_summary = {
        "sample_count": len(adaptive_values),
        "mean_spearman_ic": round(statistics.mean(adaptive_values), 6) if adaptive_values else None,
        "median_spearman_ic": round(statistics.median(adaptive_values), 6) if adaptive_values else None,
        "positive_sample_pct": round(100 * sum(value > 0 for value in adaptive_values) / len(adaptive_values), 2)
        if adaptive_values else None,
    }
    adaptive_equal = _ic_summary(adaptive_observations, equal_weights)

    latest_date = session_dates[-1]
    current_train = [
        row for row in observations if row["label_end"] < latest_date
    ][-FACTOR_TRAINING_SAMPLES:]
    if len(current_train) < FACTOR_TRAINING_SAMPLES:
        raise ValueError("Not enough recent factor observations to fit current weights")
    current_weights, current_fallback = _fit_weights(current_train, RIDGE_ALPHA)
    by_year = {}
    for year in sorted({row["date"].year for row in adaptive_observations}):
        year_rows = [row for row in adaptive_observations if row["date"].year == year]
        vals = [
            value for row in year_rows
            if (value := information_coefficient(row["_scores"], row["returns"])) is not None
        ]
        by_year[str(year)] = {
            "sample_count": len(vals),
            "mean_spearman_ic": round(statistics.mean(vals), 6) if vals else None,
            "positive_sample_pct": round(100 * sum(value > 0 for value in vals) / len(vals), 2) if vals else None,
        }

    return {
        "method": "rolling_recent_ridge_cross_sectional_21_session_purged",
        "factor_names": list(FACTORS),
        "forward_horizon_sessions": FORWARD_SESSIONS,
        "sampling_step_sessions": FORWARD_SESSIONS,
        "research_start": FACTOR_RESEARCH_START.isoformat(),
        "indicator_warmup_start": session_dates[0].isoformat(),
        "training_lookback_observations": FACTOR_TRAINING_SAMPLES,
        "ridge_alpha": RIDGE_ALPHA,
        "min_universe": min_universe,
        "session_start": FACTOR_RESEARCH_START.isoformat(),
        "session_end": latest_date.isoformat(),
        "observation_count": len(observations),
        "rolling_evaluation_count": len(adaptive_observations),
        "current_training_sample_count": len(current_train),
        "adaptive_prequential_ic": adaptive_summary,
        "adaptive_equal_weight_ic": adaptive_equal,
        "adaptive_prequential_ic_by_year": by_year,
        "current_candidate_weights": current_weights,
        "current_fit_equal_weight_fallback": current_fallback,
        "current_training_label_cutoff": max(row["label_end"] for row in current_train).isoformat(),
        "purge_rule": "Every 21-session forward label must end strictly before the feature date used for prediction.",
        "universe_limitation": (
            "The current fixed Algo universe is replayed across the 2024-onward evaluation period; "
            "historical membership and listing eligibility are not point-in-time, "
            "so results have survivorship and availability bias."
        ),
    }


__all__ = ["FACTORS", "train_factor_model"]
