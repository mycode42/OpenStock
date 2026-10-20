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

from src.config import get_algo_runtime_settings
from src.algo.cpu import available_cpu_count
from src.algo.market_store import DEFAULT_DB, SQLiteBarStore, fetch_alpaca_bars
from src.algo.strategies.registry import refresh_active_parameter_profile
from src.market_data import data_tier, regular_feed

from .adaptive_pipeline import (
    ADAPTIVE_PIPELINE_VERSION, ADAPTIVE_VERIFICATION_VERSION,
    run_adaptive_discoveries, run_adaptive_verifications,
)
from .artifact_cache import read_json, research_data_version, stable_hash
from .data import (
    _latest_dates, _load_daily_history,
    _refresh_incremental as _refresh_incremental_impl,
    load_algo_universe,
)
from .files import _atomic_json, _research_lock
from .fitness_timeline import _attach_fitness_timeline
from .live_manifest import live_configuration_manifest
from .research_ledger import ResearchLedger
from .settings import (
    ADAPTIVE_DISCOVERY_CANDIDATES, ADAPTIVE_DISCOVERY_ITERATIONS,
    ADAPTIVE_VERIFICATION_CANDIDATES,
    ADAPTIVE_VERIFICATION_STEP_DAYS, ADAPTIVE_VERIFICATION_TEST_DAYS,
    ADAPTIVE_WINDOW_DAYS, MIN_COVERED_UNIVERSE, MULTI_FACTOR_MIN_UNIVERSE_VALUES,
    RESEARCH_START, RESEARCH_WARMUP_DAYS,
    ADJUSTMENT, DEFAULT_OUTPUT_DIR, ET, FEED, LIVE_RESEARCH_STRATEGIES,
    RESEARCHABLE_STRATEGIES,
)

AUTO_RESEARCH_PIPELINE_VERSION = "algo-research-layers-v13-regime-forward-validation-2024"
logger = logging.getLogger(__name__)


def _refresh_incremental(store, *, selected_symbols, end_date):
    """Refresh the configured universe through the AutoResearch data adapter."""
    return _refresh_incremental_impl(
        store, selected_symbols=selected_symbols, end_date=end_date,
        data_tier_fn=data_tier, regular_feed_fn=regular_feed,
        fetch_bars_fn=fetch_alpaca_bars,
    )


def run_daily_research(args) -> dict:
    """Run one versioned research cohort, reusing compatible artifacts when possible."""
    output_dir = Path(args.output_dir)
    invocation_id = getattr(args, "invocation_id", None) or uuid4().hex
    ledger_path = Path(getattr(args, "ledger_path", output_dir / "research.sqlite3"))
    ledger = ResearchLedger(ledger_path)
    ledger.start_invocation(invocation_id, request={
        "workers": args.workers, "no_refresh": args.no_refresh,
        "factor_only": args.factor_only, "force": args.force,
    })
    ledger.import_legacy(output_dir)
    args.invocation_id = invocation_id
    args.ledger_path = str(ledger_path)
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
    if not args.no_refresh:
        now_et = datetime.now(ET)
        refresh_to = now_et.date() if now_et.weekday() < 5 and now_et.time() >= time(16, 10) else now_et.date() - timedelta(days=1)
        logger.info("Refreshing SIP bars for %d symbols through %s", len(symbols), refresh_to)
        refresh_report = _refresh_incremental(
            store, selected_symbols=symbols, end_date=refresh_to,
        )
    refresh_seconds = round(perf_counter() - refresh_started, 3)
    if refresh_report is not None:
        logger.info(
            "SIP refresh finished in %.1fs (%d intraday rows, %d SPY daily rows)",
            refresh_seconds, refresh_report.get("intraday_rows_written", 0),
            refresh_report.get("spy_daily_rows_written", 0),
        )

    history_start, latest_end, missing = _latest_dates(store, symbols)
    covered_symbols = [symbol for symbol in symbols if symbol not in missing]
    logger.info(
        "Market data ready: %d/%d symbols, common sessions %s through %s",
        len(covered_symbols), len(symbols), history_start, latest_end,
    )
    latest_age_days = (datetime.now(ET).date() - latest_end).days
    if not args.no_refresh and latest_age_days > 4:
        raise ValueError(
            f"Plus SIP data is stale after refresh: latest common session {latest_end} "
            f"is {latest_age_days} calendar days old"
        )

    # Anchor each run to the latest fully available common session so the
    # adaptive fit and portfolio replay keep pace with current market data.
    cohort_end = latest_end
    warmup_start = RESEARCH_START - timedelta(days=RESEARCH_WARMUP_DAYS)
    logger.info("Computing research data fingerprint for the configured universe")
    data_version = research_data_version(
        database, start=warmup_start, end=cohort_end,
        symbols=covered_symbols, feed=FEED, adjustment=ADJUSTMENT,
    )
    live_snapshot = live_configuration_manifest([*RESEARCHABLE_STRATEGIES, "multi_factor", "event_driven"])
    context = {
        "pipeline_version": AUTO_RESEARCH_PIPELINE_VERSION,
        "database": database,
        "feed": FEED,
        "adjustment": ADJUSTMENT,
        "symbols": covered_symbols,
        "stored_coverage_start": history_start.isoformat(),
        "data_end": cohort_end.isoformat(),
        "data_version": data_version,
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
            "verification_training_start": RESEARCH_START.isoformat(),
            "verification_end": cohort_end.isoformat(),
            "verification_initial_training_sessions": max(
                max(values) for values in ADAPTIVE_WINDOW_DAYS.values()
            ),
            "verification_candidates": ADAPTIVE_VERIFICATION_CANDIDATES,
            "verification_test_days": ADAPTIVE_VERIFICATION_TEST_DAYS,
            "verification_step_days": ADAPTIVE_VERIFICATION_STEP_DAYS,
            "multi_factor_min_universe_values": list(MULTI_FACTOR_MIN_UNIVERSE_VALUES),
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
    run_id = f"session-{cohort_end:%Y%m%d}-{fingerprint[:16]}"
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
    adaptive_root = Path(args.output_dir) / "adaptive"
    adaptive_discovery_started = perf_counter()
    logger.info(
        "Adaptive discovery %s",
        "skipped for factor-only run" if args.factor_only else f"starting ({len(RESEARCHABLE_STRATEGIES)} strategies)",
    )
    adaptive_discovery = (
        run_adaptive_discoveries(
            database=database, symbols=covered_symbols, latest_end=latest_end, data_version=data_version,
            output_dir=adaptive_root, workers=args.workers,
        )
        if not args.factor_only else {"status": "skipped", "reason": "factor-only run", "strategies": {}}
    )
    ledger.record_discovery_stage(invocation_id, adaptive_discovery, data_version=data_version)
    adaptive_discovery_seconds = round(perf_counter() - adaptive_discovery_started, 3)
    logger.info(
        "Adaptive discovery finished: %s in %.1fs",
        adaptive_discovery.get("status"), adaptive_discovery_seconds,
    )
    adaptive_verification_started = perf_counter()
    logger.info(
        "Rolling verification %s",
        "skipped for factor-only run" if args.factor_only else f"starting ({len(RESEARCHABLE_STRATEGIES)} strategies)",
    )
    adaptive_verification = (
        run_adaptive_verifications(
            database=database, symbols=covered_symbols,
            start=RESEARCH_START, end=cohort_end,
            data_version=data_version,
            output_dir=adaptive_root, workers=args.workers, force=args.force,
        )
        if not args.factor_only else {"status": "skipped", "reason": "factor-only run", "strategies": {}}
    )
    ledger.record_verification_stage(
        invocation_id, adaptive_verification,
        {strategy: {"verification": (adaptive_verification.get("strategies") or {}).get(strategy, {}).get("detail_artifact")}
         for strategy in RESEARCHABLE_STRATEGIES},
        data_version=data_version,
    )
    adaptive_verification_seconds = round(perf_counter() - adaptive_verification_started, 3)
    logger.info(
        "Rolling verification finished: %s in %.1fs",
        adaptive_verification.get("status"), adaptive_verification_seconds,
    )
    multi_factor_discovery = (adaptive_discovery.get("strategies") or {}).get("multi_factor", {})
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
            validation_started = perf_counter()
            from .forward_validation import run_forward_validation_cycle

            forward_validation = run_forward_validation_cycle(
                discovery=adaptive_discovery, verification=adaptive_verification,
                store=store, database=database, symbols=covered_symbols,
                end=cohort_end, data_version=data_version,
                invocation_id=invocation_id, ledger_path=ledger_path,
            )
            cached = {
                **cached,
                "invocation_id": invocation_id,
                "status": (
                    "partial_error"
                    if forward_validation.get("status") in {"error", "partial_error"}
                    else cached.get("status", "completed")
                ),
                "forward_validation": forward_validation,
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
                    "Promotion is automatic after the historical gates and the minimum sample of positive forward simulated results versus the current profile."
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
            logger.info(
                "Discovery advanced on the existing quote cohort; reused historical verification and portfolio replay"
            )
            return cached

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
        logger.info("Multi Factor diagnostics: reusing cached result")
    else:
        logger.info("Multi Factor diagnostics: fitting model")
        history = _load_daily_history(
            store, covered_symbols, start=warmup_start.isoformat(), end=cohort_end.isoformat(),
        )
        from .factor_research import train_factor_model

        factor_report = train_factor_model(history, min_universe=MIN_COVERED_UNIVERSE)
        _atomic_json(factor_path, {"fingerprint": fingerprint, "report": factor_report})
        timings["factor_model"] = {
            "seconds": round(perf_counter() - factor_started, 3), "reused": False,
        }
        logger.info(
            "Multi Factor diagnostics finished in %.1fs",
            timings["factor_model"]["seconds"],
        )

    if args.factor_only:
        validation_started = perf_counter()
        from .forward_validation import run_forward_validation_cycle

        forward_validation = run_forward_validation_cycle(
            discovery=adaptive_discovery, verification=adaptive_verification,
            store=store, database=database, symbols=covered_symbols,
            end=cohort_end, data_version=data_version, allow_stage=False,
            invocation_id=invocation_id, ledger_path=ledger_path,
        )
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
            logger.info("Portfolio replay: reusing cached result")
        else:
            logger.info("Portfolio replay starting for %d active strategies", len(LIVE_RESEARCH_STRATEGIES))
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
            logger.info("Portfolio replay finished in %.1fs", perf_counter() - portfolio_started)
        timings["portfolio_replay"] = {
            "seconds": 0.0 if portfolio_reused else round(perf_counter() - portfolio_started, 3),
            "reused": portfolio_reused,
        }
    else:
        logger.warning("Portfolio replay skipped: verification artifacts are incomplete")
        timings["portfolio_replay"] = {
            "seconds": 0.0, "status": "skipped",
            "reason": "Adaptive verification is incomplete; portfolio replay was skipped.",
        }

    factor_ic = (factor_report.get("adaptive_prequential_ic") or {}).get("mean_spearman_ic")
    validation_started = perf_counter()
    from .forward_validation import run_forward_validation_cycle

    forward_validation = run_forward_validation_cycle(
        discovery=adaptive_discovery, verification=adaptive_verification,
        store=store, database=database, symbols=covered_symbols,
        end=cohort_end, data_version=data_version,
        invocation_id=invocation_id, ledger_path=ledger_path,
    )
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
        "portfolio_coordinator_replay": portfolio_report,
        "portfolio_summary": (portfolio_report or {}).get("fold_summary"),
        "recommendation": {
            "adaptive_research": (
                "Dynamic discovery and rolling out-of-sample verification cover the active strategy set."
                if adaptive_portfolio_ready
                else "Adaptive verification is incomplete; portfolio replay was skipped."
            ),
            "multi_factor": (
                "Multi Factor is active through the shared live coordinator; regime-specific candidates can update signal parameters after historical and forward simulation gates pass."
                if factor_ic is not None and factor_ic > 0
                else "Multi Factor is active through the shared live coordinator; latest factor diagnostics were not positive, and any regime-specific candidate still needs to pass historical and forward simulation gates."
            ),
            "adaptive_parameter_promotion_requires_human_confirmation": False,
            "forward_validation": (
                "Promotion is automatic after the historical gates and the minimum sample of positive forward simulated results versus the current profile."
            ),
        },
        "orchestration": {**context, "cache_hit": False},
        "stage_timings": {**timings, "total_seconds": round(perf_counter() - total_started, 3)},
        "limitations": [
            "Adaptive fitting, verification, and fold-aligned portfolio replay use 2024 onward; earlier bars are indicator warm-up only.",
            "The fixed current Algo universe is used across the 2024-onward research range.",
            "The portfolio replay includes only strategies active in live config and available historical overlays.",
            "Each walk-forward fold begins with fresh capital; positions and risk state do not carry between folds.",
            "Forward trial replays use only sessions after candidate staging and modeled execution costs; results are not a performance forecast.",
            *([] if adaptive_portfolio_ready else [
                "Adaptive verification did not produce coordinator artifacts for every active strategy; the portfolio replay was skipped.",
            ]),
        ],
    }
    report["run_date_et"] = datetime.now(ET).date().isoformat()
    report = _attach_fitness_timeline(report, Path(args.output_dir))
    _atomic_json(report_path, report)
    _atomic_json(root / "latest.json", report)
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
    args = build_parser().parse_args(argv)
    output_dir = Path(args.output_dir)
    invocation_id = uuid4().hex
    ledger_path = output_dir / "research.sqlite3"
    ledger = None
    args.invocation_id = invocation_id
    args.ledger_path = str(ledger_path)
    log_path = _configure_progress_logging(output_dir)
    today = datetime.now(ET).date().isoformat()
    logger.info(
        "AutoResearch starting (workers=%d, refresh=%s, factor_only=%s, force=%s)",
        args.workers, not args.no_refresh, args.factor_only, args.force,
    )
    if log_path is not None:
        logger.info("Progress log: %s", log_path.resolve())
    try:
        ledger = ResearchLedger(ledger_path)
        ledger.start_invocation(invocation_id, request={
            "workers": args.workers, "no_refresh": args.no_refresh,
            "factor_only": args.factor_only, "force": args.force,
        })
        with _research_lock(Path(output_dir) / "locks" / "nightly-layers.lock"):
            ledger.import_legacy(output_dir)
            report = run_daily_research(args)
            report["run_date_et"] = today
            report["workers_requested"] = args.workers
    except Exception as exc:
        logger.error("AutoResearch failed: %s: %s", type(exc).__name__, exc)
        report = {
            "schema": "swarm_algo_nightly_research_v2",
            "invocation_id": invocation_id,
            "status": "error", "run_date_et": today,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "run_id": f"error-{datetime.now(ET).strftime('%Y%m%dT%H%M%S')}",
            "research_only": True, "live_configuration_changed": False,
            "trade_or_order_actions": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }
        report = _attach_fitness_timeline(report, output_dir)
        print(json.dumps(report, sort_keys=True), file=sys.stderr)
        invocation_report_path = output_dir / "invocations" / invocation_id / "report.json"
        report["invocation_report_path"] = str(invocation_report_path.resolve())
        _atomic_json(invocation_report_path, report)
        if ledger is not None:
            ledger.record_completed_report(
                invocation_id, report, report_path=str(invocation_report_path.resolve()),
            )
        _atomic_json(output_dir / f"{today}.json", report)
        _atomic_json(output_dir / "latest.json", report)
        return 2

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
    if ledger is not None:
        ledger.record_completed_report(
            invocation_id, report, report_path=str(invocation_report_path.resolve()),
        )
    _atomic_json(output_dir / f"{today}.json", report)
    _atomic_json(output_dir / "latest.json", report)
    discovery_report = ((report.get("adaptive_research") or {}).get("discovery") or {})
    print(json.dumps({
        "status": report["status"],
        "report": str((output_dir / "latest.json").resolve()),
        "run_id": report.get("orchestration", {}).get("run_id"),
        "range": [
            report["data"]["strategy_research_range"]["start"],
            report["data"]["strategy_research_range"]["end"],
        ],
        "stored_coverage": report["data"].get("stored_coverage"),
        "strategies": len(report.get("adaptive_strategy_artifacts", {})),
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
    }, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
