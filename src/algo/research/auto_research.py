"""Scheduled research-only refinement for Algo strategies and factor weights.

The runner may refresh historical SIP bars and write dated research reports. It
cannot modify live strategy settings or route trades. OpenClaw delivers the
resulting report to Telegram in a separate, read-only reporting job.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import contextmanager
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile
from zoneinfo import ZoneInfo

from src.algo.config import get_algo_research_controls
from src.algo.backtest import (
    DAILY_WARMUP_DAYS, AlgoPortfolioBacktest, _regular_session_bars,
    multi_factor_warmup_days,
)
from src.algo.factor_research import train_factor_model
from src.algo.market_store import DEFAULT_DB, SQLiteBarStore, fetch_alpaca_bars
from src.strategies.event_driven import EventCalendar
from src.strategies.satellite_overlay import SatelliteOverlayConfig
from src.market_data import data_tier, regular_feed
from src.strategies.multi_factor.features import aggregate_intraday_bars
from src.strategies.registry import ALGO_DEFAULT_PARAMETERS
from src.strategies.universe import configured_universe


ROOT = Path(__file__).resolve().parents[2]
ET = ZoneInfo("America/New_York")
DEFAULT_PROFILES = ROOT / "autoresearch" / "algo-daily-profiles.json"
DEFAULT_OUTPUT_DIR = ROOT / "data" / "research" / "algo-auto"
LOCK_FILE = ROOT / "data" / "locks" / "algo-auto-research.lock"
FEED = "sip"
ADJUSTMENT = "split"
MIN_COVERED_UNIVERSE = 20
INITIAL_HISTORY_DATE = date(2015, 1, 1)
CORE_STRATEGIES = (
    "mean_reversion", "trend_following", "momentum", "opening_range_breakout",
    "swing_mean_reversion",
)
ISOLATED_MAX_CANDIDATE_TICKER_DAYS_PER_SESSION = 3
COMBINED_MAX_CANDIDATE_TICKER_DAYS_PER_SESSION = 15
CORE_RESEARCH_CONTROLS = get_algo_research_controls()


def _parse_date(value: str) -> date:
    return date.fromisoformat(str(value)[:10])


def load_algo_universe() -> tuple[list[str], dict]:
    """Use the same curated fixed Algo symbols as live trading."""
    universe = configured_universe("algo")
    symbols = universe["tickers"]
    if not symbols:
        raise ValueError("Configured Algo universe is empty")
    return symbols, {
        "universe_source": "configured_fixed",
    }


def _bar_coverage(store: SQLiteBarStore) -> dict[str, dict]:
    return {
        row["symbol"]: row for row in store.coverage()
        if row["feed"] == FEED and row["adjustment"] == ADJUSTMENT and row["timeframe"] == "5Min"
    }


def _refresh_incremental(
    store: SQLiteBarStore,
    *,
    selected_symbols: list[str],
    end_date: date,
) -> dict:
    """Fetch missing recent Plus SIP bars and the SPY daily session index."""
    if data_tier() != "algo_trader_plus" or regular_feed() != FEED:
        raise ValueError("Algo AutoResearch requires the Algo Trader Plus SIP feed")
    coverage = _bar_coverage(store)
    known = [symbol for symbol in selected_symbols if symbol in coverage]
    missing_history = sorted(set(selected_symbols) - set(known))
    if len(known) < MIN_COVERED_UNIVERSE:
        raise ValueError(
            f"Only {len(known)} selected tickers have stored split-adjusted SIP history; "
            f"at least {MIN_COVERED_UNIVERSE} are required"
        )

    groups: dict[date, list[str]] = defaultdict(list)
    for symbol in selected_symbols:
        last_date = _parse_date(coverage[symbol]["last"]) if symbol in coverage else INITIAL_HISTORY_DATE - timedelta(days=1)
        request_start = last_date + timedelta(days=1)
        if request_start <= end_date:
            groups[request_start].append(symbol)
    rows_written = 0
    requests_made = 0
    for start_date, symbols in sorted(groups.items()):
        request_start = f"{start_date.isoformat()}T00:00:00Z"
        request_end = f"{end_date.isoformat()}T23:59:59Z"
        bars = fetch_alpaca_bars(
            symbols, timeframe="5Min", start=request_start, end=request_end,
            feed=FEED, adjustment=ADJUSTMENT, mode="algo",
        )
        bars, _, _ = _regular_session_bars(bars)
        rows_written += store.write_bars(
            bars, feed=FEED, timeframe="5Min", adjustment=ADJUSTMENT,
            data_tier="algo_trader_plus", requested_start=request_start,
            requested_end=request_end,
        )
        requests_made += 1

    spy_daily = next((row for row in store.coverage() if (
        row["feed"] == FEED and row["adjustment"] == ADJUSTMENT
        and row["timeframe"] == "1Day" and row["symbol"] == "SPY"
    )), None)
    daily_rows_written = 0
    if spy_daily is None:
        daily_start = "2015-01-01"
    else:
        last_daily = _parse_date(spy_daily["last"])
        daily_start = (last_daily + timedelta(days=1)).isoformat()
    if _parse_date(daily_start) <= end_date:
        request_start = f"{daily_start}T00:00:00Z"
        request_end = f"{end_date.isoformat()}T23:59:59Z"
        daily_bars = fetch_alpaca_bars(
            ["SPY"], timeframe="1Day", start=request_start, end=request_end,
            feed=FEED, adjustment=ADJUSTMENT, mode="algo",
        )
        daily_rows_written = store.write_bars(
            daily_bars, feed=FEED, timeframe="1Day", adjustment=ADJUSTMENT,
            data_tier="algo_trader_plus", requested_start=request_start,
            requested_end=request_end,
        )
    return {
        "feed": FEED, "adjustment": ADJUSTMENT, "requested_through": end_date.isoformat(),
        "symbols_refreshed": sum(len(values) for values in groups.values()),
        "missing_selected_history": missing_history,
        "request_batches": requests_made,
        "intraday_rows_written": rows_written,
        "spy_daily_rows_written": daily_rows_written,
    }


def _load_daily_history(
    store: SQLiteBarStore,
    symbols: list[str],
    *,
    start: str,
    end: str,
) -> dict[str, list[dict]]:
    """Aggregate one ticker at a time to keep 5-minute data memory bounded."""
    output = {}
    for symbol in dict.fromkeys(symbols):
        intraday = store.bars(
            [symbol], feed=FEED, timeframe="5Min", start=start, end=end,
            adjustment=ADJUSTMENT,
        )
        output[symbol] = aggregate_intraday_bars(intraday).get(symbol, [])
        del intraday
    spy_daily = store.bars(
        ["SPY"], feed=FEED, timeframe="1Day", start=start, end=end,
        adjustment=ADJUSTMENT,
    ).get("SPY", [])
    output["SPY"] = [{"t": bar["t"], "c": bar["c"]} for bar in spy_daily]
    return output


def _latest_dates(store: SQLiteBarStore, symbols: list[str]) -> tuple[date, date, list[str]]:
    coverage = _bar_coverage(store)
    available = [symbol for symbol in symbols if symbol in coverage]
    missing = sorted(set(symbols) - set(available))
    if len(available) < MIN_COVERED_UNIVERSE:
        raise ValueError(f"Only {len(available)} fixed Algo-universe tickers have SIP bars")
    # Keep the earliest date in the local store. Recently listed members simply
    # begin contributing once their own history exists.
    start = min(_parse_date(coverage[symbol]["first"]) for symbol in available)
    end = min(_parse_date(coverage[symbol]["last"]) for symbol in available)
    spy_daily = next((row for row in store.coverage() if (
        row["feed"] == FEED and row["adjustment"] == ADJUSTMENT
        and row["timeframe"] == "1Day" and row["symbol"] == "SPY"
    )), None)
    if spy_daily is None:
        raise ValueError("SPY split-adjusted SIP daily history is missing")
    end = min(end, _parse_date(spy_daily["last"]))
    start = max(start, _parse_date(spy_daily["first"]))
    return start, end, missing


def _summarize_backtest(report: dict) -> dict:
    keys = (
        "start", "end", "net_return_pct", "max_drawdown_pct", "closed_trade_count",
        "entry_count", "average_entries_per_session", "time_in_market_pct",
        "entry_count_by_strategy", "satellite_confirmed_entries",
        "event_strategy_diagnostics", "multi_factor_diagnostics",
    )
    return {key: report.get(key) for key in keys}


def _summarize_walk_forward(report: dict, *, artifact: Path) -> dict:
    folds = report.get("folds", [])
    selected_counts: dict[str, int] = defaultdict(int)
    for fold in folds:
        selected_counts[str(fold.get("selected_profile", "unknown"))] += 1
    latest = folds[-1] if folds else {}
    latest_candidates = []
    for candidate in latest.get("training_candidates", []):
        metrics = candidate.get("metrics", {})
        parameters = candidate.get("parameters", {})
        target_strategy = next((
            name for name in parameters
            if name in {"mean_reversion", "momentum", "opening_range_breakout", "trend_following"}
        ), None)
        satellites = candidate.get("enabled_satellites", [])
        candidate_component = target_strategy or (
            satellites[0] if len(satellites) == 1
            else "combined_satellites" if len(satellites) > 1
            else "core_baseline"
        )
        latest_candidates.append({
            "name": candidate.get("name"), "eligible": candidate.get("eligible"),
            "score": candidate.get("score"),
            "parameters": parameters,
            "training_return_pct": metrics.get("net_return_pct"),
            "training_drawdown_pct": metrics.get("max_drawdown_pct"),
            "training_closed_trades": metrics.get("closed_trade_count"),
            "candidate_ticker_days_per_session": metrics.get("average_candidate_ticker_days_per_session"),
            "enabled_satellites": candidate.get("enabled_satellites", []),
            "candidate_component": candidate_component,
            "satellite_sample_eligible": candidate.get("satellite_sample_eligible"),
            "satellite_confirmed_entries": metrics.get("satellite_confirmed_entries", {}),
            "out_of_sample_metrics": fold.get("out_of_sample") if candidate.get("name") == fold.get("selected_profile") else None,
            "target_strategy": target_strategy,
            "target_strategy_training_metrics": (
                metrics.get("strategy_metrics", {}).get(target_strategy)
                if target_strategy else None
            ),
        })
    baseline_metrics = next((
        item.get("metrics", {}).get("strategy_metrics", {})
        for item in latest.get("training_candidates", []) if item.get("name") == "baseline"
    ), {})
    per_strategy = {}
    for item in latest_candidates:
        strategy = item.get("target_strategy")
        if not strategy:
            continue
        baseline = baseline_metrics.get(strategy)
        candidate = item.get("target_strategy_training_metrics")
        if not baseline or not candidate:
            continue
        baseline_pnl = baseline.get("average_trade_pnl")
        candidate_pnl = candidate.get("average_trade_pnl")
        per_strategy[strategy] = {
            "candidate_profile": item["name"],
            "baseline_training_metrics": baseline,
            "candidate_training_metrics": candidate,
            "average_trade_pnl_delta": (
                round(candidate_pnl - baseline_pnl, 2)
                if isinstance(candidate_pnl, (int, float)) and isinstance(baseline_pnl, (int, float))
                else None
            ),
            "training_trade_count_gate_met": candidate.get("closed_trade_count", 0) >= 10,
        }
    return {
        "artifact": str(artifact.resolve()),
        "start": report.get("start"), "end": report.get("end"),
        "feed": report.get("feed"), "adjustment": report.get("data_adjustment"),
        "symbols": report.get("symbols"), "worker_count": report.get("worker_count"),
        "event_calendar": report.get("event_calendar"),
        "candidate_profile_count": report.get("candidate_profile_count"),
        "fold_summary": report.get("fold_summary"),
        "promotion_gate": report.get("promotion_gate"),
        "transaction_cost_model": report.get("transaction_cost_model"),
        "selected_profile_fold_counts": dict(selected_counts),
        "satellite_sample_gate": report.get("candidate_frequency_target", {}).get(
            "minimum_confirmed_entries_per_enabled_satellite"
        ),
        "latest_training_selection": {
            "train_start": latest.get("train_start"), "train_end": latest.get("train_end"),
            "test_start": latest.get("test_start"), "test_end": latest.get("test_end"),
            "selected_profile": latest.get("selected_profile"),
            "selected_parameters": latest.get("selected_parameters"),
            "selected_controls": latest.get("selected_controls"),
            "selected_reason": latest.get("selected_reason"),
            "out_of_sample": latest.get("out_of_sample"),
            "training_candidates": latest_candidates,
            "strategy_specific_training_comparisons": per_strategy,
        },
        "limitations": report.get("limitations", []),
    }


def _isolated_direction_profiles(strategy: str, direction: str) -> list[dict]:
    """Keep each fit to one core strategy and one entry direction."""
    if strategy not in CORE_STRATEGIES or direction not in {"long", "short"}:
        raise ValueError("isolated optimization requires a core strategy and long/short direction")
    allow = {"allow_long": direction == "long", "allow_short": direction == "short"}
    if strategy == "mean_reversion":
        variants = [
            ("live_opening_dip", dict(ALGO_DEFAULT_PARAMETERS[strategy])),
            ("patient_opening_dip", {
                "signal_model": "opening_dip", "minimum_dip_pct": 0.018,
                "minimum_rip_pct": 0.018, "minimum_bounce_pct": 0.0025,
                "minimum_fade_pct": 0.0025, "minimum_volume_ratio": 1.4,
                "entry_window_minutes": 45, "higher_low_bars": 1, "lower_high_bars": 1,
            }),
            ("bollinger_reversal", {
                "signal_model": "bollinger", "window": 20, "band_width": 2.0,
                "entry_rsi": 30, "exit_rsi": 58,
                "short_entry_rsi": 68, "short_exit_rsi": 42,
            }),
        ]
    elif strategy == "trend_following":
        variants = [
            ("live_daily_sma_20_50", dict(ALGO_DEFAULT_PARAMETERS[strategy])),
            ("daily_ema_20_50", {
                "average_type": "ema", "fast_period": 20, "slow_period": 50,
                "minimum_slope_pct": 0.002,
            }),
            ("daily_sma_20_60_strict_slope", {
                "average_type": "sma", "fast_period": 20, "slow_period": 60,
                "minimum_slope_pct": 0.004,
            }),
        ]
    elif strategy == "momentum":
        variants = [
            ("live_daily_5_10_20", dict(ALGO_DEFAULT_PARAMETERS[strategy])),
            ("daily_5_10_rank2", {
                "lookbacks": [5, 10], "top_n": 2,
                "minimum_score_spread": 0.03,
            }),
            ("patient_daily_10_20_rank2", {
                "lookbacks": [10, 20], "top_n": 2,
                "minimum_score_spread": 0.05,
            }),
        ]
    elif strategy == "swing_mean_reversion":
        variants = [
            ("live_cyclical_6pct", dict(ALGO_DEFAULT_PARAMETERS[strategy])),
            ("persistent_8pct", {"stretch_pct": 0.08, "minimum_stretch_days": 2}),
            ("patient_8pct_3days", {"stretch_pct": 0.08, "minimum_stretch_days": 3}),
        ]
    else:
        variants = [
            ("live_30_minute_range", dict(ALGO_DEFAULT_PARAMETERS[strategy])),
            ("confirmed_30_minute_range", {
                "opening_range_bars": 6, "post_range_delay_bars": 2,
                "minimum_volume_ratio": 1.5, "breakout_buffer_pct": 0.001,
            }),
            ("strict_30_minute_range", {
                "opening_range_bars": 6, "post_range_delay_bars": 3,
                "minimum_volume_ratio": 2.5, "breakout_buffer_pct": 0.0025,
            }),
        ]

    profiles = []
    for variant, parameters in variants:
        if strategy in {"trend_following", "momentum", "swing_mean_reversion"}:
            parameters = {**parameters, "direction_mode": f"{direction}_only"}
        else:
            parameters = {**parameters, **allow}
        profiles.append({
            "name": "baseline" if variant.startswith("live_") else f"{strategy}_{direction}_{variant}",
            "strategies": [strategy],
            "parameters": {strategy: parameters},
            "controls": CORE_RESEARCH_CONTROLS,
        })
    return profiles


def _run_isolated_direction_job(payload: tuple) -> tuple[str, dict]:
    """Run one strategy/direction candidate bank in a single fold worker."""
    strategy, direction, symbols, start, end, db, profiles_path, artifact_path = payload
    from src.algo.walk_forward import build_parser as build_walk_forward_parser, run_walk_forward

    argv = [
        "--symbols", ",".join(symbols), "--start", start, "--end", end,
        "--profiles", str(profiles_path), "--feed", FEED, "--adjustment", ADJUSTMENT,
        "--train-days", "756", "--test-days", "63", "--step-days", "63",
        "--min-train-trades", "10", "--max-train-drawdown-pct", "12",
        "--max-candidate-ticker-days-per-session", str(ISOLATED_MAX_CANDIDATE_TICKER_DAYS_PER_SESSION),
        "--workers", "1", "--db", str(db),
    ]
    args = build_walk_forward_parser().parse_args(argv)
    report = run_walk_forward(args)
    _atomic_json(Path(artifact_path), report)
    return f"{strategy}.{direction}", _summarize_walk_forward(report, artifact=Path(artifact_path))


def _run_isolated_core_research(
    *, symbols: list[str], start: date, end: date, db: str, output_dir: Path,
    run_date: date, workers: int,
) -> dict:
    """Tune every core side alone before running the combined-portfolio WFA."""
    from src.algo.walk_forward import _read_profiles

    output_dir.mkdir(parents=True, exist_ok=True)
    temporary_profiles: list[Path] = []
    jobs = []
    for strategy in CORE_STRATEGIES:
        for direction in ("long", "short"):
            handle = tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", prefix=f"{strategy}-{direction}-",
                suffix=".json", dir=output_dir, delete=False,
            )
            profile_path = Path(handle.name)
            temporary_profiles.append(profile_path)
            profiles = _isolated_direction_profiles(strategy, direction)
            json.dump(profiles, handle, indent=2)
            handle.write("\n")
            handle.close()
            # Validate the generated candidates before launching worker processes.
            _read_profiles(str(profile_path))
            artifact = output_dir / f"{run_date.isoformat()}-{strategy}-{direction}-walk-forward.json"
            jobs.append((strategy, direction, symbols, start.isoformat(), end.isoformat(), db, profile_path, artifact))

    workers_used = min(max(1, workers), len(jobs), os.cpu_count() or 1)
    results = {}
    try:
        with ProcessPoolExecutor(max_workers=workers_used) as executor:
            futures = [executor.submit(_run_isolated_direction_job, job) for job in jobs]
            for completed, future in enumerate(as_completed(futures), start=1):
                key, report = future.result()
                results[key] = report
                print(
                    f"Completed isolated Algo optimization {completed}/{len(jobs)}: {key}",
                    file=sys.stderr, flush=True,
                )
    finally:
        for profile_path in temporary_profiles:
            profile_path.unlink(missing_ok=True)

    ordered_results = {
        f"{strategy}.{direction}": results[f"{strategy}.{direction}"]
        for strategy in CORE_STRATEGIES for direction in ("long", "short")
    }
    return {
        "status": "completed",
        "strategy_count": len(CORE_STRATEGIES),
        "direction_count": 2,
        "candidate_profiles_per_strategy_direction": 3,
        "worker_count": workers_used,
        "selection_policy": (
            "Each fit contains exactly one core strategy and one entry direction. "
            "Only training-window metrics select a profile; later sessions remain out of sample. "
            "No candidate changes live settings."
        ),
        "results": ordered_results,
    }


def _combined_portfolio_replay(
    store: SQLiteBarStore,
    *,
    symbols: list[str],
    start: date,
    end: date,
    event_calendar: EventCalendar | None,
) -> dict:
    """Compare the rule portfolio with its bounded Multi Factor/Event satellites."""
    if end < start:
        raise ValueError("combined replay end must not be before start")
    warmup_days = multi_factor_warmup_days({})
    bars_start = (start - timedelta(days=warmup_days)).isoformat()
    intraday = store.bars(
        symbols, feed=FEED, timeframe="5Min", start=bars_start,
        end=end.isoformat(), adjustment=ADJUSTMENT,
    )
    spy = store.bars(
        ["SPY"], feed=FEED, timeframe="1Day",
        start=(start - timedelta(days=DAILY_WARMUP_DAYS)).isoformat(),
        end=end.isoformat(), adjustment=ADJUSTMENT,
    ).get("SPY", [])
    results = {
        variant: _combined_replay_variant(
            symbols=symbols, start=start, end=end,
            intraday=intraday, spy=spy, event_calendar=event_calendar,
            variant=variant,
        )
        for variant in ("four_core_rules", "six_strategy_satellite_portfolio")
    }
    del intraday, spy
    return {
        "start": start.isoformat(), "end": end.isoformat(),
        "event_calendar_available": event_calendar is not None,
        "results": results,
        "comparison_note": (
            "The six-strategy variant keeps the four core rules as entry owners; Multi Factor and Event Driven only confirm and amplify aligned core entries."
        ),
    }


def _prepare_event_calendar(
    *, start: date, end: date, db: str, output_dir: Path,
) -> tuple[EventCalendar | None, dict]:
    from src.algo.historical_events import build_historical_event_calendar

    calendar_path = output_dir / "event-calendar-historical.csv"
    metadata_path = output_dir / "event-calendar-historical.json"
    try:
        report = build_historical_event_calendar(
            start=start, end=end, db=db, output=calendar_path,
        )
        _atomic_json(metadata_path, report)
        return EventCalendar.from_csv(calendar_path), report
    except Exception as exc:
        # A previously built calendar is safe to reuse only if it covers this
        # exact research interval; otherwise event profiles are omitted below.
        try:
            previous = json.loads(metadata_path.read_text())
            if (
                calendar_path.is_file()
                and date.fromisoformat(previous["range_start"]) <= start
                and date.fromisoformat(previous["range_end"]) >= end
            ):
                calendar = EventCalendar.from_csv(calendar_path)
                return calendar, {
                    **previous,
                    "status": "reused_cached_calendar_after_source_error",
                    "source_error": f"{type(exc).__name__}: {exc}",
                }
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None, {
            "status": "unavailable",
            "range_start": start.isoformat(), "range_end": end.isoformat(),
            "calendar_path": str(calendar_path.resolve()),
            "error": f"{type(exc).__name__}: {exc}",
        }


def _profiles_without_event_overlay(source: str | Path) -> tuple[Path, Path | None, list[str]]:
    """Return a temporary profile set retaining core and factor research."""
    path = Path(source)
    profiles = json.loads(path.read_text())
    retained = []
    omitted = []
    for profile in profiles:
        overlay = SatelliteOverlayConfig.from_dict(profile.get("parameters", {}).get("satellite_overlay"))
        if overlay.event_driven_enabled:
            omitted.append(profile["name"])
        else:
            retained.append(profile)
    if not omitted:
        return path, None, []
    if not any(profile.get("name") == "baseline" for profile in retained):
        raise ValueError("Cannot omit event profiles because no baseline profile remains")
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix="algo-profiles-no-events-",
        suffix=".json", dir=path.parent, delete=False,
    )
    temporary = Path(handle.name)
    try:
        json.dump(retained, handle, indent=2)
        handle.write("\n")
    finally:
        handle.close()
    return temporary, temporary, omitted


def _factor_portfolio_replay(
    store: SQLiteBarStore,
    *,
    symbols: list[str],
    start: date,
    end: date,
    weights: dict[str, float],
) -> dict:
    warmup_days = multi_factor_warmup_days({"multi_factor": {"factor_weights": weights}})
    bars_start = (start - timedelta(days=warmup_days)).isoformat()
    intraday = store.bars(
        symbols, feed=FEED, timeframe="5Min", start=bars_start,
        end=end.isoformat(), adjustment=ADJUSTMENT,
    )
    spy = store.bars(
        ["SPY"], feed=FEED, timeframe="1Day",
        start=(start - timedelta(days=180)).isoformat(), end=end.isoformat(),
        adjustment=ADJUSTMENT,
    ).get("SPY", [])
    result = AlgoPortfolioBacktest(
        symbols=symbols, intraday_bars=intraday, spy_daily_bars=spy,
        start=start.isoformat(), end=end.isoformat(), feed=FEED,
        data_adjustment=ADJUSTMENT, slippage_bps=5,
        profile={"multi_factor": {"factor_weights": weights}},
        strategies=["multi_factor"],
    ).run()
    del intraday, spy
    return _summarize_backtest(result)


def _combined_replay_variant(
    *,
    symbols: list[str],
    start: date,
    end: date,
    intraday: dict[str, list[dict]],
    spy: list[dict],
    event_calendar: EventCalendar | None,
    variant: str,
) -> dict:
    core = ["mean_reversion", "momentum", "opening_range_breakout", "trend_following"]
    if variant == "four_core_rules":
        strategies = core
        profile = {"satellite_overlay": {
            "multi_factor_enabled": False, "event_driven_enabled": False,
        }}
    elif variant == "six_strategy_satellite_portfolio":
        strategies = [*core, "multi_factor", "event_driven"]
        profile = {"satellite_overlay": {
            "multi_factor_enabled": True,
            "event_driven_enabled": event_calendar is not None,
        }, "event_driven": {"enabled": event_calendar is not None}}
    else:
        raise ValueError(f"Unknown combined replay variant: {variant}")

    report = AlgoPortfolioBacktest(
        symbols=symbols, intraday_bars=intraday, spy_daily_bars=spy,
        start=start.isoformat(), end=end.isoformat(), feed=FEED,
        data_adjustment=ADJUSTMENT, slippage_bps=5, profile=profile,
        strategies=strategies, event_calendar=event_calendar,
    ).run()
    return _summarize_backtest(report)


def _run_portfolio_replay_job(job: tuple) -> tuple[str, str, str, dict]:
    """Run one independent replay in a worker process."""
    kind, group, variant, payload = job
    store = payload["store"]
    symbols = payload["symbols"]
    start = payload["start"]
    end = payload["end"]
    if kind == "factor":
        report = _factor_portfolio_replay(
            store, symbols=symbols, start=start, end=end,
            weights=payload["weights"],
        )
        return kind, group, variant, report
    if kind != "combined":
        raise ValueError(f"Unknown replay job kind: {kind}")

    warmup_days = multi_factor_warmup_days({})
    bars_start = (start - timedelta(days=warmup_days)).isoformat()
    intraday = store.bars(
        symbols, feed=FEED, timeframe="5Min", start=bars_start,
        end=end.isoformat(), adjustment=ADJUSTMENT,
    )
    spy = store.bars(
        ["SPY"], feed=FEED, timeframe="1Day",
        start=(start - timedelta(days=DAILY_WARMUP_DAYS)).isoformat(),
        end=end.isoformat(), adjustment=ADJUSTMENT,
    ).get("SPY", [])
    report = _combined_replay_variant(
        symbols=symbols, start=start, end=end,
        intraday=intraday, spy=spy,
        event_calendar=payload["event_calendar"], variant=variant,
    )
    return kind, group, variant, report


def run_daily_research(args) -> dict:
    if data_tier() != "algo_trader_plus" or regular_feed() != FEED:
        raise ValueError("Algo AutoResearch is restricted to Plus SIP data")
    symbols, universe_meta = load_algo_universe()
    store = SQLiteBarStore(args.db)
    refresh_report = None
    if not args.no_refresh:
        now_et = datetime.now(ET)
        if now_et.weekday() < 5 and now_et.time() >= time(16, 10):
            refresh_to = now_et.date()
        else:
            refresh_to = now_et.date() - timedelta(days=1)
        refresh_report = _refresh_incremental(
            store, selected_symbols=symbols, end_date=refresh_to,
        )

    start, end, missing = _latest_dates(store, symbols)
    covered_symbols = [symbol for symbol in symbols if symbol not in missing]
    if (end - start).days < 1100:
        raise ValueError(f"Need at least three years of overlapping SIP bars; found {start}..{end}")
    latest_age_days = (datetime.now(ET).date() - end).days
    if not args.no_refresh and latest_age_days > 4:
        raise ValueError(
            f"Plus SIP data is stale after refresh: latest common session {end} "
            f"is {latest_age_days} calendar days old"
        )

    output_dir = Path(args.output_dir)
    if args.factor_only:
        event_calendar, event_calendar_report = None, {"status": "not_requested_factor_only_run"}
    else:
        event_calendar, event_calendar_report = _prepare_event_calendar(
            start=start, end=end, db=str(args.db), output_dir=output_dir,
        )

    history = _load_daily_history(
        store, covered_symbols, start=start.isoformat(), end=end.isoformat(),
    )
    factor_report = train_factor_model(history, min_universe=MIN_COVERED_UNIVERSE)
    factor_replays = {}
    combined_replays = {}
    replay_jobs = []
    if not args.factor_only:
        equal_weights = {name: 0.25 for name in factor_report["factor_names"]}
        frozen_weights = factor_report["frozen_2016_2023_weights"]
        cutoff_2025 = date(2025, 12, 31)
        # The model through 2025 is a separate forward vintage for the 2026 replay.
        if date(2024, 1, 1) <= end:
            holdout_end = min(end, cutoff_2025)
            group = "fixed_2024_2025_holdout"
            factor_replays[group] = {}
            for variant, weights in (
                ("baseline_equal_weights", equal_weights),
                ("weights_fit_through_2023", frozen_weights),
            ):
                replay_jobs.append(("factor", group, variant, {
                    "store": store, "symbols": covered_symbols,
                    "start": date(2024, 1, 1), "end": holdout_end,
                    "weights": weights,
                }))
            combined_replays[group] = {
                "start": date(2024, 1, 1).isoformat(),
                "end": holdout_end.isoformat(),
                "event_calendar_available": event_calendar is not None,
                "results": {},
                "comparison_note": (
                    "The six-strategy variant keeps the four core rules as entry owners; "
                    "Multi Factor and Event Driven only confirm and amplify aligned core entries."
                ),
            }
            for variant in ("four_core_rules", "six_strategy_satellite_portfolio"):
                replay_jobs.append(("combined", group, variant, {
                    "store": store, "symbols": covered_symbols,
                    "start": date(2024, 1, 1), "end": holdout_end,
                    "event_calendar": event_calendar,
                }))
        if date(2026, 1, 1) <= end:
            weights_2025 = factor_report.get("weights_fit_through_2025")
            if weights_2025:
                group = "forward_2026"
                factor_replays[group] = {}
                for variant, weights in (
                    ("baseline_equal_weights", equal_weights),
                    ("weights_fit_through_2025", weights_2025),
                ):
                    replay_jobs.append(("factor", group, variant, {
                        "store": store, "symbols": covered_symbols,
                        "start": date(2026, 1, 1), "end": end,
                        "weights": weights,
                    }))
                combined_replays[group] = {
                    "start": date(2026, 1, 1).isoformat(),
                    "end": end.isoformat(),
                    "event_calendar_available": event_calendar is not None,
                    "results": {},
                    "comparison_note": (
                        "The six-strategy variant keeps the four core rules as entry owners; "
                        "Multi Factor and Event Driven only confirm and amplify aligned core entries."
                    ),
                }
                for variant in ("four_core_rules", "six_strategy_satellite_portfolio"):
                    replay_jobs.append(("combined", group, variant, {
                        "store": store, "symbols": covered_symbols,
                        "start": date(2026, 1, 1), "end": end,
                        "event_calendar": event_calendar,
                    }))

    portfolio_replay_workers = 0
    if replay_jobs:
        portfolio_replay_workers = min(
            max(1, args.workers), len(replay_jobs), os.cpu_count() or 1,
        )
        with ProcessPoolExecutor(max_workers=portfolio_replay_workers) as executor:
            futures = [executor.submit(_run_portfolio_replay_job, job) for job in replay_jobs]
            for completed, future in enumerate(as_completed(futures), start=1):
                kind, group, variant, replay_report = future.result()
                target = factor_replays[group] if kind == "factor" else combined_replays[group]["results"]
                target[variant] = replay_report
                print(
                    f"Completed Algo portfolio replay {completed}/{len(futures)}: {group}.{variant}",
                    file=sys.stderr, flush=True,
                )

    isolated_walk_forward = None
    walk_forward = None
    omitted_event_profiles: list[str] = []
    if not args.factor_only:
        from src.algo.walk_forward import build_parser as build_walk_forward_parser, run_walk_forward

        isolated_walk_forward = _run_isolated_core_research(
            symbols=covered_symbols, start=start, end=end, db=str(args.db),
            output_dir=Path(args.output_dir), run_date=datetime.now(ET).date(),
            workers=args.workers,
        )

        wf_output = Path(args.output_dir) / f"{datetime.now(ET).date().isoformat()}-walk-forward.json"
        profiles_path, temporary_profiles, omitted_event_profiles = (
            (Path(args.profiles).resolve(), None, [])
            if event_calendar is not None
            else _profiles_without_event_overlay(args.profiles)
        )
        try:
            wf_argv = [
                "--symbols", ",".join(covered_symbols),
                "--start", start.isoformat(), "--end", end.isoformat(),
                "--profiles", str(profiles_path),
                "--feed", FEED, "--adjustment", ADJUSTMENT,
                "--train-days", "756", "--test-days", "63", "--step-days", "63",
                "--min-train-trades", "10", "--max-train-drawdown-pct", "12",
                "--min-satellite-confirmed-entries", "3",
                # The portfolio can surface several candidates from each
                # core strategy before global entry limits are applied.
                "--max-candidate-ticker-days-per-session",
                str(COMBINED_MAX_CANDIDATE_TICKER_DAYS_PER_SESSION),
                "--workers", str(args.workers), "--db", str(args.db),
            ]
            if event_calendar is not None:
                wf_argv.extend(["--event-calendar", str(Path(event_calendar_report["calendar_path"]).resolve())])
            wf_args = build_walk_forward_parser().parse_args(wf_argv)
            full_walk_forward = run_walk_forward(wf_args)
            wf_output.parent.mkdir(parents=True, exist_ok=True)
            wf_output.write_text(json.dumps(full_walk_forward, indent=2, sort_keys=True, default=str))
            walk_forward = _summarize_walk_forward(full_walk_forward, artifact=wf_output)
        finally:
            if temporary_profiles is not None:
                temporary_profiles.unlink(missing_ok=True)

    adaptive_ic = factor_report["adaptive_prequential_ic"].get("mean_spearman_ic")
    equal_ic = factor_report["adaptive_equal_weight_ic"].get("mean_spearman_ic")
    adaptive_samples = factor_report["adaptive_prequential_ic"].get("sample_count", 0)
    if (
        adaptive_samples >= 20 and adaptive_ic is not None and equal_ic is not None
        and adaptive_ic > 0 and adaptive_ic > equal_ic
    ):
        factor_review = "prequential_ic_passed_for_human_review"
    else:
        factor_review = "not_eligible_to_advance; retain equal weights pending stronger out-of-sample evidence"
    data_freshness = "fresh" if latest_age_days <= 4 else "stale"
    combined_gate = (walk_forward or {}).get("promotion_gate") or {}
    combined_gate_passed = bool(combined_gate.get("promotable"))
    isolated_gate_results = [
        result.get("promotion_gate", {})
        for result in (isolated_walk_forward or {}).get("results", {}).values()
    ]
    isolated_gate_pass_count = sum(bool(result.get("promotable")) for result in isolated_gate_results)
    if combined_gate_passed:
        rule_strategy_proposal = (
            "A single fixed profile passed the chronological after-cost promotion gate; owner review is still required."
        )
    elif walk_forward and walk_forward.get("folds"):
        rule_strategy_proposal = (
            "No fixed profile passed the chronological after-cost promotion gate; training selections remain research-only."
        )
    else:
        rule_strategy_proposal = "No rule strategy profile was eligible for review."
    if isolated_walk_forward and isolated_gate_pass_count:
        isolated_strategy_proposal = (
            f"{isolated_gate_pass_count} isolated strategy/direction candidates passed the promotion gate; "
            "review those only, then compare the combined portfolio."
        )
    elif isolated_walk_forward:
        isolated_strategy_proposal = (
            "No isolated strategy/direction candidate passed the promotion gate; keep all candidates research-only."
        )
    else:
        isolated_strategy_proposal = "No isolated core direction optimization was run."

    return {
        "status": "completed",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "research_only": True,
        "live_configuration_changed": False,
        "trade_or_order_actions": 0,
        "portfolio_replay_worker_count": portfolio_replay_workers,
        "data": {
            "tier": data_tier(), "feed": FEED, "adjustment": ADJUSTMENT,
            "range_start": start.isoformat(), "range_end": end.isoformat(),
            "latest_session_age_days": latest_age_days,
            "freshness": data_freshness,
            "universe_count": len(symbols), "covered_universe_count": len(covered_symbols),
            "covered_symbols": covered_symbols, "universe_symbols_without_history": missing,
            **universe_meta, "incremental_refresh": refresh_report,
        },
        "factor_model": factor_report,
        "factor_portfolio_holdouts": factor_replays,
        "historical_event_calendar": event_calendar_report,
        "combined_portfolio_replays": combined_replays,
        "isolated_core_direction_walk_forward": isolated_walk_forward,
        "rule_strategy_walk_forward": walk_forward,
        "walk_forward_profiles_omitted_due_to_event_data": omitted_event_profiles,
        "recommendation": {
            "rule_strategy_proposal": rule_strategy_proposal,
            "isolated_core_direction_proposal": isolated_strategy_proposal,
            "multi_factor_status": factor_review,
            "multi_factor_proposal": (
                "Review current_candidate_weights; adoption still requires your approval and a forward paper shadow run."
                if factor_review == "prequential_ic_passed_for_human_review"
                else "Do not advance current_candidate_weights; the prequential IC gate was not met."
            ),
            "event_driven_status": (
                "historical_calendar_used_in_overlay_walk_forward_and_combined_replay"
                if event_calendar is not None
                else "historical_calendar_unavailable; event-enabled profiles were skipped"
            ),
            "combined_portfolio_review": (
                "Compare the fixed 2024-2025 and forward 2026 four-core vs satellite portfolio replay metrics; these are research evidence only."
                if combined_replays else "No combined portfolio replay window was available."
            ),
            "takeover_requires_human_confirmation": True,
            "satellite_strategy_policy": (
                "Multi Factor and Event Driven are confirmation overlays only; they cannot open standalone positions."
            ),
        },
        "limitations": [
            "The current selected Algo universe is held fixed over the historical replay; this has survivorship bias.",
            "Only Plus SIP regular-session bars are replayed; BOATS overnight sessions are not part of this research.",
            "The factor model is a small non-negative Ridge-weighted linear ranker; it is not a deep-learning model.",
            "The current daily weights are research candidates only and have no authority to change live config.",
        ],
    }


@contextmanager
def _research_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("An Algo AutoResearch run is already active") from exc
        yield


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
    os.replace(temporary, path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run research-only Algo strategy and factor-model refinement")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--profiles", default=str(DEFAULT_PROFILES))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--workers", type=int, default=8, help="bounded independent walk-forward fold workers")
    parser.add_argument("--no-refresh", action="store_true", help="use existing bars without fetching recent SIP history")
    parser.add_argument("--factor-only", action="store_true", help="fit and score the factor model without portfolio/WFA replays")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output_dir = Path(args.output_dir)
    today = datetime.now(ET).date().isoformat()
    try:
        with _research_lock(LOCK_FILE):
            report = run_daily_research(args)
            report["run_date_et"] = today
            report["workers_requested"] = args.workers
            if report.get("rule_strategy_walk_forward"):
                report["workers_used"] = report["rule_strategy_walk_forward"].get("worker_count")
    except Exception as exc:
        report = {
            "status": "error", "run_date_et": today,
            "research_only": True, "live_configuration_changed": False,
            "trade_or_order_actions": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }
        print(json.dumps(report, sort_keys=True), file=sys.stderr)
        output_dir.mkdir(parents=True, exist_ok=True)
        _atomic_json(output_dir / f"{today}.json", report)
        _atomic_json(output_dir / "latest.json", report)
        return 2

    output_dir.mkdir(parents=True, exist_ok=True)
    _atomic_json(output_dir / f"{today}.json", report)
    _atomic_json(output_dir / "latest.json", report)
    print(json.dumps({
        "status": report["status"], "report": str((output_dir / "latest.json").resolve()),
        "range": [report["data"]["range_start"], report["data"]["range_end"]],
        "symbols": report["data"]["covered_universe_count"],
        "factor_candidate_weights": report["factor_model"]["current_candidate_weights"],
        "walk_forward_folds": (
            report["rule_strategy_walk_forward"]["fold_summary"]["count"]
            if report.get("rule_strategy_walk_forward") else 0
        ),
        "live_configuration_changed": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
