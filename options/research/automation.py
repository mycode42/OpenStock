"""Nightly options research batch and its standalone report artifacts."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from autoresearch.cpu import available_cpu_count
from options.research.artifact_store import OptionsResearchArtifactStore
from options.research.history_store import OptionsHistoryStore
from options.research.runner import RESEARCH_DEFAULTS_PATH, _merge, _project_path, _read_json, run_research
from runtime.atomic import atomic_write_json
from runtime.paths import ROOT


ET = ZoneInfo("America/New_York")
SUPPORTED_STRATEGIES = ("vrp", "event_driven", "directional_overlay", "long_volatility")


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        stream.write(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
        stream.flush()
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _strategy_summary(strategy: str, report: dict) -> dict:
    return {
        "status": report.get("status", "completed"),
        "run_id": report.get("run_id"),
        "run_dir": report.get("run_dir"),
        "report_path": str(Path(report["run_dir"]) / "report.md") if report.get("run_dir") else None,
        "approved": bool((report.get("validation") or {}).get("approved")),
        "validation": report.get("validation", {}),
        "walk_forward": (report.get("walk_forward") or {}).get("oos_metrics", {}),
        "holdout": report.get("holdout", {}).get("metrics", {}),
        "final_parameters": report.get("final_parameters", {}),
        "parallelism": report.get("parallelism", {}),
        "exploratory_replay": report.get("exploratory_replay"),
        "data_mode": report.get("data_mode"),
        "underlying": report.get("underlying"),
    }


def _error_summary(
    strategy: str, error: Exception, *, started_at: datetime,
    run_log: Path, output_root: Path, underlying: str,
) -> dict:
    status = "error"
    message = f"{type(error).__name__}: {error}"
    if isinstance(error, FileNotFoundError) and "historical options dataset not found" in str(error):
        status = "skipped_missing_dataset"
    elif isinstance(error, ValueError) and "no daily_close options history" in str(error):
        status = "skipped_missing_dataset"
    elif isinstance(error, ValueError) and (
        "insufficient history for" in str(error)
        or "fewer than two" in str(error)
    ):
        status = "insufficient_history"
    # run_research records preflight failures before raising. Include the artifact
    # path so a malformed or too-short dataset can be diagnosed without guessing.
    latest_run = None
    if run_log.is_file():
        for line in run_log.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                row.get("strategy") == strategy
                and str(row.get("underlying", "")).upper() == underlying.upper()
                and row.get("status") in {"started", "preflight_failed"}
            ):
                try:
                    stamp = datetime.fromisoformat(str(row.get("created_at", "")).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if stamp >= started_at:
                    latest_run = row
    return {
        "status": status,
        "error": message,
        "run_id": (latest_run or {}).get("run_id"),
        "run_dir": str(output_root / latest_run["run_id"]) if latest_run and latest_run.get("run_id") else None,
    }


def _run_research_job(
    strategy: str, config_path: Path, data_path: Path, underlying: str,
    parallel_workers: int,
) -> dict:
    report = run_research(
        strategy, config_path=config_path, data_path=data_path, underlying=underlying,
        parallel_workers=parallel_workers,
    )
    return _strategy_summary(strategy, report)


def _input_fingerprint(paths: dict[str, Path], *, config_hash: str) -> str:
    """Fingerprint the inputs cheaply so checkpoints are invalidated on data changes."""
    metadata = {}
    for symbol, path in sorted(paths.items()):
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
            try:
                stat = candidate.stat()
            except OSError:
                continue
            metadata[f"{symbol}:{candidate.name}"] = {
                "path": str(candidate.resolve()), "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
    payload = json.dumps({"config_sha256": config_hash, "inputs": metadata}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _checkpointable_job_status(status: str | None) -> bool:
    return status in {
        "completed", "exploratory_only", "skipped_missing_dataset", "insufficient_history",
    }


def _job_report_valid(summary: dict, output_root: Path) -> bool:
    status = summary.get("status")
    if status in {"completed", "exploratory_only"}:
        run_dir = Path(str(summary.get("run_dir") or ""))
        if not run_dir.is_absolute():
            run_dir = output_root / run_dir
        run = _read_json(run_dir / "run.json")
        return bool(
            run and run.get("status") == status
            and (run_dir / "report.json").is_file()
        )
    return status in {"skipped_missing_dataset", "insufficient_history"}


def _render_markdown(report: dict) -> str:
    dataset_label = (
        f"`{report['dataset_path']}`" if report.get("dataset_path")
        else "; ".join(f"{symbol}: `{path}`" for symbol, path in report.get("dataset_paths", {}).items())
    )
    lines = [
        f"# Nightly options research ({report['run_date_et']})",
        "",
        f"Status: **{report['status']}**  ",
        f"Dataset: {dataset_label or 'none'} ({'available' if report['dataset_available'] else 'not available'})  ",
        f"Data mode: `{report.get('data_mode', 'n/a')}`  ",
        "",
        "This report is a separate research sleeve alongside equity AutoResearch. Options candidates remain research-only; exported parameters are not applied to paper or live settings.",
        "",
    ]
    eligibility = report.get("eligible_universe")
    if eligibility:
        lines.extend([
            "## Cached eligible universe",
            "",
            f"Rule version: `{eligibility.get('rule_version')}`; as of `{eligibility.get('as_of_date')}`; "
            f"{eligibility.get('eligible_count', 0)} of {eligibility.get('universe_size', 0)} tickers eligible.",
            f"Applied to strategy jobs: **{bool(report.get('eligible_universe_applied'))}**.",
            f"Cache: `{eligibility.get('cache_path')}`.",
            "",
        ])
        excluded_by_strategy = report.get("selection_excluded_by_strategy", {})
        if excluded_by_strategy:
            lines.extend(["Excluded from strategy runs by the shared cache:", ""])
            lines.extend(
                f"- **{strategy}:** {len(symbols)} ticker(s)"
                for strategy, symbols in excluded_by_strategy.items()
            )
            lines.append("")
    excluded = report.get("excluded_strategies", {})
    if excluded:
        lines.extend(["## Not researched", ""])
        lines.extend(f"- **{name}:** {reason}" for name, reason in excluded.items())
        lines.append("")
    matrix = report.get("underlyings_by_strategy", {})
    if matrix:
        lines.extend(["## Research matrix", ""])
        for strategy, symbols in matrix.items():
            lines.append(f"- **{strategy}:** {', '.join(symbols)}")
        lines.append("")
    worker_map = report.get("workers_used_by_strategy", {})
    if worker_map:
        lines.extend(["## Parallelism", ""])
        for strategy, count in worker_map.items():
            fold_count = report.get("fold_workers_used_by_strategy", {}).get(strategy, 1)
            lines.append(
                f"- **{strategy}:** {count} concurrent ticker job(s), "
                f"up to {fold_count} dataset-loading and fold worker(s) per job"
            )
        lines.append("")
    for strategy, result in report.get("strategies", {}).items():
        by_underlying = result.get("underlyings") or {result.get("underlying", "configured"): result}
        lines.extend([f"## {strategy}", "", f"Status: **{result.get('status')}**"])
        for underlying, item in by_underlying.items():
            lines.extend(["", f"### {underlying}", "", f"Status: **{item.get('status')}**"])
            if item.get("error"):
                lines.extend(["", f"Error: `{item['error']}`"])
            if item.get("status") == "completed":
                wf = item.get("walk_forward", {})
                holdout = item.get("holdout", {})
                lines.extend([
                    "", f"Approved for paper config export: **{item.get('approved')}**  ",
                    f"Rolling OOS: trades {wf.get('trade_count', 0)}, return {float(wf.get('total_return', 0.0)):.2%}, Sharpe {float(wf.get('annualized_sharpe', 0.0)):.3f}, max drawdown {float(wf.get('max_drawdown', 0.0)):.2%}  ",
                    f"Untouched holdout: trades {holdout.get('trade_count', 0)}, return {float(holdout.get('total_return', 0.0)):.2%}, Sharpe {float(holdout.get('annualized_sharpe', 0.0)):.3f}  ",
                    f"Parameters: `{json.dumps(item.get('final_parameters', {}), sort_keys=True)}`  ",
                    f"Detailed report: `{item.get('report_path')}`",
                ])
            elif item.get("status") == "exploratory_only":
                metrics = (item.get("exploratory_replay") or {}).get("metrics", {})
                lines.extend([
                    "",
                    f"Exploratory baseline replay: {metrics.get('trade_count', 0)} trades, {float(metrics.get('total_return', 0.0)):.2%} return, Sharpe {float(metrics.get('annualized_sharpe', 0.0)):.3f}  ",
                    "No parameter search, OOS validation, or promotion eligibility; treat these as descriptive results only.",
                    f"Detailed report: `{item.get('report_path')}`",
                ])
        lines.append("")
    lines.extend([
        "## Data and execution assumptions",
        "",
        "Historical chain snapshots are required for empirical results. A missing dataset skips the strategy runs and is reported as such; a model surface is only used when explicitly represented in the configured dataset. Fills include per-leg spread slippage and commissions. Assignment remains a simplified simulation heuristic.",
        "The eligibility cache and sector/correlation overlays are research-only. They do not change the live portfolio universe or place orders.",
        "",
    ])
    return "\n".join(lines)


def run_automated_options_research(
    *, config_path: str | Path | None = None, workers: int | None = None,
    run_id: str | None = None,
) -> dict:
    """Run the configured options sleeves without touching broker credentials or orders."""
    config_path_resolved = _project_path(config_path) if config_path else RESEARCH_DEFAULTS_PATH
    config = _merge(_read_json(RESEARCH_DEFAULTS_PATH), _read_json(config_path_resolved))
    strategies = [str(item).strip().lower() for item in config.get("automation", {}).get(
        "strategies", ["vrp", "event_driven", "long_volatility"],
    )]
    if not strategies or any(item not in SUPPORTED_STRATEGIES for item in strategies):
        raise ValueError(f"automation strategies must be selected from {', '.join(SUPPORTED_STRATEGIES)}")
    automation_config = config.get("automation", {})
    default_underlyings = [str(item).strip().upper() for item in automation_config.get(
        "underlyings", [config.get("underlying", "SPY")],
    )]
    if not default_underlyings or any(not item or not item.replace(".", "").isalnum() for item in default_underlyings):
        raise ValueError("automation underlyings must be non-empty ticker symbols")
    default_underlyings = list(dict.fromkeys(default_underlyings))
    configured_by_strategy = automation_config.get("underlyings_by_strategy", {})
    if not isinstance(configured_by_strategy, dict):
        raise ValueError("automation underlyings_by_strategy must be an object")
    underlyings_by_strategy = {}
    for strategy in strategies:
        configured = configured_by_strategy.get(strategy, default_underlyings)
        if not isinstance(configured, list):
            raise ValueError(f"automation underlyings_by_strategy.{strategy} must be a list")
        symbols = [str(item).strip().upper() for item in configured]
        if not symbols or any(not item or not item.replace(".", "").isalnum() for item in symbols):
            raise ValueError(f"automation underlyings for {strategy} must be non-empty ticker symbols")
        underlyings_by_strategy[strategy] = list(dict.fromkeys(symbols))
    underlyings = list(dict.fromkeys(
        symbol for strategy in strategies for symbol in underlyings_by_strategy[strategy]
    ))
    base_data_path = config.get("data_path", "data/options/research/chains.jsonl")
    data_paths = automation_config.get("data_paths", {})
    path_template = automation_config.get("data_path_template")
    underlying_data_paths = {
        symbol: _project_path(
            data_paths.get(symbol)
            or (str(path_template).format(underlying=symbol) if path_template else base_data_path)
        )
        for symbol in underlyings
    }
    configured_workers = automation_config.get("workers", 1) if workers is None else workers
    if isinstance(configured_workers, bool) or not isinstance(configured_workers, int) or configured_workers < 1:
        raise ValueError("automation workers must be a positive integer")
    configured_workers_by_strategy = automation_config.get("workers_by_strategy", {})
    if not isinstance(configured_workers_by_strategy, dict):
        raise ValueError("automation workers_by_strategy must be an object")
    available_cpus = available_cpu_count()
    configured_fold_workers = config.get("search", {}).get("parallel_workers", 1)
    if isinstance(configured_fold_workers, bool) or not isinstance(configured_fold_workers, int) or configured_fold_workers < 1:
        raise ValueError("search.parallel_workers must be a positive integer")
    fold_worker_budget = min(configured_fold_workers, available_cpus)
    workers_requested_by_strategy = {}
    workers_used_by_strategy = {}
    fold_workers_used_by_strategy = {}
    for strategy, symbols in underlyings_by_strategy.items():
        strategy_limit = configured_workers_by_strategy.get(strategy, configured_workers)
        if isinstance(strategy_limit, bool) or not isinstance(strategy_limit, int) or strategy_limit < 1:
            raise ValueError(f"automation workers_by_strategy.{strategy} must be a positive integer")
        if workers is not None:
            strategy_limit = min(strategy_limit, workers)
        workers_requested_by_strategy[strategy] = strategy_limit
        # Each ticker job may itself use several fold processes over a shared
        # copy-on-write dataset. Bound outer ticker concurrency so nested pools
        # cannot oversubscribe the cores or multiply dataset memory.
        outer_limit = max(1, available_cpus // fold_worker_budget)
        workers_used_by_strategy[strategy] = min(strategy_limit, outer_limit, len(symbols))
        fold_workers_used_by_strategy[strategy] = min(
            fold_worker_budget,
            max(1, available_cpus // workers_used_by_strategy[strategy]),
        )
    worker_count = max(workers_used_by_strategy.values(), default=0)
    output_root = _project_path(config.get("output_dir", "data/options/research/runs"))
    automation_dir = output_root.parent / "automation"
    automation_dir.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now(timezone.utc)
    run_log = output_root / "research_runs.jsonl"
    config_hash = hashlib.sha256(json.dumps(config, sort_keys=True).encode("utf-8")).hexdigest()
    fingerprint_paths = dict(underlying_data_paths)
    eligibility_fingerprint_config = automation_config.get("eligibility", {})
    configured_rules_path = (
        eligibility_fingerprint_config.get("rules_path")
        if isinstance(eligibility_fingerprint_config, dict) else None
    )
    if configured_rules_path:
        fingerprint_paths["__eligibility_rules__"] = _project_path(configured_rules_path)
    input_fingerprint = _input_fingerprint(fingerprint_paths, config_hash=config_hash)
    run_id = run_id or created_at.strftime("options-%Y%m%dT%H%M%S%fZ")
    checkpoint_dir = automation_dir / "checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = checkpoint_dir / f"{run_id}.json"
    checkpoint = (_read_json(checkpoint_path) if checkpoint_path.exists() else None) or {}
    if (
        checkpoint.get("schema") != "swarm_options_automation_checkpoint_v1"
        or checkpoint.get("run_id") != run_id
        or checkpoint.get("config_sha256") != config_hash
        or checkpoint.get("input_fingerprint") != input_fingerprint
    ):
        checkpoint = {
            "schema": "swarm_options_automation_checkpoint_v1",
            "run_id": run_id,
            "config_sha256": config_hash,
            "input_fingerprint": input_fingerprint,
            "created_at": created_at.isoformat(),
            "jobs": {},
        }
    checkpoint["updated_at"] = created_at.isoformat()
    enabled = bool(config.get("automation", {}).get("enabled", True))
    configured_exclusions = automation_config.get("excluded_strategies", {})
    if not isinstance(configured_exclusions, dict):
        raise ValueError("automation excluded_strategies must be an object")
    excluded_strategies = {str(name): str(reason) for name, reason in configured_exclusions.items()}
    strategies_report: dict[str, dict] = {}
    archive_merge = {
        symbol: {
            "underlying": symbol,
            "dataset_path": str(underlying_data_paths[symbol]),
            "skipped": True,
            "reason": "collectors write directly to the SQLite history database",
        }
        for symbol in underlyings
    }
    eligibility_config = automation_config.get("eligibility", {})
    if not isinstance(eligibility_config, dict):
        raise ValueError("automation.eligibility must be an object")
    sqlite_database_paths = {
        path.resolve() for path in underlying_data_paths.values()
        if path.suffix.lower() in {".sqlite", ".sqlite3", ".db"}
    }
    if len(sqlite_database_paths) > 1:
        raise ValueError("all options research tickers must share one SQLite history database")
    uses_sqlite_datasets = bool(sqlite_database_paths)
    apply_eligibility = bool(eligibility_config.get(
        "apply_to_research", eligibility_config.get("enabled", False),
    ))
    cache_eligibility = bool(eligibility_config.get("cache_enabled", True)) and uses_sqlite_datasets
    if apply_eligibility and not uses_sqlite_datasets:
        raise ValueError("automation.eligibility.apply_to_research requires the SQLite history store")
    uses_history_store = any(
        path.suffix.lower() in {".sqlite", ".sqlite3", ".db"}
        for path in underlying_data_paths.values()
    ) or cache_eligibility or apply_eligibility
    history_store = None
    if uses_history_store:
        history_store = (
            OptionsHistoryStore(next(iter(sqlite_database_paths)))
            if sqlite_database_paths else OptionsHistoryStore()
        )
    history_store_sync = {
        symbol: {
            "underlying": symbol,
            "status": "database_ready" if history_store.has_underlying(symbol) else "waiting_for_data",
            "coverage": history_store.coverage_summary(symbol),
        }
        for symbol in underlyings
    } if history_store else {}
    eligibility_report = None
    selection_excluded_by_strategy = {}
    if cache_eligibility or apply_eligibility:
        from options.research.universe_selection import build_eligible_cache

        eligibility_report = build_eligible_cache(
            database=history_store.path,
            rules_path=eligibility_config.get("rules_path"),
            cache_dir=eligibility_config.get("cache_dir", "data/options/research/universe_cache"),
        )
        eligible = set(eligibility_report.get("eligible_symbols", []))
        if apply_eligibility:
            original_by_strategy = underlyings_by_strategy
            underlyings_by_strategy = {
                strategy: [symbol for symbol in symbols if symbol in eligible]
                for strategy, symbols in original_by_strategy.items()
            }
            selection_excluded_by_strategy = {
                strategy: [symbol for symbol in symbols if symbol not in eligible]
                for strategy, symbols in original_by_strategy.items()
            }
            for strategy, symbols in underlyings_by_strategy.items():
                outer_limit = max(1, available_cpus // fold_worker_budget)
                workers_used_by_strategy[strategy] = min(
                    workers_requested_by_strategy[strategy], outer_limit, len(symbols),
                )
                fold_workers_used_by_strategy[strategy] = (
                    min(fold_worker_budget, max(1, available_cpus // workers_used_by_strategy[strategy]))
                    if workers_used_by_strategy[strategy] else 0
                )
            worker_count = max(workers_used_by_strategy.values(), default=0)
    data_available = any(
        history_store.has_underlying(symbol)
        if history_store and path.suffix.lower() in {".sqlite", ".sqlite3", ".db"}
        else path.is_file()
        for symbol, path in underlying_data_paths.items()
    )
    data_mode = config.get("data_mode", "auto")

    if not enabled:
        status = "disabled"
        strategies_report = {strategy: {"status": "disabled"} for strategy in strategies}
    else:
        jobs = [
            (strategy, underlying)
            for strategy in strategies
            for underlying in underlyings_by_strategy[strategy]
        ]
        job_results: dict[tuple[str, str], tuple[datetime, dict]] = {}
        run_progress_dir = automation_dir / "runs" / run_id
        run_progress_dir.mkdir(parents=True, exist_ok=True)
        checkpoint_jobs = checkpoint.setdefault("jobs", {})
        for strategy, underlying in jobs:
            key = f"{strategy}/{underlying}"
            saved = checkpoint_jobs.get(key) or {}
            summary = saved.get("summary") or {}
            if (
                saved.get("input_fingerprint") == input_fingerprint
                and _checkpointable_job_status(summary.get("status"))
                and _job_report_valid(summary, output_root)
            ):
                try:
                    started = datetime.fromisoformat(saved.get("started_at") or created_at.isoformat())
                except ValueError:
                    started = created_at
                job_results[(strategy, underlying)] = (started, {"summary": summary})

        def write_job_progress() -> None:
            checkpoint["updated_at"] = datetime.now(timezone.utc).isoformat()
            checkpoint["completed_jobs"] = len(job_results)
            checkpoint["total_jobs"] = len(jobs)
            atomic_write_json(checkpoint_path, checkpoint, indent=2, sort_keys=True, allow_nan=False)
            completed = {
                f"{strategy}/{underlying}": result.get("summary")
                for (strategy, underlying), (_, result) in job_results.items()
                if result.get("summary") is not None
            }
            pending = [
                f"{strategy}/{underlying}" for strategy, underlying in jobs
                if (strategy, underlying) not in job_results
            ]
            snapshot = {
                "schema": "swarm_options_automation_progress_v1",
                "run_id": run_id,
                "status": "running",
                "created_at": checkpoint.get("created_at"),
                "updated_at": checkpoint["updated_at"],
                "config_sha256": config_hash,
                "input_fingerprint": input_fingerprint,
                "completed_jobs": len(completed),
                "total_jobs": len(jobs),
                "pending_jobs": pending,
                "jobs": completed,
                "checkpoint_path": str(checkpoint_path.resolve()),
            }
            atomic_write_json(run_progress_dir / "progress.json", snapshot, indent=2, sort_keys=True, allow_nan=False)
            atomic_write_json(run_progress_dir / "partial-report.json", snapshot, indent=2, sort_keys=True, allow_nan=False)
            atomic_write_json(automation_dir / "latest-progress.json", snapshot, indent=2, sort_keys=True, allow_nan=False)

        def record_job(strategy: str, underlying: str, started: datetime, summary: dict) -> None:
            job_results[(strategy, underlying)] = (started, {"summary": summary})
            checkpoint_jobs[f"{strategy}/{underlying}"] = {
                "status": summary.get("status"),
                "summary": summary,
                "started_at": started.isoformat(),
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "input_fingerprint": input_fingerprint,
            }
            write_job_progress()

        write_job_progress()
        print(
            f"Options research: {len(jobs)} strategy/ticker jobs, "
            f"ticker workers {workers_used_by_strategy}; "
            f"fold workers {fold_workers_used_by_strategy} "
            f"(maximum requested {configured_workers}, CPUs available {available_cpus})",
            flush=True,
        )

        # Strategy groups have separate pools. This lets broad, lighter VRP jobs
        # occupy all available cores while keeping the memory-heavy long-vol
        # replay at its lower configured concurrency. Pools close between
        # groups so idle worker processes do not retain historical datasets.
        for strategy in strategies:
            strategy_jobs = [
                (strategy, underlying) for underlying in underlyings_by_strategy[strategy]
                if (strategy, underlying) not in job_results
            ]
            if not strategy_jobs:
                strategies_report[strategy] = {
                    "status": "skipped_eligibility_empty" if eligibility_report else "skipped_empty_universe",
                    "eligible_cache": eligibility_report.get("cache_path") if eligibility_report else None,
                }
                continue
            strategy_workers = workers_used_by_strategy[strategy]
            if strategy_workers == 1:
                for job_strategy, underlying in strategy_jobs:
                    started = datetime.now(timezone.utc)
                    try:
                        summary = _run_research_job(
                            job_strategy, config_path_resolved,
                            underlying_data_paths[underlying], underlying,
                            fold_workers_used_by_strategy[strategy],
                        )
                    except Exception as exc:
                        summary = _error_summary(
                            job_strategy, exc, started_at=started, run_log=run_log,
                            output_root=output_root, underlying=underlying,
                        )
                    record_job(job_strategy, underlying, started, summary)
                continue

            with ProcessPoolExecutor(
                max_workers=strategy_workers,
                mp_context=mp.get_context("spawn"),
            ) as executor:
                futures = {}
                for job_strategy, underlying in strategy_jobs:
                    started = datetime.now(timezone.utc)
                    future = executor.submit(
                        _run_research_job, job_strategy, config_path_resolved,
                        underlying_data_paths[underlying], underlying,
                        fold_workers_used_by_strategy[strategy],
                    )
                    futures[future] = (job_strategy, underlying, started)
                for future in as_completed(futures):
                    job_strategy, underlying, started = futures[future]
                    try:
                        summary = future.result()
                    except Exception as exc:
                        summary = _error_summary(
                            job_strategy, exc, started_at=started, run_log=run_log,
                            output_root=output_root, underlying=underlying,
                        )
                    record_job(job_strategy, underlying, started, summary)

        for strategy in strategies:
            by_underlying = {}
            strategy_underlyings = underlyings_by_strategy[strategy]
            for underlying in strategy_underlyings:
                _, job_result = job_results[(strategy, underlying)]
                by_underlying[underlying] = job_result["summary"]
            if not strategy_underlyings:
                continue
            if len(strategy_underlyings) == 1:
                strategies_report[strategy] = by_underlying[strategy_underlyings[0]]
            else:
                child_statuses = {item.get("status") for item in by_underlying.values()}
                strategy_status = (
                    "partial_error" if "error" in child_statuses
                    else "waiting_for_data" if child_statuses <= {"skipped_missing_dataset", "insufficient_history"}
                    else "partial_data" if child_statuses & {"skipped_missing_dataset", "insufficient_history", "exploratory_only"}
                    else "completed"
                )
                strategies_report[strategy] = {
                    "status": strategy_status,
                    "underlyings": by_underlying,
                    "completed_count": sum(item.get("status") == "completed" for item in by_underlying.values()),
                    "approved_count": sum(item.get("approved", False) for item in by_underlying.values()),
                }
        statuses = {
            item.get("status")
            for result in strategies_report.values()
            for item in (result.get("underlyings", {}).values() if result.get("underlyings") else [result])
        }
        if "error" in statuses:
            status = "partial_error"
        elif statuses and statuses <= {"skipped_eligibility_empty"}:
            status = "eligibility_empty"
        elif statuses and statuses <= {"skipped_missing_dataset", "insufficient_history"}:
            status = "waiting_for_data"
        elif statuses & {"skipped_missing_dataset", "insufficient_history", "exploratory_only"}:
            status = "partial_data"
        else:
            status = "completed"

    today = datetime.now(ET).date().isoformat()
    report = {
        "schema": "swarm_options_nightly_research_v1",
        "status": status,
        "run_id": run_id,
        "created_at": created_at.isoformat(),
        "run_date_et": today,
        "config_path": str(config_path_resolved),
        "config_sha256": config_hash,
        "dataset_path": str(underlying_data_paths.get(underlyings[0])) if len(underlyings) == 1 else None,
        "dataset_paths": {symbol: str(path) for symbol, path in underlying_data_paths.items()},
        "dataset_available": data_available,
        "underlyings_by_strategy": underlyings_by_strategy,
        "selection_excluded_by_strategy": selection_excluded_by_strategy,
        "eligible_universe": eligibility_report,
        "eligible_universe_applied": apply_eligibility,
        "excluded_strategies": excluded_strategies,
        "archive_merge": {
            symbol: {
                "updated": not bool(result.get("skipped")),
                "combined_sessions": None,
                "reason": result.get("reason"),
            }
            for symbol, result in archive_merge.items()
        },
        "history_store_sync": {
            symbol: {
                "status": result.get("status"),
                "coverage": result.get("coverage"),
            }
            for symbol, result in history_store_sync.items()
        },
        "data_mode": data_mode,
        "workers_requested": configured_workers,
        "workers_used": worker_count if enabled else 0,
        "workers_requested_by_strategy": workers_requested_by_strategy,
        "workers_used_by_strategy": workers_used_by_strategy if enabled else {},
        "fold_workers_requested": configured_fold_workers,
        "fold_workers_used_by_strategy": fold_workers_used_by_strategy if enabled else {},
        "strategies": strategies_report,
        "research_only": True,
        "trade_or_order_actions": 0,
        "checkpoint_path": str(checkpoint_path.resolve()),
    }
    report_path = automation_dir / f"{today}.json"
    atomic_write_json(report_path, report, indent=2, sort_keys=True, allow_nan=False)
    atomic_write_json(automation_dir / "latest.json", report, indent=2, sort_keys=True, allow_nan=False)
    markdown_path = automation_dir / f"{today}.md"
    markdown_path.write_text(_render_markdown(report), encoding="utf-8")
    (automation_dir / "latest.md").write_text(_render_markdown(report), encoding="utf-8")
    _append_jsonl(automation_dir / "runs.jsonl", {
        "run_id": run_id,
        "created_at": created_at.isoformat(), "run_date_et": today,
        "status": status, "config_sha256": config_hash,
        "checkpoint_path": str(checkpoint_path.resolve()),
        "workers_requested": configured_workers,
        "workers_used": worker_count if enabled else 0,
        "workers_requested_by_strategy": workers_requested_by_strategy,
        "workers_used_by_strategy": workers_used_by_strategy if enabled else {},
        "fold_workers_requested": configured_fold_workers,
        "fold_workers_used_by_strategy": fold_workers_used_by_strategy if enabled else {},
        "underlyings_by_strategy": underlyings_by_strategy,
        "eligible_universe": ({
            "status": eligibility_report.get("status"),
            "as_of_date": eligibility_report.get("as_of_date"),
            "eligible_count": eligibility_report.get("eligible_count"),
            "universe_size": eligibility_report.get("universe_size"),
            "cache_path": eligibility_report.get("cache_path"),
            "rules_sha256": eligibility_report.get("rules_sha256"),
            "applied": apply_eligibility,
        } if eligibility_report else None),
        "dataset_paths": {symbol: str(path) for symbol, path in underlying_data_paths.items()}, "dataset_available": data_available,
        "strategies": {
            name: (
                {
                    "status": result.get("status"),
                    "underlyings": {
                        symbol: {
                            "status": child.get("status"), "run_id": child.get("run_id"),
                            "run_dir": child.get("run_dir"), "error": child.get("error"),
                        }
                        for symbol, child in result.get("underlyings", {}).items()
                    },
                }
                if result.get("underlyings") else {
                    "status": result.get("status"), "run_id": result.get("run_id"),
                    "run_dir": result.get("run_dir"), "error": result.get("error"),
                }
            )
            for name, result in strategies_report.items()
        },
        "report_path": str(report_path),
    })
    artifact_store = OptionsResearchArtifactStore()
    for document_type, identity, path in (
        ("automation_report", today, report_path),
        ("automation_report_latest", "latest", automation_dir / "latest.json"),
        ("automation_markdown", today, markdown_path),
        ("automation_markdown_latest", "latest", automation_dir / "latest.md"),
        ("automation_history", "runs", automation_dir / "runs.jsonl"),
    ):
        artifact_store.store_document(document_type=document_type, identity=identity, path=path)
    final_path = automation_dir / "runs" / run_id / "final-report.json"
    atomic_write_json(final_path, report, indent=2, sort_keys=True, allow_nan=False)
    checkpoint.update({
        "status": "completed" if status != "partial_error" else "partial_error",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "report_path": str(final_path.resolve()),
        "completed_jobs": len(checkpoint.get("jobs", {})),
    })
    atomic_write_json(checkpoint_path, checkpoint, indent=2, sort_keys=True, allow_nan=False)
    final_progress = {
        "schema": "swarm_options_automation_progress_v1",
        "run_id": run_id,
        "status": checkpoint["status"],
        "created_at": checkpoint.get("created_at"),
        "updated_at": checkpoint["completed_at"],
        "completed_jobs": checkpoint.get("completed_jobs", 0),
        "total_jobs": checkpoint.get("total_jobs", 0),
        "pending_jobs": [],
        "report_path": str(final_path.resolve()),
        "checkpoint_path": str(checkpoint_path.resolve()),
    }
    progress_dir = automation_dir / "runs" / run_id
    atomic_write_json(progress_dir / "progress.json", final_progress, indent=2, sort_keys=True, allow_nan=False)
    atomic_write_json(progress_dir / "partial-report.json", report, indent=2, sort_keys=True, allow_nan=False)
    atomic_write_json(automation_dir / "latest-progress.json", final_progress, indent=2, sort_keys=True, allow_nan=False)
    return report


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run configured options research sleeves")
    parser.add_argument("--config", help="options research config override")
    parser.add_argument("--workers", type=int, help="maximum concurrent research processes")
    parser.add_argument("--run-id", help="stable cycle ID used to resume an interrupted research batch")
    args = parser.parse_args()
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass
    report = run_automated_options_research(
        config_path=args.config, workers=args.workers, run_id=args.run_id,
    )
    print(json.dumps({
        "status": report["status"], "report": str((ROOT / "data/options/research/automation/latest.json").resolve()),
        "strategies": {name: result.get("status") for name, result in report["strategies"].items()},
        "dataset_available": report["dataset_available"],
        "workers_used": report["workers_used"],
    }, sort_keys=True))
    return 1 if report.get("status") == "partial_error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
