"""Shared configuration for the AI Hedge Fund / Swarm Trader platform."""

import os

# ===========================================================================
# TRADING MODES — one source of truth per mode. All risk limits live here.
# Set via TRADING_MODE env var ("swing" or "day") or --mode CLI flag.
# ===========================================================================
MODES = {
    "swing": {
        "label": "Swing Trading (days to weeks)",
        "universe": {
            "core_tech": {
                "label": "Core Technology",
                "tickers": ["NVDA", "AVGO", "TSM", "MSFT", "AAPL", "GOOGL", "META", "AMZN"],
                "max_sector_pct": 0.30,
                "max_per_stock_pct": 0.08,
            },
            "growth": {
                "label": "Growth",
                "tickers": ["PLTR", "AMD", "CRM", "SNOW", "NET", "PANW"],
                "max_sector_pct": 0.25,
                "max_per_stock_pct": 0.08,
            },
            "value_dividend": {
                "label": "Value & Dividend",
                "tickers": ["JPM", "V", "UNH", "JNJ", "PG", "KO"],
                "max_sector_pct": 0.25,
                "max_per_stock_pct": 0.08,
            },
            "tactical": {
                "label": "Tactical",
                "tickers": ["COIN", "MSTR", "RKLB", "SMCI"],
                "max_sector_pct": 0.15,
                "max_per_stock_pct": 0.05,
            },
            "hedge": {
                "label": "Hedge / Direction",
                "tickers": ["SPY", "QQQ", "GLD", "TLT"],
                "max_sector_pct": 0.15,
                "max_per_stock_pct": 0.10,
            },
        },
        "risk": {
            "max_position_pct": 0.08,
            "max_sector_pct": 0.30,
            "stop_loss_pct": 0.07,         # -7% hard stop
            "trailing_stop_pct": 0.15,      # 15% from peak
            "daily_loss_limit": 0.02,       # -2% circuit breaker
            "weekly_loss_limit": 0.05,      # -5% circuit breaker
            "no_buy_if_down_pct": 0.03,     # no new buys if -3% today
            "max_trades_per_day": 4,
            "max_open_positions": 12,
            "min_cash_pct": 0.20,           # 20% cash reserve
            "max_tactical_pct": 0.03,       # 3% cap on high-risk / tactical bucket
            "flatten_eod": False,           # hold overnight
            "allow_leveraged_etfs": False,
        },
    },
    "day": {
        "label": "Day Trading (intraday only)",
        "universe": {
            "mega_cap": {
                "label": "Mega-Cap Liquid",
                "tickers": ["NVDA", "AVGO", "TSM", "AMD", "MSFT", "AAPL", "META", "GOOGL", "AMZN"],
                "max_sector_pct": 0.50,
                "max_per_stock_pct": 0.15,
            },
            "momentum": {
                "label": "Momentum",
                "tickers": ["PLTR", "COIN", "MSTR", "RKLB", "SMCI"],
                "max_sector_pct": 0.35,
                "max_per_stock_pct": 0.12,
            },
            "etf_direction": {
                "label": "ETF Direction / Hedge",
                "tickers": ["SPY", "QQQ", "TQQQ", "SOXL"],
                "max_sector_pct": 0.30,
                "max_per_stock_pct": 0.15,
            },
        },
        "risk": {
            "max_position_pct": 0.15,
            "max_sector_pct": 0.50,
            "stop_loss_pct": 0.012,         # -1.2% tight stop
            "trailing_stop_pct": 0.03,      # 3% trailing (intraday)
            "daily_loss_limit": 0.03,       # -3% circuit breaker
            "weekly_loss_limit": 0.08,      # -8% circuit breaker
            "no_buy_if_down_pct": 0.02,     # tighter -2% no-buy
            "max_trades_per_day": 20,       # more active trading
            "max_open_positions": 8,        # fewer but larger
            "min_cash_pct": 0.10,           # less cash needed (closing EOD)
            "max_tactical_pct": 1.0,        # no tactical cap (all positions are tactical)
            "flatten_eod": True,            # MUST flatten by 3:45 PM ET
            "flatten_by": "15:45",
            "allow_leveraged_etfs": True,   # OK intraday only
        },
    },
}


def resolve_mode(cli_mode: str = None) -> str:
    """
    Resolve the active trading mode. Priority:
      1. CLI flag (--mode swing|day) — explicit human/cron override
      2. trading_mode.json override (human sets override + expiry)
      3. trading_mode.json mode field ("auto", "swing", or "day")
      4. TRADING_MODE env var
      5. Default: "swing"

    When mode is "auto", returns "auto" — caller (Cassius) decides.
    """
    import json
    from datetime import datetime
    from pathlib import Path

    # 1. CLI override wins
    if cli_mode and cli_mode.lower() in MODES:
        return cli_mode.lower()

    # 2-3. Read mode file
    mode_file = Path(__file__).parent.parent / "trading_mode.json"
    if mode_file.exists():
        try:
            with open(mode_file) as f:
                mf = json.load(f)

            # Check override (human steering)
            override = mf.get("override")
            override_until = mf.get("override_until")
            if override and override in MODES:
                # Check expiry
                if override_until:
                    try:
                        expiry = datetime.fromisoformat(override_until)
                        if datetime.now() < expiry:
                            return override
                        # Expired — clear it
                        mf["override"] = None
                        mf["override_until"] = None
                        mf["last_updated"] = datetime.now().isoformat()
                        mf["updated_by"] = "system (override expired)"
                        with open(mode_file, "w") as f:
                            json.dump(mf, f, indent=2)
                    except (ValueError, TypeError):
                        return override  # Bad date, use override anyway
                else:
                    return override  # No expiry = permanent until cleared

            # Mode field
            mode = mf.get("mode", "swing")
            if mode in MODES or mode == "auto":
                return mode
        except (json.JSONDecodeError, OSError):
            pass

    # 4. Env var
    env_mode = os.environ.get("TRADING_MODE", "swing").lower()
    if env_mode in MODES or env_mode == "auto":
        return env_mode

    return "swing"


def set_mode(mode: str, reason: str = None, updated_by: str = "cassius",
             override: bool = False, override_hours: float = None) -> str:
    """
    Update trading_mode.json. Used by Cassius autonomously or by human.

    Args:
        mode:            "swing", "day", or "auto"
        reason:          Why the change was made
        updated_by:      "cassius", "human", etc.
        override:        If True, sets as override (takes priority over auto)
        override_hours:  Override duration in hours (None = permanent)

    Returns the resolved mode string.
    """
    import json
    from datetime import datetime, timedelta
    from pathlib import Path

    mode_file = Path(__file__).parent.parent / "trading_mode.json"

    try:
        with open(mode_file) as f:
            mf = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        mf = {}

    now = datetime.now()

    if override:
        mf["override"] = mode if mode in MODES else None
        if override_hours:
            mf["override_until"] = (now + timedelta(hours=override_hours)).isoformat()
        else:
            mf["override_until"] = None
    else:
        mf["mode"] = mode
        mf["override"] = None
        mf["override_until"] = None

    mf["last_mode_used"] = mode
    mf["last_mode_reason"] = reason
    mf["last_updated"] = now.isoformat()
    mf["updated_by"] = updated_by

    with open(mode_file, "w") as f:
        json.dump(mf, f, indent=2)

    return mode


def get_mode_config(mode: str = None) -> dict:
    """Get config for mode. Resolves via resolve_mode() if mode not specified."""
    if mode is None:
        mode = resolve_mode()
    if mode == "auto":
        mode = "swing"  # Safe fallback when auto hasn't been resolved by agent
    mode = mode.lower()
    if mode not in MODES:
        raise ValueError(f"Unknown mode '{mode}'. Valid: {list(MODES.keys())}")
    return MODES[mode]


# ===========================================================================
# Backward-compat aliases — DO NOT USE IN NEW CODE
# ===========================================================================

# V2 universe (swing mode) — use get_mode_config("swing")["universe"] in new code
UNIVERSE_V2 = MODES["swing"]["universe"]
ALL_V2_TICKERS = [t for cat in UNIVERSE_V2.values() for t in cat["tickers"]]

# Day trading universe — use get_mode_config("day")["universe"] in new code
DAY_TRADE_UNIVERSE = MODES["day"]["universe"]
ALL_DAY_TRADE_TICKERS = [t for cat in DAY_TRADE_UNIVERSE.values() for t in cat["tickers"]]

# V1 swing universe (legacy — has leveraged ETFs and moonshots; kept for compat only)
SWING_UNIVERSE = {
    "ai_infra": {
        "label": "AI Infrastructure",
        "tickers": ["NVDA", "AVGO", "SMCI", "TSM"],
        "target_pct": 0.40,
    },
    "leveraged_etfs": {
        "label": "Leveraged ETFs",
        "tickers": ["TQQQ", "SOXL", "UPRO"],
        "target_pct": 0.25,
    },
    "momentum": {
        "label": "Momentum Plays",
        "tickers": ["PLTR", "MSTR", "COIN", "RKLB"],
        "target_pct": 0.20,
    },
    "moonshots": {
        "label": "Moonshots",
        "tickers": ["IONQ", "RGTI", "SOUN", "LUNR"],
        "target_pct": 0.15,
    },
}
UNIVERSE = SWING_UNIVERSE           # backward-compat alias
ALL_UNIVERSE_TICKERS = [t for cat in SWING_UNIVERSE.values() for t in cat["tickers"]]
UNIVERSE_SIMPLE = {key: cat["tickers"] for key, cat in SWING_UNIVERSE.items()}

# ===========================================================================
# DEPRECATED global risk constants — use get_mode_config(mode)["risk"] instead
# ===========================================================================
MAX_POSITION_PCT            = 0.08   # DEPRECATED
MAX_SECTOR_PCT              = 0.30   # DEPRECATED
STOP_LOSS_PCT               = 0.07   # DEPRECATED
TRAILING_STOP_PCT           = 0.15   # DEPRECATED
DAILY_LOSS_CIRCUIT_BREAKER  = 0.02   # DEPRECATED
WEEKLY_LOSS_CIRCUIT_BREAKER = 0.05   # DEPRECATED
MAX_TRADES_PER_DAY          = 4      # DEPRECATED
MAX_OPEN_POSITIONS          = 12     # DEPRECATED
MIN_CASH_PCT                = 0.20   # DEPRECATED
MAX_MOONSHOT_PCT            = 0.03   # DEPRECATED
NO_BUY_IF_DOWN_PCT          = 0.03   # DEPRECATED

# Day-trading specific (kept for compat — used in legacy execute_trades.py code)
MAX_RISK_PER_TRADE          = 0.02   # DEPRECATED
MAX_PORTFOLIO_HEAT          = 0.10   # DEPRECATED
MAX_POSITION_SIZE           = 0.15   # DEPRECATED
DEFAULT_STOP_PCT            = 0.012  # DEPRECATED
DEFAULT_TARGET_MULTIPLIER   = 3.0    # R:R target multiplier (still used in order calc)
FLATTEN_BY                  = "15:45"  # DEPRECATED
