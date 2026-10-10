"""Shared imports for the decomposed deterministic Algo replay."""

from __future__ import annotations

import argparse
from bisect import bisect_right
import csv
import hashlib
import json
import logging
import math
import statistics
import sys
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import requests
from dotenv import load_dotenv
from risk_manager import _get_universe_maps, validate_trade
from src.algo.market_store import DEFAULT_DB, SQLiteBarStore, fetch_alpaca_bars
from src.config import STRATEGY_CONFIG, get_mode_config
from src.market_data import data_tier, regular_feed
from src.algo.strategies.registry import (
    ALGO_DEFAULT_PARAMETERS,
    evaluate as evaluate_strategy,
    validate_parameter_profile,
)
from src.algo.strategies.event_driven import EventCalendar, EventSignalGenerator, EventStrategyConfig
from src.algo.strategies.regime import rank_strategies
from src.algo.strategies.runner import _make_trade
from src.algo.strategies.rule_pipeline import evaluate_rule_signal
from src.algo.strategies.multi_factor.config import MultiFactorConfig
from src.algo.strategies.multi_factor.diagnostics import information_coefficient, turnover
from src.algo.strategies.multi_factor.features import aggregate_intraday_bars
from src.algo.strategies.multi_factor.signals import SignalGenerator
from src.algo.strategies.momentum.swing import generate_signals as generate_swing_momentum_signals
from src.algo.strategies.satellite_overlay import (
    CORE_STRATEGIES, SATELLITE_STRATEGIES, SatelliteOverlayConfig,
    apply_satellite_confirmations,
)
from src.algo.strategies.portfolio_coordinator import (
    INTRADAY_STRATEGIES, RESEARCH_INTRADAY_STRATEGIES,
    RESEARCH_SWING_CORE_STRATEGIES, RESEARCH_SWING_STRATEGIES,
    PortfolioCoordinator, PortfolioCoordinatorConfig,
    strategy_priority, throttle_signal,
)

load_dotenv()

from .config import (
    BAR_MINUTES, DAILY_WARMUP_DAYS, ET, FACTOR_WARMUP_DAYS,
    MIN_REGIME_DAILY_BARS, RESEARCH_CONTROL_KEYS, STRATEGY_NAMES,
    multi_factor_warmup_days, validate_research_controls, validate_strategy_filter,
)
from .models import Position
