"""Shared runtime dependencies for the Algo research pipeline."""

from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import contextmanager
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile
from zoneinfo import ZoneInfo
from src.algo.config import get_algo_research_controls
from src.algo.backtest import (
    DAILY_WARMUP_DAYS, AlgoPortfolioBacktest, _regular_session_bars,
    multi_factor_warmup_days,
)
from src.algo.research.factor_research import train_factor_model
from src.algo.market_store import DEFAULT_DB, SQLiteBarStore, fetch_alpaca_bars
from src.strategies.event_driven import EventCalendar
from src.strategies.satellite_overlay import SatelliteOverlayConfig
from src.market_data import data_tier, regular_feed
from src.strategies.multi_factor.features import aggregate_intraday_bars
from src.strategies.registry import ALGO_DEFAULT_PARAMETERS
from src.strategies.universe import configured_universe
