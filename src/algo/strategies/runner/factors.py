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
) -> tuple[dict[str, MultiFactorSignal], dict | None]:
    """Persist one point-in-time-safe factor rebalance per period."""
    config_values = dict(ALGO_DEFAULT_PARAMETERS.get("multi_factor") or {})
    if parameters:
        config_values.update(parameters)
    generator = SignalGenerator(MultiFactorConfig.from_dict(config_values))
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
    signals, diagnostics = generator.generate(
        daily_bars,
        symbols=selected_symbols,
        as_of=session - timedelta(days=1),
        session=session,
        regime=regime,
        held_positions=holdings,
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
        output[ticker] = {
            "ticker": ticker,
            "strategy": "multi_factor",
            "action": "buy" if direction == "long" else "short",
            "direction": direction,
            "rank": state.get("factor_rank"),
            "score": state.get("factor_score"),
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

