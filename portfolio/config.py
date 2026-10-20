"""Central configuration for the dedicated Algo trading service."""

from dataclasses import asdict, dataclass
from copy import deepcopy
import math

from strategies.catalog import strategy_ids_for_group


@dataclass(frozen=True)
class AlgoEntryLimits:
    """Shared entry budgets consumed by live coordination and research replay."""

    max_daily_entries: int = 10
    max_intraday_entries: int = 4
    max_swing_entries: int = 6
    max_intraday_core_entries: int = 4
    max_intraday_satellite_entries: int = 2


ALGO_ENTRY_LIMITS = AlgoEntryLimits()
ALGO_MIN_CASH_PCT = 0.10


@dataclass(frozen=True)
class AlgoRuntimeSettings:
    """Typed source of truth for settings specific to the Algo service."""

    overnight_entries_enabled: bool = True
    enable_intraday_trend: bool = False
    enable_intraday_momentum: bool = False
    intraday_satellite_position_pct: float = 0.01
    intraday_satellite_risk_multiplier: float = 0.25
    algo_multi_factor_enabled: bool = True
    algo_event_driven_enabled: bool = True

    def __post_init__(self) -> None:
        for name in (
            "overnight_entries_enabled", "enable_intraday_trend",
            "enable_intraday_momentum", "algo_multi_factor_enabled",
            "algo_event_driven_enabled",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be boolean")
        for name in (
            "intraday_satellite_position_pct", "intraday_satellite_risk_multiplier",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive finite number")

    def as_legacy_dict(self) -> dict:
        """Return the compatibility mapping consumed by older integrations."""
        return asdict(self)


ALGO_RUNTIME_SETTINGS = AlgoRuntimeSettings()
# Compatibility projection for callers that still consume runtime mappings.
ALGO_RUNTIME_CONFIG = ALGO_RUNTIME_SETTINGS.as_legacy_dict()
ALGO_SIGNAL_DATA_CONFIG = {
    "signal_bar_max_age_minutes": 20,
    "delayed_signal_bar_max_age_minutes": 20,
    "max_spy_daily_age_days": 5,
}

# Keep enough completed sessions to support rolling percentiles and monthly
# HMM/GMM fits on roughly three years of prior observations.
ALGO_REGIME_WARMUP_CALENDAR_DAYS = 1400

# Algo's fixed portfolio universe used by live coordination and research.
ALGO_PORTFOLIO_UNIVERSE = {
    "core_tech": {
        "label": "Mega-liquid Tech / Growth",
        "tickers": ["NVDA", "AVGO", "TSM", "MSFT", "AAPL", "GOOGL", "META", "AMZN", "AMD"],
        "max_sector_pct": 0.40,
        "max_per_stock_pct": 0.07,
    },
    "diversifiers": {
        "label": "High-quality Non-Tech / Diversifiers",
        "tickers": ["JPM", "V", "UNH", "JNJ", "PG", "KO"],
        "max_sector_pct": 0.30,
        "max_per_stock_pct": 0.07,
    },
    "hedge": {
        "label": "Broad Market & Hedge ETFs",
        "tickers": ["SPY", "QQQ", "GLD", "TLT"],
        "max_sector_pct": 0.20,
        "max_per_stock_pct": 0.07,
    },
    "tactical": {
        "label": "Optional Tactical Satellite",
        "tickers": ["PLTR", "CRM", "PANW"],
        "max_sector_pct": 0.04,
        "max_per_stock_pct": 0.02,
    },
}
ALGO_FACTOR_SECTOR_BY_SYMBOL = {
    "NVDA": "information_technology", "AVGO": "information_technology",
    "TSM": "information_technology", "MSFT": "information_technology",
    "AAPL": "information_technology", "AMD": "information_technology",
    "PLTR": "information_technology", "CRM": "information_technology",
    "PANW": "information_technology", "GOOGL": "communication_services",
    "META": "communication_services", "AMZN": "consumer_discretionary",
    "JPM": "financials", "V": "financials", "UNH": "health_care",
    "JNJ": "health_care", "PG": "consumer_staples", "KO": "consumer_staples",
}
ALGO_MODE_CONFIG = {
    "label": "Autonomous Algo Portfolio",
    "universe": ALGO_PORTFOLIO_UNIVERSE,
    "risk": {
        "max_position_pct": 0.07,
        # Strategy sizing cap used across Algo's holding-period groups.
        "max_strategy_position_pct": 0.07,
        "max_sector_pct": 0.40,
        # Conservative fallback plus explicit horizon-specific protection.
        "stop_loss_pct": 0.05,
        "trailing_stop_pct": 0.04,
        "stop_loss_pct_by_strategy": {
            "mean_reversion": 0.011,
            "trend_following": 0.06,
            "momentum": 0.06,
            "swing_trend_following": 0.06,
            "swing_momentum": 0.06,
            "multi_factor": 0.06,
            "event_driven": 0.04,
            "swing_mean_reversion": 0.07,
            "intraday_trend": 0.011,
            "intraday_momentum": 0.011,
        },
        "trailing_stop_pct_by_strategy": {
            "mean_reversion": 0.008,
            "trend_following": 0.045,
            "momentum": 0.045,
            "swing_trend_following": 0.045,
            "swing_momentum": 0.045,
            "multi_factor": 0.045,
            "event_driven": 0.03,
            "swing_mean_reversion": 0.05,
            "intraday_trend": 0.008,
            "intraday_momentum": 0.008,
        },
        "trailing_activation_pct_by_strategy": {
            "mean_reversion": 0.005,
            "trend_following": 0.02,
            "momentum": 0.02,
            "swing_trend_following": 0.02,
            "swing_momentum": 0.02,
            "multi_factor": 0.02,
            "event_driven": 0.02,
            "swing_mean_reversion": 0.02,
            "intraday_trend": 0.005,
            "intraday_momentum": 0.005,
        },
        "daily_loss_limit": 0.02,
        "weekly_loss_limit": 0.05,
        "no_buy_if_down_pct": 0.02,
        "max_trades_per_day": ALGO_ENTRY_LIMITS.max_daily_entries,
        "max_open_positions": 18,
        "max_positions_per_strategy": 7,
        "max_positions_per_strategy_by_strategy": {
            "trend_following": 7, "momentum": 7, "swing_mean_reversion": 7,
            "swing_trend_following": 7, "swing_momentum": 7,
            "multi_factor": 7, "event_driven": 7, "mean_reversion": 7,
            "intraday_trend": 7, "intraday_momentum": 7,
        },
        "max_short_positions": 5,
        "max_short_exposure_pct": 0.20,
        "allow_short_positions": True,
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
        },
        "max_entries_per_strategy_per_day": {
            "mean_reversion": 4,
            "trend_following": 2,
            "momentum": 2,
            "swing_trend_following": 1,
            "swing_momentum": 1,
            "multi_factor": 2,
            "event_driven": 5,
            "swing_mean_reversion": 2,
            "intraday_trend": 1,
            "intraday_momentum": 1,
        },
        "strategy_group_limits": {
            "swing": {
                "strategies": list(strategy_ids_for_group("swing")),
                "max_entries_per_day": ALGO_ENTRY_LIMITS.max_swing_entries,
            },
            "intraday_core": {
                "strategies": list(strategy_ids_for_group("intraday_core")),
                "max_entries_per_day": ALGO_ENTRY_LIMITS.max_intraday_core_entries,
            },
            "intraday_satellites": {
                "strategies": list(strategy_ids_for_group("intraday_satellites")),
                "max_entries_per_day": ALGO_ENTRY_LIMITS.max_intraday_satellite_entries,
            },
        },
        "min_cash_pct": ALGO_MIN_CASH_PCT,
        "max_tactical_pct": 0.04,
        "max_tactical_positions": 2,
        "flatten_eod": False,
        "allow_leveraged_etfs": False,
    },
}

# The coordinator pre-sizes against the regime's gross budget; execution.risk
# remains the final check on configured name, sector, cash, and trade limits.
ALGO_PORTFOLIO_COORDINATOR_CONFIG = {
    "max_name_pct": 0.10,
    # Shared equity-based exposure ceiling; preserves the configured cash floor.
    "max_gross_pct": 1.0 - ALGO_MIN_CASH_PCT,
    "max_net_long_pct": 1.0 - ALGO_MIN_CASH_PCT,
    "max_net_short_pct": 0.40,
    "max_daily_entries": ALGO_ENTRY_LIMITS.max_daily_entries,
    "max_intraday_entries": ALGO_ENTRY_LIMITS.max_intraday_entries,
    "max_swing_entries": ALGO_ENTRY_LIMITS.max_swing_entries,
    "swing_open_delay_minutes": 15,
    "intraday_close_cutoff_minutes": 15,
    "allow_opposite_netting": False,
    "allow_intraday_overnight": False,
    "regime_throttle_enabled": True,
    "regime_gross_caps": {
        "trending_up:calm": 0.90,
        "trending_up:elevated": 0.78,
        "trending_up:extreme": 0.45,
        "trending_down:calm": 0.82,
        "trending_down:elevated": 0.70,
        "trending_down:extreme": 0.40,
        "range_bound:calm": 0.82,
        "range_bound:elevated": 0.65,
        "range_bound:extreme": 0.40,
        "volatile:extreme": 0.40,
        "ambiguous:calm": 0.55,
        "ambiguous:elevated": 0.40,
        "ambiguous:extreme": 0.30,
        "unknown:unknown": 0.50,
    },
    "sector_caps": {
        "core_tech": 0.40,
        "diversifiers": 0.30,
        "hedge": 0.20,
        "tactical": 0.04,
    },
}

ALGO_REGIME_CONFIG = {
    # The supervised feature rule controls entries. HMM/GMM remain side-by-side
    # diagnostics until forward replay establishes stable incremental value.
    "engine": "rule",
    "feature_set": "marketwide",
    "strategy_policy": "high_vol_mean_reversion",
    "include_models": True,
    "min_model_confidence": 0.50,
    "model_training_window": 756,
}

ALGO_SERVICE_CONFIG = {
    "poll_seconds": 15,
    "state_file": "data/algo_state.json",
    "lock_file": "/run/lock/algo-trader.lock",
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
    }

# Runtime defaults shared by the live portfolio and research workflows.
_ALGO_RUNTIME_DEFAULTS = ALGO_RUNTIME_SETTINGS.as_legacy_dict()
STRATEGY_CONFIG = {
    "portfolio_scan_seconds": 60,
    "universe_retry_seconds": 300,
    "quote_stale_seconds": 90,
    "extended_hours_max_spread_pct": 0.02,
    "regime_llm_advisor_enabled": True,
    "regime_llm_default_model": "gpt-6-luna",
    "regime_llm_timeout_seconds": 10,
    "risk_per_trade_pct": 0.005,
    "max_strategy_position_pct": 0.05,
    "take_profit_r_multiple": 2.0,
    **ALGO_RUNTIME_CONFIG,
}


def get_algo_runtime_settings() -> AlgoRuntimeSettings:
    """Return validated effective settings, including runtime overrides."""
    values = ALGO_RUNTIME_SETTINGS.as_legacy_dict()
    for config in (ALGO_RUNTIME_CONFIG, STRATEGY_CONFIG):
        values.update({
            key: config[key] for key, default in _ALGO_RUNTIME_DEFAULTS.items()
            if key in config and config[key] != default
        })
    return AlgoRuntimeSettings(**values)


def resolve_mode(cli_mode: str | None = None) -> str:
    """Resolve the only supported account route: the Algo paper account."""
    if cli_mode is not None and cli_mode.lower() != "algo":
        raise ValueError("Only the algo account route is supported")
    return "algo"


def get_mode_config(mode: str | None = None) -> dict:
    """Return Algo settings; reject removed Swing and Day account routes."""
    resolve_mode(mode)
    return ALGO_MODE_CONFIG
