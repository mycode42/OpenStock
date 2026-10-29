"""Canonical Algo strategy identity and portfolio metadata.

Signal parameters and implementations stay in their family modules. This
catalog owns the cross-cutting labels used by live routing, coordination,
state validation, and research.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: str
    bar_interval: str
    horizon: str
    role: str
    portfolio_group: str | None
    coordinator_group: str | None
    generator: str = "per_symbol"
    runtime_setting: str | None = None
    research_workflows: tuple[str, ...] = ()


_STRATEGIES = {
    "intraday_mean_reversion": StrategySpec(
        "intraday_mean_reversion", "5Min", "intraday", "core", "intraday_core", "intraday",
        research_workflows=("nightly_intraday",),
    ),
    "swing_trend_following": StrategySpec(
        "swing_trend_following", "1Day", "swing", "core", "swing", "swing_core",
        research_workflows=("nightly_swing",),
    ),
    "swing_momentum": StrategySpec(
        "swing_momentum", "1Day", "swing", "core", "swing", "swing_core", "cross_sectional",
        research_workflows=("nightly_swing",),
    ),
    "swing_mean_reversion": StrategySpec(
        "swing_mean_reversion", "1Day", "swing", "core", "swing", "swing_core",
        research_workflows=("nightly_swing",),
    ),
    "intraday_trend_following": StrategySpec(
        "intraday_trend_following", "5Min", "intraday", "core", "intraday_core", "intraday",
        runtime_setting="enable_intraday_trend_following",
        research_workflows=("nightly_intraday",),
    ),
    "intraday_momentum": StrategySpec(
        "intraday_momentum", "5Min", "intraday", "core", "intraday_core", "intraday",
        runtime_setting="enable_intraday_momentum",
        research_workflows=("nightly_intraday",),
    ),
    "swing_multi_factor": StrategySpec(
        "swing_multi_factor", "1Day", "swing", "core", "swing", "swing_core", "cross_sectional",
        research_workflows=("nightly_swing",),
    ),
    "swing_llm_driven": StrategySpec(
        "swing_llm_driven", "1Day", "swing", "core", "swing", "swing_core", "llm",
        research_workflows=("llm_research",),
    ),
}

# Strategy IDs are persisted in portfolio ownership, trade journals, and old
# research artifacts. Normalize pre-prefix IDs at input boundaries so existing
# state and profiles keep referring to the same strategy after the rename.
STRATEGY_ID_ALIASES = MappingProxyType({
    "mean_reversion": "intraday_mean_reversion",
    "trend_following": "swing_trend_following",
    "momentum": "swing_momentum",
    "intraday_trend": "intraday_trend_following",
    "multi_factor": "swing_multi_factor",
    "cash_sleeve": "cash_reserve",
})


def canonical_strategy_id(strategy: str) -> str:
    """Map a legacy Algo strategy ID to its current horizon-explicit ID."""
    return STRATEGY_ID_ALIASES.get(strategy, strategy)


STRATEGY_CATALOG = MappingProxyType(_STRATEGIES)
STRATEGY_IDS = frozenset(STRATEGY_CATALOG)
CORE_STRATEGIES = frozenset(
    name for name, spec in STRATEGY_CATALOG.items() if spec.role == "core"
)
INTRADAY_STRATEGIES = frozenset(
    name for name, spec in STRATEGY_CATALOG.items() if spec.coordinator_group == "intraday"
)
SWING_STRATEGIES = frozenset(
    name for name, spec in STRATEGY_CATALOG.items()
    if spec.coordinator_group == "swing_core"
)
SWING_CORE_STRATEGIES = frozenset(
    name for name, spec in STRATEGY_CATALOG.items() if spec.coordinator_group == "swing_core"
)


def strategy_ids_for_group(group: str) -> tuple[str, ...]:
    """Return stable catalog order for a portfolio or research group."""
    return tuple(
        name for name, spec in STRATEGY_CATALOG.items()
        if spec.portfolio_group == group
    )


def strategy_ids_for_research_workflow(workflow: str) -> tuple[str, ...]:
    """Return the stable strategy order for a named research workflow."""
    return tuple(
        name for name, spec in STRATEGY_CATALOG.items()
        if workflow in spec.research_workflows
    )


__all__ = [
    "CORE_STRATEGIES", "INTRADAY_STRATEGIES",
    "STRATEGY_CATALOG", "STRATEGY_ID_ALIASES", "STRATEGY_IDS",
    "SWING_CORE_STRATEGIES", "SWING_STRATEGIES", "StrategySpec",
    "canonical_strategy_id", "strategy_ids_for_group",
    "strategy_ids_for_research_workflow",
]
