"""Conservative, coordinator-only repair of a missing Algo bracket stop.

The routine can run in every active Alpaca session. It persists a stable OCO
client ID before using the canonical executor, and broker orders remain the
only evidence that a replacement actually protects the whole holding.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from portfolio.incidents import record_incident, resolve_incident
from portfolio.protection.evidence import current_recovery, normalize_broker_price, recovery_oco_order, to_finite_decimal
TERMINAL_ORDER_STATUSES = frozenset({"filled", "canceled", "expired", "rejected", "replaced"})
RETRY_SECONDS = 90
MAX_DECISIONS = 3
MAX_SUBMISSIONS_PER_DECISION = 3


def _price(value: object) -> Decimal | None:
    return normalize_broker_price(value)


def _timestamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def _age_seconds(now: datetime, value: object) -> float | None:
    parsed = _timestamp(value)
    return (now - parsed).total_seconds() if parsed else None


def _finish_report(portfolio: dict, report: dict, previous_report: object, now: datetime) -> dict:
    """Persist a clear, age-tracked recovery status on every return path."""
    waiting = report.get("waiting") or []
    if waiting:
        previous = previous_report if isinstance(previous_report, dict) else {}
        previous_waiting = previous.get("waiting") or []
        previous_by_item = previous.get("waiting_since_by_item")
        previous_by_item = previous_by_item if isinstance(previous_by_item, dict) else {}
        since_by_item: dict[str, str] = {}
        item_ages: list[float] = []
        for item in waiting:
            since = previous_by_item.get(item)
            if since is None and item in previous_waiting:
                since = previous.get("waiting_since") or previous.get("checked_at")
            age = _age_seconds(now, since)
            if age is None or age < 0:
                since = now.isoformat()
                age = 0.0
            since_by_item[item] = str(since)
            item_ages.append(age)
        report["waiting_since_by_item"] = since_by_item
        report["waiting_since"] = min(since_by_item.values(), key=lambda value: _timestamp(value) or now)
        report["waiting_age_seconds"] = int(max(item_ages, default=0.0))
    portfolio["protection_recovery_status"] = report
    return report


def _wait_report(status: str, message: str, now: datetime) -> dict:
    return {
        "status": status, "checked_at": now.isoformat(), "attempted": [],
        "submitted": [], "verified": [], "waiting": [message], "errors": [],
    }


def _open_symbol_orders(symbol: str, open_orders: list[dict], nested_orders: list[dict]) -> list[dict]:
    """Treat pending cancellation and unrecognized nonterminal states as open."""
    matching = [
        order for order in open_orders
        if not isinstance(order, dict) or str(order.get("symbol") or "").upper() == symbol
    ]
    for parent in nested_orders:
        if not isinstance(parent, dict):
            matching.append({"status": "unknown"})
            continue
        legs = parent.get("legs")
        if legs is not None and not isinstance(legs, list):
            matching.append(parent)
            continue
        for order in [parent, *(legs or [])]:
            if not isinstance(order, dict):
                continue
            if str(order.get("symbol") or parent.get("symbol") or "").upper() != symbol:
                continue
            if str(order.get("status") or "").lower() not in TERMINAL_ORDER_STATUSES:
                matching.append(order)
    return matching


def _exact_nested(broker, order_id: str, symbol: str) -> dict:
    order = broker.order(order_id, nested=True)
    if (
        not isinstance(order, dict)
        or str(order.get("id") or "") != order_id
        or str(order.get("symbol") or "").upper() != symbol
    ):
        raise ValueError(f"Exact nested broker order {order_id} is incomplete for {symbol}")
    return order


def _decision_id(symbol: str, entry_id: str, qty: Decimal, stop: Decimal, target: Decimal, attempt: int) -> str:
    source = f"{symbol}|{entry_id}|{qty}|{stop}|{target}|{attempt}"
    digest = hashlib.sha256(source.encode()).hexdigest()[:24]
    return f"algo-oco-{symbol[:8]}-{digest}"


def _same_decision(recovery: dict, entry_id: str, qty: Decimal, stop: Decimal, target: Decimal) -> bool:
    return (
        recovery.get("entry_order_id") == entry_id
        and to_finite_decimal(recovery.get("qty")) == qty
        and to_finite_decimal(recovery.get("stop_price")) == stop
        and to_finite_decimal(recovery.get("take_profit")) == target
    )


def _definitive_extended_hours_rejection(recovery: dict) -> bool:
    """Whether Alpaca definitively rejected an OCO for extended-hours eligibility."""
    error = str(recovery.get("last_error") or "").lower()
    return (
        str(recovery.get("submission_status") or "").lower() in {"failed", "rejected"}
        and "40310000" in error
        and "oco orders do not support extended hours trading" in error
    )


def _lookup_matches(order: dict, symbol: str, recovery: dict) -> bool:
    expected_side = str(recovery.get("side") or "sell").lower()
    return (
        str(order.get("client_order_id") or "") == recovery.get("client_order_id")
        and str(order.get("symbol") or "").upper() == symbol
        and str(order.get("side") or "").lower() == expected_side
        and str(order.get("type") or "").lower() == "limit"
        and str(order.get("order_class") or "").lower() == "oco"
        and to_finite_decimal(order.get("qty")) == to_finite_decimal(recovery.get("qty"))
        and to_finite_decimal(order.get("limit_price")) == to_finite_decimal(recovery.get("take_profit"))
        and (
            not recovery.get("order_id")
            or str(order.get("id") or "") == str(recovery["order_id"])
        )
    )


def _fresh_inside_quote(
    broker, symbol: str, stop: Decimal, target: Decimal, now: datetime, *,
    feed: str, is_short: bool = False,
) -> Decimal | None:
    quote = broker.latest_quotes([symbol], feed=feed).get(symbol)
    if not isinstance(quote, dict):
        return None
    age = _age_seconds(now, quote.get("timestamp"))
    bid = to_finite_decimal(quote.get("bid"))
    ask = to_finite_decimal(quote.get("ask"))
    valid = bool(
        age is not None and -5 <= age <= 30
        and bid is not None and ask is not None
        and (ask < stop - Decimal("0.01") and bid > target + Decimal("0.01") if is_short else
             bid > stop + Decimal("0.01") and ask < target - Decimal("0.01"))
        and ask >= bid
    )
    return (ask if is_short else bid) if valid else None


def recover_broker_protection(
    portfolio: dict,
    *,
    window,
    broker,
    save: Callable[[], None],
    execute: Callable[..., dict],
    now: datetime | None = None,
) -> dict:
    """Repair at most one proven holding per pass; never cancel broker orders.

    Any open symbol order, including pending_cancel, prevents a submission.
    The executor independently rechecks the full live quantity and open order
    book under its account lock immediately before posting an OCO.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Protection recovery requires a timezone-aware clock")
    startup = portfolio.get("startup_reconciliation") or {}
    previous_report = portfolio.get("protection_recovery_status") or {}
    gaps = set(startup.get("unprotected_positions") or [])
    verified = set(startup.get("verified_positions") or [])
    if not gaps:
        return _finish_report(
            portfolio,
            {"status": "not_needed", "checked_at": now.isoformat(), "attempted": [],
             "submitted": [], "verified": [], "waiting": [], "errors": []},
            previous_report, now,
        )
    if not verified:
        return _finish_report(
            portfolio,
            _wait_report("waiting", "no broker-verified positions are available for protection repair", now),
            previous_report, now,
        )
    session_name = getattr(window, "name", None)
    extended_hours = session_name != "regular"
    if (
        session_name not in {"regular", "premarket", "after_hours", "overnight"}
        or bool(getattr(window, "extended_hours", extended_hours)) != extended_hours
    ):
        return _finish_report(
            portfolio,
            _wait_report("outside_trading_session", "replacement OCO submission is deferred outside a supported trading session", now),
            previous_report, now,
        )
    local_now = now.astimezone(window.regular_open.tzinfo)
    if session_name == "regular" and not window.regular_open <= local_now < window.regular_close:
        return _finish_report(
            portfolio,
            _wait_report("outside_trading_session", "replacement OCO submission is deferred outside regular market hours", now),
            previous_report, now,
        )
    checked_age = _age_seconds(now, startup.get("checked_at"))
    if checked_age is None or not -5 <= checked_age <= 600:
        return _finish_report(
            portfolio,
            _wait_report("stale_reconciliation", "startup broker reconciliation is stale; refresh it before repairing protection", now),
            previous_report, now,
        )
    if session_name == "regular":
        clock = broker.clock()
        if not isinstance(clock, dict) or clock.get("is_open") is not True:
            return _finish_report(
                portfolio,
                _wait_report("broker_market_closed", "broker reports the regular market is closed; replacement OCO is deferred", now),
                previous_report, now,
            )

    positions = broker.positions()
    if not isinstance(positions, list):
        raise ValueError("Broker protection reconciliation returned malformed positions")
    by_symbol: dict[str, dict] = {}
    for position in positions:
        if not isinstance(position, dict):
            raise ValueError("Broker positions include a malformed row")
        symbol = str(position.get("symbol") or "").upper()
        if not symbol or symbol in by_symbol:
            raise ValueError("Broker positions include a duplicate or unnamed symbol")
        by_symbol[symbol] = position

    report: dict = {
        "status": "waiting", "checked_at": now.isoformat(), "attempted": [], "submitted": [],
        "verified": [], "waiting": [], "errors": [],
    }
    owners = portfolio.get("position_owners") or {}
    states = portfolio.get("strategy_states") or {}
    for symbol in sorted(gaps & verified & set(by_symbol)):
        owner = owners.get(symbol)
        state = (states.get(owner) or {}).get(symbol) if owner else None
        pending_exit = state.get("pending_exit_intent") if isinstance(state, dict) else None
        if (
            session_name == "regular"
            and isinstance(pending_exit, dict)
            and pending_exit.get("retryable") is True
        ):
            report["waiting"].append(f"{symbol}: retryable strategy exit has priority over replacing protection")
            continue
        if not isinstance(state, dict) or state.get("phase") != "open" or state.get("owner") != owner:
            report["waiting"].append(f"{symbol}: no open strategy state matching the persisted owner")
            continue
        entry_id = state.get("entry_order_id")
        qty = to_finite_decimal(by_symbol[symbol].get("qty"))
        if not isinstance(entry_id, str) or not entry_id:
            report["waiting"].append(f"{symbol}: persisted entry order ID is missing")
            continue
        if qty is None or qty == 0:
            report["waiting"].append(f"{symbol}: current broker quantity is missing or zero")
            continue
        is_short = qty is not None and qty < 0
        quantity = abs(qty)
        if quantity != quantity.to_integral_value():
            report["waiting"].append(f"{symbol}: fractional broker quantity cannot use an integer-share OCO")
            continue
        if (startup.get("verified_entries") or {}).get(symbol) != entry_id:
            report["waiting"].append(f"{symbol}: entry order ID differs from broker-verified reconciliation")
            continue
        if to_finite_decimal((startup.get("verified_quantities") or {}).get(symbol)) != qty:
            report["waiting"].append(f"{symbol}: broker quantity changed since reconciliation")
            continue
        if to_finite_decimal(state.get("last_known_qty")) != qty:
            report["waiting"].append(f"{symbol}: broker quantity differs from persisted strategy state")
            continue
        stop = _price(state.get("stop_price"))
        target = _price(state.get("take_profit"))
        if stop is None:
            report["waiting"].append(f"{symbol}: persisted stop is not a valid positive broker price")
            continue
        if target is None:
            report["waiting"].append(f"{symbol}: persisted take-profit is not a valid positive broker price")
            continue
        crossed_levels = stop <= target if is_short else stop >= target
        if crossed_levels:
            report["waiting"].append(f"{symbol}: rounded stop and take-profit cross or coincide")
            continue

        # Symbol-filtered open orders avoid the account-wide 500-order cap.
        # Exact nested GETs keep an older bracket or OCO visible even when
        # neither appears in Alpaca's newest 500 all-order rows.
        symbol_open = broker.orders("open", nested=True, symbols=symbol)
        if (
            not isinstance(symbol_open, list)
            or len(symbol_open) >= 500
            or any(not isinstance(order, dict) for order in symbol_open)
        ):
            report["errors"].append(f"{symbol}: symbol open-order list is unavailable or capped")
            continue
        try:
            entry_nested = _exact_nested(broker, entry_id, symbol)
        except Exception as exc:
            report["errors"].append(f"{symbol}: exact entry order unavailable: {type(exc).__name__}: {exc}")
            continue
        entry_class = str(entry_nested.get("order_class") or "simple").lower()
        if (
            str(entry_nested.get("side") or "").lower() != ("sell" if is_short else "buy")
            or entry_class not in {"bracket", "simple"}
            or (entry_class == "bracket" and not isinstance(entry_nested.get("legs"), list))
        ):
            report["errors"].append(f"{symbol}: exact entry class or bracket legs cannot be proven")
            continue

        recovery = state.get("protection_recovery") or {}
        if not isinstance(recovery, dict):
            report["errors"].append(f"{symbol}: persisted OCO decision is malformed")
            continue
        if recovery and current_recovery(state) is None:
            # Left over from an earlier entry of this ticker; start fresh.
            recovery = {}
        client_id = recovery.get("client_order_id")
        if client_id is not None and (not isinstance(client_id, str) or not client_id):
            report["errors"].append(f"{symbol}: persisted OCO client ID is malformed")
            continue
        try:
            existing = broker.order_by_client_id(client_id) if client_id else None
        except Exception as exc:
            report["errors"].append(f"{symbol}: OCO client-ID lookup unavailable: {type(exc).__name__}: {exc}")
            continue
        if existing is not None and (not isinstance(existing, dict) or not _lookup_matches(existing, symbol, recovery)):
            report["errors"].append(f"{symbol}: persisted OCO client ID resolves to a conflicting broker order")
            continue
        oco_id = recovery.get("order_id") or (existing.get("id") if existing else None)
        oco_nested = None
        if oco_id:
            try:
                oco_nested = _exact_nested(broker, str(oco_id), symbol)
            except Exception as exc:
                report["errors"].append(f"{symbol}: exact OCO order unavailable: {type(exc).__name__}: {exc}")
                continue
            if oco_nested.get("client_order_id") != client_id:
                report["errors"].append(f"{symbol}: exact OCO order has a conflicting client ID")
                continue
        nested_orders = [entry_nested, *(symbol_open or []), *([oco_nested] if oco_nested else [])]
        # The exact OCO GET has the best chance to include fresh child legs;
        # remove a duplicate parent from the open view before evaluating it.
        if oco_nested:
            nested_orders = [row for row in nested_orders if row is oco_nested or str(row.get("id") or "") != str(oco_id)]
        if not extended_hours and recovery_oco_order(nested_orders, state, qty):
            incident_resolved = resolve_incident(portfolio, kind="oco_repair", ticker=symbol)
            incident_resolved = resolve_incident(
                portfolio, kind="exit_submission", ticker=symbol,
            ) or incident_resolved
            if recovery.get("status") != "verified":
                recovery["status"] = "verified"
                recovery["verified_at"] = now.isoformat()
                incident_resolved = True
            if incident_resolved:
                save()
            report["verified"].append(symbol)
            continue

        # The open endpoint is authoritative for all pending orders; nested
        # history also catches a pending_cancel leg if it is omitted there.
        if _open_symbol_orders(symbol, symbol_open, nested_orders):
            report["waiting"].append(f"{symbol}: broker order is still open or pending cancellation")
            continue

        if existing is not None:
            status = str(existing.get("status") or "").lower()
            if status not in {"canceled", "expired", "rejected"}:
                report["waiting"].append(f"{symbol}: OCO exists but active stop coverage is not verified ({status or 'unknown'})")
                continue
        elif oco_nested is not None:
            report["errors"].append(f"{symbol}: exact OCO exists but client-ID lookup disagrees")
            continue

        # Alpaca rejects OCO submissions outside regular hours. Keep the
        # read-only reconciliation above, but defer every new submission.
        if extended_hours:
            report["waiting"].append(f"{symbol}: OCO submission deferred until regular hours")
            continue

        # This specific broker 403 proves the prior client ID was rejected,
        # so retry the unchanged protection decision with a fresh ID in RTH.
        same = (
            isinstance(recovery, dict)
            and _same_decision(recovery, entry_id, quantity, stop, target)
            and not _definitive_extended_hours_rejection(recovery)
        )
        previous_age = _age_seconds(now, recovery.get("last_submission_at") or recovery.get("prepared_at")) if client_id else None
        if client_id and (previous_age is None or previous_age < RETRY_SECONDS):
            report["waiting"].append(f"{symbol}: waiting for prior OCO decision to settle")
            continue
        if client_id and existing is None and same:
            if int(recovery.get("submission_attempts") or 0) >= MAX_SUBMISSIONS_PER_DECISION:
                report["errors"].append(f"{symbol}: OCO outcome remains unverified after repeated same-ID attempts")
                continue
            # A 404 after the wait is not proof the first POST failed; retry
            # only the identical client ID so Alpaca can deduplicate it.
        else:
            attempt = int(recovery.get("decision_attempt") or 0) + 1 if isinstance(recovery, dict) else 1
            if attempt > MAX_DECISIONS:
                report["errors"].append(f"{symbol}: replacement OCO was rejected too many times")
                continue
            client_id = _decision_id(symbol, entry_id, quantity, stop, target, attempt)
            recovery = {
                "entry_order_id": entry_id, "qty": str(quantity),
                "stop_price": str(stop), "take_profit": str(target),
                "side": "buy" if is_short else "sell",
                "client_order_id": client_id, "order_id": None,
                "decision_attempt": attempt, "submission_attempts": 0,
                "status": "prepared", "prepared_at": now.isoformat(),
            }

        quote_reference = _fresh_inside_quote(
            broker, symbol, stop, target, now,
            feed=window.quote_feed, is_short=is_short,
        )
        if quote_reference is None:
            report["waiting"].append(f"{symbol}: no fresh quote strictly inside persisted stop and target")
            continue

        recovery["submission_attempts"] = int(recovery.get("submission_attempts") or 0) + 1
        recovery["side"] = "buy" if is_short else "sell"
        recovery["last_submission_at"] = now.isoformat()
        recovery["status"] = "submission_unknown"
        state["protection_recovery"] = recovery
        save()  # Durable intent and exact client ID precede any broker POST.
        trade = {
            "ticker": symbol, "action": "cover" if is_short else "sell", "qty": int(quantity),
            "order_type": "oco", "stop_price": float(stop),
            "take_profit": float(target), "entry_price": float(quote_reference),
            "strategy": owner, "client_order_id": client_id,
            "extended_hours": extended_hours,
            "reasoning": "restore broker stop and take-profit coverage for an owned Algo holding",
        }
        try:
            execution = execute([trade], mode="algo", dry_run=False)
            rows = execution.get("results") if isinstance(execution, dict) else None
            result = rows[0] if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict) else {}
            accepted = (
                result.get("status") == "submitted"
                and result.get("success") is not False
                and result.get("client_order_id") == client_id
                and bool(result.get("order_id"))
            )
            if accepted:
                recovery["order_id"] = str(result["order_id"])
            recovery["submission_status"] = result.get("status")
            recovery["broker_status"] = result.get("broker_status")
            recovery["last_error"] = str(result.get("error") or result.get("reason") or "")[:240]
            recovery["status"] = "submitted_unverified" if accepted else "submission_unknown"
            if not accepted:
                report["errors"].append(f"{symbol}: OCO executor did not confirm submission: {recovery['last_error'] or result.get('status') or 'missing result'}")
                record_incident(
                    portfolio, kind="oco_repair", ticker=symbol,
                    client_order_id=client_id, order_id=result.get("order_id"),
                    reason=recovery["last_error"] or "OCO executor did not confirm submission",
                )
        except Exception as exc:
            recovery["last_error"] = f"{type(exc).__name__}: {exc}"[:240]
            report["errors"].append(f"{symbol}: OCO executor outcome unknown: {recovery['last_error']}")
            record_incident(
                portfolio, kind="oco_repair", ticker=symbol,
                client_order_id=client_id, reason=recovery["last_error"],
            )
        save()
        report["attempted"].append(symbol)
        if recovery["status"] == "submitted_unverified":
            report["submitted"].append(symbol)

        # An accepted parent by itself is never stop coverage. Re-read the
        # broker's live position and nested legs before changing that status.
        try:
            fresh_positions = broker.positions()
            matching_positions = [
                row for row in fresh_positions
                if isinstance(row, dict) and str(row.get("symbol") or "").upper() == symbol
            ]
            if not matching_positions:
                recovery["status"] = "position_closed"
                save()
            elif len(matching_positions) == 1:
                fresh_qty = to_finite_decimal(matching_positions[0].get("qty"))
                fresh_order_id = recovery.get("order_id")
                if not fresh_order_id:
                    fresh_lookup = broker.order_by_client_id(client_id)
                    if fresh_lookup and _lookup_matches(fresh_lookup, symbol, recovery):
                        fresh_order_id = str(fresh_lookup.get("id") or "")
                fresh_oco = _exact_nested(broker, str(fresh_order_id), symbol) if fresh_order_id else None
                if (
                    not extended_hours
                    and fresh_qty is not None and fresh_oco
                    and recovery_oco_order([fresh_oco], state, fresh_qty)
                ):
                    recovery["order_id"] = str(fresh_order_id)
                    recovery["status"] = "verified"
                    recovery["verified_at"] = now.isoformat()
                    resolve_incident(portfolio, kind="oco_repair", ticker=symbol)
                    resolve_incident(portfolio, kind="exit_submission", ticker=symbol)
                    report["errors"] = [
                        error for error in report["errors"]
                        if not error.startswith(f"{symbol}:")
                    ]
                    save()
                    report["verified"].append(symbol)
                else:
                    reason = (
                        "OCO submitted; broker stop child cannot be verified until regular hours"
                        if extended_hours else
                        "OCO submitted; stop child not yet broker-verified"
                    )
                    report["waiting"].append(f"{symbol}: {reason}")
            else:
                report["errors"].append(f"{symbol}: duplicate broker position rows after OCO submission")
        except Exception as exc:
            report["errors"].append(f"{symbol}: broker OCO verification unavailable: {type(exc).__name__}: {exc}")
        break  # At most one potentially live OCO submission per pass.

    report["status"] = (
        "degraded" if report["errors"] else
        "verified" if report["verified"] and not report["waiting"] else
        "submitted" if report["submitted"] else
        "waiting" if report["waiting"] else "not_needed"
    )
    return _finish_report(portfolio, report, previous_report, now)


__all__ = ["recover_broker_protection"]
