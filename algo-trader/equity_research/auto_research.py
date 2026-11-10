"""Scheduled adaptive Algo research orchestration and CLI."""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
from pathlib import Path
import sys
from time import perf_counter
from uuid import uuid4

from portfolio.config import ALGO_FACTOR_SECTOR_BY_SYMBOL, get_algo_runtime_settings
from equity_research.cpu import available_cpu_count
from portfolio.config import ALGO_REGIME_WARMUP_CALENDAR_DAYS
from market.store import DEFAULT_DB, SQLiteBarStore, fetch_alpaca_bars
from strategies.regime_features import NON_EQUITY_CONTEXT_SYMBOLS
from strategies.registry import refresh_active_parameter_profile
from market.data import data_tier, regular_feed

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
    EQUITY_LOG_PATH,
    MIN_COVERED_UNIVERSE, RESEARCH_START, RESEARCH_WARMUP_DAYS,
    ADJUSTMENT, DEFAULT_OUTPUT_DIR, ET, FEED, RESEARCHABLE_STRATEGIES,
)

AUTO_RESEARCH_PIPELINE_VERSION = "algo-research-layers-v23-continuous-champion-queue-2026"
logger = logging.getLogger(__name__)


def _refresh_incremental(store, *, selected_symbols, end_date, minimum_covered=MIN_COVERED_UNIVERSE):
    """Refresh the configured universe through the AutoResearch data adapter."""
    return _refresh_incremental_impl(
        store, selected_symbols=selected_symbols, end_date=end_date,
        data_tier_fn=data_tier, regular_feed_fn=regular_feed,
        fetch_bars_fn=fetch_alpaca_bars, minimum_covered=minimum_covered,
    )






def _settings_replay_job(kwargs: dict) -> dict:
    """One settings replay, runnable in a worker process. `label` names it in progress events."""
    from .validation.portfolio_simulation import run_settings_replay

    kwargs = dict(kwargs)
    label = kwargs.pop("label", "replay")
    os.environ["ALGO_REPLAY_LABEL"] = label
    return run_settings_replay(**kwargs)


def _run_replays_in_parallel(jobs: dict[str, dict]) -> dict[str, dict]:
    """Run independent settings replays in separate processes; results keyed by job name."""
    import multiprocessing as mp
    from concurrent.futures import ProcessPoolExecutor

    with ProcessPoolExecutor(max_workers=len(jobs), mp_context=mp.get_context("spawn")) as pool:
        futures = {name: pool.submit(_settings_replay_job, kwargs) for name, kwargs in jobs.items()}
        return {name: future.result() for name, future in futures.items()}


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
    if data_tier() != "algo_trader_plus" or regular_feed() != FEED:
        raise ValueError("Algo AutoResearch is restricted to Plus SIP data")

    if getattr(args, "underlying_list", None):
        symbols = list(args.underlying_list)
        universe_meta = {"source": "lab_override", "symbols": symbols}
    else:
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
            minimum_covered=1 if getattr(args, "underlying_list", None) else MIN_COVERED_UNIVERSE,
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

    # A lab run with its own universe may research a single ticker. The strategies are per-ticker signals; the
    # cross-sectional factor stages need a wider universe and report that themselves.
    minimum_universe = 1 if getattr(args, "underlying_list", None) else MIN_COVERED_UNIVERSE
    history_start, latest_end, missing = _latest_dates(store, symbols, minimum=minimum_universe)
    covered_symbols = [symbol for symbol in symbols if symbol not in missing]
    if len(covered_symbols) < minimum_universe:
        raise ValueError(
            f"Only {len(covered_symbols)} selected equities have common SIP history; "
            f"at least {minimum_universe} are required"
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

    cohort_end = latest_end
    if getattr(args, "end_date", None) and args.end_date < latest_end:
        cohort_end = args.end_date
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
    live_snapshot = live_configuration_manifest([*RESEARCHABLE_STRATEGIES, "swing_multi_factor", "swing_llm_driven"])
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
        "strategies": list(RESEARCHABLE_STRATEGIES),
        "live_configuration_sha256": live_snapshot["sha256"],
        "portfolio_strategies": list(RESEARCHABLE_STRATEGIES),
        "swing_multi_factor_enabled": runtime_settings.swing_multi_factor_enabled,
        "swing_llm_driven_enabled": runtime_settings.swing_llm_driven_enabled,
        "factor_only": bool(args.factor_only),
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
    if len(covered_symbols) < factor_model_min_universe:
        factor_report = {
            "status": "skipped",
            "reason": (
                f"{len(covered_symbols)} covered symbols; the cross-sectional factor model needs "
                f"{factor_model_min_universe}"
            ),
            "weight_timeline": [],
        }
        factor_model_timing = {"seconds": 0.0, "reused": False}
        logger.info("Multi Factor diagnostics skipped: %s", factor_report["reason"])
    elif not args.force and factor_cached and factor_cached.get("fingerprint") == factor_fingerprint:
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

    # The research is a replay now: the effective parameters over the chosen history. Nothing is searched,
    # verified or promoted here; the nightly checks live settings and the lab tests profiles the user picks.
    replay_kwargs = dict(
        database=database, output_dir=output_dir, symbols=covered_symbols,
        start=RESEARCH_START, end=cohort_end, strategies=list(RESEARCHABLE_STRATEGIES),
    )
    # A lab run also replays the live settings over the same window, so the chosen parameters can be compared
    # with them. The two replays are independent, so they run in separate processes.
    if os.environ.get("ALGO_LAB_PARAMETER_OVERRIDES"):
        from strategies.registry import live_parameters_snapshot

        replays = _run_replays_in_parallel({
            "chosen": {**replay_kwargs, "label": "chosen"},
            "live": {**replay_kwargs, "label": "live", "parameters_by_strategy": live_parameters_snapshot()},
        })
        settings_report = replays["chosen"]
        live = replays["live"]
        settings_report["live_comparison"] = {
            "summary": live.get("summary"),
            "strategy_breakdown": live.get("strategy_breakdown"),
            "parameters": live.get("parameters"),
        }
    else:
        settings_report = _settings_replay_job({**replay_kwargs, "label": "chosen" if os.environ.get("ALGO_LAB_PROGRESS") == "1" else "nightly"})
    settings_report["invocation_id"] = invocation_id
    settings_report["run_date_et"] = datetime.now(ET).date().isoformat()
    settings_report["created_at"] = datetime.now(timezone.utc).isoformat()
    # The archive row reads the portfolio return from here.
    summary = settings_report.get("summary") or {}
    settings_report["fitness_timeline"] = {"current": {"portfolio": {
        "status": "completed",
        "compounded_fold_return_pct": summary.get("net_return_pct"),
        "closed_trades": summary.get("closed_trade_count"),
        "max_drawdown_pct": summary.get("max_drawdown_pct"),
    }}}
    return settings_report






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
    parser.add_argument(
        "--verification-history", choices=["auto", "recent", "full"], default="auto",
        help="auto: full-history review on Sunday (ET), recent window otherwise",
    )
    parser.add_argument("--force", action="store_true", help="create a fresh data cohort and recompute cached research stages")
    parser.add_argument("--invocation-id", help="stable research ID used to resume an interrupted cycle")
    parser.add_argument(
        "--strategies", default="",
        help="comma-separated strategy ids to research (default: every researchable strategy)",
    )
    parser.add_argument("--underlyings", default="", help="comma-separated tickers to research instead of the live-eligible universe")
    parser.add_argument("--start", default="", help="first research session (YYYY-MM-DD), not earlier than the default research start")
    parser.add_argument("--end", default="", help="last research session (YYYY-MM-DD), capped at the latest stored session")
    parser.add_argument(
        "--lab-run", action="store_true",
        help="isolated run from the research lab: writes only under --output-dir, never the nightly or weekly reports",
    )
    return parser


LAB_EVENT_PREFIX = "LAB_EVENT "


def _lab_emit(event: dict) -> None:
    """Print a progress line the research lab reads. Options runs use the same format."""
    print(LAB_EVENT_PREFIX + json.dumps(event, sort_keys=True), flush=True)


def _apply_strategy_scope(args, parser) -> None:
    """Narrow the researched strategies to --strategies. This module and the adaptive pipeline each hold their own copy of the tuple, so both are rebound."""
    global RESEARCHABLE_STRATEGIES, RESEARCH_START
    requested = [item.strip() for item in (args.strategies or "").split(",") if item.strip()]
    if requested:
        unknown = sorted(set(requested) - set(RESEARCHABLE_STRATEGIES))
        if unknown:
            parser.error(f"unknown strategies: {', '.join(unknown)}")
        RESEARCHABLE_STRATEGIES = tuple(name for name in RESEARCHABLE_STRATEGIES if name in requested)
    if args.start:
        start = _parse_run_date(args.start, parser, "--start")
        if start < RESEARCH_START:
            parser.error(f"--start must be on or after {RESEARCH_START.isoformat()}")
        RESEARCH_START = start
    if args.end:
        args.end_date = _parse_run_date(args.end, parser, "--end")
        if args.end_date <= RESEARCH_START:
            parser.error("--end must be after the research start")
    else:
        args.end_date = None
    tickers = [item.strip().upper() for item in (args.underlyings or "").split(",") if item.strip()]
    bad = [item for item in tickers if not re.fullmatch(r"[A-Z][A-Z0-9.]{0,9}", item)]
    if bad:
        parser.error(f"not ticker symbols: {', '.join(bad)}")
    args.underlying_list = tickers


def _parse_run_date(text: str, parser, flag: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        parser.error(f"{flag} must be a date as YYYY-MM-DD")


def _configure_progress_logging(output_dir: Path, *, lab_run: bool = False) -> Path | None:
    """Log to the terminal and a bounded file so a running job can be tailed."""
    handlers = [logging.StreamHandler()]
    log_path = output_dir / "equity.log" if lab_run else EQUITY_LOG_PATH
    file_error = None
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(
            log_path, maxBytes=5_000_000, backupCount=5, encoding="utf-8",
        ))
    except OSError as exc:
        file_error = exc
        log_path = None
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [research] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )
    if file_error is not None:
        logger.warning("File progress log unavailable; console logging remains enabled: %s", file_error)
    return log_path


def _record_failure(report: dict, *, output_dir: Path, latest_path: Path, today, optimization_only: bool) -> None:
    """Record a failed run without replacing the last good report.

    The failure goes to latest-error.json. It fills latest.json or the day's
    file only when no good report exists yet.
    """
    error_path = (output_dir / "optimizer" / "latest-error.json") if optimization_only else (output_dir / "latest-error.json")
    _atomic_json(error_path, report)
    if not latest_path.exists():
        _atomic_json(latest_path, report)
    day_path = output_dir / f"{today}.json"
    if not optimization_only and not day_path.exists():
        _atomic_json(day_path, report)


def equity_simulation_context(report: dict) -> dict:
    """The compact equity simulation the Telegram summary reads: KPIs and per-strategy results of the replay."""
    summary = report.get("summary") or {}
    breakdown = (report.get("strategy_breakdown") or {}).get("by_strategy") or {}
    by_strategy = [
        {
            "strategy": name, "closed_trades": item.get("closed_trades"), "pnl_usd": item.get("pnl_usd"),
            "win_rate_pct": item.get("win_rate_pct"), "profit_factor": item.get("profit_factor"),
            "average_trade_usd": item.get("average_trade_usd"),
        }
        for name, item in sorted(breakdown.items())
    ]
    window = report.get("window") or {}
    universe = report.get("universe") or {}
    context = {
        "schema": "swarm_algo_equity_simulation_v1",
        "title": "Equity simulation",
        "run_date_et": report.get("run_date_et"),
        "status": report.get("status"),
        "window": {"start": window.get("start"), "end": window.get("end")},
        "universe_symbols": universe.get("symbol_count"),
        "summary": {key: summary.get(key) for key in (
            "net_return_pct", "benchmark_net_return_pct", "excess_return_vs_buy_hold_pct_points",
            "max_drawdown_pct", "closed_trade_count", "entry_count", "average_entries_per_session", "open_position_count",
        )},
        "by_strategy": [item for item in by_strategy if item.get("closed_trades")],
        "research_only": True,
    }
    live = report.get("live_comparison")
    if live:
        context["live_comparison"] = {
            key: (live.get("summary") or {}).get(key) for key in ("net_return_pct", "closed_trade_count", "max_drawdown_pct")
        }
    return context


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _apply_strategy_scope(args, parser)
    if args.optimization_only and args.factor_only:
        parser.error("--optimization-only cannot be combined with --factor-only")
    output_dir = Path(args.output_dir)
    latest_report_path = output_dir / "latest.json"
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
            "fold", "folds_total", "fold_status", "train_start", "train_end",
            "test_start", "test_end", "fold_elapsed_seconds",
            "sensitivity_elapsed_seconds", "sensitivity_status", "selected_candidate",
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

    if getattr(args, "lab_run", False):
        # Replays inside a lab run print progress events; worker processes inherit the switch.
        os.environ["ALGO_LAB_PROGRESS"] = "1"
        # Lab runs report each strategy as an underlying, so the lab's progress and results read the same way as options.
        started_strategies = set()

        def lab_progress(*, stage, status, payload=None, strategy=None, _inner=progress_callback):
            if strategy:
                if strategy not in started_strategies:
                    started_strategies.add(strategy)
                    _lab_emit({"event": "underlying_started", "underlying": strategy, "index": len(started_strategies), "total": len(RESEARCHABLE_STRATEGIES)})
                if status == "error":
                    _lab_emit({"event": "underlying_error", "underlying": strategy, "failure": str((payload or {}).get("error") or "error")})
                elif stage == "verification" and status in ("completed", "reused"):
                    _lab_emit({"event": "underlying_done", "underlying": strategy, "status": status, "approved": False})
            _inner(stage=stage, status=status, payload=payload, strategy=strategy)

        _lab_emit({"event": "started", "strategy": "equity", "total": len(RESEARCHABLE_STRATEGIES), "underlyings": list(RESEARCHABLE_STRATEGIES)})
        args.progress_callback = lab_progress
    else:
        args.progress_callback = progress_callback
    log_path = _configure_progress_logging(output_dir, lab_run=args.lab_run)
    today = datetime.now(ET).date().isoformat()
    logger.info(
        "AutoResearch starting: invocation_id=%s pid=%d started_at=%s argv=%s "
        "workers=%d refresh=%s optimization_only=%s factor_only=%s force=%s",
        invocation_id, os.getpid(), datetime.now().astimezone().isoformat(), sys.argv,
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
                logger.warning("Marked %d abandoned research invocations as interrupted", interrupted)
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
        logger.exception("AutoResearch failed: invocation_id=%s %s: %s", invocation_id, type(exc).__name__, exc)
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
            # Keyed by the invocation, so a failure is traceable to the run that wrote it.
            "run_id": f"error-{invocation_id}",
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
        _record_failure(report, output_dir=output_dir, latest_path=latest_report_path, today=today, optimization_only=args.optimization_only)
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
    if report.get("schema") == "swarm_algo_weekly_verification_v1":
        # The weekly full-history review has no nightly sections. It gets its own folder, so the day's archive row is not replaced.
        _atomic_json(output_dir / "weekly" / f"{today}.json", report)
    elif report.get("status") == "error":
        _record_failure(report, output_dir=output_dir, latest_path=latest_report_path, today=today, optimization_only=args.optimization_only)
    else:
        if not args.optimization_only:
            _atomic_json(output_dir / f"{today}.json", report)
        _atomic_json(latest_report_path, report)
        if not args.lab_run and report.get("schema") == "swarm_algo_settings_replay_v1":
            # The Telegram equity summary reads this file (see ops/systemd/equity-research.service).
            _atomic_json(output_dir / "latest-prompt.json", equity_simulation_context(report))
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
    data_summary = report.get("data") or {}
    if not isinstance(data_summary, dict):
        data_summary = {}
    research_range = data_summary.get("strategy_research_range") or {}
    if not isinstance(research_range, dict):
        research_range = {}
    print(json.dumps(_json_safe({
        "status": report["status"],
        "report": str(latest_report_path.resolve()),
        "run_id": (report.get("orchestration") or {}).get("run_id"),
        "range": (
            [research_range["start"], research_range["end"]]
            if "start" in research_range and "end" in research_range else None
        ),
        "stored_coverage": data_summary.get("stored_coverage"),
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
        "cache_hit": (report.get("orchestration") or {}).get("cache_hit", False),
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
