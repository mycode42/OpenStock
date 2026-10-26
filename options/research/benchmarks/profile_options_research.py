#!/usr/bin/env python3
"""Profile a bounded, isolated sample of the options research pipeline.

The benchmark reads an existing historical chain archive, optionally limits it
to the most recent snapshots, and writes all generated data and reports beneath
the selected output directory. Worker profiles cover JSONL normalization, chain
preparation, and walk-forward fold evaluation in addition to the parent process.
"""

from __future__ import annotations

import argparse
import cProfile
import contextlib
from collections import deque
from datetime import datetime, timezone
import io
import json
import multiprocessing
from multiprocessing.util import Finalize
import os
from pathlib import Path
import pstats
import resource
import shutil
import sys
import time
import traceback
from typing import Any, Callable

from autoresearch.cpu import available_cpu_count
from options.research import dataset as dataset_module
from options.research import runner as runner_module
from options.research.runner import RESEARCH_DEFAULTS_PATH, _merge, _read_json
from runtime.paths import ROOT


PROFILE_DIR_ENV = "OPTIONS_RESEARCH_BENCHMARK_PROFILE_DIR"
_WORKER_PROFILES: dict[str, dict[str, Any]] = {}
_ORIGINAL_NORMALIZE_CHUNK: Callable | None = None
_ORIGINAL_PREPARE_CHAIN: Callable | None = None
_ORIGINAL_EVALUATE_FOLD: Callable | None = None
_ACTIVE_PARENT_PROFILE: cProfile.Profile | None = None


class _Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, value: str) -> int:
        for stream in self.streams:
            stream.write(value)
        return len(value)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()


def _profile_worker_call(stage: str, function: Callable, *args):
    profile_dir = os.environ.get(PROFILE_DIR_ENV)
    if not profile_dir:
        return function(*args)
    parent_profile = _ACTIVE_PARENT_PROFILE
    is_main_process = multiprocessing.current_process().name == "MainProcess"
    if parent_profile is not None:
        # Process workers fork while the coordinator profile is active. The
        # child inherits that profiler's C-level hook, which prevents its own
        # stage profiler from starting. Disable it in the child copy; in the
        # main process, pause and restore it around the worker-stage profile.
        parent_profile.disable()
    state = _WORKER_PROFILES.get(stage)
    if state is None:
        state = {
            "stage": stage,
            "pid": os.getpid(),
            "calls": 0,
            "profile": cProfile.Profile(),
            "directory": profile_dir,
        }
        _WORKER_PROFILES[stage] = state
        if multiprocessing.current_process().name != "MainProcess":
            Finalize(None, _write_worker_profile, args=(state,), exitpriority=20)
    profile = state["profile"]
    try:
        profile.enable()
        return function(*args)
    finally:
        profile.disable()
        state["calls"] += 1
        if parent_profile is not None and is_main_process:
            parent_profile.enable()


def _write_worker_profile(state: dict[str, Any]) -> None:
    if not state["calls"]:
        return
    directory = Path(state["directory"])
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{state['stage']}-pid{state['pid']}-calls{state['calls']}.pstats"
    state["profile"].dump_stats(str(path))


def _flush_parent_profiles() -> None:
    if multiprocessing.current_process().name != "MainProcess":
        return
    for state in _WORKER_PROFILES.values():
        _write_worker_profile(state)


def _profiled_normalize_chunk(task: tuple[str, int, int, str, str]):
    if _ORIGINAL_NORMALIZE_CHUNK is None:
        raise RuntimeError("options JSONL normalizer was not initialized")
    return _profile_worker_call("jsonl-normalization", _ORIGINAL_NORMALIZE_CHUNK, task)


def _profiled_prepare_chain(index: int):
    if _ORIGINAL_PREPARE_CHAIN is None:
        raise RuntimeError("options chain preparation function was not initialized")
    return _profile_worker_call("chain-preparation", _ORIGINAL_PREPARE_CHAIN, index)


def _profiled_evaluate_fold(index: int) -> dict:
    if _ORIGINAL_EVALUATE_FOLD is None:
        raise RuntimeError("options fold evaluator was not initialized")
    return _profile_worker_call("fold-evaluation", _ORIGINAL_EVALUATE_FOLD, index)


def _write_profile(profile: cProfile.Profile, directory: Path, name: str) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    profile_path = directory / f"{name}.pstats"
    summary_path = directory / f"{name}.profile.txt"
    profile.dump_stats(str(profile_path))
    stream = io.StringIO()
    stats = pstats.Stats(profile, stream=stream).strip_dirs()
    stats.sort_stats("cumulative").print_stats(60)
    stats.sort_stats("tottime").print_stats(30)
    summary_path.write_text(stream.getvalue(), encoding="utf-8")
    return {
        "profile_file": str(profile_path.resolve()),
        "profile_summary": str(summary_path.resolve()),
    }


def _profiled_stage(name: str, function: Callable, profile_dir: Path) -> tuple[Any, dict]:
    global _ACTIVE_PARENT_PROFILE

    profile = cProfile.Profile()
    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    _ACTIVE_PARENT_PROFILE = profile
    try:
        result = profile.runcall(function)
    finally:
        profile.disable()
        _ACTIVE_PARENT_PROFILE = None
        files = _write_profile(profile, profile_dir, name)
    return result, {
        "wall_seconds": round(time.perf_counter() - wall_started, 3),
        "parent_process_cpu_seconds": round(time.process_time() - cpu_started, 3),
        **files,
    }


def _latest_snapshot_subset(source: Path, destination: Path, count: int) -> tuple[Path, int, float]:
    """Copy a bounded tail of a JSONL archive without retaining rows in memory."""
    started = time.perf_counter()
    offsets: deque[int] = deque(maxlen=count)
    with source.open("rb") as stream:
        while True:
            offset = stream.tell()
            row = stream.readline()
            if not row:
                break
            if row.strip():
                offsets.append(offset)
        if not offsets:
            raise ValueError(f"options dataset is empty: {source}")
        first_offset = offsets[0]
        selected_count = len(offsets)
        stream.seek(first_offset)
        with destination.open("wb") as output:
            shutil.copyfileobj(stream, output, length=8 * 1024 * 1024)
    return destination, selected_count, round(time.perf_counter() - started, 3)


def _memory_available_bytes() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _child_usage() -> dict[str, float | int]:
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    # Linux reports ru_maxrss in KiB. The host running this project is Linux.
    return {
        "user_cpu_seconds": round(float(usage.ru_utime), 3),
        "system_cpu_seconds": round(float(usage.ru_stime), 3),
        "maximum_child_rss_kib": int(usage.ru_maxrss),
    }


def _summarize_worker_profiles(profile_dir: Path) -> dict[str, dict]:
    result = {}
    for stage in ("jsonl-normalization", "chain-preparation", "fold-evaluation"):
        paths = sorted(profile_dir.glob(f"{stage}-pid*-calls*.pstats"))
        if not paths:
            continue
        combined = pstats.Stats(str(paths[0]))
        for path in paths[1:]:
            combined.add(str(path))
        stream = io.StringIO()
        combined.stream = stream
        combined.strip_dirs().sort_stats("cumulative").print_stats(60)
        combined.sort_stats("tottime").print_stats(30)
        summary = profile_dir / f"{stage}-workers.profile.txt"
        summary.write_text(stream.getvalue(), encoding="utf-8")
        result[stage] = {
            "worker_profile_count": len(paths),
            "worker_profiles": [str(path.resolve()) for path in paths],
            "profile_summary": str(summary.resolve()),
        }
    return result


def _benchmark_config(args, output_dir: Path) -> dict:
    defaults = _read_json(RESEARCH_DEFAULTS_PATH)
    portfolio_path = ROOT / "options/research/config.portfolio.json"
    portfolio = _read_json(portfolio_path) if portfolio_path.is_file() else {}
    config = _merge(defaults, portfolio)
    config["underlying"] = args.underlying
    config["output_dir"] = str((output_dir / "runs").resolve())
    config.setdefault("search", {}).update({
        "maximum_candidates": args.candidates,
        "mode": "surrogate",
        "plateau_neighbours": min(2, args.candidates - 1),
    })
    config.setdefault("walk_forward", {}).update({
        "rolling_train_sessions": args.train_sessions,
        "oos_sessions": args.oos_sessions,
        "step_sessions": args.step_sessions,
        "minimum_oos_periods": args.minimum_oos_periods,
        "final_holdout_sessions": args.holdout_sessions,
    })
    config.setdefault("robustness", {}).update({
        "slippage_multipliers": [1.0],
        "spread_width_multipliers": [1.0],
        "run_assignment_sensitivity": False,
        "monte_carlo_paths": args.monte_carlo_paths,
    })
    return config


def _research_summary(report: dict) -> dict:
    return {
        "status": report.get("status", "completed"),
        "run_id": report.get("run_id"),
        "strategy": report.get("strategy"),
        "underlying": report.get("underlying"),
        "dataset_observations": report.get("dataset_observations"),
        "dataset_chain_errors": report.get("dataset_chain_errors"),
        "walk_forward_fold_count": len(report.get("folds") or []),
        "candidate_count": len(report.get("candidate_leaderboard") or []),
        "approved": bool((report.get("validation") or {}).get("approved")),
        "parallelism": report.get("parallelism", {}),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Profile a bounded options research run")
    parser.add_argument("--strategy", choices=("vrp", "event_driven", "directional_overlay", "long_volatility"), default="long_volatility")
    parser.add_argument("--underlying", default="GLD")
    parser.add_argument("--data-path", help="historical JSONL archive or SQLite history store; defaults to the configured ticker archive")
    parser.add_argument("--mode", choices=("full", "load"), default="full")
    parser.add_argument("--snapshot-count", type=int, default=None,
                        help="use the latest N JSONL snapshots (0 uses the full archive; SQLite defaults to full history)")
    parser.add_argument("--workers", type=int, default=min(8, available_cpu_count()),
                        help="loader and fold worker limit")
    parser.add_argument("--candidates", type=int, default=2,
                        help="candidate budget for full mode (2-100)")
    parser.add_argument("--train-sessions", type=int, default=252)
    parser.add_argument("--oos-sessions", type=int, default=63)
    parser.add_argument("--step-sessions", type=int, default=126)
    parser.add_argument("--minimum-oos-periods", type=int, default=2)
    parser.add_argument("--holdout-sessions", type=int, default=126)
    parser.add_argument("--monte-carlo-paths", type=int, default=50)
    parser.add_argument("--output-dir", help="artifact directory; defaults to a new /tmp path")
    return parser


def main(argv: list[str] | None = None) -> int:
    global _ORIGINAL_NORMALIZE_CHUNK, _ORIGINAL_PREPARE_CHAIN, _ORIGINAL_EVALUATE_FOLD

    args = _parser().parse_args(argv)
    args.underlying = str(args.underlying).strip().upper()
    if not args.underlying or not args.underlying.replace(".", "").isalnum():
        raise ValueError("underlying must be a valid ticker symbol")
    if args.snapshot_count is not None and args.snapshot_count < 0:
        raise ValueError("snapshot-count cannot be negative")
    if args.workers < 1 or args.candidates < 2 or args.candidates > 100:
        raise ValueError("workers must be positive and candidates must be between 2 and 100")
    if min(args.train_sessions, args.oos_sessions, args.step_sessions,
           args.minimum_oos_periods, args.holdout_sessions, args.monte_carlo_paths) < 1:
        raise ValueError("session counts and Monte Carlo paths must be positive")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else (
        Path("/tmp") / f"options-research-profile-{stamp}"
    )
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"benchmark output directory must be empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    profile_dir = output_dir / "worker-profiles"
    profile_dir.mkdir(parents=True, exist_ok=True)
    runs_dir = output_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    portfolio_path = ROOT / "options/research/config.portfolio.json"
    portfolio_config = _read_json(portfolio_path)
    configured_path = args.data_path or (
        portfolio_config.get("automation", {}).get("data_path_template", "")
        .replace("{underlying}", args.underlying)
    )
    if not configured_path:
        configured_path = "data/options/research/options_history.sqlite3"
    source_path = Path(configured_path).expanduser()
    if not source_path.is_absolute():
        source_path = (ROOT / source_path).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"historical options archive not found: {source_path}")
    database_source = source_path.suffix.lower() in {".sqlite", ".sqlite3", ".db"}
    if args.snapshot_count is None:
        args.snapshot_count = 0 if database_source else 900
    if database_source and args.snapshot_count:
        raise ValueError("SQLite profiling currently uses the full ticker history; pass --snapshot-count 0")

    source_used = source_path
    selected_snapshots = None
    subset_seconds = 0.0
    if args.snapshot_count:
        subset_path = output_dir / f"{args.underlying.lower()}-latest-{args.snapshot_count}.jsonl"
        source_used, selected_snapshots, subset_seconds = _latest_snapshot_subset(
            source_path, subset_path, args.snapshot_count,
        )

    config = _benchmark_config(args, output_dir)
    config_path = output_dir / "benchmark-config.json"
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True), encoding="utf-8")
    os.environ[PROFILE_DIR_ENV] = str(profile_dir.resolve())
    _ORIGINAL_NORMALIZE_CHUNK = dataset_module._normalize_jsonl_chunk
    _ORIGINAL_PREPARE_CHAIN = dataset_module._prepare_chain_index
    _ORIGINAL_EVALUATE_FOLD = runner_module._evaluate_fold_index
    dataset_module._normalize_jsonl_chunk = _profiled_normalize_chunk
    dataset_module._prepare_chain_index = _profiled_prepare_chain
    runner_module._evaluate_fold_index = _profiled_evaluate_fold

    child_usage_before = _child_usage()
    memory_before = _memory_available_bytes()
    started_at = datetime.now(timezone.utc)
    report = {
        "schema": "options_research_profile_benchmark_v1",
        "started_at": started_at.isoformat(),
        "status": "running",
        "benchmark": {
            "mode": args.mode,
            "strategy": args.strategy,
            "underlying": args.underlying,
            "source_path": str(source_path),
            "profiled_dataset_path": str(source_used),
            "source_size_bytes": source_path.stat().st_size,
            "snapshot_count_requested": args.snapshot_count,
            "snapshot_count_used": selected_snapshots,
            "subset_copy_seconds": subset_seconds,
            "workers_requested": args.workers,
            "workers_available": available_cpu_count(),
            "candidate_budget": args.candidates if args.mode == "full" else None,
            "isolated_output_dir": str(output_dir),
            "live_settings_changed": False,
        },
        "profiles": {},
    }

    log_path = output_dir / "benchmark.log"
    try:
        with log_path.open("w", encoding="utf-8") as log_stream:
            tee = _Tee(sys.stdout, log_stream)
            with contextlib.redirect_stdout(tee), contextlib.redirect_stderr(tee):
                if args.mode == "load":
                    dataset, timing = _profiled_stage(
                        "dataset-load",
                        lambda: dataset_module.load_options_research_dataset(
                            source_used,
                            underlying=args.underlying,
                            rate=float(config.get("execution", {}).get("risk_free_rate", 0.04)),
                            dividend_yield=float(config.get("execution", {}).get("dividend_yield", 0.015)),
                            data_mode=str(config.get("data_mode", "auto")),
                            model_surface_config=config.get("model_surface", {}),
                            workers=args.workers,
                        ),
                        output_dir,
                    )
                    report["research"] = {
                        "status": "loaded",
                        "observations": len(dataset.observations),
                        "chain_errors": sum(item.chain is None for item in dataset.observations),
                        "data_mode": dataset.data_mode,
                        "loading_parallelism": dataset.loading_parallelism,
                    }
                    report["stage_timings"] = {"dataset_load": timing}
                else:
                    research_report, timing = _profiled_stage(
                        "full-research-pipeline",
                        lambda: runner_module.run_research(
                            args.strategy,
                            data_path=source_used,
                            config_path=config_path,
                            output_dir=runs_dir,
                            underlying=args.underlying,
                            parallel_workers=args.workers,
                        ),
                        output_dir,
                    )
                    report["research"] = _research_summary(research_report)
                    report["stage_timings"] = {"full_research_pipeline": timing}
        _flush_parent_profiles()
        report["profiles"]["worker_stages"] = _summarize_worker_profiles(profile_dir)
        child_usage_after = _child_usage()
        report["resources"] = {
            "memory_available_before_bytes": memory_before,
            "memory_available_after_bytes": _memory_available_bytes(),
            "child_user_cpu_seconds_delta": round(
                child_usage_after["user_cpu_seconds"] - child_usage_before["user_cpu_seconds"], 3,
            ),
            "child_system_cpu_seconds_delta": round(
                child_usage_after["system_cpu_seconds"] - child_usage_before["system_cpu_seconds"], 3,
            ),
            "maximum_child_rss_kib_observed": child_usage_after["maximum_child_rss_kib"],
        }
        report["status"] = "completed"
    except Exception as exc:
        _flush_parent_profiles()
        report["status"] = "error"
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["profiles"]["worker_stages"] = _summarize_worker_profiles(profile_dir)
        log_path.write_text(
            (log_path.read_text(encoding="utf-8") if log_path.exists() else "")
            + f"\n{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
            encoding="utf-8",
        )
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    report["elapsed_seconds"] = round(
        (datetime.now(timezone.utc) - started_at).total_seconds(), 3,
    )
    report["benchmark"]["log_file"] = str(log_path.resolve())
    report_path = output_dir / "benchmark.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False), encoding="utf-8")
    print(json.dumps({
        "status": report["status"],
        "report": str(report_path.resolve()),
        "output_dir": str(output_dir),
        "elapsed_seconds": report["elapsed_seconds"],
        "research": report.get("research"),
        "profile_stages": list(report.get("profiles", {}).get("worker_stages", {}).keys()),
    }, sort_keys=True))
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
