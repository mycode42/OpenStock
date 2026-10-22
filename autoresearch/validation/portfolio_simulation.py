"""Fold-aligned portfolio-coordinator replay of independent strategy artifacts."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import multiprocessing as mp
from pathlib import Path
import sys

from autoresearch.backtest import (
    DAILY_WARMUP_DAYS, AlgoPortfolioBacktest, multi_factor_warmup_days,
    read_borrow_availability, strategy_warmup_days,
)
from autoresearch.backtest.config import REGIME_WARMUP_DAYS
from autoresearch.backtest.execution import build_replay_timeline
from portfolio.config import ALGO_REGIME_CONFIG
from market.store import SQLiteBarStore
from autoresearch.cpu import available_cpu_count
from strategies.catalog import STRATEGY_CATALOG
from strategies.event_driven import EventCalendar
from strategies.multi_factor.features import aggregate_intraday_bars
from portfolio.config import get_algo_runtime_settings

from ..artifacts.live_manifest import live_configuration_manifest
from ..artifacts.replay_logging import quiet_coordinator_rejections
from .robustness import (
    bootstrap_daily_equity, bootstrap_trade_sequence,
    simulate_ewma_allocation_shadow,
)
from ..settings import (
    LIVE_RESEARCH_STRATEGIES, RESEARCHABLE_STRATEGIES, FEED, ADJUSTMENT,
    ROBUSTNESS_BOOTSTRAP_BLOCK_SESSIONS, ROBUSTNESS_BOOTSTRAP_ITERATIONS,
    ROBUSTNESS_COST_MULTIPLIERS, REGIME_CONTEXT_WARMUP_DAYS,
)


ADAPTIVE_COORDINATOR_SCHEMA = "swarm_algo_adaptive_coordinator_v1"


def run_coordinated_portfolio(args) -> dict:
    """Combine each strategy's training-selected profile, then replay its shared OOS folds."""
    runtime_settings = get_algo_runtime_settings()
    strategies = _parse_strategies(args.strategies)
    if args.multi_factor_overlay:
        if "multi_factor" not in strategies:
            strategies.append("multi_factor")
    else:
        strategies = [strategy for strategy in strategies if strategy != "multi_factor"]
    if not strategies:
        raise ValueError("At least one strategy must remain enabled for the portfolio replay")
    disabled_intraday = [
        strategy for strategy in strategies
        if (
            (runtime_setting := STRATEGY_CATALOG[strategy].runtime_setting)
            and not getattr(runtime_settings, runtime_setting)
        )
    ]
    if disabled_intraday and not args.enable_disabled_intraday:
        raise ValueError(
            "Selected intraday strategy is disabled by live config: "
            + ", ".join(disabled_intraday)
            + ". Pass --enable-disabled-intraday to evaluate it in this research replay only."
        )
    runtime_overrides = {strategy: True for strategy in disabled_intraday}
    artifact_root = Path(args.artifact_dir)
    artifacts = {}
    for strategy in strategies:
        path = artifact_root / strategy / "latest.json"
        if not path.is_file():
            raise ValueError(f"Missing adaptive verification artifact for {strategy}: {path}")
        artifacts[strategy] = _read_artifact(path, strategy)

    common = _validate_artifacts(artifacts)
    folds = common["folds"]
    symbols = common["symbols"]
    db_paths = {artifact["source"]["database"] for artifact in artifacts.values()}
    if len(db_paths) != 1:
        raise ValueError("Adaptive verification artifacts must use the same market database")
    db = next(iter(db_paths))
    if not Path(db).is_file():
        raise ValueError(f"Market database recorded by artifacts does not exist: {db}")

    event_requested = runtime_settings.algo_event_driven_enabled
    event_report = {"status": "disabled_by_live_config"}
    event_path = None
    if args.event_calendar:
        event_path = str(Path(args.event_calendar).resolve())
        if not Path(event_path).is_file():
            raise ValueError(f"Historical event calendar does not exist: {event_path}")
        event_calendar = EventCalendar.from_csv(event_path)
        event_report = {"status": "loaded_from_argument", **event_calendar.diagnostics()}
    elif event_requested:
        # The runtime config enables Event Driven by default. Build a point-in-
        # time historical calendar for the same interval instead of silently
        # dropping that live confirmation layer from the portfolio twin.
        from ..data import _prepare_event_calendar

        event_calendar, event_report = _prepare_event_calendar(
            start=date.fromisoformat(common["start"]),
            end=date.fromisoformat(common["end"]), db=db,
            output_dir=Path(args.output_dir),
        )
        if event_calendar is not None:
            event_path = event_report.get("calendar_path")
    event_enabled = event_requested and bool(event_path)
    enabled_strategies = list(strategies)
    if event_enabled:
        enabled_strategies.append("event_driven")

    fold_profiles = []
    for index in range(len(folds)):
        parameters = {}
        controls = None
        selected_profiles = {}
        for strategy, artifact in artifacts.items():
            selected = artifact["verification"]["folds"][index]
            selected_params = selected.get("selected_parameters") or {}
            if set(selected_params) != {strategy}:
                raise ValueError(
                    f"Fold {index + 1} in {strategy} artifact must contain only that strategy's parameters"
                )
            parameters[strategy] = selected_params[strategy]
            candidate_controls = selected.get("selected_controls") or {}
            if controls is None:
                controls = candidate_controls
            elif candidate_controls != controls:
                raise ValueError("Strategy artifacts have different portfolio research controls")
            selected_profiles[strategy] = selected.get("selected_profile")

        parameters["satellite_overlay"] = {
            "multi_factor_enabled": bool(args.multi_factor_overlay),
            "event_driven_enabled": event_enabled,
        }
        parameters["event_driven"] = {"enabled": event_enabled}
        fold_profiles.append({
            "index": index,
            "window": {key: folds[index][key] for key in (
                "train_start", "train_end", "test_start", "test_end",
            )},
            "profile": parameters,
            "controls": controls or {},
            "selected_profiles": selected_profiles,
        })

    cost_models = [
        artifact["verification"].get("transaction_cost_model", {})
        for artifact in artifacts.values()
    ]
    if any(model != cost_models[0] for model in cost_models[1:]):
        raise ValueError("Adaptive verification artifacts use different transaction-cost models")
    cost_model = cost_models[0]
    borrow_paths = {
        artifact["source"].get("borrow_availability") for artifact in artifacts.values()
    }
    if len(borrow_paths) != 1:
        raise ValueError("Adaptive verification artifacts use different borrow-availability inputs")
    borrow_path = next(iter(borrow_paths))
    if borrow_path and not Path(borrow_path).is_file():
        raise ValueError(f"Borrow-availability source recorded by artifacts does not exist: {borrow_path}")

    worker_count = min(max(1, args.workers), len(fold_profiles), available_cpu_count())
    factor_weight_timeline = list(getattr(args, "factor_weight_timeline", []) or [])
    jobs = [
        (
            item, symbols, db, event_path, enabled_strategies,
            bool(args.multi_factor_overlay), cost_model, borrow_path, runtime_overrides,
            0, True, factor_weight_timeline,
        )
        for item in fold_profiles
    ]
    scenario_definitions = {}
    for multiplier in ROBUSTNESS_COST_MULTIPLIERS:
        scenario_cost = dict(cost_model)
        scenario_cost["slippage_bps_each_side"] = float(
            cost_model.get("slippage_bps_each_side", 5)
        ) * multiplier
        scenario_cost["spread_bps"] = float(cost_model.get("spread_bps", 3)) * multiplier
        scenario_cost["commission_per_share_each_side"] = float(
            cost_model.get("commission_per_share_each_side", 0.005)
        ) * multiplier
        scenario_cost["short_borrow_annual_pct"] = float(
            cost_model.get("short_borrow_annual_pct", 3)
        ) * multiplier
        scenario_cost["unknown_borrow_adverse_fill_penalty_bps"] = float(
            cost_model.get("unknown_borrow_adverse_fill_penalty_bps", 25)
        ) * multiplier
        scenario_name = f"cost_{multiplier:g}x"
        scenario_definitions[scenario_name] = {
            "jobs": [
                (
                    item, symbols, db, event_path, enabled_strategies,
                    bool(args.multi_factor_overlay), scenario_cost, borrow_path,
                    runtime_overrides, 0, False, factor_weight_timeline,
                )
                for item in fold_profiles
            ],
            "cost_multiplier": multiplier,
            "execution_delay_bars": 0,
            "cost_model": scenario_cost,
        }

    latency_multiplier = max(ROBUSTNESS_COST_MULTIPLIERS, default=1.5)
    latency_cost = dict(cost_model)
    latency_cost["slippage_bps_each_side"] = float(
        cost_model.get("slippage_bps_each_side", 5)
    ) * latency_multiplier
    latency_cost["spread_bps"] = float(cost_model.get("spread_bps", 3)) * latency_multiplier
    latency_cost["commission_per_share_each_side"] = float(
        cost_model.get("commission_per_share_each_side", 0.005)
    ) * latency_multiplier
    latency_cost["short_borrow_annual_pct"] = float(
        cost_model.get("short_borrow_annual_pct", 3)
    ) * latency_multiplier
    latency_cost["unknown_borrow_adverse_fill_penalty_bps"] = float(
        cost_model.get("unknown_borrow_adverse_fill_penalty_bps", 25)
    ) * latency_multiplier
    latency_name = f"cost_{latency_multiplier:g}x_delay_1_bar"
    scenario_definitions[latency_name] = {
        "jobs": [
            (
                item, symbols, db, event_path, enabled_strategies,
                bool(args.multi_factor_overlay), latency_cost, borrow_path,
                runtime_overrides, 1, False, factor_weight_timeline,
            )
            for item in fold_profiles
        ],
        "cost_multiplier": latency_multiplier,
        "execution_delay_bars": 1,
        "cost_model": latency_cost,
    }

    fold_job_groups = []
    for index, baseline_job in enumerate(jobs):
        entries = [("baseline", baseline_job)]
        entries.extend(
            (name, definition["jobs"][index])
            for name, definition in scenario_definitions.items()
        )
        fold_job_groups.append((index, entries))
    grouped_results = _execute_fold_job_groups(
        fold_job_groups, worker_count=worker_count,
    )
    results = [grouped_results[index]["baseline"] for index in range(len(jobs))]
    fold_returns = [item["net_return_pct"] for item in results]
    compound_return = 1.0
    for value in fold_returns:
        compound_return *= 1.0 + value / 100.0
    compound_return = (compound_return - 1.0) * 100.0
    initial_capital = float(getattr(args, "capital", 100_000.0))
    stitched_curve = []
    stitched_strategy_pnl = []
    stitched_trade_pnls = []
    stitched_equity = initial_capital
    previous_date = None
    for item in results:
        scale = stitched_equity / initial_capital
        regime_by_day = {
            str(row.get("date")): str(row.get("regime") or "unknown")
            for row in item.get("daily_equity", [])
        }
        for row in item.get("daily_equity", []):
            day = row.get("date")
            if previous_date is not None and day <= previous_date:
                continue
            value = float(row.get("equity", 0) or 0) * scale
            if value > 0:
                stitched_curve.append({"date": day, "equity": round(value, 2)})
                previous_date = day
        for row in item.get("daily_strategy_pnl", []):
            stitched_strategy_pnl.append({
                "date": row.get("date"),
                "regime": regime_by_day.get(str(row.get("date")), "unknown"),
                "pnl_by_strategy": {
                    strategy: round(float(value or 0.0) * scale, 4)
                    for strategy, value in (row.get("pnl_by_strategy") or {}).items()
                    if strategy in enabled_strategies
                },
            })
        stitched_trade_pnls.extend(
            float(value or 0.0) * scale
            for value in item.get("closed_trade_pnls", [])
        )
        stitched_equity *= 1.0 + float(item.get("net_return_pct") or 0.0) / 100.0
    monte_carlo = bootstrap_daily_equity(
        stitched_curve,
        iterations=int(getattr(args, "bootstrap_iterations", ROBUSTNESS_BOOTSTRAP_ITERATIONS)),
        block_size=int(getattr(args, "bootstrap_block_sessions", ROBUSTNESS_BOOTSTRAP_BLOCK_SESSIONS)),
        seed=int(getattr(args, "seed", 41)),
    )
    trade_monte_carlo = bootstrap_trade_sequence(
        stitched_trade_pnls, initial_capital=initial_capital,
        iterations=int(getattr(args, "bootstrap_iterations", ROBUSTNESS_BOOTSTRAP_ITERATIONS)),
        block_size=int(getattr(args, "bootstrap_trade_block_size", ROBUSTNESS_BOOTSTRAP_BLOCK_SESSIONS)),
        seed=int(getattr(args, "seed", 41)) + 1,
    )
    coordinator_shadow = simulate_ewma_allocation_shadow(
        stitched_strategy_pnl,
        initial_capital=initial_capital,
    )
    sensitivity_scenarios = {}
    for scenario_name, definition in scenario_definitions.items():
        scenario_results = [
            grouped_results[index][scenario_name]
            for index in range(len(fold_profiles))
        ]
        sensitivity_scenarios[scenario_name] = {
            **_summarize_cost_sensitivity(scenario_results),
            "cost_multiplier": definition["cost_multiplier"],
            "execution_delay_bars": definition["execution_delay_bars"],
            "cost_model": definition["cost_model"],
        }

    return {
        "schema": "swarm_algo_coordinated_portfolio_v1",
        "stage": "portfolio_coordinator_simulation",
        "research_only": True,
        "live_configuration_changed": False,
        "trade_or_order_actions": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "data": {
            "database": str(Path(db).resolve()),
            "tier": artifacts[strategies[0]]["verification"].get("data_tier_at_run"),
            "feed": common["feed"], "adjustment": common["adjustment"],
            "start": common["start"], "end": common["end"],
            "training_history_start": common.get("training_history_start", common["start"]),
            "out_of_sample_range": common.get("out_of_sample_range"),
            "symbols": symbols,
        },
        "strategies": strategies,
        "strategy_artifacts": {
            strategy: {
                "path": artifact["artifact_path"],
                "sha256": artifact["sha256"],
                "promotion_gate_passed": bool(
                    artifact["verification"].get("promotion_gate", {}).get("promotable")
                ),
            }
            for strategy, artifact in artifacts.items()
        },
        "selection_policy": (
            "For each shared fold, each strategy contributes only the profile selected on that strategy's "
            "training window. The combined portfolio coordinator is evaluated on the following "
            "shared test window."
        ),
        "satellite_overlays": {
            "multi_factor_enabled": bool(args.multi_factor_overlay),
            "event_driven_enabled": event_enabled,
            "event_calendar": event_path,
            "event_calendar_report": event_report,
            "note": "Multi Factor is a core strategy in this replay; Event Driven remains a confirmation overlay.",
        },
        "core_strategies": {"multi_factor_enabled": bool(args.multi_factor_overlay)},
        "live_configuration": live_configuration_manifest(enabled_strategies),
        "research_runtime_overrides": runtime_overrides,
        "factor_weight_timeline": {
            "observation_count": len(factor_weight_timeline),
            "sha256": hashlib.sha256(json.dumps(
                factor_weight_timeline, sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest(),
        },
        "worker_count": worker_count,
        "fold_summary": {
            "count": len(results),
            "positive_out_of_sample_folds": sum(value > 0 for value in fold_returns),
            "mean_out_of_sample_return_pct": round(sum(fold_returns) / len(fold_returns), 4),
            "compounded_fold_return_pct": round(compound_return, 4),
            "total_closed_trades": sum(item.get("closed_trade_count", 0) for item in results),
            "mean_max_drawdown_pct": round(
                sum(item["max_drawdown_pct"] for item in results) / len(results), 4,
            ),
        },
        "robustness": {
            "portfolio_daily_block_bootstrap": monte_carlo,
            "closed_trade_sequence_bootstrap": trade_monte_carlo,
            "coordinator_weight_shadow": coordinator_shadow,
            "cost_model_sensitivity": {
                "base_model": cost_model,
                "scenarios": sensitivity_scenarios,
                "sensitivity_policy": "slippage, spread, per-share commission, short-borrow rate, and unknown-borrow penalty are scaled together",
            },
        },
        "folds": results,
        "limitations": [
            "Each baseline fold starts with fresh capital and no positions or risk state carry over between folds; the shadow weight state carries across the stitched dates.",
            "The universe is fixed to the symbol list shared by the adaptive verification artifacts.",
            "The coordinator allocation shadow scales each strategy's after-cost daily sleeve P&L linearly; it does not replay changed fills, capital competition, or shared-capacity interactions.",
            "A strategy's promotion gate is reported but does not suppress research-only "
            "portfolio simulation.",
            "The final artifact does not alter live settings or authorize deployment.",
            *(["Event Driven was enabled by the live setting but omitted because a point-in-time "
               "historical event calendar could not be built."]
              if event_requested and not event_enabled else []),
        ],
    }


def _prepare_coordinated_fold_inputs(job: tuple) -> dict:
    (
        item, symbols, db, event_path, strategies, factor_enabled, _,
        borrow_path, _,
    ) = job[:9]
    fold = item["window"]
    start = date.fromisoformat(fold["test_start"])
    end = date.fromisoformat(fold["test_end"])
    warmup_days = max(
        REGIME_CONTEXT_WARMUP_DAYS, DAILY_WARMUP_DAYS,
        *(strategy_warmup_days(strategy, item["profile"]) for strategy in strategies),
        multi_factor_warmup_days(item["profile"]) if factor_enabled else 0,
    )
    bars_start = (start - timedelta(days=warmup_days)).isoformat()
    store = SQLiteBarStore(db)
    context_symbols = list(dict.fromkeys([*symbols, "SPY", "QQQ", "GLD", "TLT", "HYG", "LQD"]))
    intraday = store.bars(
        context_symbols, feed=FEED, timeframe="5Min", start=bars_start,
        end=end.isoformat(), adjustment=ADJUSTMENT,
    )
    available = [symbol for symbol in symbols if intraday.get(symbol)]
    if not available:
        raise ValueError(f"No stored {FEED} {ADJUSTMENT}-adjusted bars in test fold {start}..{end}")
    missing = [symbol for symbol in symbols if symbol not in available]
    spy_daily = store.bars(
        ["SPY"], feed=FEED, timeframe="1Day",
        start=(start - timedelta(days=REGIME_WARMUP_DAYS)).isoformat(),
        end=end.isoformat(), adjustment=ADJUSTMENT,
    ).get("SPY", [])
    from market.regime import load_fred_regime_series

    macro_series, macro_diagnostics = load_fred_regime_series(store, refresh=False)
    calendar = EventCalendar.from_csv(event_path) if event_path else None
    return {
        "start": start, "end": end, "intraday": intraday, "spy_daily": spy_daily,
        "missing": missing, "macro_series": macro_series,
        "macro_diagnostics": macro_diagnostics, "calendar": calendar,
        "borrow_availability": read_borrow_availability(borrow_path),
        "preaggregated_daily_bars": aggregate_intraday_bars(intraday),
        "precomputed_timeline": build_replay_timeline(
            symbols, intraday, start, end,
        ),
    }


def _run_coordinated_fold_group(group: tuple) -> tuple[int, dict[str, dict]]:
    """Replay a fold's robustness scenarios against one prepared market snapshot."""
    index, entries = group
    prepared = _prepare_coordinated_fold_inputs(entries[0][1])
    regime_cache = {}
    replays = {}
    for name, job in entries:
        _, summary = _run_coordinated_fold(
            job, prepared_inputs=prepared, regime_cache=regime_cache,
        )
        replays[name] = summary
    return index, replays


def _run_coordinated_fold(
    job: tuple, *, prepared_inputs: dict | None = None,
    regime_cache: dict[date, dict] | None = None,
) -> tuple[int, dict]:
    (
        item, symbols, db, event_path, strategies, factor_enabled, cost_model,
        borrow_path, runtime_overrides,
    ) = job[:9]
    execution_delay_bars = job[9] if len(job) > 9 else 0
    include_curves = job[10] if len(job) > 10 else True
    factor_weight_timeline = job[11] if len(job) > 11 else []
    prepared_inputs = prepared_inputs or _prepare_coordinated_fold_inputs(job)
    start = prepared_inputs["start"]
    end = prepared_inputs["end"]
    intraday = prepared_inputs["intraday"]
    spy_daily = prepared_inputs["spy_daily"]
    missing = prepared_inputs["missing"]
    macro_series = prepared_inputs["macro_series"]
    macro_diagnostics = prepared_inputs["macro_diagnostics"]
    calendar = prepared_inputs["calendar"]
    replay = AlgoPortfolioBacktest(
        symbols=symbols, intraday_bars=intraday, spy_daily_bars=spy_daily,
        start=start.isoformat(), end=end.isoformat(), feed=FEED,
        data_adjustment=ADJUSTMENT,
        slippage_bps=cost_model.get("slippage_bps_each_side", 5),
        spread_bps=cost_model.get("spread_bps", 3),
        commission_per_share=cost_model.get("commission_per_share_each_side", 0.005),
        short_borrow_annual_pct=cost_model.get("short_borrow_annual_pct", 3) / 100,
        unknown_borrow_penalty_bps=cost_model.get("unknown_borrow_adverse_fill_penalty_bps", 25),
        borrow_availability=prepared_inputs["borrow_availability"],
        execution_delay_bars=execution_delay_bars,
        regime_macro_series=macro_series,
        regime_config={
            **ALGO_REGIME_CONFIG,
            "macro_data_status": macro_diagnostics.get("status"),
        },
        factor_weight_timeline=factor_weight_timeline,
        profile=item["profile"], strategies=strategies,
        research_controls=item["controls"], event_calendar=calendar,
        research_runtime_overrides=runtime_overrides,
        preaggregated_daily_bars=prepared_inputs["preaggregated_daily_bars"],
        precomputed_timeline=prepared_inputs["precomputed_timeline"],
        regime_cache=regime_cache,
    )
    with quiet_coordinator_rejections():
        report = replay.run()
    selected_gates = report.get("portfolio_coordinator", {})
    keys = (
        "start", "end", "net_return_pct", "max_drawdown_pct", "closed_trade_count",
        "open_position_count", "entry_count", "average_entries_per_session",
        "benchmark_net_return_pct", "excess_return_vs_buy_hold_pct_points",
        "entry_count_by_strategy", "signal_event_count_by_strategy",
        "satellite_confirmation_candidates", "satellite_confirmed_entries",
        "portfolio_coordinator", "strategy_correlation_report",
        "daily_equity", "daily_strategy_pnl",
        "closed_trades",
    )
    summary = {key: report.get(key) for key in keys}
    summary["closed_trade_pnls"] = [
        float(trade.get("pnl") or 0.0) for trade in report.get("closed_trades", [])
    ]
    summary.pop("closed_trades", None)
    summary["execution_delay_bars"] = execution_delay_bars
    if not include_curves:
        summary.pop("daily_equity", None)
        summary.pop("daily_strategy_pnl", None)
        summary.pop("closed_trade_pnls", None)
    summary.update({
        "fold": item["window"],
        "selected_profiles": item["selected_profiles"],
        "selected_parameters": {
            strategy: item["profile"][strategy] for strategy in item["selected_profiles"]
        },
        "symbols_without_bars_in_fold": missing,
        "portfolio_coordinator_active": bool(selected_gates),
        "research_runtime_overrides": runtime_overrides,
        "profile_sha256": hashlib.sha256(
            json.dumps(item["profile"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    })
    return item["index"], summary


def _execute_fold_jobs(jobs: list[tuple], *, worker_count: int, label: str) -> list[dict]:
    groups = [
        (index, [(label, job)])
        for index, job in enumerate(jobs)
    ]
    grouped = _execute_fold_job_groups(groups, worker_count=worker_count)
    return [grouped[index][label] for index in range(len(jobs))]


def _execute_fold_job_groups(groups: list[tuple], *, worker_count: int) -> dict[int, dict[str, dict]]:
    """Run each fold's baseline and robustness replays in one prepared worker."""
    results_by_index = {}
    if worker_count <= 1:
        for completed, group in enumerate(groups, start=1):
            index, replays = _run_coordinated_fold_group(group)
            results_by_index[index] = replays
            first_replay = next(iter(replays.values()))
            print(
                f"Completed coordinator fold {completed}/{len(groups)} "
                f"({first_replay['start']}..{first_replay['end']})",
                file=sys.stderr, flush=True,
            )
        return results_by_index

    # Research already initializes BLAS and logging threads before coordinator
    # replay. Forking that live interpreter can strand queue and library locks;
    # use clean workers, as discovery and verification do.
    with ProcessPoolExecutor(
        max_workers=worker_count, mp_context=mp.get_context("spawn"),
    ) as executor:
        futures = [executor.submit(_run_coordinated_fold_group, group) for group in groups]
        for completed, future in enumerate(as_completed(futures), start=1):
            index, replays = future.result()
            results_by_index[index] = replays
            first_replay = next(iter(replays.values()))
            print(
                f"Completed coordinator fold {completed}/{len(futures)} "
                f"({first_replay['start']}..{first_replay['end']})",
                file=sys.stderr, flush=True,
            )
    return results_by_index


def _summarize_cost_sensitivity(folds: list[dict]) -> dict:
    returns = [float(row.get("net_return_pct") or 0.0) for row in folds]
    drawdowns = [float(row.get("max_drawdown_pct") or 0.0) for row in folds]
    compounded = 1.0
    for value in returns:
        compounded *= 1.0 + value / 100.0
    return {
        "fold_count": len(folds),
        "mean_oos_return_pct": round(sum(returns) / len(returns), 4) if returns else None,
        "compounded_oos_return_pct": round((compounded - 1.0) * 100.0, 4),
        "worst_oos_return_pct": round(min(returns), 4) if returns else None,
        "mean_max_drawdown_pct": round(sum(drawdowns) / len(drawdowns), 4) if drawdowns else None,
        "worst_max_drawdown_pct": round(max(drawdowns), 4) if drawdowns else None,
        "folds": [
            {
                "fold": row.get("fold"), "net_return_pct": row.get("net_return_pct"),
                "max_drawdown_pct": row.get("max_drawdown_pct"),
                "closed_trade_count": row.get("closed_trade_count"),
                "execution_delay_bars": row.get("execution_delay_bars", 0),
            }
            for row in folds
        ],
    }


def _read_artifact(path: Path, strategy: str) -> dict:
    try:
        raw = path.read_bytes()
        artifact = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read strategy artifact {path}: {exc}") from exc
    if not (
        artifact.get("schema") == ADAPTIVE_COORDINATOR_SCHEMA
        and artifact.get("stage") == "adaptive_verification_coordinator"
        and artifact.get("strategy") == strategy
    ):
        raise ValueError(f"Unsupported adaptive verification artifact for {strategy}: {path}")

    verification = artifact.get("verification")
    if not isinstance(verification, dict) or len(verification.get("folds", [])) < 3:
        raise ValueError(f"Adaptive verification artifact has fewer than three folds: {path}")
    source = artifact.get("source")
    if not isinstance(source, dict) or not source.get("database") or not source.get("symbols"):
        raise ValueError(f"Adaptive verification artifact is missing its source data manifest: {path}")
    return {
        **artifact,
        "artifact_path": str(path.resolve()),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _validate_artifacts(artifacts: dict[str, dict]) -> dict:
    if not artifacts:
        raise ValueError("At least one adaptive verification artifact is required")
    first_strategy = next(iter(artifacts))
    first = artifacts[first_strategy]["verification"]
    common = {
        "start": first.get("start"), "end": first.get("end"),
        "training_history_start": first.get("training_history_start", first.get("start")),
        "out_of_sample_range": first.get("out_of_sample_range"),
        "feed": first.get("feed"), "adjustment": first.get("data_adjustment"),
        "symbols": first.get("symbols"), "folds": first.get("folds"),
    }
    expected_windows = [
        tuple(fold.get(key) for key in ("train_start", "train_end", "test_start", "test_end"))
        for fold in common["folds"]
    ]
    if not expected_windows or any(any(value is None for value in window) for window in expected_windows):
        raise ValueError("Adaptive verification artifact contains an incomplete fold window")
    for strategy, artifact in artifacts.items():
        report = artifact["verification"]
        for key, report_key in (
            ("start", "start"), ("end", "end"), ("feed", "feed"),
            ("adjustment", "data_adjustment"),
        ):
            if report.get(report_key) != common[key]:
                raise ValueError(f"{strategy} adaptive verification artifact has a different {key}")
        if report.get("symbols") != common["symbols"]:
            raise ValueError(f"{strategy} adaptive verification artifact has a different symbol universe")
        if report.get("training_history_start", report.get("start")) != common["training_history_start"]:
            raise ValueError(f"{strategy} adaptive verification artifact has a different training history start")
        if report.get("out_of_sample_range") != common["out_of_sample_range"]:
            raise ValueError(f"{strategy} adaptive verification artifact has a different OOS range")
        windows = [
            tuple(fold.get(key) for key in ("train_start", "train_end", "test_start", "test_end"))
            for fold in report.get("folds", [])
        ]
        if windows != expected_windows:
            raise ValueError(f"{strategy} adaptive verification folds do not align with the other strategies")
        if report.get("data_tier_at_run") != first.get("data_tier_at_run"):
            raise ValueError(f"{strategy} adaptive verification artifact has a different data tier")
    return common


def _parse_strategies(value: str | None) -> list[str]:
    strategies = (
        list(LIVE_RESEARCH_STRATEGIES) if not value
        else list(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))
    )
    if not strategies or any(strategy not in RESEARCHABLE_STRATEGIES for strategy in strategies):
        raise ValueError(f"Strategies must be selected from: {', '.join(RESEARCHABLE_STRATEGIES)}")
    return strategies
