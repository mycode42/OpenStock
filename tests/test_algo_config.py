"""Algo policy limits have one shared source for live and research paths."""

from src.algo.config import (
    ALGO_ENTRY_LIMITS,
    ALGO_MODE_CONFIG,
    ALGO_PORTFOLIO_COORDINATOR_CONFIG,
    get_algo_research_controls,
)
from src.config import (
    ALGO_MODE_CONFIG as LEGACY_ALGO_MODE_CONFIG, STRATEGY_CONFIG,
    get_algo_runtime_settings,
)
from src.algo.strategies.portfolio_coordinator import PortfolioCoordinatorConfig


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

    assert ALGO_MODE_CONFIG["risk"]["max_entries_per_strategy_per_day"]["momentum"] == 2


def test_catalog_groups_preserve_risk_and_coordinator_membership():
    groups = ALGO_MODE_CONFIG["risk"]["strategy_group_limits"]

    assert groups["swing"]["strategies"] == [
        "trend_following", "momentum", "swing_mean_reversion",
        "swing_trend_following", "swing_momentum", "multi_factor", "event_driven",
    ]
    assert groups["intraday_core"]["strategies"] == [
        "mean_reversion",
    ]
    assert groups["intraday_satellites"]["strategies"] == [
        "intraday_trend", "intraday_momentum",
    ]
    assert set(PortfolioCoordinatorConfig().swing_core_strategy_ids) == {
        "trend_following", "momentum", "swing_mean_reversion", "multi_factor", "event_driven",
    }


def test_typed_runtime_settings_keep_legacy_runtime_overrides(monkeypatch):
    monkeypatch.setitem(STRATEGY_CONFIG, "enable_intraday_trend", True)

    assert get_algo_runtime_settings().enable_intraday_trend is True
