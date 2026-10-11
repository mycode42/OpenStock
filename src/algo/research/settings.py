"""Paths and fixed policy settings for scheduled Algo research."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from src.algo.config import get_algo_research_controls
from src.algo.strategies.catalog import (
    STRATEGY_CATALOG, strategy_ids_for_research_workflow,
)
from src.config import get_algo_runtime_settings

ROOT = Path(__file__).resolve().parents[3]

ET = ZoneInfo("America/New_York")

DEFAULT_PROFILES = ROOT / "autoresearch" / "algo-daily-profiles.json"

DEFAULT_OUTPUT_DIR = ROOT / "data" / "research" / "algo-auto"

LOCK_FILE = ROOT / "data" / "locks" / "algo-auto-research.lock"

FEED = "sip"

ADJUSTMENT = "split"

MIN_COVERED_UNIVERSE = 20

INITIAL_HISTORY_DATE = date(2015, 1, 1)

CORE_STRATEGIES = (
    "mean_reversion", "trend_following", "momentum", "opening_range_breakout",
    "swing_mean_reversion",
)

# Core signal families are distinct by strategy horizon. Keep this expanded
# research catalog separate from CORE_STRATEGIES, which preserves the nightly
# pipeline's established set and behavior.
INTRADAY_STRATEGIES = strategy_ids_for_research_workflow("nightly_intraday")
SWING_STRATEGIES = strategy_ids_for_research_workflow("nightly_swing")
RESEARCHABLE_STRATEGIES = (*SWING_STRATEGIES, *INTRADAY_STRATEGIES)
OVERLAY_STRATEGIES = strategy_ids_for_research_workflow("overlay")
LIVE_RESEARCH_STRATEGIES = tuple(
    strategy for strategy in RESEARCHABLE_STRATEGIES
    if (
        not STRATEGY_CATALOG[strategy].runtime_setting
        or getattr(get_algo_runtime_settings(), STRATEGY_CATALOG[strategy].runtime_setting)
    )
)

STRATEGY_FLAVORS = {
    strategy: (
        "confirmation_overlay"
        if STRATEGY_CATALOG[strategy].role == "satellite"
        else STRATEGY_CATALOG[strategy].horizon
    )
    for strategy in (*INTRADAY_STRATEGIES, *SWING_STRATEGIES, *OVERLAY_STRATEGIES)
}

ISOLATED_MAX_CANDIDATE_TICKER_DAYS_PER_SESSION = 3

COMBINED_MAX_CANDIDATE_TICKER_DAYS_PER_SESSION = 15

CORE_RESEARCH_CONTROLS = get_algo_research_controls()

# The scheduled pipeline discovers profiles on an early, frozen interval and
# selects them only on later walk-forward folds. This boundary must stay
# strictly before optimization to keep candidate discovery out of validation.
STRATEGY_DISCOVERY_END = date(2020, 12, 31)
STRATEGY_OPTIMIZATION_START = date(2021, 1, 1)
STRATEGY_ARTIFACT_MAX_AGE_DAYS = 7
STRATEGY_TRAIN_DAYS = 756
STRATEGY_TEST_DAYS = 63
STRATEGY_STEP_DAYS = 63
STRATEGY_MIN_TRAIN_TRADES = 10
STRATEGY_MAX_TRAIN_DRAWDOWN_PCT = 12

# Nightly adaptive research tries the middle length first, then neighboring
# windows if no sample-qualified, positive-after-costs improvement is found.
ADAPTIVE_WINDOW_DAYS = {
    "intraday": (10, 20, 30),
    "swing": (30, 60, 90),
}
ADAPTIVE_DISCOVERY_CANDIDATES = 16
ADAPTIVE_VERIFICATION_CANDIDATES = 8
ADAPTIVE_VERIFICATION_TEST_DAYS = 63
ADAPTIVE_VERIFICATION_STEP_DAYS = 63
ADAPTIVE_VERIFICATION_HOLDOUT_DAYS = 252

