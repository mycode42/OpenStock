"""Factor rebalance state and daily core-target helpers."""

from __future__ import annotations

from datetime import date, timedelta

from src.algo.strategies.registry import ALGO_DEFAULT_PARAMETERS, validate_parameter_profile
from src.algo.strategies.multi_factor import MultiFactorConfig, MultiFactorSignal, SignalGenerator


def _multi_factor_rebalance_signals(
    strategy_states: dict,
    daily_bars: dict[str, list[dict]],
    selected_symbols: list[str],
    position_owners: dict[str, str],
    session: date,
    regime: str = "unknown",
    intraday_bars: dict[str, list[dict]] | None = None,
    positions: dict[str, dict] | None = None,
    parameters: dict | None = None,
    groups_by_symbol: dict[str, str] | None = None,
    tradable_symbols: set[str] | None = None,
) -> tuple[dict[str, MultiFactorSignal], dict | None]:
    """Persist one point-in-time-safe factor rebalance per period."""
    config_values = dict(ALGO_DEFAULT_PARAMETERS.get("multi_factor") or {})
    if parameters:
        config_values.update(parameters)
    generator = SignalGenerator(MultiFactorConfig.from_dict(config_values))
    fundamentals_by_symbol = None
    fundamentals_diagnostics = {"status": "not_requested", "source": None}
    try:
        from src.algo.strategies.multi_factor.fundamentals import (
            load_point_in_time_fundamentals, requested_fundamental_factors,
        )

        if requested_fundamental_factors(generator.config, regime):
            equity_symbols = [symbol for symbol in selected_symbols if symbol in (groups_by_symbol or {})]
            fundamentals_by_symbol, fundamentals_diagnostics = load_point_in_time_fundamentals(
                equity_symbols or selected_symbols, as_of=session - timedelta(days=1),
            )
    except Exception as exc:
        fundamentals_by_symbol = {}
        fundamentals_diagnostics = {
            "status": "unavailable", "source": "sec_edgar_companyfacts",
            "failure_types": {"feed": type(exc).__name__},
        }
    period = generator.rebalance_key(session)
    by_symbol = strategy_states.setdefault("multi_factor", {})
    owners_for_signals = dict(position_owners)
    positions_for_signals = dict(positions or {})
    held = {ticker for ticker, owner in owners_for_signals.items() if owner == "multi_factor"}
    holdings = {
        ticker: {
            "qty": positions_for_signals.get(ticker, {}).get("qty", by_symbol.get(ticker, {}).get("last_known_qty", 1)),
            "entry_at": by_symbol.get(ticker, {}).get("opened_at") or positions_for_signals.get(ticker, {}).get("entry_at"),
        }
        for ticker in held
    }
    tracked = set(selected_symbols) | held
    if not any(by_symbol.get(ticker, {}).get("last_rebalance_period") != period for ticker in tracked):
        return {}, None
    learned_factor_weights = None
    factor_model_diagnostics = None
    if generator.config.factor_weight_mode == "ridge_walk_forward":
        from src.algo.strategies.multi_factor.learned_weights import load_live_factor_weights

        learned_factor_weights, factor_model_diagnostics = load_live_factor_weights(
            as_of=session - timedelta(days=1),
        )
    signals, diagnostics = generator.generate(
        daily_bars,
        symbols=selected_symbols,
        as_of=session - timedelta(days=1),
        session=session,
        regime=regime,
        fundamentals_by_symbol=fundamentals_by_symbol,
        fundamentals_feed_diagnostics=fundamentals_diagnostics,
        held_positions=holdings,
        groups_by_symbol=groups_by_symbol,
        tradable_symbols=tradable_symbols,
        learned_factor_weights=learned_factor_weights,
        factor_model_diagnostics=factor_model_diagnostics,
    )
    if not diagnostics["minimum_universe_met"]:
        return {}, diagnostics
    signals = generator.apply_intraday_size_tilt(signals, intraday_bars or {})
    records = {item.ticker: item for item in signals}
    for ticker in tracked:
        item = records.get(ticker)
        state = by_symbol.setdefault(ticker, {})
        state.update({
            "last_rebalance_period": period,
            "factor_score": item.score if item else None,
            "factor_rank": item.rank if item else None,
            "desired_direction": item.direction if item else "flat",
            "target_weight": item.target_weight if item else 0.0,
        })
    return {
        ticker: item for ticker, item in records.items()
        if item.action in {"buy", "sell", "cover", "short"}
    }, diagnostics


def _multi_factor_current_targets(
    strategy_states: dict,
    selected_symbols: list[str],
    session: date,
    parameters: dict | None = None,
) -> dict[str, dict]:
    """Read this period's cached targets for the standalone core strategy."""
    config_values = dict(ALGO_DEFAULT_PARAMETERS.get("multi_factor") or {})
    if parameters:
        config_values.update(parameters)
    config = MultiFactorConfig.from_dict(config_values)
    generator = SignalGenerator(config)
    period = generator.rebalance_key(session)
    by_symbol = strategy_states.get("multi_factor", {})
    output = {}
    for ticker in selected_symbols:
        state = by_symbol.get(ticker, {})
        direction = state.get("desired_direction")
        if state.get("last_rebalance_period") != period or direction not in {"long", "short"}:
            continue
        score = float(state.get("factor_score") or 0.0)
        directional_score = abs(score) if direction == "short" else score
        output[ticker] = {
            "ticker": ticker,
            "strategy": "multi_factor",
            "action": "buy" if direction == "long" else "short",
            "direction": direction,
            "rank": state.get("factor_rank"),
            "score": state.get("factor_score"),
            "strength": max(0.0, min(1.0, 0.5 + directional_score / 6)),
            "target_weight": config.max_position_pct,
            "stop_pct": config.hard_stop_pct,
            "risk_multiplier": config.risk_multiplier,
            "strategy_group": "swing",
            "reason": "current-period cross-sectional factor target",
        }
    return output


def multi_factor_parameters_for_session() -> tuple[dict, dict]:
    """Return the configured live profile; research candidates require review."""
    baseline = validate_parameter_profile({
        "multi_factor": ALGO_DEFAULT_PARAMETERS["multi_factor"],
    })["multi_factor"]
    return baseline, {"source": "live_defaults"}

