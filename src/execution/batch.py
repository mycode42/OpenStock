"""Per-trade risk checks and execution for a decision batch."""

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class BatchHooks:
    risk_manager_available: bool
    rm_validate_trade: Callable
    rm_stage_approved_trade: Callable
    get_latest_ask: Callable
    get_latest_bid: Callable
    get_latest_price: Callable
    validate_trade_legacy: Callable
    cancel_open_orders_for_symbol: Callable
    get_positions: Callable
    place_order: Callable
    default_target_multiplier: float
    market_tz: ZoneInfo
    log: logging.Logger


def execute_trade_batch(
    trades: list[dict],
    decision_digest: str,
    args,
    mode: str,
    positions: dict,
    stop_loss_pct: float,
    rm_portfolio_state: dict | None,
    hooks: BatchHooks,
) -> tuple[list[dict], int, int]:
    results = []
    submitted_count = 0
    would_execute_count = 0
    seen_client_order_ids: set[str] = set()

    # The risk manager counts and validates entries against staged portfolio
    # state. Do not truncate this list: exits later in the batch must still be
    # allowed after the day's entry quota is full.
    for trade_index, trade in enumerate(trades):
        ticker = trade["ticker"]
        action = trade["action"].lower()
        qty = int(trade.get("qty", 0))
        reasoning = trade.get("reasoning", "")
        order_type = trade.get("order_type", "market")
        stop_price = trade.get("stop_price")
        take_profit = trade.get("take_profit")
        limit_price = trade.get("limit_price")
        trail_percent = trade.get("trail_percent")
        extended_hours = bool(trade.get("extended_hours", False))
        entry_price = trade.get("entry_price")
        strategy = trade.get("strategy", "unspecified")
        timeframe = trade.get("timeframe", "unknown")
        direction = trade.get("direction") or ("long" if action == "buy" else "short" if action == "short" else "close_short" if action == "cover" else "close_long")
        strategy_group = trade.get("strategy_group")
        client_order_id = str(trade.get("client_order_id") or (f"sw-{mode[:2]}-{datetime.now(hooks.market_tz).date():%Y%m%d}-{trade_index}-{decision_digest}"))[:48]

        if client_order_id in seen_client_order_ids:
            results.append(
                {
                    "ticker": ticker,
                    "action": action,
                    "qty": qty,
                    "status": "blocked",
                    "reason": "Duplicate client order ID in decision batch",
                    "rule": "duplicate_client_order_id",
                    "strategy": strategy,
                    "client_order_id": client_order_id,
                }
            )
            continue
        seen_client_order_ids.add(client_order_id)

        if action == "hold" or qty <= 0:
            results.append({"ticker": ticker, "action": action, "status": "skipped", "reason": "Hold or zero qty"})
            continue

        if not hooks.risk_manager_available:
            reason = "V2 risk manager unavailable; order blocked"
            hooks.log.error(reason)
            results.append(
                {
                    "ticker": ticker,
                    "action": action,
                    "qty": qty,
                    "status": "blocked",
                    "reason": reason,
                    "strategy": strategy,
                }
            )
            continue

        # ── V2 Risk Manager validation ───────────────────────────────────────
        if action in ("buy", "short") and hooks.risk_manager_available:
            pos = positions.get(ticker, {})
            try:
                hinted_price = float(entry_price or 0)
            except (TypeError, ValueError):
                hinted_price = 0.0
            if not math.isfinite(hinted_price) or hinted_price <= 0:
                hinted_price = 0.0
            try:
                limit_reference = float(limit_price or 0)
            except (TypeError, ValueError):
                limit_reference = 0.0
            if not math.isfinite(limit_reference) or limit_reference <= 0:
                limit_reference = 0.0
            has_buy_limit = order_type == "limit" or (order_type == "bracket" and limit_price is not None)
            if not args.dry_run and action == "buy" and has_buy_limit:
                # A buy limit caps the actual fill price. Use that ceiling
                # even when the signal supplies a much lower stale mark.
                if limit_reference <= 0:
                    results.append(
                        {
                            "ticker": ticker,
                            "action": action,
                            "qty": qty,
                            "status": "blocked",
                            "reason": "Live buy limit price is missing or invalid",
                            "rule": "invalid_buy_limit",
                            "strategy": strategy,
                        }
                    )
                    continue
                ref_price = max(hinted_price, limit_reference)
                entry_price = limit_reference
            elif not args.dry_run and action == "buy":
                try:
                    live_ask = hooks.get_latest_ask(ticker, mode=mode)
                except Exception as exc:
                    results.append(
                        {
                            "ticker": ticker,
                            "action": action,
                            "qty": qty,
                            "status": "blocked",
                            "reason": f"Fresh broker ask unavailable: {exc}",
                            "rule": "fresh_ask_unavailable",
                            "strategy": strategy,
                        }
                    )
                    continue
                ref_price = max(hinted_price, live_ask)
                entry_price = ref_price
            elif not args.dry_run and action == "short":
                try:
                    live_bid = hooks.get_latest_bid(ticker, mode=mode)
                except Exception as exc:
                    results.append(
                        {
                            "ticker": ticker,
                            "action": action,
                            "qty": qty,
                            "status": "blocked",
                            "reason": f"Fresh broker bid unavailable: {exc}",
                            "rule": "fresh_bid_unavailable",
                            "strategy": strategy,
                        }
                    )
                    continue
                ref_price = max(hinted_price, limit_reference, live_bid)
                entry_price = ref_price
            else:
                ref_price = max(
                    hinted_price,
                    limit_reference,
                    float(pos.get("current_price", 0) or 0),
                )
                if ref_price <= 0:
                    try:
                        ref_price = hooks.get_latest_price(ticker)
                    except Exception as exc:
                        ref_price = 0.0
                        hooks.log.warning("No reference price for %s: %s", ticker, exc)
            if action == "short":
                # Require broker-side protection for every short. For market
                # entries, derive missing levels from the mode's configured
                # hard stop and the shared target multiple. Limit entries need
                # explicit levels because the fill may differ from the mark.
                auto_bracket = mode in {"swing", "day"} and (order_type == "market" or (order_type == "bracket" and limit_price is None))
                if auto_bracket and ref_price > 0:
                    if stop_price is None:
                        stop_price = round(ref_price * (1 + stop_loss_pct), 2)
                    if take_profit is None:
                        stop_dist = abs(ref_price - float(stop_price))
                        take_profit = round(
                            ref_price - stop_dist * hooks.default_target_multiplier,
                            2,
                        )
                try:
                    stop_value = float(stop_price)
                    target_value = float(take_profit)
                except (TypeError, ValueError):
                    stop_value = target_value = 0.0
                if not (target_value > 0 and target_value < ref_price < stop_value):
                    results.append(
                        {
                            "ticker": ticker,
                            "action": action,
                            "qty": qty,
                            "status": "blocked",
                            "reason": "Short bracket no longer surrounds the fresh entry reference price",
                            "rule": "stale_short_bracket",
                            "strategy": strategy,
                        }
                    )
                    continue
            rm_result = hooks.rm_validate_trade(
                ticker=ticker,
                action=action,
                qty=qty,
                entry_price=ref_price,
                portfolio_state=rm_portfolio_state,
                mode=mode,
                strategy=strategy,
            )
            if not rm_result.approved:
                hooks.log.warning(
                    "V2 risk manager BLOCKED %s %s %s %s (%s): %s",
                    strategy,
                    timeframe,
                    direction,
                    ticker,
                    action,
                    rm_result.reason,
                )
                results.append(
                    {
                        "ticker": ticker,
                        "action": action,
                        "qty": qty,
                        "status": "blocked",
                        "reason": rm_result.reason,
                        "rule": rm_result.rule,
                        "strategy": strategy,
                    }
                )
                continue
            hooks.log.info(
                "V2 risk manager approved %s %s %s %s (%s)",
                strategy,
                timeframe,
                direction,
                ticker,
                action,
            )
        elif action in ("sell", "cover") and hooks.risk_manager_available:
            # The central validator explicitly allows exits; route them through
            # it too so every submitted signal uses the same policy boundary.
            rm_result = hooks.rm_validate_trade(
                ticker=ticker,
                action=action,
                qty=qty,
                entry_price=float(entry_price or 0),
                portfolio_state=rm_portfolio_state,
                mode=mode,
                strategy=strategy,
            )
            if not rm_result.approved:
                results.append(
                    {
                        "ticker": ticker,
                        "action": action,
                        "qty": qty,
                        "status": "blocked",
                        "reason": rm_result.reason,
                        "rule": rm_result.rule,
                        "strategy": strategy,
                    }
                )
                continue
        elif action not in ("sell", "cover"):
            reason = f"Unsupported action '{action}'"
            results.append(
                {
                    "ticker": ticker,
                    "action": action,
                    "qty": qty,
                    "status": "blocked",
                    "reason": reason,
                    "strategy": strategy,
                }
            )
            continue

        # ── Auto-calculate swing long brackets (mandatory stops) ──────────────
        if mode == "swing" and action == "buy" and order_type not in ("limit", "stop", "trailing_stop", "oco"):
            pos = positions.get(ticker, {})
            ref = entry_price or limit_price or float(pos.get("current_price", 0))
            if ref > 0:
                if stop_price is None:
                    stop_price = round(ref * (1 - stop_loss_pct), 2)
                if take_profit is None and stop_price is not None:
                    stop_dist = abs(ref - float(stop_price))
                    take_profit = round(ref + stop_dist * hooks.default_target_multiplier, 2)

            if stop_price is not None:
                hooks.log.info(f"{action.upper()} {ticker}: bracket order with stop=${stop_price:.2f} " f"target=${take_profit:.2f}")

        if args.dry_run:
            dry_entry = {
                "ticker": ticker,
                "action": action,
                "qty": qty,
                "status": "would_execute",
                "reasoning": reasoning,
                "order_type": order_type,
                "extended_hours": extended_hours,
                "strategy": strategy,
                "client_order_id": client_order_id,
            }
            if strategy_group:
                dry_entry["strategy_group"] = strategy_group
            if stop_price is not None:
                dry_entry["stop_price"] = stop_price
            if take_profit is not None:
                dry_entry["take_profit"] = take_profit
            if limit_price is not None:
                dry_entry["limit_price"] = limit_price
            if trail_percent is not None:
                dry_entry["trail_percent"] = trail_percent
            if trade.get("cancel_open_orders"):
                dry_entry["cancel_open_orders"] = True
            results.append(dry_entry)
            would_execute_count += 1
            if action in {"buy", "short"} and hooks.risk_manager_available and rm_portfolio_state is not None:
                rm_portfolio_state = hooks.rm_stage_approved_trade(
                    rm_portfolio_state,
                    ticker,
                    action,
                    qty,
                    float(ref_price),
                    mode=mode,
                )
        else:
            if action in {"sell", "cover"} and (trade.get("cancel_open_orders") or order_type in {"market", "limit"}):
                cancel_result = hooks.cancel_open_orders_for_symbol(
                    ticker,
                    mode=mode,
                    allow_protective=bool(trade.get("coordinator_force_flatten") or trade.get("software_protection") or trade.get("cancel_open_orders")),
                )
                if not cancel_result.get("success"):
                    results.append(
                        {
                            "ticker": ticker,
                            "action": action,
                            "qty": qty,
                            "status": "failed",
                            "reason": cancel_result.get("error"),
                            "strategy": strategy,
                            "client_order_id": client_order_id,
                        }
                    )
                    continue
                if cancel_result.get("cancelled"):
                    hooks.log.info("Cancelled %d open order(s) for %s before strategy exit", len(cancel_result["cancelled"]), ticker)
                # An order may fill while being cancelled. Never use the
                # earlier position quantity to submit a reducing order.
                try:
                    positions = hooks.get_positions()
                except Exception as exc:
                    results.append(
                        {
                            "ticker": ticker,
                            "action": action,
                            "qty": qty,
                            "status": "failed",
                            "reason": f"Could not reconcile exit position: {exc}",
                            "strategy": strategy,
                            "client_order_id": client_order_id,
                        }
                    )
                    continue
                valid, reason = hooks.validate_trade_legacy(
                    ticker,
                    action,
                    qty,
                    positions,
                )
                if not valid:
                    results.append(
                        {
                            "ticker": ticker,
                            "action": action,
                            "qty": qty,
                            "status": "blocked",
                            "reason": reason,
                            "strategy": strategy,
                            "client_order_id": client_order_id,
                        }
                    )
                    continue
            # Short brackets are populated from the fresh quote above. Swing
            # longs may use hooks.place_order()'s stop_pct fallback.
            result = hooks.place_order(
                ticker,
                action,
                qty,
                order_type=order_type,
                stop_price=stop_price,
                take_profit=take_profit,
                limit_price=limit_price,
                trail_percent=trail_percent,
                entry_price=entry_price,
                stop_pct=stop_loss_pct if mode == "swing" and action == "buy" else None,
                client_order_id=client_order_id,
                extended_hours=extended_hours,
            )
            status = "submitted" if result["success"] else "failed"
            results.append(
                {
                    "ticker": ticker,
                    "action": action,
                    "qty": qty,
                    "status": status,
                    "submission_status": status,
                    "reasoning": reasoning,
                    "strategy": strategy,
                    "strategy_group": strategy_group,
                    "client_order_id": client_order_id,
                    **result,
                }
            )
            if result["success"]:
                if not result.get("idempotent_replay"):
                    submitted_count += 1
                if action in {"buy", "short"} and not result.get("idempotent_replay") and hooks.risk_manager_available and rm_portfolio_state is not None:
                    rm_portfolio_state = hooks.rm_stage_approved_trade(
                        rm_portfolio_state,
                        ticker,
                        action,
                        qty,
                        float(ref_price),
                        mode=mode,
                    )

    return results, submitted_count, would_execute_count
