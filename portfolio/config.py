"""Central configuration for the dedicated Algo trading service."""

from dataclasses import asdict, dataclass
from copy import deepcopy
import json
import math
from pathlib import Path

from runtime.paths import DATA_DIR, ROOT
from strategies.catalog import strategy_ids_for_group


@dataclass(frozen=True)
class AlgoEntryLimits:
    """Shared entry budgets consumed by live coordination and research replay."""

    max_daily_entries: int = 10
    max_intraday_entries: int = 4
    max_swing_entries: int = 6
    max_intraday_core_entries: int = 4


ALGO_ENTRY_LIMITS = AlgoEntryLimits()
ALGO_MIN_CASH_PCT = 0.10


@dataclass(frozen=True)
class AlgoRuntimeSettings:
    """Per-scan projection of strategy activation and session settings."""

    overnight_entries_enabled: bool = True
    enable_intraday_trend_following: bool = True
    enable_intraday_momentum: bool = True
    swing_multi_factor_enabled: bool = True
    swing_event_driven_enabled: bool = True

    def __post_init__(self) -> None:
        for name in (
            "overnight_entries_enabled", "enable_intraday_trend_following",
            "enable_intraday_momentum", "swing_multi_factor_enabled",
            "swing_event_driven_enabled",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be boolean")
    def as_dict(self) -> dict:
        """Return the scan's resolved strategy and session switches."""
        return asdict(self)
ALGO_STRATEGY_DEFAULTS_PATH = Path(__file__).with_name("algo-strategy-config.default.json")
ALGO_STRATEGY_CONFIG_PATH = DATA_DIR / "algo-strategy-config.json"
ALGO_SIGNAL_DATA_CONFIG = {
    "signal_bar_max_age_minutes": 20,
    "delayed_signal_bar_max_age_minutes": 20,
    # During the BOATS overnight session, swing-horizon signals may use the
    # last completed 5-minute bar as context for up to two hours. Intraday
    # strategies and all quote-based execution/protection checks keep their
    # normal freshness limits.
    "overnight_swing_signal_bar_max_age_minutes": 120,
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
        "tickers": ["PLTR", "CRM", "PANW", "TSLA", "SPCX"],
        "max_sector_pct": 0.10,
        "max_per_stock_pct": 0.07,
    },
}
# The equity candidate pool is the 117-name research universe. The live universe
# is the selector's eligible subset of it. Static names above keep their own
# categories and caps; pool-only names use these pool categories.
ALGO_CANDIDATE_POOL_PATH = ROOT / "options" / "research" / "universe.json"
ALGO_CANDIDATE_SECTOR_CAP = 0.30
ALGO_CANDIDATE_PER_STOCK_CAP = 0.07


def _load_candidate_universe() -> dict:
    payload = json.loads(ALGO_CANDIDATE_POOL_PATH.read_text(encoding="utf-8"))
    categories = payload.get("categories") or {}
    if not isinstance(categories, dict) or not categories:
        raise ValueError(f"candidate pool has no categories: {ALGO_CANDIDATE_POOL_PATH}")
    return {
        category: {
            "label": category.replace("_", " ").title(),
            "tickers": [str(ticker).strip().upper() for ticker in tickers],
            "max_sector_pct": ALGO_CANDIDATE_SECTOR_CAP,
            "max_per_stock_pct": ALGO_CANDIDATE_PER_STOCK_CAP,
        }
        for category, tickers in categories.items()
    }


ALGO_CANDIDATE_UNIVERSE = _load_candidate_universe()
ALGO_FACTOR_SECTOR_BY_SYMBOL = {
    "NVDA": "information_technology", "AVGO": "information_technology",
    "TSM": "information_technology", "MSFT": "information_technology",
    "AAPL": "information_technology", "AMD": "information_technology",
    "PLTR": "information_technology", "CRM": "information_technology",
    "PANW": "information_technology", "GOOGL": "communication_services",
    "META": "communication_services", "AMZN": "consumer_discretionary",
    "TSLA": "consumer_discretionary", "SPCX": "industrials",
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
            "intraday_mean_reversion": 0.011,
            "swing_trend_following": 0.06,
            "swing_momentum": 0.06,
            "swing_multi_factor": 0.06,
            "swing_event_driven": 0.04,
            "swing_mean_reversion": 0.07,
            "intraday_trend_following": 0.011,
            "intraday_momentum": 0.011,
        },
        "trailing_stop_pct_by_strategy": {
            "intraday_mean_reversion": 0.008,
            "swing_trend_following": 0.045,
            "swing_momentum": 0.045,
            "swing_multi_factor": 0.045,
            "swing_event_driven": 0.03,
            "swing_mean_reversion": 0.05,
            "intraday_trend_following": 0.008,
            "intraday_momentum": 0.008,
        },
        "trailing_activation_pct_by_strategy": {
            "intraday_mean_reversion": 0.005,
            "swing_trend_following": 0.02,
            "swing_momentum": 0.02,
            "swing_multi_factor": 0.02,
            "swing_event_driven": 0.02,
            "swing_mean_reversion": 0.02,
            "intraday_trend_following": 0.005,
            "intraday_momentum": 0.005,
        },
        "daily_loss_limit": 0.02,
        "weekly_loss_limit": 0.05,
        "no_buy_if_down_pct": 0.02,
        "max_trades_per_day": ALGO_ENTRY_LIMITS.max_daily_entries,
        "max_open_positions": 18,
        "max_positions_per_strategy": 7,
        "max_positions_per_strategy_by_strategy": {
            "swing_trend_following": 7, "swing_momentum": 7, "swing_mean_reversion": 7,
            "swing_multi_factor": 7, "swing_event_driven": 7, "intraday_mean_reversion": 7,
            "intraday_trend_following": 7, "intraday_momentum": 7,
        },
        "max_short_positions": 5,
        "max_short_exposure_pct": 0.20,
        "allow_short_positions": True,
        "range_regime_short_risk_multiplier": 0.75,
        "minimum_hold_minutes_by_strategy": {
            "intraday_mean_reversion": 20,
            "swing_trend_following": 0,
            "swing_momentum": 0,
            "swing_mean_reversion": 0,
            "intraday_trend_following": 20,
            "intraday_momentum": 20,
            "swing_multi_factor": 0, "swing_event_driven": 0,
        },
        "cooldown_minutes_by_strategy": {
            "intraday_mean_reversion": 90,
            "swing_trend_following": 5 * 24 * 60,
            "swing_momentum": 5 * 24 * 60,
            "swing_mean_reversion": 5 * 24 * 60,
            "intraday_trend_following": 60,
            "intraday_momentum": 60,
            "swing_multi_factor": 5 * 24 * 60,
            "swing_event_driven": 5 * 24 * 60,
        },
        "max_entries_per_strategy_per_day": {
            "intraday_mean_reversion": 4,
            "swing_trend_following": 2,
            "swing_momentum": 2,
            "swing_multi_factor": 2,
            "swing_event_driven": 5,
            "swing_mean_reversion": 2,
            "intraday_trend_following": 1,
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
        },
        "min_cash_pct": ALGO_MIN_CASH_PCT,
        "max_tactical_pct": 0.10,
        "max_tactical_positions": 3,
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
    "strategy_policy": "configured",
    "include_models": True,
    "min_model_confidence": 0.50,
    "model_training_window": 756,
}

ALGO_SERVICE_CONFIG = {
    "poll_seconds": 15,
    "state_db": "data/algo_state.sqlite3",
    "lock_file": "/run/lock/algo-trader.lock",
}

def get_algo_research_controls() -> dict:
    """Return the live portfolio limits that research replays must respect."""
    risk = get_mode_config("algo")["risk"]
    return {
        "max_entries_per_day": risk["max_trades_per_day"],
        "max_open_positions": risk["max_open_positions"],
        "max_positions_per_strategy": risk["max_positions_per_strategy"],
        "max_entries_per_strategy_per_day": deepcopy(risk["max_entries_per_strategy_per_day"]),
        "cooldown_minutes_by_strategy": deepcopy(risk["cooldown_minutes_by_strategy"]),
    }

STRATEGY_CONFIG = {
    "portfolio_scan_seconds": 60,
    "universe_retry_seconds": 300,
    "quote_stale_seconds": 90,
    # Overnight and extended-hours quotes are indicative and sparse; a 90s window
    # paused entries and skipped protective updates on live Alpaca data.
    "extended_quote_stale_seconds": 300,
    "extended_hours_max_spread_pct": 0.02,
    "risk_per_trade_pct": 0.005,
    "max_strategy_position_pct": 0.05,
    "take_profit_r_multiple": 2.0,
}


def quote_stale_limit_seconds(session_name: str | None) -> int:
    """Quote freshness window for a session; extended sessions use the wider limit."""
    if session_name in {"premarket", "after_hours", "overnight"}:
        return int(STRATEGY_CONFIG["extended_quote_stale_seconds"])
    return int(STRATEGY_CONFIG["quote_stale_seconds"])


def _merge_configuration(base: dict, override: dict) -> dict:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_configuration(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def get_algo_configuration() -> dict:
    """Load the canonical editable live configuration, falling back to defaults."""
    try:
        defaults = json.loads(ALGO_STRATEGY_DEFAULTS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Algo strategy defaults are unreadable") from exc
    if not isinstance(defaults, dict):
        raise ValueError("Algo strategy defaults must be an object")
    if not ALGO_STRATEGY_CONFIG_PATH.exists():
        return defaults
    try:
        overrides = json.loads(ALGO_STRATEGY_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Algo strategy configuration is unreadable") from exc
    if not isinstance(overrides, dict):
        raise ValueError("Algo strategy configuration must be an object")
    return _merge_configuration(defaults, overrides)


def get_algo_strategy_settings(strategy: str) -> dict:
    """Return the effective editable policy and sizing for one catalog strategy."""
    from strategies.catalog import canonical_strategy_id

    strategy = canonical_strategy_id(strategy)
    settings = get_algo_configuration()["strategies"].get(strategy)
    if settings is None:
        raise KeyError(f"Unknown Algo strategy: {strategy}")
    return settings


def get_algo_regime_config() -> dict:
    """Return effective regime classification and per-strategy routing settings."""
    config = get_algo_configuration()
    general = config["general"]
    return {
        "engine": general["regime_engine"],
        "feature_set": general["regime_feature_set"],
        "strategy_policy": "configured",
        "include_models": general["model_diagnostics_enabled"],
        "min_model_confidence": general["model_min_confidence"],
        "model_training_window": general["model_training_window"],
        "strategy_regime_weights": {
            name: dict(settings["regime_weights"])
            for name, settings in config["strategies"].items()
        },
        "strategy_enabled": {
            name: bool(settings["enabled"])
            for name, settings in config["strategies"].items()
        },
        "volatile_policy_risk_multiplier": general["volatile_policy_risk_multiplier"],
        "volatility_risk_scales": dict(general["volatility_risk_scales"]),
    }


def get_algo_portfolio_coordinator_config() -> dict:
    """Return the effective settings used by the scan-local exposure coordinator."""
    portfolio = get_algo_configuration()["portfolio"]
    return {
        "max_name_pct": portfolio["max_name_pct"],
        "max_gross_pct": portfolio["max_gross_pct"],
        "max_net_long_pct": portfolio["max_net_long_pct"],
        "max_net_short_pct": portfolio["max_net_short_pct"],
        "max_daily_entries": portfolio["max_daily_entries"],
        "max_intraday_entries": portfolio["max_intraday_entries"],
        "max_swing_entries": portfolio["max_swing_entries"],
        "swing_open_delay_minutes": portfolio["swing_open_delay_minutes"],
        "intraday_close_cutoff_minutes": portfolio["intraday_close_cutoff_minutes"],
        "allow_opposite_netting": portfolio["allow_opposite_netting"],
        "allow_intraday_overnight": portfolio["allow_intraday_overnight"],
        "regime_throttle_enabled": portfolio["regime_throttle_enabled"],
        "regime_gross_caps": dict(portfolio["regime_gross_caps"]),
        "sector_caps": dict(portfolio["sector_caps"]),
    }


def get_algo_runtime_settings() -> AlgoRuntimeSettings:
    """Project common scan switches from the editable per-strategy configuration."""
    config = get_algo_configuration()
    strategies = config["strategies"]
    return AlgoRuntimeSettings(
        overnight_entries_enabled=config["general"]["overnight_entries_enabled"],
        enable_intraday_trend_following=strategies["intraday_trend_following"]["enabled"],
        enable_intraday_momentum=strategies["intraday_momentum"]["enabled"],
        swing_multi_factor_enabled=strategies["swing_multi_factor"]["enabled"],
        swing_event_driven_enabled=strategies["swing_event_driven"]["enabled"],
    )


def resolve_mode(cli_mode: str | None = None) -> str:
    """Resolve the only supported account route: the Algo paper account."""
    if cli_mode is not None and cli_mode.lower() != "algo":
        raise ValueError("Only the algo account route is supported")
    return "algo"


def get_mode_config(mode: str | None = None) -> dict:
    """Return effective Algo settings; reject removed Swing and Day account routes."""
    resolve_mode(mode)
    config = get_algo_configuration()
    general = config["general"]
    portfolio = config["portfolio"]
    result = deepcopy(ALGO_MODE_CONFIG)
    risk = result["risk"]
    risk.update({
        "max_position_pct": portfolio["max_position_pct"],
        "max_strategy_position_pct": portfolio["max_strategy_position_pct"],
        "max_sector_pct": max(portfolio["sector_caps"].values()),
        "daily_loss_limit": portfolio["daily_loss_limit_pct"],
        "weekly_loss_limit": portfolio["weekly_loss_limit_pct"],
        "no_buy_if_down_pct": portfolio["no_buy_if_down_pct"],
        "max_trades_per_day": portfolio["max_daily_entries"],
        "max_open_positions": portfolio["max_open_positions"],
        "max_positions_per_strategy": portfolio["max_positions_per_strategy"],
        "max_short_positions": portfolio["max_short_positions"],
        "max_short_exposure_pct": portfolio["max_short_exposure_pct"],
        "allow_short_positions": portfolio["allow_short_positions"],
        "max_tactical_pct": portfolio["max_tactical_pct"],
        "max_tactical_positions": portfolio["max_tactical_positions"],
        "flatten_eod": portfolio["flatten_eod"],
        "allow_leveraged_etfs": portfolio["allow_leveraged_etfs"],
        "range_regime_short_risk_multiplier": portfolio["range_regime_short_risk_multiplier"],
        "min_cash_pct": portfolio["min_cash_pct"],
        "risk_per_trade_pct": general["risk_per_trade_pct"],
        "take_profit_r_multiple": general["take_profit_r_multiple"],
        "stop_loss_pct_by_strategy": {},
        "trailing_stop_pct_by_strategy": {},
        "trailing_activation_pct_by_strategy": {},
        "max_entries_per_strategy_per_day": {},
        "max_positions_per_strategy_by_strategy": {},
        "max_position_pct_by_strategy": {},
        "risk_multiplier_by_strategy": {},
        "minimum_hold_minutes_by_strategy": {},
        "cooldown_minutes_by_strategy": {},
    })
    for strategy, settings in config["strategies"].items():
        risk["stop_loss_pct_by_strategy"][strategy] = settings["hard_stop_pct"]
        risk["trailing_stop_pct_by_strategy"][strategy] = settings["trailing_stop_pct"]
        risk["trailing_activation_pct_by_strategy"][strategy] = settings["trailing_activation_pct"]
        risk["max_entries_per_strategy_per_day"][strategy] = settings["max_entries_per_day"]
        risk["max_positions_per_strategy_by_strategy"][strategy] = min(
            settings["max_positions"], portfolio["max_positions_per_strategy"],
        )
        risk["max_position_pct_by_strategy"][strategy] = settings["max_position_pct"]
        risk["risk_multiplier_by_strategy"][strategy] = settings["risk_multiplier"]
        risk["minimum_hold_minutes_by_strategy"][strategy] = settings["minimum_hold_minutes"]
        risk["cooldown_minutes_by_strategy"][strategy] = settings["cooldown_minutes"]
    risk["strategy_group_limits"] = {
        "swing": {
            "strategies": list(strategy_ids_for_group("swing")),
            "max_entries_per_day": portfolio["max_swing_entries"],
        },
        "intraday_core": {
            "strategies": list(strategy_ids_for_group("intraday_core")),
            "max_entries_per_day": portfolio["max_intraday_core_entries"],
        },
    }
    result["coordinator"] = get_algo_portfolio_coordinator_config()
    result["coordinator"]["max_daily_entries"] = portfolio["max_daily_entries"]
    result["coordinator"]["max_intraday_entries"] = portfolio["max_intraday_entries"]
    result["coordinator"]["max_swing_entries"] = portfolio["max_swing_entries"]
    universe = deepcopy(ALGO_PORTFOLIO_UNIVERSE)
    for group, limits in universe.items():
        limits["max_sector_pct"] = portfolio["sector_caps"].get(
            group, limits["max_sector_pct"],
        )
        limits["max_per_stock_pct"] = portfolio["max_position_pct"]
    result["universe"] = universe
    result["candidate_universe"] = deepcopy(ALGO_CANDIDATE_UNIVERSE)
    return result


def candidate_pool_tickers() -> list[str]:
    """Every ticker in the equity candidate pool, in pool order."""
    seen: list[str] = []
    for bucket in ALGO_CANDIDATE_UNIVERSE.values():
        for ticker in bucket["tickers"]:
            if ticker not in seen:
                seen.append(ticker)
    return seen


def risk_universe(mode_config: dict) -> dict:
    """Ticker catalog for risk caps: static categories win over pool categories."""
    return {**mode_config.get("candidate_universe", {}), **mode_config["universe"]}
