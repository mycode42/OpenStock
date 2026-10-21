"""Adaptive discovery, rolling verification, and portfolio artifact stages."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
import hashlib
import json
import logging
import multiprocessing as mp
from pathlib import Path
from types import SimpleNamespace

from autoresearch.cpu import available_cpu_count

from ..artifacts.artifact_cache import read_json, stable_hash
from ..artifacts.files import _atomic_json
from ..artifacts.live_manifest import live_configuration_manifest
from ..artifacts.candidate_archive import record_verification, reserve_search_seed
from ..settings import (
    ADAPTIVE_MINIMUM_FOLDS, ADAPTIVE_PURGE_SESSIONS,
    ADAPTIVE_DISCOVERY_CANDIDATES, ADAPTIVE_DISCOVERY_ITERATIONS,
    ADAPTIVE_VERIFICATION_CANDIDATES,
    ADAPTIVE_VERIFICATION_STEP_DAYS, ADAPTIVE_VERIFICATION_TEST_DAYS,
    ADAPTIVE_WINDOW_DAYS, ADJUSTMENT, CORE_RESEARCH_CONTROLS, ET, FEED,
    MINIMUM_POSITIVE_OOS_FOLD_FRACTION, ROBUSTNESS_BOOTSTRAP_ITERATIONS,
    ROBUSTNESS_COST_MULTIPLIERS,
    MULTI_FACTOR_MIN_UNIVERSE_VALUES,
    MAX_ADAPTIVE_FOLD_WORKERS_PER_STRATEGY, RESEARCHABLE_STRATEGIES,
    STRATEGY_FLAVORS,
)

ADAPTIVE_PIPELINE_VERSION = "algo-research-layers-v17-robust-archive-candidate-cohort-2026"
ADAPTIVE_VERIFICATION_VERSION = "algo-adaptive-verification-v12-ridge-weight-replay-2026"
_ADAPTIVE_COST_MODEL = {
    "slippage_bps": 5.0,
    "spread_bps": 3.0,
    "commission_per_share": 0.005,
    "short_borrow_annual_pct": 3.0,
    "unknown_borrow_penalty_bps": 25.0,
}
logger = logging.getLogger(__name__)


def _quiet_research_worker_logging() -> None:
    """Keep child simulations from writing into the parent's rotating progress log."""
    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
        handler.close()
    root_logger.addHandler(logging.NullHandler())
    root_logger.setLevel(logging.CRITICAL)
    strategy_logger = logging.getLogger("strategy_runner")
    strategy_logger.handlers.clear()
    strategy_logger.setLevel(logging.CRITICAL)


def _adaptive_window_options(strategy: str) -> list[int]:
    return list(ADAPTIVE_WINDOW_DAYS[STRATEGY_FLAVORS[strategy]])


def run_adaptive_discoveries(
    *, database: str, symbols: list[str], latest_end: date, data_version: str, output_dir: Path,
    workers: int, factor_weight_timeline: list[dict] | None = None,
    invocation_id: str | None = None,
) -> dict:
    """Run a new seeded search for every strategy on each research invocation."""
    statuses = {}
    jobs = []
    total = len(RESEARCHABLE_STRATEGIES)
    logger.info("Discovery: reserving new search sequences for %d strategies", total)
    for strategy in RESEARCHABLE_STRATEGIES:
        options = _adaptive_window_options(strategy)
        archive_dir = output_dir / "archive"
        seed, sequence = reserve_search_seed(archive_dir, strategy, ADAPTIVE_PIPELINE_VERSION)
        jobs.append((
            strategy, database, symbols, latest_end, data_version,
            output_dir, options, archive_dir, seed, sequence,
            factor_weight_timeline or [], invocation_id,
        ))

    if jobs:
        worker_budget = min(max(1, workers), available_cpu_count())
        strategy_concurrency = min(len(jobs), max(1, (worker_budget + 3) // 4))
        batch_count = (len(jobs) + strategy_concurrency - 1) // strategy_concurrency
        logger.info(
            "Discovery: running %d new strategy searches with %d candidate workers; up to %d strategies concurrently",
            len(jobs), worker_budget, strategy_concurrency,
        )
        for offset in range(0, len(jobs), strategy_concurrency):
            batch = jobs[offset:offset + strategy_concurrency]
            batch_number = offset // strategy_concurrency + 1
            if len(batch) == 1:
                job = batch[0]
                strategy = job[0]
                logger.info(
                    "Discovery batch %d/%d: running %s with up to %d candidate workers",
                    batch_number, batch_count, strategy, worker_budget,
                )
                try:
                    statuses[strategy] = _run_adaptive_discovery_job((*job, worker_budget))
                except Exception as exc:
                    statuses[strategy] = {
                        "status": "error", "strategy": strategy,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                status = statuses[strategy].get("status", "unknown")
                log = logger.warning if status == "error" else logger.info
                log("Discovery progress %d/%d: %s %s", len(statuses), total, strategy, status)
                continue

            # Strategy threads coordinate independent candidate process pools.
            # Split the candidate worker budget so concurrent strategies cannot
            # multiply the configured CPU limit.
            base_workers, extra_workers = divmod(worker_budget, len(batch))
            candidate_workers = [
                base_workers + (1 if index < extra_workers else 0)
                for index in range(len(batch))
            ]
            logger.info(
                "Discovery batch %d/%d: launching %d strategies with candidate worker counts %s",
                batch_number, batch_count, len(batch), candidate_workers,
            )
            with ThreadPoolExecutor(
                max_workers=len(batch), thread_name_prefix="adaptive-discovery",
            ) as executor:
                futures = {
                    executor.submit(_run_adaptive_discovery_job, (*job, candidate_workers[index])): job[0]
                    for index, job in enumerate(batch)
                }
                for future in as_completed(futures):
                    strategy = futures[future]
                    try:
                        statuses[strategy] = future.result()
                    except Exception as exc:
                        statuses[strategy] = {
                            "status": "error", "strategy": strategy,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    status = statuses[strategy].get("status", "unknown")
                    log = logger.warning if status == "error" else logger.info
                    log("Discovery progress %d/%d: %s %s", len(statuses), total, strategy, status)
    errors = [strategy for strategy, item in statuses.items() if item.get("status") == "error"]
    stage_status = "error" if len(errors) == total else "completed" if not errors else "partial_error"
    logger.info("Discovery stage finished: %s (%d errors)", stage_status, len(errors))
    return {
        "status": stage_status,
        "invocation_id": invocation_id,
        "window_days_by_strategy": {
            strategy: _adaptive_window_options(strategy) for strategy in RESEARCHABLE_STRATEGIES
        },
        "strategies": {strategy: statuses[strategy] for strategy in RESEARCHABLE_STRATEGIES if strategy in statuses},
        "error_strategies": errors,
    }


def _run_adaptive_discovery_job(payload: tuple) -> dict:
    (strategy, database, symbols, latest_end, data_version, output_dir, options,
     archive_dir, seed, sequence, factor_weight_timeline, invocation_id,
     candidate_workers) = payload
    from .adaptive_discovery import run_window_ladder_discovery
    from .adaptive_search import _run_search_with_bars

    discovery_args = SimpleNamespace(
        strategy=strategy, symbols=",".join(symbols), db=database,
        adjustment=ADJUSTMENT, search_method="bayes",
        candidates=ADAPTIVE_DISCOVERY_CANDIDATES,
        search_iterations=ADAPTIVE_DISCOVERY_ITERATIONS,
        seed=seed, archive_dir=str(archive_dir), archive_sequence=sequence,
        search_space=None, fitness_weights=None, min_trades=3, capital=100_000.0,
        data_version=data_version,
        slippage_bps=5.0, spread_bps=3.0, commission_per_share=0.005,
        short_borrow_annual_pct=3.0, unknown_borrow_penalty_bps=25.0,
        borrow_availability=None, start=None, end=latest_end.isoformat(),
        window_days=options[len(options) // 2], output_dir=str(output_dir),
        candidate_workers=candidate_workers,
        factor_weight_timeline=factor_weight_timeline,
        invocation_id=invocation_id,
    )
    report = run_window_ladder_discovery(
        discovery_args, options, pipeline_version=ADAPTIVE_PIPELINE_VERSION,
        data_version=data_version, search_func=_run_search_with_bars,
    )
    detail, _ = read_json(report["latest_detail_artifact"])
    if not detail:
        raise ValueError(f"adaptive discovery artifact was not readable for {strategy}")
    latest = Path(report["latest_detail_artifact"])
    return {
        **_discovery_status(detail, latest, "completed"),
        "archive_sequence": sequence,
        "search_seed": seed,
    }


def _discovery_status(detail: dict, path: Path, status: str) -> dict:
    selected = detail.get("selected_candidate") or {}
    parent = detail.get("parent_candidate") or {}
    return {
        "status": status, "strategy": detail.get("strategy"),
        "selection_status": detail.get("selection_status"),
        "adaptive_improvement": bool(detail.get("adaptive_improvement")),
        "search_iterations": detail.get("search_iterations", 1),
        "search_iteration_summaries": detail.get("search_iteration_summaries", []),
        "adaptive_novelty": detail.get("adaptive_novelty"),
        "window": detail.get("window"),
        "window_search": detail.get("adaptive_window_search"),
        "archive_sequence": detail.get("archive_sequence"),
        "search_seed": detail.get("search_seed"),
        "parent_candidate": {
            "used": bool(parent.get("used")),
            "candidate": parent.get("candidate"),
            "window_end": parent.get("window_end"),
            "candidate_ids": parent.get("candidate_ids", []),
            "reason": parent.get("reason"),
        },
        "selected_candidate": {
            "name": selected.get("name"), "parameters": selected.get("parameters"),
            "metrics": selected.get("metrics"), "eligible": selected.get("eligible"),
        },
        "detail_artifact": str(path.resolve()),
    }


def run_adaptive_verifications(
    *, database: str, symbols: list[str], start: date, end: date,
    data_version: str, output_dir: Path, workers: int, force: bool,
    factor_weight_timeline: list[dict] | None = None,
) -> dict:
    """Verify the short-window fitting policy on recent chronological folds."""
    factor_weight_timeline = list(factor_weight_timeline or [])
    factor_weight_timeline_sha256 = stable_hash(factor_weight_timeline)
    statuses = {}
    jobs = []
    total = len(RESEARCHABLE_STRATEGIES)
    logger.info("Verification: checking reusable fold results for %d strategies", total)
    for strategy in RESEARCHABLE_STRATEGIES:
        options = _adaptive_window_options(strategy)
        latest_path = output_dir / strategy / "latest-walk-forward.json"
        prior, _ = read_json(latest_path)
        cached_summary, _ = read_json(output_dir / strategy / "latest-walk-forward-summary.json")
        if not force and _adaptive_verification_is_reusable(
            prior, strategy=strategy, database=database, symbols=symbols,
            start=start, end=end, options=options, data_version=data_version,
            factor_weight_timeline_sha256=factor_weight_timeline_sha256,
        ) and cached_summary:
            statuses[strategy] = {
                "status": "reused", "strategy": strategy,
                "summary": cached_summary, "detail_artifact": str(latest_path.resolve()),
            }
            logger.info(
                "Verification progress %d/%d: %s folds reused",
                len(statuses), total, strategy,
            )
        else:
            jobs.append((
                strategy, database, symbols, start, end, data_version,
                output_dir, options, factor_weight_timeline,
            ))

    if jobs:
        budget = min(max(1, workers), available_cpu_count())
        fold_workers = min(MAX_ADAPTIVE_FOLD_WORKERS_PER_STRATEGY, budget)
        concurrency = min(len(jobs), max(1, (budget + fold_workers - 1) // fold_workers))
        for offset in range(0, len(jobs), concurrency):
            batch = jobs[offset:offset + concurrency]
            per_strategy_workers = min(fold_workers, max(1, budget // len(batch)))
            logger.info(
                "Verification batch %d/%d: launching %d strategies with up to %d fold workers each",
                offset // concurrency + 1,
                (len(jobs) + concurrency - 1) // concurrency,
                len(batch), per_strategy_workers,
            )
            with ProcessPoolExecutor(
                max_workers=len(batch), initializer=_quiet_research_worker_logging,
                mp_context=mp.get_context("spawn"),
            ) as executor:
                futures = {
                    executor.submit(_run_adaptive_verification_job, (*job, per_strategy_workers)): job[0]
                    for job in batch
                }
                for future in as_completed(futures):
                    strategy = futures[future]
                    try:
                        statuses[strategy] = future.result()
                    except Exception as exc:
                        statuses[strategy] = {
                            "status": "error", "strategy": strategy,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    status = statuses[strategy].get("status", "unknown")
                    log = logger.warning if status == "error" else logger.info
                    log(
                        "Verification progress %d/%d: %s folds %s",
                        len(statuses), total, strategy, status,
                    )
    for index, strategy in enumerate(RESEARCHABLE_STRATEGIES, start=1):
        status = statuses.get(strategy) or {}
        if status.get("status") == "error":
            logger.warning(
                "Coordinator artifact progress %d/%d: %s unavailable (%s)",
                index, total, strategy, status.get("error", "verification failed"),
            )
            continue
        detail_path = status.get("detail_artifact")
        if detail_path:
            detail, _ = read_json(Path(detail_path))
            if detail:
                status["archive_verification_folds_added"] = record_verification(
                    output_dir / "archive", strategy, detail,
                )
                statuses[strategy] = status
        detail, _ = read_json(output_dir / strategy / "latest-walk-forward.json")
        summary = status.get("summary")
        if not summary:
            summary, _ = read_json(output_dir / strategy / "latest-walk-forward-summary.json")
        try:
            if not detail or not summary:
                raise ValueError("adaptive verification detail or summary is missing")
            artifact_path = _write_adaptive_coordinator_artifact(
                detail, summary, output_dir / "verification-coordinator",
            )
            status["coordinator_artifact"] = str(artifact_path.resolve())
            statuses[strategy] = status
            logger.info("Coordinator artifact progress %d/%d: %s ready", index, total, strategy)
        except Exception as exc:
            statuses[strategy] = {
                **status, "status": "error", "strategy": strategy,
                "error": f"{type(exc).__name__}: {exc}",
            }
            logger.warning(
                "Coordinator artifact progress %d/%d: %s failed (%s)",
                index, total, strategy, statuses[strategy]["error"],
            )
    errors = [strategy for strategy, item in statuses.items() if item.get("status") == "error"]
    stage_status = "error" if len(errors) == total else "completed" if not errors else "partial_error"
    logger.info("Verification stage finished: %s (%d errors)", stage_status, len(errors))
    return {
        "status": stage_status,
        "window_days_by_strategy": {
            strategy: _adaptive_window_options(strategy) for strategy in RESEARCHABLE_STRATEGIES
        },
        "verification_range": {"start": start.isoformat(), "end": end.isoformat()},
        "strategies": {strategy: statuses[strategy] for strategy in RESEARCHABLE_STRATEGIES if strategy in statuses},
        "error_strategies": errors,
    }


def _write_adaptive_coordinator_artifact(detail: dict, summary: dict, output_dir: Path) -> Path:
    """Adapt verified short-window fold choices to the coordinator input schema."""
    strategy = detail["strategy"]
    data = detail["data"]
    folds = []
    for item in detail.get("folds") or []:
        controls = item.get("train_controls") or {}
        folds.append({
            **{key: item[key] for key in ("train_start", "train_end", "test_start", "test_end")},
            "selected_profile": item["selected_candidate"],
            "selected_parameters": {strategy: item["train_parameters"]},
            "selected_strategies": [strategy],
            "selected_controls": controls,
            "selected_reason": (
                f"Adaptive {item['fit_window_days']}-session fit beat baseline on training data"
                if item.get("adaptive_improvement")
                else "Baseline retained after no qualifying short-window improvement"
            ),
            "adaptive_improvement": bool(item.get("adaptive_improvement")),
            "fit_window_days": item.get("fit_window_days"),
            "out_of_sample": item.get("oos_metrics"),
        })
    if len(folds) < 3:
        raise ValueError(f"adaptive verification needs at least three folds for {strategy}")
    output_dir = output_dir / strategy
    output_dir.mkdir(parents=True, exist_ok=True)
    detail_path = Path(detail.get("detail_artifact") or output_dir / "latest-walk-forward.json")
    try:
        detail_bytes = detail_path.read_bytes()
    except OSError:
        detail_bytes = json.dumps(detail, sort_keys=True).encode()
    run_date = datetime.now(ET).date().isoformat()
    promotion = {
        "promotable": bool((summary.get("checks") or {}).get("overall_pass")),
        "source": "short_window_adaptive_walk_forward",
        "checks": summary.get("checks"),
    }
    artifact = {
        "schema": "swarm_algo_adaptive_coordinator_v1",
        "stage": "adaptive_verification_coordinator",
        "research_only": True,
        "live_configuration_changed": False,
        "trade_or_order_actions": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "strategy": strategy,
        "strategy_flavor": STRATEGY_FLAVORS[strategy],
        "direction_scope": "both",
        "live_configuration": live_configuration_manifest([strategy]),
        "source": {
            "database": data["database"], "symbols": data["symbols"],
            "verification_artifact": str(detail_path.resolve()),
            "verification_sha256": hashlib.sha256(detail_bytes).hexdigest(),
        },
        "verification": {
            "start": data["start"], "end": data["end"], "feed": data["feed"],
            "training_history_start": data.get("requested_start", data["start"]),
            "out_of_sample_range": {
                "start": folds[0]["test_start"], "end": folds[-1]["test_end"],
            },
            "data_adjustment": data["adjustment"], "symbols": data["symbols"],
            "data_tier_at_run": data.get("data_tier"),
            "transaction_cost_model": {
                "slippage_bps_each_side": _ADAPTIVE_COST_MODEL["slippage_bps"],
                "spread_bps": _ADAPTIVE_COST_MODEL["spread_bps"],
                "commission_per_share_each_side": _ADAPTIVE_COST_MODEL["commission_per_share"],
                "short_borrow_annual_pct": _ADAPTIVE_COST_MODEL["short_borrow_annual_pct"],
                "unknown_borrow_adverse_fill_penalty_bps": _ADAPTIVE_COST_MODEL["unknown_borrow_penalty_bps"],
            },
            "folds": folds, "promotion_gate": promotion,
            "adaptive_verification_summary": summary,
        },
        "latest_training_selection": {
            "train_start": folds[-1]["train_start"],
            "train_end": folds[-1]["train_end"],
            "test_start": folds[-1]["test_start"],
            "test_end": folds[-1]["test_end"],
            "profile": folds[-1]["selected_profile"],
            "parameters": folds[-1]["selected_parameters"],
            "strategies": folds[-1]["selected_strategies"],
            "controls": folds[-1]["selected_controls"],
            "reason": folds[-1]["selected_reason"],
        },
        "promotion_gate": promotion,
    }
    latest = output_dir / "latest.json"
    dated = output_dir / f"{run_date}-adaptive-verification.json"
    _atomic_json(latest, artifact)
    _atomic_json(dated, artifact)
    return latest


def _adaptive_verification_is_reusable(
    detail: dict | None, *, strategy: str, database: str, symbols: list[str],
    start: date, end: date, options: list[int], data_version: str,
    factor_weight_timeline_sha256: str,
) -> bool:
    if not detail:
        return False
    config = detail.get("window_config") or {}
    search = detail.get("search") or {}
    data = detail.get("data") or {}
    return bool(
        detail.get("strategy") == strategy
        and detail.get("adaptive_policy_version") == ADAPTIVE_VERIFICATION_VERSION
        and data.get("requested_start", data.get("start")) == start.isoformat()
        and data.get("end") == end.isoformat()
        and data.get("symbols") == symbols
        and data.get("database") == str(Path(database).resolve())
        and data.get("data_version") == data_version
        and detail.get("factor_weight_timeline_sha256") == factor_weight_timeline_sha256
        and data.get("feed") == FEED
        and data.get("adjustment") == ADJUSTMENT
        and config.get("train_days") == max(max(values) for values in ADAPTIVE_WINDOW_DAYS.values())
        and config.get("train_window_options") == options
        and config.get("test_days") == ADAPTIVE_VERIFICATION_TEST_DAYS
        and config.get("step_days") == ADAPTIVE_VERIFICATION_STEP_DAYS
        and config.get("minimum_folds") == ADAPTIVE_MINIMUM_FOLDS
        and config.get("purge_sessions") == ADAPTIVE_PURGE_SESSIONS
        and config.get("minimum_positive_fold_fraction") == MINIMUM_POSITIVE_OOS_FOLD_FRACTION
        and config.get("cost_sensitivity_multipliers") == list(ROBUSTNESS_COST_MULTIPLIERS)
        and config.get("bootstrap_iterations") == ROBUSTNESS_BOOTSTRAP_ITERATIONS
        and search.get("method") == "bayes"
        and search.get("candidates_per_fold") == ADAPTIVE_VERIFICATION_CANDIDATES
        and all(
            (fold.get("train_controls") or {}) == CORE_RESEARCH_CONTROLS
            for fold in detail.get("folds", [])
        )
        and (
            strategy != "multi_factor"
            or (search.get("search_space") or {}).get("min_universe")
            == list(MULTI_FACTOR_MIN_UNIVERSE_VALUES)
        )
    )


def _run_adaptive_verification_job(payload: tuple) -> dict:
    (
        strategy, database, symbols, start, end, data_version, output_dir,
        options, factor_weight_timeline, fold_workers,
    ) = payload
    from .adaptive_verification import run_adaptive_walk_forward

    walk_args = SimpleNamespace(
        strategy=strategy, symbols=",".join(symbols), db=database,
        adjustment=ADJUSTMENT, search_method="bayes",
        candidates=ADAPTIVE_VERIFICATION_CANDIDATES,
        seed=sum(map(ord, strategy)),
        search_space=None, fitness_weights=None, min_trades=3, capital=100_000.0,
        slippage_bps=5.0, spread_bps=3.0, commission_per_share=0.005,
        short_borrow_annual_pct=3.0, unknown_borrow_penalty_bps=25.0,
        borrow_availability=None, start=start.isoformat(), end=end.isoformat(),
        train_days=max(max(values) for values in ADAPTIVE_WINDOW_DAYS.values()),
        train_window_options=options,
        test_days=ADAPTIVE_VERIFICATION_TEST_DAYS,
        step_days=ADAPTIVE_VERIFICATION_STEP_DAYS,
        workers=fold_workers, minimum_folds=ADAPTIVE_MINIMUM_FOLDS, minimum_oos_sharpe=0.0,
        maximum_average_decay=0.35, output_dir=str(output_dir),
        purge_sessions=ADAPTIVE_PURGE_SESSIONS,
        minimum_positive_fold_fraction=MINIMUM_POSITIVE_OOS_FOLD_FRACTION,
        cost_sensitivity_multipliers=list(ROBUSTNESS_COST_MULTIPLIERS),
        bootstrap_iterations=ROBUSTNESS_BOOTSTRAP_ITERATIONS,
        adaptive_policy_version=ADAPTIVE_VERIFICATION_VERSION,
        data_version=data_version,
        factor_weight_timeline=factor_weight_timeline,
    )
    report = run_adaptive_walk_forward(walk_args)
    detail, _ = read_json(report["latest_detail_artifact"])
    return {
        "status": "completed", "strategy": strategy,
        "summary": report.get("summary"),
        "detail_artifact": report.get("latest_detail_artifact"),
        "summary_artifact": report.get("summary_artifact"),
        "worker_count": (detail or {}).get("worker_count"),
    }
