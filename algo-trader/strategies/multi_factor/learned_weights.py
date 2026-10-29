"""Validated point-in-time access to multi-factor Ridge weight artifacts."""

from __future__ import annotations

from datetime import date
import json
import math
from pathlib import Path

from portfolio.profile_store import RESEARCH_ROOT
from strategies.multi_factor.config import SUPPORTED_FACTORS
from strategies.multi_factor.tree_models import TREE_LOGIC_VERSIONS


DEFAULT_MODEL_REPORT = RESEARCH_ROOT / "layers" / "latest.json"
RIDGE_LOGIC_VERSION = "multi_factor_ridge_v5_neutral_missing_factor_timeline"


def _validated_weights(value: object) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    weights: dict[str, float] = {}
    for name, raw in value.items():
        if name not in SUPPORTED_FACTORS or isinstance(raw, bool):
            continue
        try:
            weight = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(weight) and weight > 0:
            weights[str(name)] = weight
    total = sum(weights.values())
    return {name: weight / total for name, weight in weights.items()} if total > 0 else {}


def resolve_walk_forward_weights(
    timeline: list[dict] | None,
    *,
    as_of: date,
    max_snapshot_age_days: int = 90,
    max_training_age_days: int = 90,
) -> tuple[dict[str, float] | None, dict]:
    """Return a recent fit with recent, fully matured training labels."""
    eligible = []
    stale_training = []
    for snapshot in timeline or []:
        if not isinstance(snapshot, dict):
            continue
        if snapshot.get("logic_version") != RIDGE_LOGIC_VERSION:
            continue
        try:
            effective_date = date.fromisoformat(str(snapshot["effective_date"])[:10])
            label_cutoff = date.fromisoformat(str(snapshot["training_label_cutoff"])[:10])
        except (KeyError, TypeError, ValueError):
            continue
        weights = _validated_weights(snapshot.get("weights"))
        snapshot_age = (as_of - effective_date).days
        training_age = (as_of - label_cutoff).days
        if (
            effective_date <= as_of and label_cutoff < effective_date and label_cutoff < as_of
            and 0 <= snapshot_age <= max_snapshot_age_days
            and training_age > max_training_age_days
        ):
            stale_training.append((label_cutoff, training_age))
        if (
            effective_date <= as_of and label_cutoff < effective_date and label_cutoff < as_of
            and 0 <= snapshot_age <= max_snapshot_age_days
            and 0 <= training_age <= max_training_age_days and weights
        ):
            eligible.append((effective_date, label_cutoff, snapshot, weights))
    if not eligible:
        reason = "no fresh, purged Ridge snapshot was available by the feature date"
        if stale_training:
            _, training_age = max(stale_training, key=lambda row: row[0])
            reason = f"recent Ridge fit has stale training labels ({training_age} days old)"
        return None, {
            "status": "unavailable",
            "reason": reason,
            "newest_stale_training_label_cutoff": max(stale_training, key=lambda row: row[0])[0].isoformat()
            if stale_training else None,
            "as_of": as_of.isoformat(),
        }
    effective_date, label_cutoff, snapshot, weights = max(eligible, key=lambda row: row[0])
    return weights, {
        "status": "loaded",
        "model_family": snapshot.get("model_family", "ridge_cross_sectional"),
        "logic_version": snapshot.get("logic_version"),
        "effective_date": effective_date.isoformat(),
        "training_label_cutoff": label_cutoff.isoformat(),
        "training_observation_count": snapshot.get("training_observation_count"),
        "equal_weight_fallback": bool(snapshot.get("equal_weight_fallback", False)),
        "weight_count": len(weights), "snapshot_age_days": (as_of - effective_date).days,
        "training_age_days": (as_of - label_cutoff).days,
        "as_of": as_of.isoformat(),
    }


def load_live_factor_weights(
    *,
    as_of: date,
    report_path: str | Path | None = None,
    max_age_days: int = 7,
    max_training_age_days: int = 90,
) -> tuple[dict[str, float] | None, dict]:
    """Load the current purged fit for live ranking, failing closed if stale."""
    path = Path(report_path) if report_path is not None else DEFAULT_MODEL_REPORT
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return None, {
            "status": "unavailable", "reason": f"model report unreadable: {type(exc).__name__}",
            "path": str(path),
        }
    report = payload.get("factor_model") if isinstance(payload, dict) else None
    if not isinstance(report, dict):
        return None, {"status": "unavailable", "reason": "model report has no factor_model section", "path": str(path)}
    if report.get("logic_version") != RIDGE_LOGIC_VERSION:
        return None, {
            "status": "unavailable", "reason": "model report uses an unsupported Ridge logic version",
            "logic_version": report.get("logic_version"), "path": str(path),
        }
    try:
        model_date = date.fromisoformat(str(report["session_end"])[:10])
        label_cutoff = date.fromisoformat(str(report["current_training_label_cutoff"])[:10])
    except (KeyError, TypeError, ValueError):
        return None, {"status": "unavailable", "reason": "model report is missing valid fit dates", "path": str(path)}
    age_days = (as_of - model_date).days
    training_age_days = (as_of - label_cutoff).days
    weights = _validated_weights(report.get("current_candidate_weights"))
    if model_date > as_of or label_cutoff >= model_date or label_cutoff >= as_of:
        reason = "model fit or training labels extend beyond the live feature date"
    elif age_days > max_age_days:
        reason = f"model report is stale ({age_days} calendar days old)"
    elif training_age_days > max_training_age_days:
        reason = f"model training labels are stale ({training_age_days} calendar days old)"
    elif not weights:
        reason = "model report contains no valid positive factor weights"
    else:
        return weights, {
            "status": "loaded", "model_family": report.get("model_family", "ridge_cross_sectional"),
            "logic_version": report.get("logic_version"),
            "effective_date": model_date.isoformat(),
            "training_label_cutoff": label_cutoff.isoformat(),
            "training_observation_count": report.get("current_training_sample_count"),
            "equal_weight_fallback": bool(report.get("current_fit_equal_weight_fallback", False)),
            "weight_count": len(weights), "training_age_days": training_age_days,
            "data_version": report.get("data_version"),
            "fundamental_data_version": report.get("fundamental_data_version"),
            "as_of": as_of.isoformat(), "path": str(path),
        }
    return None, {
        "status": "unavailable", "reason": reason, "effective_date": model_date.isoformat(),
        "training_label_cutoff": label_cutoff.isoformat(), "as_of": as_of.isoformat(),
        "training_age_days": training_age_days,
        "path": str(path),
    }


def _valid_tree_snapshot(snapshot: object, model_mode: str) -> bool:
    if not isinstance(snapshot, dict):
        return False
    expected_format = "lightgbm_text" if model_mode == "lightgbm_walk_forward" else "xgboost_json"
    names = snapshot.get("factor_names")
    minimum_factor_coverage = snapshot.get("minimum_factor_coverage")
    return (
        snapshot.get("model_mode") == model_mode
        and snapshot.get("logic_version") == TREE_LOGIC_VERSIONS.get(model_mode)
        and snapshot.get("model_format") == expected_format
        and isinstance(snapshot.get("model_payload"), str)
        and isinstance(names, list) and bool(names)
        and all(isinstance(name, str) for name in names)
        and not (set(names) - SUPPORTED_FACTORS)
        and isinstance(minimum_factor_coverage, int)
        and not isinstance(minimum_factor_coverage, bool)
        and minimum_factor_coverage >= 2
    )


def resolve_walk_forward_tree_model(
    timeline: list[dict] | None,
    *,
    model_mode: str,
    as_of: date,
    max_snapshot_age_days: int = 90,
    max_training_age_days: int = 90,
) -> tuple[dict | None, dict]:
    """Select a recent point-in-time tree model trained on recent matured labels."""
    if model_mode not in TREE_LOGIC_VERSIONS:
        return None, {"status": "unavailable", "reason": "unsupported tree model mode", "as_of": as_of.isoformat()}
    eligible = []
    stale_training = []
    for snapshot in timeline or []:
        if not _valid_tree_snapshot(snapshot, model_mode):
            continue
        try:
            effective_date = date.fromisoformat(str(snapshot["effective_date"])[:10])
            label_cutoff = date.fromisoformat(str(snapshot["training_label_cutoff"])[:10])
        except (KeyError, TypeError, ValueError):
            continue
        snapshot_age = (as_of - effective_date).days
        training_age = (as_of - label_cutoff).days
        if (
            effective_date <= as_of and label_cutoff < effective_date and label_cutoff < as_of
            and 0 <= snapshot_age <= max_snapshot_age_days
            and training_age > max_training_age_days
        ):
            stale_training.append((label_cutoff, training_age))
        if (
            effective_date <= as_of and label_cutoff < effective_date and label_cutoff < as_of
            and 0 <= snapshot_age <= max_snapshot_age_days
            and 0 <= training_age <= max_training_age_days
        ):
            eligible.append((effective_date, label_cutoff, snapshot))
    if not eligible:
        reason = f"no fresh, purged {model_mode} snapshot was available by the feature date"
        if stale_training:
            _, training_age = max(stale_training, key=lambda row: row[0])
            reason = f"recent {model_mode} fit has stale training labels ({training_age} days old)"
        return None, {
            "status": "unavailable", "reason": reason,
            "newest_stale_training_label_cutoff": max(stale_training, key=lambda row: row[0])[0].isoformat()
            if stale_training else None,
            "as_of": as_of.isoformat(),
        }
    effective_date, label_cutoff, snapshot = max(eligible, key=lambda row: row[0])
    artifact = {
        key: snapshot.get(key)
        for key in (
            "model_mode", "model_family", "logic_version", "model_format", "model_payload",
            "factor_names", "feature_importance", "cost_model_version", "data_version",
            "fundamentals_data_version", "minimum_factor_coverage",
        )
    }
    return artifact, {
        "status": "loaded", "model_family": snapshot["model_family"],
        "logic_version": snapshot["logic_version"], "effective_date": effective_date.isoformat(),
        "training_label_cutoff": label_cutoff.isoformat(),
        "training_observation_count": snapshot.get("training_observation_count"),
        "training_age_days": (as_of - label_cutoff).days,
        "factor_count": len(snapshot["factor_names"]),
        "snapshot_age_days": (as_of - effective_date).days, "as_of": as_of.isoformat(),
    }


def load_live_tree_factor_model(
    *,
    as_of: date,
    model_mode: str,
    report_path: str | Path | None = None,
    max_age_days: int = 7,
    max_training_age_days: int = 90,
) -> tuple[dict | None, dict]:
    """Load the current point-in-time tree model for live ranking, failing closed if stale."""
    path = Path(report_path) if report_path is not None else DEFAULT_MODEL_REPORT
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return None, {"status": "unavailable", "reason": f"model report unreadable: {type(exc).__name__}", "path": str(path)}
    report = payload.get("factor_model") if isinstance(payload, dict) else None
    if not isinstance(report, dict):
        return None, {"status": "unavailable", "reason": "model report has no factor_model section", "path": str(path)}
    snapshot = (report.get("current_models") or {}).get(model_mode)
    if not _valid_tree_snapshot(snapshot, model_mode):
        return None, {
            "status": "unavailable", "reason": f"model report has no valid {model_mode} fit",
            "model_mode": model_mode, "path": str(path),
        }
    try:
        model_date = date.fromisoformat(str(snapshot["effective_date"])[:10])
        label_cutoff = date.fromisoformat(str(snapshot["training_label_cutoff"])[:10])
    except (KeyError, TypeError, ValueError):
        return None, {"status": "unavailable", "reason": "model snapshot is missing valid fit dates", "path": str(path)}
    age_days = (as_of - model_date).days
    training_age_days = (as_of - label_cutoff).days
    if model_date > as_of or label_cutoff >= model_date or label_cutoff >= as_of:
        reason = "model fit or training labels extend beyond the live feature date"
    elif age_days > max_age_days:
        reason = f"model report is stale ({age_days} calendar days old)"
    elif training_age_days > max_training_age_days:
        reason = f"model training labels are stale ({training_age_days} calendar days old)"
    else:
        artifact = {
            key: snapshot.get(key)
            for key in (
                "model_mode", "model_family", "logic_version", "model_format", "model_payload",
                "factor_names", "feature_importance", "cost_model_version", "data_version",
                "fundamentals_data_version", "minimum_factor_coverage",
            )
        }
        return artifact, {
            "status": "loaded", "model_family": snapshot["model_family"],
            "logic_version": snapshot["logic_version"], "effective_date": model_date.isoformat(),
            "training_label_cutoff": label_cutoff.isoformat(),
            "training_observation_count": snapshot.get("training_observation_count"),
            "factor_count": len(snapshot["factor_names"]), "training_age_days": training_age_days,
            "as_of": as_of.isoformat(),
            "data_version": report.get("data_version"),
            "fundamental_data_version": report.get("fundamental_data_version"), "path": str(path),
        }
    return None, {
        "status": "unavailable", "reason": reason, "effective_date": model_date.isoformat(),
        "training_label_cutoff": label_cutoff.isoformat(), "as_of": as_of.isoformat(), "path": str(path),
        "training_age_days": training_age_days,
    }


__all__ = [
    "load_live_factor_weights", "load_live_tree_factor_model", "resolve_walk_forward_tree_model",
    "resolve_walk_forward_weights",
]
