#!/usr/bin/env python3
"""Bounded, local profiling jobs for the Algo research pipeline.

Run with ``poetry run python benchmarks/profile_algo_research.py --help``.
The jobs only read the existing market database and write profiles/logs into
the selected output directory. They never refresh bars or change live config.
"""

from __future__ import annotations

import argparse
import cProfile
import contextlib
from datetime import date, datetime, timedelta, timezone
import io
import json
import logging
import pstats
from pathlib import Path
import resource
import signal
import subprocess
import sys
import time
import traceback
from types import SimpleNamespace

from src.algo.backtest import AlgoPortfolioBacktest, DAILY_WARMUP_DAYS
from src.algo.market_store import DEFAULT_DB, SQLiteBarStore
from src.algo.research.data import _load_daily_history
from src.algo.research.factor_research import train_factor_model
from src.algo.research.strategy_research import run_strategy_research
from src.algo.strategies.universe import configured_universe
from src.algo.walk_forward import build_parser as build_walk_forward_parser
from src.algo.walk_forward import run_walk_forward


FEED = "sip"
ADJUSTMENT = "split"
STRATEGIES = (
    "trend_following", "momentum", "swing_mean_reversion", "mean_reversion",
    "opening_range_breakout", "intraday_trend", "intraday_momentum",
)


def _bytes_for_profile(value: object) -> int:
    return len(json.dumps(value, default=str).encode("utf-8"))


def _summary(result: object) -> dict:
    if not isinstance(result, dict):
        return {"result_type": type(result).__name__}
    keys = (
        "strategy", "start", "end", "net_return_pct", "max_drawdown_pct",
        "closed_trade_count", "entry_count", "walk_forward_session_count",
        "candidate_profile_count", "worker_count", "status",
    )
    output = {key: result[key] for key in keys if key in result}
    if isinstance(result.get("folds"), list):
        output["fold_count"] = len(result["folds"])
    if isinstance(result.get("candidate_comparisons"), list):
        output["candidate_count"] = len(result["candidate_comparisons"])
    return output


def _profiled_stage(name: str, function, output_dir: Path) -> tuple[object, dict]:
    profile = cProfile.Profile()
    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    result = None
    try:
        result = profile.runcall(function)
    finally:
        profile_path = output_dir / f"{name}.pstats"
        profile.dump_stats(profile_path)
        formatted = io.StringIO()
        stats = pstats.Stats(profile, stream=formatted).strip_dirs()
        stats.sort_stats("cumulative").print_stats(50)
        stats.sort_stats("tottime").print_stats(30)
        (output_dir / f"{name}.profile.txt").write_text(formatted.getvalue())
    return result, {
        "wall_seconds": round(time.perf_counter() - started_wall, 3),
        "process_cpu_seconds": round(time.process_time() - started_cpu, 3),
        "profile_file": str(profile_path.resolve()),
        "profile_summary": str((output_dir / f"{name}.profile.txt").resolve()),
    }


def _rss_peak_kib() -> int:
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def _symbols(value: str | None) -> list[str]:
    selected = (
        [part.strip().upper() for part in value.split(",") if part.strip()]
        if value else configured_universe("algo")["tickers"]
    )
    symbols = list(dict.fromkeys(selected))
    if not symbols:
        raise ValueError("The Algo benchmark universe is empty")
    return symbols


def _period(args, symbols: list[str]) -> tuple[date, date]:
    if args.start and args.end:
        start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    elif args.start or args.end:
        raise ValueError("Pass both --start and --end, or neither")
    else:
        coverage = SQLiteBarStore(args.db).coverage()
        required = set(symbols)
        rows = [
            row for row in coverage
            if row["feed"] == FEED and row["adjustment"] == ADJUSTMENT
            and row["timeframe"] == "5Min" and row["symbol"] in required
        ]
        latest_by_symbol = {row["symbol"]: date.fromisoformat(row["last"][:10]) for row in rows}
        missing = sorted(required - set(latest_by_symbol))
        if missing:
            raise ValueError("No stored split-adjusted SIP bars for: " + ", ".join(missing))
        end = min(latest_by_symbol.values())
        start = end - timedelta(days=args.days)
    if end < start:
        raise ValueError("Benchmark end date must not be before start date")
    return start, end


def _load_intraday_and_spy(args, symbols: list[str], start: date, end: date):
    store = SQLiteBarStore(args.db)
    intraday = store.bars(
        symbols, feed=FEED, timeframe="5Min", start=start.isoformat(),
        end=end.isoformat(), adjustment=ADJUSTMENT,
    )
    daily_start = (start - timedelta(days=DAILY_WARMUP_DAYS)).isoformat()
    spy_daily = store.bars(
        ["SPY"], feed=FEED, timeframe="1Day", start=daily_start,
        end=end.isoformat(), adjustment=ADJUSTMENT,
    ).get("SPY", [])
    return intraday, spy_daily


def _job_load(args, symbols: list[str], start: date, end: date, output_dir: Path) -> dict:
    store = SQLiteBarStore(args.db)

    def load_intraday():
        return store.bars(
            symbols, feed=FEED, timeframe="5Min", start=start.isoformat(),
            end=end.isoformat(), adjustment=ADJUSTMENT,
        )

    intraday, intraday_timing = _profiled_stage("sqlite_intraday_load", load_intraday, output_dir)
    daily_start = (start - timedelta(days=DAILY_WARMUP_DAYS)).isoformat()

    def load_daily():
        return store.bars(
            ["SPY"], feed=FEED, timeframe="1Day", start=daily_start,
            end=end.isoformat(), adjustment=ADJUSTMENT,
        ).get("SPY", [])

    daily, daily_timing = _profiled_stage("sqlite_daily_load", load_daily, output_dir)
    return {
        "intraday_rows": sum(len(rows) for rows in intraday.values()),
        "daily_rows": len(daily), "symbols_with_intraday": sum(bool(rows) for rows in intraday.values()),
        "timings": {"intraday": intraday_timing, "daily": daily_timing},
    }


def _job_replay(args, symbols: list[str], start: date, end: date, output_dir: Path) -> dict:
    intraday, spy_daily = _load_intraday_and_spy(args, symbols, start, end)
    available = [symbol for symbol in symbols if intraday.get(symbol)]
    if not available:
        raise ValueError("No stored bars for the requested symbols and period")
    replay, setup_timing = _profiled_stage(
        "portfolio_setup",
        lambda: AlgoPortfolioBacktest(
            symbols=available, intraday_bars={name: intraday[name] for name in available},
            spy_daily_bars=spy_daily, start=start.isoformat(), end=end.isoformat(),
            feed=FEED, data_adjustment=ADJUSTMENT, strategies=[args.strategy],
            initial_capital=100_000, slippage_bps=5, spread_bps=3,
            commission_per_share=0.005, short_borrow_annual_pct=0.03,
            unknown_borrow_penalty_bps=25, force_close_at_end=False,
        ),
        output_dir,
    )
    report, replay_timing = _profiled_stage("portfolio_run", replay.run, output_dir)
    return {
        "report": _summary(report), "bars_by_symbol": {name: len(rows) for name, rows in intraday.items()},
        "profile_scope": "portfolio constructor and run are profiled separately; SQLite loading is a separate job",
        "timings": {"portfolio_setup": setup_timing, "portfolio_run": replay_timing},
    }


def _job_research(args, symbols: list[str], start: date, end: date, output_dir: Path) -> dict:
    research_args = SimpleNamespace(
        strategy=args.strategy, symbols=",".join(symbols), db=args.db,
        start=start.isoformat(), end=end.isoformat(), slippage_bps=5.0,
        spread_bps=3.0, commission_per_share=0.005,
        short_borrow_annual_pct=3.0, unknown_borrow_penalty_bps=25.0,
        force_close_at_end=False, created_at=datetime.now(timezone.utc).isoformat(),
    )
    report, timing = _profiled_stage(
        "strategy_research", lambda: run_strategy_research(research_args), output_dir,
    )
    metric_keys = ("net_return_pct", "max_drawdown_pct", "closed_trade_count", "entry_count")
    candidate_summaries = [
        {
            "profile": candidate.get("profile"),
            "metrics": {
                key: (candidate.get("metrics") or {}).get(key)
                for key in metric_keys
            },
        }
        for candidate in report.get("candidate_comparisons", [])
    ]
    return {
        "strategy": args.strategy,
        "candidate_count": len(report.get("candidate_comparisons", [])),
        "candidate_summaries": candidate_summaries,
        "date_range": {"start": report["data"]["start"], "end": report["data"]["end"]},
        "timings": {"strategy_research": timing},
    }


def _job_walk_forward(args, symbols: list[str], start: date, end: date, output_dir: Path) -> dict:
    if args.workers != 1:
        raise ValueError("Profiled walk-forward jobs require --workers 1 so cProfile captures the fold work")
    profiles_path = output_dir / "walk_forward_profiles.json"
    profiles_path.write_text(json.dumps([{
        "name": "baseline", "strategies": [args.strategy],
        "parameters": {}, "controls": {},
    }], indent=2) + "\n")
    parser = build_walk_forward_parser()
    wf_args = parser.parse_args([
        "--symbols", ",".join(symbols), "--start", start.isoformat(),
        "--end", end.isoformat(), "--profiles", str(profiles_path),
        "--feed", FEED, "--adjustment", ADJUSTMENT,
        "--train-days", str(args.train_days), "--test-days", str(args.test_days),
        "--step-days", str(args.test_days), "--min-train-trades", "0",
        "--max-train-drawdown-pct", "100", "--workers", "1", "--db", args.db,
    ])
    report, timing = _profiled_stage(
        "walk_forward", lambda: run_walk_forward(wf_args), output_dir,
    )
    return {
        "strategy": args.strategy,
        "date_range": {"start": start.isoformat(), "end": end.isoformat()},
        "fold_count": len(report.get("folds", [])),
        "candidate_profile_count": report.get("candidate_profile_count"),
        "worker_count": report.get("worker_count"),
        "profile_scope": "serial folds; includes SQLite loads and replay work",
        "timings": {"walk_forward": timing},
    }


def _job_factor(args, symbols: list[str], end: date, output_dir: Path) -> dict:
    coverage = SQLiteBarStore(args.db).coverage()
    daily = [
        row for row in coverage
        if row["feed"] == FEED and row["adjustment"] == ADJUSTMENT
        and row["timeframe"] == "1Day" and row["symbol"] in set(symbols) | {"SPY"}
    ]
    if not daily:
        raise ValueError("No split-adjusted SIP daily bars are available")
    start = min(date.fromisoformat(row["first"][:10]) for row in daily)
    histories, load_timing = _profiled_stage(
        "factor_daily_load",
        lambda: _load_daily_history(
            SQLiteBarStore(args.db), symbols, start=start.isoformat(), end=end.isoformat(),
        ),
        output_dir,
    )
    report, fit_timing = _profiled_stage(
        "factor_model_fit", lambda: train_factor_model(histories, min_universe=20), output_dir,
    )
    return {
        "symbol_count": len(histories), "daily_rows": sum(len(rows) for rows in histories.values()),
        "adaptive_prequential_ic": (report.get("adaptive_prequential_ic") or {}).get("mean_spearman_ic"),
        "date_range": {"start": start.isoformat(), "end": end.isoformat()},
        "timings": {"daily_load": load_timing, "factor_fit": fit_timing},
    }


def _run_job(name: str, function, args, metadata: dict, output_dir: Path) -> dict:
    log_path = output_dir / f"{name}.log"
    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    status = "ok"
    result = {}
    error = None
    root_logger = logging.getLogger()
    previous_handlers = root_logger.handlers[:]
    with log_path.open("w", encoding="utf-8") as log_handle:
        with contextlib.redirect_stdout(log_handle), contextlib.redirect_stderr(log_handle):
            log_handler = logging.StreamHandler(log_handle)
            for handler in previous_handlers:
                root_logger.removeHandler(handler)
            root_logger.addHandler(log_handler)
            previous_handler = None
            if not args.no_timeout:
                previous_handler = signal.getsignal(signal.SIGALRM)

                def raise_timeout(_signum, _frame):
                    raise TimeoutError("benchmark time budget reached")

                signal.signal(signal.SIGALRM, raise_timeout)
                signal.setitimer(signal.ITIMER_REAL, args.timeout_seconds)
            try:
                result = function()
            except TimeoutError as exc:
                status, error = "timeout", str(exc)
                traceback.print_exc(file=log_handle)
            except Exception as exc:
                status, error = "error", f"{type(exc).__name__}: {exc}"
                traceback.print_exc(file=log_handle)
            finally:
                if not args.no_timeout:
                    signal.setitimer(signal.ITIMER_REAL, 0)
                    signal.signal(signal.SIGALRM, previous_handler)
                root_logger.removeHandler(log_handler)
                for handler in previous_handlers:
                    root_logger.addHandler(handler)
    summary = {
        "job": name, "status": status, "error": error,
        "wall_seconds": round(time.perf_counter() - started_wall, 3),
        "process_cpu_seconds": round(time.process_time() - started_cpu, 3),
        "peak_rss_kib": _rss_peak_kib(),
        "metadata": metadata, "result": result,
        "log_file": str(log_path.resolve()),
    }
    (output_dir / f"{name}.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")
    print(json.dumps(summary, sort_keys=True, default=str))
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--job", choices=("load", "replay", "research", "walk-forward", "factor", "all"),
        default="all",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--symbols", help="comma-separated Algo symbols; defaults to the configured universe")
    parser.add_argument("--strategy", choices=STRATEGIES, default="trend_following")
    parser.add_argument("--start", help="benchmark start date; defaults to one year before latest shared data")
    parser.add_argument("--end", help="benchmark end date; defaults to latest shared data")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--timeout-seconds", type=int, default=120,
                        help="per-job limit; partial cProfile data is saved on timeout")
    parser.add_argument("--no-timeout", action="store_true",
                        help="run each selected benchmark job to completion without installing a timer")
    parser.add_argument("--train-days", type=int, default=63)
    parser.add_argument("--test-days", type=int, default=21)
    parser.add_argument("--workers", type=int, default=1,
                        help="profiled walk-forward requires one worker; use later scaling runs for comparisons")
    parser.add_argument("--output-dir", help="defaults to a timestamped folder under /tmp")
    parser.add_argument("--_job-worker", action="store_true", help=argparse.SUPPRESS)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.days < 1 or args.timeout_seconds < 1 or args.train_days < 1 or args.test_days < 1:
        raise SystemExit("days, timeout, train-days, and test-days must be positive")
    output_dir = Path(args.output_dir or (
        Path("/tmp") / f"algo-research-profile-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    )).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    symbols = _symbols(args.symbols)
    start, end = _period(args, symbols)
    shared = {
        "database": str(Path(args.db).resolve()), "symbols": symbols,
        "strategy": args.strategy, "start": start.isoformat(), "end": end.isoformat(),
        "feed": FEED, "adjustment": ADJUSTMENT,
        "timeout_seconds_per_job": None if args.no_timeout else args.timeout_seconds,
        "no_timeout": args.no_timeout,
        "python": sys.version, "output_dir": str(output_dir),
    }
    print(json.dumps({"benchmark_suite": shared}, sort_keys=True))

    jobs = {
        "load": lambda: _job_load(args, symbols, start, end, output_dir),
        "replay": lambda: _job_replay(args, symbols, start, end, output_dir),
        "research": lambda: _job_research(args, symbols, start, end, output_dir),
        "walk-forward": lambda: _job_walk_forward(args, symbols, start, end, output_dir),
        "factor": lambda: _job_factor(args, symbols, end, output_dir),
    }
    if args.job == "all" and not args._job_worker:
        summaries = []
        for name in jobs:
            print(json.dumps({"job_start": name}), flush=True)
            command = [
                sys.executable, str(Path(__file__).resolve()), "--job", name,
                "--db", args.db, "--symbols", ",".join(symbols),
                "--strategy", args.strategy, "--start", start.isoformat(),
                "--end", end.isoformat(), "--days", str(args.days),
                "--timeout-seconds", str(args.timeout_seconds),
                "--train-days", str(args.train_days), "--test-days", str(args.test_days),
                "--workers", str(args.workers), "--output-dir", str(output_dir),
                "--_job-worker",
            ]
            if args.no_timeout:
                command.append("--no-timeout")
            completed = subprocess.run(command, capture_output=True, text=True, check=False)
            summary_path = output_dir / f"{name}.json"
            if summary_path.exists():
                summary = json.loads(summary_path.read_text())
            else:
                summary = {
                    "job": name, "status": "error",
                    "error": f"worker exited {completed.returncode} without a summary",
                    "metadata": {**shared, "job": name},
                }
                (output_dir / f"{name}.runner.log").write_text(
                    completed.stdout + completed.stderr, encoding="utf-8",
                )
            summaries.append(summary)
            print(json.dumps(summary, sort_keys=True, default=str), flush=True)
        suite = {"output_dir": str(output_dir), "jobs": summaries}
        (output_dir / "suite.json").write_text(json.dumps(suite, indent=2, default=str) + "\n")
        print(json.dumps({"suite_report": str((output_dir / 'suite.json').resolve())}, sort_keys=True))
        return 0 if all(job["status"] == "ok" for job in summaries) else 1

    selected = list(jobs) if args.job == "all" else [args.job]
    summaries = []
    for name in selected:
        metadata = {**shared, "job": name}
        summaries.append(_run_job(name, jobs[name], args, metadata, output_dir))
    suite = {"output_dir": str(output_dir), "jobs": summaries}
    (output_dir / "suite.json").write_text(json.dumps(suite, indent=2, default=str) + "\n")
    print(json.dumps({"suite_report": str((output_dir / 'suite.json').resolve())}, sort_keys=True))
    return 0 if all(job["status"] == "ok" for job in summaries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
