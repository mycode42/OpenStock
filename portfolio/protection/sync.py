"""Keep broker-hosted Algo stop legs aligned with persisted trailing stops."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from portfolio.protection.evidence import (
    ACTIVE_ORDER_STATUSES,
    broker_prices_match,
    recovery_oco_order,
    remaining_order_quantity,
    to_finite_decimal,
)
from portfolio.reconciliation import OPEN_ORDER_STATUSES


PENDING_REPLACE_STATUSES = frozenset({
    "accepted", "pending_new", "pending_cancel", "pending_replace",
})
RETRY_SECONDS = 60


def _valid_oco_parent(parent: dict, state: dict, recovery: dict, qty: Decimal, stop: Decimal) -> bool:
    """Confirm the existing OCO is the recorded full-size order before PATCH."""
    if to_finite_decimal(state.get("take_profit")) != to_finite_decimal(recovery.get("take_profit")):
        return False
    check_state = dict(state)
    check_state["stop_price"] = stop
    check_state["take_profit"] = recovery.get("take_profit")
    check_recovery = dict(recovery)
    check_recovery["stop_price"] = stop
    check_state["protection_recovery"] = check_recovery
    return recovery_oco_order([parent], check_state, qty) is not None


def _bracket_stop(parent: dict, state: dict, qty: Decimal) -> tuple[dict | None, str | None]:
    """Return the exact active full-size stop child of a filled entry bracket."""
    ticker = str(state.get("ticker") or "").upper()
    signed_qty = to_finite_decimal(qty)
    if not ticker or signed_qty is None or signed_qty == 0:
        return None, "position identity or quantity is invalid"
    exit_side = "buy" if signed_qty < 0 else "sell"
    entry_side = "sell" if signed_qty < 0 else "buy"
    if (
        str(parent.get("id") or "") != str(state.get("entry_order_id") or "")
        or str(parent.get("symbol") or "").upper() != ticker
        or str(parent.get("side") or "").lower() != entry_side
        or str(parent.get("order_class") or "").lower() != "bracket"
    ):
        return None, "exact bracket entry identity could not be verified"
    legs = parent.get("legs")
    if not isinstance(legs, list):
        return None, "bracket children are unavailable"
    stops = [
        leg for leg in legs
        if isinstance(leg, dict)
        and str(leg.get("side") or "").lower() == exit_side
        and str(leg.get("type") or "").lower() in {"stop", "stop_limit"}
        and str(leg.get("status") or "").lower() in OPEN_ORDER_STATUSES
        and remaining_order_quantity(leg) == abs(signed_qty)
    ]
    targets = [
        leg for leg in legs
        if isinstance(leg, dict)
        and str(leg.get("side") or "").lower() == exit_side
        and str(leg.get("type") or "").lower() == "limit"
        and str(leg.get("status") or "").lower() in OPEN_ORDER_STATUSES
        and remaining_order_quantity(leg) == abs(signed_qty)
        and broker_prices_match(leg.get("limit_price"), state.get("take_profit"))
    ]
    if len(stops) != 1 or len(targets) != 1 or not stops[0].get("id"):
        return None, "bracket stop and target do not prove exact full-size protection"
    return stops[0], None


def _quote_is_inside(
    broker, symbol: str, qty: Decimal, stop: Decimal, target: Decimal,
    now: datetime, *, feed: str,
) -> bool:
    quotes = broker.latest_quotes([symbol], feed=feed)
    quote = quotes.get(symbol) if isinstance(quotes, dict) else None
    if not isinstance(quote, dict):
        return False
    bid, ask = to_finite_decimal(quote.get("bid")), to_finite_decimal(quote.get("ask"))
    timestamp = quote.get("timestamp")
    if isinstance(timestamp, str):
        try:
            timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            return False
    if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
        return False
    age = (now - timestamp).total_seconds()
    if not -5 <= age <= 30 or bid is None or ask is None or ask < bid:
        return False
    if qty > 0:
        return bid > stop + Decimal("0.01") and ask < target - Decimal("0.01")
    return ask < stop - Decimal("0.01") and bid > target + Decimal("0.01")


def _finish_sync(state: dict, recovery: dict | None, parent: dict, child: dict, stop: Decimal, now: datetime) -> None:
    child_id = str(child.get("id") or "")
    parent_id = str(parent.get("id") or "")
    state["broker_stop_price"] = float(stop)
    state["broker_stop_order_id"] = child_id
    state["protection_sync"] = {
        "status": "verified",
        "parent_order_id": parent_id,
        "stop_order_id": child_id,
        "stop_price": str(stop),
        "verified_at": now.isoformat(),
    }
    if recovery is not None:
        recovery["stop_price"] = str(stop)
        recovery["stop_order_id"] = child_id
        recovery["status"] = "verified"
        recovery["verified_at"] = now.isoformat()
        recovery.pop("pending_stop_sync", None)


def _clear_repaired_startup_pauses(portfolio: dict, verified: set[str]) -> None:
    startup = portfolio.get("startup_reconciliation")
    if not isinstance(startup, dict) or not verified:
        return
    gaps = set(startup.get("unprotected_positions") or []) - verified
    startup["unprotected_positions"] = sorted(gaps)
    reasons = [
        str(reason) for reason in startup.get("entry_pause_reasons") or []
        if not str(reason).startswith("Algo holdings lack a verified stop:")
    ]
    if gaps:
        reasons.append("Algo holdings lack a verified stop: " + ", ".join(sorted(gaps)))
    startup["entry_pause_reasons"] = reasons
    still_degraded = bool(
        gaps or startup.get("errors") or startup.get("uncertain")
        or startup.get("unowned_positions") or reasons
        or startup.get("status") == "error"
    )
    startup["entries_paused"] = still_degraded
    if not still_degraded:
        startup["status"] = "verified"


def sync_broker_stops(
    portfolio: dict,
    *,
    window,
    broker,
    save,
    now: datetime | None = None,
) -> dict:
    """Safely raise/lower existing stop legs to the owned position's current trail.

    Only exact, full-size open bracket or recorded recovery OCO orders are
    eligible. The stop may only move in the favorable direction, and a fresh
    quote must remain strictly between the stop and take-profit levels.
    """
    now = now or datetime.now(timezone.utc)
    report = {"status": "not_needed", "checked_at": now.isoformat(), "updated": [], "verified": [], "waiting": [], "errors": []}
    session_name = getattr(window, "name", None)
    extended_hours = session_name != "regular"
    if (
        session_name not in {"regular", "premarket", "after_hours", "overnight"}
        or bool(getattr(window, "extended_hours", extended_hours)) != extended_hours
    ):
        return report
    if session_name == "regular":
        regular_open = getattr(window, "regular_open", None)
        regular_close = getattr(window, "regular_close", None)
        if not isinstance(regular_open, datetime) or not isinstance(regular_close, datetime):
            return report
        local_now = now.astimezone(regular_open.tzinfo or timezone.utc)
        if not regular_open <= local_now < regular_close:
            return report

    owners = portfolio.get("position_owners") or {}
    states = portfolio.get("strategy_states") or {}
    if not owners:
        return report
    try:
        positions_raw = broker.positions()
        if not isinstance(positions_raw, list):
            raise ValueError("broker positions are malformed")
        positions: dict[str, dict] = {}
        for position in positions_raw:
            if not isinstance(position, dict):
                raise ValueError("broker positions contain a malformed row")
            symbol = str(position.get("symbol") or "").upper()
            if not symbol or symbol in positions:
                raise ValueError("broker positions contain a duplicate or unnamed symbol")
            positions[symbol] = position
    except Exception as exc:
        report["status"] = "degraded"
        report["errors"].append(f"cannot refresh positions before stop sync: {type(exc).__name__}: {exc}")
        return report

    clock_checked = False
    for symbol in sorted(owners):
        owner = owners.get(symbol)
        state = (states.get(owner) or {}).get(symbol) if owner else None
        if not isinstance(state, dict) or state.get("phase") != "open" or symbol not in positions:
            continue
        signed_qty = to_finite_decimal(positions[symbol].get("qty"))
        desired = to_finite_decimal(state.get("stop_price"))
        target = to_finite_decimal(state.get("take_profit"))
        entry_id = str(state.get("entry_order_id") or "")
        if (
            signed_qty is None or signed_qty == 0 or desired is None or target is None
            or not entry_id or to_finite_decimal(state.get("last_known_qty")) != signed_qty
        ):
            report["waiting"].append(f"{symbol}: owned position changed or its persisted exit levels are incomplete")
            continue
        is_short = signed_qty < 0
        if (desired <= target if is_short else desired >= target):
            report["errors"].append(f"{symbol}: saved stop and take-profit levels cross")
            continue

        recovery = state.get("protection_recovery")
        recovery = recovery if isinstance(recovery, dict) and recovery.get("client_order_id") else None
        try:
            if recovery is not None:
                parent_id = str(recovery.get("order_id") or "")
                if not parent_id:
                    found = broker.order_by_client_id(str(recovery["client_order_id"]))
                    parent_id = str(found.get("id") or "") if isinstance(found, dict) else ""
                if not parent_id:
                    report["waiting"].append(f"{symbol}: recorded OCO parent has not appeared at the broker")
                    continue
                parent = broker.order(parent_id, nested=True)
                if not isinstance(parent, dict) or str(parent.get("id") or "") != parent_id:
                    raise ValueError("exact OCO parent lookup failed")
                old_stop = to_finite_decimal(recovery.get("stop_price"))
                if old_stop is None or recovery.get("take_profit") is None:
                    raise ValueError("recorded OCO stop metadata is incomplete")
                if str(parent.get("client_order_id") or "") != str(recovery.get("client_order_id")):
                    raise ValueError("exact OCO parent has a different client ID")
                child = (parent.get("legs") or [None])[0]
                if not isinstance(child, dict) or not _valid_oco_parent(parent, state, recovery, signed_qty, old_stop):
                    # A replacement can be accepted before the local state is
                    # committed. Recognize that exact desired order and finish
                    # the durable state transition without submitting again.
                    if not isinstance(child, dict) or not _valid_oco_parent(parent, state, recovery, signed_qty, desired):
                        raise ValueError("recorded OCO does not prove exact full-size protection")
                    _finish_sync(state, recovery, parent, child, desired, now)
                    report["verified"].append(symbol)
                    save()
                    continue
                current_stop = old_stop
                child_type = "oco"
            else:
                parent_id = entry_id
                parent = broker.order(parent_id, nested=True)
                if not isinstance(parent, dict) or str(parent.get("id") or "") != parent_id:
                    raise ValueError("exact bracket entry lookup failed")
                child, child_error = _bracket_stop(parent, state, signed_qty)
                if child_error:
                    report["waiting"].append(f"{symbol}: {child_error}")
                    continue
                current_stop = to_finite_decimal(child.get("stop_price"))
                if current_stop is None:
                    raise ValueError("bracket stop price is invalid")
                child_type = "bracket"
        except Exception as exc:
            report["errors"].append(f"{symbol}: broker stop verification failed: {type(exc).__name__}: {exc}")
            continue

        if current_stop == desired:
            _finish_sync(state, recovery, parent, child, desired, now)
            report["verified"].append(symbol)
            continue

        pending = state.get("protection_sync") or {}
        pending_requested = to_finite_decimal(pending.get("requested_stop_price")) if isinstance(pending, dict) else None
        pending_at = pending.get("requested_at") if isinstance(pending, dict) else None
        if pending_requested is not None and pending_requested != desired:
            age = _age_seconds(now, pending_at)
            if age is None or age < RETRY_SECONDS:
                report["waiting"].append(f"{symbol}: previous broker stop replacement is still settling")
                continue
        status = str(child.get("status") or "").lower()
        if status in PENDING_REPLACE_STATUSES:
            report["waiting"].append(f"{symbol}: broker stop is {status}; waiting for replacement to settle")
            continue
        if status not in ACTIVE_ORDER_STATUSES:
            report["errors"].append(f"{symbol}: broker stop is not replaceable in status {status or 'unknown'}")
            continue
        # Never loosen native protection based on a stale or reset software value.
        moves_favorably = desired < current_stop if is_short else desired > current_stop
        if not moves_favorably:
            report["errors"].append(f"{symbol}: saved trailing stop would loosen the broker stop")
            continue
        if not child.get("id"):
            report["errors"].append(f"{symbol}: exact broker stop child has no order ID")
            continue

        try:
            open_orders = broker.orders("open", nested=True, symbols=symbol)
            if not isinstance(open_orders, list) or len(open_orders) >= 500:
                raise ValueError("symbol open-order view is unavailable or capped")
            allowed_ids = {parent_id, str(child.get("id"))}
            allowed_ids.update(
                str(leg.get("id")) for leg in parent.get("legs") or []
                if isinstance(leg, dict) and leg.get("id")
            )
            unexpected = [
                order for order in open_orders
                if isinstance(order, dict)
                and str(order.get("symbol") or "").upper() == symbol
                and str(order.get("status") or "").lower() in (OPEN_ORDER_STATUSES | {"pending_cancel", "pending_replace"})
                and str(order.get("id") or "") not in allowed_ids
            ]
            if unexpected:
                report["waiting"].append(f"{symbol}: another open broker order prevents stop replacement")
                continue
            if not _quote_is_inside(
                broker, symbol, signed_qty, desired, target, now,
                feed=window.quote_feed,
            ):
                report["waiting"].append(f"{symbol}: no fresh quote strictly inside the saved stop and target")
                continue
            if not clock_checked and session_name == "regular":
                clock = broker.clock()
                clock_checked = True
                if not isinstance(clock, dict) or clock.get("is_open") is not True:
                    report["waiting"].append("broker reports the regular market is closed")
                    break

            state["protection_sync"] = {
                "status": "pending",
                "order_class": child_type,
                "parent_order_id": parent_id,
                "stop_order_id": str(child["id"]),
                "prior_stop_price": str(current_stop),
                "requested_stop_price": str(desired),
                "requested_at": now.isoformat(),
            }
            save()  # Persist intent before the broker PATCH.
            broker.replace_stop_order(str(child["id"]), desired)
            updated_parent = broker.order(parent_id, nested=True)
            if recovery is not None:
                updated_child = (updated_parent.get("legs") or [None])[0] if isinstance(updated_parent, dict) else None
                matched = bool(
                    isinstance(updated_parent, dict)
                    and isinstance(updated_child, dict)
                    and str(updated_parent.get("id") or "") == parent_id
                    and _valid_oco_parent(updated_parent, state, recovery, signed_qty, desired)
                )
            else:
                updated_child, child_error = _bracket_stop(updated_parent, state, signed_qty) if isinstance(updated_parent, dict) else (None, "updated bracket is unavailable")
                matched = bool(not child_error and isinstance(updated_child, dict) and to_finite_decimal(updated_child.get("stop_price")) == desired)
            if not matched or not isinstance(updated_child, dict):
                report["waiting"].append(f"{symbol}: replacement accepted but the exact broker stop is not yet verified")
                continue
            _finish_sync(state, recovery, updated_parent, updated_child, desired, now)
            report["updated"].append(symbol)
            report["verified"].append(symbol)
            save()
        except Exception as exc:
            # A PATCH timeout is ambiguous. Leave its durable intent in place;
            # the next pass reads the exact order before considering a retry.
            state["protection_sync"] = {
                **(state.get("protection_sync") or {}),
                "status": "pending",
                "last_error": f"{type(exc).__name__}: {exc}"[:240],
            }
            save()
            report["waiting"].append(f"{symbol}: stop replacement outcome needs broker verification ({type(exc).__name__})")

    if report["errors"]:
        report["status"] = "degraded"
    elif report["waiting"]:
        report["status"] = "waiting"
    elif report["updated"]:
        report["status"] = "verified"
    elif report["verified"]:
        report["status"] = "verified"
    _clear_repaired_startup_pauses(portfolio, set(report["verified"]))
    recovery_report = portfolio.get("protection_recovery_status")
    if report["verified"] and not report["errors"] and not report["waiting"] and isinstance(recovery_report, dict):
        verified = set(report["verified"])
        recovery_report["verified"] = sorted(set(recovery_report.get("verified") or []) | verified)
        recovery_report["synced"] = sorted(set(recovery_report.get("synced") or []) | set(report["updated"]))
        recovery_report["waiting"] = [
            item for item in recovery_report.get("waiting") or []
            if str(item).split(":", 1)[0] not in verified
        ]
        if not recovery_report.get("waiting") and not recovery_report.get("errors"):
            recovery_report["status"] = "verified"
            recovery_report["checked_at"] = now.isoformat()
    portfolio["protection_sync_status"] = report
    return report


def _age_seconds(now: datetime, value: object) -> float | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return (now - parsed).total_seconds()


__all__ = ["sync_broker_stops"]
