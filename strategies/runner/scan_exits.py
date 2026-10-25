"""Persist and resume strategy exits that cannot be submitted immediately."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib

from .scan_types import ScanScope


def prepare_strategy_exits(scope: ScanScope, generated: list[dict]) -> None:
    """Persist off-session exits and restore retryable exits during regular hours."""
    now_utc = scope.now_utc or datetime.now(timezone.utc)
    if scope.session_name != "regular" and scope.strategy_states is not None:
        for item in generated:
            action = str(item.get("action") or "").lower()
            if action not in {"sell", "cover"}:
                continue
            strategy = str(item.get("strategy") or "")
            symbol = str(item.get("ticker") or "").upper()
            if not strategy or not symbol or scope.owner_map.get(symbol) != strategy:
                continue
            state = (scope.strategy_states.get(strategy) or {}).get(symbol)
            if not isinstance(state, dict) or state.get("phase") != "open":
                continue
            if isinstance(state.get("pending_exit_intent"), dict):
                continue
            signal_kind = "protective" if item.get("software_protection") else "rule"
            identity = (
                f"{scope.mode}:{scope.today}:{strategy}:{symbol}:{action}:{signal_kind}:"
                f"{item.get('event_id') or ''}:{item.get('signal_bar_time') or ''}"
            )
            client_order_id = (
                f"rule-{scope.today.replace('-', '')}-"
                f"{hashlib.sha256(identity.encode()).hexdigest()[:12]}"
            )
            state["pending_exit_intent"] = {
                "action": action,
                "strategy": strategy,
                "reason": str(item.get("reason") or "deferred strategy exit"),
                "timeframe": item.get("timeframe"),
                "signal_bar_time": item.get("signal_bar_time"),
                "event_id": item.get("event_id"),
                "client_order_id": client_order_id,
                "software_protection": bool(item.get("software_protection")),
                "coordinator_force_flatten": bool(item.get("coordinator_force_flatten")),
                "retryable": True,
                "status": "deferred_until_regular_hours",
                "created_at": now_utc.isoformat(),
            }
            state["updated_at"] = now_utc.isoformat()

    if scope.session_name == "regular" and scope.strategy_states is not None:
        generated_exit_keys = {
            (
                str(item.get("strategy") or ""),
                str(item.get("ticker") or "").upper(),
                str(item.get("action") or "").lower(),
            )
            for item in generated
        }
        for strategy, by_symbol in scope.strategy_states.items():
            for raw_symbol, state in by_symbol.items():
                symbol = str(raw_symbol).upper()
                intent = state.get("pending_exit_intent") if isinstance(state, dict) else None
                if (
                    not isinstance(intent, dict)
                    or intent.get("retryable") is not True
                    or scope.owner_map.get(symbol) != strategy
                ):
                    continue
                symbol_orders = scope.pending_by_symbol.get(symbol, [])
                if any(
                    str(order.get("status") or "").lower() in {"pending_cancel", "pending_replace"}
                    for order in symbol_orders
                ):
                    continue
                retry_after = intent.get("retry_after")
                if symbol_orders and retry_after:
                    try:
                        retry_after_time = datetime.fromisoformat(
                            str(retry_after).replace("Z", "+00:00"),
                        )
                        if retry_after_time.tzinfo is None:
                            retry_after_time = retry_after_time.replace(tzinfo=timezone.utc)
                        if retry_after_time > now_utc:
                            continue
                    except ValueError:
                        pass
                quantity = float((scope.positions.get(symbol) or {}).get("qty", 0) or 0)
                if quantity == 0:
                    continue
                action = "cover" if quantity < 0 else "sell"
                if (strategy, symbol, action) in generated_exit_keys:
                    continue
                generated.append({
                    "ticker": symbol,
                    "strategy": strategy,
                    "action": action,
                    "reason": str(intent.get("reason") or "resume previously blocked strategy exit"),
                    "timeframe": intent.get("timeframe"),
                    "signal_bar_time": intent.get("signal_bar_time"),
                    "event_id": intent.get("event_id"),
                    "software_protection": bool(intent.get("software_protection")),
                    "coordinator_force_flatten": bool(intent.get("coordinator_force_flatten")),
                    "retry_client_order_id": intent.get("client_order_id"),
                    "deferred_exit_retry": True,
                })
                generated_exit_keys.add((strategy, symbol, action))

    if scope.strategy_states is not None:
        retryable_exit_symbols = sorted({
            str(symbol).upper()
            for by_symbol in scope.strategy_states.values()
            for symbol, state in by_symbol.items()
            if isinstance(state, dict)
            and isinstance(state.get("pending_exit_intent"), dict)
            and state["pending_exit_intent"].get("retryable") is True
        })
        if retryable_exit_symbols:
            scope.entry_pause_reasons.append(
                "Algo strategy exits are deferred until regular hours or broker cancellation: "
                + ", ".join(retryable_exit_symbols)
            )
