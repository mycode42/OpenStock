"""Human-readable and compact options research reports."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from runtime.atomic import atomic_write_json


def report_for_history(report: dict) -> dict:
    """Keep replay P&L while removing legacy parameter-search details."""
    stored = deepcopy(report)
    legacy_search_report = any(
        key in stored for key in ("candidate_leaderboard", "candidate_reports", "final_candidate_id")
    )
    for key in (
        "candidate_leaderboard", "candidate_reports", "final_candidate_id",
        "baseline_comparison", "parameter_stability",
    ):
        stored.pop(key, None)
    if legacy_search_report:
        stored.pop("folds", None)
    validation = stored.get("validation")
    if isinstance(validation, dict):
        validation.pop("champion", None)

    def strip_candidate_details(value):
        if isinstance(value, dict):
            for key in ("candidate", "candidate_pool", "candidate_leaderboard", "candidate_reports"):
                value.pop(key, None)
            for child in value.values():
                strip_candidate_details(child)
        elif isinstance(value, list):
            for child in value:
                strip_candidate_details(child)

    strip_candidate_details(stored)
    return stored


def report_for_filesystem(report: dict) -> dict:
    """Keep lookup metrics in JSON; detailed equity and trade rows stay in SQLite/CSV."""
    summary = report_for_history(report)

    def trim(value):
        if isinstance(value, dict):
            for key in ("trades", "equity_curve", "candidate", "candidate_pool"):
                value.pop(key, None)
            for child in value.values():
                trim(child)
        elif isinstance(value, list):
            for child in value:
                trim(child)

    trim(summary)
    return summary


def _fmt(value, *, percent: bool = False, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    number = float(value)
    return f"{number:.{digits}%}" if percent else f"{number:.{digits}f}"


def write_research_report(run_dir: Path, report: dict) -> None:
    """Write a compact lookup JSON and a concise fixed-configuration P&L report."""
    atomic_write_json(
        run_dir / "report.json", report_for_filesystem(report),
        indent=2, sort_keys=True, allow_nan=False,
    )
    exploratory = report.get("exploratory_replay") or {}
    walk_forward = report.get("walk_forward") or {}
    metrics = exploratory.get("metrics") or walk_forward.get("oos_metrics") or {}
    quality = report.get("validation") or {}
    profile = report.get("strategy_configuration") or {}
    period = exploratory
    if not period:
        folds = report.get("folds") or []
        if folds:
            period = {
                "period_start": folds[0].get("oos_equity_summary", {}).get("start"),
                "period_end": folds[-1].get("oos_equity_summary", {}).get("end"),
            }
        elif walk_forward.get("equity_curve"):
            curve = walk_forward["equity_curve"]
            period = {"period_start": curve[0].get("date"), "period_end": curve[-1].get("date")}
    lines = [
        f"# Options replay: {report.get('strategy')} ({report.get('run_id')})",
        "",
        f"Created: {report.get('created_at')}  ",
        f"Underlying: {report.get('underlying')}  ",
        f"Data mode: {report.get('data_mode')}  ",
        f"Replay mode: {report.get('research_tier', 'fixed configuration')}  ",
        f"Period: {period.get('period_start', 'n/a')} to {period.get('period_end', 'n/a')}  ",
        f"Settings: {json.dumps(profile, sort_keys=True)}",
        "",
        "## P&L and risk",
        "",
        f"Trades: {metrics.get('trade_count', 0)}  ",
        f"Return: {_fmt(metrics.get('total_return'), percent=True)}  ",
        f"Sharpe: {_fmt(metrics.get('annualized_sharpe'))}  ",
        f"Maximum drawdown: {_fmt(metrics.get('max_drawdown'), percent=True)}  ",
        f"Risk-adjusted score: {_fmt(metrics.get('risk_adjusted_score'))}  ",
        f"Validation status: {quality.get('status', 'descriptive_only')}  ",
        "",
        "This report replays one fixed configuration. It does not search, rank, or promote parameter sets.",
        "",
    ]
    if walk_forward:
        greek = walk_forward.get("greek_summary") or {}
        lines.extend([
            f"Walk-forward folds: {len(report.get('folds') or [])}  ",
            f"Mean train score: {_fmt(walk_forward.get('mean_train_risk_adjusted_score'))}  ",
            f"Out-of-sample score: {_fmt(walk_forward.get('mean_oos_risk_adjusted_score'))}  ",
            f"Walk-forward efficiency: {_fmt(walk_forward.get('walk_forward_efficiency'))}  ",
            "",
            "## Out-of-sample P&L attribution",
            "",
            f"Greeks: {json.dumps(greek, sort_keys=True)}  ",
            f"Regime breakdown: {json.dumps(walk_forward.get('regime_breakdown', {}), sort_keys=True)}  ",
            f"Volatility breakdown: {json.dumps(walk_forward.get('volatility_breakdown', {}), sort_keys=True)}  ",
            "",
            "Daily P&L and trade rows are in equity_curve.csv and oos_trades.csv.",
            "",
        ])
    elif exploratory:
        lines.extend([
            "Daily P&L and trade rows are in exploratory_equity_curve.csv and exploratory_trades.csv.",
            "",
        ])
    robustness = report.get("robustness") or {}
    sensitivity = robustness.get("slippage_sensitivity") or []
    if sensitivity:
        lines.extend(["## Slippage and spread sensitivity", ""])
        for item in sensitivity:
            result = item.get("metrics") or {}
            lines.append(
                f"- Slippage {item.get('slippage_multiplier')}x; spread "
                f"{item.get('spread_width_multiplier')}x: "
                f"{_fmt(result.get('total_return'), percent=True)} return, "
                f"{_fmt(result.get('risk_adjusted_score'))} score."
            )
        lines.append("")
    assignment = robustness.get("assignment_sensitivity")
    if assignment:
        lines.extend([
            "## Assignment sensitivity",
            "",
            json.dumps(assignment, sort_keys=True),
            "",
        ])
    assumptions = report.get("data_assumptions") or {}
    if assumptions:
        lines.extend([
            "## Data assumptions",
            "",
            f"Dataset SHA-256: {report.get('dataset_sha256')}. "
            f"Historical chains required: {assumptions.get('historical_chain_snapshots_required_for_empirical_claims')}. "
            f"Model or synthetic rows present: {assumptions.get('model_or_synthetic_rows_present')}.",
            "",
        ])
    (run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


__all__ = ["report_for_filesystem", "report_for_history", "write_research_report"]
