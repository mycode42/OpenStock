"""Paths and adaptive research settings for the scheduled Algo pipeline."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src.algo.config import get_algo_research_controls
from src.algo.strategies.catalog import (
    STRATEGY_CATALOG, strategy_ids_for_research_workflow,
)
from src.config import get_algo_runtime_settings

ROOT = Path(__file__).resolve().parents[3]

ET = ZoneInfo("America/New_York")

DEFAULT_OUTPUT_DIR = ROOT / "data" / "research" / "algo-auto"

LOCK_FILE = ROOT / "data" / "locks" / "algo-auto-research.lock"

FEED = "sip"

ADJUSTMENT = "split"

MIN_COVERED_UNIVERSE = 20

RESEARCH_START = date(2024, 1, 1)
RESEARCH_WARMUP_DAYS = 260
INITIAL_HISTORY_DATE = RESEARCH_START - timedelta(days=RESEARCH_WARMUP_DAYS)

# Keep the scheduled research catalog derived from workflow metadata.
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

CORE_RESEARCH_CONTROLS = get_algo_research_controls()

MAX_ADAPTIVE_FOLD_WORKERS_PER_STRATEGY = 4

# Nightly adaptive research tries the middle length first, then neighboring
# windows if no sample-qualified, positive-after-costs improvement is found.
ADAPTIVE_WINDOW_DAYS = {
    "intraday": (10, 20, 30),
    "swing": (30, 60, 90),
}
ADAPTIVE_DISCOVERY_CANDIDATES = 16
ADAPTIVE_DISCOVERY_ITERATIONS = 5
ADAPTIVE_VERIFICATION_CANDIDATES = 8
ADAPTIVE_VERIFICATION_TEST_DAYS = 63
ADAPTIVE_VERIFICATION_STEP_DAYS = 63

# The configured Algo universe has 22 symbols. Test thresholds around the
# current 20-name requirement without changing the data-coverage floor above.
MULTI_FACTOR_MIN_UNIVERSE_VALUES = (16, 18, 20, 22)

