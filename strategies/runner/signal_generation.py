"""Strategy signal generation for a single Algo portfolio scan."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from portfolio.config import quote_stale_limit_seconds
from strategies.catalog import CORE_STRATEGIES, INTRADAY_STRATEGIES, STRATEGY_CATALOG
from strategies.portfolio_coordinator import choose_priority
from strategies.registry import (
    ALGO_RULE_MODULES,
    RULE_MODULES,
    evaluate as evaluate_strategy,
    parameters_for_regime,
)
from strategies.rule_pipeline import evaluate_rule_signal
from strategies.runner.sizing import (
    _is_protective_sell_order,
    _positive_price_level,
    _update_trailing_stop,
)


@dataclass
class SignalGenerationContext:
    """Explicit inputs and scan state used while building ticker signals."""

    mode: str
    strategy_symbols: list[str]
    positions: dict[str, dict]
    bars_by_symbol: dict[str, list[dict]]
    owner_map: dict[str, str]
    track_owners: bool
    coordinator_config: Any
    now: datetime
    regular_close: datetime | None
    session_name: str | None
    regime: dict
    multi_factor_target_signals: dict[str, Any]
    event_generator: Any
    event_strategy_enabled: bool
    strategy_states: dict
    daily_bars: dict[str, list[dict]]
    now_utc: datetime
    quote_check_time: datetime
    selected_symbols: list[str]
    latest_quotes: dict[str, dict]
    risk_config: dict
    rank_score: dict[str, float]
    lifecycle: Any
    multi_factor_signals: dict[str, Any]
    momentum_signals: dict[str, Any]
    runtime_settings: Any
    pending_by_symbol: dict[str, list[dict]]
    universe: dict
    fresh_signal_bar_symbols: set[str]
    overnight_swing_signal_bar_symbols: set[str]
    scan_errors: list[str]
    entry_pause_reasons: list[str]


def generate_strategy_signals(
    context: SignalGenerationContext,
) -> list[dict]:
    """Build one peer strategy candidate per eligible symbol."""
    generated: list[dict] = []

    for ticker in context.strategy_symbols:
        symbol_signals = _generate_symbol_signals(context, ticker)
        generated.extend(symbol_signals)

    return generated


def _generate_symbol_signals(context: SignalGenerationContext, ticker: str) -> list[dict]:
    position = context.positions.get(ticker, {})
    position_qty = float(position.get("qty", 0) or 0)
    symbol_bars = context.bars_by_symbol.get(ticker, [])
    owner = context.owner_map.get(ticker)

    forced_exit = _intraday_flatten_signal(
        context, ticker, owner, position_qty, symbol_bars,
    )
    if forced_exit is not None:
        return [forced_exit]
    if context.track_owners and position_qty != 0 and owner is None:
        # An unknown holding must not be taken over by an active strategy.
        return []

    candidates = _strategy_candidates(context, ticker, owner, position_qty)
    event_signal = _event_signal(context, ticker, owner, position, position_qty, candidates)
    options = _protective_exit_option(
        context, ticker, owner, position, position_qty, symbol_bars,
    )

    for strategy_name in candidates:
        option = _strategy_option(
            context,
            ticker,
            strategy_name,
            owner,
            position,
            position_qty,
            symbol_bars,
            event_signal,
        )
        if option is not None:
            options.append(option)

    if not options:
        return []

    chosen, lower_priority = _choose_option(context, options)
    output = list(lower_priority)
    suppressed = _suppress_conflicting_orders(
        context, ticker, position_qty, chosen,
    )
    if suppressed:
        output.append(chosen)
        return output

    if chosen["action"] == "short":
        suppression = _short_entry_suppression(context, ticker)
        if suppression:
            chosen["suppressed_reason"] = suppression
            output.append(chosen)
            return output
        if str(context.regime.get("regime", "unknown")) == "range_bound":
            chosen["risk_multiplier"] = float(
                context.risk_config.get("range_regime_short_risk_multiplier", 0.75)
            )

    chosen_strategy = STRATEGY_CATALOG.get(str(chosen.get("strategy") or ""))
    overnight_swing_bar_eligible = bool(
        context.session_name == "overnight"
        and chosen_strategy is not None
        and chosen_strategy.horizon == "swing"
        and ticker in context.overnight_swing_signal_bar_symbols
    )
    if (
        context.mode == "algo"
        and not chosen.get("software_protection")
        and ticker not in context.fresh_signal_bar_symbols
        and not overnight_swing_bar_eligible
    ):
        chosen["suppressed_reason"] = "latest completed signal bar is stale or unavailable"
    output.append(chosen)
    return output


def _intraday_flatten_signal(
    context: SignalGenerationContext,
    ticker: str,
    owner: str | None,
    position_qty: float,
    symbol_bars: list[dict],
) -> dict | None:
    if context.mode != "algo" or owner is None or position_qty == 0:
        return None
    flatten_all_eod = bool(context.risk_config.get("flatten_eod", False))
    flatten_intraday = (
        owner in INTRADAY_STRATEGIES
        and not context.coordinator_config.allow_intraday_overnight
    )
    if not (flatten_all_eod or flatten_intraday):
        return None

    close_cutoff = (
        context.regular_close
        or context.now.replace(hour=16, minute=0, second=0, microsecond=0)
    ) - timedelta(minutes=context.coordinator_config.intraday_close_cutoff_minutes)
    if context.session_name in {None, "regular"} and context.now < close_cutoff:
        return None

    return {
        "ticker": ticker,
        "action": "cover" if position_qty < 0 else "sell",
        "strategy": owner,
        "reason": (
            "portfolio configuration end-of-day flatten"
            if flatten_all_eod else "portfolio coordinator mandatory intraday session flatten"
        ),
        "strength": 1.0,
        "rank_score": 1_000_000.0,
        "signal_bar_time": symbol_bars[-1].get("t") if symbol_bars else None,
        "timeframe": "5Min",
        "software_protection": True,
        "coordinator_force_flatten": True,
    }


def _strategy_candidates(
    context: SignalGenerationContext,
    ticker: str,
    owner: str | None,
    position_qty: float,
) -> list[str]:
    if owner:
        return [owner]

    candidates = [
        name for name in context.regime.get("active", [])
        if name in CORE_STRATEGIES
    ]
    return list(dict.fromkeys(candidates))


def _event_signal(
    context: SignalGenerationContext,
    ticker: str,
    owner: str | None,
    position: dict,
    position_qty: float,
    candidates: list[str],
) -> Any | None:
    if not (
        context.mode == "algo"
        and context.event_generator is not None
        and (context.event_strategy_enabled or owner == "swing_event_driven")
        and context.session_name in {None, "regular"}
        and "swing_event_driven" in candidates
    ):
        return None

    try:
        event_state = context.strategy_states.setdefault("swing_event_driven", {})
        return context.event_generator.generate(
            ticker,
            context.daily_bars.get(ticker, []),
            as_of=context.now_utc,
            universe=set(context.selected_symbols) | ({"SPY"} if ticker == "SPY" else set()),
            regime=str(context.regime.get("regime", "unknown")),
            strategy_state=event_state,
            position_qty=position_qty if owner == "swing_event_driven" else 0,
            entry_at=(
                event_state.get(ticker, {}).get("opened_at")
                if owner == "swing_event_driven" else None
            ),
            entry_price=(position.get("avg_entry_price") if owner == "swing_event_driven" else None),
        )
    except (KeyError, TypeError, ValueError) as exc:
        context.scan_errors.append(
            f"daily event strategy signal {ticker}: {type(exc).__name__}: {exc}"
        )
        return None


def _protective_exit_option(
    context: SignalGenerationContext,
    ticker: str,
    owner: str | None,
    position: dict,
    position_qty: float,
    symbol_bars: list[dict],
) -> list[dict]:
    quote = context.latest_quotes.get(ticker)
    if not owner or position_qty == 0 or not quote:
        return []

    quote_age = (context.quote_check_time - quote["timestamp"]).total_seconds()
    # Use the closing side of the quote: bid for longs and ask for shorts.
    current_price = float(quote.get("ask" if position_qty < 0 else "bid", quote["price"]))
    protective_state = (context.strategy_states or {}).get(owner, {}).get(ticker, {})
    stop_price = _positive_price_level(protective_state.get("stop_price"))
    is_fresh = 0 <= quote_age <= quote_stale_limit_seconds(context.session_name)
    if is_fresh:
        stop_price = _update_trailing_stop(
            protective_state,
            strategy=owner,
            qty=position_qty,
            entry_price=position.get("avg_entry_price"),
            current_price=current_price,
            mode_risk=context.risk_config,
        )

    target_price = _positive_price_level(protective_state.get("take_profit"))
    protective_reason = None
    if is_fresh:
        if position_qty > 0:
            if stop_price is not None and current_price <= stop_price:
                protective_reason = "software stop-loss reached"
            elif target_price is not None and current_price >= target_price:
                protective_reason = "software take-profit reached"
        else:
            if stop_price is not None and current_price >= stop_price:
                protective_reason = "software short stop-loss reached"
            elif target_price is not None and current_price <= target_price:
                protective_reason = "software short take-profit reached"

    if not protective_reason or (
        context.lifecycle is not None
        and not context.lifecycle.allows_protective_exit(owner, ticker)
    ):
        return []
    return [{
        "ticker": ticker,
        "action": "cover" if position_qty < 0 else "sell",
        "strategy": owner,
        "reason": protective_reason,
        "strength": 1.0,
        "rank_score": context.rank_score.get(owner, 0),
        "signal_bar_time": symbol_bars[-1].get("t") if symbol_bars else None,
        "software_protection": True,
    }]


def _strategy_option(
    context: SignalGenerationContext,
    ticker: str,
    strategy_name: str,
    owner: str | None,
    position: dict,
    position_qty: float,
    symbol_bars: list[dict],
    event_signal: Any | None,
) -> dict | None:
    if strategy_name == "swing_event_driven" and (
        context.mode != "algo" or context.session_name not in {None, "regular"}
    ):
        return None

    strategy_spec = STRATEGY_CATALOG.get(strategy_name)
    signal_bars = symbol_bars
    signal_bar_time = symbol_bars[-1].get("t") if symbol_bars else None
    daily_strategy = bool(strategy_spec and strategy_spec.bar_interval == "1Day")
    if daily_strategy:
        signal_bars = context.daily_bars.get(ticker, [])
        signal_bar_time = (
            signal_bars[-1].get("t", signal_bars[-1].get("date"))
            if signal_bars else None
        )

    signal = _provided_signal(context, strategy_name, ticker, owner, position_qty, event_signal)
    if context.lifecycle:
        context.lifecycle.observe(strategy_name, ticker, signal_bar_time)

    if signal is None and (strategy_name in RULE_MODULES or strategy_name in ALGO_RULE_MODULES):
        try:
            if context.mode == "algo":
                if strategy_name == "swing_momentum":
                    return None
                if (
                    strategy_spec is not None
                    and strategy_spec.runtime_setting
                    and not getattr(context.runtime_settings, strategy_spec.runtime_setting)
                ):
                    return None
                protective_state = (
                    (context.strategy_states or {}).get(strategy_name, {}).get(ticker, {})
                )
                signal = evaluate_rule_signal(
                    strategy_name,
                    ticker,
                    signal_bars,
                    parameters=parameters_for_regime(
                        strategy_name, str(context.regime.get("regime", "unknown")),
                    ),
                    position_qty=position_qty,
                    entry_at=protective_state.get("opened_at"),
                    entry_price=(position.get("avg_entry_price") if position else None),
                    position_target=protective_state.get("take_profit"),
                )
            else:
                signal = evaluate_strategy(
                    strategy_name, context.mode, ticker, symbol_bars, position_qty,
                )
        except Exception as exc:
            if context.mode != "algo":
                raise
            context.scan_errors.append(
                f"{strategy_name} signal {ticker}: {type(exc).__name__}: {exc}"
            )
            context.entry_pause_reasons.append("one or more Algo signal rules failed")
            return None

    signal_action = (
        signal.get("action") if isinstance(signal, dict)
        else getattr(signal, "action", None)
    )
    expected_exit = "cover" if position_qty < 0 else "sell"
    if signal is None or (owner and signal_action != expected_exit):
        return None

    item = dict(signal) if isinstance(signal, dict) else signal.to_dict()
    item["rank_score"] = context.rank_score.get(strategy_name, 0)
    item["signal_bar_time"] = signal_bar_time
    item["timeframe"] = strategy_spec.bar_interval if strategy_spec else "5Min"
    if context.lifecycle and not context.lifecycle.allows(
        strategy_name, ticker, signal_action, item["signal_bar_time"],
    ):
        return None
    return item


def _provided_signal(
    context: SignalGenerationContext,
    strategy_name: str,
    ticker: str,
    owner: str | None,
    position_qty: float,
    event_signal: Any | None,
) -> Any | None:
    if strategy_name == "swing_multi_factor":
        signal = context.multi_factor_signals.get(ticker)
        if signal is None and owner is None and position_qty == 0:
            signal = context.multi_factor_target_signals.get(ticker)
        signal_action = (
            signal.get("action") if isinstance(signal, dict)
            else getattr(signal, "action", None)
        )
        return signal if signal is not None and signal_action in {"buy", "short", "sell", "cover"} else None
    if strategy_name == "swing_event_driven":
        return event_signal
    if strategy_name == "swing_momentum":
        return context.momentum_signals.get(ticker)
    return None


def _choose_option(context: SignalGenerationContext, options: list[dict]) -> tuple[dict, list[dict]]:
    if context.mode == "algo" and len(options) > 1:
        return choose_priority(options)
    chosen = max(
        options,
        key=lambda item: (
            bool(item.get("software_protection")),
            item["rank_score"],
            float(item.get("strength", 0.0) or 0.0),
        ),
    )
    return chosen, []


def _suppress_conflicting_orders(
    context: SignalGenerationContext,
    ticker: str,
    position_qty: float,
    chosen: dict,
) -> bool:
    ticker_orders = context.pending_by_symbol.get(ticker, [])
    pending_entry = position_qty == 0 and any(
        str(order.get("side", "")).lower() in {"buy", "sell"}
        for order in ticker_orders
    )
    pending_exit = any(
        str(order.get("side", "")).lower() == ("buy" if position_qty < 0 else "sell")
        and not _is_protective_sell_order(order, position_qty)
        for order in ticker_orders
    )
    if (
        (chosen["action"] in {"buy", "short"} and ticker_orders)
        or pending_entry
        or (pending_exit and not chosen.get("coordinator_force_flatten"))
    ):
        chosen["suppressed_reason"] = "a conflicting Alpaca order is already open for this symbol"
        return True
    return False


def _short_entry_suppression(context: SignalGenerationContext, ticker: str) -> str | None:
    if context.session_name not in {None, "regular"}:
        return "short entries are restricted to regular market hours"
    if ticker not in set(context.universe.get("easy_to_borrow_tickers") or []):
        return "Alpaca has not confirmed this ticker as easy to borrow"
    return None


__all__ = ["SignalGenerationContext", "generate_strategy_signals"]
