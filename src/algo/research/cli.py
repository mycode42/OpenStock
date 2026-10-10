"""Independent entry points for the Algo research layers."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import requests

from src.algo.market_store import DEFAULT_DB
from src.algo.config import ALGO_RUNTIME_CONFIG

from .files import _atomic_json
from .portfolio_simulation import run_coordinated_portfolio
from .settings import (
    DEFAULT_OUTPUT_DIR, LIVE_RESEARCH_STRATEGIES, RESEARCHABLE_STRATEGIES,
)
from .strategy_optimization import run_strategy_optimization
from .strategy_research import run_strategy_research


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.algo.research",
        description="Run standalone Algo strategy research, optimization, and coordinator simulation stages",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    research = commands.add_parser("research", help="compare fixed candidate profiles for one strategy")
    _add_strategy_data_args(research)
    research.add_argument("--start", required=True)
    research.add_argument("--end", required=True)
    research.add_argument("--slippage-bps", type=float, default=5)
    research.add_argument("--spread-bps", type=float, default=3)
    research.add_argument("--commission-per-share", type=float, default=0.005)
    research.add_argument("--short-borrow-annual-pct", type=float, default=3)
    research.add_argument("--unknown-borrow-penalty-bps", type=float, default=25)
    research.add_argument("--force-close-at-end", action="store_true")
    research.add_argument(
        "--output-dir", default=str(DEFAULT_OUTPUT_DIR / "layers" / "research"),
    )
    research.set_defaults(handler=_research_command)

    optimize = commands.add_parser(
        "optimize", help="run standalone chronological walk-forward optimization for one strategy",
    )
    _add_strategy_data_args(optimize)
    optimize.add_argument("--start", required=True)
    optimize.add_argument("--end", required=True)
    optimize.add_argument("--train-days", type=int, default=756)
    optimize.add_argument("--test-days", type=int, default=63)
    optimize.add_argument("--step-days", type=int, default=63)
    optimize.add_argument("--min-train-trades", type=int, default=10)
    optimize.add_argument("--max-train-drawdown-pct", type=float, default=12)
    optimize.add_argument("--workers", type=int, default=8)
    optimize.add_argument("--slippage-bps", type=float, default=5)
    optimize.add_argument("--spread-bps", type=float, default=3)
    optimize.add_argument("--commission-per-share", type=float, default=0.005)
    optimize.add_argument("--short-borrow-annual-pct", type=float, default=3)
    optimize.add_argument("--unknown-borrow-penalty-bps", type=float, default=25)
    optimize.add_argument("--borrow-availability")
    optimize.add_argument(
        "--research-artifact",
        help="candidate bank from an earlier, non-overlapping research run for this strategy",
    )
    optimize.add_argument(
        "--output-dir", default=str(DEFAULT_OUTPUT_DIR / "layers" / "optimization"),
    )
    optimize.set_defaults(handler=_optimize_command)

    portfolio = commands.add_parser(
        "portfolio", help="combine strategy artifacts and replay shared holdout folds through the portfolio coordinator",
    )
    portfolio.add_argument("--artifact-dir", default=str(DEFAULT_OUTPUT_DIR / "layers" / "optimization"))
    portfolio.add_argument(
        "--strategies", default=",".join(LIVE_RESEARCH_STRATEGIES),
        help="comma-separated strategy artifacts to combine",
    )
    portfolio.add_argument(
        "--enable-disabled-intraday", action="store_true",
        help="enable an intraday strategy that is disabled in live config for this research replay only",
    )
    portfolio.add_argument("--event-calendar", help="point-in-time historical event calendar CSV")
    portfolio.add_argument(
        "--multi-factor-overlay", action=argparse.BooleanOptionalAction,
        default=bool(ALGO_RUNTIME_CONFIG.get("algo_multi_factor_enabled", True)),
        help="enable the Multi Factor confirmation overlay (enabled by default to match Algo runtime config)",
    )
    portfolio.add_argument("--workers", type=int, default=8)
    portfolio.add_argument(
        "--output-dir", default=str(DEFAULT_OUTPUT_DIR / "layers" / "portfolio"),
    )
    portfolio.set_defaults(handler=_portfolio_command)
    return parser


def _add_strategy_data_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--strategy", required=True, type=_normalize_strategy, choices=RESEARCHABLE_STRATEGIES,
        help="one strategy id; momentum-swing is an alias for the Algo momentum strategy",
    )
    parser.add_argument("--symbols", help="fixed comma-separated universe; defaults to configured Algo universe")
    parser.add_argument("--db", default=str(DEFAULT_DB))


def _normalize_strategy(value: str) -> str:
    aliases = {
        "momentum-swing": "momentum",
        "momentum_swing": "momentum",
        "trend-following-swing": "trend_following",
        "trend_following_swing": "trend_following",
        "mean-reversion-swing": "swing_mean_reversion",
        "mean_reversion_swing": "swing_mean_reversion",
        "mean-reversion-intraday": "mean_reversion",
        "mean_reversion_intraday": "mean_reversion",
        "opening-range-breakout-intraday": "opening_range_breakout",
        "opening_range_breakout_intraday": "opening_range_breakout",
        "trend-following-intraday": "intraday_trend",
        "trend_following_intraday": "intraday_trend",
        "momentum-intraday": "intraday_momentum",
        "intraday-momentum": "intraday_momentum",
        "swing-mean-reversion": "swing_mean_reversion",
    }
    normalized = value.strip().lower()
    strategy = aliases.get(normalized, normalized)
    if strategy not in RESEARCHABLE_STRATEGIES:
        raise argparse.ArgumentTypeError(
            f"unknown Algo research strategy {value!r}; choose from {', '.join(RESEARCHABLE_STRATEGIES)}"
        )
    return strategy


def _research_command(args) -> dict:
    args.created_at = datetime.now(timezone.utc).isoformat()
    report = run_strategy_research(args)
    directory = Path(args.output_dir) / args.strategy
    path = _save_stage(directory, "research", report)
    return {
        "status": "completed", "stage": report["stage"], "strategy": args.strategy,
        "artifact": str(path.resolve()), "selection_performed": False,
        "candidate_count": len(report["candidate_comparisons"]),
    }


def _optimize_command(args) -> dict:
    result = run_strategy_optimization(args)
    return {
        "status": "completed", "stage": result["stage"], "strategy": args.strategy,
        "artifact": result["artifact"], "date_artifact": result["dated_artifact"],
        "folds": len(result["optimization"]["folds"]),
        "promotion_gate": result["promotion_gate"],
        "latest_training_selection": result["latest_training_selection"],
    }


def _portfolio_command(args) -> dict:
    report = run_coordinated_portfolio(args)
    path = _save_stage(Path(args.output_dir), "portfolio", report)
    return {
        "status": "completed", "stage": report["stage"],
        "artifact": str(path.resolve()), "strategies": report["strategies"],
        "worker_count": report["worker_count"], "fold_summary": report["fold_summary"],
        "research_only": True,
    }


def _save_stage(directory: Path, label: str, report: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    run_date = datetime.now().date().isoformat()
    dated = directory / f"{run_date}-{label}.json"
    latest = directory / "latest.json"
    _atomic_json(dated, report)
    _atomic_json(latest, report)
    return latest


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = args.handler(args)
    except (ValueError, OSError, requests.RequestException) as exc:
        print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, default=str))
    return 0
