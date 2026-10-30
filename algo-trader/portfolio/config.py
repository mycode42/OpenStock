"""Central configuration for the dedicated Algo trading service."""

from dataclasses import asdict, dataclass
from copy import deepcopy
import json
import math
from pathlib import Path

from portfolio.settings_rules import validate_settings
from runtime.paths import DATA_DIR, ROOT
from strategies.catalog import strategy_ids_for_group


@dataclass(frozen=True)
class AlgoRuntimeSettings:
    """Per-scan projection of strategy activation and session settings."""

    enable_intraday_trend_following: bool = True
    enable_intraday_momentum: bool = True
    swing_multi_factor_enabled: bool = True
    swing_llm_driven_enabled: bool = True

    def __post_init__(self) -> None:
        for name in (
            "enable_intraday_trend_following",
            "enable_intraday_momentum", "swing_multi_factor_enabled",
            "swing_llm_driven_enabled",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be boolean")
    def as_dict(self) -> dict:
        """Return the scan's resolved strategy and session switches."""
        return asdict(self)
ALGO_STRATEGY_DEFAULTS_PATH = Path(__file__).with_name("algo-strategy-config.default.json")
ALGO_STRATEGY_CONFIG_PATH = DATA_DIR / "algo-strategy-config.json"
# Static defaults for module-level structures; get_mode_config() applies overrides.
_PORTFOLIO_DEFAULTS = json.loads(ALGO_STRATEGY_DEFAULTS_PATH.read_text(encoding="utf-8"))["portfolio"]
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
def _load_candidate_categories() -> dict:
    payload = json.loads(ALGO_CANDIDATE_POOL_PATH.read_text(encoding="utf-8"))
    categories = payload.get("categories") or {}
    if not isinstance(categories, dict) or not categories:
        raise ValueError(f"candidate pool has no categories: {ALGO_CANDIDATE_POOL_PATH}")
    return {
        category: [str(ticker).strip().upper() for ticker in tickers]
        for category, tickers in categories.items()
    }


_CANDIDATE_CATEGORIES = _load_candidate_categories()


def get_candidate_universe() -> dict:
    """Pool categories with the effective per-sector and per-stock caps."""
    portfolio = _effective_policy()["portfolio"]
    return {
        category: {
            "label": category.replace("_", " ").title(),
            "tickers": list(tickers),
            "max_sector_pct": float(portfolio["candidate_sector_cap"]),
            "max_per_stock_pct": float(portfolio["candidate_per_stock_cap"]),
        }
        for category, tickers in _CANDIDATE_CATEGORIES.items()
    }

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
        "max_position_pct": _PORTFOLIO_DEFAULTS["max_position_pct"],
        # Strategy sizing cap used across Algo's holding-period groups.
        "max_strategy_position_pct": _PORTFOLIO_DEFAULTS["max_strategy_position_pct"],
        "max_sector_pct": max(_PORTFOLIO_DEFAULTS["sector_caps"].values()),
        # Conservative fallback plus explicit horizon-specific protection.
        "stop_loss_pct": 0.05,
        "trailing_stop_pct": 0.04,
        "trailing_stop_pct_by_strategy": {
            "intraday_mean_reversion": 0.008,
            "swing_trend_following": 0.045,
            "swing_momentum": 0.045,
            "swing_multi_factor": 0.045,
            "swing_llm_driven": 0.03,
            "swing_mean_reversion": 0.05,
            "intraday_trend_following": 0.008,
            "intraday_momentum": 0.008,
        },
        "trailing_activation_pct_by_strategy": {
            "intraday_mean_reversion": 0.005,
            "swing_trend_following": 0.02,
            "swing_momentum": 0.02,
            "swing_multi_factor": 0.02,
            "swing_llm_driven": 0.02,
            "swing_mean_reversion": 0.02,
            "intraday_trend_following": 0.005,
            "intraday_momentum": 0.005,
        },
        "daily_loss_limit": _PORTFOLIO_DEFAULTS["daily_loss_limit_pct"],
        "weekly_loss_limit": _PORTFOLIO_DEFAULTS["weekly_loss_limit_pct"],
        "no_buy_if_down_pct": _PORTFOLIO_DEFAULTS["no_buy_if_down_pct"],
        "max_trades_per_day": _PORTFOLIO_DEFAULTS["max_daily_entries"],
        "max_open_positions": _PORTFOLIO_DEFAULTS["max_open_positions"],
        "max_positions_per_strategy": _PORTFOLIO_DEFAULTS["max_positions_per_strategy"],
        "max_positions_per_strategy_by_strategy": {
            "swing_trend_following": 7, "swing_momentum": 7, "swing_mean_reversion": 7,
            "swing_multi_factor": 7, "swing_llm_driven": 7, "intraday_mean_reversion": 7,
            "intraday_trend_following": 7, "intraday_momentum": 7,
        },
        "max_short_positions": _PORTFOLIO_DEFAULTS["max_short_positions"],
        "max_short_exposure_pct": _PORTFOLIO_DEFAULTS["max_short_exposure_pct"],
        "range_regime_short_risk_multiplier": 0.75,
        "minimum_hold_minutes_by_strategy": {
            "intraday_mean_reversion": 20,
            "swing_trend_following": 0,
            "swing_momentum": 0,
            "swing_mean_reversion": 0,
            "intraday_trend_following": 20,
            "intraday_momentum": 20,
            "swing_multi_factor": 0, "swing_llm_driven": 0,
        },
        "cooldown_minutes_by_strategy": {
            "intraday_mean_reversion": 90,
            "swing_trend_following": 5 * 24 * 60,
            "swing_momentum": 5 * 24 * 60,
            "swing_mean_reversion": 5 * 24 * 60,
            "intraday_trend_following": 60,
            "intraday_momentum": 60,
            "swing_multi_factor": 5 * 24 * 60,
            "swing_llm_driven": 5 * 24 * 60,
        },
        "max_entries_per_strategy_per_day": {
            "intraday_mean_reversion": 2,
            "swing_trend_following": 3,
            "swing_momentum": 3,
            "swing_mean_reversion": 4,
            "intraday_trend_following": 2,
            "intraday_momentum": 2,
            "swing_multi_factor": 3,
            "swing_llm_driven": 2,
        },
        "strategy_group_limits": {
            "swing": {
                "strategies": list(strategy_ids_for_group("swing")),
                "max_entries_per_day": _PORTFOLIO_DEFAULTS["max_swing_entries"],
            },
            "intraday_core": {
                "strategies": list(strategy_ids_for_group("intraday_core")),
                "max_entries_per_day": _PORTFOLIO_DEFAULTS["max_intraday_core_entries"],
            },
        },
        "min_cash_pct": _PORTFOLIO_DEFAULTS["min_cash_pct"],
        "max_tactical_pct": _PORTFOLIO_DEFAULTS["max_tactical_pct"],
        "max_tactical_positions": _PORTFOLIO_DEFAULTS["max_tactical_positions"],
        "flatten_eod": False,
    },
}

# The coordinator pre-sizes against the regime's gross budget; execution.risk
# remains the final check on configured name, sector, cash, and trade limits.
ALGO_PORTFOLIO_COORDINATOR_CONFIG = {
    "max_name_pct": _PORTFOLIO_DEFAULTS["max_name_pct"],
    # Shared equity-based exposure ceiling; preserves the configured cash floor.
    "max_gross_pct": 1.0 - _PORTFOLIO_DEFAULTS["min_cash_pct"],
    "max_net_long_pct": 1.0 - _PORTFOLIO_DEFAULTS["min_cash_pct"],
    "max_net_short_pct": _PORTFOLIO_DEFAULTS["max_net_short_pct"],
    "max_daily_entries": _PORTFOLIO_DEFAULTS["max_daily_entries"],
    "max_intraday_entries": _PORTFOLIO_DEFAULTS["max_intraday_entries"],
    "max_swing_entries": _PORTFOLIO_DEFAULTS["max_swing_entries"],
    "intraday_close_cutoff_minutes": _PORTFOLIO_DEFAULTS["intraday_close_cutoff_minutes"],
    "allow_opposite_netting": _PORTFOLIO_DEFAULTS["allow_opposite_netting"],
    "regime_throttle_enabled": _PORTFOLIO_DEFAULTS["regime_throttle_enabled"],
    "regime_gross_caps": dict(_PORTFOLIO_DEFAULTS["regime_gross_caps"]),
    "sector_caps": dict(_PORTFOLIO_DEFAULTS["sector_caps"]),
}

# Idle cash is parked in a cash-proxy ETF. The proxy is invisible to the risk
# gate, entry gates, exposure, and reconciliation; see ``is_cash_proxy``.
def is_cash_proxy(symbol: object) -> bool:
    proxy = str(get_cash_reserve_settings()["proxy_symbol"]).upper()
    return str(symbol or "").upper() == proxy


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

def quote_stale_limit_seconds(session_name: str | None) -> int:
    """Quote freshness window for a session; extended sessions use the wider limit."""
    execution = get_algo_execution_settings()
    if session_name in {"premarket", "after_hours", "overnight"}:
        return int(execution["extended_quote_stale_seconds"])
    return int(execution["quote_stale_seconds"])


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
    merged = _merge_configuration(defaults, overrides)
    validate_settings(merged, defaults)
    return merged


_POLICY_CACHE: dict = {}


def _effective_policy() -> dict:
    """Effective editable settings, re-read when the defaults or override file changes."""
    stamp = tuple(
        (str(path), path.stat().st_mtime_ns if path.exists() else None)
        for path in (ALGO_STRATEGY_DEFAULTS_PATH, ALGO_STRATEGY_CONFIG_PATH)
    )
    if _POLICY_CACHE.get("stamp") != stamp:
        _POLICY_CACHE["value"] = get_algo_configuration()
        _POLICY_CACHE["stamp"] = stamp
    return _POLICY_CACHE["value"]


def get_algo_execution_settings() -> dict:
    """Scan cadence, quote freshness, and entry-execution settings for the live engine."""
    return dict(_effective_policy()["execution"])


def get_signal_data_settings() -> dict:
    """Signal-bar freshness limits for the live scan."""
    return dict(_effective_policy()["signal_data"])


def get_cash_reserve_settings() -> dict:
    """Idle-cash reserve settings; the proxy symbol also defines what counts as a cash proxy."""
    return dict(_effective_policy()["cash_reserve"])


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
        # Route weights pick eligible strategies; risk_scale alone sets their size.
        "strategy_risk_scales": {
            name: float(settings.get("risk_scale", 1.0))
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
        "intraday_close_cutoff_minutes": portfolio["intraday_close_cutoff_minutes"],
        "allow_opposite_netting": portfolio["allow_opposite_netting"],
        "regime_throttle_enabled": portfolio["regime_throttle_enabled"],
        "regime_gross_caps": dict(portfolio["regime_gross_caps"]),
        "sector_caps": dict(portfolio["sector_caps"]),
    }


def get_algo_runtime_settings() -> AlgoRuntimeSettings:
    """Project common scan switches from the editable per-strategy configuration."""
    config = get_algo_configuration()
    strategies = config["strategies"]
    return AlgoRuntimeSettings(
        enable_intraday_trend_following=strategies["intraday_trend_following"]["enabled"],
        enable_intraday_momentum=strategies["intraday_momentum"]["enabled"],
        swing_multi_factor_enabled=strategies["swing_multi_factor"]["enabled"],
        swing_llm_driven_enabled=strategies["swing_llm_driven"]["enabled"],
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
        "max_tactical_pct": portfolio["max_tactical_pct"],
        "max_tactical_positions": portfolio["max_tactical_positions"],
        "flatten_eod": portfolio["flatten_eod"],
        "range_regime_short_risk_multiplier": portfolio["range_regime_short_risk_multiplier"],
        "hard_to_borrow_short_risk_multiplier": portfolio["hard_to_borrow_short_risk_multiplier"],
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
    result["candidate_universe"] = get_candidate_universe()
    return result


def candidate_pool_tickers() -> list[str]:
    """Every ticker in the equity candidate pool, in pool order."""
    seen: list[str] = []
    for bucket in get_candidate_universe().values():
        for ticker in bucket["tickers"]:
            if ticker not in seen:
                seen.append(ticker)
    return seen


def risk_universe(mode_config: dict) -> dict:
    """Ticker catalog for risk caps: static categories win over pool categories."""
    return {**mode_config.get("candidate_universe", {}), **mode_config["universe"]}
