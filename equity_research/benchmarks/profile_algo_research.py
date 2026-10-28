#!/usr/bin/env python3
"""Run an isolated, profiled sample of the Algo optimizer or research pipeline.

The benchmark reads stored SIP bars and writes all artifacts under the selected
output directory (by default a timestamped directory in /tmp). Full mode runs
one bounded strategy through factor diagnostics, optimizer discovery, rolling
verification, exact candidate validation, forward simulation, and replay. It
does not refresh data or change live settings.
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

from equity_research.cpu import available_cpu_count
from market.store import DEFAULT_DB, SQLiteBarStore
from equity_research.settings import (
    ADAPTIVE_PURGE_SESSIONS, ADAPTIVE_WINDOW_DAYS, ADJUSTMENT, FEED,
    MIN_COVERED_UNIVERSE, RESEARCH_START, RESEARCH_WARMUP_DAYS,
    RESEARCHABLE_STRATEGIES, STRATEGY_FLAVORS,
)
from portfolio.config import ALGO_FACTOR_SECTOR_BY_SYMBOL
from strategies.universe import configured_universe


STRATEGIES = tuple(RESEARCHABLE_STRATEGIES)
COORDINATOR_PROFILE_ENV = "ALGO_BENCH_PROFILE_DIR"
CANDIDATE_VALIDATION_PROFILE_ENV = "ALGO_BENCH_CANDIDATE_VALIDATION_PROFILE_DIR"
_ADAPTIVE_FOLD_ORIGINAL = None
_COORDINATOR_FOLD_ORIGINAL = None
_CANDIDATE_VALIDATION_FOLD_ORIGINAL = None


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
    verification = importlib.import_module("equity_research.pipeline.adaptive_verification")
    original = _ADAPTIVE_FOLD_ORIGINAL or verification._run_adaptive_fold
    index, _, args = payload
    output_dir = Path(args.profile_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    profile = cProfile.Profile()
    result = profile.runcall(original, payload)
    _profile_files(profile, output_dir, f"verification-fold-{index + 1:02d}")
    return result


def _profiled_coordinator_fold(payload: tuple) -> tuple[int, dict[str, dict]]:
    global _COORDINATOR_FOLD_ORIGINAL
    simulation = importlib.import_module("equity_research.validation.portfolio_simulation")
    original = _COORDINATOR_FOLD_ORIGINAL or simulation._run_coordinated_fold_group
    index = payload[0]
    output_value = os.environ.get(COORDINATOR_PROFILE_ENV)
    if output_value:
        output_dir = Path(output_value)
        output_dir.mkdir(parents=True, exist_ok=True)
        profile = cProfile.Profile()
        result = profile.runcall(original, payload)
        _profile_files(profile, output_dir, f"portfolio-fold-{int(index) + 1:02d}")
        return result
    return original(payload)


def _profiled_candidate_validation_fold(payload: tuple) -> tuple[int, list[dict]]:
    global _CANDIDATE_VALIDATION_FOLD_ORIGINAL
    validation = importlib.import_module("equity_research.pipeline.candidate_validation")
    original = _CANDIDATE_VALIDATION_FOLD_ORIGINAL or validation._evaluate_fold
    output_value = os.environ.get(CANDIDATE_VALIDATION_PROFILE_ENV)
    if not output_value:
        return original(payload)
    output_dir = Path(output_value)
    output_dir.mkdir(parents=True, exist_ok=True)
    profile = cProfile.Profile()
    result = profile.runcall(original, payload)
    _profile_files(profile, output_dir, f"candidate-validation-fold-{int(payload[0]) + 1:02d}")
    return result


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
        minimum = 20 if args.strategy == "swing_multi_factor" else 7 if args.strategy == "swing_momentum" else 1
        count = max(args.symbol_count, minimum)
        selected = [str(symbol).upper() for symbol in universe if str(symbol).upper() in available_set][:count]
    missing = sorted(set(selected) - available_set)
    if missing:
        raise ValueError("No stored split-adjusted SIP intraday bars for: " + ", ".join(missing))
    minimum = 20 if args.strategy == "swing_multi_factor" else 7 if args.strategy == "swing_momentum" else 1
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


def _run_factor_diagnostics(db: str, end: date) -> dict:
    """Run the production factor fit from stored daily bars, without SEC refresh."""
    from equity_research.data import _load_daily_history
    from equity_research.factor_research import train_factor_model

    store = SQLiteBarStore(db)
    universe = [
        str(symbol).upper()
        for symbol in configured_universe("algo").get("tickers", [])
    ]
    daily = _load_daily_history(
        store, universe,
        start=(RESEARCH_START - timedelta(days=RESEARCH_WARMUP_DAYS)).isoformat(),
        end=end.isoformat(),
    )
    symbols = [symbol for symbol in universe if daily.get(symbol)]
    if len(symbols) < MIN_COVERED_UNIVERSE:
        raise ValueError(
            f"Factor profiling needs {MIN_COVERED_UNIVERSE} stored daily symbols; found {len(symbols)}"
        )
    history = {symbol: daily[symbol] for symbol in symbols}
    history["SPY"] = daily.get("SPY", [])
    report = train_factor_model(
        history, min_universe=MIN_COVERED_UNIVERSE,
        sector_by_symbol={
            symbol: sector for symbol, sector in ALGO_FACTOR_SECTOR_BY_SYMBOL.items()
            if symbol in symbols
        },
        fundamentals_by_symbol={},
        fundamentals_diagnostics={"status": "omitted_for_isolated_profile"},
        fundamentals_data_version="profile-benchmark",
        data_version="profile-benchmark",
    )
    return {
        "report": report,
        "symbols": symbols,
        "daily_bar_count": sum(len(daily.get(symbol, [])) for symbol in [*symbols, "SPY"]),
    }


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
        workers=args.workers, candidate_workers=1,
        search_iterations=args.iterations, minimum_folds=args.folds,
        minimum_oos_sharpe=0.0, maximum_average_decay=0.35,
        adaptive_policy_version="profile-benchmark-v1", data_version=None,
        profile_dir=str(output_dir / "verification-fold-profiles"),
        purge_sessions=ADAPTIVE_PURGE_SESSIONS,
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
    from equity_research.pipeline import (
        adaptive_common, adaptive_pipeline,
        adaptive_search, adaptive_verification,
    )
    from equity_research.artifacts.candidate_archive import candidate_id, reserve_search_seed
    from equity_research.validation import portfolio_simulation

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
    factor_result, factor_timing = _profiled_stage(
        "factor-model", lambda: _run_factor_diagnostics(args.db, end), output_dir,
    )
    factor_report = factor_result["report"]
    factor_weight_timeline = factor_report.get("weight_timeline") or []
    archive_dir = adaptive_output / "archive"
    seed, sequence = reserve_search_seed(
        archive_dir, args.strategy, adaptive_pipeline.ADAPTIVE_PIPELINE_VERSION,
    )
    discovery_payload = (
        args.strategy, args.db, symbols, end, "profile-benchmark",
        adaptive_output, options, archive_dir, seed, sequence, factor_weight_timeline,
        f"profile-{output_dir.name}", 1,
    )
    original_candidate_count = adaptive_pipeline.ADAPTIVE_DISCOVERY_CANDIDATES
    original_iterations = adaptive_pipeline.ADAPTIVE_DISCOVERY_ITERATIONS
    try:
        adaptive_pipeline.ADAPTIVE_DISCOVERY_CANDIDATES = args.candidates
        adaptive_pipeline.ADAPTIVE_DISCOVERY_ITERATIONS = args.iterations
        discovery, discovery_timing = _profiled_stage(
            "optimizer-discovery",
            lambda: adaptive_pipeline._run_adaptive_discovery_job(discovery_payload), output_dir,
        )
    finally:
        adaptive_pipeline.ADAPTIVE_DISCOVERY_CANDIDATES = original_candidate_count
        adaptive_pipeline.ADAPTIVE_DISCOVERY_ITERATIONS = original_iterations

    stages = {
        "factor_model": {
            "timing": factor_timing,
            "summary": {
                "symbols": len(factor_result["symbols"]),
                "daily_bar_count": factor_result["daily_bar_count"],
                "observation_count": factor_report.get("observation_count"),
                "rolling_evaluation_count": factor_report.get("rolling_evaluation_count"),
                "weight_timeline_count": len(factor_weight_timeline),
            },
        },
        "optimizer_discovery": {
            "timing": discovery_timing,
            "summary": discovery.get("summary"),
        },
    }
    training = max(options)
    verification_options = None
    if args.mode == "full":
        verification_args = _adaptive_args(args, symbols, start, end, adaptive_output / "verification")
        verification_options = verification_args.train_window_options
        adaptive_common._prepare_args(verification_args)
        verification_args.factor_weight_timeline = factor_weight_timeline
        verification_args.cost_sensitivity_multipliers = [1.5, 2.0]
        verification_args.bootstrap_iterations = 1000
        sessions = adaptive_search._trading_sessions(
            verification_args.db, end, start, verification_args.adjustment,
        )
        training = verification_args.train_days
        required_sessions = (
            training + verification_args.purge_sessions
            + verification_args.test_days * args.folds
        )
        if len(sessions) < required_sessions:
            raise ValueError(
                f"Need {required_sessions} SPY sessions for the bounded verification; found {len(sessions)}"
            )
        verification_sessions = sessions[-required_sessions:]
        verification_args.start = verification_sessions[0].isoformat()
        verification_args.end = verification_sessions[-1].isoformat()

        adaptive_fold_original = adaptive_verification._run_adaptive_fold
        _ADAPTIVE_FOLD_ORIGINAL = adaptive_fold_original
        adaptive_verification._run_adaptive_fold = _profiled_adaptive_fold
        try:
            verification, verification_timing = _timed_stage(
                lambda: adaptive_verification.run_adaptive_walk_forward(verification_args),
            )
        finally:
            adaptive_verification._run_adaptive_fold = adaptive_fold_original
            _ADAPTIVE_FOLD_ORIGINAL = None

        detail = json.loads(Path(verification["latest_detail_artifact"]).read_text())
        from equity_research.pipeline import candidate_validation

        candidate_validation_original = candidate_validation._evaluate_fold
        _CANDIDATE_VALIDATION_FOLD_ORIGINAL = candidate_validation_original
        # For one worker, the parent stage profiler sees the entire fold call;
        # nested cProfile instances cannot run in the same process.
        candidate_validation._evaluate_fold = (
            candidate_validation_original if args.workers == 1
            else _profiled_candidate_validation_fold
        )
        prior_candidate_profile_dir = os.environ.get(CANDIDATE_VALIDATION_PROFILE_ENV)
        candidate_profile_dir = output_dir / "candidate-validation-fold-profiles"
        os.environ[CANDIDATE_VALIDATION_PROFILE_ENV] = str(candidate_profile_dir)
        verification_cohort = {
            "status": "completed",
            "strategies": {
                args.strategy: {
                    "status": "completed",
                    "detail_artifact": verification["latest_detail_artifact"],
                },
            },
        }
        discovery_cohort = {
            "status": discovery.get("status", "completed"),
            "invocation_id": f"profile-{output_dir.name}",
            "strategies": {args.strategy: {
                **discovery,
                # Exercise the exact archived-candidate validation path even
                # when a tiny smoke search has too few trades to be eligible.
                # This changes only the temporary benchmark cohort; the
                # production archive and selection gates remain untouched.
                "selected_candidate": {
                    **(discovery.get("selected_candidate") or {}),
                    "eligible": True,
                },
            }},
        }
        try:
            candidate_validation_report, candidate_validation_timing = _profiled_stage(
                "candidate-validation-orchestration",
                lambda: candidate_validation.run_candidate_validation(
                    database=args.db, symbols=symbols, start=RESEARCH_START, end=end,
                    data_version="profile-benchmark", output_dir=adaptive_output,
                    verification=verification_cohort, discovery=discovery_cohort,
                    workers=args.workers, force=True,
                    factor_weight_timeline=factor_weight_timeline,
                ),
                output_dir,
            )
        finally:
            candidate_validation._evaluate_fold = candidate_validation_original
            _CANDIDATE_VALIDATION_FOLD_ORIGINAL = None
            if prior_candidate_profile_dir is None:
                os.environ.pop(CANDIDATE_VALIDATION_PROFILE_ENV, None)
            else:
                os.environ[CANDIDATE_VALIDATION_PROFILE_ENV] = prior_candidate_profile_dir
        candidate_validation_profiles = _fold_profile_totals(candidate_profile_dir)
        from equity_research.validation import forward_validation

        forward_state_path = output_dir / "forward-validation.json"
        promoted_profile_path = output_dir / "promoted-profile.json"
        selected = discovery.get("selected_candidate") or {}
        parameters = selected.get("parameters") or {}
        if parameters:
            trial_id = f"profile-{output_dir.name}"
            regime = "trending_up"
            candidate_key = candidate_id(args.strategy, parameters)
            forward_state_path.write_text(json.dumps({
                "schema": "swarm_algo_forward_validation_v1",
                "active_trials": {
                    f"{args.strategy}:{regime}:{candidate_key}": {
                        "trial_id": trial_id,
                        "candidate_id": candidate_key,
                        "strategy": args.strategy,
                        "regime": regime,
                        "parameters": parameters,
                        "staged_on": (end - timedelta(days=30)).isoformat(),
                        "staged_at": datetime.now().astimezone().isoformat(),
                        "status": "validating",
                        "latest_simulation": None,
                    },
                },
                "history": [],
            }, indent=2) + "\n", encoding="utf-8")
        original_forward_state = forward_validation.FORWARD_VALIDATION_PATH
        original_promoted_profile = forward_validation.PROMOTED_PROFILE_PATH
        forward_validation.FORWARD_VALIDATION_PATH = forward_state_path
        forward_validation.PROMOTED_PROFILE_PATH = promoted_profile_path
        try:
            forward_report, forward_timing = _profiled_stage(
                "forward-validation",
                lambda: forward_validation.run_forward_validation_cycle(
                    discovery=discovery_cohort, verification=verification_cohort,
                    store=SQLiteBarStore(args.db), database=args.db,
                    symbols=symbols, end=end, data_version="profile-benchmark",
                    allow_stage=False, allow_promotion=False,
                    factor_weight_timeline=factor_weight_timeline,
                ),
                output_dir,
            ) if parameters else ({"status": "skipped", "reason": "no staged candidate parameters"}, {
                "wall_seconds": 0.0, "process_cpu_seconds": 0.0,
            })
        finally:
            forward_validation.FORWARD_VALIDATION_PATH = original_forward_state
            forward_validation.PROMOTED_PROFILE_PATH = original_promoted_profile
        stages["forward_validation"] = {
            "timing": forward_timing,
            "status": forward_report.get("status"),
            "simulation_errors": forward_report.get("simulation_errors"),
            "live_configuration_changed": forward_report.get("live_configuration_changed", False),
        }
        coordinator_artifact = adaptive_pipeline._write_adaptive_coordinator_artifact(
            detail, verification["summary"], adaptive_output / "verification-coordinator",
        )
        portfolio_args = argparse.Namespace(
            strategies=args.strategy,
            event_calendar=None, artifact_dir=str(coordinator_artifact.parent.parent),
            workers=args.workers,
            output_dir=str(output_dir / "portfolio-replay"),
        )
        coordinator_fold_original = portfolio_simulation._run_coordinated_fold_group
        _COORDINATOR_FOLD_ORIGINAL = coordinator_fold_original
        portfolio_simulation._run_coordinated_fold_group = (
            coordinator_fold_original if args.workers == 1
            else _profiled_coordinator_fold
        )
        prior_profile_dir = os.environ.get(COORDINATOR_PROFILE_ENV)
        os.environ[COORDINATOR_PROFILE_ENV] = str(output_dir / "portfolio-fold-profiles")
        try:
            portfolio, portfolio_timing = _profiled_stage(
                "portfolio-orchestration",
                lambda: portfolio_simulation.run_coordinated_portfolio(portfolio_args),
                output_dir,
            )
        finally:
            portfolio_simulation._run_coordinated_fold_group = coordinator_fold_original
            _COORDINATOR_FOLD_ORIGINAL = None
            if prior_profile_dir is None:
                os.environ.pop(COORDINATOR_PROFILE_ENV, None)
            else:
                os.environ[COORDINATOR_PROFILE_ENV] = prior_profile_dir

        adaptive_fold_profiles = _fold_profile_totals(Path(verification_args.profile_dir))
        portfolio_fold_profiles = _fold_profile_totals(output_dir / "portfolio-fold-profiles")
        stages.update({
            "adaptive_verification": {
                "timing": verification_timing,
                "summary": verification.get("summary"),
                "worker_count": detail.get("worker_count"),
                "worker_profiles": adaptive_fold_profiles,
            },
            "candidate_validation": {
                "timing": candidate_validation_timing,
                "status": candidate_validation_report.get("status"),
                "errors": candidate_validation_report.get("errors"),
                "worker_profiles": candidate_validation_profiles,
            },
            "coordinator_replay": {
                "timing": portfolio_timing,
                "worker_count": portfolio.get("worker_count"),
                "fold_summary": portfolio.get("fold_summary"),
                "worker_profiles": portfolio_fold_profiles,
            },
        })
    report = {
        "benchmark": {
            "status": "completed", "strategy": args.strategy, "symbols": symbols,
            "mode": args.mode,
            "range": {"start": start.isoformat(), "end": end.isoformat()},
            "feed": FEED, "adjustment": ADJUSTMENT, "database": str(Path(args.db).resolve()),
            "adaptive_windows": options, "candidate_count": args.candidates,
            "discovery_iterations": args.iterations,
            "verification_train_sessions": training,
            "verification_training_window_options": verification_options,
            "verification_test_sessions": args.test_sessions,
            "verification_folds": args.folds,
            "requested_workers": args.workers,
            "available_cpu_count": available_cpu_count(),
            "data_refreshed": False, "live_configuration_changed": False,
            "sec_fundamentals_loaded": False,
            "forward_promotion_enabled": False,
            "output_dir": str(output_dir),
            "log_file": str((output_dir / "benchmark.log").resolve()),
        },
        "stages": stages,
    }
    report_path = output_dir / "benchmark.json"
    report_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--strategy", choices=STRATEGIES, default="swing_momentum")
    parser.add_argument("--mode", choices=("optimizer", "full"), default="full",
                        help="profile one optimizer discovery job or include rolling verification and replay")
    parser.add_argument("--symbols", help="comma-separated research symbols; default is a small configured subset")
    parser.add_argument("--symbol-count", type=int, default=8)
    parser.add_argument("--start", help="benchmark start date; pass together with --end")
    parser.add_argument("--end", help="benchmark end date; pass together with --start")
    parser.add_argument("--days", type=int, default=365,
                        help="calendar-day lookback when explicit dates are omitted")
    parser.add_argument("--candidates", type=int, default=2,
                        help="small candidate budget used by discovery and each verification fold")
    parser.add_argument("--iterations", type=int, default=1,
                        help="small discovery refinement-iteration budget")
    parser.add_argument("--folds", type=int, default=3,
                        help="verification and replay folds; at least three are needed for replay")
    parser.add_argument("--train-sessions", type=int,
                        help="training length; defaults to the largest policy window for the strategy")
    parser.add_argument("--test-sessions", type=int, default=21)
    parser.add_argument("--workers", type=int, default=1)
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
        "optimizer_discovery_wall_seconds": report["stages"]["optimizer_discovery"]["timing"]["wall_seconds"],
        "factor_model_wall_seconds": report["stages"]["factor_model"]["timing"]["wall_seconds"],
        "verification_wall_seconds": (
            report["stages"].get("adaptive_verification", {}).get("timing", {}).get("wall_seconds")
        ),
        "portfolio_wall_seconds": (
            report["stages"].get("coordinator_replay", {}).get("timing", {}).get("wall_seconds")
        ),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
