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
from .candidate_archive import (
    archive_discoveries, candidate_id, evaluated_candidate_ids, logic_version,
    model_family, parent_candidates,
)
from .adaptive_common import _prepare_args, _read_json_file, _write_report
from src.algo.strategies.multi_factor.features import aggregate_intraday_bars

from .adaptive_search import (
    ET, _baseline_improvement, _load_window_data, _ordered_window_options,
    _profile_for, _run_iterative_discovery_search, _trading_sessions,
)
from .settings import FEED, MULTI_FACTOR_MIN_UNIVERSE_VALUES, RESEARCH_WARMUP_DAYS

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
        getattr(args, "archive_dir", args.output_dir), args.output_dir, args.strategy,
    )
    bars, spy = _load_window_data(
        db=args.db, symbols=args.symbols_resolved, start=start, end=end,
        adjustment=args.adjustment, strategy=args.strategy,
    )
    preaggregated_daily_bars = aggregate_intraday_bars(bars)
    excluded_candidate_ids = set(getattr(args, "excluded_candidate_ids", ()))
    report = _run_iterative_discovery_search(
        strategy=args.strategy, method=args.search_method, count=args.candidates,
        iterations=args.search_iterations, seed=args.seed,
        space=args.search_space_data, symbols=args.symbols_resolved,
        bars=bars, spy_daily=spy, start=start, end=end, args=args,
        weights=args.fitness_weights_data, min_trades=args.min_trades,
        parent_parameters=parent_parameters,
        excluded_candidate_ids=getattr(args, "excluded_candidate_ids", ()),
        preaggregated_daily_bars=preaggregated_daily_bars,
        search_func=search_func,
    )
    report.update({
        "schema": "swarm_algo_adaptive_discovery_v1", "stage": "short_window_discovery",
        "created_at": datetime.now(timezone.utc).isoformat(), "research_only": True,
        "live_configuration_changed": False, "trade_or_order_actions": 0,
        "window": {"start": start.isoformat(), "end": end.isoformat(), "completed_sessions": window},
        "data_version": getattr(args, "data_version", None),
        "parent_candidate": parent_metadata,
        "archive_sequence": getattr(args, "archive_sequence", None),
        "search_seed": args.seed,
        "adaptive_novelty": _novelty_summary(
            args.strategy, report.get("candidates") or [], excluded_candidate_ids,
        ),
        "research_controls": _profile_for(
            args.strategy, report["selected_candidate"]["parameters"],
        )["controls"],
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
    archive_root = getattr(args, "archive_dir", args.output_dir)
    seen_candidate_ids = (
        evaluated_candidate_ids(
            archive_root, args.strategy, data_version,
            window_end=end.isoformat(),
        )
        if data_version else set()
    )
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
        trial_args.excluded_candidate_ids = sorted(seen_candidate_ids)
        report = run_discovery(trial_args, search_func=search_func)
        detail, _ = _read_json_file(Path(report["latest_detail_artifact"]))
        if not detail:
            raise ValueError(f"adaptive discovery detail is missing for {args.strategy}, {window_days} sessions")
        for candidate in detail.get("candidates") or []:
            parameters = candidate.get("parameters")
            if isinstance(parameters, dict):
                seen_candidate_ids.add(candidate_id(args.strategy, parameters))
        comparison = detail.get("baseline_comparison") or {}
        selected = detail.get("selected_candidate") or {}
        baseline = comparison.get("baseline_candidate") or {}
        attempt = {
            "window_days": window_days,
            "search_iterations": detail.get("search_iterations", 1),
            "candidate_count_evaluated": detail.get("candidate_count_evaluated"),
            "adaptive_novelty": detail.get("adaptive_novelty"),
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
    if args.strategy == "multi_factor":
        detail["adaptive_window_search"]["minimum_universe_values_searched"] = sorted({
            int((candidate.get("parameters") or {}).get("min_universe"))
            for candidate in detail.get("candidates", [])
            if (candidate.get("parameters") or {}).get("min_universe") is not None
        })
        detail["adaptive_window_search"]["minimum_universe_values_configured"] = list(
            MULTI_FACTOR_MIN_UNIVERSE_VALUES
        )
    archived = archive_discoveries(
        archive_root, args.strategy, [item[1] for item in reports],
    )
    baseline = next(
        (row for row in detail.get("candidates", []) if row.get("name") == "baseline"),
        None,
    )
    baseline_regimes = (baseline or {}).get("regime_metrics") or {}
    regime_best: dict[tuple[str, str], dict] = {}
    for candidate in detail.get("candidates") or []:
        if candidate.get("name") == "baseline":
            continue
        parameters = candidate.get("parameters")
        if not isinstance(parameters, dict):
            continue
        for regime, result in (candidate.get("regime_metrics") or {}).items():
            base_result = baseline_regimes.get(regime) or {}
            if not isinstance(result, dict):
                continue
            candidate_name = str(candidate.get("name") or "candidate")
            planned_ablation = candidate_name.startswith("ablation:") or ":ablation:" in candidate_name
            if not planned_ablation and not result.get("eligible"):
                continue
            fitness = float(result.get("fitness") or 0.0)
            baseline_fitness = float(base_result.get("fitness") or 0.0)
            baseline_sample_met = bool(base_result.get("minimum_sample_met"))
            family = model_family(args.strategy, parameters)
            key = (regime, family)
            profile = {
                "regime": regime,
                "candidate_id": candidate_id(args.strategy, parameters, regime),
                "parameters": parameters,
                "model_family": family,
                "logic_version": logic_version(args.strategy),
                "candidate_name": candidate_name,
                "parent_candidate_id": candidate.get("parent_candidate_id"),
                "cost_model_version": candidate.get("cost_model_version"),
                "fitness": fitness,
                "metrics": result.get("metrics"),
                "source_candidate_id": candidate_id(args.strategy, parameters),
                "baseline_fitness": baseline_fitness,
                "improved_over_baseline": bool(
                    baseline_sample_met and fitness > baseline_fitness + 1e-9
                ),
                "planned_ablation": planned_ablation,
                "historical_sample_met": bool(result.get("minimum_sample_met")),
                "historical_eligible": bool(result.get("eligible")),
                "historical_positive_after_costs": bool(result.get("positive_after_costs")),
            }
            current = regime_best.get(key)
            if (
                current is None
                or (profile["planned_ablation"] and not current.get("planned_ablation"))
                or (
                    profile["planned_ablation"] == current.get("planned_ablation")
                    and fitness > float(current.get("fitness") or -1e9)
                )
            ):
                regime_best[key] = profile
    regime_profiles = [regime_best[key] for key in sorted(regime_best)]
    detail["adaptive_regime_search"] = {
        "regimes": [row["regime"] for row in regime_profiles],
        "profiles": regime_profiles,
        "selection_basis": "planned ablations and sample-qualified positive-after-costs search candidates grouped by model family; planned variants may proceed to prospective trials without historical improvement; future-only validation still requires positive after-cost results that beat the paired current baseline",
        "archive_candidates_added": archived,
    }
    detail["adaptive_improvement"] = bool(successful)
    return _write_report(args.output_dir, args.strategy, "discovery", detail)

def _load_positive_parent(archive_dir: str, output_dir: str, strategy: str) -> tuple[list[dict], dict]:
    archived, metadata = parent_candidates(
        archive_dir, strategy, limit=6,
    )
    if archived:
        return archived, metadata
    path = Path(output_dir) / strategy / "latest-discovery.json"
    try:
        parent = json.loads(path.read_text())
        parent_end = str(parent.get("window", {}).get("end") or "") or None
        selected = parent.get("selected_candidate") or {}
        metrics = selected.get("metrics") or {}
        valid = (
            parent.get("schema") == "swarm_algo_adaptive_discovery_v1"
            and parent.get("strategy") == strategy
            and parent.get("selection_status") == "eligible_positive_candidate"
            and bool(selected.get("eligible"))
            and float(metrics.get("net_return_pct") or 0) > 0
        )
        if not valid:
            return [], {"used": False, "reason": "no positive archived candidate or prior discovery"}
        parameters = validate_parameter_profile({strategy: selected["parameters"]})[strategy]
        return [parameters], {
            "used": True, "artifact": str(path.resolve()),
            "window_end": parent_end,
            "candidate": selected.get("name"),
            "count": 1,
        }
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return [], {"used": False, "reason": "no valid prior discovery artifact"}


def _novelty_summary(strategy: str, candidates: list[dict], excluded_ids: set[str]) -> dict:
    unique_ids = set()
    novel_ids = set()
    for row in candidates:
        parameters = row.get("parameters")
        if not isinstance(parameters, dict):
            continue
        key = candidate_id(strategy, parameters)
        unique_ids.add(key)
        is_baseline = row.get("name") == "baseline" or str(row.get("name", "")).endswith(":baseline")
        if not is_baseline and key not in excluded_ids:
            novel_ids.add(key)
    return {
        "previously_evaluated_parameter_sets": len(excluded_ids),
        "unique_parameter_sets_evaluated": len(unique_ids),
        "new_parameter_variants_on_this_data": len(novel_ids),
    }
