#!/usr/bin/env python3
"""Run a small, isolated, profiled slice of the adaptive Algo research pipeline.

The benchmark reads stored SIP bars, profiles adaptive discovery, rolling
verification, and coordinator replay, and writes all generated artifacts under
the selected output directory (by default a timestamped directory in /tmp).
It does not refresh data or change live settings.
"""

from __future__ import annotations

import argparse
import cProfile
import contextlib
from datetime import date, datetime, timedelta
import importlib
import io
import json
import logging
import os
from pathlib import Path
import pstats
import sys
import time

from src.algo.cpu import available_cpu_count
from src.algo.market_store import DEFAULT_DB, SQLiteBarStore
from src.algo.research.settings import (
    ADAPTIVE_WINDOW_DAYS, ADJUSTMENT, FEED, RESEARCHABLE_STRATEGIES,
    STRATEGY_FLAVORS,
)
from src.algo.strategies.universe import configured_universe


STRATEGIES = tuple(RESEARCHABLE_STRATEGIES)
COORDINATOR_PROFILE_ENV = "SWARM_ALGO_BENCH_PROFILE_DIR"
_ADAPTIVE_FOLD_ORIGINAL = None
_COORDINATOR_FOLD_ORIGINAL = None


def _profile_files(profile: cProfile.Profile, output_dir: Path, name: str) -> dict:
    profile_path = output_dir / f"{name}.pstats"
    summary_path = output_dir / f"{name}.profile.txt"
    profile.dump_stats(profile_path)
    stream = io.StringIO()
    stats = pstats.Stats(profile, stream=stream).strip_dirs()
    stats.sort_stats("cumulative").print_stats(50)
    stats.sort_stats("tottime").print_stats(30)
    summary_path.write_text(stream.getvalue(), encoding="utf-8")
    return {
        "profile_file": str(profile_path.resolve()),
        "profile_summary": str(summary_path.resolve()),
    }


def _profiled_stage(name: str, function, output_dir: Path) -> tuple[object, dict]:
    profile = cProfile.Profile()
    wall_start = time.perf_counter()
    cpu_start = time.process_time()
    try:
        result = profile.runcall(function)
    finally:
        files = _profile_files(profile, output_dir, name)
    return result, {
        "wall_seconds": round(time.perf_counter() - wall_start, 3),
        "process_cpu_seconds": round(time.process_time() - cpu_start, 3),
        **files,
    }


def _timed_stage(function) -> tuple[object, dict]:
    wall_start = time.perf_counter()
    cpu_start = time.process_time()
    result = function()
    return result, {
        "wall_seconds": round(time.perf_counter() - wall_start, 3),
        "parent_process_cpu_seconds": round(time.process_time() - cpu_start, 3),
    }


def _profiled_adaptive_fold(payload: tuple[int, dict, argparse.Namespace]) -> tuple[int, dict]:
    global _ADAPTIVE_FOLD_ORIGINAL
    adaptive = importlib.import_module("src.algo.research.adaptive")
    original = _ADAPTIVE_FOLD_ORIGINAL or adaptive._run_adaptive_fold
    index, _, args = payload
    output_dir = Path(args.profile_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    profile = cProfile.Profile()
    result = profile.runcall(original, payload)
    _profile_files(profile, output_dir, f"verification-fold-{index + 1:02d}")
    return result


def _profiled_coordinator_fold(payload: tuple) -> tuple[int, dict]:
    global _COORDINATOR_FOLD_ORIGINAL
    simulation = importlib.import_module("src.algo.research.portfolio_simulation")
    original = _COORDINATOR_FOLD_ORIGINAL or simulation._run_coordinated_fold
    item = payload[0]
    output_value = os.environ.get(COORDINATOR_PROFILE_ENV)
    if output_value:
        output_dir = Path(output_value)
        output_dir.mkdir(parents=True, exist_ok=True)
        profile = cProfile.Profile()
        result = profile.runcall(original, payload)
        _profile_files(profile, output_dir, f"portfolio-fold-{int(item['index']) + 1:02d}")
        return result
    return original(payload)


def _available_symbols(db: str) -> tuple[list[str], dict[str, date]]:
    coverage = SQLiteBarStore(db).coverage()
    latest = {
        row["symbol"]: date.fromisoformat(row["last"][:10])
        for row in coverage
        if row["feed"] == FEED and row["adjustment"] == ADJUSTMENT
        and row["timeframe"] == "5Min"
    }
    return list(latest), latest


def _symbols(args) -> list[str]:
    available, _ = _available_symbols(args.db)
    available_set = set(available)
    if args.symbols:
        selected = list(dict.fromkeys(
            part.strip().upper() for part in args.symbols.split(",") if part.strip()
        ))
    else:
        universe = configured_universe("algo").get("tickers", [])
        minimum = 20 if args.strategy == "multi_factor" else 7 if args.strategy == "momentum" else 1
        count = max(args.symbol_count, minimum)
        selected = [str(symbol).upper() for symbol in universe if str(symbol).upper() in available_set][:count]
    missing = sorted(set(selected) - available_set)
    if missing:
        raise ValueError("No stored split-adjusted SIP intraday bars for: " + ", ".join(missing))
    minimum = 20 if args.strategy == "multi_factor" else 7 if args.strategy == "momentum" else 1
    if len(selected) < minimum:
        raise ValueError(
            f"{args.strategy} needs at least {minimum} benchmark symbols; received {len(selected)}"
        )
    if not selected:
        raise ValueError("The benchmark symbol set is empty")
    return selected


def _date_range(args, symbols: list[str]) -> tuple[date, date]:
    if bool(args.start) != bool(args.end):
        raise ValueError("Pass both --start and --end, or neither")
    latest_by_symbol = _available_symbols(args.db)[1]
    latest = [latest_by_symbol[symbol] for symbol in symbols if symbol in latest_by_symbol]
    if len(latest) != len(symbols):
        raise ValueError("Every benchmark symbol must have stored SIP intraday bars")
    end = date.fromisoformat(args.end) if args.end else min(latest)
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=args.days)
    if end < start:
        raise ValueError("Benchmark end date must not be before start date")
    return start, end


def _adaptive_args(
    args, symbols: list[str], start: date, end: date, output_dir: Path,
) -> argparse.Namespace:
    policy_windows = list(ADAPTIVE_WINDOW_DAYS[STRATEGY_FLAVORS[args.strategy]])
    training_windows = [args.train_sessions] if args.train_sessions else policy_windows
    return argparse.Namespace(
        strategy=args.strategy, symbols=",".join(symbols), db=args.db,
        adjustment=ADJUSTMENT, search_method="bayes", candidates=args.candidates,
        seed=41, search_space=None, fitness_weights=None, min_trades=3,
        capital=100_000.0, slippage_bps=5.0, spread_bps=3.0,
        commission_per_share=0.005, short_borrow_annual_pct=3.0,
        unknown_borrow_penalty_bps=25.0, borrow_availability=None,
        start=start.isoformat(), end=end.isoformat(), output_dir=str(output_dir),
        window_days=policy_windows[len(policy_windows) // 2],
        train_days=max(training_windows), train_window_options=training_windows,
        test_days=args.test_sessions, step_days=args.test_sessions,
        workers=args.workers, minimum_folds=args.folds,
        minimum_oos_sharpe=0.0, maximum_average_decay=0.35,
        adaptive_policy_version="profile-benchmark-v1", data_version=None,
        profile_dir=str(output_dir / "verification-fold-profiles"),
    )


def _fold_profile_totals(directory: Path) -> dict:
    cpu_seconds = 0.0
    profiles = sorted(directory.glob("*.pstats"))
    for path in profiles:
        stats = pstats.Stats(str(path))
        cpu_seconds += stats.total_tt
    return {
        "profiled_fold_count": len(profiles),
        "worker_cpu_seconds_from_cprofile": round(cpu_seconds, 3),
        "profile_files": [str(path.resolve()) for path in profiles],
    }


def _run(args) -> dict:
    global _ADAPTIVE_FOLD_ORIGINAL, _COORDINATOR_FOLD_ORIGINAL
    from src.algo.research import adaptive, adaptive_pipeline, portfolio_simulation

    if args.days < 1 or args.candidates < 1 or args.folds < 3 or args.test_sessions < 1:
        raise ValueError("days/candidates/test sessions must be positive and folds must be at least three")
    if args.workers < 1 or args.symbol_count < 1:
        raise ValueError("workers and symbol count must be positive")
    if args.train_sessions is not None and args.train_sessions < 2:
        raise ValueError("train sessions must be at least two")

    symbols = _symbols(args)
    start, end = _date_range(args, symbols)
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    adaptive_output = output_dir / "adaptive-artifacts"
    options = list(ADAPTIVE_WINDOW_DAYS[STRATEGY_FLAVORS[args.strategy]])
    discovery_args = _adaptive_args(args, symbols, start, end, adaptive_output / "discovery")
    discovery, discovery_timing = _profiled_stage(
        "adaptive-discovery",
        lambda: adaptive.run_window_ladder_discovery(
            discovery_args, options, pipeline_version="profile-benchmark-v1",
        ),
        output_dir,
    )

    verification_args = _adaptive_args(args, symbols, start, end, adaptive_output / "verification")
    adaptive._prepare_args(verification_args)
    sessions = adaptive._trading_sessions(
        verification_args.db, end, start, verification_args.adjustment,
    )
    training = verification_args.train_days
    required_sessions = training + verification_args.test_days * args.folds
    if len(sessions) < required_sessions:
        raise ValueError(
            f"Need {required_sessions} SPY sessions for the bounded verification; found {len(sessions)}"
        )
    verification_sessions = sessions[-required_sessions:]
    verification_args.start = verification_sessions[0].isoformat()
    verification_args.end = verification_sessions[-1].isoformat()

    adaptive_fold_original = adaptive._run_adaptive_fold
    _ADAPTIVE_FOLD_ORIGINAL = adaptive_fold_original
    adaptive._run_adaptive_fold = _profiled_adaptive_fold
    try:
        verification, verification_timing = _timed_stage(
            lambda: adaptive.run_adaptive_walk_forward(verification_args),
        )
    finally:
        adaptive._run_adaptive_fold = adaptive_fold_original
        _ADAPTIVE_FOLD_ORIGINAL = None

    detail = json.loads(Path(verification["latest_detail_artifact"]).read_text())
    coordinator_artifact = adaptive_pipeline._write_adaptive_coordinator_artifact(
        detail, verification["summary"], adaptive_output / "verification-coordinator",
    )
    portfolio_args = argparse.Namespace(
        strategies=args.strategy, multi_factor_overlay=args.strategy == "multi_factor",
        event_calendar=None, artifact_dir=str(coordinator_artifact.parent.parent),
        enable_disabled_intraday=False, workers=args.workers,
        output_dir=str(output_dir / "portfolio-replay"),
    )
    coordinator_fold_original = portfolio_simulation._run_coordinated_fold
    _COORDINATOR_FOLD_ORIGINAL = coordinator_fold_original
    portfolio_simulation._run_coordinated_fold = _profiled_coordinator_fold
    prior_profile_dir = os.environ.get(COORDINATOR_PROFILE_ENV)
    os.environ[COORDINATOR_PROFILE_ENV] = str(output_dir / "portfolio-fold-profiles")
    try:
        portfolio, portfolio_timing = _timed_stage(
            lambda: portfolio_simulation.run_coordinated_portfolio(portfolio_args),
        )
    finally:
        portfolio_simulation._run_coordinated_fold = coordinator_fold_original
        _COORDINATOR_FOLD_ORIGINAL = None
        if prior_profile_dir is None:
            os.environ.pop(COORDINATOR_PROFILE_ENV, None)
        else:
            os.environ[COORDINATOR_PROFILE_ENV] = prior_profile_dir

    adaptive_fold_profiles = _fold_profile_totals(Path(verification_args.profile_dir))
    portfolio_fold_profiles = _fold_profile_totals(output_dir / "portfolio-fold-profiles")
    report = {
        "benchmark": {
            "status": "completed", "strategy": args.strategy, "symbols": symbols,
            "range": {"start": start.isoformat(), "end": end.isoformat()},
            "feed": FEED, "adjustment": ADJUSTMENT, "database": str(Path(args.db).resolve()),
            "adaptive_windows": options, "candidate_count": args.candidates,
            "verification_train_sessions": training,
            "verification_training_window_options": verification_args.train_window_options,
            "verification_test_sessions": args.test_sessions,
            "verification_folds": args.folds,
            "requested_workers": args.workers,
            "available_cpu_count": available_cpu_count(),
            "data_refreshed": False, "live_configuration_changed": False,
            "output_dir": str(output_dir),
            "log_file": str((output_dir / "benchmark.log").resolve()),
        },
        "stages": {
            "adaptive_discovery": {
                "timing": discovery_timing,
                "summary": discovery.get("summary"),
            },
            "adaptive_verification": {
                "timing": verification_timing,
                "summary": verification.get("summary"),
                "worker_count": detail.get("worker_count"),
                "worker_profiles": adaptive_fold_profiles,
            },
            "coordinator_replay": {
                "timing": portfolio_timing,
                "worker_count": portfolio.get("worker_count"),
                "fold_summary": portfolio.get("fold_summary"),
                "worker_profiles": portfolio_fold_profiles,
            },
        },
    }
    report_path = output_dir / "benchmark.json"
    report_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--strategy", choices=STRATEGIES, default="momentum")
    parser.add_argument("--symbols", help="comma-separated research symbols; default is a small configured subset")
    parser.add_argument("--symbol-count", type=int, default=8)
    parser.add_argument("--start", help="benchmark start date; pass together with --end")
    parser.add_argument("--end", help="benchmark end date; pass together with --start")
    parser.add_argument("--days", type=int, default=365,
                        help="calendar-day lookback when explicit dates are omitted")
    parser.add_argument("--candidates", type=int, default=2,
                        help="small candidate budget used by discovery and each verification fold")
    parser.add_argument("--folds", type=int, default=3,
                        help="verification and replay folds; at least three are needed for replay")
    parser.add_argument("--train-sessions", type=int,
                        help="training length; defaults to the largest policy window for the strategy")
    parser.add_argument("--test-sessions", type=int, default=5)
    parser.add_argument("--workers", type=int, default=min(2, available_cpu_count()))
    parser.add_argument("--output-dir", help="defaults to a timestamped directory under /tmp")
    return parser


def main() -> int:
    args = _parser().parse_args()
    args.output_dir = str(Path(args.output_dir or (
        Path("/tmp") / f"algo-research-profile-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    )).resolve())
    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    log_path = Path(args.output_dir) / "benchmark.log"
    root_logger = logging.getLogger()
    old_handlers = root_logger.handlers[:]
    log_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    for handler in old_handlers:
        root_logger.removeHandler(handler)
    root_logger.addHandler(log_handler)
    try:
        with log_path.open("a", encoding="utf-8") as log_stream:
            with contextlib.redirect_stdout(log_stream), contextlib.redirect_stderr(log_stream):
                report = _run(args)
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        return 2
    finally:
        root_logger.removeHandler(log_handler)
        log_handler.close()
        for handler in old_handlers:
            root_logger.addHandler(handler)
    print(json.dumps({
        "status": report["benchmark"]["status"],
        "report": str((Path(report["benchmark"]["output_dir"]) / "benchmark.json").resolve()),
        "strategy": report["benchmark"]["strategy"],
        "symbols": len(report["benchmark"]["symbols"]),
        "discovery_wall_seconds": report["stages"]["adaptive_discovery"]["timing"]["wall_seconds"],
        "verification_wall_seconds": report["stages"]["adaptive_verification"]["timing"]["wall_seconds"],
        "portfolio_wall_seconds": report["stages"]["coordinator_replay"]["timing"]["wall_seconds"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
