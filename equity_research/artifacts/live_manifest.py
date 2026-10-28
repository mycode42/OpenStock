"""Auditable snapshot of the live settings used by research replays."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from portfolio.config import (
    get_algo_configuration, get_algo_portfolio_coordinator_config,
    get_algo_research_controls, get_mode_config,
)
from strategies.catalog import STRATEGY_IDS
from strategies.registry import (
    ACTIVE_PROFILE_METADATA, ALGO_DEFAULT_PARAMETERS, REGIME_PARAMETER_PROFILES,
)
from portfolio.config import get_algo_runtime_settings


def live_configuration_manifest(strategies: list[str] | tuple[str, ...]) -> dict:
    """Return the exact live risk/coordinator baseline and its stable digest.

    Profile search varies signal rules only. The replay consumes these live
    risk and coordinator settings directly; the snapshot makes that contract
    reviewable in each generated artifact.
    """
    selected = list(dict.fromkeys(strategies))
    live_config = get_algo_configuration()
    risk = get_mode_config("algo")["risk"]
    snapshot = {
        "entry_limits": {
            "max_daily_entries": live_config["portfolio"]["max_daily_entries"],
            "max_intraday_entries": live_config["portfolio"]["max_intraday_entries"],
            "max_swing_entries": live_config["portfolio"]["max_swing_entries"],
            "max_intraday_core_entries": live_config["portfolio"]["max_intraday_core_entries"],
        },
        "runtime": get_algo_runtime_settings().as_dict(),
        "coordinator": get_algo_portfolio_coordinator_config(),
        "risk": deepcopy(risk),
        "strategy_settings": deepcopy(live_config["strategies"]),
        "research_controls": get_algo_research_controls(),
        "strategy_defaults": {
            strategy: deepcopy(ALGO_DEFAULT_PARAMETERS[strategy])
            for strategy in selected if strategy in ALGO_DEFAULT_PARAMETERS
        },
        "regime_profiles": {
            strategy: deepcopy(REGIME_PARAMETER_PROFILES[strategy])
            for strategy in selected if strategy in REGIME_PARAMETER_PROFILES
        },
        "regime_profile_ids": {
            strategy: {
                regime: (ACTIVE_PROFILE_METADATA.get("profile_ids") or {}).get(
                    f"{strategy}:{regime}", "base",
                )
                for regime in profiles
            }
            for strategy, profiles in REGIME_PARAMETER_PROFILES.items()
            if strategy in selected
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
    current_risk = get_mode_config("algo")["risk"]
    risk = {}
    for key in current_risk:
        value = source_risk.get(key)
        if key == "strategy_group_limits" and isinstance(value, dict):
            current_groups = current_risk[key]
            risk[key] = {
                group: {
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
        "strategy_settings": {
            strategy: (snapshot.get("strategy_settings") or {}).get(strategy)
            for strategy in selected
            if strategy in (snapshot.get("strategy_settings") or {})
        },
        "risk": risk,
        "research_controls": controls,
        "strategy_defaults": {
            strategy: defaults[strategy] for strategy in selected if strategy in defaults
        },
        "regime_profiles": {
            strategy: (snapshot.get("regime_profiles") or {})[strategy]
            for strategy in selected if strategy in (snapshot.get("regime_profiles") or {})
        },
        "regime_profile_ids": {
            strategy: (snapshot.get("regime_profile_ids") or {})[strategy]
            for strategy in selected if strategy in (snapshot.get("regime_profile_ids") or {})
        },
    }
