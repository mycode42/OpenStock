"""Factor rebalance state and satellite evidence helpers."""

from __future__ import annotations

from datetime import date, timedelta

from src.algo.strategies.registry import ALGO_DEFAULT_PARAMETERS
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
) -> tuple[dict[str, MultiFactorSignal], dict | None]:
    """Persist one PIT-safe factor rebalance per configured period."""
    generator = SignalGenerator(MultiFactorConfig.from_dict(ALGO_DEFAULT_PARAMETERS.get("multi_factor")))
    period = generator.rebalance_key(session)
    by_symbol = strategy_states.setdefault("multi_factor", {})
    held = {ticker for ticker, owner in position_owners.items() if owner == "multi_factor"}
    holdings = {
        ticker: {
            "qty": (positions or {}).get(ticker, {}).get("qty", by_symbol.get(ticker, {}).get("last_known_qty", 1)),
            "entry_at": by_symbol.get(ticker, {}).get("opened_at"),
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


def _multi_factor_satellite_signals(
    strategy_states: dict,
    selected_symbols: list[str],
    session: date,
) -> dict[str, dict]:
    """Read this period's factor targets as confirmation evidence only."""
    generator = SignalGenerator(MultiFactorConfig.from_dict(ALGO_DEFAULT_PARAMETERS.get("multi_factor")))
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
            "reason": "current-period cross-sectional factor target",
        }
    return output

