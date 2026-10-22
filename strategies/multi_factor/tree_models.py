"""Regularized, serializable tree models for point-in-time factor research."""

from __future__ import annotations

import math

import numpy as np


TREE_MODEL_FAMILIES = {
    "lightgbm_walk_forward": "lightgbm_cross_sectional",
    "xgboost_walk_forward": "xgboost_cross_sectional",
}

TREE_LOGIC_VERSIONS = {
    "lightgbm_walk_forward": "multi_factor_lightgbm_v2_neutral_missing_factor_timeline",
    "xgboost_walk_forward": "multi_factor_xgboost_v2_neutral_missing_factor_timeline",
}


def _normalized_importance(values: list[float], factor_names: list[str]) -> dict[str, float]:
    positive = np.maximum(np.asarray(values, dtype=float), 0.0)
    total = float(positive.sum())
    if not math.isfinite(total) or total <= 1e-12:
        positive = np.ones(len(factor_names), dtype=float)
        total = float(len(factor_names))
    return {
        name: round(float(value / total), 8)
        for name, value in zip(factor_names, positive)
    }


def fit_tree_factor_model(
    mode: str,
    matrix: np.ndarray,
    target: np.ndarray,
    factor_names: list[str] | tuple[str, ...],
) -> dict:
    """Fit a small, regularized booster and return a JSON-safe model snapshot."""
    names = list(factor_names)
    x = np.asarray(matrix, dtype=np.float32)
    y = np.asarray(target, dtype=np.float32)
    if mode not in TREE_MODEL_FAMILIES:
        raise ValueError(f"unsupported tree factor model: {mode}")
    if x.ndim != 2 or x.shape[1] != len(names) or y.ndim != 1 or len(x) != len(y):
        raise ValueError("tree factor training arrays have incompatible dimensions")
    if len(y) < max(40, len(names) * 8) or not np.isfinite(x).all() or not np.isfinite(y).all():
        raise ValueError("insufficient finite observations to fit tree factor model")

    if mode == "lightgbm_walk_forward":
        from lightgbm import LGBMRegressor

        estimator = LGBMRegressor(
            objective="regression",
            n_estimators=31,
            learning_rate=0.035,
            max_depth=2,
            num_leaves=4,
            min_child_samples=40,
            reg_alpha=2.0,
            reg_lambda=10.0,
            colsample_bytree=0.8,
            subsample=0.8,
            subsample_freq=1,
            random_state=41,
            n_jobs=1,
            verbosity=-1,
            deterministic=True,
            force_col_wise=True,
        )
        estimator.fit(x, y, feature_name=names)
        booster = estimator.booster_
        model_payload = booster.model_to_string()
        importance = booster.feature_importance(importance_type="gain").tolist()
        model_format = "lightgbm_text"
    else:
        from xgboost import XGBRegressor

        estimator = XGBRegressor(
            objective="reg:squarederror",
            n_estimators=31,
            learning_rate=0.035,
            max_depth=2,
            min_child_weight=10.0,
            reg_alpha=2.0,
            reg_lambda=10.0,
            subsample=0.8,
            colsample_bytree=0.8,
            tree_method="hist",
            n_jobs=1,
            random_state=41,
            verbosity=0,
        )
        estimator.fit(x, y, verbose=False)
        booster = estimator.get_booster()
        model_payload = bytes(booster.save_raw(raw_format="json")).decode("utf-8")
        raw_importance = booster.get_score(importance_type="gain")
        importance = [float(raw_importance.get(f"f{index}", 0.0)) for index in range(len(names))]
        model_format = "xgboost_json"

    return {
        "model_mode": mode,
        "model_family": TREE_MODEL_FAMILIES[mode],
        "logic_version": TREE_LOGIC_VERSIONS[mode],
        "model_format": model_format,
        "model_payload": model_payload,
        "factor_names": names,
        "feature_importance": _normalized_importance(importance, names),
    }


def predict_tree_factor_model(artifact: dict, matrix: np.ndarray) -> np.ndarray:
    """Load a portable booster snapshot and predict its cross-sectional scores."""
    model_format = artifact.get("model_format")
    payload = artifact.get("model_payload")
    values = np.asarray(matrix, dtype=np.float32)
    if not isinstance(payload, str) or values.ndim != 2:
        raise ValueError("tree factor model snapshot is malformed")
    if model_format == "lightgbm_text":
        from lightgbm import Booster

        model = Booster(model_str=payload)
        predictions = model.predict(values, num_threads=1)
    elif model_format == "xgboost_json":
        from xgboost import Booster

        model = Booster()
        model.load_model(bytearray(payload.encode("utf-8")))
        predictions = model.inplace_predict(values, validate_features=False)
    else:
        raise ValueError("unsupported tree factor model serialization")
    return np.asarray(predictions, dtype=float)


__all__ = [
    "TREE_LOGIC_VERSIONS", "TREE_MODEL_FAMILIES", "fit_tree_factor_model",
    "predict_tree_factor_model",
]
