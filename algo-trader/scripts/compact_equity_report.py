#!/usr/bin/env python3
"""Build a small prompt context without changing the full equity research log.

The research pipeline's ``latest.json`` remains the extended, authoritative
artifact. This projection keeps report-facing summaries and omits bulky
per-observation timelines, folds, and strategy artifacts.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile
from typing import Any

MAX_COMPACT_BYTES = 200_000


def _pick(value: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in keys if key in value}


def compact_equity_report(report: dict[str, Any]) -> dict[str, Any]:
    """Return the stable, prompt-relevant subset of a completed research report."""
    compact: dict[str, Any] = _pick(
        report,
        (
            "schema", "invocation_id", "run_date_et", "created_at", "status",
            "research_only", "workers_requested", "live_configuration_changed",
            "trade_or_order_actions", "stage_timings", "limitations", "recommendation",
        ),
    )

    compact["orchestration"] = _pick(
        report.get("orchestration"),
        (
            "run_id", "invocation_id", "cache_hit", "feed", "data_end",
            "stored_coverage_start", "strategies", "symbols",
            "strategy_research_range", "pipeline_version",
        ),
    )
    compact["data"] = _pick(
        report.get("data"),
        (
            "feed", "freshness", "tier", "covered_universe_count",
            "covered_symbols", "latest_available_end", "latest_session_age_days",
            "range_start", "research_data_end", "stored_coverage",
            "strategy_research_range", "universe_count", "selected_universe_count",
            "universe_source", "incremental_refresh",
        ),
    )

    # This is the canonical compact source for discovery, rolling OOS
    # verification, comparisons, recent timeline, and next-run guidance.
    if isinstance(report.get("fitness_timeline"), dict):
        compact["fitness_timeline"] = report["fitness_timeline"]

    factor_keys = (
        "research_start", "session_start", "session_end", "indicator_warmup_start",
        "latest_factor_observation_date", "latest_mature_training_label_cutoff",
        "model_family", "primary_model_mode", "observation_count",
        "rolling_evaluation_count", "forward_horizon_sessions",
        "fundamental_data_status", "fundamental_factors_available", "fundamental_feed",
        "factor_observation_coverage", "learned_vs_legacy_static",
        "adaptive_prequential_ic", "adaptive_prequential_net_of_costs_ic",
        "adaptive_after_cost_long_short_return", "equal_weight_after_cost_long_short_return",
        "legacy_static_baseline_after_cost_long_short_return", "feature_importance",
        "factor_names", "universe_limitation", "transaction_cost_model",
    )
    factor_model = _pick(report.get("factor_model"), factor_keys)
    if factor_model:
        compact["factor_model"] = factor_model

    if isinstance(report.get("portfolio_summary"), dict):
        compact["portfolio_summary"] = report["portfolio_summary"]

    replay = report.get("portfolio_coordinator_replay")
    replay_summary = _pick(
        replay,
        (
            "created_at", "stage", "worker_count", "profile_selection_source",
            "live_configuration_changed", "research_only", "trade_or_order_actions",
            "limitations",
        ),
    )
    if isinstance(replay, dict) and isinstance(replay.get("fold_summary"), dict):
        replay_summary["fold_summary"] = replay["fold_summary"]
    if replay_summary:
        compact["portfolio_coordinator_replay"] = replay_summary

    forward = _pick(
        report.get("forward_validation"),
        (
            "status", "active_trial_count", "current_regime", "live_configuration_changed",
            "promotion_paused_by_drift", "promotion_pause_reasons", "promoted",
            "rejected_candidate_ids", "staging_errors", "simulation_errors",
        ),
    )
    if forward:
        compact["forward_validation"] = forward

    adaptive = report.get("adaptive_research")
    if isinstance(adaptive, dict):
        stages: dict[str, Any] = {}
        for stage_name in ("discovery", "verification"):
            stage = adaptive.get(stage_name)
            if isinstance(stage, dict):
                stages[stage_name] = _pick(
                    stage,
                    (
                        "status", "error_strategies", "verification_range",
                        "window_days_by_strategy", "candidate_validation",
                    ),
                )
        if stages:
            compact["adaptive_research"] = stages

    drift = _pick(
        report.get("drift_monitor"),
        ("status", "action", "promotion_pause", "live_sizing_changed", "research_only"),
    )
    if drift:
        compact["drift_monitor"] = drift

    discovery = _pick(
        report.get("multi_factor_discovery"),
        (
            "status", "strategy", "window", "selection_status",
            "adaptive_improvement", "adaptive_novelty",
        ),
    )
    if discovery:
        compact["multi_factor_discovery"] = discovery

    return compact


def write_compact_report(source: Path, output: Path) -> int:
    """Write the projection atomically; return serialized byte count."""
    report = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError(f"expected a JSON object in {source}")
    payload = json.dumps(compact_equity_report(report), ensure_ascii=False, separators=(",", ":")) + "\n"
    encoded = payload.encode("utf-8")
    if len(encoded) > MAX_COMPACT_BYTES:
        raise ValueError(
            f"compact report is {len(encoded)} bytes, exceeding the {MAX_COMPACT_BYTES}-byte safety limit"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    try:
        with os.fdopen(fd, "wb") as temp_file:
            temp_file.write(encoded)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, output)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return len(encoded)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="full equity research JSON; never modified")
    parser.add_argument("--output", type=Path, required=True, help="compact prompt-context JSON to create")
    args = parser.parse_args(argv)
    size = write_compact_report(args.source, args.output)
    print(f"Wrote compact equity report ({size} bytes) to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
