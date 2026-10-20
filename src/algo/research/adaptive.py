"""Stable entry points for adaptive Algo discovery and rolling verification.

Implementation is split by responsibility across adaptive_search,
adaptive_discovery, adaptive_verification, and adaptive_common.
"""

from __future__ import annotations

import argparse
import json

from src.algo.cpu import available_cpu_count
from src.algo.market_store import DEFAULT_DB
from .adaptive_common import _prepare_args, _read_json_file, _write_report
from .adaptive_discovery import _load_positive_parent
from .adaptive_search import (
    DEFAULT_FITNESS_WEIGHTS, DEFAULT_SEARCH_SPACES, ET,
    _baseline_improvement, _bayes_proposal, _candidate_parameters, _daily_returns,
    _distance, _evaluate_candidate_in_worker, _evaluate_search_candidate,
    _fit_window_ladder as _search_fit_window_ladder, _fitness, _flatten,
    _load_window_data, _make_parameters, _metrics, _normalize_weights,
    _ordered_window_options, _profile_for, _random_values,
    _run_iterative_discovery_search, _run_replay, _run_search_with_bars,
    _search_space, _symbols, _trading_sessions,
)
from .adaptive_discovery import run_discovery as _run_discovery
from .adaptive_discovery import run_window_ladder_discovery as _run_window_ladder_discovery
from .adaptive_verification import (_parameter_stability, _run_adaptive_fold, _stitch, run_adaptive_walk_forward, walk_forward_windows)
from .settings import (
    ADJUSTMENT, ADAPTIVE_MINIMUM_FOLDS, ADAPTIVE_PURGE_SESSIONS, DEFAULT_OUTPUT_DIR,
    MINIMUM_POSITIVE_OOS_FOLD_FRACTION, ROBUSTNESS_BOOTSTRAP_ITERATIONS,
    ROBUSTNESS_COST_MULTIPLIERS,
)


def _fit_window_ladder(*args, **kwargs):
    """Compatibility wrapper honoring callers that patch the search function."""
    kwargs.setdefault("search_func", _run_search_with_bars)
    return _search_fit_window_ladder(*args, **kwargs)


def run_discovery(args):
    return _run_discovery(args, search_func=_run_search_with_bars)


def run_window_ladder_discovery(args, window_options, *, pipeline_version, data_version=None):
    return _run_window_ladder_discovery(
        args, window_options, pipeline_version=pipeline_version,
        data_version=data_version, search_func=_run_search_with_bars,
    )


def add_cli_commands(commands) -> None:
    discovery = commands.add_parser(
        "adaptive-search", help="discover short-window regime-fitted parameter candidates",
    )
    _add_adaptive_args(discovery)
    discovery.add_argument("--window-days", type=int, default=60,
                           help="number of latest completed trading sessions used for discovery")
    discovery.add_argument(
        "--search-iterations", type=int, default=1,
        help="training-only candidate refinement rounds before the separate verification stage",
    )
    discovery.set_defaults(handler=run_discovery)

    verification = commands.add_parser(
        "adaptive-walk-forward", help="verify short-window fitting across rolling future windows",
    )
    _add_adaptive_args(verification, include_range=False)
    verification.add_argument("--start", required=True)
    verification.add_argument("--end", required=True)
    verification.add_argument("--train-days", type=int, default=60,
                              help="recent completed sessions per rolling fit window")
    verification.add_argument(
        "--train-window-options", type=int, nargs="+",
        help="try these trailing training-window lengths in sessions, starting from the middle value",
    )
    verification.add_argument("--test-days", type=int, default=20,
                              help="immediately following unseen sessions per fold")
    verification.add_argument("--step-days", type=int, default=20,
                              help="advance between folds; must be at least test-days")
    verification.add_argument("--workers", type=int, default=min(8, available_cpu_count()))
    verification.add_argument("--minimum-folds", type=int, default=ADAPTIVE_MINIMUM_FOLDS)
    verification.add_argument("--minimum-oos-sharpe", type=float, default=0.0)
    verification.add_argument("--maximum-average-decay", type=float, default=0.35)
    verification.add_argument("--purge-sessions", type=int, default=ADAPTIVE_PURGE_SESSIONS)
    verification.add_argument(
        "--minimum-positive-fold-fraction", type=float,
        default=MINIMUM_POSITIVE_OOS_FOLD_FRACTION,
    )
    verification.add_argument(
        "--cost-sensitivity-multipliers", type=float, nargs="+",
        default=list(ROBUSTNESS_COST_MULTIPLIERS),
    )
    verification.add_argument(
        "--bootstrap-iterations", type=int, default=ROBUSTNESS_BOOTSTRAP_ITERATIONS,
    )
    verification.set_defaults(handler=run_adaptive_walk_forward)


def _add_adaptive_args(parser: argparse.ArgumentParser, *, include_range: bool = True) -> None:
    parser.add_argument("--strategy", required=True, choices=sorted(DEFAULT_SEARCH_SPACES))
    parser.add_argument("--symbols", help="comma-separated universe; defaults to configured Algo universe")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--adjustment", choices=["raw", "split", "dividend", "all"], default=ADJUSTMENT)
    parser.add_argument("--search-method", choices=["grid", "random", "bayes"], default="random")
    parser.add_argument("--candidates", type=int, default=16, help="maximum parameter sets, including baseline")
    parser.add_argument("--seed", type=int, default=41)
    parser.add_argument("--search-space", help="JSON file mapping tunable parameter names to discrete candidate values")
    parser.add_argument("--fitness-weights", default=json.dumps(DEFAULT_FITNESS_WEIGHTS))
    parser.add_argument("--min-trades", type=int, default=3)
    parser.add_argument("--capital", type=float, default=100_000)
    parser.add_argument("--slippage-bps", type=float, default=5)
    parser.add_argument("--spread-bps", type=float, default=3)
    parser.add_argument("--commission-per-share", type=float, default=0.005)
    parser.add_argument("--short-borrow-annual-pct", type=float, default=3)
    parser.add_argument("--unknown-borrow-penalty-bps", type=float, default=25)
    parser.add_argument("--borrow-availability")
    if include_range:
        parser.add_argument("--start")
        parser.add_argument("--end")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR / "adaptive"))
