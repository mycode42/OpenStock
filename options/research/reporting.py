"""Human-readable and machine-readable options research reports."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from runtime.atomic import atomic_write_json


def _fmt(value, *, percent: bool = False, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    number = float(value)
    if percent:
        return f"{number:.{digits}%}"
    return f"{number:.{digits}f}"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        rows = [["n/a"] * len(headers)]
    widths = [max(len(header), *(len(row[index]) for row in rows)) for index, header in enumerate(headers)]
    line = "| " + " | ".join(header.ljust(widths[index]) for index, header in enumerate(headers)) + " |"
    divider = "| " + " | ".join("-" * widths[index] for index in range(len(headers))) + " |"
    body = ["| " + " | ".join(value.ljust(widths[index]) for index, value in enumerate(row)) + " |" for row in rows]
    return "\n".join([line, divider, *body])


def write_research_report(run_dir: Path, report: dict) -> None:
    """Write stable JSON and a concise Markdown report for a completed run."""
    atomic_write_json(run_dir / "report.json", report, indent=2, sort_keys=True, allow_nan=False)
    if report.get("research_tier") == "short_history_baseline_replay":
        replay = report.get("exploratory_replay", {})
        metrics = replay.get("metrics", {})
        lines = [
            f"# Exploratory options replay: {report.get('strategy')} ({report.get('run_id')})",
            "",
            f"Status: **EXPLORATORY ONLY — NOT VALIDATED**  ",
            f"Underlying: {report.get('underlying')}  ",
            f"Available sessions: {report.get('dataset_observations')}  ",
            f"Period: {replay.get('period_start')} to {replay.get('period_end')}  ",
            f"Profile: {replay.get('profile')}  ",
            "",
            "This is a descriptive replay on the full available history. It is not out-of-sample validation, includes no parameter search, and cannot be promoted or exported to paper/live configuration.",
            "",
            "## Descriptive metrics",
            "",
            f"Trades: {metrics.get('trade_count', 0)}  ",
            f"Return: {_fmt(metrics.get('total_return'), percent=True)}  ",
            f"Sharpe: {_fmt(metrics.get('annualized_sharpe'))}  ",
            f"Maximum drawdown: {_fmt(metrics.get('max_drawdown'), percent=True)}  ",
            f"Risk-adjusted score: {_fmt(metrics.get('risk_adjusted_score'))}  ",
            f"Detailed equity and trade rows: `exploratory_equity_curve.csv`, `exploratory_trades.csv`  ",
            "",
            f"Standard validation requirement: `{report.get('split', {}).get('standard_validation_requirement')}`",
            "",
        ]
        (run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
        return
    oos = report.get("walk_forward", {}).get("oos_metrics", {})
    holdout = report.get("holdout", {}).get("metrics", {})
    rows = [
        ["Rolling OOS", str(oos.get("trade_count", 0)), _fmt(oos.get("total_return"), percent=True),
         _fmt(oos.get("annualized_sharpe")), _fmt(oos.get("max_drawdown"), percent=True),
         _fmt(oos.get("risk_adjusted_score"))],
        ["Untouched holdout", str(holdout.get("trade_count", 0)), _fmt(holdout.get("total_return"), percent=True),
         _fmt(holdout.get("annualized_sharpe")), _fmt(holdout.get("max_drawdown"), percent=True),
         _fmt(holdout.get("risk_adjusted_score"))],
    ]
    metrics_table = _table(["Period", "Trades", "Return", "Sharpe", "Max DD", "Risk score"], rows)
    candidate_rows = []
    for item in report.get("candidate_leaderboard", []):
        metrics = item.get("mean_is_metrics", {})
        candidate_rows.append([
            str(item.get("candidate_id", "")), json.dumps(item.get("parameters", {}), sort_keys=True),
            ", ".join(item.get("allowed_regimes", [])), str(item.get("selected_oos_folds", 0)),
            _fmt(metrics.get("risk_adjusted_score")),
        ])
    candidate_table = _table(["Candidate", "Parameters", "Regimes", "Selected OOS folds", "Mean IS score"], candidate_rows)

    greek = oos.get("greeks", {})
    regime_rows = [
        [label, str(value.get("sessions", 0)), _fmt(value.get("pnl")),
         str(value.get("positive_sessions", 0)), str(value.get("negative_sessions", 0))]
        for label, value in report.get("walk_forward", {}).get("regime_breakdown", {}).items()
    ]
    regime_table = _table(["Market regime", "Sessions", "PnL", "Positive", "Negative"], regime_rows)
    vol_rows = [
        [label, str(value.get("sessions", 0)), _fmt(value.get("pnl")),
         str(value.get("positive_sessions", 0)), str(value.get("negative_sessions", 0))]
        for label, value in report.get("walk_forward", {}).get("volatility_breakdown", {}).items()
    ]
    vol_table = _table(["Volatility state", "Sessions", "PnL", "Positive", "Negative"], vol_rows)
    sensitivity_rows = []
    for item in report.get("robustness", {}).get("slippage_sensitivity", []):
        metrics = item.get("metrics", {})
        sensitivity_rows.append([
            _fmt(item.get("slippage_multiplier")), _fmt(item.get("spread_width_multiplier")),
            str(metrics.get("trade_count", 0)), _fmt(metrics.get("total_return"), percent=True),
            _fmt(metrics.get("annualized_sharpe")), _fmt(metrics.get("risk_adjusted_score")),
        ])
    sensitivity_table = _table(["Slippage x", "Spread width x", "Trades", "Return", "Sharpe", "Risk score"], sensitivity_rows)
    params = report.get("final_parameters", {})
    coverage = report.get("split", {}).get("holdout_regime_coverage", {})
    quality = report.get("validation", {})
    lines = [
        f"# Options research: {report.get('strategy')} ({report.get('run_id')})",
        "",
        f"Created: {report.get('created_at')}  ",
        f"Underlying: {report.get('underlying')}  ",
        f"Data mode: `{report.get('data_mode')}`  ",
        f"Validation status: **{'APPROVED FOR PAPER CONFIG EXPORT' if quality.get('approved') else 'RESEARCH ONLY'}**",
        "",
        "## Validation metrics",
        "",
        metrics_table,
        "",
        f"Walk-forward efficiency (OOS/IS risk score): **{_fmt(report.get('walk_forward', {}).get('walk_forward_efficiency'))}**  ",
        f"Parameter stability score: **{_fmt(report.get('parameter_stability', {}).get('stability_score'))}**  ",
        f"Minimum OOS trades met: **{quality.get('minimum_oos_trades_met')}**  ",
        f"Holdout coverage met: **{coverage.get('meets_minimum_coverage')}**  ",
        f"Stress costs passed: **{quality.get('stress_case_positive_after_costs')}**  ",
        f"Parameter stability gate passed: **{quality.get('parameter_stability_gate_met')}**  ",
        f"Gamma/vega concentration gate passed: **{quality.get('returns_not_dominated_by_gamma_or_vega_episodes')}**",
        "",
        "## Selected configuration",
        "",
        "```json",
        json.dumps({"parameters": params, "allowed_regimes": report.get("final_allowed_regimes", [])}, indent=2, sort_keys=True),
        "```",
        "",
        "## Candidate ledger",
        "",
        "Candidates are ranked on training windows; OOS results are shown only for configurations selected using prior training data.",
        "Per-candidate rolling metrics, curve summaries, Greek summaries, regime breakdowns, and selected OOS reports are in `candidate_reports.json`.",
        "",
        candidate_table,
        "",
        "## OOS equity curve and Greeks",
        "",
        f"Daily OOS equity observations: {len(report.get('walk_forward', {}).get('equity_curve', []))}. The full daily curve is in `equity_curve.csv`.  ",
        f"Peak |delta notional|: {_fmt(greek.get('peak_abs_delta_notional'))}; mean: {_fmt(greek.get('mean_abs_delta_notional'))}.  ",
        f"Peak gamma P&L for a 1% spot move: {_fmt(greek.get('peak_abs_gamma_pnl_1pct'))}; mean absolute: {_fmt(greek.get('mean_abs_gamma_pnl_1pct'))}.  ",
        f"Peak vega P&L per vol point: {_fmt(greek.get('peak_abs_vega_per_vol_point'))}; mean absolute: {_fmt(greek.get('mean_abs_vega_per_vol_point'))}.  ",
        f"P&L attribution: `{json.dumps(greek.get('pnl_attribution', {}), sort_keys=True)}`.",
        "",
        "## OOS market-regime breakdown",
        "",
        regime_table,
        "",
        "## OOS volatility-state breakdown",
        "",
        vol_table,
        "",
        "## Robustness",
        "",
        sensitivity_table,
        "",
        f"Assignment sensitivity: `{json.dumps(report.get('robustness', {}).get('assignment_sensitivity', {}), sort_keys=True)}`  ",
        f"Trade-sequence reshuffle: `{json.dumps(report.get('robustness', {}).get('trade_sequence_reshuffle', {}), sort_keys=True)}`  ",
        f"Gamma/vega episode dominance flagged: **{oos.get('returns_dominated_by_gamma_or_vega_episodes')}**",
        "",
        "## Data, execution, and limitations",
        "",
        f"Dataset SHA-256: `{report.get('dataset_sha256')}`. Rows with missing chains, stale quotes, wide spreads, and missing/low open interest are skipped and counted.  ",
        "Every leg is filled away from mid by the configured fraction of its displayed spread; commissions apply at entry and exit. Assignment is a simplified American-style exercise heuristic and pin-risk simulation.  ",
        "Model-generated chains must be labeled as such and do not replace historical chain snapshots, corporate-action/dividend records, or observed multi-leg fills. Results are research evidence, not live sizing approval.",
        "",
    ]
    (run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


__all__ = ["write_research_report"]
