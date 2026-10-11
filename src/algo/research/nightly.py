"""Scheduled orchestration of strategy discovery, validation, and portfolio replay."""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter
from types import SimpleNamespace

import requests

from src.config import get_algo_runtime_settings
from src.algo.cpu import available_cpu_count
from src.algo.market_store import DEFAULT_DB, SQLiteBarStore
from src.market_data import data_tier, regular_feed

from .artifact_cache import (
    read_json, research_data_version, stable_hash,
)
from .data import (
    _latest_dates, _load_daily_history, _refresh_incremental,
    load_algo_universe,
)
from .files import _atomic_json, _research_lock
from .live_manifest import live_configuration_manifest
from .settings import (
    ADAPTIVE_DISCOVERY_CANDIDATES, ADAPTIVE_VERIFICATION_CANDIDATES,
    ADAPTIVE_VERIFICATION_HOLDOUT_DAYS, ADAPTIVE_VERIFICATION_STEP_DAYS,
    ADAPTIVE_VERIFICATION_TEST_DAYS, ADAPTIVE_WINDOW_DAYS,
    ADJUSTMENT, DEFAULT_OUTPUT_DIR, ET, FEED, LIVE_RESEARCH_STRATEGIES,
    MAX_ADAPTIVE_FOLD_WORKERS_PER_STRATEGY, MIN_COVERED_UNIVERSE,
    RESEARCHABLE_STRATEGIES, STRATEGY_FLAVORS,
)


NIGHTLY_PIPELINE_VERSION = "algo-research-layers-v7-adaptive-only"
# Keep the adaptive policy version independent from nightly orchestration so
# compatible dynamic-search artifacts survive cache and report changes.
ADAPTIVE_PIPELINE_VERSION = "algo-research-layers-v5-adaptive-all-strategies"
_ADAPTIVE_COST_MODEL = {
    "slippage_bps": 5.0,
    "spread_bps": 3.0,
    "commission_per_share": 0.005,
    "short_borrow_annual_pct": 3.0,
    "unknown_borrow_penalty_bps": 25.0,
}


def run_daily_research(args) -> dict:
    """Run one versioned cohort, reusing compatible artifacts when possible."""
    runtime_settings = get_algo_runtime_settings()
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
    data_version = research_data_version(
        database, start=history_start, end=cohort_end,
        symbols=covered_symbols, feed=FEED, adjustment=ADJUSTMENT,
    )
    live_snapshot = live_configuration_manifest([*RESEARCHABLE_STRATEGIES, "multi_factor", "event_driven"])
    context = {
        "pipeline_version": NIGHTLY_PIPELINE_VERSION,
        "database": database,
        "feed": FEED,
        "adjustment": ADJUSTMENT,
        "symbols": covered_symbols,
        "history_start": history_start.isoformat(),
        "data_end": cohort_end.isoformat(),
        "data_version": data_version,
        "adaptive_pipeline": {
            "window_days_by_flavor": {
                flavor: list(values) for flavor, values in ADAPTIVE_WINDOW_DAYS.items()
            },
            "discovery_method": "bayes",
            "discovery_candidates": ADAPTIVE_DISCOVERY_CANDIDATES,
            "verification_start": history_start.isoformat(),
            "verification_end": cohort_end.isoformat(),
            "verification_candidates": ADAPTIVE_VERIFICATION_CANDIDATES,
            "verification_test_days": ADAPTIVE_VERIFICATION_TEST_DAYS,
            "verification_step_days": ADAPTIVE_VERIFICATION_STEP_DAYS,
            "verification_holdout_days": ADAPTIVE_VERIFICATION_HOLDOUT_DAYS,
        },
        "strategies": list(RESEARCHABLE_STRATEGIES),
        "live_configuration_sha256": live_snapshot["sha256"],
        "portfolio_strategies": list(LIVE_RESEARCH_STRATEGIES),
        "multi_factor_enabled": runtime_settings.algo_multi_factor_enabled,
        "event_driven_enabled": runtime_settings.algo_event_driven_enabled,
        "factor_only": bool(args.factor_only),
    }
    fingerprint = stable_hash(context)
    if args.force:
        fingerprint = stable_hash({"context": context, "forced_at": datetime.now(timezone.utc).isoformat()})
    run_id = f"week-{cohort_end:%Y%m%d}-{fingerprint[:16]}"
    context["fingerprint"] = fingerprint
    context["run_id"] = run_id

    root = Path(args.output_dir) / "layers"
    cohort_dir = root / "runs" / run_id
    report_path = cohort_dir / "report.json"
    adaptive_root = Path(args.output_dir) / "adaptive"
    adaptive_discovery_started = perf_counter()
    adaptive_discovery = (
        _run_nightly_adaptive_discoveries(
            database=database, symbols=covered_symbols, latest_end=latest_end, data_version=data_version,
            output_dir=adaptive_root, workers=args.workers, force=args.force,
        )
        if not args.factor_only else {"status": "skipped", "reason": "factor-only run", "strategies": {}}
    )
    adaptive_discovery_seconds = round(perf_counter() - adaptive_discovery_started, 3)
    adaptive_verification_started = perf_counter()
    adaptive_verification = (
        _run_nightly_adaptive_verifications(
            database=database, symbols=covered_symbols,
            start=history_start, end=cohort_end,
            data_version=data_version,
            output_dir=adaptive_root, workers=args.workers, force=args.force,
        )
        if not args.factor_only else {"status": "skipped", "reason": "factor-only run", "strategies": {}}
    )
    adaptive_verification_seconds = round(perf_counter() - adaptive_verification_started, 3)
    adaptive_shadow_discovery = (adaptive_discovery.get("strategies") or {}).get("multi_factor", {})
    adaptive_coordinator_root = adaptive_root / "verification-coordinator"
    verification_statuses = adaptive_verification.get("strategies") or {}
    adaptive_portfolio_ready = all(
        (verification_statuses.get(strategy) or {}).get("coordinator_artifact")
        and Path((verification_statuses.get(strategy) or {})["coordinator_artifact"]).is_file()
        for strategy in LIVE_RESEARCH_STRATEGIES
    )
    portfolio_source = "adaptive_short_window"
    if adaptive_portfolio_ready and not args.force and not args.factor_only:
        cached, _ = read_json(report_path)
        if (
            cached
            and cached.get("orchestration", {}).get("fingerprint") == fingerprint
            and (cached.get("portfolio_coordinator_replay") or {}).get("profile_selection_source") == portfolio_source
        ):
            cached = {
                **cached,
                "stage_timings": {
                    **(cached.get("stage_timings") or {}),
                    "adaptive_discovery": {
                        "seconds": adaptive_discovery_seconds,
                        "status": adaptive_discovery.get("status"),
                    },
                    "adaptive_verification": {
                        "seconds": adaptive_verification_seconds,
                        "status": adaptive_verification.get("status"),
                    },
                    "total_seconds": round(perf_counter() - total_started, 3),
                },
                "adaptive_research": {
                    "discovery": adaptive_discovery,
                    "verification": adaptive_verification,
                },
                "adaptive_strategy_artifacts": {
                    strategy: {
                        "discovery": (adaptive_discovery.get("strategies") or {}).get(strategy, {}).get("detail_artifact"),
                        "verification": (adaptive_verification.get("strategies") or {}).get(strategy, {}).get("detail_artifact"),
                        "coordinator_artifact": (adaptive_verification.get("strategies") or {}).get(strategy, {}).get("coordinator_artifact"),
                        "verification_summary": (adaptive_verification.get("strategies") or {}).get(strategy, {}).get("summary"),
                    }
                    for strategy in RESEARCHABLE_STRATEGIES
                },
                "adaptive_shadow_discovery": adaptive_shadow_discovery,
            }
            _atomic_json(report_path, cached)
            _atomic_json(root / "latest.json", cached)
            return {**cached, "cache_hit": True}

    cohort_dir.mkdir(parents=True, exist_ok=True)
    timings: dict[str, object] = {
        "data_refresh_seconds": refresh_seconds,
        "adaptive_discovery": {
            "seconds": adaptive_discovery_seconds, "status": adaptive_discovery.get("status"),
        },
        "adaptive_verification": {
            "seconds": adaptive_verification_seconds, "status": adaptive_verification.get("status"),
        },
    }
    factor_started = perf_counter()
    factor_path = cohort_dir / "factor_model.json"
    factor_cached, _ = read_json(factor_path)
    if not args.force and factor_cached and factor_cached.get("fingerprint") == fingerprint:
        factor_report = factor_cached["report"]
        timings["factor_model"] = {"seconds": 0.0, "reused": True}
    else:
        history = _load_daily_history(
            store, covered_symbols, start=history_start.isoformat(), end=cohort_end.isoformat(),
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
                history_start, latest_end, cohort_end, latest_age_days,
                covered_symbols, symbols, missing, universe_meta, refresh_report,
            ),
            "adaptive_research": {
                "discovery": adaptive_discovery,
                "verification": adaptive_verification,
            },
            "adaptive_shadow_discovery": adaptive_shadow_discovery,
            "orchestration": {**context, "cache_hit": False},
            "stage_timings": {**timings, "total_seconds": round(perf_counter() - total_started, 3)},
            "limitations": [
                "Factor fitting is research-only and does not update live weights.",
                "No adaptive strategy discovery, verification, or portfolio replay was requested.",
            ],
        }
        _atomic_json(report_path, report)
        return report

    portfolio_path = cohort_dir / "portfolio"
    portfolio_report = None
    if adaptive_portfolio_ready:
        portfolio_started = perf_counter()
        cached_portfolio, _ = read_json(portfolio_path / "latest.json")
        if (
            not args.force and cached_portfolio
            and cached_portfolio.get("orchestration", {}).get("fingerprint") == fingerprint
            and cached_portfolio.get("profile_selection_source") == portfolio_source
        ):
            portfolio_report = cached_portfolio
            portfolio_reused = True
        else:
            from .portfolio_simulation import run_coordinated_portfolio

            portfolio_args = SimpleNamespace(
                strategies=",".join(LIVE_RESEARCH_STRATEGIES),
                enable_disabled_intraday=False,
                artifact_dir=str(adaptive_coordinator_root),
                event_calendar=None,
                multi_factor_overlay=runtime_settings.algo_multi_factor_enabled,
                workers=max(1, args.workers), output_dir=str(portfolio_path),
            )
            portfolio_report = run_coordinated_portfolio(portfolio_args)
            portfolio_report["orchestration"] = {
                "run_id": run_id, "fingerprint": fingerprint,
            }
            portfolio_report["profile_selection_source"] = portfolio_source
            _atomic_json(portfolio_path / "latest.json", portfolio_report)
            _atomic_json(
                portfolio_path / f"{cohort_end.isoformat()}-portfolio.json",
                portfolio_report,
            )
            portfolio_reused = False
        timings["portfolio_replay"] = {
            "seconds": 0.0 if portfolio_reused else round(perf_counter() - portfolio_started, 3),
            "reused": portfolio_reused,
        }
    else:
        timings["portfolio_replay"] = {
            "seconds": 0.0, "status": "skipped",
            "reason": "Adaptive verification is incomplete; portfolio replay was skipped.",
        }

    factor_ic = (factor_report.get("adaptive_prequential_ic") or {}).get("mean_spearman_ic")
    report = {
        "schema": "swarm_algo_nightly_research_v2",
        "status": "completed" if adaptive_portfolio_ready else "partial",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "research_only": True, "live_configuration_changed": False,
        "trade_or_order_actions": 0,
        "data": _data_summary(
            history_start, latest_end, cohort_end, latest_age_days,
            covered_symbols, symbols, missing, universe_meta, refresh_report,
        ),
        "factor_model": factor_report,
        "adaptive_research": {
            "discovery": adaptive_discovery,
            "verification": adaptive_verification,
        },
        "adaptive_strategy_artifacts": {
            strategy: {
                "discovery": (adaptive_discovery.get("strategies") or {}).get(strategy, {}).get("detail_artifact"),
                "verification": (adaptive_verification.get("strategies") or {}).get(strategy, {}).get("detail_artifact"),
                "coordinator_artifact": (adaptive_verification.get("strategies") or {}).get(strategy, {}).get("coordinator_artifact"),
                "verification_summary": (adaptive_verification.get("strategies") or {}).get(strategy, {}).get("summary"),
            }
            for strategy in RESEARCHABLE_STRATEGIES
        },
        "adaptive_shadow_discovery": adaptive_shadow_discovery,
        "portfolio_coordinator_replay": portfolio_report,
        "portfolio_summary": (portfolio_report or {}).get("fold_summary"),
        "recommendation": {
            "adaptive_research": (
                "Dynamic discovery and rolling out-of-sample verification cover the active strategy set."
                if adaptive_portfolio_ready
                else "Adaptive verification is incomplete; portfolio replay was skipped."
            ),
            "multi_factor": (
                "The recent-regime Multi Factor candidate is routed to its shadow book only; broker execution remains disabled."
                if factor_ic is not None and factor_ic > 0
                else "Factor model did not show positive adaptive prequential IC; retain default parameters in the shadow book until positive discovery evidence is available."
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
            *([] if adaptive_portfolio_ready else [
                "Adaptive verification did not produce coordinator artifacts for every active strategy; the portfolio replay was skipped.",
            ]),
        ],
    }
    _atomic_json(report_path, report)
    _atomic_json(root / "latest.json", report)
    return report


def _adaptive_window_options(strategy: str) -> list[int]:
    return list(ADAPTIVE_WINDOW_DAYS[STRATEGY_FLAVORS[strategy]])


def _run_nightly_adaptive_discoveries(
    *, database: str, symbols: list[str], latest_end: date, data_version: str, output_dir: Path,
    workers: int, force: bool,
) -> dict:
    """Run recent-window discovery for every strategy, reusing today's artifacts."""
    statuses = {}
    jobs = []
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
            and window_search.get("data_version") == data_version
            and prior.get("data_tier") == "algo_trader_plus"
            and prior.get("feed") == FEED
            and prior.get("adjustment") == ADJUSTMENT
            and set(prior.get("symbols") or []) == set(symbols)
            and prior.get("database") == str(Path(database).resolve())
        )
        if reusable:
            statuses[strategy] = _discovery_status(prior, latest_path, "reused")
        else:
            jobs.append((strategy, database, symbols, latest_end, data_version, output_dir, options))

    if jobs:
        worker_count = min(max(1, workers), len(jobs), available_cpu_count())
        with ProcessPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(_run_nightly_adaptive_discovery_job, job): job[0]
                for job in jobs
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
    errors = [strategy for strategy, item in statuses.items() if item.get("status") == "error"]
    return {
        "status": "error" if len(errors) == len(RESEARCHABLE_STRATEGIES) else "completed" if not errors else "partial_error",
        "window_days_by_strategy": {
            strategy: _adaptive_window_options(strategy) for strategy in RESEARCHABLE_STRATEGIES
        },
        "strategies": {strategy: statuses[strategy] for strategy in RESEARCHABLE_STRATEGIES if strategy in statuses},
        "error_strategies": errors,
    }


def _run_nightly_adaptive_discovery_job(payload: tuple) -> dict:
    strategy, database, symbols, latest_end, data_version, output_dir, options = payload
    from .adaptive import run_window_ladder_discovery

    discovery_args = SimpleNamespace(
        strategy=strategy, symbols=",".join(symbols), db=database,
        adjustment=ADJUSTMENT, search_method="bayes",
        candidates=ADAPTIVE_DISCOVERY_CANDIDATES,
        seed=latest_end.toordinal() + sum(map(ord, strategy)),
        search_space=None, fitness_weights=None, min_trades=3, capital=100_000.0,
        slippage_bps=5.0, spread_bps=3.0, commission_per_share=0.005,
        short_borrow_annual_pct=3.0, unknown_borrow_penalty_bps=25.0,
        borrow_availability=None, start=None, end=latest_end.isoformat(),
        window_days=options[len(options) // 2], output_dir=str(output_dir),
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
    return {
        "status": status, "strategy": detail.get("strategy"),
        "selection_status": detail.get("selection_status"),
        "adaptive_improvement": bool(detail.get("adaptive_improvement")),
        "window": detail.get("window"),
        "window_search": detail.get("adaptive_window_search"),
        "selected_candidate": {
            "name": selected.get("name"), "parameters": selected.get("parameters"),
            "metrics": selected.get("metrics"), "eligible": selected.get("eligible"),
        },
        "detail_artifact": str(path.resolve()),
    }


def _run_nightly_adaptive_verifications(
    *, database: str, symbols: list[str], start: date, end: date,
    data_version: str, output_dir: Path, workers: int, force: bool,
) -> dict:
    """Verify the short-window fitting policy on rolling historical holdouts."""
    statuses = {}
    jobs = []
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
        else:
            jobs.append((strategy, database, symbols, start, end, data_version, output_dir, options))

    if jobs:
        budget = min(max(1, workers), available_cpu_count())
        fold_workers = min(MAX_ADAPTIVE_FOLD_WORKERS_PER_STRATEGY, budget)
        concurrency = min(len(jobs), max(1, (budget + fold_workers - 1) // fold_workers))
        for offset in range(0, len(jobs), concurrency):
            batch = jobs[offset:offset + concurrency]
            per_strategy_workers = min(fold_workers, max(1, budget // len(batch)))
            with ProcessPoolExecutor(max_workers=len(batch)) as executor:
                futures = {
                    executor.submit(_run_nightly_adaptive_verification_job, (*job, per_strategy_workers)): job[0]
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
    for strategy in RESEARCHABLE_STRATEGIES:
        status = statuses.get(strategy) or {}
        if status.get("status") == "error":
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
        except Exception as exc:
            statuses[strategy] = {
                **status, "status": "error", "strategy": strategy,
                "error": f"{type(exc).__name__}: {exc}",
            }
    errors = [strategy for strategy, item in statuses.items() if item.get("status") == "error"]
    return {
        "status": "error" if len(errors) == len(RESEARCHABLE_STRATEGIES) else "completed" if not errors else "partial_error",
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
        and detail.get("adaptive_policy_version") == ADAPTIVE_PIPELINE_VERSION
        and data.get("start") == start.isoformat()
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
        and config.get("locked_holdout_days") == ADAPTIVE_VERIFICATION_HOLDOUT_DAYS
        and search.get("method") == "bayes"
        and search.get("candidates_per_fold") == ADAPTIVE_VERIFICATION_CANDIDATES
    )


def _run_nightly_adaptive_verification_job(payload: tuple) -> dict:
    strategy, database, symbols, start, end, data_version, output_dir, options, fold_workers = payload
    from .adaptive import run_adaptive_walk_forward

    walk_args = SimpleNamespace(
        strategy=strategy, symbols=",".join(symbols), db=database,
        adjustment=ADJUSTMENT, search_method="bayes",
        candidates=ADAPTIVE_VERIFICATION_CANDIDATES,
        seed=end.toordinal() + sum(map(ord, strategy)),
        search_space=None, fitness_weights=None, min_trades=3, capital=100_000.0,
        slippage_bps=5.0, spread_bps=3.0, commission_per_share=0.005,
        short_borrow_annual_pct=3.0, unknown_borrow_penalty_bps=25.0,
        borrow_availability=None, start=start.isoformat(), end=end.isoformat(),
        train_days=max(max(values) for values in ADAPTIVE_WINDOW_DAYS.values()),
        train_window_options=options,
        test_days=ADAPTIVE_VERIFICATION_TEST_DAYS,
        step_days=ADAPTIVE_VERIFICATION_STEP_DAYS,
        holdout_days=ADAPTIVE_VERIFICATION_HOLDOUT_DAYS,
        workers=fold_workers, minimum_folds=3, minimum_oos_sharpe=0.0,
        maximum_average_decay=0.35, output_dir=str(output_dir),
        adaptive_policy_version=ADAPTIVE_PIPELINE_VERSION,
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


def _last_completed_friday(latest_session: date) -> date:
    days_since_friday = (latest_session.weekday() - 4) % 7
    return latest_session - timedelta(days=days_since_friday)


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
        description="Run adaptive Algo discovery, rolling verification, and portfolio replay",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument(
        "--workers", type=int, default=min(8, available_cpu_count()),
        help="CPU budget shared by adaptive discovery and verification workers",
    )
    parser.add_argument("--no-refresh", action="store_true", help="use existing bars without fetching recent SIP history")
    parser.add_argument("--factor-only", action="store_true", help="fit the factor model without adaptive or portfolio stages")
    parser.add_argument("--force", action="store_true", help="create a fresh cohort and recompute dynamic research stages")
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
        "strategies": len(report.get("adaptive_strategy_artifacts", {})),
        "portfolio": report.get("portfolio_summary"),
        "cache_hit": report.get("orchestration", {}).get("cache_hit", False),
        "live_configuration_changed": False,
    }, sort_keys=True, default=str))
    return 0
