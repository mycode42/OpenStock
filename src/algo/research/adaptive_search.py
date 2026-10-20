"""Candidate scoring, parameter search, replay preparation, and fit-window selection."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
import itertools
import json
import logging
import math
from pathlib import Path
import random
import statistics
import hashlib
from concurrent.futures import ProcessPoolExecutor
from zoneinfo import ZoneInfo

from src.algo.backtest import AlgoPortfolioBacktest, strategy_warmup_days
from src.algo.backtest.config import REGIME_WARMUP_DAYS
from src.algo.market_store import SQLiteBarStore
from src.algo.cpu import available_cpu_count
from src.algo.backtest.execution import build_replay_timeline
from src.algo.strategies.multi_factor.features import aggregate_intraday_bars
from src.algo.strategies.multi_factor.config import REGIME_FACTOR_WEIGHT_PROFILES
from src.algo.strategies.registry import ALGO_DEFAULT_PARAMETERS, PARAMETER_KEYS, validate_parameter_profile
from src.algo.strategies.universe import configured_universe
from .candidate_archive import candidate_id, logic_version, model_family
from .replay_logging import quiet_coordinator_rejections
from .settings import (
    ADJUSTMENT, CORE_RESEARCH_CONTROLS, FEED, MULTI_FACTOR_MIN_UNIVERSE_VALUES,
    RESEARCH_START, RESEARCH_WARMUP_DAYS, SIP_EXTENDED_LOCAL_FEED,
    REGIME_CONTEXT_WARMUP_DAYS,
)

ET = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)
_CANDIDATE_REPLAY_CONTEXT: dict | None = None
MULTI_FACTOR_EQUAL_ABLATION_WEIGHTS = {
    name: 0.1 for name in (
        "momentum_1m", "momentum_3m", "momentum_6m", "low_volatility",
        "price_volume_momentum", "value_earnings_yield", "value_book_to_price",
        "value_fcf_yield", "quality_profitability", "quality_balance_sheet",
    )
}
MULTI_FACTOR_MIN_NET_IC_IMPROVEMENT = 0.005

DEFAULT_FITNESS_WEIGHTS = {
    "sharpe": 0.25,
    "sortino": 0.15,
    "return": 0.25,
    "win_rate": 0.10,
    "profit_factor": 0.15,
    "drawdown": 0.10,
}

ADAPTIVE_REGIMES = ("trending_up", "trending_down", "range_bound", "volatile")

DEFAULT_SEARCH_SPACES: dict[str, dict[str, list]] = {
    "mean_reversion": {
        "signal_model": ["opening_dip", "bollinger"],
        "minimum_dip_pct": [0.010, 0.014, 0.020],
        "minimum_bounce_pct": [0.001, 0.002, 0.003],
        "minimum_volume_ratio": [1.0, 1.3, 1.7],
        "entry_window_minutes": [30, 60, 90],
        "window": [15, 20, 30], "band_width": [1.8, 2.0, 2.4],
        "entry_rsi": [25, 30, 35], "exit_rsi": [55, 58, 62],
        "regime_filter": ["none", "er", "adx", "combined"],
        "threshold_mode": ["fixed", "volatility_regime_adjusted"],
        "efficiency_ratio_max": [0.25, 0.35, 0.45], "adx_max": [16.0, 20.0, 24.0],
    },
    "trend_following": {
        "average_type": ["sma", "ema", "kama"], "stop_mode": ["fixed_pct", "atr"],
        "atr_stop_multiple": [2.0, 2.5, 3.0], "fast_period": [10, 15, 20, 25, 30],
        "slow_period": [40, 50, 60, 70, 80], "minimum_slope_pct": [0.0, 0.001, 0.0025, 0.005],
    },
    "momentum": {
        "momentum_mode": ["cross_sectional", "time_series"],
        "lookbacks": [
            [5, 10, 20], [10, 20, 40], [10, 20, 60],
            [21, 63, 126, 252], [5, 21, 63, 126, 252],
        ],
        "top_n": [2, 3, 4], "minimum_score_spread": [0.02, 0.04, 0.06, 0.08],
        "minimum_time_series_return": [0.0, 0.02, 0.04],
    },
    "swing_mean_reversion": {
        "lookback_days": [10, 15, 20, 25], "stretch_pct": [0.05, 0.07, 0.08, 0.10, 0.12],
        "minimum_stretch_days": [2, 3, 4], "use_rsi_confirmation": [True, False],
        "long_rsi_max": [25, 30, 35], "short_rsi_min": [65, 70, 75],
        "regime_filter": ["none", "er", "adx", "combined"],
        "entry_distance_mode": ["fixed_pct", "standardized"],
        "lookback_mode": ["fixed", "volatility_adaptive"],
        "stretch_zscore": [1.25, 1.75, 2.25],
    },
    "multi_factor": {
        "rebalance_frequency": ["daily", "weekly"], "top_n": [1, 2, 3, 4],
        "min_universe": list(MULTI_FACTOR_MIN_UNIVERSE_VALUES),
        "minimum_score": [0.0, 0.05, 0.1, 0.2],
        "direction_mode": ["long_only", "regime_dependent", "long_short"],
        "factor_neutralization": ["none", "sector"],
        "portfolio_weight_method": ["equal", "shrinkage_min_variance"],
        "regime_factor_weights": [{}, REGIME_FACTOR_WEIGHT_PROFILES],
        "factor_weights": [
            {"momentum_1m": 0.30, "momentum_3m": 0.30, "momentum_6m": 0.20, "low_volatility": 0.20},
            {"momentum_1m": 0.20, "momentum_3m": 0.30, "momentum_6m": 0.30, "low_volatility": 0.20},
            {"momentum_1m": 0.10, "momentum_3m": 0.25, "momentum_6m": 0.45, "low_volatility": 0.20},
            {"momentum_1m": 0.0, "momentum_3m": 0.0, "momentum_6m": 1.0},
            {"momentum_1m": 0.0, "momentum_3m": 0.50, "momentum_6m": 0.30, "low_volatility": 0.20},
            {"momentum_1m": 0.22, "momentum_3m": 0.27, "momentum_6m": 0.21, "low_volatility": 0.12, "price_volume_momentum": 0.18},
            {"price_volume_momentum": 1.0},
            {"low_volatility": 1.0},
            {"value_earnings_yield": 0.5, "value_book_to_price": 0.25, "value_fcf_yield": 0.25},
            {"quality_profitability": 0.5, "quality_balance_sheet": 0.5},
        ],
    },
}

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
            "profit_factor": round(gross_profit / gross_loss, 6) if gross_loss > 0 else (None if not gross_profit else float("inf")),
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

def _search_space(strategy: str, path: str | None) -> dict[str, list]:
    space = deepcopy(DEFAULT_SEARCH_SPACES[strategy])
    if path:
        supplied = json.loads(Path(path).read_text())
        if not isinstance(supplied, dict):
            raise ValueError("search-space JSON must map parameter names to non-empty value arrays")
        space.update(supplied)
    if not space or any(not isinstance(values, list) or not values for values in space.values()):
        raise ValueError("each search-space parameter must have a non-empty list of candidate values")
    # Searchable strategy parameters are broader than the fields explicitly
    # overridden in ALGO_DEFAULT_PARAMETERS (notably the intraday signal knobs).
    allowed = set(PARAMETER_KEYS[strategy])
    unknown = set(space) - allowed
    if unknown:
        raise ValueError(f"search space includes unsupported {strategy} parameters: {', '.join(sorted(unknown))}")
    return space

def _make_parameters(strategy: str, values: dict) -> dict:
    parameters = deepcopy(ALGO_DEFAULT_PARAMETERS[strategy])
    parameters.update(deepcopy(values))
    return validate_parameter_profile({strategy: parameters})[strategy]


def _planned_ablation_variants(strategy: str, baseline: dict) -> list[tuple[str, dict]]:
    """Build a small, ordered cohort with one-feature and planned combined variants."""
    if strategy == "mean_reversion":
        return [
            ("opening_dip_er_gate", {"signal_model": "opening_dip", "regime_filter": "er"}),
            ("opening_dip_adx_gate", {"signal_model": "opening_dip", "regime_filter": "adx"}),
            ("opening_dip_combined_gate", {"signal_model": "opening_dip", "regime_filter": "combined"}),
            ("bollinger_baseline", {"signal_model": "bollinger"}),
            ("er_gate", {"signal_model": "bollinger", "regime_filter": "er"}),
            ("adx_gate", {"signal_model": "bollinger", "regime_filter": "adx"}),
            ("combined_gate", {"signal_model": "bollinger", "regime_filter": "combined"}),
            ("volatility_threshold", {
                "signal_model": "bollinger", "threshold_mode": "volatility_regime_adjusted",
            }),
            ("combined_regime_volatility", {
                "signal_model": "bollinger", "regime_filter": "combined",
                "threshold_mode": "volatility_regime_adjusted",
            }),
        ]
    if strategy == "swing_mean_reversion":
        return [
            ("er_gate", {"regime_filter": "er"}),
            ("adx_gate", {"regime_filter": "adx"}),
            ("combined_gate", {"regime_filter": "combined"}),
            ("standardized_distance", {"entry_distance_mode": "standardized"}),
            ("adaptive_lookback", {"lookback_mode": "volatility_adaptive"}),
            ("combined_volatility", {
                "entry_distance_mode": "standardized", "lookback_mode": "volatility_adaptive",
            }),
            ("combined_regime_volatility", {
                "regime_filter": "combined", "entry_distance_mode": "standardized",
                "lookback_mode": "volatility_adaptive",
            }),
        ]
    if strategy in {"trend_following", "swing_trend_following"}:
        return [
            ("kama_average", {"average_type": "kama"}),
            ("atr_stop", {"stop_mode": "atr"}),
            ("kama_atr", {"average_type": "kama", "stop_mode": "atr"}),
        ]
    if strategy in {"momentum", "swing_momentum"}:
        return [
            ("cross_sectional_21_session", {
                "momentum_mode": "cross_sectional", "lookbacks": [21],
            }),
            ("cross_sectional_63_session", {
                "momentum_mode": "cross_sectional", "lookbacks": [63],
            }),
            ("cross_sectional_126_session", {
                "momentum_mode": "cross_sectional", "lookbacks": [126],
            }),
            ("cross_sectional_252_session", {
                "momentum_mode": "cross_sectional", "lookbacks": [252],
            }),
            ("cross_sectional_long_horizons", {
                "momentum_mode": "cross_sectional", "lookbacks": [21, 63, 126, 252],
            }),
            ("time_series_long_horizons", {
                "momentum_mode": "time_series", "lookbacks": [21, 63, 126, 252],
            }),
        ]
    if strategy == "multi_factor":
        return [
            ("all_factors_equal_weights", {
                "factor_weights": MULTI_FACTOR_EQUAL_ABLATION_WEIGHTS,
                "factor_neutralization": "none",
                "regime_factor_weights": {},
                "min_universe": 15,
            }),
            ("ridge_factor_set_equal_weights", {
                "factor_weights": MULTI_FACTOR_EQUAL_ABLATION_WEIGHTS,
                "factor_neutralization": "sector",
                "regime_factor_weights": {},
                "min_universe": 15,
            }),
            ("ridge_walk_forward_weights", {
                "factor_weight_mode": "ridge_walk_forward",
                "regime_factor_weights": {},
                "factor_neutralization": "sector",
                "min_universe": 15,
            }),
            ("momentum_only", {"factor_weights": {
                "momentum_1m": 0.33, "momentum_3m": 0.34, "momentum_6m": 0.33,
            }, "regime_factor_weights": {}}),
            ("low_volatility_only", {"factor_weights": {"low_volatility": 1.0}, "regime_factor_weights": {}}),
            ("price_volume_only", {"factor_weights": {"price_volume_momentum": 1.0}, "regime_factor_weights": {}}),
            ("value_only", {"factor_weights": {
                "value_earnings_yield": 0.5, "value_book_to_price": 0.25,
                "value_fcf_yield": 0.25,
            }, "regime_factor_weights": {}, "min_universe": 15}),
            ("quality_only", {"factor_weights": {
                "quality_profitability": 0.5, "quality_balance_sheet": 0.5,
            }, "regime_factor_weights": {}, "min_universe": 15}),
            ("earnings_yield_only", {"factor_weights": {"value_earnings_yield": 1.0}, "regime_factor_weights": {}, "min_universe": 15}),
            ("book_to_price_only", {"factor_weights": {"value_book_to_price": 1.0}, "regime_factor_weights": {}, "min_universe": 15}),
            ("free_cash_flow_yield_only", {"factor_weights": {"value_fcf_yield": 1.0}, "regime_factor_weights": {}, "min_universe": 15}),
            ("profitability_only", {"factor_weights": {"quality_profitability": 1.0}, "regime_factor_weights": {}, "min_universe": 15}),
            ("balance_sheet_quality_only", {"factor_weights": {"quality_balance_sheet": 1.0}, "regime_factor_weights": {}, "min_universe": 15}),
            ("sector_neutralized_equal_weights", {
                "factor_weights": MULTI_FACTOR_EQUAL_ABLATION_WEIGHTS,
                "factor_neutralization": "sector", "regime_factor_weights": {},
                "min_universe": 15,
            }),
            ("regime_timing", {
                "factor_weights": MULTI_FACTOR_EQUAL_ABLATION_WEIGHTS,
                "factor_neutralization": "sector",
                "regime_factor_weights": REGIME_FACTOR_WEIGHT_PROFILES,
                "min_universe": 15,
            }),
            ("shrinkage_min_variance", {"portfolio_weight_method": "shrinkage_min_variance"}),
            ("combined_shrinkage_min_variance", {
                "factor_weights": MULTI_FACTOR_EQUAL_ABLATION_WEIGHTS,
                "factor_neutralization": "sector", "regime_factor_weights": {},
                "min_universe": 15,
                "portfolio_weight_method": "shrinkage_min_variance",
            }),
        ]
    return []

def _random_values(space: dict[str, list], rng: random.Random) -> dict:
    return {key: deepcopy(rng.choice(values)) for key, values in space.items()}

def _flatten(value, prefix="") -> dict[str, object]:
    if isinstance(value, dict):
        output = {}
        for key, child in value.items():
            output.update(_flatten(child, f"{prefix}.{key}" if prefix else str(key)))
        return output
    if isinstance(value, (list, tuple)):
        return {prefix: list(value)}
    return {prefix: value}

def _distance(left: dict, right: dict, tuned_parameters: set[str] | None = None) -> float:
    first, second = _flatten(left), _flatten(right)
    keys = sorted(set(first) | set(second))
    if tuned_parameters is not None:
        keys = [key for key in keys if key.split(".", 1)[0] in tuned_parameters]
    if not keys:
        return 0.0
    distances = []
    for key in keys:
        a, b = first.get(key), second.get(key)
        if a == b:
            distances.append(0.0)
        elif isinstance(a, (int, float)) and not isinstance(a, bool) and isinstance(b, (int, float)) and not isinstance(b, bool):
            scale = max(abs(float(a)), abs(float(b)), 1e-12)
            distances.append(min(1.0, abs(float(a) - float(b)) / scale))
        else:
            distances.append(1.0)
    return statistics.mean(distances)

def _bayes_proposal(space: dict[str, list], history: list[dict], seen: set[str], rng: random.Random) -> dict:
    pool = []
    pool_keys = set()
    for _ in range(160):
        proposal = _random_values(space, rng)
        key = json.dumps(proposal, sort_keys=True, separators=(",", ":"))
        if key not in seen and key not in pool_keys:
            pool.append(proposal)
            pool_keys.add(key)
    if not pool:
        return {}
    if len(history) < 4:
        return pool[0]

    def acquisition(candidate: dict) -> float:
        weights = [math.exp(-6.0 * _distance(candidate, row["parameters"], set(space))) for row in history]
        total = sum(weights)
        predicted = sum(weight * row["fitness"] for weight, row in zip(weights, history)) / total if total else 0.0
        uncertainty = 1.0 / math.sqrt(max(total, 1e-9))
        return predicted + 0.15 * uncertainty

    return max(pool, key=acquisition)

def _candidate_parameters(
    strategy: str, method: str, count: int, seed: int, space: dict[str, list],
    parent_parameters: dict | list[dict] | None = None,
    excluded_candidate_ids: set[str] | None = None,
) -> list[dict]:
    """Produce a bounded parameter bank, prioritizing variants unseen on these bars."""
    if count < 1 or count > 512:
        raise ValueError("candidate count must be between 1 and 512")
    baseline = deepcopy(ALGO_DEFAULT_PARAMETERS[strategy])
    excluded = set(excluded_candidate_ids or ())

    def previously_evaluated(parameters: dict) -> bool:
        return candidate_id(strategy, parameters) in excluded

    baseline = validate_parameter_profile({strategy: baseline})[strategy]
    output = [{
        "name": "baseline", "parameters": baseline,
        "model_family": model_family(strategy, baseline),
        "logic_version": logic_version(strategy),
    }]
    seen = {json.dumps(baseline, sort_keys=True, separators=(",", ":"))}
    rng = random.Random(seed)
    baseline_id = candidate_id(strategy, baseline)
    for name, changes in _planned_ablation_variants(strategy, baseline):
        if len(output) >= count:
            break
        variation = deepcopy(baseline)
        variation.update(deepcopy(changes))
        try:
            parameters = _make_parameters(strategy, variation)
        except ValueError:
            continue
        key = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
        if key in seen or previously_evaluated(parameters):
            continue
        seen.add(key)
        output.append({
            "name": f"ablation:{name}", "parameters": parameters,
            "model_family": model_family(strategy, parameters),
            "logic_version": logic_version(strategy),
            "parent_candidate_id": baseline_id,
        })
    if strategy == "multi_factor" and len(output) < count:
        for min_universe in space.get("min_universe", []):
            variation = deepcopy(baseline)
            variation["min_universe"] = min_universe
            try:
                parameters = _make_parameters(strategy, variation)
            except ValueError:
                continue
            key = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
            if key in seen or previously_evaluated(parameters):
                continue
            seen.add(key)
            output.append({
                "name": f"min_universe_{min_universe}", "parameters": parameters,
            })
            if len(output) >= count:
                break
    raw_parents = parent_parameters if isinstance(parent_parameters, list) else [parent_parameters]
    parents = []
    for raw in raw_parents:
        if not isinstance(raw, dict):
            continue
        try:
            parent = validate_parameter_profile({strategy: raw})[strategy]
        except ValueError:
            continue
        if parent not in parents:
            parents.append(parent)
    if parents and len(output) < count:
        for index, parent in enumerate(parents[:max(1, count // 4)]):
            key = json.dumps(parent, sort_keys=True, separators=(",", ":"))
            if key not in seen and not previously_evaluated(parent):
                seen.add(key)
                output.append({
                    "name": f"archive_{index + 1:02d}", "parameters": parent,
                    "model_family": model_family(strategy, parent),
                    "logic_version": logic_version(strategy),
                })
        # Spend a stable fraction of the budget on neighboring variants from
        # several archived elites; reserve the rest for independent exploration.
        derivative_limit = min(count, max(len(output), count * 2 // 3))
        for parameter, values in space.items():
            for parent_index, parent in enumerate(parents):
                for value in values:
                    if len(output) >= derivative_limit:
                        break
                    if parent.get(parameter) == value:
                        continue
                    variation = deepcopy(parent)
                    variation[parameter] = deepcopy(value)
                    try:
                        parameters = _make_parameters(strategy, variation)
                    except ValueError:
                        continue
                    key = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
                    if key in seen or previously_evaluated(parameters):
                        continue
                    seen.add(key)
                    output.append({
                        "name": f"archive_{parent_index + 1:02d}_variant_{len(output):03d}",
                        "parameters": parameters,
                        "model_family": model_family(strategy, parameters),
                        "logic_version": logic_version(strategy),
                        "parent_candidate_id": candidate_id(strategy, parent),
                    })
                if len(output) >= derivative_limit:
                    break
            if len(output) >= derivative_limit:
                break
    if method == "grid":
        combinations = itertools.product(*(space[key] for key in space))
        for row in combinations:
            values = dict(zip(space, row))
            try:
                parameters = _make_parameters(strategy, values)
            except ValueError:
                continue
            key = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
            if key in seen or previously_evaluated(parameters):
                continue
            seen.add(key)
            output.append({"name": f"grid_{len(output):03d}", "parameters": parameters})
            if len(output) >= count:
                break
    elif method == "random":
        attempts = 0
        while len(output) < count and attempts < count * 40:
            attempts += 1
            try:
                parameters = _make_parameters(strategy, _random_values(space, rng))
            except ValueError:
                continue
            key = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
            if key in seen or previously_evaluated(parameters):
                continue
            seen.add(key)
            output.append({"name": f"random_{len(output):03d}", "parameters": parameters})
    elif method != "bayes":
        raise ValueError("search method must be grid, random, or bayes")
    return output[:count]

def _baseline_improvement(search: dict) -> dict:
    """Return whether the selected result improves on baseline after costs."""
    candidates = search.get("candidates") or []
    baseline = next((row for row in candidates if row.get("name") == "baseline"), None)
    selected = search.get("selected_candidate") or {}
    baseline_fitness = float((baseline or {}).get("fitness") or 0.0)
    selected_fitness = float(selected.get("fitness") or 0.0)
    improved = bool(
        baseline is not None
        and selected.get("name") != "baseline"
        and selected.get("eligible")
        and selected_fitness > baseline_fitness + 1e-9
    )
    return {
        "improved": improved,
        "baseline_candidate": baseline,
        "baseline_fitness": baseline_fitness,
        "selected_fitness": selected_fitness,
        "fitness_delta": round(selected_fitness - baseline_fitness, 8),
    }

def _ordered_window_options(options: list[int], preferred: int | None = None) -> list[int]:
    """Start from the previous winner or the middle option, then try neighbors."""
    if not options:
        raise ValueError("at least one adaptive window length is required")
    unique = sorted(set(int(value) for value in options))
    first = preferred if preferred in unique else unique[len(unique) // 2]
    return [first, *sorted((value for value in unique if value != first), key=lambda value: (abs(value - first), value))]

def _load_window_data(
    *, db: str, symbols: list[str], start: date, end: date, adjustment: str,
    strategy: str,
) -> tuple[dict[str, list[dict]], list[dict]]:
    store = SQLiteBarStore(db)
    warmup = max(strategy_warmup_days(strategy), REGIME_CONTEXT_WARMUP_DAYS)
    warm_start = (start - timedelta(days=warmup)).isoformat()
    context_symbols = list(dict.fromkeys([*symbols, "QQQ", "GLD", "TLT", "HYG", "LQD"]))
    bars = store.bars(
        context_symbols, feed=FEED, timeframe="5Min", start=warm_start,
        end=end.isoformat(), adjustment=adjustment,
    )
    spy = store.bars(
        ["SPY"], feed=FEED, timeframe="1Day",
        start=(start - timedelta(days=REGIME_WARMUP_DAYS)).isoformat(),
        end=end.isoformat(), adjustment=adjustment,
    ).get("SPY", [])
    return bars, spy


def _load_forward_window_data(
    *, db: str, symbols: list[str], start: date, end: date, adjustment: str,
    strategy: str,
) -> tuple[dict[str, list[dict]], list[dict], dict[str, list[dict]]]:
    """Load regular bars plus any stored SIP extended and BOATS session bars."""
    regular_bars, spy = _load_window_data(
        db=db, symbols=symbols, start=start, end=end,
        adjustment=adjustment, strategy=strategy,
    )
    store = SQLiteBarStore(db)
    warmup = max(strategy_warmup_days(strategy), REGIME_CONTEXT_WARMUP_DAYS)
    warm_start = (start - timedelta(days=warmup + 1)).isoformat()
    session_end = (end + timedelta(days=1)).isoformat()
    context_symbols = [symbol for symbol in regular_bars if symbol not in symbols]
    merged = {
        symbol: list(regular_bars.get(symbol, []))
        for symbol in dict.fromkeys([*symbols, *context_symbols])
    }
    for rows in merged.values():
        for bar in rows:
            bar["_source_feed"] = FEED
    for feed in (SIP_EXTENDED_LOCAL_FEED, "boats"):
        extra = store.bars(
            symbols, feed=feed, timeframe="5Min", start=warm_start,
            end=session_end, adjustment=adjustment,
        )
        for symbol, rows in extra.items():
            for bar in rows:
                bar["_source_feed"] = feed
            merged.setdefault(symbol, []).extend(rows)
    for symbol, rows in merged.items():
        by_timestamp = {}
        # Regular SIP records win if an upstream feed has overlapping timestamps.
        for bar in regular_bars.get(symbol, []):
            by_timestamp[str(bar["t"])] = bar
        for bar in rows:
            by_timestamp.setdefault(str(bar["t"]), bar)
        merged[symbol] = sorted(by_timestamp.values(), key=lambda bar: str(bar["t"]))
    return merged, spy, regular_bars

def _profile_for(strategy: str, parameters: dict) -> dict:
    controls = deepcopy(CORE_RESEARCH_CONTROLS)
    if strategy == "multi_factor":
        per_strategy = controls.setdefault("max_entries_per_strategy_per_day", {})
        per_strategy["multi_factor"] = min(int(per_strategy.get("multi_factor", 2)), 2)
    return {
        "name": "adaptive_candidate", "parameters": {strategy: parameters},
        "strategies": [strategy], "controls": controls,
    }

def _run_replay(
    *, strategy: str, parameters: dict, symbols: list[str], bars: dict,
    spy_daily: list[dict], start: date, end: date, args,
    capital: float | None = None,
    regime_profiles: dict[str, dict[str, dict]] | None = None,
    force_close_at_end: bool = True,
    preaggregated_daily_bars: dict[str, list[dict]] | None = None,
    precomputed_timeline: dict[datetime, dict[str, dict]] | None = None,
    include_extended_hours: bool = False,
) -> dict:
    profile = _profile_for(strategy, parameters)
    replay = AlgoPortfolioBacktest(
        symbols=symbols, intraday_bars=bars, spy_daily_bars=spy_daily,
        start=start.isoformat(), end=end.isoformat(), feed=FEED,
        data_adjustment=args.adjustment, initial_capital=float(capital or args.capital),
        slippage_bps=args.slippage_bps, spread_bps=args.spread_bps,
        commission_per_share=args.commission_per_share,
        short_borrow_annual_pct=args.short_borrow_annual_pct / 100,
        unknown_borrow_penalty_bps=args.unknown_borrow_penalty_bps,
        execution_delay_bars=int(getattr(args, "execution_delay_bars", 0)),
        borrow_availability=args.borrow_availability_data,
        regime_macro_series=getattr(args, "regime_macro_series", None),
        regime_config=getattr(args, "regime_config", None),
        profile=profile["parameters"], strategies=profile["strategies"],
        research_controls=profile["controls"], force_close_at_end=force_close_at_end,
        regime_profiles=regime_profiles,
        preaggregated_daily_bars=preaggregated_daily_bars,
        precomputed_timeline=precomputed_timeline,
        include_extended_hours=include_extended_hours,
        factor_weight_timeline=getattr(args, "factor_weight_timeline", None),
    )
    with quiet_coordinator_rejections():
        return replay.run()

def _candidate_replay_worker_init(context: dict) -> None:
    """Install read-only replay inputs once in each candidate worker."""
    global _CANDIDATE_REPLAY_CONTEXT
    _CANDIDATE_REPLAY_CONTEXT = context
    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
        handler.close()
    root_logger.addHandler(logging.NullHandler())
    root_logger.setLevel(logging.CRITICAL)
    logging.getLogger("strategy_runner").setLevel(logging.CRITICAL)

def _evaluate_search_candidate(candidate: dict, context: dict) -> dict:
    report = _run_replay(
        strategy=context["strategy"], parameters=candidate["parameters"],
        symbols=context["symbols"], bars=context["bars"],
        spy_daily=context["spy_daily"], start=context["start"], end=context["end"],
        args=context["args"],
        preaggregated_daily_bars=context["preaggregated_daily_bars"],
        precomputed_timeline=context["precomputed_timeline"],
    )
    metrics = _metrics(report, capital=context["args"].capital)
    fitness, components = _fitness(metrics, context["weights"])
    factor_evidence = None
    factor_net_ic = None
    factor_net_return = None
    if context["strategy"] == "multi_factor":
        diagnostics = report.get("multi_factor_diagnostics") or {}
        net_ic = diagnostics.get("net_of_costs_ic_forward_21_sessions") or {}
        net_return = diagnostics.get("after_cost_target_portfolio_forward_21_session_return") or {}
        factor_net_ic = float(net_ic.get("mean") or 0.0)
        factor_net_return = float(net_return.get("mean") or 0.0)
        ic_count = int(net_ic.get("sample_count") or 0)
        positive_ic_pct = float(net_ic.get("positive_sample_pct") or 0.0)
        ic_component = math.tanh(factor_net_ic / 0.05)
        stability_component = max(-1.0, min(1.0, (positive_ic_pct - 50.0) / 50.0))
        economic_component = math.tanh(factor_net_return / 0.02)
        factor_fitness = 0.40 * ic_component + 0.25 * stability_component + 0.35 * economic_component
        fitness = 0.5 * fitness + 0.5 * factor_fitness
        components.update({
            "net_factor_ic": ic_component,
            "factor_ic_stability": stability_component,
            "after_cost_factor_return": economic_component,
        })
        factor_evidence = bool(
            ic_count >= 20 and factor_net_ic > 0
            and positive_ic_pct >= 55.0 and factor_net_return > 0
        )
        metrics["multi_factor_oos"] = {
            "net_of_costs_ic_mean": round(factor_net_ic, 6),
            "net_of_costs_ic_positive_sample_pct": round(positive_ic_pct, 2),
            "net_of_costs_ic_observation_count": ic_count,
            "ic_horizon_sessions": diagnostics.get("ic_horizon_sessions"),
            "ic_labels_overlap": diagnostics.get("ic_labels_overlap"),
            "after_cost_target_portfolio_return_mean": round(factor_net_return, 6),
            "minimum_factor_evidence_met": factor_evidence,
        }
    regime_metrics = _metrics_by_regime(
        report, capital=context["args"].capital,
        weights=context["weights"], min_trades=context["min_trades"],
        multi_factor_diagnostics=(report.get("multi_factor_diagnostics") if context["strategy"] == "multi_factor" else None),
    )
    enough_trades = int(metrics.get("closed_trade_count") or 0) >= context["min_trades"]
    positive_after_costs = float(metrics.get("net_return_pct") or 0) > 0
    if context["strategy"] == "multi_factor":
        positive_after_costs = positive_after_costs and bool(factor_net_return and factor_net_return > 0)
    del report
    return {
        "name": candidate["name"], "parameters": candidate["parameters"],
        "model_family": candidate.get("model_family") or model_family(context["strategy"], candidate["parameters"]),
        "logic_version": candidate.get("logic_version") or logic_version(context["strategy"]),
        "parent_candidate_id": candidate.get("parent_candidate_id"),
        "cost_model_version": _cost_model_version(context["args"]),
        "metrics": metrics, "fitness": round(fitness, 8), "_fitness_raw": fitness,
        "fitness_components": {key: round(value, 8) for key, value in components.items()},
        "regime_metrics": regime_metrics,
        "eligible": enough_trades and positive_after_costs and (factor_evidence is not False),
        "minimum_factor_evidence_met": factor_evidence,
        "minimum_trade_count_met": enough_trades,
        "positive_after_costs": positive_after_costs,
    }

def _evaluate_candidate_in_worker(candidate: dict) -> dict:
    if _CANDIDATE_REPLAY_CONTEXT is None:
        raise RuntimeError("candidate replay worker was not initialized")
    return _evaluate_search_candidate(candidate, _CANDIDATE_REPLAY_CONTEXT)


def _apply_multi_factor_ic_improvement_gate(candidates: list[dict], strategy: str) -> None:
    """Require changed factor signals to beat baseline net IC by a margin."""
    if strategy != "multi_factor":
        return
    baseline = next((row for row in candidates if row.get("name") == "baseline"), None)
    baseline_oos = ((baseline or {}).get("metrics") or {}).get("multi_factor_oos") or {}
    baseline_ic = float(baseline_oos.get("net_of_costs_ic_mean") or 0.0)
    baseline_regimes = (baseline or {}).get("regime_metrics") or {}
    score_parameters = (
        "factor_weights", "factor_neutralization", "regime_factor_weights", "factor_weight_mode",
        "momentum_lookbacks", "volatility_lookback", "reversal_lookback",
        "liquidity_lookback", "minimum_price", "minimum_average_dollar_volume",
        "minimum_history_sessions", "maximum_bar_age_days", "winsor_quantile",
        "min_universe",
    )
    baseline_parameters = (baseline or {}).get("parameters") or {}
    for row in candidates:
        if row.get("name") == "baseline":
            row["net_ic_improvement_vs_baseline"] = None
            continue
        metrics = row.setdefault("metrics", {})
        oos = metrics.get("multi_factor_oos") or {}
        candidate_ic = float(oos.get("net_of_costs_ic_mean") or 0.0)
        delta = candidate_ic - baseline_ic
        candidate_parameters = row.get("parameters") or {}
        improvement_required = any(
            candidate_parameters.get(name) != baseline_parameters.get(name)
            for name in score_parameters
        )
        improved = not improvement_required or delta >= MULTI_FACTOR_MIN_NET_IC_IMPROVEMENT
        oos["net_ic_improvement_vs_baseline"] = round(delta, 6)
        oos["minimum_net_ic_improvement"] = MULTI_FACTOR_MIN_NET_IC_IMPROVEMENT
        oos["ic_improvement_gate_required"] = improvement_required
        oos["ic_improvement_gate_met"] = improved
        metrics["multi_factor_oos"] = oos
        row["net_ic_improvement_vs_baseline"] = round(delta, 6)
        row["eligible"] = bool(row.get("eligible") and improved)
        for regime, result in (row.get("regime_metrics") or {}).items():
            result_oos = ((result.get("metrics") or {}).get("multi_factor_oos") or {})
            baseline_regime_oos = (
                ((baseline_regimes.get(regime) or {}).get("metrics") or {}).get("multi_factor_oos") or {}
            )
            regime_delta = float(result_oos.get("net_of_costs_ic_mean") or 0.0)
            regime_delta -= float(baseline_regime_oos.get("net_of_costs_ic_mean") or 0.0)
            regime_improvement_required = improvement_required
            regime_improved = not regime_improvement_required or regime_delta >= MULTI_FACTOR_MIN_NET_IC_IMPROVEMENT
            result["net_ic_improvement_vs_baseline"] = round(regime_delta, 6)
            result["minimum_net_ic_improvement"] = MULTI_FACTOR_MIN_NET_IC_IMPROVEMENT
            result["ic_improvement_gate_required"] = regime_improvement_required
            result["ic_improvement_gate_met"] = regime_improved
            result["eligible"] = bool(result.get("eligible") and regime_improved)

def _run_search_with_bars(
    *, strategy: str, method: str, count: int, seed: int,
    space: dict[str, list], symbols: list[str], bars: dict,
    spy_daily: list[dict], start: date, end: date, args,
    weights: dict[str, float], min_trades: int,
    parent_parameters: dict | list[dict] | None = None,
    progress_label: str | None = None,
    preaggregated_daily_bars: dict[str, list[dict]] | None = None,
    precomputed_timeline: dict[datetime, dict[str, dict]] | None = None,
) -> dict:
    # Candidates in this search share the same input bars and replay window.
    # Prepare their data-derived inputs once instead of rebuilding them per run.
    if preaggregated_daily_bars is None:
        preaggregated_daily_bars = aggregate_intraday_bars(bars)
    if precomputed_timeline is None:
        precomputed_timeline = build_replay_timeline(symbols, bars, start, end)
    worker_count = min(
        max(1, int(getattr(args, "candidate_workers", 1))),
        available_cpu_count(),
    )
    replay_context = {
        "strategy": strategy, "symbols": symbols, "bars": bars,
        "spy_daily": spy_daily, "start": start, "end": end, "args": args,
        "weights": weights, "min_trades": min_trades,
        "preaggregated_daily_bars": preaggregated_daily_bars,
        "precomputed_timeline": precomputed_timeline,
    }
    candidates = _candidate_parameters(
        strategy, method, count, seed, space, parent_parameters=parent_parameters,
        excluded_candidate_ids=getattr(args, "excluded_candidate_ids", None),
    )
    evaluated: list[dict] = []
    history: list[dict] = []
    seen = {
        json.dumps(row["parameters"], sort_keys=True, separators=(",", ":"))
        for row in candidates
    }
    queue = list(candidates)
    rng = random.Random(seed)
    bayes_attempts = 0
    executor = None
    try:
        while queue or (method == "bayes" and len(evaluated) < count and bayes_attempts < count * 40):
            had_queued_candidates = bool(queue)
            batch = []
            while queue and len(batch) < worker_count:
                batch.append(queue.pop(0))
            # Bayesian proposals use completed results from the previous batch.
            # New proposals within this batch are unique, but do not wait for
            # another candidate's replay, keeping the CPU workers busy.
            while (
                not had_queued_candidates and not queue and method == "bayes"
                and len(evaluated) + len(batch) < count
                and len(batch) < worker_count and bayes_attempts < count * 40
            ):
                bayes_attempts += 1
                values = _bayes_proposal(space, history, seen, rng)
                if not values:
                    break
                try:
                    parameters = _make_parameters(strategy, values)
                except ValueError:
                    continue
                key = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
                excluded = getattr(args, "excluded_candidate_ids", ()) or ()
                if key in seen or candidate_id(strategy, parameters) in excluded:
                    continue
                seen.add(key)
                batch.append({
                    "name": f"bayes_{len(evaluated) + len(batch):03d}",
                    "parameters": parameters,
                })
            if not batch:
                break

            if executor is None and worker_count > 1 and len(batch) > 1:
                executor = ProcessPoolExecutor(
                    max_workers=worker_count,
                    initializer=_candidate_replay_worker_init,
                    initargs=(replay_context,),
                )
                logger.info(
                    "Adaptive search %s%s: evaluating candidates in batches of up to %d processes",
                    strategy, f" ({progress_label})" if progress_label else "", worker_count,
                )
            if executor is None:
                rows = [_evaluate_search_candidate(candidate, replay_context) for candidate in batch]
            else:
                rows = list(executor.map(_evaluate_candidate_in_worker, batch))
            for candidate, row in zip(batch, rows):
                raw_fitness = row.pop("_fitness_raw")
                evaluated.append(row)
                history.append({"parameters": candidate["parameters"], "fitness": raw_fitness})
            logger.info(
                "Adaptive search %s%s: completed %d/%d candidates",
                strategy, f" ({progress_label})" if progress_label else "", len(evaluated), count,
            )
    finally:
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
    _apply_multi_factor_ic_improvement_gate(evaluated, strategy)
    eligible_rows = [row for row in evaluated if row["eligible"]]
    sample_eligible = [row for row in evaluated if row["minimum_trade_count_met"]]
    pool = eligible_rows or sample_eligible or evaluated
    selected = max(pool, key=lambda row: (row["fitness"], -float(row["metrics"].get("max_drawdown_pct") or 0)))
    return {
        "strategy": strategy, "method": method, "candidate_count_requested": count,
        "candidate_count_evaluated": len(evaluated), "min_trades_for_selection": min_trades,
        "selection_status": (
            "eligible_positive_candidate" if eligible_rows else
            "no_positive_candidate" if sample_eligible else
            "fallback_low_sample"
        ),
        "fitness_weights": weights, "selected_candidate": selected,
        "leaderboard": sorted(evaluated, key=lambda row: row["fitness"], reverse=True),
        "candidates": evaluated,
    }

def _run_iterative_discovery_search(
    *, strategy: str, method: str, count: int, iterations: int, seed: int,
    space: dict[str, list], symbols: list[str], bars: dict,
    spy_daily: list[dict], start: date, end: date, args,
    weights: dict[str, float], min_trades: int,
    parent_parameters: dict | list[dict] | None = None,
    excluded_candidate_ids: set[str] | list[str] | tuple[str, ...] = (),
    preaggregated_daily_bars: dict[str, list[dict]] | None = None,
    search_func=None,
) -> dict:
    """Refine on training data for bounded rounds; verification remains separate."""
    if iterations < 1:
        raise ValueError("search iterations must be positive")

    candidates = []
    iteration_summaries = []
    incumbent = None
    excluded = set(excluded_candidate_ids)
    for iteration in range(1, iterations + 1):
        active_parent = (
            [incumbent["parameters"], *(parent_parameters if isinstance(parent_parameters, list) else ([parent_parameters] if parent_parameters else []))]
            if incumbent else parent_parameters
        )
        args.excluded_candidate_ids = excluded
        search_runner = search_func or _run_search_with_bars
        search_options = dict(
            strategy=strategy, method=method, count=count,
            seed=seed + (iteration - 1) * 104_729, space=space, symbols=symbols,
            bars=bars, spy_daily=spy_daily, start=start, end=end, args=args,
            weights=weights, min_trades=min_trades,
            parent_parameters=active_parent,
            progress_label=f"iteration {iteration}/{iterations}",
            preaggregated_daily_bars=preaggregated_daily_bars,
        )
        search = search_runner(**search_options)
        selected = search["selected_candidate"]
        comparison = _baseline_improvement(search)
        selected_name = f"iteration_{iteration:02d}:{selected.get('name', 'unknown')}"
        logger.info(
            "Adaptive refinement %s iteration %d/%d: %s fitness %.4f (baseline delta %+.4f)%s",
            strategy, iteration, iterations, selected.get("name"),
            float(selected.get("fitness") or 0.0), comparison["fitness_delta"],
            "; incumbent updated" if comparison["improved"] and (
                incumbent is None or float(selected["fitness"]) > float(incumbent["fitness"])
            ) else "",
        )
        iteration_summaries.append({
            "iteration": iteration,
            "candidate_count_evaluated": search["candidate_count_evaluated"],
            "parent_candidate_used": bool(active_parent),
            "selected_candidate": selected_name,
            "selected_fitness": selected.get("fitness"),
            "baseline_fitness": comparison["baseline_fitness"],
            "fitness_delta_vs_baseline": comparison["fitness_delta"],
            "improved_vs_baseline": comparison["improved"],
        })
        for row in search["candidates"]:
            excluded.add(candidate_id(strategy, row["parameters"]))
            original_name = str(row.get("name", "candidate"))
            candidates.append({
                **row,
                "name": "baseline" if iteration == 1 and original_name == "baseline"
                else f"iteration_{iteration:02d}:{original_name}",
                "search_iteration": iteration,
            })
        if (
            comparison["improved"]
            and (incumbent is None or float(selected["fitness"]) > float(incumbent["fitness"]))
        ):
            incumbent = selected

    eligible_rows = [row for row in candidates if row["eligible"]]
    sample_eligible = [row for row in candidates if row["minimum_trade_count_met"]]
    pool = eligible_rows or sample_eligible or candidates
    selected = max(
        pool,
        key=lambda row: (
            row["fitness"], -float(row["metrics"].get("max_drawdown_pct") or 0),
        ),
    )
    return {
        "strategy": strategy, "method": method,
        "candidate_count_requested": count * iterations,
        "candidate_count_per_iteration": count,
        "search_iterations": iterations,
        "candidate_count_evaluated": len(candidates),
        "min_trades_for_selection": min_trades,
        "selection_status": (
            "eligible_positive_candidate" if eligible_rows else
            "no_positive_candidate" if sample_eligible else
            "fallback_low_sample"
        ),
        "fitness_weights": weights, "selected_candidate": selected,
        "leaderboard": sorted(candidates, key=lambda row: row["fitness"], reverse=True),
        "candidates": candidates,
        "iteration_summaries": iteration_summaries,
    }

def _fit_window_ladder(
    *, strategy: str, method: str, count: int, seed: int, space: dict[str, list],
    symbols: list[str], bars: dict, spy_daily: list[dict], train_sessions: list[date],
    args, weights: dict[str, float], min_trades: int, window_options: list[int],
    preaggregated_daily_bars: dict[str, list[dict]] | None = None,
    search_func=None,
) -> dict:
    """Search one or more trailing train windows, falling back until improvement."""
    if preaggregated_daily_bars is None:
        preaggregated_daily_bars = aggregate_intraday_bars(bars)
    ordered = _ordered_window_options(window_options)
    results = []
    attempts = []
    for window_days in ordered:
        if len(train_sessions) < window_days:
            attempts.append({"window_days": window_days, "skipped": "insufficient_training_sessions"})
            continue
        fit_start = train_sessions[-window_days]
        fit_end = train_sessions[-1]
        search = (search_func or _run_search_with_bars)(
            strategy=strategy, method=method, count=count,
            seed=seed + window_days * 73, space=space, symbols=symbols,
            bars=bars, spy_daily=spy_daily, start=fit_start, end=fit_end,
            args=args, weights=weights, min_trades=min_trades,
            preaggregated_daily_bars=preaggregated_daily_bars,
        )
        comparison = _baseline_improvement(search)
        selected = search["selected_candidate"]
        baseline = comparison.get("baseline_candidate") or selected
        attempt = {
            "window_days": window_days,
            "fit_start": fit_start.isoformat(), "fit_end": fit_end.isoformat(),
            "selection_status": search["selection_status"],
            "selected_candidate": selected.get("name"),
            "selected_fitness": comparison["selected_fitness"],
            "baseline_fitness": comparison["baseline_fitness"],
            "fitness_delta": comparison["fitness_delta"],
            "improved": comparison["improved"],
        }
        attempts.append(attempt)
        results.append({
            "window_days": window_days, "fit_start": fit_start,
            "fit_end": fit_end,
            "search": search, "baseline": baseline, "comparison": comparison,
        })
        # A single configured window preserves the CLI's original behavior.
        if len(ordered) == 1 or comparison["improved"]:
            break

    if not results:
        raise ValueError("adaptive fit has no training window with enough sessions")
    improved = next((result for result in results if result["comparison"]["improved"]), None)
    if improved:
        chosen = improved
        selected = chosen["search"]["selected_candidate"]
    elif len(ordered) == 1:
        chosen = results[0]
        selected = chosen["search"]["selected_candidate"]
    else:
        chosen = max(results, key=lambda result: result["comparison"]["baseline_fitness"])
        selected = chosen["baseline"]

    return {
        "search": chosen["search"], "selected_candidate": selected,
        "fit_window_days": chosen["window_days"],
        "fit_window_start": chosen["fit_start"].isoformat(),
        "fit_window_end": chosen["fit_end"].isoformat(),
        "window_attempts": attempts, "adaptive_improvement": bool(improved),
        "selection_status": (
            chosen["search"]["selection_status"] if improved or len(ordered) == 1
            else "no_adaptive_improvement"
        ),
    }

def _symbols(value: str | None) -> list[str]:
    if value:
        symbols = list(dict.fromkeys(part.strip().upper() for part in value.split(",") if part.strip()))
    else:
        symbols = configured_universe("algo")["tickers"]
    if not symbols:
        raise ValueError("the Algo research universe is empty")
    return symbols

def _trading_sessions(db: str, end: date, start: date | None = None, adjustment: str = ADJUSTMENT) -> list[date]:
    lookup_start = start or (RESEARCH_START - timedelta(days=RESEARCH_WARMUP_DAYS))
    store = SQLiteBarStore(db)
    daily = store.bars(
        ["SPY"], feed=FEED, timeframe="1Day",
        start=lookup_start.isoformat(), end=end.isoformat(),
        adjustment=adjustment,
    ).get("SPY", [])
    result = set()
    for bar in daily:
        raw = str(bar.get("date") or bar.get("t") or "")
        try:
            stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            day = stamp.astimezone(ET).date() if stamp.tzinfo else date.fromisoformat(raw[:10])
        except (TypeError, ValueError):
            try:
                day = date.fromisoformat(raw[:10])
            except ValueError:
                continue
        if day <= end and (start is None or day >= start):
            result.add(day)
    return sorted(result)
