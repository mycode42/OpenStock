"""Scheduled adaptive Algo research orchestration and CLI."""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
from time import perf_counter
from types import SimpleNamespace
from uuid import uuid4

from portfolio.config import ALGO_FACTOR_SECTOR_BY_SYMBOL, get_algo_runtime_settings
from autoresearch.cpu import available_cpu_count
from portfolio.config import ALGO_REGIME_WARMUP_CALENDAR_DAYS
from market.store import DEFAULT_DB, SQLiteBarStore, fetch_alpaca_bars
from strategies.regime_features import NON_EQUITY_CONTEXT_SYMBOLS
from strategies.registry import refresh_active_parameter_profile
from market.data import data_tier, regular_feed

from .pipeline.adaptive_pipeline import (
    ADAPTIVE_PIPELINE_VERSION, ADAPTIVE_VERIFICATION_VERSION,
    run_adaptive_discoveries, run_adaptive_verifications,
)
from .artifacts.artifact_cache import read_json, research_data_version, stable_hash
from .data import (
    _latest_dates, _load_daily_history,
    _refresh_incremental as _refresh_incremental_impl,
    load_algo_universe,
)
from .artifacts.files import _atomic_json, _json_safe, _research_lock
from .artifacts.fitness_timeline import _attach_fitness_timeline
from .artifacts.live_manifest import live_configuration_manifest
from .artifacts.research_ledger import ResearchLedger
from .settings import (
    ADAPTIVE_DISCOVERY_CANDIDATES, ADAPTIVE_DISCOVERY_ITERATIONS,
    ADAPTIVE_DISCOVERY_MAX_DOMAIN_REFINEMENTS,
    ADAPTIVE_DISCOVERY_MIN_NOVELTY_FRACTION,
    ADAPTIVE_DISCOVERY_MIN_ROUNDS_BEFORE_STOP,
    ADAPTIVE_PURGE_SESSIONS, ADAPTIVE_VERIFICATION_CANDIDATES,
    ADAPTIVE_VERIFICATION_STEP_DAYS, ADAPTIVE_VERIFICATION_TEST_DAYS,
    ADAPTIVE_WINDOW_DAYS, MIN_COVERED_UNIVERSE, MULTI_FACTOR_MIN_UNIVERSE_VALUES,
    REGIME_CONTEXT_WARMUP_DAYS, RESEARCH_START, RESEARCH_WARMUP_DAYS,
    ADJUSTMENT, DEFAULT_OUTPUT_DIR, ET, FEED, RESEARCHABLE_STRATEGIES,
)

AUTO_RESEARCH_PIPELINE_VERSION = "algo-research-layers-v23-continuous-champion-queue-2026"
logger = logging.getLogger(__name__)


def _refresh_incremental(store, *, selected_symbols, end_date):
    """Refresh the configured universe through the AutoResearch data adapter."""
    return _refresh_incremental_impl(
        store, selected_symbols=selected_symbols, end_date=end_date,
        data_tier_fn=data_tier, regular_feed_fn=regular_feed,
        fetch_bars_fn=fetch_alpaca_bars,
    )


def run_daily_research(args) -> dict:
    """Run one full research cohort or archive-backed optimizer cycle."""
    output_dir = Path(args.output_dir)
    optimization_only = bool(getattr(args, "optimization_only", False))
    invocation_id = getattr(args, "invocation_id", None) or uuid4().hex
    ledger_path = Path(getattr(args, "ledger_path", output_dir / "research.sqlite3"))
    ledger = ResearchLedger(ledger_path)
    ledger.start_invocation(invocation_id, request={
        "workers": args.workers, "no_refresh": args.no_refresh,
        "factor_only": args.factor_only,
        "optimization_only": optimization_only,
        "force": args.force,
    })
    ledger.import_legacy(output_dir)
    args.invocation_id = invocation_id
    args.ledger_path = str(ledger_path)

    def stage_progress(stage: str, status: str, payload: dict | None = None, strategy: str | None = None) -> None:
        callback = getattr(args, "progress_callback", None)
        if callback:
            callback(stage=stage, status=status, payload=payload or {}, strategy=strategy)
        artifact = (payload or {}).get("detail_artifact") or (payload or {}).get("report_path")
        summary = None
        if payload:
            summary = {
                key: payload.get(key)
                for key in ("status", "error", "detail_artifact", "coordinator_artifact", "candidate_count", "fold_count")
                if key in payload
            }
        ledger.record_stage(
            invocation_id, stage=stage, strategy=strategy, status=status,
            reused=status == "reused", source_artifact=artifact,
            summary=summary, error=(payload or {}).get("error"),
        )

    def strategy_progress(stage: str, strategy: str, payload: dict) -> None:
        stage_progress(stage, str(payload.get("status") or "completed"), payload, strategy)

    refresh_active_parameter_profile()
    runtime_settings = get_algo_runtime_settings()
    total_started = perf_counter()
    if data_tier() != "algo_trader_plus" or regular_feed() != FEED:
        raise ValueError("Algo AutoResearch is restricted to Plus SIP data")

    symbols, universe_meta = load_algo_universe()
    database = str(Path(args.db or DEFAULT_DB).resolve())
    store = SQLiteBarStore(database)
    refresh_started = perf_counter()
    refresh_report = None
    context_symbols = sorted(NON_EQUITY_CONTEXT_SYMBOLS - {"SPY"})
    refresh_symbols = list(dict.fromkeys([*symbols, *context_symbols]))
    if not args.no_refresh:
        now_et = datetime.now(ET)
        refresh_to = now_et.date() if now_et.weekday() < 5 and now_et.time() >= time(16, 10) else now_et.date() - timedelta(days=1)
        logger.info("Refreshing SIP bars for %d symbols through %s", len(symbols), refresh_to)
        refresh_report = _refresh_incremental(
            store, selected_symbols=refresh_symbols, end_date=refresh_to,
        )
    refresh_seconds = round(perf_counter() - refresh_started, 3)
    if refresh_report is not None:
        logger.info(
            "SIP refresh finished in %.1fs (%d intraday rows, %d SPY daily rows)",
            refresh_seconds, refresh_report.get("intraday_rows_written", 0),
            refresh_report.get("spy_daily_rows_written", 0),
        )
    stage_progress("data_refresh", "completed", {
        "status": "completed", "seconds": refresh_seconds,
        "rows_written": (refresh_report or {}).get("intraday_rows_written", 0),
    })

    history_start, latest_end, missing = _latest_dates(store, symbols)
    covered_symbols = [symbol for symbol in symbols if symbol not in missing]
    if len(covered_symbols) < MIN_COVERED_UNIVERSE:
        raise ValueError(
            f"Only {len(covered_symbols)} selected equities have common SIP history; "
            f"at least {MIN_COVERED_UNIVERSE} are required"
        )
    logger.info(
        "Market data ready: %d/%d symbols, common sessions %s through %s",
        len(covered_symbols), len(symbols), history_start, latest_end,
    )
    latest_age_days = (datetime.now(ET).date() - latest_end).days
    if not args.no_refresh and latest_age_days > 4 and not optimization_only:
        raise ValueError(
            f"Plus SIP data is stale after refresh: latest common session {latest_end} "
            f"is {latest_age_days} calendar days old"
        )

    from .validation.final_holdout import supersede_final_holdout

    # Retain the former holdout manifest for audit, but let all research stages
    # use the latest completed common session. Each staged candidate is then
    # evaluated only on sessions after its own immutable staging date.
    holdout_report = supersede_final_holdout(output_dir=output_dir)
    cohort_end = latest_end
    warmup_start = RESEARCH_START - timedelta(days=RESEARCH_WARMUP_DAYS)
    logger.info("Computing research data fingerprint for the configured universe")
    market_data_version = research_data_version(
        database, start=warmup_start, end=cohort_end,
        symbols=[*covered_symbols, *context_symbols], feed=FEED, adjustment=ADJUSTMENT,
    )
    from market.regime import load_fred_regime_series

    macro_series, macro_diagnostics = load_fred_regime_series(
        store, refresh=not args.no_refresh,
    )
    macro_series = {
        name: [
            row for row in rows
            if str(row.get("date", ""))[:10] <= cohort_end.isoformat()
        ]
        for name, rows in macro_series.items()
    }
    regime_data_version = research_data_version(
        database,
        start=RESEARCH_START - timedelta(days=ALGO_REGIME_WARMUP_CALENDAR_DAYS),
        end=cohort_end, symbols=[], feed=FEED, adjustment=ADJUSTMENT,
    )
    from strategies.multi_factor.fundamentals import load_point_in_time_fundamentals

    try:
        equity_symbols = [symbol for symbol in covered_symbols if symbol in ALGO_FACTOR_SECTOR_BY_SYMBOL]
        fundamentals_by_symbol, fundamentals_diagnostics = load_point_in_time_fundamentals(
            equity_symbols, as_of=cohort_end,
        )
    except Exception as exc:
        fundamentals_by_symbol = {}
        fundamentals_diagnostics = {
            "status": "unavailable", "source": "sec_edgar_companyfacts",
            "symbols_requested": len(covered_symbols), "symbols_with_point_in_time_facts": 0,
            "failure_types": {"feed": type(exc).__name__},
            "availability_policy": "SEC filed date plus one calendar day; same-day filings excluded conservatively",
        }
    fundamentals_data_version = fundamentals_diagnostics.get("data_version")
    fundamental_field_coverage = fundamentals_diagnostics.get("factor_symbol_coverage") or {}
    observed_factor_coverage = [
        int(value) for value in fundamental_field_coverage.values()
        if isinstance(value, int) and value >= 12
    ]
    factor_model_min_universe = min(
        MIN_COVERED_UNIVERSE,
        min(observed_factor_coverage) if observed_factor_coverage else MIN_COVERED_UNIVERSE,
    )
    data_version = stable_hash({
        "market_data_version": market_data_version,
        "regime_data_version": regime_data_version,
        "regime_macro_data_version": stable_hash(macro_series),
        "fundamentals_data_version": fundamentals_data_version,
    })
    live_snapshot = live_configuration_manifest([*RESEARCHABLE_STRATEGIES, "swing_multi_factor", "swing_event_driven"])
    context = {
        "pipeline_version": AUTO_RESEARCH_PIPELINE_VERSION,
        "cohort_id": None,
        "database": database,
        "feed": FEED,
        "adjustment": ADJUSTMENT,
        "symbols": covered_symbols,
        "stored_coverage_start": history_start.isoformat(),
        "data_end": cohort_end.isoformat(),
        "data_version": data_version,
        "market_data_version": market_data_version,
        "regime_data_version": regime_data_version,
        "regime_macro_data_version": stable_hash(macro_series),
        "regime_macro_status": macro_diagnostics.get("status"),
        "fundamentals_data_version": fundamentals_data_version,
        "fundamentals_feed_status": fundamentals_diagnostics.get("status", "unavailable"),
        "factor_model_min_universe": factor_model_min_universe,
        "strategy_research_range": {
            "start": RESEARCH_START.isoformat(), "end": cohort_end.isoformat(),
            "indicator_warmup_start": warmup_start.isoformat(),
        },
        "adaptive_pipeline": {
            "discovery_version": ADAPTIVE_PIPELINE_VERSION,
            "verification_version": ADAPTIVE_VERIFICATION_VERSION,
            "window_days_by_flavor": {
                flavor: list(values) for flavor, values in ADAPTIVE_WINDOW_DAYS.items()
            },
            "discovery_method": "bayes",
            "discovery_candidates": ADAPTIVE_DISCOVERY_CANDIDATES,
            "discovery_iterations": ADAPTIVE_DISCOVERY_ITERATIONS,
            "discovery_saturation_policy": {
                "minimum_novelty_fraction": ADAPTIVE_DISCOVERY_MIN_NOVELTY_FRACTION,
                "maximum_domain_refinements": ADAPTIVE_DISCOVERY_MAX_DOMAIN_REFINEMENTS,
                "minimum_rounds_before_early_stop": ADAPTIVE_DISCOVERY_MIN_ROUNDS_BEFORE_STOP,
            },
            "verification_training_start": RESEARCH_START.isoformat(),
            "verification_end": cohort_end.isoformat(),
            "verification_initial_training_sessions": max(
                max(values) for values in ADAPTIVE_WINDOW_DAYS.values()
            ),
            "verification_candidates": ADAPTIVE_VERIFICATION_CANDIDATES,
            "verification_test_days": ADAPTIVE_VERIFICATION_TEST_DAYS,
            "verification_step_days": ADAPTIVE_VERIFICATION_STEP_DAYS,
            "verification_purge_sessions": ADAPTIVE_PURGE_SESSIONS,
            "multi_factor_min_universe_values": list(MULTI_FACTOR_MIN_UNIVERSE_VALUES),
            "continuous_improvement": {
                "version": "validated-champion-queue-v1",
                "persistent_champion_seeding": True,
                "persistent_challenger_validation_queue": True,
                "live_setting_in_exact_validation_cohort": True,
                "max_queued_challengers_per_strategy_per_full_run": 2,
            },
        },
        "strategies": list(RESEARCHABLE_STRATEGIES),
        "live_configuration_sha256": live_snapshot["sha256"],
        "portfolio_strategies": list(RESEARCHABLE_STRATEGIES),
        "swing_multi_factor_enabled": runtime_settings.swing_multi_factor_enabled,
        "swing_event_driven_enabled": runtime_settings.swing_event_driven_enabled,
        "factor_only": bool(args.factor_only),
        "final_holdout_quarantine": False,
        "final_holdout_status": holdout_report.get("status"),
        "research_cutoff_policy": "latest_completed_common_market_session",
        "historical_validation_policy": "rolling_walk_forward_with_outcome_horizon_purge",
        "forward_validation_policy": "immutable_candidate_versions_scored_on_sessions_after_staging",
    }
    fingerprint = stable_hash(context)
    if args.force:
        fingerprint = stable_hash({"context": context, "forced_at": datetime.now(timezone.utc).isoformat()})
    run_id = f"session-{cohort_end:%Y%m%d}-{fingerprint[:16]}"
    context["cohort_id"] = run_id
    optimization_cycle_id = f"optimization-{invocation_id}"
    if optimization_only:
        context["optimization_cycle_id"] = optimization_cycle_id
    context["fingerprint"] = fingerprint
    context["run_id"] = run_id
    context["invocation_id"] = invocation_id
    ledger.set_run_context(
        invocation_id, cohort_id=run_id, data_version=data_version,
        pipeline_version=AUTO_RESEARCH_PIPELINE_VERSION,
        metadata={"research_context": context},
    )
    logger.info(
        "Research cohort %s: %s through %s, %d symbols, %d workers",
        run_id, RESEARCH_START, cohort_end, len(covered_symbols), args.workers,
    )

    root = Path(args.output_dir) / "layers"
    cohort_dir = root / "runs" / run_id
    report_path = cohort_dir / "report.json"
    cohort_dir.mkdir(parents=True, exist_ok=True)
    factor_started = perf_counter()
    factor_path = cohort_dir / "factor_model.json"
    factor_cached, _ = read_json(factor_path)
    from .factor_research import COST_MODEL_VERSION, LOGIC_VERSION, train_factor_model
    from strategies.multi_factor.tree_models import TREE_LOGIC_VERSIONS

    factor_fingerprint = stable_hash({
        "cohort_fingerprint": fingerprint,
        "factor_logic_version": LOGIC_VERSION,
        "factor_timeline_schema_version": "current-tree-fits-in-point-in-time-timeline-v1",
        "tree_factor_logic_versions": TREE_LOGIC_VERSIONS,
        "factor_cost_model_version": COST_MODEL_VERSION,
        "sector_map": ALGO_FACTOR_SECTOR_BY_SYMBOL,
        "fundamentals_data_version": fundamentals_data_version,
    })
    if not args.force and factor_cached and factor_cached.get("fingerprint") == factor_fingerprint:
        factor_report = factor_cached["report"]
        factor_model_timing = {"seconds": 0.0, "reused": True}
        logger.info("Multi Factor diagnostics: reusing cached result")
    else:
        logger.info("Multi Factor diagnostics: fitting purged Ridge and boosted tree models before replay discovery")
        history = _load_daily_history(
            store, covered_symbols, start=warmup_start.isoformat(), end=cohort_end.isoformat(),
        )
        factor_report = train_factor_model(
            history, min_universe=factor_model_min_universe,
            sector_by_symbol=ALGO_FACTOR_SECTOR_BY_SYMBOL,
            fundamentals_by_symbol=fundamentals_by_symbol,
            fundamentals_diagnostics=fundamentals_diagnostics,
            fundamentals_data_version=fundamentals_data_version,
            data_version=data_version,
        )
        _atomic_json(factor_path, {"fingerprint": factor_fingerprint, "report": factor_report})
        factor_model_timing = {
            "seconds": round(perf_counter() - factor_started, 3), "reused": False,
        }
        logger.info("Multi Factor diagnostics finished in %.1fs", factor_model_timing["seconds"])
    stage_progress("factor_model", "reused" if factor_model_timing.get("reused") else "completed", {
        "status": "completed", "seconds": factor_model_timing.get("seconds", 0),
        "reused": factor_model_timing.get("reused", False),
        "data_version": data_version,
    })

    adaptive_root = Path(args.output_dir) / "adaptive"
    adaptive_discovery_started = perf_counter()
    logger.info(
        "Adaptive discovery %s",
        "skipped for factor-only run" if args.factor_only else f"starting ({len(RESEARCHABLE_STRATEGIES)} strategies)",
    )
    stage_progress("discovery", "running", {"status": "running", "strategies_total": len(RESEARCHABLE_STRATEGIES)})
    adaptive_discovery = (
        run_adaptive_discoveries(
            database=database, symbols=covered_symbols, latest_end=cohort_end, data_version=data_version,
            output_dir=adaptive_root, workers=args.workers,
            factor_weight_timeline=factor_report.get("weight_timeline") or [],
            invocation_id=getattr(args, "invocation_id", None),
            on_strategy_complete=strategy_progress,
        )
        if not args.factor_only else {"status": "skipped", "reason": "factor-only run", "strategies": {}}
    )
    ledger.record_discovery_stage(invocation_id, adaptive_discovery, data_version=data_version)
    stage_progress("discovery", adaptive_discovery.get("status", "completed"), adaptive_discovery)
    adaptive_discovery_seconds = round(perf_counter() - adaptive_discovery_started, 3)
    logger.info(
        "Adaptive discovery finished: %s in %.1fs",
        adaptive_discovery.get("status"), adaptive_discovery_seconds,
    )
    multi_factor_discovery = (adaptive_discovery.get("strategies") or {}).get("swing_multi_factor", {})
    if optimization_only:
        discovery_failed = adaptive_discovery.get("status") in {"error", "partial_error"}
        report = {
            "invocation_id": invocation_id,
            "schema": "swarm_algo_optimizer_cycle_v1",
            "status": "partial_error" if discovery_failed else "completed",
            "optimization_only": True,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "run_date_et": datetime.now(ET).date().isoformat(),
            "research_only": True,
            "live_configuration_changed": False,
            "trade_or_order_actions": 0,
            "data": _data_summary(
                history_start, latest_end, cohort_end, latest_age_days,
                covered_symbols, symbols, missing, universe_meta, refresh_report,
                research_start=RESEARCH_START, warmup_start=warmup_start,
            ),
            "factor_model": factor_report,
            "adaptive_research": {
                "discovery": adaptive_discovery,
                "verification": {
                    "status": "deferred",
                    "reason": "Optimizer cycles do not wait for rolling verification.",
                },
            },
            "adaptive_strategy_artifacts": {
                strategy: {
                    "discovery": (adaptive_discovery.get("strategies") or {}).get(strategy, {}).get("detail_artifact"),
                    "verification": None,
                    "coordinator_artifact": None,
                    "verification_summary": None,
                }
                for strategy in RESEARCHABLE_STRATEGIES
            },
            "multi_factor_discovery": multi_factor_discovery,
            "final_holdout": holdout_report,
            "orchestration": {
                **context,
                "cohort_id": run_id,
                "run_id": optimization_cycle_id,
                "optimization_cycle_id": optimization_cycle_id,
                "cycle_type": "archive_backed_optimizer",
                "cache_hit": False,
            },
            "stage_timings": {
                "factor_model": factor_model_timing,
                "data_refresh_seconds": refresh_seconds,
                "adaptive_discovery": {
                    "seconds": adaptive_discovery_seconds,
                    "status": adaptive_discovery.get("status"),
                },
                "adaptive_verification": {"seconds": 0.0, "status": "deferred"},
                "portfolio_replay": {"seconds": 0.0, "status": "deferred"},
                "forward_validation": {"seconds": 0.0, "status": "deferred"},
                "total_seconds": round(perf_counter() - total_started, 3),
            },
            "recommendation": {
                "optimizer": "The optimizer seeds each strategy from its persistent exact-validated champion and top queued challengers, keeps exploring archive variants, and queues discovery winners for rolling verification.",
                "verification": "The full cycle applies the same rolling gates to current live settings, champions, and up to two queued challengers per strategy before forward trials.",
                "adaptive_parameter_promotion_requires_human_confirmation": False,
            },
            "limitations": [
                "This cycle records discovery candidates in the persistent validation queue but does not run rolling verification, portfolio replay, or forward promotion checks.",
                "Training fitness can vary across rolling windows and does not guarantee future performance.",
            ],
        }
        stage_progress("report_assembly", "completed", {"status": report.get("status"), "optimization_only": True})
        return report

    adaptive_verification_started = perf_counter()
    logger.info(
        "Rolling verification %s",
        "skipped for factor-only run" if args.factor_only else f"starting ({len(RESEARCHABLE_STRATEGIES)} strategies)",
    )
    stage_progress("verification", "running", {"status": "running", "strategies_total": len(RESEARCHABLE_STRATEGIES)})
    adaptive_verification = (
        run_adaptive_verifications(
            database=database, symbols=covered_symbols,
            start=RESEARCH_START, end=cohort_end,
            data_version=data_version,
            output_dir=adaptive_root, workers=args.workers, force=args.force,
            factor_weight_timeline=factor_report.get("weight_timeline") or [],
            on_strategy_complete=strategy_progress,
        )
        if not args.factor_only else {"status": "skipped", "reason": "factor-only run", "strategies": {}}
    )
    candidate_validation_started = perf_counter()
    stage_progress("candidate_validation", "running", {"status": "running", "strategies_total": len(RESEARCHABLE_STRATEGIES)})
    if not args.factor_only:
        from .pipeline.candidate_validation import run_candidate_validation

        candidate_validation = run_candidate_validation(
            database=database, symbols=covered_symbols,
            start=RESEARCH_START, end=cohort_end,
            data_version=data_version, output_dir=adaptive_root,
            verification=adaptive_verification, discovery=adaptive_discovery,
            workers=args.workers, force=args.force,
            factor_weight_timeline=factor_report.get("weight_timeline") or [],
            on_strategy_complete=strategy_progress,
        )
    else:
        candidate_validation = {
            "status": "skipped", "reason": "factor-only run", "strategies": {},
        }
    adaptive_verification["candidate_validation"] = candidate_validation
    stage_progress("candidate_validation", candidate_validation.get("status", "completed"), candidate_validation)
    candidate_validation_seconds = round(perf_counter() - candidate_validation_started, 3)
    logger.info(
        "Exact candidate validation finished: %s in %.1fs",
        candidate_validation.get("status"), candidate_validation_seconds,
    )
    ledger.record_verification_stage(
        invocation_id, adaptive_verification,
        {strategy: {"verification": (adaptive_verification.get("strategies") or {}).get(strategy, {}).get("detail_artifact")}
         for strategy in RESEARCHABLE_STRATEGIES},
        data_version=data_version,
    )
    stage_progress("verification", adaptive_verification.get("status", "completed"), adaptive_verification)
    adaptive_verification_seconds = round(perf_counter() - adaptive_verification_started, 3)
    logger.info(
        "Rolling verification finished: %s in %.1fs",
        adaptive_verification.get("status"), adaptive_verification_seconds,
    )
    from .validation.drift_monitor import update_research_drift_monitor

    drift_report = update_research_drift_monitor(
        adaptive_verification, output_dir=output_dir,
    )
    stage_progress("drift_monitor", "completed", drift_report)
    promotion_pause_reasons = []
    if drift_report.get("promotion_pause"):
        promotion_pause_reasons.append("drift")
    promotion_allowed = not promotion_pause_reasons
    adaptive_coordinator_root = adaptive_root / "verification-coordinator"
    verification_statuses = adaptive_verification.get("strategies") or {}
    adaptive_portfolio_ready = all(
        (verification_statuses.get(strategy) or {}).get("coordinator_artifact")
        and Path((verification_statuses.get(strategy) or {})["coordinator_artifact"]).is_file()
        for strategy in RESEARCHABLE_STRATEGIES
    )
    portfolio_source = "adaptive_short_window"
    if adaptive_portfolio_ready and not args.force and not args.factor_only:
        cached, _ = read_json(report_path)
        if (
            cached
            and cached.get("orchestration", {}).get("fingerprint") == fingerprint
            and (cached.get("portfolio_coordinator_replay") or {}).get("profile_selection_source") == portfolio_source
        ):
            stage_progress("portfolio_replay", "reused", {
                "status": "completed", "reused": True,
                "report_path": str((cohort_dir / "report.json").resolve()),
            })
            validation_started = perf_counter()
            from .validation.forward_validation import run_forward_validation_cycle

            forward_validation = run_forward_validation_cycle(
                discovery=adaptive_discovery, verification=adaptive_verification,
                store=store, database=database, symbols=covered_symbols,
                end=cohort_end, data_version=data_version,
                allow_stage=True,
                allow_promotion=promotion_allowed,
                promotion_pause_reasons=promotion_pause_reasons,
                invocation_id=invocation_id, ledger_path=ledger_path,
                factor_weight_timeline=factor_report.get("weight_timeline") or [],
            )
            stage_progress("forward_validation", forward_validation.get("status", "completed"), forward_validation)
            if adaptive_portfolio_ready:
                refresh_active_parameter_profile()
            cached = {
                **cached,
                "invocation_id": invocation_id,
                "status": (
                    "partial_error"
                    if forward_validation.get("status") in {"error", "partial_error"}
                    else cached.get("status", "completed")
                ),
                "forward_validation": forward_validation,
                "drift_monitor": drift_report,
                "final_holdout": holdout_report,
                "regime_inputs": {
                    "macro_status": macro_diagnostics.get("status"),
                    "macro_data_version": stable_hash(macro_series),
                    "market_data_version": regime_data_version,
                    "context_symbols": context_symbols,
                    "context_warmup_days": REGIME_CONTEXT_WARMUP_DAYS,
                    "spy_regime_warmup_days": ALGO_REGIME_WARMUP_CALENDAR_DAYS,
                },
                "live_configuration_changed": bool(forward_validation.get("live_configuration_changed")),
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
                    "adaptive_candidate_validation": {
                        "seconds": candidate_validation_seconds,
                        "status": candidate_validation.get("status"),
                    },
                    "forward_validation": {
                        "seconds": round(perf_counter() - validation_started, 3),
                        "status": forward_validation.get("status"),
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
                "multi_factor_discovery": multi_factor_discovery,
            }
            cached.setdefault("recommendation", {}).update({
                "adaptive_parameter_promotion_requires_human_confirmation": False,
                "forward_validation": (
                    "Promotion follows rolling historical and versioned forward simulation gates; a detected drift warning pauses it."
                ),
            })
            cached["orchestration"] = {
                **(cached.get("orchestration") or {}),
                "invocation_id": invocation_id,
                "cache_hit": False,
                "cohort_report_reused": True,
                "portfolio_reused": True,
            }
            cached["cache_hit"] = False
            cached["stage_timings"]["portfolio_replay"] = {
                "seconds": 0.0,
                "reused": True,
            }
            cached["run_date_et"] = datetime.now(ET).date().isoformat()
            cached["created_at"] = datetime.now(timezone.utc).isoformat()
            cached = _attach_fitness_timeline(cached, Path(args.output_dir))
            _atomic_json(report_path, cached)
            _atomic_json(root / "latest.json", cached)
            stage_progress("report_assembly", "completed", {"status": cached.get("status"), "cohort_report_reused": True})
            logger.info(
                "Discovery advanced on the existing quote cohort; reused historical verification and portfolio replay"
            )
            return cached

    timings: dict[str, object] = {
        "factor_model": factor_model_timing,
        "data_refresh_seconds": refresh_seconds,
        "adaptive_discovery": {
            "seconds": adaptive_discovery_seconds, "status": adaptive_discovery.get("status"),
        },
        "adaptive_verification": {
            "seconds": adaptive_verification_seconds, "status": adaptive_verification.get("status"),
        },
        "adaptive_candidate_validation": {
            "seconds": candidate_validation_seconds,
            "status": candidate_validation.get("status"),
        },
    }
    if args.factor_only:
        validation_started = perf_counter()
        stage_progress("forward_validation", "running", {"status": "running"})
        from .validation.forward_validation import run_forward_validation_cycle

        forward_validation = run_forward_validation_cycle(
            discovery=adaptive_discovery, verification=adaptive_verification,
            store=store, database=database, symbols=covered_symbols,
            end=cohort_end, data_version=data_version, allow_stage=False,
            allow_promotion=promotion_allowed,
            promotion_pause_reasons=promotion_pause_reasons,
            invocation_id=invocation_id, ledger_path=ledger_path,
            factor_weight_timeline=factor_report.get("weight_timeline") or [],
        )
        stage_progress("forward_validation", forward_validation.get("status", "completed"), forward_validation)
        timings["forward_validation"] = {
            "seconds": round(perf_counter() - validation_started, 3),
            "status": forward_validation.get("status"),
        }
        report = {
            "invocation_id": invocation_id,
            "schema": "swarm_algo_nightly_research_v2",
            "status": (
                "partial_error"
                if forward_validation.get("status") in {"error", "partial_error"}
                else "completed"
            ),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "research_only": not bool(forward_validation.get("live_configuration_changed")),
            "live_configuration_changed": bool(forward_validation.get("live_configuration_changed")),
            "trade_or_order_actions": 0, "factor_model": factor_report,
            "factor_only": True,
            "forward_validation": forward_validation,
            "drift_monitor": drift_report,
            "final_holdout": holdout_report,
            "regime_inputs": {
                "macro_status": macro_diagnostics.get("status"),
                "macro_data_version": stable_hash(macro_series),
                "market_data_version": regime_data_version,
                "context_symbols": context_symbols,
                "context_warmup_days": REGIME_CONTEXT_WARMUP_DAYS,
                "spy_regime_warmup_days": ALGO_REGIME_WARMUP_CALENDAR_DAYS,
            },
            "data": _data_summary(
                history_start, latest_end, cohort_end, latest_age_days,
                covered_symbols, symbols, missing, universe_meta, refresh_report,
                research_start=RESEARCH_START, warmup_start=warmup_start,
            ),
            "adaptive_research": {
                "discovery": adaptive_discovery,
                "verification": adaptive_verification,
            },
            "multi_factor_discovery": multi_factor_discovery,
            "orchestration": {**context, "cache_hit": False},
            "stage_timings": {**timings, "total_seconds": round(perf_counter() - total_started, 3)},
            "limitations": [
                "Factor fitting is research-only and does not update live weights.",
                "No adaptive strategy discovery, verification, or portfolio replay was requested.",
                "Existing forward simulation trials may continue or promote; this factor-only run does not stage new candidates.",
            ],
        }
        report["run_date_et"] = datetime.now(ET).date().isoformat()
        report = _attach_fitness_timeline(report, Path(args.output_dir))
        _atomic_json(report_path, report)
        _atomic_json(root / "latest.json", report)
        stage_progress("report_assembly", "completed", {"status": report.get("status")})
        return report

    portfolio_path = cohort_dir / "portfolio"
    portfolio_report = None
    if adaptive_portfolio_ready:
        portfolio_started = perf_counter()
        stage_progress("portfolio_replay", "running", {"status": "running"})
        cached_portfolio, _ = read_json(portfolio_path / "latest.json")
        if (
            not args.force and cached_portfolio
            and cached_portfolio.get("orchestration", {}).get("fingerprint") == fingerprint
            and cached_portfolio.get("profile_selection_source") == portfolio_source
        ):
            portfolio_report = cached_portfolio
            portfolio_reused = True
            logger.info("Portfolio replay: reusing cached result")
        else:
            logger.info("Portfolio replay starting for %d research strategies", len(RESEARCHABLE_STRATEGIES))
            from .validation.portfolio_simulation import run_coordinated_portfolio

            portfolio_args = SimpleNamespace(
                strategies=",".join(RESEARCHABLE_STRATEGIES),
                artifact_dir=str(adaptive_coordinator_root),
                event_calendar=None,
                factor_weight_timeline=factor_report.get("weight_timeline") or [],
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
            logger.info("Portfolio replay finished in %.1fs", perf_counter() - portfolio_started)
        timings["portfolio_replay"] = {
            "seconds": 0.0 if portfolio_reused else round(perf_counter() - portfolio_started, 3),
            "reused": portfolio_reused,
        }
        stage_progress("portfolio_replay", "reused" if portfolio_reused else "completed", {
            "status": "completed", "reused": portfolio_reused,
            "report_path": str((portfolio_path / "latest.json").resolve()),
        })
    else:
        logger.warning("Portfolio replay skipped: verification artifacts are incomplete")
        timings["portfolio_replay"] = {
            "seconds": 0.0, "status": "skipped",
            "reason": "Adaptive verification is incomplete; portfolio replay was skipped.",
        }
        stage_progress("portfolio_replay", "skipped", {
            "status": "skipped", "reason": "Adaptive verification is incomplete; portfolio replay was skipped.",
        })

    factor_ic = (factor_report.get("adaptive_prequential_ic") or {}).get("mean_spearman_ic")
    validation_started = perf_counter()
    stage_progress("forward_validation", "running", {"status": "running"})
    from .validation.forward_validation import run_forward_validation_cycle

    forward_validation = run_forward_validation_cycle(
        discovery=adaptive_discovery, verification=adaptive_verification,
        store=store, database=database, symbols=covered_symbols,
        end=cohort_end, data_version=data_version,
        allow_stage=True,
        allow_promotion=promotion_allowed,
        promotion_pause_reasons=promotion_pause_reasons,
        invocation_id=invocation_id, ledger_path=ledger_path,
        factor_weight_timeline=factor_report.get("weight_timeline") or [],
    )
    stage_progress("forward_validation", forward_validation.get("status", "completed"), forward_validation)
    if adaptive_portfolio_ready:
        refresh_active_parameter_profile()
    timings["forward_validation"] = {
        "seconds": round(perf_counter() - validation_started, 3),
        "status": forward_validation.get("status"),
    }
    report = {
        "invocation_id": invocation_id,
        "schema": "swarm_algo_nightly_research_v2",
        "status": (
            "partial_error"
            if forward_validation.get("status") in {"error", "partial_error"}
            else "completed" if adaptive_portfolio_ready else "partial"
        ),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "research_only": not bool(forward_validation.get("live_configuration_changed")),
        "live_configuration_changed": bool(forward_validation.get("live_configuration_changed")),
        "trade_or_order_actions": 0,
        "data": _data_summary(
            history_start, latest_end, cohort_end, latest_age_days,
            covered_symbols, symbols, missing, universe_meta, refresh_report,
            research_start=RESEARCH_START, warmup_start=warmup_start,
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
        "multi_factor_discovery": multi_factor_discovery,
        "forward_validation": forward_validation,
        "drift_monitor": drift_report,
        "final_holdout": holdout_report,
        "regime_inputs": {
            "macro_status": macro_diagnostics.get("status"),
            "macro_data_version": stable_hash(macro_series),
            "market_data_version": regime_data_version,
            "context_symbols": context_symbols,
            "context_warmup_days": REGIME_CONTEXT_WARMUP_DAYS,
            "spy_regime_warmup_days": ALGO_REGIME_WARMUP_CALENDAR_DAYS,
        },
        "portfolio_coordinator_replay": portfolio_report,
        "portfolio_summary": (portfolio_report or {}).get("fold_summary"),
        "recommendation": {
            "adaptive_research": (
                "Dynamic discovery and rolling out-of-sample verification cover the active strategy set."
                if adaptive_portfolio_ready
                else "Adaptive verification is incomplete; portfolio replay was skipped."
            ),
            "swing_multi_factor": (
                "Multi Factor is active through the shared live coordinator; regime-specific candidates can update signal parameters after historical and forward simulation gates pass."
                if factor_ic is not None and factor_ic > 0
                else "Multi Factor is active through the shared live coordinator; latest factor diagnostics were not positive, and any regime-specific candidate still needs to pass historical and forward simulation gates."
            ),
            "adaptive_parameter_promotion_requires_human_confirmation": False,
            "forward_validation": (
                "Promotion follows rolling historical and versioned forward simulation gates; a detected drift warning pauses it."
            ),
        },
        "orchestration": {**context, "cache_hit": False},
        "stage_timings": {**timings, "total_seconds": round(perf_counter() - total_started, 3)},
        "limitations": [
            "Adaptive fitting, verification, and fold-aligned portfolio replay use 2020 onward; earlier bars are indicator warm-up only.",
            "The fixed current Algo universe is used across the 2020-onward research range; historical universe membership is not reconstructed.",
            "The portfolio replay includes strategies enabled in live config with available historical data.",
            "Each walk-forward fold begins with fresh capital; positions and risk state do not carry between folds.",
            "Each immutable forward candidate version is scored only on sessions after its staging date and with modeled execution costs; results are not a performance forecast.",
            "Historical walk-forward results are reused across research cycles and are not a pristine independent holdout; all candidate trials remain in the archive and promotion requires forward evidence.",
            *([] if adaptive_portfolio_ready else [
                "Adaptive verification did not produce coordinator artifacts for every active strategy; the portfolio replay was skipped.",
            ]),
        ],
    }
    report["run_date_et"] = datetime.now(ET).date().isoformat()
    report = _attach_fitness_timeline(report, Path(args.output_dir))
    _atomic_json(report_path, report)
    _atomic_json(root / "latest.json", report)
    stage_progress("report_assembly", "completed", {"status": report.get("status")})
    return report


def _data_summary(
    history_start: date, latest_end: date, cohort_end: date, latest_age_days: int,
    covered_symbols: list[str], requested_symbols: list[str], missing: list[str],
    universe_meta: dict, refresh_report: dict | None, *, research_start: date,
    warmup_start: date,
) -> dict:
    return {
        "tier": data_tier(), "feed": FEED, "adjustment": ADJUSTMENT,
        "range_start": research_start.isoformat(),
        "stored_coverage": {
            "start": history_start.isoformat(), "end": latest_end.isoformat(),
        },
        "strategy_research_range": {
            "start": research_start.isoformat(), "end": cohort_end.isoformat(),
            "indicator_warmup_start": warmup_start.isoformat(),
        },
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
        description="Run an archive-backed optimization cycle or full Algo research verification",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument(
        "--workers", type=int, default=min(8, available_cpu_count()),
        help="CPU budget shared by adaptive discovery and verification workers",
    )
    parser.add_argument("--no-refresh", action="store_true", help="use existing bars without fetching recent SIP history")
    parser.add_argument("--factor-only", action="store_true", help="fit the factor model without adaptive or portfolio stages")
    parser.add_argument(
        "--optimization-only", action="store_true",
        help="run a new archive-backed discovery cycle and defer rolling verification and portfolio replay",
    )
    parser.add_argument("--force", action="store_true", help="create a fresh data cohort and recompute cached research stages")
    parser.add_argument("--invocation-id", help="stable research ID used to resume an interrupted cycle")
    return parser


def _configure_progress_logging(output_dir: Path) -> Path | None:
    """Log to the terminal and a bounded file so a running job can be tailed."""
    handlers = [logging.StreamHandler()]
    log_path = output_dir / "logs" / "auto-research.log"
    file_error = None
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(
            log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8",
        ))
    except OSError as exc:
        file_error = exc
        log_path = None
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [auto-research] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )
    if file_error is not None:
        logger.warning("File progress log unavailable; console logging remains enabled: %s", file_error)
    return log_path


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.optimization_only and args.factor_only:
        parser.error("--optimization-only cannot be combined with --factor-only")
    output_dir = Path(args.output_dir)
    optimizer_latest_path = output_dir / "optimizer" / "latest.json"
    latest_report_path = optimizer_latest_path if args.optimization_only else output_dir / "latest.json"
    invocation_id = args.invocation_id or uuid4().hex
    ledger_path = output_dir / "research.sqlite3"
    ledger = None
    args.invocation_id = invocation_id
    args.ledger_path = str(ledger_path)
    progress_dir = output_dir / "invocations" / invocation_id
    progress_dir.mkdir(parents=True, exist_ok=True)
    progress_state = {
        "schema": "swarm_autoresearch_progress_v1",
        "invocation_id": invocation_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "current_stage": None,
        "stages": {},
        "strategies": {},
    }

    def compact_progress_payload(payload: dict | None) -> dict:
        payload = payload or {}
        keys = (
            "status", "error", "reason", "detail_artifact", "report_path",
            "coordinator_artifact", "reused", "seconds", "candidate_count",
            "fold_count", "strategies_total", "data_version", "optimization_only",
            "completed_fold_jobs", "last_test_start", "last_test_end",
        )
        summary = {key: payload[key] for key in keys if key in payload}
        for field in ("strategies", "errors", "error_strategies"):
            value = payload.get(field)
            if isinstance(value, dict):
                summary[field] = {
                    name: {
                        key: item.get(key)
                        for key in (
                            "status", "error", "detail_artifact", "coordinator_artifact",
                            "selection_status", "adaptive_improvement", "archive_sequence",
                            "search_seed", "reused_fold_count", "candidate_count",
                        ) if isinstance(item, dict) and key in item
                    }
                    for name, item in value.items()
                }
            elif isinstance(value, list):
                summary[field] = value
        return _json_safe(summary)

    def progress_callback(*, stage: str, status: str, payload=None, strategy: str | None = None) -> None:
        compact = compact_progress_payload(payload)
        now = datetime.now(timezone.utc).isoformat()
        progress_state["updated_at"] = now
        progress_state["current_stage"] = stage if status == "running" else progress_state.get("current_stage")
        if strategy:
            progress_state["strategies"].setdefault(stage, {})[strategy] = {
                "status": status, **compact, "updated_at": now,
            }
        else:
            progress_state["stages"][stage] = {
                "status": status, **compact, "updated_at": now,
            }
            if status not in {"running", "pending"} and progress_state.get("current_stage") == stage:
                progress_state["current_stage"] = None
        snapshot = {**progress_state, "progress_path": str((progress_dir / "progress.json").resolve())}
        _atomic_json(progress_dir / "progress.json", snapshot)
        _atomic_json(output_dir / "latest-progress.json", snapshot)
        _atomic_json(progress_dir / "partial-report.json", {
            "schema": "swarm_autoresearch_partial_report_v1",
            "invocation_id": invocation_id,
            "status": "running",
            "current_stage": progress_state.get("current_stage"),
            "updated_at": now,
            "stages": progress_state["stages"],
            "strategies": progress_state["strategies"],
            "progress_path": str((progress_dir / "progress.json").resolve()),
        })

    args.progress_callback = progress_callback
    log_path = _configure_progress_logging(output_dir)
    today = datetime.now(ET).date().isoformat()
    logger.info(
        "AutoResearch starting (workers=%d, refresh=%s, optimization_only=%s, factor_only=%s, force=%s)",
        args.workers, not args.no_refresh, args.optimization_only, args.factor_only, args.force,
    )
    if log_path is not None:
        logger.info("Progress log: %s", log_path.resolve())
    try:
        ledger = ResearchLedger(ledger_path)
        with _research_lock(Path(output_dir) / "locks" / "nightly-layers.lock"):
            interrupted = ledger.interrupt_abandoned_invocations(
                except_invocation_id=invocation_id,
            )
            if interrupted:
                logger.warning("Marked %d abandoned AutoResearch invocations interrupted", interrupted)
            ledger.start_invocation(invocation_id, request={
                "workers": args.workers, "no_refresh": args.no_refresh,
                "factor_only": args.factor_only,
                "optimization_only": args.optimization_only,
                "force": args.force,
            })
            ledger.import_legacy(output_dir)
            report = run_daily_research(args)
            report["run_date_et"] = today
            report["workers_requested"] = args.workers
    except Exception as exc:
        logger.error("AutoResearch failed: %s: %s", type(exc).__name__, exc)
        failed_stage = progress_state.get("current_stage") or "orchestration"
        progress_callback(
            stage=failed_stage, status="failed",
            payload={"status": "failed", "error": f"{type(exc).__name__}: {exc}"},
        )
        report = {
            "schema": (
                "swarm_algo_optimizer_cycle_v1"
                if args.optimization_only else "swarm_algo_nightly_research_v2"
            ),
            "invocation_id": invocation_id,
            "status": "error", "run_date_et": today,
            "optimization_only": args.optimization_only,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "run_id": f"error-{datetime.now(ET).strftime('%Y%m%dT%H%M%S')}",
            "research_only": True, "live_configuration_changed": False,
            "trade_or_order_actions": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }
        if not args.optimization_only:
            report = _attach_fitness_timeline(report, output_dir)
        report = _json_safe(report)
        print(json.dumps(report, sort_keys=True), file=sys.stderr)
        invocation_report_path = output_dir / "invocations" / invocation_id / "report.json"
        report["invocation_report_path"] = str(invocation_report_path.resolve())
        _atomic_json(invocation_report_path, report)
        if args.optimization_only:
            cycle_path = output_dir / "optimizer" / "cycles" / f"{invocation_id}.json"
            _atomic_json(cycle_path, report)
        if ledger is not None:
            ledger.start_invocation(invocation_id, request={
                "workers": args.workers, "no_refresh": args.no_refresh,
                "factor_only": args.factor_only,
                "optimization_only": args.optimization_only,
                "force": args.force,
            })
            ledger.record_completed_report(
                invocation_id, report, report_path=str(invocation_report_path.resolve()),
            )
        if not args.optimization_only:
            _atomic_json(output_dir / f"{today}.json", report)
        _atomic_json(latest_report_path, report)
        progress_state.update({"status": "failed", "completed_at": datetime.now(timezone.utc).isoformat()})
        snapshot = {**progress_state, "report_path": str(invocation_report_path.resolve())}
        _atomic_json(progress_dir / "progress.json", snapshot)
        _atomic_json(output_dir / "latest-progress.json", snapshot)
        _atomic_json(progress_dir / "partial-report.json", report)
        return 2

    report = _json_safe(report)
    logger.info(
        "AutoResearch finished: status=%s run_id=%s cache_hit=%s total=%.1fs",
        report.get("status"),
        (report.get("orchestration") or {}).get("run_id"),
        (report.get("orchestration") or {}).get("cache_hit", False),
        (report.get("stage_timings") or {}).get("total_seconds", 0.0),
    )
    report["invocation_id"] = invocation_id
    invocation_report_path = output_dir / "invocations" / invocation_id / "report.json"
    report["invocation_report_path"] = str(invocation_report_path.resolve())
    _atomic_json(invocation_report_path, report)
    if args.optimization_only:
        cycle_path = output_dir / "optimizer" / "cycles" / f"{invocation_id}.json"
        _atomic_json(cycle_path, report)
    if ledger is not None:
        ledger.record_completed_report(
            invocation_id, report, report_path=str(invocation_report_path.resolve()),
        )
    if not args.optimization_only:
        _atomic_json(output_dir / f"{today}.json", report)
    _atomic_json(latest_report_path, report)
    progress_state.update({
        "status": "completed" if report.get("status") not in {"error", "partial_error"} else "partial_error",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "current_stage": None,
    })
    snapshot = {**progress_state, "report_path": str(invocation_report_path.resolve())}
    _atomic_json(progress_dir / "progress.json", snapshot)
    _atomic_json(output_dir / "latest-progress.json", snapshot)
    _atomic_json(progress_dir / "partial-report.json", report)
    discovery_report = ((report.get("adaptive_research") or {}).get("discovery") or {})
    print(json.dumps(_json_safe({
        "status": report["status"],
        "report": str(latest_report_path.resolve()),
        "run_id": report.get("orchestration", {}).get("run_id"),
        "range": [
            report["data"]["strategy_research_range"]["start"],
            report["data"]["strategy_research_range"]["end"],
        ],
        "stored_coverage": report["data"].get("stored_coverage"),
        "strategies": len(
            report.get("adaptive_strategy_artifacts")
            or discovery_report.get("strategies") or {}
        ),
        "discovery": {
            "status": discovery_report.get("status"),
            "strategies": {
                strategy: {
                    "archive_sequence": item.get("archive_sequence"),
                    "new_parameter_variants_on_this_data": (
                        item.get("adaptive_novelty") or {}
                    ).get("new_parameter_variants_on_this_data"),
                }
                for strategy, item in (discovery_report.get("strategies") or {}).items()
            },
        },
        "portfolio": report.get("portfolio_summary"),
        "cache_hit": report.get("orchestration", {}).get("cache_hit", False),
        "live_configuration_changed": bool(report.get("live_configuration_changed")),
        "forward_validation": {
            "status": (report.get("forward_validation") or {}).get("status"),
            "active_trial_count": (report.get("forward_validation") or {}).get("active_trial_count"),
            "staged_candidate_ids": (report.get("forward_validation") or {}).get("staged_candidate_ids", []),
            "promoted_candidate_count": len((report.get("forward_validation") or {}).get("promoted", [])),
        },
    }), sort_keys=True, default=str))
    return 2 if report.get("status") in {"error", "partial_error"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
