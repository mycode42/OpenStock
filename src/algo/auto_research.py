"""Scheduled research-only refinement for Algo strategies and factor weights.

The runner may refresh historical SIP bars and write dated research reports. It
cannot modify live strategy settings or route trades. OpenClaw delivers the
resulting report to Telegram in a separate, read-only reporting job.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile
from zoneinfo import ZoneInfo

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


def _combined_portfolio_replay(
    store: SQLiteBarStore,
    *,
    symbols: list[str],
    start: date,
    end: date,
    event_calendar: EventCalendar | None,
) -> dict:
    """Compare the four core rules with the six-strategy confirmation portfolio."""
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
    core = ["mean_reversion", "momentum", "opening_range_breakout", "trend_following"]
    all_six = [*core, "multi_factor", "event_driven"]
    results = {}
    variants = {
        "four_core_rules": (core, {"satellite_overlay": {
            "multi_factor_enabled": False, "event_driven_enabled": False,
        }}),
        "six_strategy_satellite_portfolio": (all_six, {"satellite_overlay": {
            "multi_factor_enabled": True,
            "event_driven_enabled": event_calendar is not None,
        }, "event_driven": {"enabled": event_calendar is not None}}),
    }
    for name, (strategies, profile) in variants.items():
        report = AlgoPortfolioBacktest(
            symbols=symbols, intraday_bars=intraday, spy_daily_bars=spy,
            start=start.isoformat(), end=end.isoformat(), feed=FEED,
            data_adjustment=ADJUSTMENT, slippage_bps=5, profile=profile,
            strategies=strategies, event_calendar=event_calendar,
        ).run()
        results[name] = _summarize_backtest(report)
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
    if not args.factor_only:
        equal_weights = {name: 0.25 for name in factor_report["factor_names"]}
        frozen_weights = factor_report["frozen_2016_2023_weights"]
        cutoff_2025 = date(2025, 12, 31)
        # The model through 2025 is a separate forward vintage for the 2026 replay.
        if date(2024, 1, 1) <= end:
            holdout_end = min(end, cutoff_2025)
            factor_replays["fixed_2024_2025_holdout"] = {
                "baseline_equal_weights": _factor_portfolio_replay(
                    store, symbols=covered_symbols, start=date(2024, 1, 1),
                    end=holdout_end, weights=equal_weights,
                ),
                "weights_fit_through_2023": _factor_portfolio_replay(
                    store, symbols=covered_symbols, start=date(2024, 1, 1),
                    end=holdout_end, weights=frozen_weights,
                ),
            }
        if date(2026, 1, 1) <= end:
            weights_2025 = factor_report.get("weights_fit_through_2025")
            if weights_2025:
                factor_replays["forward_2026"] = {
                    "baseline_equal_weights": _factor_portfolio_replay(
                        store, symbols=covered_symbols, start=date(2026, 1, 1),
                        end=end, weights=equal_weights,
                    ),
                    "weights_fit_through_2025": _factor_portfolio_replay(
                        store, symbols=covered_symbols, start=date(2026, 1, 1),
                        end=end, weights=weights_2025,
                    ),
                }

        if date(2024, 1, 1) <= end:
            combined_end = min(end, date(2025, 12, 31))
            combined_replays["fixed_2024_2025_holdout"] = _combined_portfolio_replay(
                store, symbols=covered_symbols, start=date(2024, 1, 1),
                end=combined_end, event_calendar=event_calendar,
            )
        if date(2026, 1, 1) <= end:
            combined_replays["forward_2026"] = _combined_portfolio_replay(
                store, symbols=covered_symbols, start=date(2026, 1, 1),
                end=end, event_calendar=event_calendar,
            )

    walk_forward = None
    omitted_event_profiles: list[str] = []
    if not args.factor_only:
        from src.algo.walk_forward import build_parser as build_walk_forward_parser, run_walk_forward

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
                "--max-candidate-ticker-days-per-session", "3",
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

    return {
        "status": "completed",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "research_only": True,
        "live_configuration_changed": False,
        "trade_or_order_actions": 0,
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
        "rule_strategy_walk_forward": walk_forward,
        "walk_forward_profiles_omitted_due_to_event_data": omitted_event_profiles,
        "recommendation": {
            "rule_strategy_proposal": (
                "Review the latest training-selected profile and its chronological holdout record."
                if walk_forward and walk_forward.get("folds") else "No rule strategy profile was eligible for review."
            ),
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
