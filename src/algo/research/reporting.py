"""Compact research report shaping."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

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
