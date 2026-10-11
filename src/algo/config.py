"""Central configuration for the dedicated Algo trading service."""

from dataclasses import asdict, dataclass
from copy import deepcopy
import math

from src.algo.strategies.catalog import strategy_ids_for_group


@dataclass(frozen=True)
class AlgoEntryLimits:
    """Shared entry budgets consumed by live coordination and research replay."""

    max_daily_entries: int = 10
    max_intraday_entries: int = 3
    max_swing_entries: int = 3
    max_intraday_core_entries: int = 3
    max_intraday_satellite_entries: int = 2


ALGO_ENTRY_LIMITS = AlgoEntryLimits()


@dataclass(frozen=True)
class AlgoRuntimeSettings:
    """Typed source of truth for settings specific to the Algo service."""

    overnight_entries_enabled: bool = False
    enable_intraday_trend: bool = False
    enable_intraday_momentum: bool = False
    intraday_satellite_position_pct: float = 0.01
    intraday_satellite_risk_multiplier: float = 0.25
    algo_multi_factor_enabled: bool = True
    # Core-strategy candidates are measured in shadow mode before any order is routed.
    algo_multi_factor_execution_mode: str = "shadow"
    algo_multi_factor_shadow_capital: int = 100_000
    algo_multi_factor_shadow_slippage_bps: float = 5.0
    algo_multi_factor_shadow_commission_per_share: float = 0.005
    algo_event_driven_enabled: bool = False

    def __post_init__(self) -> None:
        for name in (
            "overnight_entries_enabled", "enable_intraday_trend",
            "enable_intraday_momentum", "algo_multi_factor_enabled",
            "algo_event_driven_enabled",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be boolean")
        if self.algo_multi_factor_execution_mode not in {"live", "shadow"}:
            raise ValueError("algo_multi_factor_execution_mode must be live or shadow")
        for name in (
            "intraday_satellite_position_pct", "intraday_satellite_risk_multiplier",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive finite number")
        if (
            isinstance(self.algo_multi_factor_shadow_capital, bool)
            or not isinstance(self.algo_multi_factor_shadow_capital, int)
            or self.algo_multi_factor_shadow_capital <= 0
        ):
            raise ValueError("algo_multi_factor_shadow_capital must be a positive integer")
        for name in (
            "algo_multi_factor_shadow_slippage_bps",
            "algo_multi_factor_shadow_commission_per_share",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative number")

    def as_legacy_dict(self) -> dict:
        """Return the compatibility mapping consumed by older integrations."""
        return asdict(self)


ALGO_RUNTIME_SETTINGS = AlgoRuntimeSettings()
# Compatibility projection for callers that still consume runtime mappings.
ALGO_RUNTIME_CONFIG = ALGO_RUNTIME_SETTINGS.as_legacy_dict()
ALGO_SIGNAL_DATA_CONFIG = {
    "signal_bar_max_age_minutes": 15,
    "delayed_signal_bar_max_age_minutes": 35,
    "max_spy_daily_age_days": 5,
}

# Algo has its own fixed universe while signal and portfolio behavior are
# validated. Swing and Day retain their separately configured fixed lists.
ALGO_PORTFOLIO_UNIVERSE = {
    "core_tech": {
        "label": "Mega-liquid Tech / Growth",
        "tickers": ["NVDA", "AVGO", "TSM", "MSFT", "AAPL", "GOOGL", "META", "AMZN", "AMD"],
        "max_sector_pct": 0.40,
        "max_per_stock_pct": 0.05,
    },
    "diversifiers": {
        "label": "High-quality Non-Tech / Diversifiers",
        "tickers": ["JPM", "V", "UNH", "JNJ", "PG", "KO"],
        "max_sector_pct": 0.30,
        "max_per_stock_pct": 0.05,
    },
    "hedge": {
        "label": "Broad Market & Hedge ETFs",
        "tickers": ["SPY", "QQQ", "GLD", "TLT"],
        "max_sector_pct": 0.20,
        "max_per_stock_pct": 0.05,
    },
    "tactical": {
        "label": "Optional Tactical Satellite",
        "tickers": ["PLTR", "CRM", "PANW"],
        "max_sector_pct": 0.04,
        "max_per_stock_pct": 0.02,
    },
}
ALGO_MODE_CONFIG = {
    "label": "Autonomous Algo Portfolio",
    "universe": ALGO_PORTFOLIO_UNIVERSE,
    "risk": {
        "max_position_pct": 0.05,
        "max_sector_pct": 0.40,
        # Conservative fallback plus explicit horizon-specific protection.
        "stop_loss_pct": 0.05,
        "trailing_stop_pct": 0.04,
        "stop_loss_pct_by_strategy": {
            "mean_reversion": 0.011,
            "opening_range_breakout": 0.011,
            "trend_following": 0.06,
            "momentum": 0.06,
            "swing_trend_following": 0.06,
            "swing_momentum": 0.06,
            "multi_factor": 0.06,
            "swing_mean_reversion": 0.07,
            "intraday_trend": 0.011,
            "intraday_momentum": 0.011,
        },
        "trailing_stop_pct_by_strategy": {
            "mean_reversion": 0.008,
            "opening_range_breakout": 0.008,
            "trend_following": 0.045,
            "momentum": 0.045,
            "swing_trend_following": 0.045,
            "swing_momentum": 0.045,
            "multi_factor": 0.045,
            "swing_mean_reversion": 0.05,
            "intraday_trend": 0.008,
            "intraday_momentum": 0.008,
        },
        "trailing_activation_pct_by_strategy": {
            "mean_reversion": 0.005,
            "opening_range_breakout": 0.005,
            "trend_following": 0.02,
            "momentum": 0.02,
            "swing_trend_following": 0.02,
            "swing_momentum": 0.02,
            "multi_factor": 0.02,
            "swing_mean_reversion": 0.02,
            "intraday_trend": 0.005,
            "intraday_momentum": 0.005,
        },
        "daily_loss_limit": 0.02,
        "weekly_loss_limit": 0.05,
        "no_buy_if_down_pct": 0.02,
        "max_trades_per_day": ALGO_ENTRY_LIMITS.max_daily_entries,
        "max_open_positions": 16,
        "max_positions_per_strategy": 5,
        "max_positions_per_strategy_by_strategy": {
            "trend_following": 4, "momentum": 4, "swing_mean_reversion": 4,
            "swing_trend_following": 4, "swing_momentum": 4,
            "multi_factor": 4,
            "mean_reversion": 5, "opening_range_breakout": 5,
            "intraday_trend": 1, "intraday_momentum": 1,
        },
        "max_short_positions": 5,
        "max_short_exposure_pct": 0.20,
        "allow_short_positions": True,
        "short_disabled_in_bull_regime": True,
        "range_regime_short_risk_multiplier": 0.75,
        "minimum_hold_minutes_by_strategy": {
            "mean_reversion": 20,
            "trend_following": 0,
            "momentum": 0,
            "swing_trend_following": 0,
            "swing_momentum": 0,
            "swing_mean_reversion": 0,
            "intraday_trend": 20,
            "intraday_momentum": 20,
            "multi_factor": 0, "event_driven": 0,
            "opening_range_breakout": 15,
        },
        "cooldown_minutes_by_strategy": {
            "mean_reversion": 90,
            "trend_following": 5 * 24 * 60,
            "momentum": 5 * 24 * 60,
            "swing_trend_following": 5 * 24 * 60,
            "swing_momentum": 5 * 24 * 60,
            "swing_mean_reversion": 5 * 24 * 60,
            "intraday_trend": 60,
            "intraday_momentum": 60,
            "multi_factor": 5 * 24 * 60,
            "event_driven": 5 * 24 * 60,
            "opening_range_breakout": 60,
        },
        "max_entries_per_strategy_per_day": {
            "mean_reversion": 3,
            "trend_following": 1,
            "momentum": 1,
            "swing_trend_following": 1,
            "swing_momentum": 1,
            "multi_factor": 2,
            "swing_mean_reversion": 1,
            "intraday_trend": 1,
            "intraday_momentum": 1,
            "opening_range_breakout": 3,
        },
        "strategy_group_limits": {
            "swing": {
                "strategies": list(strategy_ids_for_group("swing")),
                "max_open_positions": 10, "max_entries_per_day": ALGO_ENTRY_LIMITS.max_swing_entries,
            },
            "intraday_core": {
                "strategies": list(strategy_ids_for_group("intraday_core")),
                "max_open_positions": 6, "max_entries_per_day": ALGO_ENTRY_LIMITS.max_intraday_core_entries,
            },
            "intraday_satellites": {
                "strategies": list(strategy_ids_for_group("intraday_satellites")),
                "max_open_positions": 2, "max_entries_per_day": ALGO_ENTRY_LIMITS.max_intraday_satellite_entries,
            },
        },
        "one_orb_attempt_per_ticker_per_day": True,
        "min_cash_pct": 0.15,
        "max_tactical_pct": 0.04,
        "max_tactical_positions": 2,
        "flatten_eod": False,
        "allow_leveraged_etfs": False,
    },
}

# Portfolio-level coordinator is deliberately stricter than risk_manager.
# Risk-manager limits remain the final approval layer after coordinator sizing.
ALGO_PORTFOLIO_COORDINATOR_CONFIG = {
    "max_name_pct": 0.08,
    "max_gross_pct": 1.00,
    "max_net_long_pct": 0.60,
    "max_net_short_pct": 0.40,
    "max_daily_entries": ALGO_ENTRY_LIMITS.max_daily_entries,
    "max_intraday_entries": ALGO_ENTRY_LIMITS.max_intraday_entries,
    "max_swing_entries": ALGO_ENTRY_LIMITS.max_swing_entries,
    "swing_open_delay_minutes": 15,
    "intraday_close_cutoff_minutes": 15,
    "allow_opposite_netting": False,
    "allow_intraday_overnight": False,
    "regime_throttle_enabled": True,
    "sector_caps": {
        "core_tech": 0.30,
        "diversifiers": 0.25,
        "hedge": 0.25,
        "tactical": 0.15,
    },
}

ALGO_SERVICE_CONFIG = {
    "poll_seconds": 15,
    "state_file": "data/algo_state.json",
    "lock_file": "/run/lock/swarm-trader-algo.lock",
}


def get_algo_research_controls() -> dict:
    """Return the live portfolio limits that research replays must respect."""
    risk = ALGO_MODE_CONFIG["risk"]
    return {
        "max_entries_per_day": ALGO_ENTRY_LIMITS.max_daily_entries,
        "max_open_positions": risk["max_open_positions"],
        "max_positions_per_strategy": risk["max_positions_per_strategy"],
        "max_entries_per_strategy_per_day": deepcopy(risk["max_entries_per_strategy_per_day"]),
        "cooldown_minutes_by_strategy": deepcopy(risk["cooldown_minutes_by_strategy"]),
        "one_orb_attempt_per_ticker_per_day": risk["one_orb_attempt_per_ticker_per_day"],
    }
