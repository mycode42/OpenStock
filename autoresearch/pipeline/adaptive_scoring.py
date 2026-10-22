"""Replay metric calculation and candidate fitness scoring."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import statistics

from .adaptive_config import (
    ADAPTIVE_REGIMES, DEFAULT_FITNESS_WEIGHTS,
)

def _metrics(report: dict, *, capital: float) -> dict:
    curve = report.get("daily_equity") or []
    returns = _daily_returns(curve, capital)
    downside = [value for value in returns if value < 0]
    downside_deviation = math.sqrt(sum(value * value for value in downside) / len(downside)) if downside else 0.0
    mean_return = statistics.mean(returns) if returns else 0.0
    sortino = math.sqrt(252) * mean_return / downside_deviation if downside_deviation > 0 else None
    raw = {
        key: report.get(key) for key in (
            "net_return_pct", "max_drawdown_pct", "closed_trade_count", "entry_count",
            "win_rate_pct", "profit_factor", "daily_sharpe_annualized", "final_equity",
            "excess_return_vs_buy_hold_pct_points", "open_position_count",
        )
    }
    trades = report.get("closed_trades") or []
    traded_notional = 0.0
    for trade in trades:
        try:
            traded_notional += abs(float(trade.get("qty") or 0.0) * float(trade.get("entry_price") or 0.0))
        except (TypeError, ValueError):
            continue
    raw["turnover_pct_of_initial_capital"] = round(100.0 * traded_notional / capital, 4) if capital > 0 else None
    raw["sortino_annualized"] = round(sortino, 4) if sortino is not None else None
    return raw

def _metrics_by_regime(
    report: dict, *, capital: float, weights: dict[str, float], min_trades: int,
    multi_factor_diagnostics: dict | None = None,
) -> dict:
    """Score daily replay returns and closed trades in their point-in-time regime."""
    rows = report.get("daily_equity") or []
    trades_by_regime: dict[str, list[dict]] = {regime: [] for regime in ADAPTIVE_REGIMES}
    for trade in report.get("closed_trades") or []:
        regime = str(trade.get("entry_regime") or "unknown")
        if regime in trades_by_regime:
            trades_by_regime[regime].append(trade)

    returns_by_regime: dict[str, list[float]] = {regime: [] for regime in ADAPTIVE_REGIMES}
    previous = float(capital)
    for row in rows:
        try:
            equity = float(row.get("equity"))
        except (TypeError, ValueError):
            continue
        regime = str(row.get("regime") or "unknown")
        if regime in returns_by_regime and previous > 0 and equity > 0:
            returns_by_regime[regime].append(equity / previous - 1.0)
        if equity > 0:
            previous = equity

    result = {}
    factor_ic_by_regime = (multi_factor_diagnostics or {}).get(
        "net_of_costs_ic_forward_21_sessions_by_regime", {},
    )
    factor_return_by_regime = (multi_factor_diagnostics or {}).get(
        "after_cost_target_portfolio_forward_21_session_return_by_regime", {},
    )
    for regime in ADAPTIVE_REGIMES:
        values = returns_by_regime[regime]
        equity = float(capital)
        curve = []
        for index, daily_return in enumerate(values):
            equity *= 1.0 + daily_return
            curve.append({"date": str(index), "equity": equity})
        trades = trades_by_regime[regime]
        metrics = _metrics({"daily_equity": curve}, capital=capital)
        pnls = [float(row.get("pnl") or 0.0) for row in trades]
        gross_profit = sum(value for value in pnls if value > 0)
        gross_loss = -sum(value for value in pnls if value < 0)
        metrics.update({
            "closed_trade_count": len(trades),
            "entry_count": len(trades),
            "net_realized_pnl_after_costs": round(sum(pnls), 2),
            "turnover_pct_of_initial_capital": round(
                100.0 * sum(abs(float(row.get("qty") or 0.0) * float(row.get("entry_price") or 0.0)) for row in trades) / capital,
                4,
            ) if capital > 0 else None,
            "win_rate_pct": round(100.0 * sum(value > 0 for value in pnls) / len(pnls), 4) if pnls else None,
            "profit_factor": round(gross_profit / gross_loss, 6) if gross_loss > 0 else (None if not gross_profit else "inf"),
            "regime_session_count": len(values),
        })
        fitness, components = _fitness(metrics, weights)
        count_ok = len(trades) >= min_trades and len(values) >= 15
        positive = float(metrics.get("net_return_pct") or 0.0) > 0
        factor_evidence = None
        if multi_factor_diagnostics is not None:
            ic = factor_ic_by_regime.get(regime) or {}
            economic = factor_return_by_regime.get(regime) or {}
            ic_mean = float(ic.get("mean") or 0.0)
            ic_positive_pct = float(ic.get("positive_sample_pct") or 0.0)
            ic_count = int(ic.get("sample_count") or 0)
            economic_mean = float(economic.get("mean") or 0.0)
            factor_evidence = bool(
                ic_count >= 20 and ic_mean > 0 and ic_positive_pct >= 55.0
                and economic_mean > 0
            )
            metrics["multi_factor_oos"] = {
                "net_of_costs_ic_mean": round(ic_mean, 6),
                "net_of_costs_ic_positive_sample_pct": round(ic_positive_pct, 2),
                "net_of_costs_ic_observation_count": ic_count,
                "after_cost_target_portfolio_return_mean": round(economic_mean, 6),
                "minimum_factor_evidence_met": factor_evidence,
            }
        result[regime] = {
            "metrics": metrics,
            "fitness": round(fitness, 8),
            "fitness_components": {key: round(value, 8) for key, value in components.items()},
            "eligible": bool(count_ok and positive and factor_evidence is not False),
            "minimum_sample_met": bool(count_ok),
            "positive_after_costs": bool(positive),
            "minimum_factor_evidence_met": factor_evidence,
        }
    return result

def _cost_model_version(args) -> str:
    descriptor = {
        "version": "algo-replay-cost-v1",
        "slippage_bps_each_side": float(args.slippage_bps),
        "spread_bps_full": float(args.spread_bps),
        "commission_per_share_each_side": float(args.commission_per_share),
        "short_borrow_annual_pct": float(args.short_borrow_annual_pct),
        "unknown_borrow_penalty_bps": float(args.unknown_borrow_penalty_bps),
    }
    encoded = json.dumps(descriptor, sort_keys=True, separators=(",", ":"))
    return f"{descriptor['version']}:{hashlib.sha256(encoded.encode()).hexdigest()[:12]}"

def _daily_returns(curve: list[dict], initial_equity: float) -> list[float]:
    returns = []
    previous = float(initial_equity)
    for row in curve:
        current = float(row.get("equity", 0) or 0)
        if previous > 0 and current > 0:
            returns.append(current / previous - 1)
        previous = current
    return returns

def _fitness(metrics: dict, weights: dict[str, float]) -> tuple[float, dict[str, float]]:
    sharpe = metrics.get("daily_sharpe_annualized")
    sortino = metrics.get("sortino_annualized")
    pf = metrics.get("profit_factor")
    try:
        pf_value = float(pf)
    except (TypeError, ValueError):
        pf_value = 0.0
    if not math.isfinite(pf_value):
        pf_component = 1.0 if pf_value > 0 else -1.0
    elif pf_value <= 0:
        pf_component = -1.0
    else:
        pf_component = math.tanh(math.log(pf_value))
    components = {
        "sharpe": max(-1.0, min(1.0, float(sharpe or 0) / 5.0)),
        "sortino": max(-1.0, min(1.0, float(sortino or 0) / 5.0)),
        "return": math.tanh(float(metrics.get("net_return_pct") or 0) / 10.0),
        "win_rate": (float(metrics["win_rate_pct"]) / 100.0 - 0.5) * 2.0
        if metrics.get("win_rate_pct") is not None else 0.0,
        "profit_factor": pf_component,
        "drawdown": -min(1.0, max(0.0, float(metrics.get("max_drawdown_pct") or 0) / 100.0)),
    }
    return sum(weights[name] * components[name] for name in weights), components

def _normalize_weights(value: str | dict | None) -> dict[str, float]:
    if value is None:
        weights = dict(DEFAULT_FITNESS_WEIGHTS)
    elif isinstance(value, dict):
        weights = dict(value)
    else:
        raw = value
        if raw.lstrip().startswith("{"):
            weights = json.loads(raw)
        else:
            candidate = Path(raw)
            if candidate.is_file():
                weights = json.loads(candidate.read_text())
            else:
                weights = json.loads(raw)
    if not isinstance(weights, dict) or set(weights) - set(DEFAULT_FITNESS_WEIGHTS):
        raise ValueError("fitness weights must be a JSON object using sharpe, sortino, return, win_rate, profit_factor, and drawdown")
    result = {key: float(weights.get(key, 0.0)) for key in DEFAULT_FITNESS_WEIGHTS}
    if any(not math.isfinite(item) or item < 0 for item in result.values()) or sum(result.values()) <= 0:
        raise ValueError("fitness weights must be finite non-negative values with a positive total")
    total = sum(result.values())
    return {key: item / total for key, item in result.items()}

__all__ = [
    "_cost_model_version", "_daily_returns", "_fitness", "_metrics",
    "_metrics_by_regime", "_normalize_weights",
]
