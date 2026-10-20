"""Validated point-in-time access to multi-factor Ridge weight artifacts."""

from __future__ import annotations

from datetime import date
import json
import math
from pathlib import Path

from src.algo.profile_store import RESEARCH_ROOT
from src.algo.strategies.multi_factor.config import SUPPORTED_FACTORS


DEFAULT_MODEL_REPORT = RESEARCH_ROOT / "layers" / "latest.json"
RIDGE_LOGIC_VERSION = "multi_factor_ridge_v4_pit_weight_timeline"


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
) -> tuple[dict[str, float] | None, dict]:
    """Return the most recent model whose fit and labels predate ``as_of``."""
    eligible = []
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
        if effective_date <= as_of and label_cutoff < effective_date and label_cutoff < as_of and weights:
            eligible.append((effective_date, label_cutoff, snapshot, weights))
    if not eligible:
        return None, {
            "status": "unavailable",
            "reason": "no purged Ridge snapshot was available by the feature date",
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
        "weight_count": len(weights),
        "as_of": as_of.isoformat(),
    }


def load_live_factor_weights(
    *,
    as_of: date,
    report_path: str | Path | None = None,
    max_age_days: int = 7,
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
    weights = _validated_weights(report.get("current_candidate_weights"))
    if model_date > as_of or label_cutoff >= model_date or label_cutoff >= as_of:
        reason = "model fit or training labels extend beyond the live feature date"
    elif age_days > max_age_days:
        reason = f"model report is stale ({age_days} calendar days old)"
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
            "weight_count": len(weights), "data_version": report.get("data_version"),
            "fundamental_data_version": report.get("fundamental_data_version"),
            "as_of": as_of.isoformat(), "path": str(path),
        }
    return None, {
        "status": "unavailable", "reason": reason, "effective_date": model_date.isoformat(),
        "training_label_cutoff": label_cutoff.isoformat(), "as_of": as_of.isoformat(),
        "path": str(path),
    }


__all__ = ["load_live_factor_weights", "resolve_walk_forward_weights"]
