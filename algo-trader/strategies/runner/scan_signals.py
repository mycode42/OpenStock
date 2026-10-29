"""Build strategy-specific candidates for one Algo scan."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from strategies.multi_factor import MultiFactorSignal
from strategies.momentum.swing import generate_signals as generate_swing_momentum_signals
from strategies.registry import parameters_for_regime
from strategies.runner.factors import (
    _multi_factor_current_targets,
    _multi_factor_rebalance_signals,
)
from strategies.runner.signal_generation import (
    SignalGenerationContext,
    generate_strategy_signals,
)

from .scan_types import MarketSnapshot, QuoteSnapshot, ScanScope, SignalSnapshot


def _limit_entries_to_selected(generated: list[dict], selected_symbols) -> list[dict]:
    """Drop new buys and shorts outside the selected universe; exits always pass."""
    selected = {str(symbol).upper() for symbol in selected_symbols}
    return [
        signal for signal in generated
        if str(signal.get("action") or "").lower() not in {"buy", "short"}
        or str(signal.get("ticker") or "").upper() in selected
    ]


def generate_scan_signals(
    scope: ScanScope, market: MarketSnapshot, quotes: QuoteSnapshot,
) -> SignalSnapshot:
    """Generate Multi Factor, momentum, and per-symbol strategy proposals."""
    multi_factor_signals: dict[str, MultiFactorSignal] = {}
    multi_factor_diagnostics = None
    if (
        scope.runtime_settings.swing_multi_factor_enabled
        or "swing_multi_factor" in set(scope.owner_map.values())
    ) and quotes.fresh_quote_symbols.intersection(scope.strategy_symbols):
        try:
            multi_factor_signals, multi_factor_diagnostics = _multi_factor_rebalance_signals(
                scope.strategy_states, market.daily_bars, scope.selected_symbols,
                scope.owner_map, date.fromisoformat(scope.today),
                regime=str(market.regime.get("regime", "unknown")),
                intraday_bars=market.bars_by_symbol,
                positions=scope.positions,
                parameters=market.multi_factor_parameters,
                groups_by_symbol=scope.universe.get("sector_by_symbol"),
                tradable_symbols=None,
            )
        except Exception as exc:
            scope.scan_errors.append(f"multi-factor rebalance: {type(exc).__name__}: {exc}")

    rank_score = {
        entry["strategy"]: entry.get("selection_score", entry["score"])
        for entry in market.regime.get("ranked", [])
    }
    momentum_signals: dict[str, object] = {}
    if scope.mode == "algo":
        try:
            momentum_parameters = parameters_for_regime("swing_momentum", market.regime_name)
            momentum_parameters.update({
                "hard_stop_pct": scope.risk_config["stop_loss_pct_by_strategy"]["swing_momentum"],
                "trailing_stop_pct": scope.risk_config["trailing_stop_pct_by_strategy"]["swing_momentum"],
                "trailing_activation_pct": scope.risk_config[
                    "trailing_activation_pct_by_strategy"
                ]["swing_momentum"],
            })
            held_momentum = {}
            for ticker, owner in scope.owner_map.items():
                if owner != "swing_momentum":
                    continue
                state = (scope.strategy_states or {}).get("swing_momentum", {}).get(ticker, {})
                held_momentum[ticker] = {
                    "qty": float((scope.positions.get(ticker) or {}).get("qty", 0) or 0),
                    "entry_at": state.get("opened_at"),
                    "entry_price": (scope.positions.get(ticker) or {}).get("avg_entry_price"),
                }
            momentum_signals, _momentum_diagnostics = generate_swing_momentum_signals(
                market.daily_bars,
                symbols=scope.selected_symbols,
                as_of=date.fromisoformat(scope.today) - timedelta(days=1),
                regime=str(market.regime.get("regime", "unknown")),
                held_positions=held_momentum,
                parameters=momentum_parameters,
            )
        except Exception as exc:
            scope.scan_errors.append(f"daily momentum ranking: {type(exc).__name__}: {exc}")
            scope.entry_pause_reasons.append("daily momentum ranking failed")

    multi_factor_target_signals = (
        _multi_factor_current_targets(
            scope.strategy_states, scope.selected_symbols, date.fromisoformat(scope.today),
            parameters=market.multi_factor_parameters,
        )
        if scope.runtime_settings.swing_multi_factor_enabled else {}
    )
    generated = generate_strategy_signals(
        SignalGenerationContext(
            mode=scope.mode,
            strategy_symbols=scope.strategy_symbols,
            positions=scope.positions,
            bars_by_symbol=market.bars_by_symbol,
            owner_map=scope.owner_map,
            track_owners=scope.track_owners,
            coordinator_config=scope.coordinator_config,
            now=scope.now,
            regular_close=scope.regular_close,
            session_name=scope.session_name,
            regime=market.regime,
            multi_factor_target_signals=multi_factor_target_signals,
            llm_generator=market.llm_generator,
            llm_strategy_enabled=scope.llm_strategy_enabled,
            strategy_states=scope.strategy_states,
            daily_bars=market.daily_bars,
            now_utc=scope.now_utc or datetime.now(timezone.utc),
            quote_check_time=datetime.now(timezone.utc),
            selected_symbols=scope.selected_symbols,
            latest_quotes=quotes.latest_quotes,
            risk_config=scope.risk_config,
            rank_score=rank_score,
            lifecycle=scope.lifecycle,
            multi_factor_signals=multi_factor_signals,
            momentum_signals=momentum_signals,
            runtime_settings=scope.runtime_settings,
            pending_by_symbol=scope.pending_by_symbol,
            universe=scope.universe,
            fresh_signal_bar_symbols=market.fresh_signal_bar_symbols,
            overnight_swing_signal_bar_symbols=market.overnight_swing_signal_bar_symbols,
            scan_errors=scope.scan_errors,
            entry_pause_reasons=scope.entry_pause_reasons,
        )
    )
    # Names outside today's selected universe are exit-only: a held position
    # outside the universe may be closed or reduced, but never opened or added to.
    generated = _limit_entries_to_selected(generated, scope.selected_symbols)
    return SignalSnapshot(
        multi_factor_diagnostics=multi_factor_diagnostics,
        rank_score=rank_score,
        momentum_signals=momentum_signals,
        generated=generated,
    )
