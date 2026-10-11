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
    research_groups: tuple[str, ...] = ()
    runtime_setting: str | None = None
    research_evaluator: str | None = None
    research_workflows: tuple[str, ...] = ()


_STRATEGIES = {
    "mean_reversion": StrategySpec(
        "mean_reversion", "5Min", "intraday", "core", "intraday_core", "intraday",
        research_groups=("intraday",), research_workflows=("nightly_intraday",),
    ),
    "opening_range_breakout": StrategySpec(
        "opening_range_breakout", "5Min", "intraday", "core", "intraday_core", "intraday",
        research_groups=("intraday",), research_workflows=("nightly_intraday",),
    ),
    "trend_following": StrategySpec(
        "trend_following", "1Day", "swing", "core", "swing", "swing_core",
        research_groups=("intraday",), research_evaluator="intraday_trend",
        research_workflows=("nightly_swing",),
    ),
    "momentum": StrategySpec(
        "momentum", "1Day", "swing", "core", "swing", "swing_core", "cross_sectional",
        ("intraday",), research_evaluator="intraday_momentum",
        research_workflows=("nightly_swing",),
    ),
    "swing_mean_reversion": StrategySpec(
        "swing_mean_reversion", "1Day", "swing", "core", "swing", "swing_core",
        research_groups=("swing_core", "swing"), research_workflows=("nightly_swing",),
    ),
    "swing_trend_following": StrategySpec(
        "swing_trend_following", "1Day", "swing", "core", "swing", None,
        research_groups=("swing_core", "swing"),
    ),
    "swing_momentum": StrategySpec(
        "swing_momentum", "1Day", "swing", "core", "swing", None, "cross_sectional",
        ("swing_core", "swing"),
    ),
    "intraday_trend": StrategySpec(
        "intraday_trend", "5Min", "intraday", "core", "intraday_satellites", "intraday",
        research_groups=("intraday",), runtime_setting="enable_intraday_trend",
        research_workflows=("nightly_intraday",),
    ),
    "intraday_momentum": StrategySpec(
        "intraday_momentum", "5Min", "intraday", "core", "intraday_satellites", "intraday",
        research_groups=("intraday",), runtime_setting="enable_intraday_momentum",
        research_workflows=("nightly_intraday",),
    ),
    "multi_factor": StrategySpec(
        "multi_factor", "1Day", "swing", "core", "swing", "swing_core", "cross_sectional",
        ("swing_core", "swing"), research_workflows=("nightly_swing",),
    ),
    "event_driven": StrategySpec(
        "event_driven", "1Day", "swing", "satellite", None, "satellite", "event",
        ("swing",), research_workflows=("overlay",),
    ),
}

STRATEGY_CATALOG = MappingProxyType(_STRATEGIES)
STRATEGY_IDS = frozenset(STRATEGY_CATALOG)
CORE_STRATEGIES = frozenset(
    name for name, spec in STRATEGY_CATALOG.items() if spec.role == "core"
)
SATELLITE_STRATEGIES = frozenset(
    name for name, spec in STRATEGY_CATALOG.items() if spec.role == "satellite"
)
INTRADAY_STRATEGIES = frozenset(
    name for name, spec in STRATEGY_CATALOG.items() if spec.coordinator_group == "intraday"
)
SWING_STRATEGIES = frozenset(
    name for name, spec in STRATEGY_CATALOG.items()
    if spec.coordinator_group in {"swing_core", "satellite"}
)
SWING_CORE_STRATEGIES = frozenset(
    name for name, spec in STRATEGY_CATALOG.items() if spec.coordinator_group == "swing_core"
)
RESEARCH_INTRADAY_STRATEGIES = frozenset(
    name for name, spec in STRATEGY_CATALOG.items() if "intraday" in spec.research_groups
)
RESEARCH_SWING_CORE_STRATEGIES = frozenset(
    name for name, spec in STRATEGY_CATALOG.items() if "swing_core" in spec.research_groups
)
RESEARCH_SWING_STRATEGIES = frozenset(
    name for name, spec in STRATEGY_CATALOG.items() if "swing" in spec.research_groups
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
