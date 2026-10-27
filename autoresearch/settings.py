"""Paths and adaptive research settings for the scheduled Algo pipeline."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from portfolio.config import get_algo_research_controls
from strategies.catalog import (
    STRATEGY_CATALOG, strategy_ids_for_research_workflow,
)
from portfolio.config import get_algo_runtime_settings

from runtime.paths import DATA_DIR, ROOT

ET = ZoneInfo("America/New_York")

DEFAULT_OUTPUT_DIR = DATA_DIR / "research" / "algo-auto"

LOCK_FILE = DATA_DIR / "locks" / "algo-auto-research.lock"

FEED = "sip"

ADJUSTMENT = "split"

MIN_COVERED_UNIVERSE = 20

RESEARCH_START = date(2020, 1, 1)
# 252-session momentum plus regime cross-market features need a little over
# 252 calendar sessions to cover holidays and incomplete symbols.
RESEARCH_WARMUP_DAYS = 450
INITIAL_HISTORY_DATE = RESEARCH_START - timedelta(days=RESEARCH_WARMUP_DAYS)
REGIME_CONTEXT_WARMUP_DAYS = 450

# Keep the scheduled research catalog derived from workflow metadata.
INTRADAY_STRATEGIES = strategy_ids_for_research_workflow("nightly_intraday")
SWING_STRATEGIES = strategy_ids_for_research_workflow("nightly_swing")
RESEARCHABLE_STRATEGIES = (*SWING_STRATEGIES, *INTRADAY_STRATEGIES)
LLM_RESEARCH_STRATEGIES = strategy_ids_for_research_workflow("llm_research")
LIVE_RESEARCH_STRATEGIES = tuple(
    strategy for strategy in RESEARCHABLE_STRATEGIES
    if (
        not STRATEGY_CATALOG[strategy].runtime_setting
        or getattr(get_algo_runtime_settings(), STRATEGY_CATALOG[strategy].runtime_setting)
    )
)

STRATEGY_FLAVORS = {
    strategy: STRATEGY_CATALOG[strategy].horizon
    for strategy in (*INTRADAY_STRATEGIES, *SWING_STRATEGIES, *LLM_RESEARCH_STRATEGIES)
}

CORE_RESEARCH_CONTROLS = get_algo_research_controls()

MAX_ADAPTIVE_FOLD_WORKERS_PER_STRATEGY = 4

# Nightly adaptive research tries the middle length first, then neighboring
# windows if no sample-qualified, positive-after-costs improvement is found.
ADAPTIVE_WINDOW_DAYS = {
    "intraday": (10, 20, 30),
    "swing": (30, 60, 90),
}
ADAPTIVE_DISCOVERY_CANDIDATES = 8
ADAPTIVE_DISCOVERY_ITERATIONS = 4
# New discovery evaluations per strategy per night, across all window attempts.
ADAPTIVE_DISCOVERY_NIGHTLY_EVALUATIONS = 60
ADAPTIVE_DISCOVERY_MIN_NOVELTY_FRACTION = 0.35
ADAPTIVE_DISCOVERY_MAX_DOMAIN_REFINEMENTS = 2
ADAPTIVE_DISCOVERY_MIN_ROUNDS_BEFORE_STOP = 2
ADAPTIVE_VERIFICATION_CANDIDATES = 8
ADAPTIVE_VERIFICATION_TEST_DAYS = 63
ADAPTIVE_VERIFICATION_STEP_DAYS = 63
# The multi-factor labels use a 21-session forward return. Purge that outcome
# horizon between fit and test; long feature lookbacks are valid historical
# inputs and do not require an extra year of embargo.
ADAPTIVE_PURGE_SESSIONS = 21
MINIMUM_POSITIVE_OOS_FOLD_FRACTION = 0.60
ADAPTIVE_MINIMUM_FOLDS = 5
ROBUSTNESS_COST_MULTIPLIERS = (1.5, 2.0)
ROBUSTNESS_BOOTSTRAP_ITERATIONS = 1000
ROBUSTNESS_BOOTSTRAP_BLOCK_SESSIONS = 5
FINAL_HOLDOUT_MIN_SESSIONS = 252
FINAL_HOLDOUT_MIN_REGIMES = 3

# Session-specific bars need to cover the complete lifetime of a forward trial
# when the feed is first enabled, then refresh incrementally.
FORWARD_TRIAL_MAX_DAYS = 180
SIP_EXTENDED_LOCAL_FEED = "sip_extended"

# The configured Algo universe has 22 symbols. Test thresholds around the
# current 20-name requirement without changing the data-coverage floor above.
MULTI_FACTOR_MIN_UNIVERSE_VALUES = (15, 16, 18, 20, 22)

