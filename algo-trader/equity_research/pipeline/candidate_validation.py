"""Incremental rolling validation for live, champion, and queued settings."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
from copy import deepcopy
from datetime import date
import math
import multiprocessing as mp
import statistics
from types import SimpleNamespace

from equity_research.cpu import available_cpu_count
from strategies.registry import (
    ALGO_DEFAULT_PARAMETERS, REGIME_PARAMETER_PROFILES,
    refresh_active_parameter_profile,
)
from strategies.multi_factor.features import aggregate_intraday_bars

from ..artifacts.artifact_cache import read_json, stable_hash
from ..artifacts.candidate_archive import (
    archive_reference, archived_candidate, candidate_id, champion_candidate,
    logic_version, model_family,
    pending_validation_candidates, record_candidate_validation_folds,
    record_champion_validation_result, record_validation_queue_result,
    update_champion_if_better,
)
from ..settings import ADJUSTMENT
from .adaptive_common import _prepare_args
from .adaptive_search import _fitness, _load_window_data, _metrics, _run_replay
from .adaptive_scoring import _cost_model_version


VALIDATION_VERSION = "exact-candidate-rolling-oos-v2-champion-live-queue"
VALIDATION_COST_MODEL_VERSION = "algo-replay-cost-v1-slippage5-spread3-commission0.005-borrow3-penalty25"
MAX_QUEUED_CHALLENGERS_PER_STRATEGY = 2
MINIMUM_VALIDATION_FOLDS = 3
MINIMUM_VALIDATION_TRADES = 10
MINIMUM_POSITIVE_FOLD_FRACTION = 0.60


def _evaluate_fold(payload: tuple) -> tuple[int, list[dict]]:
    index, window, candidates, args = payload
    train_start = date.fromisoformat(window["train_start"])
    test_start = date.fromisoformat(window["test_start"])
    test_end = date.fromisoformat(window["test_end"])
    bars, spy = _load_window_data(
        db=args.db, symbols=args.symbols_resolved,
        start=train_start, end=test_end,
        adjustment=args.adjustment, strategy=args.strategy,
    )
    preaggregated_daily_bars = aggregate_intraday_bars(bars)
    rows = []
    for candidate in candidates:
        parameters = candidate["parameters"]
        report = _run_replay(
            strategy=args.strategy, parameters=parameters,
            symbols=args.symbols_resolved, bars=bars, spy_daily=spy,
            start=test_start, end=test_end, args=args, capital=args.capital,
            preaggregated_daily_bars=preaggregated_daily_bars,
            regime_profiles=(
                {args.strategy: candidate["regime_profiles"]}
                if candidate.get("regime_profiles") else None
            ),
        )
        metrics = _metrics(report, capital=args.capital)
        fitness, _ = _fitness(metrics, args.fitness_weights_data)
        rows.append({
            "candidate_id": candidate["candidate_id"],
            "parameters": parameters,
            "regime_profiles": candidate.get("regime_profiles") or {},
            "validation_role": candidate.get("validation_role"),
            "validation_roles": candidate.get("validation_roles") or [],
            "model_family": candidate.get("model_family"),
            "logic_version": candidate.get("logic_version"),
            "cost_model_version": candidate.get("cost_model_version"),
            "parent_candidate_id": candidate.get("parent_candidate_id"),
            "last_invocation_id": candidate.get("last_invocation_id"),
            "validation_version": VALIDATION_VERSION,
            "validation_cost_model_version": VALIDATION_COST_MODEL_VERSION,
            "validation_context_sha256": args.validation_context_sha256,
            "data_version": args.data_version,
            "train_start": window["train_start"],
            "train_end": window["train_end"],
            "test_start": window["test_start"],
            "test_end": window["test_end"],
            "fitness": round(float(fitness), 8) if math.isfinite(float(fitness)) else None,
            "metrics": metrics,
            "status": "completed",
        })
        del report
    del bars, spy, preaggregated_daily_bars
    return index, rows


def _summarize_candidate(candidate: dict, folds: list[dict]) -> dict:
    ordered = sorted(folds, key=lambda row: str(row.get("test_start") or ""))
    valid = [
        row for row in ordered
        if row.get("status") == "completed"
        and isinstance(row.get("fitness"), (int, float))
        and isinstance(row.get("metrics"), dict)
    ]
    scores = sorted(float(row["fitness"]) for row in valid)
    lower_quartile = scores[int((len(scores) - 1) * 0.25)] if scores else None
    median_fitness = statistics.median(scores) if scores else None
    trade_count = sum(int((row.get("metrics") or {}).get("closed_trade_count") or 0) for row in valid)
    positive_folds = sum(float(row["fitness"]) > 0 for row in valid)
    positive_fraction = positive_folds / len(valid) if valid else 0.0
    returns = [
        float((row.get("metrics") or {}).get("net_return_pct"))
        for row in valid
        if isinstance((row.get("metrics") or {}).get("net_return_pct"), (int, float))
    ]
    median_return = statistics.median(returns) if returns else None
    drawdowns = [
        float((row.get("metrics") or {}).get("max_drawdown_pct"))
        for row in valid
        if isinstance((row.get("metrics") or {}).get("max_drawdown_pct"), (int, float))
    ]
    worst_drawdown = max(drawdowns) if drawdowns else None
    enough_evidence = (
        len(valid) >= MINIMUM_VALIDATION_FOLDS
        and trade_count >= MINIMUM_VALIDATION_TRADES
    )
    passed = bool(
        enough_evidence
        and positive_fraction >= MINIMUM_POSITIVE_FOLD_FRACTION
        and median_fitness is not None and median_fitness > 0
        and median_return is not None and median_return > 0
    )
    status = "passed" if passed else "failed" if enough_evidence else "insufficient_sample"
    return {
        "candidate_id": candidate["candidate_id"],
        "parameters": candidate["parameters"],
        "regime_profiles": candidate.get("regime_profiles") or {},
        "validation_role": candidate.get("validation_role"),
        "validation_roles": candidate.get("validation_roles") or [],
        "queue_status": candidate.get("queue_status"),
        "model_family": candidate.get("model_family"),
        "logic_version": candidate.get("logic_version"),
        "cost_model_version": candidate.get("cost_model_version"),
        "parent_candidate_id": candidate.get("parent_candidate_id"),
        "source_invocation_id": candidate.get("last_invocation_id") or candidate.get("source_invocation_id"),
        "source_invocation_ids": candidate.get("source_invocation_ids") or [],
        "last_window": candidate.get("last_window") or {},
        "archive_artifact": candidate.get("archive_artifact"),
        "eligible": passed,
        "status": status,
        "fold_count": len(valid),
        "closed_trade_count": trade_count,
        "positive_fold_count": positive_folds,
        "positive_fold_fraction": round(positive_fraction, 4),
        "median_fitness": round(median_fitness, 8) if median_fitness is not None else None,
        "lower_quartile_fitness": round(lower_quartile, 8) if lower_quartile is not None else None,
        "median_net_return_pct": round(median_return, 6) if median_return is not None else None,
        "worst_fold_drawdown_pct": round(worst_drawdown, 6) if worst_drawdown is not None else None,
        "folds": [
            {
                "train_start": row.get("train_start"), "train_end": row.get("train_end"),
                "test_start": row.get("test_start"), "test_end": row.get("test_end"),
                "fitness": row.get("fitness"), "metrics": row.get("metrics"),
            }
            for row in valid
        ],
        "regime_metrics": candidate.get("regime_metrics") or {},
        "current_regime_metrics": candidate.get("current_regime_metrics") or {},
        "last_seen": candidate.get("last_seen"),
    }


def _current_regime_metrics(detail: dict, strategy: str) -> dict[str, dict]:
    rows = detail.get("candidates") or []
    baseline = next((row for row in rows if row.get("name") == "baseline"), {})
    baseline_regimes = baseline.get("regime_metrics") or {}
    profiles = {
        row.get("source_candidate_id"): row
        for row in (detail.get("adaptive_regime_search") or {}).get("profiles") or []
        if row.get("source_candidate_id")
    }
    result = {}
    for row in rows:
        parameters = row.get("parameters")
        if not isinstance(parameters, dict) or row.get("name") == "baseline":
            continue
        candidate_key = candidate_id(strategy, parameters)
        profile = profiles.get(candidate_key) or {}
        by_regime = {}
        for regime, metrics in (row.get("regime_metrics") or {}).items():
            baseline_metrics = baseline_regimes.get(regime) or {}
            baseline_sample_met = bool(baseline_metrics.get("minimum_sample_met"))
            by_regime[regime] = {
                **metrics,
                "baseline_fitness": baseline_metrics.get("fitness"),
                "improved_over_baseline": bool(
                    baseline_sample_met
                    and float(metrics.get("fitness") or 0.0)
                    > float(baseline_metrics.get("fitness") or 0.0) + 1e-9
                ),
                "historical_sample_met": bool(metrics.get("minimum_sample_met")),
                "historical_positive_after_costs": bool(metrics.get("positive_after_costs")),
                "planned_ablation": bool(profile.get("planned_ablation")),
                "parent_candidate_id": row.get("parent_candidate_id"),
            }
        result[candidate_key] = by_regime
    return result


def _prepared_args(
    *, strategy: str, database: str, symbols: list[str], start: date, end: date,
    data_version: str, output_dir, factor_weight_timeline: list[dict] | None,
) -> SimpleNamespace:
    args = SimpleNamespace(
        strategy=strategy, symbols=",".join(symbols), db=database,
        adjustment=ADJUSTMENT, search_method="bayes", candidates=1,
        search_space=None, fitness_weights=None, min_trades=0,
        capital=100_000.0, slippage_bps=5.0, spread_bps=3.0,
        commission_per_share=0.005, short_borrow_annual_pct=3.0,
        unknown_borrow_penalty_bps=25.0, borrow_availability=None,
        start=start.isoformat(), end=end.isoformat(), output_dir=str(output_dir),
        data_version=data_version,
    )
    _prepare_args(args)
    args.factor_weight_timeline = list(factor_weight_timeline or [])
    args.validation_context_sha256 = stable_hash({
        "validation_version": VALIDATION_VERSION,
        "validation_cost_model_version": VALIDATION_COST_MODEL_VERSION,
        "data_version": data_version,
        "symbols": args.symbols_resolved,
        "adjustment": args.adjustment,
        "borrow_availability": args.borrow_availability_data,
        "regime_macro_series": args.regime_macro_series,
        "factor_weight_timeline": args.factor_weight_timeline,
    })
    return args


def run_candidate_validation(
    *, database: str, symbols: list[str], start: date, end: date,
    data_version: str, output_dir, verification: dict, discovery: dict,
    workers: int, force: bool = False,
    factor_weight_timeline: list[dict] | None = None,
    on_strategy_complete=None,
) -> dict:
    """Apply identical rolling gates to live, champion, and queued challenger settings."""
    refresh_active_parameter_profile()
    archive_root = output_dir / "archive"
    verification_strategies = verification.get("strategies") or {}
    discovery_strategies = discovery.get("strategies") or {}
    results = {}
    failures = []
    failed_strategies = set()
    cohorts_by_strategy: dict[str, list[dict]] = {}
    context_by_strategy: dict[str, str] = {}
    current_regime_metrics_by_strategy: dict[str, dict[str, dict]] = {}
    computed_by_strategy: dict[str, list[dict]] = {}
    completed_fold_jobs_by_strategy: dict[str, int] = {}
    jobs = []

    for strategy, verification_status in verification_strategies.items():
        if verification_status.get("status") not in {"completed", "reused"}:
            results[strategy] = {
                "status": "deferred", "reason": "rolling verification is unavailable",
                "candidates": [],
            }
            continue
        detail_path = verification_status.get("detail_artifact") or (
            output_dir / strategy / "latest-walk-forward.json"
        )
        detail, _ = read_json(detail_path)
        if not detail or not detail.get("folds"):
            results[strategy] = {
                "status": "deferred", "reason": "rolling fold detail is missing",
                "candidates": [],
            }
            continue
        discovery_detail_path = (discovery_strategies.get(strategy) or {}).get("detail_artifact")
        discovery_detail, _ = read_json(discovery_detail_path) if discovery_detail_path else (None, None)
        current_regime_metrics_by_strategy[strategy] = _current_regime_metrics(
            discovery_detail or {}, strategy,
        )
        cohort_by_id = {}

        def add_to_cohort(record: dict, role: str) -> None:
            parameters = record.get("parameters")
            if not isinstance(parameters, dict):
                return
            regime_profiles = record.get("regime_profiles") or {}
            identifier = record.get("candidate_id") or candidate_id(
                strategy, parameters, regime_profiles=regime_profiles,
            )
            archived = archived_candidate(archive_root, strategy, identifier) or {}
            candidate = {
                **archived,
                **record,
                "candidate_id": identifier,
                "strategy": strategy,
                "parameters": parameters,
                "regime_profiles": regime_profiles,
                "model_family": record.get("model_family") or archived.get("model_family"),
                "logic_version": record.get("logic_version") or archived.get("logic_version"),
                "candidate_validation_folds": archived.get("candidate_validation_folds") or [],
                "validation_roles": list(dict.fromkeys([
                    *(cohort_by_id.get(identifier, {}).get("validation_roles") or []),
                    *(record.get("validation_roles") or []),
                ])),
                "archive_artifact": archive_reference(archive_root),
            }
            if role not in candidate["validation_roles"]:
                candidate["validation_roles"].append(role)
            candidate["validation_role"] = role
            if role == "challenger":
                candidate["queue_status"] = record.get("status", "queued")
            cohort_by_id[identifier] = candidate

        live_parameters = deepcopy(ALGO_DEFAULT_PARAMETERS[strategy])
        live_regimes = deepcopy(REGIME_PARAMETER_PROFILES.get(strategy) or {})
        add_to_cohort({
            "candidate_id": candidate_id(
                strategy, live_parameters, regime_profiles=live_regimes,
            ),
            "parameters": live_parameters,
            "regime_profiles": live_regimes,
            "model_family": model_family(strategy, live_parameters),
            "logic_version": logic_version(strategy),
            "source": "live_setting",
        }, "live_setting")

        champion = champion_candidate(archive_root, strategy)
        if champion:
            add_to_cohort(champion, "champion")

        queued = pending_validation_candidates(
            archive_root, strategy,
            limit=MAX_QUEUED_CHALLENGERS_PER_STRATEGY,
            data_version=data_version,
        )
        for candidate in queued:
            add_to_cohort(candidate, "challenger")
        cohort = list(cohort_by_id.values())
        if not cohort:
            results[strategy] = {"status": "completed", "candidates": []}
            continue
        cohorts_by_strategy[strategy] = cohort
        args = _prepared_args(
            strategy=strategy, database=database, symbols=symbols,
            start=start, end=end, data_version=data_version,
            output_dir=output_dir, factor_weight_timeline=factor_weight_timeline,
        )
        for candidate in cohort:
            if not candidate.get("cost_model_version"):
                candidate["cost_model_version"] = _cost_model_version(args)
        context_by_strategy[strategy] = args.validation_context_sha256
        missing_by_fold = []
        for fold_index, window in enumerate(detail.get("folds") or []):
            missing = []
            for candidate in cohort:
                cached = candidate.get("candidate_validation_folds") or []
                found = any(
                    item.get("status") == "completed"
                    and item.get("validation_version") == VALIDATION_VERSION
                    and item.get("validation_cost_model_version") == VALIDATION_COST_MODEL_VERSION
                    and item.get("validation_context_sha256") == args.validation_context_sha256
                    and item.get("logic_version") == candidate.get("logic_version")
                    and item.get("data_version") == data_version
                    and item.get("train_start") == window.get("train_start")
                    and item.get("train_end") == window.get("train_end")
                    and item.get("test_start") == window.get("test_start")
                    and item.get("test_end") == window.get("test_end")
                    for item in cached
                )
                if force or not found:
                    missing.append(candidate)
            if missing:
                missing_by_fold.append((fold_index, window, missing))
        results[strategy] = {
            "status": "running", "candidate_count": len(cohort),
            "candidate_roles": {
                candidate["candidate_id"]: candidate.get("validation_roles") or []
                for candidate in cohort
            },
            "queued_challengers_selected": sum(
                "challenger" in (candidate.get("validation_roles") or [])
                for candidate in cohort
            ),
            "reused_fold_count": sum(
                len(cohort) for _ in detail.get("folds", [])
            ) - sum(len(missing) for _, _, missing in missing_by_fold),
            "candidates": [],
        }
        for fold_index, window, missing in missing_by_fold:
            jobs.append((strategy, fold_index, window, missing, args))

    if jobs:
        budget = min(max(1, workers), available_cpu_count(), len(jobs))

        def persist_fold_job(strategy: str, window: dict, records: list[dict]) -> None:
            if records:
                record_candidate_validation_folds(archive_root, strategy, records)
                computed_by_strategy.setdefault(strategy, []).extend(records)
            completed_fold_jobs_by_strategy[strategy] = completed_fold_jobs_by_strategy.get(strategy, 0) + 1
            if on_strategy_complete:
                on_strategy_complete("candidate_validation", strategy, {
                    "status": "running",
                    "completed_fold_jobs": completed_fold_jobs_by_strategy[strategy],
                    "last_test_start": window.get("test_start"),
                    "last_test_end": window.get("test_end"),
                    "candidate_count": len(records),
                })

        if budget == 1:
            for strategy, fold_index, window, missing, args in jobs:
                try:
                    _, records = _evaluate_fold((fold_index, window, missing, args))
                    persist_fold_job(strategy, window, records)
                except Exception as exc:
                    failed_strategies.add(strategy)
                    failures.append(f"{strategy} fold {window.get('test_start')}: {type(exc).__name__}: {exc}")
        else:
            with ProcessPoolExecutor(
                max_workers=budget, mp_context=mp.get_context("spawn"),
            ) as executor:
                futures = {
                    executor.submit(_evaluate_fold, (fold_index, window, missing, args)):
                    (strategy, window)
                    for strategy, fold_index, window, missing, args in jobs
                }
                for future in as_completed(futures):
                    strategy, window = futures[future]
                    try:
                        _, records = future.result()
                        persist_fold_job(strategy, window, records)
                    except Exception as exc:
                        failed_strategies.add(strategy)
                        failures.append(
                            f"{strategy} fold {window.get('test_start')}: {type(exc).__name__}: {exc}"
                        )

    for strategy, result in results.items():
        if result.get("status") == "deferred":
            if on_strategy_complete:
                on_strategy_complete("candidate_validation", strategy, result)
            continue
        cohort = cohorts_by_strategy.get(strategy, [])
        newly_computed = computed_by_strategy.get(strategy, [])
        computed_keys = {
            (
                row.get("candidate_id"), row.get("validation_version"),
                row.get("data_version"), row.get("validation_cost_model_version"),
                row.get("validation_context_sha256"), row.get("logic_version"),
                row.get("test_start"), row.get("test_end"),
            )
            for row in newly_computed
        }
        candidates = []
        for candidate in cohort:
            folds = [
                item for item in candidate.get("candidate_validation_folds") or []
                if item.get("validation_version") == VALIDATION_VERSION
                and item.get("validation_cost_model_version") == VALIDATION_COST_MODEL_VERSION
                and item.get("validation_context_sha256") == context_by_strategy.get(strategy)
                and item.get("logic_version") == candidate.get("logic_version")
                and item.get("data_version") == data_version
                and (
                    candidate["candidate_id"], item.get("validation_version"),
                    item.get("data_version"), item.get("validation_cost_model_version"),
                    item.get("validation_context_sha256"), item.get("logic_version"),
                    item.get("test_start"), item.get("test_end"),
                ) not in computed_keys
            ]
            folds.extend(
                row for row in newly_computed
                if row.get("candidate_id") == candidate["candidate_id"]
                and row.get("validation_version") == VALIDATION_VERSION
                and row.get("validation_cost_model_version") == VALIDATION_COST_MODEL_VERSION
                and row.get("validation_context_sha256") == context_by_strategy.get(strategy)
                and row.get("logic_version") == candidate.get("logic_version")
                and row.get("data_version") == data_version
            )
            current_metrics = current_regime_metrics_by_strategy.get(strategy, {}).get(
                candidate["candidate_id"],
            ) or {}
            summary = _summarize_candidate(candidate, folds)
            summary["validation_version"] = VALIDATION_VERSION
            summary["validation_data_version"] = data_version
            summary["validation_end"] = end.isoformat()
            summary["current_regime_metrics"] = {
                **(candidate.get("regime_metrics") or {}),
                **current_metrics,
            }
            if strategy in failed_strategies:
                summary["eligible"] = False
                summary["status"] = "validation_error"
            candidates.append(summary)
        result["candidates"] = candidates
        result["status"] = "partial_error" if strategy in failed_strategies else "completed"
        result["candidate_count"] = len(candidates)
        result["minimum_sample"] = {
            "folds": MINIMUM_VALIDATION_FOLDS,
            "closed_trades": MINIMUM_VALIDATION_TRADES,
            "positive_fold_fraction": MINIMUM_POSITIVE_FOLD_FRACTION,
        }
        result["champion_updates"] = []
        for candidate in candidates:
            roles = candidate.get("validation_roles") or []
            if "challenger" in roles:
                record_validation_queue_result(
                    archive_root, strategy, candidate["candidate_id"],
                    status=candidate.get("status", "validation_error"),
                    summary={
                        key: candidate.get(key) for key in (
                            "status", "fold_count", "closed_trade_count",
                            "positive_fold_fraction", "median_fitness",
                            "lower_quartile_fitness", "median_net_return_pct",
                            "worst_fold_drawdown_pct",
                        )
                    },
                    data_version=data_version, validation_end=end.isoformat(),
                )
            if candidate.get("status") == "passed":
                result["champion_updates"].append(update_champion_if_better(
                    archive_root, strategy, candidate, candidate,
                ))
            if "champion" in roles:
                record_champion_validation_result(
                    archive_root, strategy, candidate, candidate,
                    data_version=data_version, validation_end=end.isoformat(),
                )
        result["validation_queue"] = {
            "challengers_selected": result.get("queued_challengers_selected", 0),
            "challengers_remaining": len(pending_validation_candidates(
                archive_root, strategy, limit=10_000,
            )),
        }
        if on_strategy_complete:
            on_strategy_complete("candidate_validation", strategy, result)

    return {
        "status": "partial_error" if failures else "completed",
        "version": VALIDATION_VERSION,
        "data_version": data_version,
        "strategies": results,
        "errors": failures,
    }
