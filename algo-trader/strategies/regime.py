"""Causal market-regime classification and strategy routing."""

from __future__ import annotations

import hashlib
import math
import os
import pickle
from threading import RLock

from strategies.indicators import closes, moving_average
from strategies.regime_features import (
    FEATURE_SETS, MODEL_CORE_FEATURES, MODEL_EXTENDED_FEATURES,
    compute_regime_feature_history,
)
from runtime.paths import CACHE_DIR
from strategies.regime_models import infer_regime_model


REGIME_ENGINE_VERSION = "3"
# The rule detector anchors the initial committee while the two model votes can
# collectively overrule it; walk-forward evidence should guide future weights.
REGIME_LABELS = (
    "trending_up", "trending_down", "range_bound", "volatile", "ambiguous", "unknown",
)
COMMITTEE_VOTER_WEIGHTS = {"rule": 0.40, "hmm": 0.30, "gmm": 0.30}
_FEATURE_CACHE_LOCK = RLock()
# Bump when the feature computation changes; old disk entries are then ignored.
_FEATURE_CACHE_VERSION = "regime-features-v1"
_FEATURE_DISK_CACHE = CACHE_DIR / "regime-features"
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
            _FEATURE_CACHE_VALUE = _load_or_compute_features(
                key, spy_daily_bars, context_bars, macro_series,
            )
            _FEATURE_CACHE_KEY = key
        return _FEATURE_CACHE_VALUE


def _load_or_compute_features(key, spy_daily_bars, context_bars, macro_series) -> list[dict]:
    """Share one computed history across processes through the disk cache.

    Research replays run in fresh worker processes, which otherwise each recompute
    the same history for the same bars.
    """
    digest = hashlib.sha256(f"{_FEATURE_CACHE_VERSION}|{key!r}".encode("utf-8")).hexdigest()
    path = _FEATURE_DISK_CACHE / f"{digest}.pkl"
    try:
        with path.open("rb") as handle:
            return pickle.load(handle)
    except (OSError, pickle.UnpicklingError, EOFError):
        pass
    value = compute_regime_feature_history(spy_daily_bars, context_bars=context_bars, macro_series=macro_series)
    try:
        _FEATURE_DISK_CACHE.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(f".{os.getpid()}.tmp")
        with temp.open("wb") as handle:
            pickle.dump(value, handle, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(temp, path)
    except OSError:
        pass
    return value


def _legacy_rank(
    spy_daily_bars: list[dict], mode: str, *, llm_enabled: bool,
    llm_disable_in_volatile: bool, intraday_trend_enabled: bool,
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
        weights = {"swing_llm_driven": 0.56} if llm_enabled and not llm_disable_in_volatile else {}
        reason = f"20-day daily volatility {volatility:.1%} exceeds 2.5%"
    elif sma20 and values[-1] > sma20 and return_20d >= 0.01:
        regime = "trending_up"
        weights = {"swing_trend_following": 1.0, "swing_momentum": 0.9, "swing_multi_factor": 0.8, "swing_llm_driven": 0.58, "intraday_mean_reversion": 0.25, "swing_mean_reversion": 0.15}
        reason = f"SPY above 20-day average with {return_20d:.1%} 20-day return"
    elif sma20 and values[-1] < sma20 and return_20d <= -0.01:
        regime = "trending_down"
        weights = {"swing_trend_following": 0.72, "swing_momentum": 0.65, "swing_multi_factor": 0.62, "swing_llm_driven": 0.56, "intraday_mean_reversion": 0.58, "swing_mean_reversion": 0.15}
        reason = f"SPY below 20-day average with {return_20d:.1%} 20-day return"
    else:
        regime = "range_bound"
        weights = {"intraday_mean_reversion": 1.0, "swing_mean_reversion": 0.78, "swing_multi_factor": 0.65, "swing_llm_driven": 0.58, "swing_trend_following": 0.4, "swing_momentum": 0.4}
        reason = f"SPY 20-day return {return_20d:.1%}; no directional trend"

    available = {"swing_trend_following", "swing_momentum", "intraday_mean_reversion"}
    if mode == "algo":
        available.update({"swing_multi_factor", "swing_mean_reversion"})
        if llm_enabled:
            available.add("swing_llm_driven")
        if intraday_trend_enabled:
            weights["intraday_trend_following"] = 0.3 if regime == "range_bound" else 0.58
            available.add("intraday_trend_following")
        if intraday_momentum_enabled:
            weights["intraday_momentum"] = 0.3 if regime == "range_bound" else 0.56
            available.add("intraday_momentum")
    ranked = sorted(weights.items(), key=lambda item: item[1], reverse=True)
    active = [
        name for name, score in ranked if name in available
        and (regime != "volatile" or name == "swing_llm_driven")
        and (name != "swing_llm_driven" or score >= 0.55)
    ]
    return {
        "regime": regime, "reason": reason,
        "spy_return_20d": return_20d,
        "spy_volatility_20d": volatility,
        "ranked": [
            {"strategy": name, "score": score, "available": name in available}
            for name, score in ranked
        ],
        "active": active,
    }


def _selected_model(
    features: list[dict], *, engine: str, feature_set: str, training_window: int = 756,
) -> dict:
    keys = MODEL_EXTENDED_FEATURES if feature_set in {"macro", "marketwide"} else MODEL_CORE_FEATURES
    return infer_regime_model(
        features, engine=engine, feature_names=tuple(keys), training_window=training_window,
    )


def _committee_consensus(
    *, raw_rule_regime: str, smoothed_rule_regime: str,
    model_results: dict[str, dict], model_min_confidence: float,
) -> dict:
    """Combine the deterministic rule vote with available HMM/GMM posteriors."""
    rule_state = smoothed_rule_regime if smoothed_rule_regime in REGIME_LABELS else "unknown"
    raw_state = raw_rule_regime if raw_rule_regime in REGIME_LABELS else "unknown"
    rule_transition = raw_state != rule_state
    rule_probabilities = {rule_state: 1.0}
    if rule_transition:
        rule_probabilities = {rule_state: 0.65, raw_state: 0.35}

    voters = [{
        "name": "rule",
        "weight": COMMITTEE_VOTER_WEIGHTS["rule"],
        "regime": rule_state,
        "probabilities": rule_probabilities,
        "confidence": max(rule_probabilities.values()),
        "converged": True,
    }]
    model_details = {}
    for name in ("hmm", "gmm"):
        result = model_results.get(name, {})
        raw_probabilities = result.get("probabilities")
        probabilities = {}
        if result.get("status") == "ok" and isinstance(raw_probabilities, dict):
            for regime, probability in raw_probabilities.items():
                if regime not in REGIME_LABELS:
                    continue
                if isinstance(probability, bool) or not isinstance(probability, (int, float)):
                    continue
                if not math.isfinite(probability) or probability < 0:
                    continue
                probabilities[regime] = float(probability)
        total_probability = sum(probabilities.values())
        if total_probability > 0:
            probabilities = {
                regime: probability / total_probability
                for regime, probability in probabilities.items()
            }
            model_regime = max(
                probabilities,
                key=lambda regime: (probabilities[regime], regime == result.get("regime")),
            )
            model_confidence = probabilities[model_regime]
            voters.append({
                "name": name,
                "weight": COMMITTEE_VOTER_WEIGHTS[name],
                "regime": model_regime,
                "probabilities": probabilities,
                "confidence": model_confidence,
                "converged": bool(result.get("converged", True)),
            })
        else:
            model_regime = result.get("regime")
            model_confidence = result.get("confidence")
        model_details[name] = {
            "status": result.get("status", "unavailable"),
            "regime": model_regime,
            "confidence": model_confidence,
            "converged": result.get("converged"),
            "used": total_probability > 0,
        }

    total_weight = sum(voter["weight"] for voter in voters)
    distribution = {
        regime: sum(
            voter["weight"] * voter["probabilities"].get(regime, 0.0)
            for voter in voters
        ) / total_weight
        for regime in REGIME_LABELS
    }
    distribution = {regime: value for regime, value in distribution.items() if value > 0}
    leaders = [
        regime for regime, value in distribution.items()
        if math.isclose(value, max(distribution.values()), rel_tol=0.0, abs_tol=1e-12)
    ]
    selected = rule_state if rule_state in leaders else sorted(leaders)[0]
    confidence = distribution[selected]
    agreement_weight = sum(
        voter["weight"] for voter in voters if voter["regime"] == selected
    )
    agreement = agreement_weight / total_weight
    complete = len(voters) == len(COMMITTEE_VOTER_WEIGHTS)
    models_confident = all(
        voter["confidence"] >= model_min_confidence
        for voter in voters if voter["name"] in {"hmm", "gmm"}
    )
    models_converged = all(voter["converged"] for voter in voters)

    if selected in {"ambiguous", "unknown"}:
        uncertainty_multiplier = 0.25
    elif confidence >= 0.75 and agreement >= 0.90 and models_confident and models_converged:
        uncertainty_multiplier = 1.0
    elif confidence >= 0.55 and agreement >= 0.66:
        uncertainty_multiplier = 0.70
    else:
        uncertainty_multiplier = 0.40
    if not complete:
        uncertainty_multiplier = min(uncertainty_multiplier, 0.60)
    if rule_transition:
        uncertainty_multiplier = min(uncertainty_multiplier, 0.60)
    if not models_confident or not models_converged:
        uncertainty_multiplier = min(uncertainty_multiplier, 0.70)

    detector_details = {
        "rule": {
            "status": "ok",
            "regime": rule_state,
            "raw_regime": raw_state,
            "confidence": max(rule_probabilities.values()),
            "transition_pending": rule_transition,
            "used": True,
        },
        **model_details,
    }
    return {
        "regime": selected,
        "consensus_score": round(confidence, 6),
        "agreement": round(agreement, 6),
        "complete": complete,
        "uncertainty_risk_multiplier": round(uncertainty_multiplier, 4),
        "applied_weights": {
            voter["name"]: round(voter["weight"] / total_weight, 6)
            for voter in voters
        },
        "weighted_support": {
            regime: round(value, 6)
            for regime, value in sorted(distribution.items())
        },
        "detectors": detector_details,
    }


def _raw_rule_states(feature_rows: list[dict], feature_set: str) -> tuple[list[str], list[str]]:
    """Create raw daily regimes and risk states with enter/exit hysteresis."""
    states: list[str] = []
    volatility_states: list[str] = []
    high_vol_previous = False
    for row in feature_rows:
        vol_features = [row.get("realized_vol_percentile_252d")]
        if feature_set in {"technical", "macro", "marketwide"}:
            vol_features.append(row.get("atr_percentile_252d"))
        credit_percentile = None
        if feature_set in {"macro", "marketwide"}:
            vol_features.append(row.get("vix_percentile_252d"))
            credit_percentile = row.get("high_yield_spread_percentile_252d")
            if (
                row.get("vix_term_slope") is not None
                and row.get("vix_percentile_252d") is not None
                and row["vix_term_slope"] >= 0.05
                and row["vix_percentile_252d"] >= 0.80
            ):
                vol_features.append(0.95)
        vol_values = [float(value) for value in vol_features if isinstance(value, (int, float)) and math.isfinite(value)]
        # A wide high-yield spread counts only alongside elevated volatility. On its own it is not volatility stress.
        if credit_percentile is not None and math.isfinite(credit_percentile) and vol_values and max(vol_values) >= 0.80:
            vol_values.append(float(credit_percentile))
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
    regime: str, *, strategy_policy: str, llm_enabled: bool,
    llm_disable_in_volatile: bool,
) -> dict[str, float]:
    trend = {
        "swing_trend_following": 1.0, "swing_momentum": 0.9, "swing_multi_factor": 0.75,
        "swing_llm_driven": 0.58,
    }
    if strategy_policy == "legacy":
        return {}
    if regime in {"trending_up", "trending_down"}:
        return trend
    if regime == "range_bound" and strategy_policy == "structural":
        return {"intraday_mean_reversion": 1.0, "swing_mean_reversion": 0.9}
    if regime == "volatile":
        if strategy_policy == "high_vol_mean_reversion":
            return {"intraday_mean_reversion": 1.0, "swing_mean_reversion": 0.9}
        if llm_enabled and not llm_disable_in_volatile:
            return {"swing_llm_driven": 0.56}
    return {}


def _available_strategies(
    weights: dict[str, float], mode: str, *, llm_enabled: bool,
    intraday_trend_enabled: bool, intraday_momentum_enabled: bool,
) -> set[str]:
    available = {"swing_trend_following", "swing_momentum", "intraday_mean_reversion"}
    if mode == "algo":
        available.update({"swing_multi_factor", "swing_mean_reversion"})
        if llm_enabled:
            available.add("swing_llm_driven")
        if intraday_trend_enabled:
            available.add("intraday_trend_following")
        if intraday_momentum_enabled:
            available.add("intraday_momentum")
    return available


def _risk_scale_value(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 1.5:
        raise ValueError(f"{name} risk_scale must be a finite number in (0, 1.5]")
    return float(value)


def rank_strategies(
    spy_daily_bars: list[dict], mode: str = "algo", *,
    llm_enabled: bool = False, llm_disable_in_volatile: bool = True,
    intraday_trend_enabled: bool = False, intraday_momentum_enabled: bool = False,
    feature_set: str = "legacy", engine: str = "legacy",
    strategy_policy: str = "legacy", context_bars: dict[str, list[dict]] | None = None,
    macro_series: dict | None = None, include_models: bool = False,
    model_training_window: int = 756, model_min_confidence: float = 0.50,
    strategy_regime_weights: dict[str, dict[str, float]] | None = None,
    strategy_enabled: dict[str, bool] | None = None,
    strategy_risk_scales: dict[str, float] | None = None,
    volatile_policy_risk_multiplier: float = 0.25,
    volatility_risk_scales: dict[str, float] | None = None,
    precomputed_features: list[dict] | None = None,
) -> dict:
    """Rank eligible strategies using the legacy rule, composite rules, HMM, or GMM.

    Engine `legacy` preserves the original behavior. New engines use completed
    bars only; HMM/GMM are explicit models that never silently replace rules.
    """
    if feature_set not in {"legacy", *FEATURE_SETS}:
        raise ValueError(f"Unknown regime feature set: {feature_set}")
    if engine not in {"legacy", "rule", "hmm", "gmm", "committee"}:
        raise ValueError(f"Unknown regime engine: {engine}")
    if strategy_policy not in {"legacy", "structural", "high_vol_mean_reversion", "configured"}:
        raise ValueError(f"Unknown regime strategy policy: {strategy_policy}")
    if (
        isinstance(volatile_policy_risk_multiplier, bool)
        or not isinstance(volatile_policy_risk_multiplier, (int, float))
        or not math.isfinite(volatile_policy_risk_multiplier)
        or not 0 < volatile_policy_risk_multiplier <= 1.5
    ):
        raise ValueError("volatile_policy_risk_multiplier must be in (0, 1.5]")
    if (
        isinstance(model_min_confidence, bool)
        or not isinstance(model_min_confidence, (int, float))
        or not math.isfinite(model_min_confidence)
        or not 0 <= model_min_confidence <= 1
    ):
        raise ValueError("model_min_confidence must be a finite fraction in [0, 1]")
    if engine == "legacy" and feature_set == "legacy" and strategy_policy == "legacy" and not include_models:
        return _legacy_rank(
            spy_daily_bars, mode, llm_enabled=llm_enabled,
            llm_disable_in_volatile=llm_disable_in_volatile,
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
        spy_daily_bars, mode, llm_enabled=llm_enabled,
        llm_disable_in_volatile=llm_disable_in_volatile,
        intraday_trend_enabled=intraday_trend_enabled,
        intraday_momentum_enabled=intraday_momentum_enabled,
    )
    model_shadow = {}
    if include_models or engine in {"hmm", "gmm", "committee"}:
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
        rule_selected = smoothed[-1]
        volatility_state = volatility_states[-1]
        current = features[-1]
        rule_reason = (
            f"ER20={current.get('efficiency_ratio_20d')}; "
            f"RV20 percentile={current.get('realized_vol_percentile_252d')}; "
            f"volatility state={volatility_state}; feature set={feature_set}"
        )
        if engine == "committee":
            committee = _committee_consensus(
                raw_rule_regime=raw_states[-1],
                smoothed_rule_regime=rule_selected,
                model_results=model_shadow,
                model_min_confidence=model_min_confidence,
            )
            selected = committee["regime"]
            if selected == "volatile":
                volatility_state = "extreme"
            reason = (
                f"Committee selected {selected}: "
                f"consensus={committee['consensus_score']:.0%}, "
                f"agreement={committee['agreement']:.0%}, "
                f"risk scale={committee['uncertainty_risk_multiplier']:.0%}; {rule_reason}"
            )
        else:
            selected = rule_selected
            reason = rule_reason
        use_legacy_weights = False

    if strategy_regime_weights is not None:
        enabled = strategy_enabled or {}
        weights = {}
        for name, regimes in strategy_regime_weights.items():
            if enabled.get(name, True) is False or not isinstance(regimes, dict):
                continue
            score = regimes.get(selected, 0.0)
            if isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score) and score > 0:
                weights[name] = float(score)
    elif use_legacy_weights:
        weights = {
            item["strategy"]: float(item["score"])
            for item in legacy.get("ranked", [])
        }
    elif strategy_policy == "legacy":
        weights = _strategy_weights(
            selected, strategy_policy="structural",
            llm_enabled=llm_enabled,
            llm_disable_in_volatile=llm_disable_in_volatile,
        )
    else:
        weights = _strategy_weights(
            selected, strategy_policy=strategy_policy,
            llm_enabled=llm_enabled,
            llm_disable_in_volatile=llm_disable_in_volatile,
        )

    available = _available_strategies(
        weights, mode, llm_enabled=llm_enabled,
        intraday_trend_enabled=intraday_trend_enabled,
        intraday_momentum_enabled=intraday_momentum_enabled,
    )
    if mode == "algo" and not use_legacy_weights and strategy_regime_weights is None:
        if intraday_trend_enabled and selected in {"trending_up", "trending_down"}:
            weights["intraday_trend_following"] = 0.60
        if intraday_momentum_enabled and selected in {"trending_up", "trending_down"}:
            weights["intraday_momentum"] = 0.55
        available = _available_strategies(
            weights, mode, llm_enabled=llm_enabled,
            intraday_trend_enabled=intraday_trend_enabled,
            intraday_momentum_enabled=intraday_momentum_enabled,
        )
    ranked = sorted(weights.items(), key=lambda item: item[1], reverse=True)
    active = [
        name for name, score in ranked
        if name in available and (
            strategy_regime_weights is not None
            or name != "swing_llm_driven"
            or score >= 0.55
        )
    ]

    risk_scale_defaults = {"calm": 1.0, "elevated": 0.75, "extreme": 0.5, "unknown": 1.0}
    risk_scale_defaults.update(volatility_risk_scales or {})
    risk_scale = float(risk_scale_defaults.get(volatility_state, 1.0))
    uncertainty_multiplier = committee["uncertainty_risk_multiplier"] if engine == "committee" else 1.0
    if selected == "ambiguous":
        risk_scale = min(risk_scale, 0.5)
    policy_scale = float(volatile_policy_risk_multiplier) if selected == "volatile" else 1.0
    if risk_scale * policy_scale <= 0:
        # A zero scale is a deliberate pause for this volatility state. Keep
        # route weights in diagnostics, but don't advertise strategies as live.
        active = []
    # Route weights decide eligibility on a configured route table; size comes
    # from each strategy's risk_scale. Derived legacy weights still size trades.
    if strategy_regime_weights is None:
        size_factors = {name: float(weights.get(name, 1.0)) for name in active}
    else:
        scales = strategy_risk_scales or {}
        size_factors = {name: _risk_scale_value(scales.get(name, 1.0), name) for name in active}
    risk_multipliers = {
        name: round(min(1.5, risk_scale * policy_scale * size_factors[name]), 4)
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
        "regime_committee": committee if engine == "committee" else None,
        "ranked": rank_items,
        "active": active,
        "strategy_risk_multipliers": risk_multipliers,
        "regime_uncertainty_risk_multiplier": uncertainty_multiplier,
        "risk_budget_multiplier": round(risk_scale * policy_scale * uncertainty_multiplier, 4),
    }
    if engine in {"rule", "committee"}:
        output["regime_history"] = {
            "raw_regime": raw_states[-1],
            "smoothed_regime": smoothed[-1],
            "confirmation_days": 2,
        }
    return output


__all__ = ["REGIME_ENGINE_VERSION", "rank_strategies"]
