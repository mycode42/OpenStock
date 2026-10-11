"""Compatibility exports for the Algo backtest package.

Replay mixins import their own dependencies directly. This module remains for
the package facade and existing CLI imports while that internal layout stays
stable for callers.
"""

from dotenv import load_dotenv
from risk_manager import validate_trade

from src.algo.market_store import DEFAULT_DB, SQLiteBarStore, fetch_alpaca_bars
from src.algo.strategies.event_driven import EventCalendar
from src.algo.strategies.regime import rank_strategies
from src.algo.strategies.rule_pipeline import evaluate_rule_signal
from src.config import STRATEGY_CONFIG, get_mode_config
from src.market_data import data_tier, regular_feed

load_dotenv()

__all__ = [
    "DEFAULT_DB", "SQLiteBarStore", "fetch_alpaca_bars", "STRATEGY_CONFIG",
    "evaluate_rule_signal", "validate_trade", "EventCalendar", "get_mode_config",
    "data_tier", "regular_feed", "rank_strategies",
]
