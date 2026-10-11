"""Auditable snapshot of the live settings used by research replays."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from src.algo.config import (
    ALGO_ENTRY_LIMITS, ALGO_MODE_CONFIG, ALGO_PORTFOLIO_COORDINATOR_CONFIG,
    get_algo_research_controls,
)
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
    digest = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=list).encode()
    ).hexdigest()
    return {"sha256": digest, "snapshot": snapshot}
