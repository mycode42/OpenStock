"""Replay input loading and candidate evaluation."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
import logging
import math

from autoresearch.backtest import AlgoPortfolioBacktest, strategy_warmup_days
from autoresearch.backtest.config import REGIME_WARMUP_DAYS
from market.store import SQLiteBarStore
from ..artifacts.candidate_archive import logic_version, model_family
from ..artifacts.replay_logging import quiet_coordinator_rejections
from ..settings import (
    CORE_RESEARCH_CONTROLS, FEED, REGIME_CONTEXT_WARMUP_DAYS,
    SIP_EXTENDED_LOCAL_FEED,
)
from .adaptive_config import MULTI_FACTOR_MIN_NET_IC_IMPROVEMENT
from .adaptive_scoring import (
    _cost_model_version, _fitness, _metrics, _metrics_by_regime,
)

logger = logging.getLogger(__name__)
_CANDIDATE_REPLAY_CONTEXT: dict | None = None

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
    regime_cache: dict | None = None,
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
        regime_cache=regime_cache,
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
        regime_cache=context["regime_cache"],
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

__all__ = [
    "_apply_multi_factor_ic_improvement_gate", "_candidate_replay_worker_init",
    "_evaluate_candidate_in_worker", "_evaluate_search_candidate",
    "_load_forward_window_data", "_load_window_data", "_ordered_window_options",
    "_profile_for", "_run_replay",
]
