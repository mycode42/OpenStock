"""Scheduled orchestration of strategy discovery, validation, and portfolio replay."""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from time import perf_counter
from types import SimpleNamespace

import requests

from src.algo.config import ALGO_RUNTIME_CONFIG
from src.algo.market_store import DEFAULT_DB, SQLiteBarStore
from src.market_data import data_tier, regular_feed

from .artifact_cache import (
    read_json, research_data_version, stable_hash,
    valid_cohort_optimization, valid_cohort_research,
)
from .data import (
    _latest_dates, _load_daily_history, _refresh_incremental,
    _replay_start_after_spy_warmup, load_algo_universe,
)
from .files import _atomic_json, _research_lock
from .live_manifest import live_configuration_manifest
from .profiles import profile_search_manifest
from .settings import (
    ADJUSTMENT, DEFAULT_OUTPUT_DIR, ET, FEED, LIVE_RESEARCH_STRATEGIES,
    MIN_COVERED_UNIVERSE, RESEARCHABLE_STRATEGIES, STRATEGY_ARTIFACT_MAX_AGE_DAYS,
    STRATEGY_DISCOVERY_END, STRATEGY_MAX_TRAIN_DRAWDOWN_PCT,
    STRATEGY_MIN_TRAIN_TRADES, STRATEGY_OPTIMIZATION_START,
    STRATEGY_STEP_DAYS, STRATEGY_TEST_DAYS, STRATEGY_TRAIN_DAYS,
)


NIGHTLY_PIPELINE_VERSION = "algo-research-layers-v2"
MAX_WALK_FORWARD_WORKERS_PER_STRATEGY = 4
_RESEARCH_COSTS = {
    "slippage_bps": 5.0,
    "spread_bps": 3.0,
    "commission_per_share": 0.005,
    "short_borrow_annual_pct": 3.0,
    "unknown_borrow_penalty_bps": 25.0,
    "force_close_at_end": False,
}
_OPTIMIZATION_COSTS = {
    "slippage_bps": 5.0,
    "spread_bps": 3.0,
    "commission_per_share": 0.005,
    "short_borrow_annual_pct": 3.0,
    "unknown_borrow_penalty_bps": 25.0,
}


def run_daily_research(args) -> dict:
    """Run one versioned cohort, reusing compatible artifacts when possible."""
    total_started = perf_counter()
    if data_tier() != "algo_trader_plus" or regular_feed() != FEED:
        raise ValueError("Algo AutoResearch is restricted to Plus SIP data")

    symbols, universe_meta = load_algo_universe()
    database = str(Path(args.db or DEFAULT_DB).resolve())
    store = SQLiteBarStore(database)
    refresh_started = perf_counter()
    refresh_report = None
    if not args.no_refresh:
        now_et = datetime.now(ET)
        refresh_to = now_et.date() if now_et.weekday() < 5 and now_et.time() >= time(16, 10) else now_et.date() - timedelta(days=1)
        refresh_report = _refresh_incremental(
            store, selected_symbols=symbols, end_date=refresh_to,
        )
    refresh_seconds = round(perf_counter() - refresh_started, 3)

    history_start, latest_end, missing = _latest_dates(store, symbols)
    covered_symbols = [symbol for symbol in symbols if symbol not in missing]
    if (latest_end - history_start).days < 1100:
        raise ValueError(
            f"Need at least three years of overlapping SIP bars; found {history_start}..{latest_end}"
        )
    latest_age_days = (datetime.now(ET).date() - latest_end).days
    if not args.no_refresh and latest_age_days > 4:
        raise ValueError(
            f"Plus SIP data is stale after refresh: latest common session {latest_end} "
            f"is {latest_age_days} calendar days old"
        )

    cohort_end = _last_completed_friday(latest_end)
    if history_start >= STRATEGY_DISCOVERY_END:
        raise ValueError(
            f"Strategy discovery needs stored history before {STRATEGY_DISCOVERY_END}; "
            f"earliest common date is {history_start}"
        )
    if cohort_end < STRATEGY_OPTIMIZATION_START:
        raise ValueError(
            f"Strategy validation needs data from {STRATEGY_OPTIMIZATION_START}; "
            f"latest completed weekly cutoff is {cohort_end}"
        )

    discovery_end = STRATEGY_DISCOVERY_END
    discovery_start = _replay_start_after_spy_warmup(
        store, history_start, discovery_end,
    )
    optimization_start = STRATEGY_OPTIMIZATION_START
    optimization_end = cohort_end
    data_version = research_data_version(
        database, start=history_start, end=optimization_end,
        symbols=covered_symbols, feed=FEED, adjustment=ADJUSTMENT,
    )
    live_snapshot = live_configuration_manifest([*RESEARCHABLE_STRATEGIES, "multi_factor", "event_driven"])
    live_hashes = {
        strategy: live_configuration_manifest([strategy])["sha256"]
        for strategy in RESEARCHABLE_STRATEGIES
    }
    search_versions = {
        strategy: profile_search_manifest(strategy)["version"]
        for strategy in RESEARCHABLE_STRATEGIES
    }
    context = {
        "pipeline_version": NIGHTLY_PIPELINE_VERSION,
        "database": database,
        "feed": FEED,
        "adjustment": ADJUSTMENT,
        "symbols": covered_symbols,
        "history_start": history_start.isoformat(),
        "data_end": optimization_end.isoformat(),
        "data_version": data_version,
        "research": {
            "start": discovery_start.isoformat(), "end": discovery_end.isoformat(),
            "costs": _RESEARCH_COSTS,
        },
        "optimization": {
            "start": optimization_start.isoformat(), "end": optimization_end.isoformat(),
            "train_days": STRATEGY_TRAIN_DAYS, "test_days": STRATEGY_TEST_DAYS,
            "step_days": STRATEGY_STEP_DAYS,
            "min_train_trades": STRATEGY_MIN_TRAIN_TRADES,
            "max_train_drawdown_pct": STRATEGY_MAX_TRAIN_DRAWDOWN_PCT,
            "costs": _OPTIMIZATION_COSTS,
        },
        "strategies": list(RESEARCHABLE_STRATEGIES),
        "profile_search_versions": search_versions,
        "live_configuration_sha256": live_snapshot["sha256"],
        "strategy_configuration_sha256": live_hashes,
        "portfolio_strategies": list(LIVE_RESEARCH_STRATEGIES),
        "multi_factor_enabled": bool(ALGO_RUNTIME_CONFIG.get("algo_multi_factor_enabled", True)),
        "event_driven_enabled": bool(ALGO_RUNTIME_CONFIG.get("algo_event_driven_enabled", True)),
        "factor_only": bool(args.factor_only),
    }
    fingerprint = stable_hash(context)
    if args.force:
        fingerprint = stable_hash({"context": context, "forced_at": datetime.now(timezone.utc).isoformat()})
    run_id = f"week-{optimization_end:%Y%m%d}-{fingerprint[:16]}"
    context["fingerprint"] = fingerprint
    context["run_id"] = run_id

    root = Path(args.output_dir) / "layers"
    cohort_dir = root / "runs" / run_id
    report_path = cohort_dir / "report.json"
    if not args.force and not args.factor_only:
        cached, _ = read_json(report_path)
        if cached and cached.get("orchestration", {}).get("fingerprint") == fingerprint:
            return {**cached, "cache_hit": True}

    cohort_dir.mkdir(parents=True, exist_ok=True)
    timings: dict[str, object] = {"data_refresh_seconds": refresh_seconds}
    strategy_reports = {}
    factor_started = perf_counter()
    factor_path = cohort_dir / "factor_model.json"
    factor_cached, _ = read_json(factor_path)
    if not args.force and factor_cached and factor_cached.get("fingerprint") == fingerprint:
        factor_report = factor_cached["report"]
        timings["factor_model"] = {"seconds": 0.0, "reused": True}
    else:
        history = _load_daily_history(
            store, covered_symbols, start=history_start.isoformat(), end=optimization_end.isoformat(),
        )
        from .factor_research import train_factor_model

        factor_report = train_factor_model(history, min_universe=MIN_COVERED_UNIVERSE)
        _atomic_json(factor_path, {"fingerprint": fingerprint, "report": factor_report})
        timings["factor_model"] = {
            "seconds": round(perf_counter() - factor_started, 3), "reused": False,
        }

    if args.factor_only:
        report = {
            "schema": "swarm_algo_nightly_research_v2",
            "status": "completed", "created_at": datetime.now(timezone.utc).isoformat(),
            "research_only": True, "live_configuration_changed": False,
            "trade_or_order_actions": 0, "factor_model": factor_report,
            "factor_only": True,
            "data": _data_summary(
                history_start, latest_end, optimization_end, latest_age_days,
                covered_symbols, symbols, missing, universe_meta, refresh_report,
            ),
            "orchestration": {**context, "cache_hit": False},
            "stage_timings": {**timings, "total_seconds": round(perf_counter() - total_started, 3)},
            "limitations": [
                "Factor fitting is research-only and does not update live weights.",
                "No strategy optimization or portfolio coordinator replay was requested.",
            ],
        }
        _atomic_json(report_path, report)
        return report

    stage_started = perf_counter()
    strategy_reports = _run_strategy_stages(
        context=context, cohort_dir=cohort_dir, output_root=root,
        symbols=covered_symbols, database=database, workers=args.workers,
        force=args.force,
    )
    timings["strategy_stages"] = {
        "seconds": round(perf_counter() - stage_started, 3),
        "worker_count": min(max(1, args.workers), len(strategy_reports), os.cpu_count() or 1),
        "strategies": strategy_reports,
    }

    portfolio_started = perf_counter()
    portfolio_path = cohort_dir / "portfolio"
    cached_portfolio, _ = read_json(portfolio_path / "latest.json")
    if (
        not args.force and cached_portfolio
        and cached_portfolio.get("orchestration", {}).get("fingerprint") == fingerprint
    ):
        portfolio_report = cached_portfolio
        portfolio_reused = True
    else:
        from .portfolio_simulation import run_coordinated_portfolio

        portfolio_args = SimpleNamespace(
            strategies=",".join(LIVE_RESEARCH_STRATEGIES),
            enable_disabled_intraday=False,
            artifact_dir=str(cohort_dir / "optimization"),
            event_calendar=None,
            multi_factor_overlay=bool(ALGO_RUNTIME_CONFIG.get("algo_multi_factor_enabled", True)),
            workers=max(1, args.workers), output_dir=str(portfolio_path),
        )
        portfolio_report = run_coordinated_portfolio(portfolio_args)
        portfolio_report["orchestration"] = {
            "run_id": run_id, "fingerprint": fingerprint,
        }
        _atomic_json(portfolio_path / "latest.json", portfolio_report)
        _atomic_json(
            portfolio_path / f"{optimization_end.isoformat()}-portfolio.json",
            portfolio_report,
        )
        portfolio_reused = False
    timings["portfolio_replay"] = {
        "seconds": 0.0 if portfolio_reused else round(perf_counter() - portfolio_started, 3),
        "reused": portfolio_reused,
    }

    factor_ic = (factor_report.get("adaptive_prequential_ic") or {}).get("mean_spearman_ic")
    report = {
        "schema": "swarm_algo_nightly_research_v2",
        "status": "completed", "created_at": datetime.now(timezone.utc).isoformat(),
        "research_only": True, "live_configuration_changed": False,
        "trade_or_order_actions": 0,
        "data": _data_summary(
            history_start, latest_end, optimization_end, latest_age_days,
            covered_symbols, symbols, missing, universe_meta, refresh_report,
        ),
        "research_window": context["research"],
        "optimization_window": context["optimization"],
        "factor_model": factor_report,
        "strategy_jobs": strategy_reports,
        "portfolio_coordinator_replay": portfolio_report,
        "strategy_artifacts": {
            strategy: {
                "research": item["research_artifact"],
                "optimization": item["optimization_artifact"],
                "research_reused": item["research_reused"],
                "optimization_reused": item["optimization_reused"],
                "latest_training_selection": item["latest_training_selection"],
                "promotion_gate": item["promotion_gate"],
            }
            for strategy, item in strategy_reports.items()
        },
        "portfolio_summary": portfolio_report.get("fold_summary"),
        "recommendation": {
            "strategy_profiles": "Training-window selections were evaluated on later out-of-sample folds; review artifacts before any promotion.",
            "multi_factor": (
                "Factor model diagnostics are available for human review; weights remain research-only."
                if factor_ic is not None and factor_ic > 0
                else "Factor model did not show positive adaptive prequential IC; retain live weights."
            ),
            "takeover_requires_human_confirmation": True,
        },
        "orchestration": {**context, "cache_hit": False},
        "stage_timings": {**timings, "total_seconds": round(perf_counter() - total_started, 3)},
        "limitations": [
            "The fixed current Algo universe is used across all historical periods.",
            "Intraday trend and momentum are researched with replay-only overrides while disabled in live config.",
            "The portfolio replay includes only strategies active in live config and available historical overlays.",
            "Each walk-forward fold begins with fresh capital; positions and risk state do not carry between folds.",
            "The artifacts are research evidence, not a performance forecast or deployment approval.",
        ],
    }
    _atomic_json(report_path, report)
    _atomic_json(root / "latest.json", report)
    return report


def _run_strategy_stages(
    *, context: dict, cohort_dir: Path, output_root: Path,
    symbols: list[str], database: str, workers: int, force: bool,
) -> dict[str, dict]:
    research_root = cohort_dir / "research"
    optimization_root = cohort_dir / "optimization"
    research_root.mkdir(parents=True, exist_ok=True)
    optimization_root.mkdir(parents=True, exist_ok=True)

    statuses = {}
    jobs = []
    for strategy in RESEARCHABLE_STRATEGIES:
        research_path = research_root / strategy / "latest.json"
        optimization_path = optimization_root / strategy / "latest.json"
        research_artifact = None if force else valid_cohort_research(
            research_path, strategy=strategy, fingerprint=context["fingerprint"],
        )
        research_reused = research_artifact is not None
        if research_artifact is None and not force:
            external = _load_standalone_research(
                output_root, strategy=strategy, context=context,
            )
            if external is not None:
                research_artifact = external
                research_artifact["orchestration"] = _artifact_context(context)
                _atomic_json(research_path, research_artifact)
                research_reused = True

        research_bytes = research_path.read_bytes() if research_path.is_file() else b""
        optimization_artifact = None if force or not research_artifact else valid_cohort_optimization(
            optimization_path, strategy=strategy,
            fingerprint=context["fingerprint"], research_path=research_path,
            research_bytes=research_bytes,
        )
        optimization_reused = optimization_artifact is not None
        if optimization_artifact is None and not force and research_artifact is not None:
            external_pair = _load_standalone_pair(
                output_root, strategy=strategy, context=context,
                research_artifact=research_artifact,
            )
            if external_pair is not None:
                optimization_artifact = external_pair
                optimization_artifact["orchestration"] = _artifact_context(context)
                discovered = optimization_artifact["source"]["candidate_profiles_artifact"]
                discovered["path"] = str(research_path.resolve())
                discovered["sha256"] = hashlib.sha256(research_bytes).hexdigest()
                _atomic_json(optimization_path, optimization_artifact)
                optimization_reused = True

        statuses[strategy] = {
            "research_reused": research_reused,
            "optimization_reused": optimization_reused,
            "research_artifact": str(research_path.resolve()),
            "optimization_artifact": str(optimization_path.resolve()),
            "latest_training_selection": (
                optimization_artifact.get("latest_training_selection")
                if optimization_artifact else None
            ),
            "promotion_gate": optimization_artifact.get("promotion_gate") if optimization_artifact else None,
        }
        if not research_reused or not optimization_reused:
            jobs.append((
                strategy, context, symbols, database, research_path,
                optimization_path, research_reused, optimization_reused,
            ))

    if jobs:
        completed = 0
        for strategy, result in _run_strategy_jobs_in_batches(jobs, workers=workers):
            statuses[strategy].update(result)
            completed += 1
            print(
                f"Completed nightly strategy stages {completed}/{len(jobs)}: {strategy}",
                file=sys.stderr, flush=True,
            )

    try:
        _validate_cohort_artifacts(statuses)
    except ValueError as exc:
        if "not cohort-aligned" not in str(exc):
            raise
        # Standalone optimization artifacts can be individually valid but
        # come from different fold windows. Refit only the optimization stage
        # for the whole cohort so the coordinator receives one aligned set.
        alignment_jobs = [
            (
                strategy, context, symbols, database,
                Path(statuses[strategy]["research_artifact"]),
                Path(statuses[strategy]["optimization_artifact"]),
                True, False,
            )
            for strategy in RESEARCHABLE_STRATEGIES
        ]
        for strategy, result in _run_strategy_jobs_in_batches(
            alignment_jobs, workers=workers,
        ):
            statuses[strategy].update(result)
        _validate_cohort_artifacts(statuses)
    return statuses


def _run_strategy_jobs_in_batches(jobs: list[tuple], *, workers: int) -> list[tuple[str, dict]]:
    """Share the CPU budget between strategy jobs and each job's walk-forward folds."""
    if not jobs:
        return []
    worker_budget = min(max(1, workers), os.cpu_count() or 1)
    fold_workers = min(MAX_WALK_FORWARD_WORKERS_PER_STRATEGY, worker_budget)
    strategy_concurrency = min(
        len(jobs), max(1, (worker_budget + fold_workers - 1) // fold_workers),
    )
    results = []
    for offset in range(0, len(jobs), strategy_concurrency):
        batch = jobs[offset:offset + strategy_concurrency]
        batch_fold_workers = min(fold_workers, max(1, worker_budget // len(batch)))
        batch_payloads = [(*job, batch_fold_workers) for job in batch]
        with ProcessPoolExecutor(max_workers=len(batch_payloads)) as executor:
            futures = [executor.submit(_run_strategy_job, payload) for payload in batch_payloads]
            results.extend(future.result() for future in as_completed(futures))
    return results


def _run_strategy_job(payload: tuple) -> tuple[str, dict]:
    (
        strategy, context, symbols, database, research_path, optimization_path,
        research_reused, optimization_reused, walk_forward_workers,
    ) = payload
    from .strategy_optimization import run_strategy_optimization
    from .strategy_research import run_strategy_research

    research_path, optimization_path = Path(research_path), Path(optimization_path)
    stage_context = _artifact_context(context)
    timings = {}
    if not research_reused:
        research_args = SimpleNamespace(
            strategy=strategy, symbols=",".join(symbols), db=database,
            start=context["research"]["start"], end=context["research"]["end"],
            slippage_bps=_RESEARCH_COSTS["slippage_bps"],
            spread_bps=_RESEARCH_COSTS["spread_bps"],
            commission_per_share=_RESEARCH_COSTS["commission_per_share"],
            short_borrow_annual_pct=_RESEARCH_COSTS["short_borrow_annual_pct"],
            unknown_borrow_penalty_bps=_RESEARCH_COSTS["unknown_borrow_penalty_bps"],
            force_close_at_end=_RESEARCH_COSTS["force_close_at_end"],
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        stage_started = perf_counter()
        report = run_strategy_research(research_args)
        report["orchestration"] = stage_context
        _atomic_json(research_path, report)
        _atomic_json(
            research_path.parent / f"{context['run_id']}-research.json", report,
        )
        timings["research_seconds"] = round(perf_counter() - stage_started, 3)

    research_artifact, research_bytes = read_json(research_path)
    if not research_artifact or research_bytes is None:
        raise ValueError(f"Research artifact could not be read after completion: {research_path}")

    if not optimization_reused:
        optimization_args = SimpleNamespace(
            strategy=strategy, symbols=",".join(symbols), db=database,
            start=context["optimization"]["start"], end=context["optimization"]["end"],
            train_days=STRATEGY_TRAIN_DAYS, test_days=STRATEGY_TEST_DAYS,
            step_days=STRATEGY_STEP_DAYS, min_train_trades=STRATEGY_MIN_TRAIN_TRADES,
            max_train_drawdown_pct=STRATEGY_MAX_TRAIN_DRAWDOWN_PCT,
            workers=walk_forward_workers, slippage_bps=_OPTIMIZATION_COSTS["slippage_bps"],
            spread_bps=_OPTIMIZATION_COSTS["spread_bps"],
            commission_per_share=_OPTIMIZATION_COSTS["commission_per_share"],
            short_borrow_annual_pct=_OPTIMIZATION_COSTS["short_borrow_annual_pct"],
            unknown_borrow_penalty_bps=_OPTIMIZATION_COSTS["unknown_borrow_penalty_bps"],
            borrow_availability=None, research_artifact=str(research_path.resolve()),
            output_dir=str(optimization_path.parent.parent),
        )
        stage_started = perf_counter()
        result = run_strategy_optimization(optimization_args)
        optimization_path.parent.mkdir(parents=True, exist_ok=True)
        dated_path = Path(result["dated_artifact"])
        for path in (Path(result["artifact"]), dated_path):
            stored, _ = read_json(path)
            if not stored:
                raise ValueError(f"Optimization artifact could not be read after completion: {path}")
            stored["orchestration"] = stage_context
            _atomic_json(path, stored)
        timings["optimization_seconds"] = round(perf_counter() - stage_started, 3)

    optimization_artifact, _ = read_json(optimization_path)
    if not optimization_artifact:
        raise ValueError(f"Optimization artifact could not be read after completion: {optimization_path}")
    return strategy, {
        "research_reused": research_reused,
        "optimization_reused": optimization_reused,
        "walk_forward_workers": walk_forward_workers,
        "research_artifact": str(research_path.resolve()),
        "optimization_artifact": str(optimization_path.resolve()),
        "latest_training_selection": optimization_artifact.get("latest_training_selection"),
        "promotion_gate": optimization_artifact.get("promotion_gate"),
        **timings,
    }


def _load_standalone_research(output_root: Path, *, strategy: str, context: dict) -> dict | None:
    source = output_root / "research" / strategy / "latest.json"
    artifact, _ = read_json(source)
    if not artifact:
        return None
    research = context["research"]
    data = artifact.get("data") or {}
    if (
        artifact.get("schema") != "swarm_algo_strategy_research_v1"
        or artifact.get("strategy") != strategy
        or data.get("start") != research["start"]
        or data.get("end") != research["end"]
        or data.get("requested_symbols") != context["symbols"]
        or data.get("database") != context["database"]
        or artifact.get("profile_search", {}).get("version") != context["profile_search_versions"][strategy]
        or artifact.get("live_configuration", {}).get("sha256") != context["strategy_configuration_sha256"][strategy]
        or artifact.get("transaction_cost_model", {}).get("slippage_bps_each_side") != _RESEARCH_COSTS["slippage_bps"]
        or artifact.get("transaction_cost_model", {}).get("spread_bps") != _RESEARCH_COSTS["spread_bps"]
        or artifact.get("transaction_cost_model", {}).get("commission_per_share_each_side") != _RESEARCH_COSTS["commission_per_share"]
        or artifact.get("transaction_cost_model", {}).get("short_borrow_annual_pct") != _RESEARCH_COSTS["short_borrow_annual_pct"]
        or artifact.get("transaction_cost_model", {}).get("unknown_borrow_adverse_fill_penalty_bps") != _RESEARCH_COSTS["unknown_borrow_penalty_bps"]
        or artifact.get("transaction_cost_model", {}).get("force_close_at_end") != _RESEARCH_COSTS["force_close_at_end"]
        or not _artifact_is_recent(artifact)
    ):
        return None
    return artifact


def _load_standalone_pair(
    output_root: Path, *, strategy: str, context: dict,
    research_artifact: dict,
) -> dict | None:
    research_path = output_root / "research" / strategy / "latest.json"
    optimization_path = output_root / "optimization" / strategy / "latest.json"
    artifact, _ = read_json(optimization_path)
    research_source, research_bytes = read_json(research_path)
    if not artifact or not research_source or research_bytes is None:
        return None
    optimization = artifact.get("optimization") or {}
    source = artifact.get("source") or {}
    expected = context["optimization"]
    discovered = source.get("candidate_profiles_artifact") or {}
    costs = optimization.get("transaction_cost_model") or {}
    if (
        artifact.get("schema") != "swarm_algo_strategy_optimization_v1"
        or artifact.get("strategy") != strategy
        or optimization.get("start") != expected["start"]
        or optimization.get("end") != expected["end"]
        or optimization.get("feed") != context["feed"]
        or optimization.get("data_adjustment") != context["adjustment"]
        or optimization.get("symbols") != context["symbols"]
        or source.get("database") != context["database"]
        or source.get("symbols") != context["symbols"]
        or artifact.get("profile_search", {}).get("version") != context["profile_search_versions"][strategy]
        or artifact.get("live_configuration", {}).get("sha256") != context["strategy_configuration_sha256"][strategy]
        or stable_hash(source.get("candidate_profiles")) != stable_hash(research_artifact.get("candidate_profiles"))
        or discovered.get("sha256") != hashlib.sha256(research_bytes).hexdigest()
        or costs.get("slippage_bps_each_side") != _OPTIMIZATION_COSTS["slippage_bps"]
        or costs.get("spread_bps") != _OPTIMIZATION_COSTS["spread_bps"]
        or costs.get("commission_per_share_each_side") != _OPTIMIZATION_COSTS["commission_per_share"]
        or costs.get("short_borrow_annual_pct") != _OPTIMIZATION_COSTS["short_borrow_annual_pct"]
        or costs.get("unknown_borrow_adverse_fill_penalty_bps") != _OPTIMIZATION_COSTS["unknown_borrow_penalty_bps"]
        or not optimization.get("folds")
        or not _artifact_is_recent(artifact)
    ):
        return None
    return artifact


def _artifact_is_recent(artifact: dict) -> bool:
    try:
        created = datetime.fromisoformat(str(artifact["created_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - created.astimezone(timezone.utc)
    return timedelta(0) <= age <= timedelta(days=STRATEGY_ARTIFACT_MAX_AGE_DAYS)


def _validate_cohort_artifacts(statuses: dict[str, dict]) -> None:
    expected_folds = None
    expected_costs = None
    for strategy in RESEARCHABLE_STRATEGIES:
        path = Path(statuses[strategy]["optimization_artifact"])
        artifact, _ = read_json(path)
        if not artifact:
            raise ValueError(f"Missing completed optimization artifact for {strategy}: {path}")
        report = artifact.get("optimization") or {}
        folds = [
            tuple(item.get(name) for name in ("train_start", "train_end", "test_start", "test_end"))
            for item in report.get("folds", [])
        ]
        costs = report.get("transaction_cost_model")
        if expected_folds is None:
            expected_folds, expected_costs = folds, costs
        elif folds != expected_folds or costs != expected_costs:
            raise ValueError(
                f"Optimization artifacts are not cohort-aligned at {strategy}; "
                "the mismatched strategy will be retrained on the next run."
            )


def _last_completed_friday(latest_session: date) -> date:
    days_since_friday = (latest_session.weekday() - 4) % 7
    return latest_session - timedelta(days=days_since_friday)


def _artifact_context(context: dict) -> dict:
    return {
        "run_id": context["run_id"],
        "fingerprint": context["fingerprint"],
        "data_version": context["data_version"],
    }


def _data_summary(
    history_start: date, latest_end: date, cohort_end: date, latest_age_days: int,
    covered_symbols: list[str], requested_symbols: list[str], missing: list[str],
    universe_meta: dict, refresh_report: dict | None,
) -> dict:
    return {
        "tier": data_tier(), "feed": FEED, "adjustment": ADJUSTMENT,
        "range_start": history_start.isoformat(),
        "latest_available_end": latest_end.isoformat(),
        "research_data_end": cohort_end.isoformat(),
        "latest_session_age_days": latest_age_days,
        "freshness": "fresh" if latest_age_days <= 4 else "stale",
        "universe_count": len(requested_symbols),
        "covered_universe_count": len(covered_symbols),
        "covered_symbols": covered_symbols,
        "universe_symbols_without_history": missing,
        **universe_meta,
        "incremental_refresh": refresh_report,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run versioned per-strategy Algo research and coordinator validation",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument(
        "--workers", type=int, default=8,
        help="CPU budget shared by concurrent strategy jobs and their walk-forward folds",
    )
    parser.add_argument("--no-refresh", action="store_true", help="use existing bars without fetching recent SIP history")
    parser.add_argument("--factor-only", action="store_true", help="fit the factor model without strategy or portfolio stages")
    parser.add_argument("--force", action="store_true", help="create a fresh cohort and recompute every stage")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output_dir = Path(args.output_dir)
    today = datetime.now(ET).date().isoformat()
    try:
        with _research_lock(Path(output_dir) / "locks" / "nightly-layers.lock"):
            report = run_daily_research(args)
            report["run_date_et"] = today
            report["workers_requested"] = args.workers
    except (ValueError, OSError, requests.RequestException, RuntimeError) as exc:
        report = {
            "schema": "swarm_algo_nightly_research_v2",
            "status": "error", "run_date_et": today,
            "research_only": True, "live_configuration_changed": False,
            "trade_or_order_actions": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }
        print(json.dumps(report, sort_keys=True), file=sys.stderr)
        _atomic_json(output_dir / "latest.json", report)
        return 2

    _atomic_json(output_dir / f"{today}.json", report)
    _atomic_json(output_dir / "latest.json", report)
    print(json.dumps({
        "status": report["status"],
        "report": str((output_dir / "latest.json").resolve()),
        "run_id": report.get("orchestration", {}).get("run_id"),
        "range": [report["data"]["range_start"], report["data"].get("research_data_end")],
        "strategies": len(report.get("strategy_jobs", {})),
        "portfolio": report.get("portfolio_summary"),
        "cache_hit": report.get("orchestration", {}).get("cache_hit", False),
        "live_configuration_changed": False,
    }, sort_keys=True, default=str))
    return 0
