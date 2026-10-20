"""Small rolling HMM and GMM regime models used beside deterministic rules."""

from __future__ import annotations

from functools import lru_cache
import math

import numpy as np
from hmmlearn.hmm import GaussianHMM
from sklearn.mixture import GaussianMixture
from sklearn.preprocessing import StandardScaler


def _regime_for_state(means: dict[str, float]) -> str:
    efficiency = means.get("efficiency_ratio_20d", 0.0)
    return_20d = means.get("return_20d", 0.0)
    adx = means.get("adx_14")
    volatility = max(
        means.get("realized_vol_percentile_252d", 0.0),
        means.get("atr_percentile_252d", 0.0),
        means.get("vix_percentile_252d", 0.0),
        means.get("high_yield_spread_percentile_252d", 0.0),
    )
    if volatility >= 0.80:
        return "volatile"
    # State means smooth daily ER, so use a lower trend threshold than the
    # pointwise rule detector. Requiring directionality prevents neutral drift
    # from being labeled a trend.
    if efficiency >= 0.30 and abs(return_20d) >= 0.01 and (adx is None or adx >= 18.0):
        return "trending_up" if return_20d >= 0 else "trending_down"
    if efficiency <= 0.25:
        return "range_bound"
    return "ambiguous"


@lru_cache(maxsize=96)
def _cached_model_fit(
    engine: str,
    feature_names: tuple[str, ...],
    training: tuple[tuple[str, tuple[float, ...]], ...],
    seed: int,
) -> tuple[object, StandardScaler, bool]:
    x_train = np.asarray([values for _day, values in training], dtype=float)
    if x_train.ndim != 2 or not np.isfinite(x_train).all():
        raise ValueError("invalid training features")
    scaler = StandardScaler()
    train_scaled = scaler.fit_transform(x_train)
    if engine == "hmm":
        model = GaussianHMM(
            n_components=3, covariance_type="diag", random_state=seed,
            n_iter=60, tol=1e-3, min_covar=1e-3, implementation="scaling",
        )
        model.fit(train_scaled)
        converged = bool(model.monitor_.converged)
    elif engine == "gmm":
        model = GaussianMixture(
            n_components=3, covariance_type="diag", reg_covar=1e-4,
            n_init=3, max_iter=100, random_state=seed,
        )
        model.fit(train_scaled)
        converged = bool(model.converged_)
    else:
        raise ValueError(f"Unsupported unsupervised regime engine: {engine}")
    return model, scaler, converged


@lru_cache(maxsize=96)
def _cached_fit(
    engine: str,
    feature_names: tuple[str, ...],
    training: tuple[tuple[str, tuple[float, ...]], ...],
    prediction: tuple[tuple[str, tuple[float, ...]], ...],
    seed: int,
) -> dict:
    """Cache deterministic monthly model fit and same-day inference."""
    if len(training) < 252 or not prediction:
        return {"status": "warming_up", "regime": "unknown", "confidence": 0.0, "probabilities": {}}
    x_train = np.asarray([values for _day, values in training], dtype=float)
    x_prediction = np.asarray([values for _day, values in prediction], dtype=float)
    if x_train.ndim != 2 or not np.isfinite(x_train).all() or not np.isfinite(x_prediction).all():
        return {"status": "invalid_features", "regime": "unknown", "confidence": 0.0, "probabilities": {}}
    try:
        model, scaler, converged = _cached_model_fit(engine, feature_names, training, seed)
        train_scaled = scaler.transform(x_train)
        prediction_scaled = scaler.transform(x_prediction)
        if engine == "hmm":
            posterior = model.predict_proba(np.vstack((train_scaled, prediction_scaled)))[-1]
        else:
            posterior = model.predict_proba(prediction_scaled[-1:])[0]
        state_means = scaler.inverse_transform(model.means_)
    except (ValueError, FloatingPointError, ArithmeticError, np.linalg.LinAlgError) as exc:
        return {
            "status": "fit_failed", "error": type(exc).__name__,
            "regime": "unknown", "confidence": 0.0, "probabilities": {},
        }

    category_probabilities: dict[str, float] = {}
    state_assignments = []
    for index, raw_mean in enumerate(state_means):
        means = {name: float(raw_mean[offset]) for offset, name in enumerate(feature_names)}
        category = _regime_for_state(means)
        probability = float(posterior[index])
        category_probabilities[category] = category_probabilities.get(category, 0.0) + probability
        state_assignments.append({
            "state": index, "regime": category,
            "posterior": round(probability, 6),
            "feature_means": {name: round(value, 6) for name, value in means.items()},
        })
    selected = max(category_probabilities, key=category_probabilities.get)
    confidence = category_probabilities[selected]
    return {
        "status": "ok",
        "engine": engine,
        "as_of": prediction[-1][0],
        "regime": selected,
        "confidence": round(float(confidence), 6),
        "probabilities": {name: round(float(value), 6) for name, value in sorted(category_probabilities.items())},
        "state_assignments": state_assignments,
        "converged": converged,
        "fit_samples": len(training),
        "prediction_samples": len(prediction),
        "features": list(feature_names),
    }


def infer_regime_model(
    feature_rows: list[dict], *, engine: str, feature_names: tuple[str, ...],
    seed: int = 41, training_window: int = 756,
) -> dict:
    """Fit only through the prior month, then infer through the current date."""
    if engine not in {"hmm", "gmm"}:
        raise ValueError("engine must be hmm or gmm")
    dated = []
    for row in feature_rows:
        day = str(row.get("date", ""))[:10]
        try:
            values = tuple(float(row[name]) for name in feature_names)
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if day and all(math.isfinite(value) for value in values):
            dated.append((day, values))
    if not dated:
        return {"status": "warming_up", "engine": engine, "regime": "unknown", "confidence": 0.0, "probabilities": {}}
    as_of = dated[-1][0]
    month_start = f"{as_of[:7]}-01"
    training = tuple(row for row in dated if row[0] < month_start)[-training_window:]
    prediction = tuple(row for row in dated if row[0] >= month_start and row[0] <= as_of)
    result = dict(_cached_fit(engine, tuple(feature_names), training, prediction, seed))
    result["engine"] = engine
    result["as_of"] = as_of
    return result


__all__ = ["infer_regime_model"]
