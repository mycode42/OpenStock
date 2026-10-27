"""Purged, point-in-time research for the small Algo multi-factor model.

This module fits regularized Ridge weights from cross-sectional factor
z-scores and purged forward returns. It never writes strategy configuration
or submits orders.
"""

from __future__ import annotations
from autoresearch.costs import EQUITY_COMMISSION_PER_SHARE, EQUITY_SLIPPAGE_BPS

import math
import statistics
from bisect import bisect_right
from datetime import date
import hashlib
import json

import numpy as np

from strategies.multi_factor.config import MultiFactorConfig, REGIME_FACTOR_WEIGHT_PROFILES
from strategies.multi_factor.diagnostics import information_coefficient
from strategies.multi_factor.features import _available_value
from strategies.multi_factor.ranking import FactorRanker
from .artifacts.candidate_archive import candidate_id as archive_candidate_id
from strategies.multi_factor.tree_models import (
    TREE_LOGIC_VERSIONS, TREE_MODEL_FAMILIES, fit_tree_factor_model,
    predict_tree_factor_model,
)


FACTORS = ("momentum_1m", "momentum_3m", "momentum_6m", "low_volatility")
OPTIONAL_FACTORS = ("price_volume_momentum",)
FUNDAMENTAL_FACTORS = (
    "value_earnings_yield", "value_book_to_price", "value_fcf_yield",
    "quality_profitability", "quality_balance_sheet",
)
FORWARD_SESSIONS = 21
FACTOR_RESEARCH_START = date(2020, 1, 1)
FACTOR_TRAINING_SAMPLES = 12
RIDGE_ALPHA = 1.0
MODEL_FAMILY = "ridge_cross_sectional"
LOGIC_VERSION = "multi_factor_ridge_v5_neutral_missing_factor_timeline"
COST_MODEL_VERSION = "factor_shadow_costs_v1"


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
    sector_by_symbol: dict[str, str] | None = None,
    fundamentals_by_symbol: dict[str, list[dict]] | None = None,
) -> list[dict]:
    """Create monthly, as-of cross-sections with non-overlapping 21-day labels."""
    series: dict[str, tuple[list[date], list[float], list[float | None], list[float]]] = {}
    for symbol, rows in daily_by_symbol.items():
        clean = []
        for row in rows:
            session = _as_date(row.get("date", row.get("t")))
            try:
                close = float(row.get("c", row.get("close")))
            except (TypeError, ValueError):
                continue
            try:
                raw_volume = row.get("v", row.get("volume"))
                volume = float(raw_volume) if raw_volume is not None else None
            except (TypeError, ValueError):
                volume = None
            if volume is not None and (not math.isfinite(volume) or volume < 0):
                volume = None
            if session is not None and math.isfinite(close) and close > 0:
                clean.append((session, close, volume))
        clean.sort()
        dates = [row[0] for row in clean]
        closes = [row[1] for row in clean]
        volumes = [row[2] for row in clean]
        returns = [0.0] + [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
        if dates:
            series[symbol.upper()] = (dates, closes, volumes, returns)
    if "SPY" not in series:
        raise ValueError("SPY has no usable daily closes for factor research")

    config = MultiFactorConfig(
        min_universe=min_universe,
        factor_neutralization="sector",
        momentum_lookbacks={"momentum_1m": 20, "momentum_3m": 60, "momentum_6m": 120},
        factor_weights={
            **{name: 1.0 for name in FACTORS},
            **{name: 0.2 for name in OPTIONAL_FACTORS + FUNDAMENTAL_FACTORS},
        },
    )
    ranker = FactorRanker(config)
    first_index = max(126, max(config.volatility_lookback, *config.momentum_lookbacks.values()))
    observations = []
    # A 21-session step makes the 21-session forward-return labels non-overlapping.
    spy_dates, spy_closes, _, _ = series["SPY"]
    for index in range(first_index, len(session_dates) - FORWARD_SESSIONS, FORWARD_SESSIONS):
        as_of = session_dates[index]
        spy_index = bisect_right(spy_dates, as_of) - 1
        if spy_index < 63:
            continue
        spy_return = spy_closes[spy_index] / spy_closes[spy_index - 21] - 1
        market_regime = (
            "trending_up" if spy_return >= 0.03 else
            "trending_down" if spy_return <= -0.03 else "range_bound"
        )
        factor_values: dict[str, dict[str, float]] = {}
        future_returns: dict[str, float] = {}
        label_end = session_dates[index + FORWARD_SESSIONS]
        start_prices: dict[str, float] = {}
        tradability: dict[str, dict] = {}
        for symbol, (dates, closes, volumes, returns) in series.items():
            if symbol == "SPY":
                continue
            local_index = bisect_right(dates, as_of) - 1
            if local_index < 126 or dates[local_index] != as_of:
                continue
            if closes[local_index] < config.minimum_price:
                continue
            local_volumes = volumes[max(0, local_index - config.liquidity_lookback + 1):local_index + 1]
            usable_volumes = [value for value in local_volumes if value is not None and value > 0]
            liquidity_enabled = len(usable_volumes) == len(local_volumes) == config.liquidity_lookback
            average_dollar_volume = (
                statistics.mean(
                    closes[local_index - config.liquidity_lookback + 1 + offset] * float(volume)
                    for offset, volume in enumerate(local_volumes)
                )
                if liquidity_enabled else None
            )
            if liquidity_enabled and average_dollar_volume < config.minimum_average_dollar_volume:
                continue
            tradability[symbol] = {
                "history_sessions": local_index + 1,
                "liquidity_checked": liquidity_enabled,
                "average_dollar_volume": average_dollar_volume,
            }
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
            if local_index >= 21:
                recent_volumes = volumes[local_index - 20:local_index + 1]
                baseline = [value for value in recent_volumes[:-1] if value is not None and value > 0]
                if len(baseline) == 20 and all(value is not None and value > 0 for value in recent_volumes):
                    typical = statistics.median(baseline)
                    weighted_returns = []
                    for bar_index in range(local_index - 19, local_index + 1):
                        relative_volume = min(4.0, max(0.25, float(volumes[bar_index]) / typical))
                        weighted_returns.append(returns[bar_index] * relative_volume)
                    values["price_volume_momentum"] = sum(weighted_returns)
            fundamental_rows = (fundamentals_by_symbol or {}).get(symbol, [])
            current_price = closes[local_index]
            fundamental_factors = (
                ("value_earnings_yield", ("earnings_yield",), ("eps_per_share", "earnings_per_share_diluted", "earnings_per_share_basic")),
                ("value_book_to_price", ("book_to_price", "book_equity_to_market_cap"), ("book_value_per_share",)),
                ("value_fcf_yield", ("free_cash_flow_yield", "fcf_yield"), ("free_cash_flow_per_share",)),
                ("quality_profitability", ("gross_profitability", "return_on_invested_capital", "return_on_equity"), ()),
            )
            for name, direct_keys, per_share_keys in fundamental_factors:
                value = _available_value(fundamental_rows, as_of, direct_keys)
                if value is None and per_share_keys:
                    per_share = _available_value(fundamental_rows, as_of, per_share_keys)
                    value = per_share / current_price if per_share is not None else None
                if value is not None:
                    values[name] = value
            leverage = _available_value(
                fundamental_rows, as_of, ("debt_to_equity", "net_debt_to_ebitda"),
            )
            if leverage is not None:
                values["quality_balance_sheet"] = -leverage
            if all(math.isfinite(value) for value in values.values()):
                factor_values[symbol] = values
                future_returns[symbol] = closes[future_index] / closes[local_index] - 1
                start_prices[symbol] = closes[local_index]

        ranked = ranker.rank(factor_values, groups_by_symbol=sector_by_symbol)
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
            "start_prices": {symbol: start_prices[symbol] for symbol in eligible_returns},
            "factor_names": list(ranked[0]["factor_zscores"]) if ranked else [],
            "tradability": tradability,
            "neutralization_applied": bool(sector_by_symbol),
            "market_regime": market_regime,
        })
    return observations


def _training_arrays(
    observations: list[dict], factor_names: tuple[str, ...] | list[str] = FACTORS,
) -> tuple[np.ndarray, np.ndarray]:
    rows = []
    labels = []
    for observation in observations:
        returns = observation.get("net_returns", observation["returns"])
        center = statistics.mean(returns.values())
        deviation = statistics.pstdev(returns.values())
        if not math.isfinite(deviation) or deviation <= 0:
            continue
        for symbol, row_factors in observation["zscores"].items():
            if symbol not in returns or any(name not in row_factors for name in factor_names):
                continue
            rows.append([float(row_factors[name]) for name in factor_names])
            labels.append((returns[symbol] - center) / deviation)
    if not rows:
        return np.empty((0, len(factor_names))), np.empty((0,))
    return np.asarray(rows, dtype=float), np.asarray(labels, dtype=float)


def _fit_weights(
    observations: list[dict], alpha: float, factor_names: tuple[str, ...] | list[str] = FACTORS,
) -> tuple[dict[str, float], bool]:
    matrix, target = _training_arrays(observations, factor_names)
    if len(target) < len(factor_names) * 4:
        raise ValueError("Insufficient purged cross-sectional observations to fit factor weights")
    gram = matrix.T @ matrix
    coefficients = np.linalg.solve(
        gram + np.eye(len(factor_names)) * float(alpha) * len(target),
        matrix.T @ target,
    )
    positive = np.maximum(coefficients, 0.0)
    used_fallback = float(positive.sum()) <= 1e-12
    if used_fallback:
        positive = np.ones(len(factor_names), dtype=float)
    positive /= positive.sum()
    return {name: round(float(weight), 8) for name, weight in zip(factor_names, positive)}, used_fallback


def _scores(observation: dict, weights: dict[str, float]) -> dict[str, float]:
    return {
        symbol: sum(float(weights[name]) * float(factors[name]) for name in weights)
        for symbol, factors in observation["zscores"].items()
        if symbol in observation["returns"] and all(name in factors for name in weights)
    }


def _tree_scores(observation: dict, artifact: dict) -> dict[str, float]:
    names = artifact["factor_names"]
    symbols = [
        symbol for symbol, factors in sorted(observation["zscores"].items())
        if symbol in observation["returns"] and all(name in factors for name in names)
    ]
    if not symbols:
        return {}
    matrix = np.asarray(
        [[observation["zscores"][symbol][name] for name in names] for symbol in symbols],
        dtype=float,
    )
    predictions = predict_tree_factor_model(artifact, matrix)
    return {symbol: float(value) for symbol, value in zip(symbols, predictions)}


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
        "ic_standard_deviation": round(statistics.pstdev(values), 6) if values else None,
        "positive_sample_pct": round(100 * sum(value > 0 for value in values) / len(values), 2) if values else None,
    }


def _net_long_returns(
    observation: dict,
    *,
    spread_bps: float,
    slippage_bps: float,
    commission_per_share: float,
) -> dict[str, float]:
    round_trip_rate = 2 * (spread_bps / 2 + slippage_bps) / 10_000
    output = {}
    for symbol, value in observation["returns"].items():
        price = float(observation.get("start_prices", {}).get(symbol, 0))
        commission_rate = 2 * commission_per_share / price if price > 0 else 0.0
        output[symbol] = float(value) - round_trip_rate - commission_rate
    return output


def _after_cost_long_short(
    observation: dict,
    scores: dict[str, float],
    *,
    spread_bps: float,
    slippage_bps: float,
    commission_per_share: float,
    short_borrow_annual_pct: float,
) -> float | None:
    """Equal-weight top/bottom baskets after modeled entry, exit, and borrow costs."""
    shared = [symbol for symbol in scores if symbol in observation["returns"]]
    if len(shared) < 6:
        return None
    ordered = sorted(shared, key=lambda symbol: (scores[symbol], symbol))
    bucket_size = max(1, len(ordered) // 10)
    shorts, longs = ordered[:bucket_size], ordered[-bucket_size:]
    side_cost_rate = (spread_bps / 2 + slippage_bps) / 10_000
    holding_years = FORWARD_SESSIONS / 252
    outcomes = []
    for symbol in longs:
        price = float(observation["start_prices"].get(symbol, 0))
        if price <= 0:
            continue
        cost = 2 * side_cost_rate + 2 * commission_per_share / price
        outcomes.append(float(observation["returns"][symbol]) - cost)
    for symbol in shorts:
        price = float(observation["start_prices"].get(symbol, 0))
        if price <= 0:
            continue
        cost = 2 * side_cost_rate + 2 * commission_per_share / price
        borrow = short_borrow_annual_pct / 100 * holding_years
        outcomes.append(-float(observation["returns"][symbol]) - cost - borrow)
    return statistics.mean(outcomes) if outcomes else None


def _metric_summary(values: list[float]) -> dict:
    return {
        "sample_count": len(values),
        "mean": round(statistics.mean(values), 6) if values else None,
        "median": round(statistics.median(values), 6) if values else None,
        "standard_deviation": round(statistics.pstdev(values), 6) if values else None,
        "positive_sample_pct": round(100 * sum(value > 0 for value in values) / len(values), 2) if values else None,
    }


def _configured_regime_weights(factor_names: tuple[str, ...], regime: str) -> dict[str, float]:
    """Project the configured regime timing profile onto researchable factors."""
    config = MultiFactorConfig()
    weights = dict(config.factor_weights)
    weights.update(REGIME_FACTOR_WEIGHT_PROFILES.get(regime, {}))
    projected = {name: float(weights.get(name, 0.0)) for name in factor_names if weights.get(name, 0.0) > 0}
    total = sum(projected.values())
    return {name: value / total for name, value in projected.items()} if total > 0 else {
        name: 1 / len(factor_names) for name in factor_names
    }


def train_factor_model(
    daily_by_symbol: dict[str, list[dict]],
    *,
    min_universe: int = 20,
    sector_by_symbol: dict[str, str] | None = None,
    fundamentals_by_symbol: dict[str, list[dict]] | None = None,
    fundamentals_diagnostics: dict | None = None,
    fundamentals_data_version: str | None = None,
    data_version: str | None = None,
    spread_bps: float = 3.0,
    slippage_bps: float = EQUITY_SLIPPAGE_BPS,
    commission_per_share: float = EQUITY_COMMISSION_PER_SHARE,
    short_borrow_annual_pct: float = 3.0,
) -> dict:
    """Evaluate recent factor weights with a trailing, purged walk-forward fit.

    Factor observations are limited to the configured research period. Each prediction uses only
    the previous 12 observations whose 21-session labels ended before the
    prediction date. Earlier prices are indicator warm-up only.
    """
    for name, value in (
        ("spread_bps", spread_bps), ("slippage_bps", slippage_bps),
        ("commission_per_share", commission_per_share),
        ("short_borrow_annual_pct", short_borrow_annual_pct),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be a finite non-negative number")
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
            sector_by_symbol=sector_by_symbol,
            fundamentals_by_symbol=fundamentals_by_symbol,
        )
        if row["date"] >= FACTOR_RESEARCH_START
    ]
    if not observations:
        raise ValueError("No factor cross-sections passed the history and tradability masks")
    if len(observations) <= FACTOR_TRAINING_SAMPLES:
        raise ValueError(
            "Need more than 12 monthly factor observations in the configured research period; "
            f"found {len(observations)}"
        )
    for observation in observations:
        observation["net_returns"] = _net_long_returns(
            observation, spread_bps=spread_bps, slippage_bps=slippage_bps,
            commission_per_share=commission_per_share,
        )

    available_factors = set(FACTORS + OPTIONAL_FACTORS + FUNDAMENTAL_FACTORS)
    factor_names = tuple(
        name for name in FACTORS + OPTIONAL_FACTORS + FUNDAMENTAL_FACTORS
        if name in available_factors and all(name in row["factor_names"] for row in observations)
    )
    if not factor_names:
        raise ValueError("No factor has complete cross-sectional coverage across the research observations")
    equal_weights = {name: 1 / len(factor_names) for name in factor_names}
    legacy_config_weights = {
        name: float(weight) for name, weight in MultiFactorConfig().factor_weights.items()
        if name in factor_names and weight > 0
    }
    legacy_weight_total = sum(legacy_config_weights.values())
    legacy_weights = (
        {name: weight / legacy_weight_total for name, weight in legacy_config_weights.items()}
        if legacy_weight_total > 0 else equal_weights
    )
    adaptive_observations = []
    tree_timelines: dict[str, list[dict]] = {mode: [] for mode in TREE_MODEL_FAMILIES}
    for index, observation in enumerate(observations):
        # Labels must finish before the prediction date; retain only the
        # latest year of eligible observations to follow recent regimes.
        prior = [
            row for row in observations[:index]
            if row["label_end"] < observation["date"]
        ][-FACTOR_TRAINING_SAMPLES:]
        if len(prior) < FACTOR_TRAINING_SAMPLES:
            continue
        weights, equal_weight_fallback = _fit_weights(prior, RIDGE_ALPHA, factor_names)
        predictions = _scores(observation, weights)
        tree_scores = {}
        matrix, target = _training_arrays(prior, factor_names)
        for mode in TREE_MODEL_FAMILIES:
            artifact = fit_tree_factor_model(mode, matrix, target, factor_names)
            model_scores = _tree_scores(observation, artifact)
            snapshot = {
                **artifact,
                "effective_date": observation["date"].isoformat(),
                "training_label_cutoff": max(row["label_end"] for row in prior).isoformat(),
                "training_observation_count": len(prior),
                "minimum_factor_coverage": min_universe,
                "cost_model_version": COST_MODEL_VERSION,
                "data_version": data_version,
                "fundamentals_data_version": fundamentals_data_version,
            }
            tree_timelines[mode].append(snapshot)
            tree_scores[mode] = model_scores
        adaptive_observations.append({
            **observation,
            "_scores": predictions,
            "_tree_scores": tree_scores,
            "_weights": weights,
            "_weight_fit_fallback": equal_weight_fallback,
            "_training_label_cutoff": max(row["label_end"] for row in prior).isoformat(),
            "_training_observation_count": len(prior),
            "_after_cost_return": _after_cost_long_short(
                observation, predictions, spread_bps=spread_bps,
                slippage_bps=slippage_bps, commission_per_share=commission_per_share,
                short_borrow_annual_pct=short_borrow_annual_pct,
            ),
        })
    if len(adaptive_observations) < 3:
        raise ValueError(
            "Need at least three rolling factor evaluations after the 12-observation warm-up"
        )

    adaptive_values = [
        value for row in adaptive_observations
        if (value := information_coefficient(row["_scores"], row["returns"])) is not None
    ]
    adaptive_summary = _metric_summary(adaptive_values)
    adaptive_summary["mean_spearman_ic"] = adaptive_summary.pop("mean")
    adaptive_summary["median_spearman_ic"] = adaptive_summary.pop("median")
    adaptive_summary["ic_standard_deviation"] = adaptive_summary.pop("standard_deviation")
    adaptive_equal = _ic_summary(adaptive_observations, equal_weights)
    adaptive_legacy_static = _ic_summary(adaptive_observations, legacy_weights)
    adaptive_net_ic = _metric_summary([
        value for row in adaptive_observations
        if (value := information_coefficient(row["_scores"], row["net_returns"])) is not None
    ])
    equal_net_ic = _metric_summary([
        value for row in adaptive_observations
        if (value := information_coefficient(_scores(row, equal_weights), row["net_returns"])) is not None
    ])
    legacy_static_net_ic = _metric_summary([
        value for row in adaptive_observations
        if (value := information_coefficient(_scores(row, legacy_weights), row["net_returns"])) is not None
    ])
    adaptive_net = [row["_after_cost_return"] for row in adaptive_observations if row["_after_cost_return"] is not None]
    equal_net = [
        value for row in adaptive_observations
        if (value := _after_cost_long_short(
            row, _scores(row, equal_weights), spread_bps=spread_bps,
            slippage_bps=slippage_bps, commission_per_share=commission_per_share,
            short_borrow_annual_pct=short_borrow_annual_pct,
        )) is not None
    ]
    legacy_static_net = [
        value for row in adaptive_observations
        if (value := _after_cost_long_short(
            row, _scores(row, legacy_weights), spread_bps=spread_bps,
            slippage_bps=slippage_bps, commission_per_share=commission_per_share,
            short_borrow_annual_pct=short_borrow_annual_pct,
        )) is not None
    ]
    learned_net_by_date = {
        row["date"].isoformat(): row["_after_cost_return"]
        for row in adaptive_observations if row["_after_cost_return"] is not None
    }
    legacy_net_by_date = {
        row["date"].isoformat(): value
        for row in adaptive_observations
        if (value := _after_cost_long_short(
            row, _scores(row, legacy_weights), spread_bps=spread_bps,
            slippage_bps=slippage_bps, commission_per_share=commission_per_share,
            short_borrow_annual_pct=short_borrow_annual_pct,
        )) is not None
    }
    paired_dates = sorted(set(learned_net_by_date) & set(legacy_net_by_date))
    paired_return_delta = [
        learned_net_by_date[day] - legacy_net_by_date[day] for day in paired_dates
    ]
    latest_date = session_dates[-1]
    current_train = [
        row for row in observations if row["label_end"] < latest_date
    ][-FACTOR_TRAINING_SAMPLES:]
    if len(current_train) < FACTOR_TRAINING_SAMPLES:
        raise ValueError("Not enough recent factor observations to fit current weights")
    current_weights, current_fallback = _fit_weights(current_train, RIDGE_ALPHA, factor_names)
    current_matrix, current_target = _training_arrays(current_train, factor_names)
    current_models = {}
    for mode in TREE_MODEL_FAMILIES:
        artifact = fit_tree_factor_model(mode, current_matrix, current_target, factor_names)
        current_models[mode] = {
            **artifact,
            "effective_date": latest_date.isoformat(),
            "training_label_cutoff": max(row["label_end"] for row in current_train).isoformat(),
            "training_observation_count": len(current_train),
            "minimum_factor_coverage": min_universe,
            "cost_model_version": COST_MODEL_VERSION,
            "data_version": data_version,
            "fundamentals_data_version": fundamentals_data_version,
            "current_fit": True,
        }
    tree_model_comparisons = {}
    for mode, family in TREE_MODEL_FAMILIES.items():
        rows = [
            row for row in adaptive_observations
            if row.get("_tree_scores", {}).get(mode)
        ]
        tree_net_ics = [
            value for row in rows
            if (value := information_coefficient(row["_tree_scores"][mode], row["net_returns"])) is not None
        ]
        tree_gross_ics = [
            value for row in rows
            if (value := information_coefficient(row["_tree_scores"][mode], row["returns"])) is not None
        ]
        tree_returns = [
            value for row in rows
            if (value := _after_cost_long_short(
                row, row["_tree_scores"][mode], spread_bps=spread_bps,
                slippage_bps=slippage_bps, commission_per_share=commission_per_share,
                short_borrow_annual_pct=short_borrow_annual_pct,
            )) is not None
        ]
        tree_return_by_date = {
            row["date"].isoformat(): _after_cost_long_short(
                row, row["_tree_scores"][mode], spread_bps=spread_bps,
                slippage_bps=slippage_bps, commission_per_share=commission_per_share,
                short_borrow_annual_pct=short_borrow_annual_pct,
            )
            for row in rows
        }
        paired_tree_dates = sorted(
            day for day in tree_return_by_date
            if tree_return_by_date[day] is not None and day in legacy_net_by_date
        )
        paired_tree_delta = [
            tree_return_by_date[day] - legacy_net_by_date[day]
            for day in paired_tree_dates
        ]
        model_id_payload = {
            "model_family": family,
            "logic_version": TREE_LOGIC_VERSIONS[mode],
            "factor_names": factor_names,
            "current_feature_importance": current_models[mode]["feature_importance"],
            "data_version": data_version,
            "fundamentals_data_version": fundamentals_data_version,
            "cost_model_version": COST_MODEL_VERSION,
            "model_parameters": current_models[mode]["model_mode"],
            "minimum_factor_coverage": current_models[mode]["minimum_factor_coverage"],
            "training_label_cutoff": current_models[mode]["training_label_cutoff"],
        }
        tree_model_comparisons[mode] = {
            "model_family": family,
            "logic_version": TREE_LOGIC_VERSIONS[mode],
            "candidate_id": hashlib.sha256(
                json.dumps(model_id_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            "gross_ic": _metric_summary(tree_gross_ics),
            "net_of_costs_ic": _metric_summary(tree_net_ics),
            "after_cost_long_short_return": _metric_summary(tree_returns),
            "after_cost_return_delta_vs_legacy_static": {
                "mean": round(statistics.mean(paired_tree_delta), 6) if paired_tree_delta else None,
                "paired_oos_sample_count": len(paired_tree_dates),
            },
            "feature_importance": current_models[mode]["feature_importance"],
            "evaluation_count": len(rows),
            "training_lookback_observations": FACTOR_TRAINING_SAMPLES,
        }
    regime_timed_rows = []
    for row in adaptive_observations:
        weights = _configured_regime_weights(factor_names, row["market_regime"])
        predictions = _scores(row, weights)
        regime_timed_rows.append({
            "scores": predictions,
            "gross_ic": information_coefficient(predictions, row["returns"]),
            "net_ic": information_coefficient(predictions, row["net_returns"]),
            "net_return": _after_cost_long_short(
                row, predictions, spread_bps=spread_bps,
                slippage_bps=slippage_bps, commission_per_share=commission_per_share,
                short_borrow_annual_pct=short_borrow_annual_pct,
            ),
        })
    regime_timed_gross_ic = [row["gross_ic"] for row in regime_timed_rows if row["gross_ic"] is not None]
    regime_timed_net_ic = [row["net_ic"] for row in regime_timed_rows if row["net_ic"] is not None]
    regime_timed_net_return = [row["net_return"] for row in regime_timed_rows if row["net_return"] is not None]

    weight_timeline = [
        {
            "effective_date": row["date"].isoformat(),
            "training_label_cutoff": row["_training_label_cutoff"],
            "training_observation_count": row["_training_observation_count"],
            "weights": row["_weights"],
            "equal_weight_fallback": row["_weight_fit_fallback"],
            "model_family": MODEL_FAMILY,
            "logic_version": LOGIC_VERSION,
        }
        for row in adaptive_observations
    ]
    weight_timeline.append({
        "effective_date": latest_date.isoformat(),
        "training_label_cutoff": max(row["label_end"] for row in current_train).isoformat(),
        "training_observation_count": len(current_train),
        "weights": current_weights,
        "equal_weight_fallback": current_fallback,
        "model_family": MODEL_FAMILY,
        "logic_version": LOGIC_VERSION,
        "current_fit": True,
    })
    ridge_timeline = list(weight_timeline)
    by_year = {}
    for year in sorted({row["date"].year for row in adaptive_observations}):
        year_rows = [row for row in adaptive_observations if row["date"].year == year]
        vals = [
            value for row in year_rows
            if (value := information_coefficient(row["_scores"], row["returns"])) is not None
        ]
        yearly = _metric_summary(vals)
        by_year[str(year)] = {
            "sample_count": yearly["sample_count"],
            "mean_spearman_ic": yearly["mean"],
            "ic_standard_deviation": yearly["standard_deviation"],
            "positive_sample_pct": yearly["positive_sample_pct"],
        }

    factor_observation_coverage = {
        factor: sum(factor in row["factor_names"] for row in observations)
        for factor in OPTIONAL_FACTORS + FUNDAMENTAL_FACTORS
    }
    factor_ic = {}
    for factor in factor_names:
        gross_values = []
        net_values = []
        net_returns = []
        for row in adaptive_observations:
            scores = {
                symbol: values[factor] for symbol, values in row["zscores"].items()
                if symbol in row["returns"] and factor in values
            }
            gross = information_coefficient(scores, row["returns"])
            net = information_coefficient(scores, row["net_returns"])
            if gross is not None:
                gross_values.append(gross)
            if net is not None:
                net_values.append(net)
            portfolio_return = _after_cost_long_short(
                row, scores, spread_bps=spread_bps,
                slippage_bps=slippage_bps, commission_per_share=commission_per_share,
                short_borrow_annual_pct=short_borrow_annual_pct,
            )
            if portfolio_return is not None:
                net_returns.append(portfolio_return)
        factor_ic[factor] = {
            "gross": _metric_summary(gross_values),
            "net_of_costs": _metric_summary(net_values),
            "single_factor_after_cost_long_short_return": _metric_summary(net_returns),
        }

    by_regime = {}
    for regime in sorted({row["market_regime"] for row in adaptive_observations}):
        rows = [row for row in adaptive_observations if row["market_regime"] == regime]
        gross_ics = [
            value for row in rows
            if (value := information_coefficient(row["_scores"], row["returns"])) is not None
        ]
        ics = [
            value for row in rows
            if (value := information_coefficient(row["_scores"], row["net_returns"])) is not None
        ]
        net = [row["_after_cost_return"] for row in rows if row["_after_cost_return"] is not None]
        by_regime[regime] = {
            "prequential_gross_ic": _metric_summary(gross_ics),
            "prequential_net_of_costs_ic": _metric_summary(ics),
            "after_cost_long_short_return": _metric_summary(net),
        }

    candidate_payload = {
        "model_family": MODEL_FAMILY, "logic_version": LOGIC_VERSION,
        "factor_names": factor_names, "weights": current_weights,
        "data_version": data_version,
        "sector_map": sector_by_symbol,
        "fundamentals_data_version": fundamentals_data_version,
        "cost_model_version": COST_MODEL_VERSION,
        "ridge_alpha": RIDGE_ALPHA, "min_universe": min_universe,
        "spread_bps": spread_bps, "slippage_bps": slippage_bps,
        "commission_per_share": commission_per_share,
        "short_borrow_annual_pct": short_borrow_annual_pct,
    }
    legacy_baseline_parameters = MultiFactorConfig.from_dict({
        "factor_weights": {
            "momentum_1m": 0.30, "momentum_3m": 0.30,
            "momentum_6m": 0.20, "low_volatility": 0.20,
        },
        "factor_neutralization": "none",
        "regime_factor_weights": {},
        "portfolio_weight_method": "equal",
    }).to_dict()
    parent_candidate_id = archive_candidate_id("swing_multi_factor", legacy_baseline_parameters)
    candidate_payload["parent_candidate_id"] = parent_candidate_id
    candidate_id = hashlib.sha256(
        json.dumps(candidate_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    replay_model_timeline = [*ridge_timeline]
    for mode, snapshots in tree_timelines.items():
        # Make the latest fit available to subsequent as-of dates. Its
        # effective_date remains the actual fit date, so an earlier replay
        # session cannot look ahead and use this model.
        snapshots.append(dict(current_models[mode]))
        replay_model_timeline.extend(snapshots)
    tree_model_comparisons["ridge_walk_forward"] = {
        "model_family": MODEL_FAMILY,
        "logic_version": LOGIC_VERSION,
        "candidate_id": candidate_id,
        "gross_ic": adaptive_summary,
        "net_of_costs_ic": adaptive_net_ic,
        "after_cost_long_short_return": _metric_summary(adaptive_net),
        "after_cost_return_delta_vs_legacy_static": {
            "mean": round(statistics.mean(paired_return_delta), 6) if paired_return_delta else None,
            "paired_oos_sample_count": len(paired_dates),
        },
        "feature_importance": current_weights,
        "evaluation_count": len(adaptive_observations),
        "training_lookback_observations": FACTOR_TRAINING_SAMPLES,
    }
    tree_model_comparisons["legacy_static"] = {
        "model_family": "static_linear_composite",
        "gross_ic": adaptive_legacy_static,
        "net_of_costs_ic": legacy_static_net_ic,
        "after_cost_long_short_return": _metric_summary(legacy_static_net),
        "evaluation_count": len(adaptive_observations),
    }
    tree_model_comparisons["equal_weight"] = {
        "model_family": "equal_weight_linear_composite",
        "gross_ic": adaptive_equal,
        "net_of_costs_ic": equal_net_ic,
        "after_cost_long_short_return": _metric_summary(equal_net),
        "evaluation_count": len(adaptive_observations),
    }

    return {
        "method": "rolling_recent_purged_multi_model_cross_sectional_21_session_comparison",
        "model_family": MODEL_FAMILY,
        "logic_version": LOGIC_VERSION,
        "primary_model_mode": "ridge_walk_forward",
        "candidate_id": candidate_id,
        "parent_candidate": "multi_factor_equal_weight_baseline_v1",
        "parent_candidate_id": parent_candidate_id,
        "data_version": data_version,
        "cost_model_version": COST_MODEL_VERSION,
        "factor_names": list(factor_names),
        "missing_factor_value_policy": "neutral_zero_cross_sectional_zscore_for_missing_usable_factors",
        "factor_observation_coverage": factor_observation_coverage,
        "fundamental_data_status": (fundamentals_diagnostics or {}).get(
            "status", "supplied" if fundamentals_by_symbol else "unavailable",
        ),
        "fundamental_data_version": fundamentals_data_version,
        "fundamental_feed": fundamentals_diagnostics or {
            "status": "supplied" if fundamentals_by_symbol else "unavailable",
            "source": "caller_supplied_point_in_time_rows",
        },
        "fundamental_factors_available": [
            name for name in FUNDAMENTAL_FACTORS if name in factor_names
        ],
        "feature_importance": dict(sorted(current_weights.items(), key=lambda item: (-item[1], item[0]))),
        "model_comparisons": tree_model_comparisons,
        "current_models": current_models,
        "model_comparison_protocol": {
            "oos_predictions": "all model families use the same cross-sections and 21-session forward labels",
            "purge_rule": "the training labels must mature strictly before each evaluation feature date",
            "training_window_observations": FACTOR_TRAINING_SAMPLES,
            "training_target": "cross-sectionally centered and scaled 21-session returns net of estimated trading costs",
            "tree_regularization": "31 estimators, depth 2, shrinkage, row/column subsampling, L1/L2, one CPU thread",
        },
        "factor_oos_ic": factor_ic,
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
        "latest_factor_observation_date": observations[-1]["date"].isoformat(),
        "latest_mature_training_label_cutoff": max(
            row["label_end"] for row in current_train
        ).isoformat(),
        "current_training_label_age_days": (
            latest_date - max(row["label_end"] for row in current_train)
        ).days,
        "rolling_evaluation_count": len(adaptive_observations),
        "current_training_sample_count": len(current_train),
        "adaptive_prequential_ic": adaptive_summary,
        "adaptive_equal_weight_ic": adaptive_equal,
        "adaptive_legacy_static_baseline_ic": adaptive_legacy_static,
        "adaptive_prequential_net_of_costs_ic": adaptive_net_ic,
        "adaptive_equal_weight_net_of_costs_ic": equal_net_ic,
        "adaptive_legacy_static_baseline_net_of_costs_ic": legacy_static_net_ic,
        "adaptive_prequential_ic_by_year": by_year,
        "adaptive_prequential_ic_by_regime": by_regime,
        "adaptive_after_cost_long_short_return": _metric_summary(adaptive_net),
        "equal_weight_after_cost_long_short_return": _metric_summary(equal_net),
        "legacy_static_baseline_after_cost_long_short_return": _metric_summary(legacy_static_net),
        "learned_vs_legacy_static": {
            "mean_net_ic_delta": round(
                float(adaptive_net_ic["mean"] or 0.0) - float(legacy_static_net_ic["mean"] or 0.0), 6,
            ),
            "mean_after_cost_return_delta": round(statistics.mean(paired_return_delta), 6)
            if paired_return_delta else None,
            "paired_oos_sample_count": len(paired_dates),
        },
        "configured_regime_timed_baseline": {
            "gross_ic": _metric_summary(regime_timed_gross_ic),
            "net_of_costs_ic": _metric_summary(regime_timed_net_ic),
            "after_cost_long_short_return": _metric_summary(regime_timed_net_return),
            "weight_profiles": REGIME_FACTOR_WEIGHT_PROFILES,
        },
        "transaction_cost_model": {
            "spread_bps_round_trip_reference": spread_bps,
            "slippage_bps_each_side": slippage_bps,
            "commission_per_share_each_side": commission_per_share,
            "annual_short_borrow_pct": short_borrow_annual_pct,
            "costs_applied_to": "top/bottom decile, equal-weight, 21-session shadow portfolio",
        },
        "neutralization": {
            "method": "within-sector demeaning before cross-sectional z-score",
            "applied": bool(sector_by_symbol),
            "sector_coverage": len(sector_by_symbol or {}),
            "single_name_sector_policy": "left unchanged because cross-sectional neutralization cannot be estimated from one name",
            "sector_metadata_limitation": "Current fixed-universe sector labels are reused historically; point-in-time sector membership is unavailable.",
        },
        "tradability_mask": {
            "minimum_price": 5.0,
            "minimum_average_dollar_volume": 500_000,
            "average_dollar_volume_mask_applied_when_full_volume_history_is_available": True,
        },
        "current_candidate_weights": current_weights,
        "weight_timeline": replay_model_timeline,
        "model_snapshot_count_by_family": {
            "ridge_cross_sectional": len(ridge_timeline),
            **{family: len(tree_timelines[mode]) for mode, family in TREE_MODEL_FAMILIES.items()},
        },
        "current_fit_equal_weight_fallback": current_fallback,
        "current_training_label_cutoff": max(row["label_end"] for row in current_train).isoformat(),
        "purge_rule": "Every 21-session forward label must end strictly before the feature date used for prediction.",
        "universe_limitation": (
            "The current fixed Algo universe is replayed across the 2020-onward evaluation period; "
            "historical membership and listing eligibility are not point-in-time, "
            "so results have survivorship and availability bias."
        ),
    }


__all__ = ["FACTORS", "train_factor_model"]
