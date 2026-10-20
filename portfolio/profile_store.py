"""Persistent live-profile manifests for Algo and forward research."""

from __future__ import annotations

import json
from pathlib import Path


from runtime.paths import DATA_DIR
from runtime.atomic import atomic_write_json
RESEARCH_ROOT = DATA_DIR / "research" / "algo-auto"
PROMOTED_PROFILE_PATH = RESEARCH_ROOT / "promoted-profile.json"
FORWARD_VALIDATION_PATH = RESEARCH_ROOT / "forward-validation.json"


def _read_profile(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot safely read {path.name}: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def effective_profile_metadata() -> dict:
    """Load promoted parameter overrides for the live Algo process."""
    live = _read_profile(PROMOTED_PROFILE_PATH)
    strategy_parameters: dict[str, dict] = {}
    regime_parameters: dict[str, dict[str, dict]] = {}
    profile_ids: dict[str, str] = {}

    parameters = live.get("parameters") or {}
    if isinstance(parameters, dict):
        for strategy, values in parameters.items():
            if isinstance(strategy, str) and isinstance(values, dict):
                strategy_parameters[strategy] = dict(values)
                profile_ids[strategy] = str(
                    (live.get("profile_ids") or {}).get(strategy)
                    or live.get("release_id") or "base"
                )
    regimes = live.get("regime_profiles") or {}
    if isinstance(regimes, dict):
        for strategy, by_regime in regimes.items():
            if not isinstance(strategy, str) or not isinstance(by_regime, dict):
                continue
            target = regime_parameters.setdefault(strategy, {})
            for regime, values in by_regime.items():
                if isinstance(regime, str) and isinstance(values, dict):
                    target[regime] = dict(values)
                    id_map = live.get("regime_profile_ids") or {}
                    profile_ids[f"{strategy}:{regime}"] = str(
                        (id_map.get(strategy) or {}).get(regime)
                        or live.get("release_id") or "base"
                    )

    return {
        "profile": "algo",
        "parameters": strategy_parameters,
        "regime_profiles": regime_parameters,
        "profile_ids": profile_ids,
        "live_release_id": live.get("release_id"),
    }


def write_json_atomic(path: Path, payload: dict) -> None:
    """Atomically replace a JSON artifact after flushing it to disk."""
    atomic_write_json(path, payload, default=str)
