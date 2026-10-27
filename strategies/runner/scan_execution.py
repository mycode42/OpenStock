"""Apply shared risk limits and size proposals into broker-ready trades."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import logging

from options.risk import load_options_delta_overlay
from strategies.catalog import CORE_STRATEGIES
from market.calendar import EXTENDED_SESSIONS
from portfolio.config import STRATEGY_CONFIG
from strategies.catalog import strategy_ids_for_group
from strategies.portfolio_coordinator import (
    INTRADAY_STRATEGIES,
    PortfolioCoordinator,
    strategy_priority,
    throttle_signal,
)
from strategies.runner.sizing import (
    _is_protective_sell_order, _make_trade, entry_limit_price, intraday_volatility_stop_pct,
)
from strategies.registry import profile_id_for_regime

from .scan_types import ExecutionResult, MarketSnapshot, QuoteSnapshot, ScanScope
from portfolio.config import risk_universe


LOG = logging.getLogger("strategy_runner")


def execute_scan_candidates(
    scope: ScanScope,
    market: MarketSnapshot,
    quotes: QuoteSnapshot,
    generated: list[dict],
) -> ExecutionResult:
    """Enforce scan budgets, coordinator limits, and execution protections."""
    entries_paused = bool(scope.entry_pause_reasons)
    if entries_paused:
        reason = "; ".join(dict.fromkeys(scope.entry_pause_reasons))
        for item in generated:
            if item["action"] in {"buy", "short"} and not item.get("suppressed_reason"):
                item["suppressed_reason"] = reason

    now_utc = datetime.now(timezone.utc)
    strategy_entry_counts = {
        strategy: sum(
            int(state.get("entries_today", 0) or 0)
            for state in by_symbol.values()
            if state.get("entry_day") == scope.today and state.get("strategy") == strategy
        )
        for strategy, by_symbol in (scope.strategy_states or {}).items()
    }
    strategy_open_counts = {
        strategy: sum(owner == strategy for owner in scope.owner_map.values())
        for strategy in CORE_STRATEGIES
    }
    group_limits = scope.risk_config.get("strategy_group_limits", {})
    group_entry_counts = {
        name: sum(
            int(state.get("entries_today", 0) or 0)
            for strategy, by_symbol in (scope.strategy_states or {}).items()
            if strategy in set(group.get("strategies", []))
            for state in by_symbol.values()
            if state.get("entry_day") == scope.today and state.get("strategy") == strategy
        )
        for name, group in group_limits.items()
    }
    strategy_position_caps = scope.risk_config.get("max_positions_per_strategy_by_strategy", {})
    batch_entry_counts: dict[str, int] = {}
    batch_open_counts: dict[str, int] = {}
    if scope.mode == "algo":
        coordinator = PortfolioCoordinator(scope.coordinator_config)
        entries_today = sum(
            int(state.get("entries_today", 0) or 0)
            for by_symbol in (scope.strategy_states or {}).values()
            for state in by_symbol.values()
            if state.get("entry_day") == scope.today
        )
        intraday_entries_today = sum(
            int(state.get("entries_today", 0) or 0)
            for strategy, by_symbol in (scope.strategy_states or {}).items()
            if strategy in INTRADAY_STRATEGIES
            for state in by_symbol.values()
            if state.get("entry_day") == scope.today
        )
        pending_buys: dict[str, float] = {}
        pending_shorts: dict[str, float] = {}
        for order in scope.open_orders:
            ticker = str(order.get("symbol", "")).upper()
            if not ticker or float((scope.positions.get(ticker) or {}).get("qty", 0) or 0) != 0:
                continue
            side = str(order.get("side", "")).lower()
            if side not in {"buy", "sell"}:
                continue
            qty = max(0.0, float(order.get("qty") or 0) - float(order.get("filled_qty") or 0))
            reference = max((
                float(order.get(key) or 0)
                for key in ("limit_price", "stop_price", "filled_avg_price")
            ), default=0.0)
            if reference <= 0:
                quote = quotes.latest_quotes.get(ticker)
                reference = float(quote.get("price") or 0) if quote else 0.0
            if qty <= 0 or reference <= 0:
                continue
            target = pending_buys if side == "buy" else pending_shorts
            target[ticker] = target.get(ticker, 0.0) + qty * reference
        options_delta_overlay, options_overlay_status = load_options_delta_overlay()
        coordinator_book = coordinator.new_book(
            equity=scope.equity,
            positions=scope.positions,
            position_owners=scope.owner_map,
            pending_buy_values=pending_buys,
            pending_short_values=pending_shorts,
            options_delta_notional_by_underlying=options_delta_overlay,
            entries_today=entries_today,
            intraday_entries_today=intraday_entries_today,
            swing_entries_today=group_entry_counts.get("swing", 0),
        )
    else:
        options_delta_overlay, options_overlay_status = {}, {
            "available": False, "reason": "equity options overlay is Algo-only",
        }

    ordered_generated = list(generated)
    if scope.mode == "algo":
        ordered_generated = sorted(ordered_generated, key=lambda item: (
            0 if item.get("software_protection") else
            1 if item.get("action") in {"sell", "cover"} else
            2 + strategy_priority(str(item.get("strategy", ""))),
            -float(item.get("rank_score", 0) or 0), str(item.get("ticker", "")),
        ))
    trades = []
    for item in ordered_generated:
        if item.get("suppressed_reason"):
            continue
        action = str(item.get("action", "")).lower()
        if scope.mode == "algo" and scope.session_name not in {"regular", *EXTENDED_SESSIONS}:
            item["suppressed_reason"] = "Algo order actions are restricted to Alpaca trading sessions"
            continue
        strategy = str(item.get("strategy", ""))
        if action in {"buy", "short"}:
            strategy_limit = scope.risk_config.get(
                "max_entries_per_strategy_per_day", {},
            ).get(strategy)
            already_entered = strategy_entry_counts.get(strategy, 0) + batch_entry_counts.get(strategy, 0)
            if strategy_limit is not None and already_entered >= int(strategy_limit):
                item["suppressed_reason"] = "per-strategy daily entry limit reached"
                continue
            position_limit = strategy_position_caps.get(
                strategy, scope.risk_config.get("max_positions_per_strategy"),
            )
            already_open = strategy_open_counts.get(strategy, 0) + batch_open_counts.get(strategy, 0)
            if position_limit is not None and already_open >= int(position_limit):
                item["suppressed_reason"] = "per-strategy open-position limit reached"
                continue
            group_blocked = False
            for group_name, group in group_limits.items():
                if strategy not in group.get("strategies", []):
                    continue
                if group_entry_counts.get(group_name, 0) >= int(group.get("max_entries_per_day", 10**9)):
                    item["suppressed_reason"] = f"{group_name} daily-entry budget reached"
                    group_blocked = True
                if group_blocked:
                    break
            if group_blocked:
                continue
        quote = quotes.latest_quotes.get(item["ticker"])
        if quote is None:
            item["suppressed_reason"] = "latest Alpaca quote unavailable"
            continue
        price = float(quote["price"])
        quote_time = quote["timestamp"]
        age = (now_utc - quote_time).total_seconds()
        if age < 0 or age > quotes.freshness_limit:
            item["suppressed_reason"] = f"latest Alpaca quote is stale ({age:.0f}s)"
            continue
        if scope.extended_hours:
            spread_pct = (float(quote["ask"]) - float(quote["bid"])) / price if price else 1.0
            if item["action"] == "buy" and spread_pct > float(
                STRATEGY_CONFIG["extended_hours_max_spread_pct"]
            ):
                item["suppressed_reason"] = f"extended-hours spread is too wide ({spread_pct:.1%})"
                continue
            if (
                scope.session_name == "overnight"
                and item["ticker"] not in (scope.overnight_tradable_tickers or set())
            ):
                item["suppressed_reason"] = "Alpaca does not mark this asset overnight_tradable"
                continue
        execution_price = (
            float(quote["ask"] if item["action"] in {"buy", "cover"} else quote["bid"])
            if scope.extended_hours else price
        )
        sizing_signal = item
        if scope.mode == "algo" and action in {"buy", "short"}:
            sizing_signal = dict(item)
            strategy_weight_caps = scope.risk_config.get("max_position_pct_by_strategy", {})
            strategy_risks = scope.risk_config.get("risk_multiplier_by_strategy", {})
            configured_cap = strategy_weight_caps.get(strategy)
            if isinstance(configured_cap, (int, float)) and configured_cap > 0:
                existing_cap = sizing_signal.get("target_weight")
                sizing_signal["target_weight"] = (
                    min(float(existing_cap), float(configured_cap))
                    if isinstance(existing_cap, (int, float)) and existing_cap > 0
                    else float(configured_cap)
                )
            configured_risk = strategy_risks.get(strategy)
            if isinstance(configured_risk, (int, float)) and configured_risk > 0:
                sizing_signal["risk_multiplier"] = float(configured_risk)
            uncertainty_multiplier = float(
                market.regime.get("regime_uncertainty_risk_multiplier", 1.0)
            )
            sizing_signal["risk_multiplier"] = float(
                sizing_signal.get("risk_multiplier", 1.0)
            ) * uncertainty_multiplier
            if uncertainty_multiplier < 1.0:
                base_weight = sizing_signal.get("target_weight")
                if not isinstance(base_weight, (int, float)) or base_weight <= 0:
                    base_weight = configured_cap or scope.risk_config.get("max_position_pct")
                if isinstance(base_weight, (int, float)) and base_weight > 0:
                    sizing_signal["target_weight"] = float(base_weight) * uncertainty_multiplier
            if action == "short" and str(market.regime.get("regime", "unknown")) == "range_bound":
                sizing_signal["risk_multiplier"] *= float(
                    scope.risk_config.get("range_regime_short_risk_multiplier", 0.75)
                )
            sizing_signal, throttle_note = throttle_signal(
                sizing_signal, str(market.regime.get("regime", "unknown")), scope.coordinator_config,
                risk_multiplier=float(
                    market.regime.get("strategy_risk_multipliers", {}).get(strategy, 1.0)
                ),
            )
            if throttle_note:
                LOG.info("%s for %s", throttle_note, item["ticker"])
        minimum_stop_pct = None
        if item["strategy"] in strategy_ids_for_group("intraday_core"):
            minimum_stop_pct = intraday_volatility_stop_pct(
                market.bars_by_symbol.get(item["ticker"]),
                multiple=STRATEGY_CONFIG["intraday_stop_vol_multiple"],
                lookback=int(STRATEGY_CONFIG["intraday_stop_vol_lookback_bars"]),
            )
        trade = _make_trade(
            sizing_signal, execution_price, scope.equity,
            scope.positions.get(item["ticker"], {}), scope.risk_config,
            mode_universe=risk_universe(scope.mode_config),
            minimum_stop_pct=minimum_stop_pct,
        )
        if not trade:
            item["suppressed_reason"] = "position or strategy sizing produced zero shares"
            continue
        if scope.mode == "algo" and action in {"buy", "short"}:
            decision = coordinator_book.review(
                trade,
                session=scope.session_name or "regular",
                timestamp=scope.now,
                regime=str(market.regime.get("regime", "unknown")),
                volatility_state=str(market.regime.get("volatility_state", "unknown")),
                session_open=scope.regular_open,
                session_close=scope.regular_close,
            )
            if not decision.approved or decision.proposal is None:
                item["suppressed_reason"] = f"portfolio coordinator: {decision.reason}"
                continue
            trade = decision.proposal
        if scope.extended_hours:
            trade.update({
                "order_type": "limit",
                "limit_price": execution_price,
                "extended_hours": True,
            })
        elif scope.mode == "algo" and action in {"buy", "short"}:
            # Regular-session entries use a limit band so a fill never chases the signal.
            limit = entry_limit_price(action, float(trade.get("entry_price") or execution_price),
                                      STRATEGY_CONFIG["entry_limit_band_bps"])
            if limit is not None:
                trade.update({"order_type": "limit", "limit_price": limit})
        signal_kind = "protective" if item.get("software_protection") else "rule"
        identity = (
            f"{scope.mode}:{scope.today}:{item['strategy']}:{item['ticker']}:{item['action']}:{signal_kind}:"
            f"{item.get('event_id') or ''}:{item.get('signal_bar_time') or ''}"
        )
        trade["client_order_id"] = item.get("retry_client_order_id") or (
            f"rule-{scope.today.replace('-', '')}-"
            f"{hashlib.sha256(identity.encode()).hexdigest()[:12]}"
        )
        trade["signal_bar_time"] = item.get("signal_bar_time")
        if scope.mode == "algo":
            regime_name = market.regime_name
            trade["market_regime"] = regime_name
            owner_state = (scope.strategy_states or {}).get(item["strategy"], {}).get(item["ticker"], {})
            trade["research_profile_id"] = (
                owner_state.get("research_profile_id")
                if item["action"] in {"sell", "cover"} and owner_state.get("research_profile_id")
                else profile_id_for_regime(item["strategy"], regime_name)
            )
            trade["research_profile_regime"] = (
                owner_state.get("research_profile_regime", regime_name)
                if item["action"] in {"sell", "cover"}
                else regime_name
            )
        if item.get("event_id"):
            trade["event_id"] = item["event_id"]
        if item.get("strategy") == "swing_event_driven" and market.event_generator is not None:
            try:
                market.event_generator.record_proposal(
                    item, as_of=now_utc,
                    strategy_state=scope.strategy_states.setdefault("swing_event_driven", {}),
                )
            except (TypeError, ValueError):
                scope.scan_errors.append("event strategy cooldown state could not be recorded")

        item_orders = scope.pending_by_symbol.get(item["ticker"], [])
        held_qty = float((scope.positions.get(item["ticker"]) or {}).get("qty", 0) or 0)
        protective_exit = any(_is_protective_sell_order(order, held_qty) for order in item_orders)
        state = (scope.strategy_states or {}).get(item["strategy"], {}).get(item["ticker"], {})
        pending_intent = state.get("pending_exit_intent") if isinstance(state, dict) else None
        if (
            trade["action"] in {"sell", "cover"}
            and isinstance(pending_intent, dict)
            and pending_intent.get("retryable") is True
        ):
            if any(
                str(order.get("status") or "").lower() in {"pending_cancel", "pending_replace"}
                for order in item_orders
            ):
                item["suppressed_reason"] = "waiting for the broker order cancellation to finish"
                continue
            retry_after = pending_intent.get("retry_after")
            if item_orders and retry_after:
                try:
                    retry_after_time = datetime.fromisoformat(str(retry_after).replace("Z", "+00:00"))
                    if retry_after_time.tzinfo is None:
                        retry_after_time = retry_after_time.replace(tzinfo=timezone.utc)
                    if retry_after_time > now_utc:
                        item["suppressed_reason"] = "waiting before retrying the broker order cancellation"
                        continue
                except ValueError:
                    pass
        if (
            trade["action"] in {"sell", "cover"}
            and scope.session_name != "regular"
            and isinstance(pending_intent, dict)
            and pending_intent.get("retryable") is True
        ):
            item["suppressed_reason"] = "persisted strategy exit deferred until regular hours"
            continue
        if trade["action"] in {"sell", "cover"} and (
            protective_exit or item.get("coordinator_force_flatten")
        ):
            trade["cancel_open_orders"] = True
        if item.get("software_protection"):
            trade["software_protection"] = True
        if item.get("coordinator_force_flatten"):
            trade["coordinator_force_flatten"] = True
        trades.append(trade)
        if action in {"buy", "short"}:
            batch_entry_counts[strategy] = batch_entry_counts.get(strategy, 0) + 1
            batch_open_counts[strategy] = batch_open_counts.get(strategy, 0) + 1
            for group_name, group in group_limits.items():
                if strategy in group.get("strategies", []):
                    group_entry_counts[group_name] = group_entry_counts.get(group_name, 0) + 1
        item["entry_price"] = price
        item["qty"] = trade["qty"]

    return ExecutionResult(
        trades=trades,
        entries_paused=entries_paused,
        options_delta_overlay=options_delta_overlay,
        options_overlay_status=options_overlay_status,
    )
