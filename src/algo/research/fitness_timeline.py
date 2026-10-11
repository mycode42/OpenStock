"""Fitness scorecards, run comparisons, and persistent research history."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .artifact_cache import read_json, stable_hash
from .files import _atomic_json

FITNESS_TIMELINE_SCHEMA = "swarm_algo_fitness_timeline_v1"
FITNESS_SCORE_VERSION = "adaptive-composite-v1"


def _fitness_scorecard(report: dict) -> dict:
    """Summarize recent discovery and stitched OOS scores in run order."""
    from .adaptive import DEFAULT_FITNESS_WEIGHTS, _fitness

    orchestration = report.get("orchestration") or {}
    adaptive = report.get("adaptive_research") or {}
    discoveries = (adaptive.get("discovery") or {}).get("strategies") or {}
    verifications = (adaptive.get("verification") or {}).get("strategies") or {}
    artifacts = report.get("adaptive_strategy_artifacts") or {}
    strategies = orchestration.get("strategies") or list(dict.fromkeys([*discoveries, *verifications]))
    strategy_scores = {}

    for strategy in strategies:
        discovery = discoveries.get(strategy) or {}
        window_search = discovery.get("window_search") or {}
        attempts = window_search.get("attempts") or []
        selected_window = window_search.get("selected_window_days")
        selected_attempt = next(
            (item for item in attempts if item.get("window_days") == selected_window),
            attempts[-1] if attempts else {},
        )
        verification = verifications.get(strategy) or {}
        summary = verification.get("summary") or {}
        oos = summary.get("stitched_oos") or {}
        score_metrics = dict(oos)
        if "sortino_annualized" not in score_metrics:
            score_metrics["sortino_annualized"] = oos.get("daily_sortino_annualized")
        try:
            oos_fitness = _fitness(score_metrics, DEFAULT_FITNESS_WEIGHTS)[0] if oos else None
        except (TypeError, ValueError, KeyError):
            oos_fitness = None

        oos_range = {}
        detail_path = (artifacts.get(strategy) or {}).get("verification")
        if detail_path:
            detail, _ = read_json(detail_path)
            if detail:
                folds = detail.get("folds") or []
                if folds:
                    oos_range = {
                        "start": folds[0].get("test_start"),
                        "end": folds[-1].get("test_end"),
                    }

        selected = discovery.get("selected_candidate") or {}
        parent = discovery.get("parent_candidate") or {}
        selected_metrics = selected.get("metrics") or {}
        selected_return = selected_metrics.get("net_return_pct")
        eligible_parent = bool(
            discovery.get("selection_status") == "eligible_positive_candidate"
            and selected.get("eligible")
            and isinstance(selected_return, (int, float))
            and selected_return > 0
        )
        strategy_scores[strategy] = {
            "discovery": {
                "status": discovery.get("status", "missing"),
                "selection_status": discovery.get("selection_status"),
                "improvement_found": bool(window_search.get("improvement_found")),
                "search_iterations": discovery.get("search_iterations", 1),
                "search_iteration_summaries": discovery.get("search_iteration_summaries", []),
                "selected_candidate": selected.get("name"),
                "selected_window_days": selected_window,
                "attempts": [
                    {
                        "window_days": item.get("window_days"),
                        "selected_fitness": item.get("selected_fitness"),
                        "baseline_fitness": item.get("baseline_fitness"),
                        "fitness_delta_vs_baseline": item.get("fitness_delta"),
                        "selected_return_pct": item.get("selected_net_return_pct"),
                        "baseline_return_pct": item.get("baseline_net_return_pct"),
                        "improved": bool(item.get("improved")),
                    }
                    for item in attempts
                ],
                "selected_attempt": {
                    "selected_fitness": selected_attempt.get("selected_fitness"),
                    "baseline_fitness": selected_attempt.get("baseline_fitness"),
                    "fitness_delta_vs_baseline": selected_attempt.get("fitness_delta"),
                    "improved": bool(selected_attempt.get("improved")),
                } if selected_attempt else None,
                "candidate_parent_for_next_run": {
                    "eligible": eligible_parent,
                    "candidate": selected.get("name"),
                    "current_parent_was_used": bool(parent.get("used")),
                },
            },
            "verification": {
                "status": verification.get("status", "missing"),
                "error": verification.get("error"),
                "fold_count": summary.get("fold_count"),
                "oos_range": oos_range,
                "fitness": round(oos_fitness, 8) if oos_fitness is not None else None,
                "net_return_pct": oos.get("net_return_pct"),
                "sharpe": oos.get("daily_sharpe_annualized"),
                "max_drawdown_pct": oos.get("max_drawdown_pct"),
                "average_fitness_decay": summary.get("average_fitness_decay"),
                "parameter_stability": summary.get("parameter_stability"),
                "passed": (summary.get("checks") or {}).get("overall_pass"),
            },
        }

    portfolio = report.get("portfolio_coordinator_replay") or {}
    portfolio_summary = portfolio.get("fold_summary") or {}
    replay_status = (
        "completed" if portfolio else
        (report.get("stage_timings") or {}).get("portfolio_replay", {}).get("status", "skipped")
    )
    data = report.get("data") or {}
    range_data = data.get("strategy_research_range") or orchestration.get("strategy_research_range") or {}
    adaptive_policy = dict(orchestration.get("adaptive_pipeline") or {})
    adaptive_policy.pop("verification_end", None)
    scorecard = {
        "score_version": FITNESS_SCORE_VERSION,
        "fitness_weights": dict(DEFAULT_FITNESS_WEIGHTS),
        "strategies": strategy_scores,
        "portfolio": {
            "status": replay_status,
            "compounded_fold_return_pct": portfolio_summary.get("compounded_fold_return_pct"),
            "positive_oos_folds": portfolio_summary.get("positive_out_of_sample_folds"),
            "fold_count": portfolio_summary.get("count"),
            "mean_max_drawdown_pct": portfolio_summary.get("mean_max_drawdown_pct"),
            "closed_trades": portfolio_summary.get("total_closed_trades"),
        },
        "scope": {
            "score_version": FITNESS_SCORE_VERSION,
            "pipeline_version": orchestration.get("pipeline_version"),
            "discovery_version": (orchestration.get("adaptive_pipeline") or {}).get("discovery_version"),
            "verification_version": (orchestration.get("adaptive_pipeline") or {}).get("verification_version"),
            "research_start": range_data.get("start"),
            "feed": data.get("feed") or orchestration.get("feed"),
            "adjustment": data.get("adjustment") or orchestration.get("adjustment"),
            "symbols": sorted(data.get("covered_symbols") or orchestration.get("symbols") or []),
            "strategies": list(strategies),
            "portfolio_strategies": list(orchestration.get("portfolio_strategies") or []),
            "live_configuration_sha256": orchestration.get("live_configuration_sha256"),
            "adaptive_policy_sha256": stable_hash(adaptive_policy),
        },
    }
    return scorecard


def _fitness_timeline_entry(report: dict, *, source: str = "scheduled_run") -> dict:
    orchestration = report.get("orchestration") or {}
    created_at = report.get("created_at") or datetime.now(timezone.utc).isoformat()
    return {
        "invocation_id": datetime.now(timezone.utc).isoformat(),
        "run_id": orchestration.get("run_id") or report.get("run_id"),
        "run_date_et": report.get("run_date_et"),
        "created_at": created_at,
        "source": source,
        "status": report.get("status", "unknown"),
        "cache_hit": bool(orchestration.get("cache_hit", report.get("cache_hit", False))),
        "data_end": (report.get("data") or {}).get("research_data_end"),
        "scorecard": _fitness_scorecard(report),
    }


def _numeric_delta(current: object, previous: object) -> float | None:
    if (
        isinstance(current, (int, float)) and not isinstance(current, bool)
        and isinstance(previous, (int, float)) and not isinstance(previous, bool)
    ):
        return round(float(current) - float(previous), 8)
    return None


def _compare_fitness_runs(current: dict, previous: dict | None) -> dict:
    if not previous:
        return {"available": False, "comparable": False, "reason": "No earlier research run is retained."}

    current_scope = current.get("scorecard", {}).get("scope") or {}
    previous_scope = previous.get("scorecard", {}).get("scope") or {}
    scope_keys = (
        "score_version", "pipeline_version", "discovery_version", "verification_version", "research_start",
        "feed", "adjustment", "symbols", "strategies", "portfolio_strategies",
        "live_configuration_sha256", "adaptive_policy_sha256",
    )
    reasons = [
        key for key in scope_keys
        if current_scope.get(key) != previous_scope.get(key)
    ]
    required_scope = (
        "pipeline_version", "discovery_version", "verification_version", "research_start",
        "feed", "adjustment", "live_configuration_sha256", "adaptive_policy_sha256",
    )
    if any(current_scope.get(key) is None for key in required_scope):
        reasons.append("current scope metadata missing")
    if any(previous_scope.get(key) is None for key in required_scope):
        reasons.append("previous scope metadata missing")
    comparable = not reasons
    changes = {}
    prior_scores = previous.get("scorecard", {}).get("strategies") or {}
    current_scores = current.get("scorecard", {}).get("strategies") or {}
    if comparable:
        for strategy in current_scope.get("strategies") or []:
            old = prior_scores.get(strategy) or {}
            new = current_scores.get(strategy) or {}
            old_oos = old.get("verification") or {}
            new_oos = new.get("verification") or {}
            old_discovery = old.get("discovery") or {}
            new_discovery = new.get("discovery") or {}

            new_attempts = new_discovery.get("attempts") or []
            old_attempts = old_discovery.get("attempts") or []
            new_discovery_delta = next(
                (item.get("fitness_delta_vs_baseline") for item in new_attempts if item.get("improved")),
                0.0,
            )
            old_discovery_delta = next(
                (item.get("fitness_delta_vs_baseline") for item in old_attempts if item.get("improved")),
                0.0,
            )
            changes[strategy] = {
                "oos_fitness_delta": _numeric_delta(new_oos.get("fitness"), old_oos.get("fitness")),
                "oos_return_delta_percentage_points": _numeric_delta(new_oos.get("net_return_pct"), old_oos.get("net_return_pct")),
                "oos_sharpe_delta": _numeric_delta(new_oos.get("sharpe"), old_oos.get("sharpe")),
                "oos_drawdown_delta_percentage_points": _numeric_delta(new_oos.get("max_drawdown_pct"), old_oos.get("max_drawdown_pct")),
                "discovery_improvement_delta_change": _numeric_delta(new_discovery_delta, old_discovery_delta),
                "verification_gate_changed": old_oos.get("passed") != new_oos.get("passed"),
            }

    old_portfolio = previous.get("scorecard", {}).get("portfolio") or {}
    new_portfolio = current.get("scorecard", {}).get("portfolio") or {}
    portfolio_delta = None
    if comparable:
        old_return = old_portfolio.get("compounded_fold_return_pct")
        new_return = new_portfolio.get("compounded_fold_return_pct")
        if isinstance(old_return, (int, float)) and isinstance(new_return, (int, float)):
            portfolio_delta = round(float(new_return) - float(old_return), 8)

    same_run = bool(current.get("run_id") and current.get("run_id") == previous.get("run_id"))
    reason = (
        "Same cached cohort; no new scored data or new research computation."
        if same_run
        else "Scores use the same strategy, data, and policy definitions. The rolling OOS range expands as new sessions arrive, so deltas are trend changes, not a controlled A/B test."
        if comparable
        else "Prior run is not directly comparable because these scope fields changed: " + ", ".join(reasons)
    )
    return {
        "available": True,
        "comparable": comparable,
        "prior_run_id": previous.get("run_id"),
        "prior_run_date_et": previous.get("run_date_et"),
        "reason": reason,
        "same_cached_cohort": same_run,
        "strategy_changes": changes if comparable else {},
        "portfolio_compounded_return_delta_percentage_points": portfolio_delta,
    }


def _next_run_plan(scorecard: dict) -> dict:
    plans = {}
    for strategy, item in (scorecard.get("strategies") or {}).items():
        discovery = item.get("discovery") or {}
        verification = item.get("verification") or {}
        if verification.get("status") == "error":
            action = "Retry verification after resolving its reported error; the portfolio replay remains blocked until all required verifications complete."
        elif discovery.get("candidate_parent_for_next_run", {}).get("eligible"):
            action = "Seed the next research search from this eligible positive recent discovery, explore nearby variants, and require a fresh rolling OOS pass before promotion."
        elif discovery.get("status") == "error":
            action = "Retry discovery after resolving its reported error; keep live parameters unchanged."
        else:
            action = "Keep the configured baseline because discovery found no eligible positive improvement; continue the window ladder and recheck on rolling OOS folds."
        plans[strategy] = action
    return {
        "policy": "Recent discovery may seed the next search only as a research candidate. Baseline remains when no after-cost improvement qualifies. Every run is reverified out of sample; research never changes live parameters.",
        "by_strategy": plans,
    }


def _attach_fitness_timeline(report: dict, output_dir: Path) -> dict:
    """Persist one scorecard per invocation and compare it with its predecessor."""
    output_dir = Path(output_dir)
    history_path = output_dir / "layers" / "fitness-history.json"
    history, _ = read_json(history_path)
    runs = list((history or {}).get("runs") or [])

    current = _fitness_timeline_entry(report)
    current_run_id = current.get("run_id")
    if not runs:
        previous_report, _ = read_json(output_dir / "latest.json")
        previous_id = ((previous_report or {}).get("orchestration") or {}).get("run_id")
        if previous_report and previous_id and previous_id != current_run_id:
            runs.append(_fitness_timeline_entry(previous_report, source="prior_latest_report"))

    previous = runs[-1] if runs else None
    comparison = _compare_fitness_runs(current, previous)
    current["comparison_to_previous"] = comparison
    runs.append(current)
    _atomic_json(history_path, {
        "schema": FITNESS_TIMELINE_SCHEMA,
        "score_version": FITNESS_SCORE_VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "runs": runs,
    })

    brief_history = []
    for entry in runs[-8:]:
        scores = entry.get("scorecard", {}).get("strategies") or {}
        brief_history.append({
            "run_id": entry.get("run_id"),
            "run_date_et": entry.get("run_date_et"),
            "status": entry.get("status"),
            "cache_hit": entry.get("cache_hit"),
            "data_end": entry.get("data_end"),
            "oos_scores": {
                strategy: {
                    "fitness": (item.get("verification") or {}).get("fitness"),
                    "return_pct": (item.get("verification") or {}).get("net_return_pct"),
                    "passed": (item.get("verification") or {}).get("passed"),
                }
                for strategy, item in scores.items()
            },
            "portfolio_return_pct": (entry.get("scorecard", {}).get("portfolio") or {}).get("compounded_fold_return_pct"),
        })
    report["fitness_timeline"] = {
        "score_version": FITNESS_SCORE_VERSION,
        "history_count": len(runs),
        "current": current["scorecard"],
        "comparison_to_previous": comparison,
        "recent_runs": brief_history,
        "next_run_plan": _next_run_plan(current["scorecard"]),
        "interpretation": "Discovery fitness measures the recent training-window candidate against its baseline. Verification fitness is computed from stitched out-of-sample results. Portfolio returns compound across OOS folds within a run; returns are not compounded across separate nightly runs.",
    }
    return report
