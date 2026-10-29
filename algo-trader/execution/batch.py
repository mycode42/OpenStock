"""Per-trade risk checks and execution for a decision batch."""

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from execution.risk_models import ValidationResult
from market.calendar import extended_session_name
from market.data import feeds_for_session
from portfolio.config import get_cash_reserve_settings, is_cash_proxy
from portfolio.protection.software import exit_limit_price, exit_order_policy


@dataclass(frozen=True)
class BatchHooks:
    risk_validator_available: bool
    rm_validate_trade: Callable
    rm_stage_approved_trade: Callable
    get_latest_ask: Callable
    get_latest_bid: Callable
    get_latest_price: Callable
    validate_position_reduction: Callable
    open_exit_orders_for_symbol: Callable
    get_positions: Callable
    place_order: Callable
    market_tz: ZoneInfo
    log: logging.Logger
    get_market_schedule: Callable | None = None


def _extended_limit_trade(trade: dict) -> bool:
    """Extended-hours entries and exits must be plain limit orders with a positive limit."""
    try:
        limit_price = float(trade.get("limit_price") or 0)
    except (TypeError, ValueError):
        return False
    return (
        bool(trade.get("extended_hours"))
        and str(trade.get("order_type") or "").lower() == "limit"
        and str(trade.get("action") or "").lower() in {"buy", "sell", "cover"}
        and math.isfinite(limit_price) and limit_price > 0
    )


_CASH_RESERVE_APPROVAL = ValidationResult(approved=True, reason="cash reserve: idle-cash storage, not risk-gated")


def _cash_reserve_bypass(trade: dict, action: str, qty: int, reference_price: float, positions: dict, rm_portfolio_state: dict | None) -> bool:
    """Only the reserve ETF may skip the risk gate, and only within its size cap.

    A buy is capped at the reserve's maximum share of equity; a sell cannot
    exceed the proxy shares held. Any other trade, even one flagged
    cash_reserve, goes through the normal risk validator.
    """
    ticker = str(trade.get("ticker") or "")
    if not trade.get("cash_reserve") or not is_cash_proxy(ticker) or action not in {"buy", "sell"}:
        return False
    if action == "sell":
        held = float((positions.get(ticker) or {}).get("qty", 0) or 0)
        return 0 < qty <= held
    equity = float((rm_portfolio_state or {}).get("equity") or 0)
    return (
        math.isfinite(reference_price) and reference_price > 0 and equity > 0
        and qty * reference_price <= float(get_cash_reserve_settings()["max_proxy_pct"]) * equity
    )


def _extended_exit_trade(trade: dict, session_name: str, hooks: BatchHooks, mode: str) -> tuple[dict | None, str | None]:
    """Price a sell or cover for an extended session as a marketable limit.

    Market orders are not accepted outside regular hours, so the limit is set
    beyond the closing side of the quote. Returns (trade, None) or (None, reason).
    """
    action = str(trade.get("action") or "").lower()
    if _extended_limit_trade(trade):
        return trade, None
    policy = exit_order_policy(session_name)
    try:
        closing_quote = (
            hooks.get_latest_bid(
                trade["ticker"], mode=mode, feed=feeds_for_session(session_name)[1],
            ) if action == "sell" else hooks.get_latest_ask(
                trade["ticker"], mode=mode, feed=feeds_for_session(session_name)[1],
            )
        )
        limit_price = exit_limit_price(action, closing_quote, closing_quote, session_name)
    except Exception as exc:
        return None, f"Extended-hours exit quote unavailable: {type(exc).__name__}: {exc}"
    return {
        **trade,
        "order_type": policy["order_type"],
        "limit_price": limit_price,
        "extended_hours": True,
    }, None


def _off_hours_row(trade: dict, reason: str, rule: str) -> dict:
    action = str(trade.get("action") or "").lower()
    row = {
        "ticker": trade.get("ticker"),
        "action": action,
        "qty": trade.get("qty"),
        "status": "failed" if action in {"sell", "cover"} else "blocked",
        "reason": reason,
        "rule": rule,
        "strategy": trade.get("strategy"),
        "client_order_id": trade.get("client_order_id"),
    }
    if action in {"sell", "cover"}:
        row["safe_to_retry"] = True
    return row


def execute_trade_batch(
    trades: list[dict],
    decision_digest: str,
    args,
    mode: str,
    positions: dict,
    rm_portfolio_state: dict | None,
    hooks: BatchHooks,
) -> tuple[list[dict], int, int]:
    results = []
    submitted_count = 0
    would_execute_count = 0
    seen_client_order_ids: set[str] = set()
    extended_exit_session = None

    # Session detection only decides how exits are priced in extended hours. It never
    # drops an order; the broker has the final word on timing.
    if mode == "algo" and trades:
        extended_session = None
        try:
            schedule = (
                hooks.get_market_schedule(mode="algo")
                if hooks.get_market_schedule is not None else None
            )
            now = datetime.now(hooks.market_tz)
            regular_open = bool(schedule is not None and schedule.market_open(now))
            if not regular_open and hooks.get_market_schedule is not None:
                extended_session = extended_session_name(
                    now,
                    lambda on_date: hooks.get_market_schedule(on_date=on_date, mode="algo"),
                )
        except Exception:
            extended_session = None
            regular_open = False
        if not regular_open:
            extended_exit_session = extended_session

    # The risk validator counts and validates entries against staged portfolio
    # state. Do not truncate this list: exits later in the batch must still be
    # allowed after the day's entry quota is full.
    for trade_index, trade in enumerate(trades):
        if extended_exit_session and str(trade.get("action") or "").lower() in {"sell", "cover"}:
            priced_trade, pricing_error = _extended_exit_trade(trade, extended_exit_session, hooks, mode)
            if priced_trade is None:
                results.append({
                    "ticker": trade["ticker"],
                    "action": str(trade.get("action") or "").lower(),
                    "qty": trade.get("qty"),
                    "status": "failed",
                    "reason": pricing_error,
                    "strategy": trade.get("strategy"),
                    "client_order_id": trade.get("client_order_id"),
                    "safe_to_retry": True,
                })
                continue
            trade = priced_trade
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

        if not hooks.risk_validator_available:
            reason = "risk validator unavailable; order blocked"
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

        # ── Risk validator ──────────────────────────────────────────────────
        if action in ("buy", "short") and hooks.risk_validator_available:
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
            has_buy_limit = order_type == "limit"
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
                # Algo shorts require explicit broker-side protection. The
                # signal generator supplies levels before execution.
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
                            "reason": "Short exit levels no longer surround the fresh entry reference price",
                            "rule": "stale_short_bracket",
                            "strategy": strategy,
                        }
                    )
                    continue
            rm_result = _CASH_RESERVE_APPROVAL if _cash_reserve_bypass(
                trade, action, qty, float(ref_price), positions, rm_portfolio_state,
            ) else hooks.rm_validate_trade(
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
                    "risk validator BLOCKED %s %s %s %s (%s): %s",
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
                "risk validator approved %s %s %s %s (%s)",
                strategy,
                timeframe,
                direction,
                ticker,
                action,
            )
        elif action in ("sell", "cover") and hooks.risk_validator_available:
            # The central validator explicitly allows exits; route them through
            # it too so every submitted signal uses the same policy boundary.
            rm_result = _CASH_RESERVE_APPROVAL if _cash_reserve_bypass(
                trade, action, qty, float(entry_price or 0), positions, rm_portfolio_state,
            ) else hooks.rm_validate_trade(
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
            results.append(dry_entry)
            would_execute_count += 1
            if action in {"buy", "short"} and hooks.risk_validator_available and rm_portfolio_state is not None:
                rm_portfolio_state = hooks.rm_stage_approved_trade(
                    rm_portfolio_state,
                    ticker,
                    action,
                    qty,
                    float(ref_price),
                    mode=mode,
                )
        else:
            # Exits never cancel broker orders: protection is software-managed,
            # so the broker holds no stop legs to remove. An exit already open
            # at the broker blocks a second exit for the same symbol.
            if action in {"sell", "cover"}:
                try:
                    open_exits = hooks.open_exit_orders_for_symbol(ticker, mode=mode, action=action)
                except Exception as exc:
                    results.append(
                        {
                            "ticker": ticker,
                            "action": action,
                            "qty": qty,
                            "status": "failed",
                            "reason": f"Could not verify open exit orders: {type(exc).__name__}: {exc}",
                            "strategy": strategy,
                            "client_order_id": client_order_id,
                            "safe_to_retry": True,
                        }
                    )
                    continue
                if open_exits:
                    results.append(
                        {
                            "ticker": ticker,
                            "action": action,
                            "qty": qty,
                            "status": "failed",
                            "reason": "An exit order is already open at the broker; no duplicate exit is submitted",
                            "rule": "exit_in_progress",
                            "strategy": strategy,
                            "client_order_id": client_order_id,
                            "safe_to_retry": True,
                        }
                    )
                    continue
                # An order may fill after the scan snapshot. Never use the
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
                            "safe_to_retry": True,
                        }
                    )
                    continue
                valid, reason = hooks.validate_position_reduction(
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
            # Long and short entry levels are supplied by the Algo signal.
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
                if action in {"buy", "short"} and not result.get("idempotent_replay") and hooks.risk_validator_available and rm_portfolio_state is not None:
                    rm_portfolio_state = hooks.rm_stage_approved_trade(
                        rm_portfolio_state,
                        ticker,
                        action,
                        qty,
                        float(ref_price),
                        mode=mode,
                    )

    return results, submitted_count, would_execute_count
