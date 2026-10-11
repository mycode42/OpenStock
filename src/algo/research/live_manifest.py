"""Auditable snapshot of the live settings used by research replays."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from src.algo.config import (
    ALGO_ENTRY_LIMITS, ALGO_MODE_CONFIG, ALGO_PORTFOLIO_COORDINATOR_CONFIG,
    get_algo_research_controls,
)
from src.algo.strategies.catalog import STRATEGY_IDS
from src.algo.strategies.registry import ALGO_DEFAULT_PARAMETERS
from src.config import get_algo_runtime_settings


def live_configuration_manifest(strategies: list[str] | tuple[str, ...]) -> dict:
    """Return the exact live risk/coordinator baseline and its stable digest.

    Profile search varies signal rules only. The replay consumes these live
    risk and coordinator settings directly; the snapshot makes that contract
    reviewable in each generated artifact.
    """
    selected = list(dict.fromkeys(strategies))
    snapshot = {
        "entry_limits": {
            "max_daily_entries": ALGO_ENTRY_LIMITS.max_daily_entries,
            "max_intraday_entries": ALGO_ENTRY_LIMITS.max_intraday_entries,
            "max_swing_entries": ALGO_ENTRY_LIMITS.max_swing_entries,
            "max_intraday_core_entries": ALGO_ENTRY_LIMITS.max_intraday_core_entries,
            "max_intraday_satellite_entries": ALGO_ENTRY_LIMITS.max_intraday_satellite_entries,
        },
        "runtime": get_algo_runtime_settings().as_legacy_dict(),
        "coordinator": deepcopy(ALGO_PORTFOLIO_COORDINATOR_CONFIG),
        "risk": deepcopy(ALGO_MODE_CONFIG["risk"]),
        "research_controls": get_algo_research_controls(),
        "strategy_defaults": {
            strategy: deepcopy(ALGO_DEFAULT_PARAMETERS[strategy])
            for strategy in selected if strategy in ALGO_DEFAULT_PARAMETERS
        },
    }
    snapshot = _scope_snapshot(snapshot, selected)
    digest = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=list).encode()
    ).hexdigest()
    return {"sha256": digest, "snapshot": snapshot}


def live_configuration_matches(saved: dict | None, expected: dict, *, strategy: str) -> bool:
    """Compare one strategy's effective settings, including legacy full snapshots.

    Older artifacts stored every strategy's risk values in each strategy
    manifest. Projecting those snapshots onto the selected strategy allows
    unchanged work to survive unrelated strategy removals or setting changes.
    """
    if not isinstance(saved, dict):
        return False
    if saved.get("sha256") == expected.get("sha256"):
        return True
    old_snapshot = saved.get("snapshot")
    expected_snapshot = expected.get("snapshot")
    if not isinstance(old_snapshot, dict) or not isinstance(expected_snapshot, dict):
        return False
    try:
        return _scope_snapshot(old_snapshot, [strategy]) == expected_snapshot
    except (KeyError, TypeError, ValueError):
        return False


def _scope_snapshot(snapshot: dict, strategies: list[str]) -> dict:
    """Keep global policy plus settings that can affect the requested strategies."""
    selected = list(dict.fromkeys(strategies))
    source_risk = snapshot.get("risk") or {}
    current_risk = ALGO_MODE_CONFIG["risk"]
    risk = {}
    for key in current_risk:
        value = source_risk.get(key)
        if key == "strategy_group_limits" and isinstance(value, dict):
            current_groups = current_risk[key]
            risk[key] = {
                group: {
                    "max_open_positions": value.get(group, {}).get("max_open_positions"),
                    "max_entries_per_day": value.get(group, {}).get("max_entries_per_day"),
                    "selected_members": [
                        strategy for strategy in selected
                        if strategy in value.get(group, {}).get("strategies", [])
                    ],
                }
                for group in current_groups
            }
        elif isinstance(value, dict) and set(value).intersection(STRATEGY_IDS):
            risk[key] = {strategy: value[strategy] for strategy in selected if strategy in value}
        else:
            risk[key] = value

    source_controls = snapshot.get("research_controls") or {}
    current_controls = get_algo_research_controls()
    controls = {}
    for key in current_controls:
        value = source_controls.get(key)
        if isinstance(value, dict) and set(value).intersection(STRATEGY_IDS):
            controls[key] = {strategy: value[strategy] for strategy in selected if strategy in value}
        else:
            controls[key] = value

    defaults = snapshot.get("strategy_defaults") or {}
    return {
        "entry_limits": snapshot.get("entry_limits"),
        "runtime": snapshot.get("runtime"),
        "coordinator": snapshot.get("coordinator"),
        "risk": risk,
        "research_controls": controls,
        "strategy_defaults": {
            strategy: defaults[strategy] for strategy in selected if strategy in defaults
        },
    }
