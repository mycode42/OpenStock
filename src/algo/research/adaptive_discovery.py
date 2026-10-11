"""Recent-window adaptive candidate discovery and incumbent selection."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path

from src.algo.strategies.registry import validate_parameter_profile
from src.market_data import data_tier

from .files import _atomic_json
from .adaptive_common import _prepare_args, _read_json_file, _write_report
from src.algo.strategies.multi_factor.features import aggregate_intraday_bars

from .adaptive_search import (
    ET, _baseline_improvement, _load_window_data, _ordered_window_options,
    _run_iterative_discovery_search, _trading_sessions,
)
from .settings import FEED, RESEARCH_WARMUP_DAYS

def run_discovery(args, *, search_func=None) -> dict:
    """Run short-window discovery only and persist detailed plus summary JSON."""
    _prepare_args(args)
    end = date.fromisoformat(args.end) if args.end else datetime.now(ET).date() - timedelta(days=1)
    window = args.window_days
    session_lookback_days = max(RESEARCH_WARMUP_DAYS, int(window) * 3)
    sessions = _trading_sessions(
        args.db, end, start=end - timedelta(days=session_lookback_days),
        adjustment=args.adjustment,
    )
    if args.start:
        sessions = [session for session in sessions if session >= date.fromisoformat(args.start)]
    if window < 2 or len(sessions) < window:
        raise ValueError(f"need {window} completed SPY sessions for discovery; found {len(sessions)}")
    selected_sessions = sessions[-window:]
    start, end = selected_sessions[0], selected_sessions[-1]
    parent_parameters, parent_metadata = _load_positive_parent(
        args.output_dir, args.strategy, end=end,
    )
    bars, spy = _load_window_data(
        db=args.db, symbols=args.symbols_resolved, start=start, end=end,
        adjustment=args.adjustment, strategy=args.strategy,
    )
    preaggregated_daily_bars = aggregate_intraday_bars(bars)
    report = _run_iterative_discovery_search(
        strategy=args.strategy, method=args.search_method, count=args.candidates,
        iterations=args.search_iterations, seed=args.seed,
        space=args.search_space_data, symbols=args.symbols_resolved,
        bars=bars, spy_daily=spy, start=start, end=end, args=args,
        weights=args.fitness_weights_data, min_trades=args.min_trades,
        parent_parameters=parent_parameters,
        preaggregated_daily_bars=preaggregated_daily_bars,
        search_func=search_func,
    )
    report.update({
        "schema": "swarm_algo_adaptive_discovery_v1", "stage": "short_window_discovery",
        "created_at": datetime.now(timezone.utc).isoformat(), "research_only": True,
        "live_configuration_changed": False, "trade_or_order_actions": 0,
        "window": {"start": start.isoformat(), "end": end.isoformat(), "completed_sessions": window},
        "parent_candidate": parent_metadata,
        "search_iterations": args.search_iterations,
        "search_iteration_summaries": report.get("iteration_summaries", []),
        "symbols": args.symbols_resolved, "database": str(Path(args.db).resolve()),
        "data_tier": data_tier(), "feed": FEED, "adjustment": args.adjustment,
        "limitations": [
            "The short-window winner is a discovery result, not evidence of persistent profitability.",
            "Parameter domains are bounded and discrete; execution costs and risk limits are held fixed.",
        ],
    })
    report["baseline_comparison"] = _baseline_improvement(report)
    return _write_report(args.output_dir, args.strategy, "discovery", report)

def run_window_ladder_discovery(
    args, window_options: list[int], *, pipeline_version: str, data_version: str | None = None,
    search_func=None,
) -> dict:
    """Try recent windows from preferred to neighboring lengths until a real improvement appears."""
    options = list(dict.fromkeys(int(value) for value in window_options))
    if any(value < 2 for value in options):
        raise ValueError("adaptive discovery windows must be at least two sessions")

    canonical = Path(args.output_dir) / args.strategy / "latest-discovery.json"
    prior, _ = _read_json_file(canonical)
    preferred = None
    if prior and prior.get("strategy") == args.strategy:
        prior_window = (prior.get("adaptive_window_search") or {}).get("selected_window_days")
        if prior_window in options and prior.get("selection_status") == "eligible_positive_candidate":
            preferred = int(prior_window)
        elif prior.get("window", {}).get("completed_sessions") in options:
            preferred = int(prior["window"]["completed_sessions"])
    ordered = _ordered_window_options(options, preferred)
    end = date.fromisoformat(args.end) if args.end else datetime.now(ET).date() - timedelta(days=1)
    attempts = []
    reports = []
    for window_days in ordered:
        trial_output = Path(args.output_dir) / args.strategy / "window-search" / f"{end.isoformat()}-{window_days}"
        trial_output.mkdir(parents=True, exist_ok=True)
        previous_canonical, _ = _read_json_file(canonical)
        if previous_canonical:
            _atomic_json(trial_output / args.strategy / "latest-discovery.json", previous_canonical)
        trial_args = argparse.Namespace(**vars(args))
        trial_args.output_dir = str(trial_output)
        trial_args.window_days = window_days
        report = run_discovery(trial_args, search_func=search_func)
        detail, _ = _read_json_file(Path(report["latest_detail_artifact"]))
        if not detail:
            raise ValueError(f"adaptive discovery detail is missing for {args.strategy}, {window_days} sessions")
        comparison = detail.get("baseline_comparison") or {}
        selected = detail.get("selected_candidate") or {}
        baseline = comparison.get("baseline_candidate") or {}
        attempt = {
            "window_days": window_days,
            "search_iterations": detail.get("search_iterations", 1),
            "candidate_count_evaluated": detail.get("candidate_count_evaluated"),
            "search_iteration_summaries": detail.get("search_iteration_summaries", []),
            "selection_status": detail.get("selection_status"),
            "selected_candidate": selected.get("name"),
            "selected_fitness": comparison.get("selected_fitness"),
            "baseline_fitness": comparison.get("baseline_fitness"),
            "fitness_delta": comparison.get("fitness_delta"),
            "selected_net_return_pct": (selected.get("metrics") or {}).get("net_return_pct"),
            "baseline_net_return_pct": (baseline.get("metrics") or {}).get("net_return_pct"),
            "improved": bool(comparison.get("improved")),
            "detail_artifact": report["latest_detail_artifact"],
        }
        attempts.append(attempt)
        reports.append((report, detail, attempt))
        if attempt["improved"]:
            break

    successful = next((item for item in reports if item[2]["improved"]), None)
    if successful:
        _, detail, chosen_attempt = successful
    else:
        _, detail, chosen_attempt = max(
            reports,
            key=lambda item: float(item[2].get("baseline_fitness") or -math.inf),
        )
        baseline = (detail.get("baseline_comparison") or {}).get("baseline_candidate")
        if baseline:
            detail["selected_candidate"] = baseline
            comparison = detail.setdefault("baseline_comparison", {})
            comparison.update({
                "selected_fitness": comparison.get("baseline_fitness", baseline.get("fitness", 0.0)),
                "fitness_delta": 0.0,
                "improved": False,
            })
        detail["selection_status"] = "no_adaptive_improvement"
        detail["adaptive_improvement"] = False

    detail["adaptive_window_search"] = {
        "pipeline_version": pipeline_version,
        "data_version": data_version,
        "configured_window_days": options,
        "candidate_count": args.candidates,
        "search_iterations": args.search_iterations,
        "candidate_count_per_window": args.candidates * args.search_iterations,
        "search_method": args.search_method,
        "attempt_order": ordered,
        "attempts": attempts,
        "selected_window_days": chosen_attempt["window_days"],
        "improvement_found": bool(successful),
        "fallback_used": len(attempts) > 1,
        "selection_rule": "require a sample-qualified positive-after-costs candidate with higher fitness than baseline",
    }
    detail["adaptive_improvement"] = bool(successful)
    return _write_report(args.output_dir, args.strategy, "discovery", detail)

def _load_positive_parent(output_dir: str, strategy: str, *, end: date) -> tuple[dict | None, dict]:
    path = Path(output_dir) / strategy / "latest-discovery.json"
    try:
        parent = json.loads(path.read_text())
        parent_end = date.fromisoformat(str(parent.get("window", {}).get("end", "")))
        selected = parent.get("selected_candidate") or {}
        metrics = selected.get("metrics") or {}
        valid = (
            parent.get("schema") == "swarm_algo_adaptive_discovery_v1"
            and parent.get("strategy") == strategy
            and parent.get("selection_status") == "eligible_positive_candidate"
            and parent_end < end
            and bool(selected.get("eligible"))
            and float(metrics.get("net_return_pct") or 0) > 0
        )
        if not valid:
            return None, {"used": False, "reason": "no prior positive candidate strictly before this window"}
        parameters = validate_parameter_profile({strategy: selected["parameters"]})[strategy]
        return parameters, {
            "used": True, "artifact": str(path.resolve()),
            "window_end": parent_end.isoformat(),
            "candidate": selected.get("name"),
        }
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None, {"used": False, "reason": "no valid prior discovery artifact"}
