"""Causal market-regime classification and strategy routing."""

from __future__ import annotations

import math
from threading import RLock

from src.algo.strategies.indicators import closes, moving_average
from src.algo.strategies.regime_features import (
    FEATURE_SETS, MODEL_CORE_FEATURES, MODEL_EXTENDED_FEATURES,
    compute_regime_feature_history,
)
from src.algo.strategies.regime_models import infer_regime_model


REGIME_ENGINE_VERSION = "2"
_FEATURE_CACHE_LOCK = RLock()
_FEATURE_CACHE_KEY = None
_FEATURE_CACHE_VALUE: list[dict] | None = None


def _bars_signature(bars: list[dict]) -> tuple[tuple, ...]:
    return tuple(
        (
            str(bar.get("date", bar.get("t", "")))[:10],
            bar.get("o"), bar.get("h"), bar.get("l"), bar.get("c"), bar.get("v"),
        )
        for bar in bars if isinstance(bar, dict)
    )


def _cached_feature_history(
    spy_daily_bars: list[dict], context_bars: dict[str, list[dict]] | None,
    macro_series: dict | None,
) -> list[dict]:
    """Reuse completed-daily features across the live service's 15s scans."""
    key = (
        _bars_signature(spy_daily_bars),
        tuple((name, _bars_signature(rows)) for name, rows in sorted((context_bars or {}).items())),
        tuple(
            (name, tuple(sorted(rows.items())) if isinstance(rows, dict) else tuple(
                sorted((str(row.get("date", "")), row.get("value")) for row in rows if isinstance(row, dict))
            ))
            for name, rows in sorted((macro_series or {}).items())
        ),
    )
    global _FEATURE_CACHE_KEY, _FEATURE_CACHE_VALUE
    with _FEATURE_CACHE_LOCK:
        if key != _FEATURE_CACHE_KEY or _FEATURE_CACHE_VALUE is None:
            _FEATURE_CACHE_VALUE = compute_regime_feature_history(
                spy_daily_bars, context_bars=context_bars, macro_series=macro_series,
            )
            _FEATURE_CACHE_KEY = key
        return _FEATURE_CACHE_VALUE


def _legacy_rank(
    spy_daily_bars: list[dict], mode: str, *, event_enabled: bool,
    event_disable_in_volatile: bool, intraday_trend_enabled: bool,
    intraday_momentum_enabled: bool,
) -> dict:
    """Retain the existing selector for baseline comparisons and migration."""
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
        weights = {"trend_following": 1.0, "momentum": 0.9, "multi_factor": 0.8, "event_driven": 0.58, "mean_reversion": 0.25, "swing_mean_reversion": 0.15}
        reason = f"SPY above 20-day average with {return_20d:.1%} 20-day return"
    elif sma20 and values[-1] < sma20 and return_20d <= -0.01:
        regime = "trending_down"
        weights = {"trend_following": 0.72, "momentum": 0.65, "multi_factor": 0.62, "event_driven": 0.56, "mean_reversion": 0.58, "swing_mean_reversion": 0.15}
        reason = f"SPY below 20-day average with {return_20d:.1%} 20-day return"
    else:
        regime = "range_bound"
        weights = {"mean_reversion": 1.0, "swing_mean_reversion": 0.78, "multi_factor": 0.65, "event_driven": 0.58, "trend_following": 0.4, "momentum": 0.4}
        reason = f"SPY 20-day return {return_20d:.1%}; no directional trend"

    available = {"trend_following", "momentum", "mean_reversion"}
    if mode == "algo":
        available.update({"multi_factor", "swing_mean_reversion"})
        if event_enabled:
            available.add("event_driven")
        if intraday_trend_enabled:
            weights["intraday_trend"] = 0.3 if regime == "range_bound" else 0.58
            available.add("intraday_trend")
        if intraday_momentum_enabled:
            weights["intraday_momentum"] = 0.3 if regime == "range_bound" else 0.56
            available.add("intraday_momentum")
    ranked = sorted(weights.items(), key=lambda item: item[1], reverse=True)
    active = [
        name for name, score in ranked if name in available
        and (regime != "volatile" or name == "event_driven")
        and (name != "event_driven" or score >= 0.55)
    ]
    return {
        "regime": regime, "reason": reason,
        "spy_return_20d": return_20d,
        "spy_volatility_20d": volatility,
        "ranked": [
            {"strategy": name, "score": score, "available": name in available}
            for name, score in ranked
        ],
        "active": active, "satellites": [],
    }


def _selected_model(
    features: list[dict], *, engine: str, feature_set: str, training_window: int = 756,
) -> dict:
    keys = MODEL_EXTENDED_FEATURES if feature_set in {"macro", "marketwide"} else MODEL_CORE_FEATURES
    return infer_regime_model(
        features, engine=engine, feature_names=tuple(keys), training_window=training_window,
    )


def _raw_rule_states(feature_rows: list[dict], feature_set: str) -> tuple[list[str], list[str]]:
    """Create raw daily regimes and risk states with enter/exit hysteresis."""
    states: list[str] = []
    volatility_states: list[str] = []
    high_vol_previous = False
    for row in feature_rows:
        vol_features = [row.get("realized_vol_percentile_252d")]
        if feature_set in {"technical", "macro", "marketwide"}:
            vol_features.append(row.get("atr_percentile_252d"))
        if feature_set in {"macro", "marketwide"}:
            vol_features.extend((
                row.get("vix_percentile_252d"),
                row.get("high_yield_spread_percentile_252d"),
            ))
            if (
                row.get("vix_term_slope") is not None
                and row.get("vix_percentile_252d") is not None
                and row["vix_term_slope"] >= 0.05
                and row["vix_percentile_252d"] >= 0.80
            ):
                vol_features.append(0.95)
        vol_values = [float(value) for value in vol_features if isinstance(value, (int, float)) and math.isfinite(value)]
        stress = max(vol_values) if vol_values else None
        if feature_set == "marketwide":
            breadth = row.get("breadth_positive_20d")
            correlation_pct = row.get("average_pairwise_corr_percentile_252d")
            if breadth is not None and correlation_pct is not None and breadth <= 0.30 and correlation_pct >= 0.90:
                stress = max(stress or 0.0, 0.95)
        if high_vol_previous:
            high_vol = stress is not None and stress >= 0.80
        else:
            high_vol = stress is not None and stress >= 0.95
        high_vol_previous = high_vol
        vol_state = "extreme" if high_vol else (
            "elevated" if stress is not None and stress >= 0.80 else
            "calm" if stress is not None else "unknown"
        )
        volatility_states.append(vol_state)

        efficiency = row.get("efficiency_ratio_20d")
        adx = row.get("adx_14") if feature_set in {"technical", "macro", "marketwide"} else None
        if high_vol:
            candidate = "volatile"
        elif efficiency is None:
            candidate = "unknown"
        elif efficiency >= 0.45 and (adx is None or adx >= 18.0):
            candidate = "trending_up" if float(row.get("return_20d") or 0.0) >= 0 else "trending_down"
        elif efficiency <= 0.30 and (adx is None or adx <= 25.0):
            candidate = "range_bound"
        else:
            candidate = "ambiguous"
        states.append(candidate)
    return states, volatility_states


def _apply_hysteresis(raw_states: list[str]) -> list[str]:
    output: list[str] = []
    current: str | None = None
    pending: str | None = None
    pending_count = 0
    for candidate in raw_states:
        if candidate in {"unknown", "volatile"}:
            current = candidate if candidate == "volatile" or current is None else current
            if candidate == "volatile":
                pending, pending_count = None, 0
            output.append(current or "unknown")
            continue
        if current in {None, "unknown"}:
            current = candidate
            pending, pending_count = None, 0
        elif candidate == current:
            pending, pending_count = None, 0
        elif candidate == pending:
            pending_count += 1
            if pending_count >= 2:
                current = candidate
                pending, pending_count = None, 0
        else:
            pending, pending_count = candidate, 1
        output.append(current)
    return output


def _strategy_weights(
    regime: str, *, strategy_policy: str, event_enabled: bool,
    event_disable_in_volatile: bool,
) -> dict[str, float]:
    trend = {
        "trend_following": 1.0, "momentum": 0.9, "multi_factor": 0.75,
        "event_driven": 0.58,
    }
    if strategy_policy == "legacy":
        return {}
    if regime in {"trending_up", "trending_down"}:
        return trend
    if regime == "range_bound" and strategy_policy == "structural":
        return {"mean_reversion": 1.0, "swing_mean_reversion": 0.9}
    if regime == "volatile":
        if strategy_policy == "high_vol_mean_reversion":
            return {"mean_reversion": 1.0, "swing_mean_reversion": 0.9}
        if event_enabled and not event_disable_in_volatile:
            return {"event_driven": 0.56}
    return {}


def _available_strategies(
    weights: dict[str, float], mode: str, *, event_enabled: bool,
    intraday_trend_enabled: bool, intraday_momentum_enabled: bool,
) -> set[str]:
    available = {"trend_following", "momentum", "mean_reversion"}
    if mode == "algo":
        available.update({"multi_factor", "swing_mean_reversion"})
        if event_enabled:
            available.add("event_driven")
        if intraday_trend_enabled:
            available.add("intraday_trend")
        if intraday_momentum_enabled:
            available.add("intraday_momentum")
    return available


def rank_strategies(
    spy_daily_bars: list[dict], mode: str = "swing", *,
    event_enabled: bool = False, event_disable_in_volatile: bool = True,
    intraday_trend_enabled: bool = False, intraday_momentum_enabled: bool = False,
    feature_set: str = "legacy", engine: str = "legacy",
    strategy_policy: str = "legacy", context_bars: dict[str, list[dict]] | None = None,
    macro_series: dict | None = None, include_models: bool = False,
    model_training_window: int = 756, model_min_confidence: float = 0.50,
    precomputed_features: list[dict] | None = None,
) -> dict:
    """Rank eligible strategies using the legacy rule, composite rules, HMM, or GMM.

    Engine `legacy` preserves the original behavior. New engines use completed
    bars only; HMM/GMM are explicit models that never silently replace rules.
    """
    if feature_set not in {"legacy", *FEATURE_SETS}:
        raise ValueError(f"Unknown regime feature set: {feature_set}")
    if engine not in {"legacy", "rule", "hmm", "gmm"}:
        raise ValueError(f"Unknown regime engine: {engine}")
    if strategy_policy not in {"legacy", "structural", "high_vol_mean_reversion"}:
        raise ValueError(f"Unknown regime strategy policy: {strategy_policy}")
    if (
        isinstance(model_min_confidence, bool)
        or not isinstance(model_min_confidence, (int, float))
        or not math.isfinite(model_min_confidence)
        or not 0 <= model_min_confidence <= 1
    ):
        raise ValueError("model_min_confidence must be a finite fraction in [0, 1]")
    if engine == "legacy" and feature_set == "legacy" and strategy_policy == "legacy" and not include_models:
        return _legacy_rank(
            spy_daily_bars, mode, event_enabled=event_enabled,
            event_disable_in_volatile=event_disable_in_volatile,
            intraday_trend_enabled=intraday_trend_enabled,
            intraday_momentum_enabled=intraday_momentum_enabled,
        )

    features = precomputed_features if precomputed_features is not None else _cached_feature_history(
        spy_daily_bars, context_bars, macro_series,
    )
    values = closes(spy_daily_bars)
    if len(values) < 22 or not features:
        return {"regime": "unknown", "reason": "insufficient completed daily bars", "ranked": [], "active": []}

    legacy = _legacy_rank(
        spy_daily_bars, mode, event_enabled=event_enabled,
        event_disable_in_volatile=event_disable_in_volatile,
        intraday_trend_enabled=intraday_trend_enabled,
        intraday_momentum_enabled=intraday_momentum_enabled,
    )
    model_shadow = {}
    if include_models or engine in {"hmm", "gmm"}:
        model_shadow = {
            model_name: _selected_model(
                features, engine=model_name, feature_set=feature_set,
                training_window=model_training_window,
            )
            for model_name in ("hmm", "gmm")
        }

    if engine == "legacy":
        selected = legacy["regime"]
        volatility_state = "extreme" if selected == "volatile" else "unknown"
        smoothed = [selected] * len(features)
        raw_states = smoothed
        reason = legacy["reason"]
        use_legacy_weights = True
    elif engine in {"hmm", "gmm"}:
        selected_model = model_shadow.get(engine) or _selected_model(
            features, engine=engine, feature_set=feature_set,
            training_window=model_training_window,
        )
        if (
            selected_model.get("status") != "ok"
            or float(selected_model.get("confidence", 0.0)) < model_min_confidence
        ):
            selected = "ambiguous"
            reason = f"{engine.upper()} posterior is unavailable or diffuse"
        else:
            selected = str(selected_model["regime"])
            reason = f"{engine.upper()} rolling posterior selected {selected}"
        raw_states = [selected]
        smoothed = [selected]
        volatility_state = "extreme" if selected == "volatile" else "unknown"
        use_legacy_weights = False
    else:
        raw_states, volatility_states = _raw_rule_states(features, feature_set)
        smoothed = _apply_hysteresis(raw_states)
        selected = smoothed[-1]
        volatility_state = volatility_states[-1]
        current = features[-1]
        reason = (
            f"ER20={current.get('efficiency_ratio_20d')}; "
            f"RV20 percentile={current.get('realized_vol_percentile_252d')}; "
            f"volatility state={volatility_state}; feature set={feature_set}"
        )
        use_legacy_weights = False

    if use_legacy_weights:
        weights = {
            item["strategy"]: float(item["score"])
            for item in legacy.get("ranked", [])
        }
    elif strategy_policy == "legacy":
        weights = _strategy_weights(
            selected, strategy_policy="structural",
            event_enabled=event_enabled,
            event_disable_in_volatile=event_disable_in_volatile,
        )
    else:
        weights = _strategy_weights(
            selected, strategy_policy=strategy_policy,
            event_enabled=event_enabled,
            event_disable_in_volatile=event_disable_in_volatile,
        )

    available = _available_strategies(
        weights, mode, event_enabled=event_enabled,
        intraday_trend_enabled=intraday_trend_enabled,
        intraday_momentum_enabled=intraday_momentum_enabled,
    )
    if mode == "algo" and not use_legacy_weights:
        if intraday_trend_enabled and selected in {"trending_up", "trending_down"}:
            weights["intraday_trend"] = 0.60
        if intraday_momentum_enabled and selected in {"trending_up", "trending_down"}:
            weights["intraday_momentum"] = 0.55
        available = _available_strategies(
            weights, mode, event_enabled=event_enabled,
            intraday_trend_enabled=intraday_trend_enabled,
            intraday_momentum_enabled=intraday_momentum_enabled,
        )
    ranked = sorted(weights.items(), key=lambda item: item[1], reverse=True)
    active = [name for name, score in ranked if name in available and (name != "event_driven" or score >= 0.55)]

    risk_scale = 1.0
    if volatility_state == "elevated":
        risk_scale = 0.75
    elif volatility_state == "extreme" or selected == "volatile":
        risk_scale = 0.50
    elif selected == "ambiguous":
        risk_scale = 0.50
    policy_scale = 0.25 if strategy_policy == "high_vol_mean_reversion" and selected == "volatile" else 1.0
    risk_multipliers = {
        name: round(min(1.5, risk_scale * policy_scale * float(weights.get(name, 1.0))), 4)
        for name in active
    }

    rank_items = [
        {"strategy": name, "score": score, "available": name in available}
        for name, score in ranked
    ]
    output = {
        "regime": selected,
        "regime_engine_version": REGIME_ENGINE_VERSION,
        "regime_engine": engine,
        "regime_feature_set": feature_set,
        "strategy_policy": strategy_policy,
        "reason": reason,
        "spy_return_20d": features[-1].get("return_20d"),
        "spy_volatility_20d": features[-1].get("realized_vol_20d"),
        "volatility_state": volatility_state,
        "regime_features": features[-1],
        "regime_shadow": model_shadow,
        "ranked": rank_items,
        "active": active,
        "satellites": [],
        "strategy_risk_multipliers": risk_multipliers,
        "risk_budget_multiplier": risk_scale,
    }
    if engine == "rule":
        output["regime_history"] = {
            "raw_regime": raw_states[-1],
            "smoothed_regime": smoothed[-1],
            "confirmation_days": 2,
        }
    return output


__all__ = ["REGIME_ENGINE_VERSION", "rank_strategies"]
