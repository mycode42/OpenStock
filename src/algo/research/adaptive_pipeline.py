"""Adaptive discovery, rolling verification, and portfolio artifact stages."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace

from src.algo.cpu import available_cpu_count

from .artifact_cache import read_json
from .files import _atomic_json
from .live_manifest import live_configuration_manifest
from .settings import (
    ADAPTIVE_DISCOVERY_CANDIDATES, ADAPTIVE_DISCOVERY_ITERATIONS,
    ADAPTIVE_VERIFICATION_CANDIDATES,
    ADAPTIVE_VERIFICATION_STEP_DAYS, ADAPTIVE_VERIFICATION_TEST_DAYS,
    ADAPTIVE_WINDOW_DAYS, ADJUSTMENT, ET, FEED,
    MAX_ADAPTIVE_FOLD_WORKERS_PER_STRATEGY, RESEARCHABLE_STRATEGIES,
    STRATEGY_FLAVORS,
)

ADAPTIVE_PIPELINE_VERSION = "algo-research-layers-v8-iterative-candidate-search-2024"
ADAPTIVE_VERIFICATION_VERSION = "algo-adaptive-verification-v7-rolling-2024"
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
    workers: int, force: bool,
) -> dict:
    """Run recent-window discovery for every strategy, reusing today's artifacts."""
    statuses = {}
    jobs = []
    total = len(RESEARCHABLE_STRATEGIES)
    logger.info("Discovery: checking reusable results for %d strategies", total)
    for strategy in RESEARCHABLE_STRATEGIES:
        options = _adaptive_window_options(strategy)
        latest_path = output_dir / strategy / "latest-discovery.json"
        prior, _ = read_json(latest_path)
        window_search = (prior or {}).get("adaptive_window_search") or {}
        reusable = bool(
            not force
            and prior
            and prior.get("schema") == "swarm_algo_adaptive_discovery_v1"
            and prior.get("strategy") == strategy
            and prior.get("window", {}).get("end") == latest_end.isoformat()
            and window_search.get("pipeline_version") == ADAPTIVE_PIPELINE_VERSION
            and window_search.get("configured_window_days") == options
            and window_search.get("candidate_count") == ADAPTIVE_DISCOVERY_CANDIDATES
            and window_search.get("search_iterations") == ADAPTIVE_DISCOVERY_ITERATIONS
            and window_search.get("data_version") == data_version
            and prior.get("data_tier") == "algo_trader_plus"
            and prior.get("feed") == FEED
            and prior.get("adjustment") == ADJUSTMENT
            and set(prior.get("symbols") or []) == set(symbols)
            and prior.get("database") == str(Path(database).resolve())
        )
        if reusable:
            statuses[strategy] = _discovery_status(prior, latest_path, "reused")
            logger.info(
                "Discovery progress %d/%d: %s reused",
                len(statuses), total, strategy,
            )
        else:
            jobs.append((strategy, database, symbols, latest_end, data_version, output_dir, options))

    if jobs:
        worker_count = min(max(1, workers), available_cpu_count())
        logger.info(
            "Discovery: running %d uncached strategy searches; each candidate search can use up to %d workers",
            len(jobs), worker_count,
        )
        for job in jobs:
            strategy = job[0]
            try:
                statuses[strategy] = _run_adaptive_discovery_job((*job, worker_count))
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
        "window_days_by_strategy": {
            strategy: _adaptive_window_options(strategy) for strategy in RESEARCHABLE_STRATEGIES
        },
        "strategies": {strategy: statuses[strategy] for strategy in RESEARCHABLE_STRATEGIES if strategy in statuses},
        "error_strategies": errors,
    }


def _run_adaptive_discovery_job(payload: tuple) -> dict:
    strategy, database, symbols, latest_end, data_version, output_dir, options, candidate_workers = payload
    from .adaptive import run_window_ladder_discovery

    discovery_args = SimpleNamespace(
        strategy=strategy, symbols=",".join(symbols), db=database,
        adjustment=ADJUSTMENT, search_method="bayes",
        candidates=ADAPTIVE_DISCOVERY_CANDIDATES,
        search_iterations=ADAPTIVE_DISCOVERY_ITERATIONS,
        seed=latest_end.toordinal() + sum(map(ord, strategy)),
        search_space=None, fitness_weights=None, min_trades=3, capital=100_000.0,
        slippage_bps=5.0, spread_bps=3.0, commission_per_share=0.005,
        short_borrow_annual_pct=3.0, unknown_borrow_penalty_bps=25.0,
        borrow_availability=None, start=None, end=latest_end.isoformat(),
        window_days=options[len(options) // 2], output_dir=str(output_dir),
        candidate_workers=candidate_workers,
    )
    report = run_window_ladder_discovery(
        discovery_args, options, pipeline_version=ADAPTIVE_PIPELINE_VERSION,
        data_version=data_version,
    )
    detail, _ = read_json(report["latest_detail_artifact"])
    if not detail:
        raise ValueError(f"adaptive discovery artifact was not readable for {strategy}")
    latest = Path(report["latest_detail_artifact"])
    return _discovery_status(detail, latest, "completed")


def _discovery_status(detail: dict, path: Path, status: str) -> dict:
    selected = detail.get("selected_candidate") or {}
    parent = detail.get("parent_candidate") or {}
    return {
        "status": status, "strategy": detail.get("strategy"),
        "selection_status": detail.get("selection_status"),
        "adaptive_improvement": bool(detail.get("adaptive_improvement")),
        "search_iterations": detail.get("search_iterations", 1),
        "search_iteration_summaries": detail.get("search_iteration_summaries", []),
        "window": detail.get("window"),
        "window_search": detail.get("adaptive_window_search"),
        "parent_candidate": {
            "used": bool(parent.get("used")),
            "candidate": parent.get("candidate"),
            "window_end": parent.get("window_end"),
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
) -> dict:
    """Verify the short-window fitting policy on recent chronological folds."""
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
                output_dir, options,
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
        and data.get("feed") == FEED
        and data.get("adjustment") == ADJUSTMENT
        and config.get("train_days") == max(max(values) for values in ADAPTIVE_WINDOW_DAYS.values())
        and config.get("train_window_options") == options
        and config.get("test_days") == ADAPTIVE_VERIFICATION_TEST_DAYS
        and config.get("step_days") == ADAPTIVE_VERIFICATION_STEP_DAYS
        and search.get("method") == "bayes"
        and search.get("candidates_per_fold") == ADAPTIVE_VERIFICATION_CANDIDATES
    )


def _run_adaptive_verification_job(payload: tuple) -> dict:
    strategy, database, symbols, start, end, data_version, output_dir, options, fold_workers = payload
    from .adaptive import run_adaptive_walk_forward

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
        workers=fold_workers, minimum_folds=3, minimum_oos_sharpe=0.0,
        maximum_average_decay=0.35, output_dir=str(output_dir),
        adaptive_policy_version=ADAPTIVE_VERIFICATION_VERSION,
        data_version=data_version,
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
