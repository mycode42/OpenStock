"""Algo policy limits have one shared source for live and research paths."""

from src.algo.config import (
    ALGO_ENTRY_LIMITS,
    ALGO_MODE_CONFIG,
    ALGO_PORTFOLIO_COORDINATOR_CONFIG,
    get_algo_research_controls,
)
from src.config import ALGO_MODE_CONFIG as LEGACY_ALGO_MODE_CONFIG
from src.strategies.portfolio_coordinator import PortfolioCoordinatorConfig


def test_algo_daily_entry_budgets_are_shared_by_live_and_research():
    risk = ALGO_MODE_CONFIG["risk"]
    groups = risk["strategy_group_limits"]
    research = get_algo_research_controls()
    coordinator = PortfolioCoordinatorConfig()

    assert risk["max_trades_per_day"] == ALGO_ENTRY_LIMITS.max_daily_entries
    assert research["max_entries_per_day"] == ALGO_ENTRY_LIMITS.max_daily_entries
    assert ALGO_PORTFOLIO_COORDINATOR_CONFIG["max_daily_entries"] == ALGO_ENTRY_LIMITS.max_daily_entries
    assert ALGO_PORTFOLIO_COORDINATOR_CONFIG["max_intraday_entries"] == ALGO_ENTRY_LIMITS.max_intraday_entries
    assert ALGO_PORTFOLIO_COORDINATOR_CONFIG["max_swing_entries"] == ALGO_ENTRY_LIMITS.max_swing_entries
    assert groups["swing"]["max_entries_per_day"] == ALGO_ENTRY_LIMITS.max_swing_entries
    assert groups["intraday_core"]["max_entries_per_day"] == ALGO_ENTRY_LIMITS.max_intraday_core_entries
    assert groups["intraday_satellites"]["max_entries_per_day"] == ALGO_ENTRY_LIMITS.max_intraday_satellite_entries
    assert coordinator.max_daily_entries == ALGO_ENTRY_LIMITS.max_daily_entries
    assert coordinator.max_intraday_entries == ALGO_ENTRY_LIMITS.max_intraday_entries
    assert coordinator.max_swing_entries == ALGO_ENTRY_LIMITS.max_swing_entries
    assert LEGACY_ALGO_MODE_CONFIG is ALGO_MODE_CONFIG


def test_research_controls_are_detached_from_live_risk_config():
    controls = get_algo_research_controls()
    controls["max_entries_per_strategy_per_day"]["momentum"] = 99

    assert ALGO_MODE_CONFIG["risk"]["max_entries_per_strategy_per_day"]["momentum"] == 1
