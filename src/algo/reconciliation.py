"""Conservative startup attribution of Algo paper positions.

The account has one net position per symbol. A strategy may own it only when
the broker's current fill episode is fully reconstructed and every entry buy
in that episode has an exact matching Algo journal order ID and one strategy.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from src.algo.incidents import open_incident_symbols, resolve_incident
from src.algo.protection import verified_recovery_oco
from src.strategies.registry import ALGO_DEFAULT_PARAMETERS
from src.strategies.state_machine import ENTRY_PENDING, EXIT_PENDING, RuleStrategyStateMachine


OPEN_ORDER_STATUSES = {"new", "held", "accepted", "pending_new", "partially_filled", "accepted_for_bidding"}
PROVEN_SUBMISSION_STATUSES = {
    "submitted", "executed", "accepted", "new", "pending_new", "partially_filled", "filled",
}
FAILED_BROKER_STATUSES = {"rejected", "canceled", "expired"}


def _decimal(value: object) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid broker quantity or price: {value!r}") from exc
    if not amount.is_finite():
        raise ValueError("Broker quantity or price is not finite")
    return amount


def _journal_entries(path: Path, account_id: str | None = None) -> tuple[dict[str, dict], list[str]]:
    if not path.is_file():
        return {}, ["Algo trade journal is missing"]
    rows: dict[str, dict] = {}
    errors = []
    for line_number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            errors.append(f"Malformed trade journal row {line_number}")
            continue
        if not isinstance(row, dict):
            errors.append(f"Malformed trade journal row {line_number}")
            continue
        if row.get("trading_mode") != "algo" or str(row.get("action", "")).lower() not in {"buy", "short"}:
            continue
        if account_id is not None and row.get("account_id") != account_id:
            continue
        order_id = row.get("order_id")
        if not order_id:
            continue
        # A broker ID in a failed result can belong to a different order
        # after a client-ID identity mismatch. It is never ownership proof.
        outcome = str(row.get("submission_status") or row.get("status") or "").lower()
        raw_status = str(row.get("status") or "").lower()
        broker_status = str(row.get("broker_status") or "").lower()
        if (
            outcome not in PROVEN_SUBMISSION_STATUSES
            or (raw_status and raw_status not in PROVEN_SUBMISSION_STATUSES)
            or broker_status in FAILED_BROKER_STATUSES
            or row.get("error")
            or not row.get("client_order_id")
        ):
            continue
        order_id = str(order_id)
        prior = rows.get(order_id)
        if prior is not None and (
            prior.get("strategy") != row.get("strategy")
            or str(prior.get("ticker", "")).upper() != str(row.get("ticker", "")).upper()
            or prior.get("client_order_id") != row.get("client_order_id")
        ):
            errors.append(f"Conflicting journal strategies for broker order {order_id}")
            continue
        rows[order_id] = row
    return rows, errors


def _filled_orders(orders: list[dict]) -> tuple[dict[str, list[dict]], list[str]]:
    by_symbol: dict[str, list[dict]] = {}
    errors = []
    seen_ids = set()
    for order in orders:
        if not isinstance(order, dict):
            errors.append("Broker order history contains a malformed row")
            continue
        order_id = str(order.get("id") or "")
        ticker = str(order.get("symbol") or "").upper()
        if not order_id or not ticker or order_id in seen_ids:
            errors.append(f"Broker order history contains a duplicate or incomplete order for {ticker or '?'}")
            continue
        seen_ids.add(order_id)
        try:
            filled = _decimal(order.get("filled_qty") or 0)
        except ValueError:
            errors.append(f"Broker order {order_id} has invalid filled quantity")
            continue
        if filled == 0:
            continue
        if filled < 0 or order.get("side") not in {"buy", "sell"} or not order.get("filled_at"):
            errors.append(f"Broker order {order_id} has an ambiguous fill")
            continue
        by_symbol.setdefault(ticker, []).append(order)
    for ticker, events in by_symbol.items():
        events.sort(key=lambda order: str(order["filled_at"]))
        timestamps = [str(order["filled_at"]) for order in events]
        if len(set(timestamps)) != len(timestamps):
            errors.append(f"Broker fill order is ambiguous for {ticker}")
    return by_symbol, errors


def _current_episode(position: dict, filled_orders: list[dict]) -> tuple[list[dict], str | None]:
    """Walk fills backward from current quantity to the most recent flat point."""
    try:
        current_qty = _decimal(position.get("qty") or 0)
    except ValueError as exc:
        return [], str(exc)
    if current_qty == 0:
        return [], "Broker position quantity is zero"
    remaining = current_qty
    episode_reverse = []
    for order in reversed(filled_orders):
        filled = _decimal(order["filled_qty"])
        delta = filled if order["side"] == "buy" else -filled
        remaining -= delta
        episode_reverse.append(order)
        if remaining != 0 and (remaining > 0) != (current_qty > 0):
            return [], "Broker fills cannot reconstruct the current position"
        if remaining == 0:
            episode = list(reversed(episode_reverse))
            break
    else:
        return [], "Broker history does not reach the current position's last flat point"

    quantity = Decimal(0)
    average = Decimal(0)
    for order in episode:
        filled = _decimal(order["filled_qty"])
        delta = filled if order["side"] == "buy" else -filled
        if quantity == 0:
            try:
                price = _decimal(order.get("filled_avg_price"))
            except ValueError:
                return [], "An entry fill has no valid average price"
            if price <= 0:
                return [], "An entry fill has no valid average price"
            quantity = delta
            average = price
        elif (quantity > 0) == (delta > 0):
            try:
                price = _decimal(order.get("filled_avg_price"))
            except ValueError:
                return [], "An entry fill has no valid average price"
            if price <= 0:
                return [], "An entry fill has no valid average price"
            average = (average * abs(quantity) + price * abs(delta)) / (abs(quantity) + abs(delta))
            quantity += delta
        else:
            next_quantity = quantity + delta
            if next_quantity != 0 and (next_quantity > 0) != (quantity > 0):
                return [], "A fill crosses through flat and changes position direction"
            quantity = next_quantity
            if quantity == 0:
                average = Decimal(0)
    if quantity != current_qty:
        return [], "Broker fill quantity differs from the current position"
    try:
        broker_average = _decimal(position.get("avg_entry_price"))
    except ValueError:
        return [], "Current position has no valid average entry price"
    if abs(average - broker_average) > Decimal("0.02"):
        return [], "Broker fill basis differs from the current position"
    return episode, None


def _bracket_levels(entry_order_id: str, nested_orders: list[dict], quantity: Decimal) -> tuple[float | None, float | None]:
    parent = next((order for order in nested_orders if str(order.get("id")) == entry_order_id), None)
    if not parent or parent.get("order_class") != "bracket":
        return None, None
    legs = parent.get("legs") or []
    stops = []
    targets = []
    exit_side = "buy" if quantity < 0 else "sell"
    for leg in legs:
        if leg.get("side") != exit_side or leg.get("status") not in OPEN_ORDER_STATUSES:
            continue
        try:
            if _decimal(leg.get("qty") or 0) != abs(quantity):
                continue
            if leg.get("type") in {"stop", "stop_limit"}:
                price = _decimal(leg.get("stop_price"))
                if price > 0:
                    stops.append(float(price))
            elif leg.get("type") == "limit":
                price = _decimal(leg.get("limit_price"))
                if price > 0:
                    targets.append(float(price))
        except ValueError:
            continue
    return (stops[0] if len(stops) == 1 else None, targets[0] if len(targets) == 1 else None)


def _resolve_execution_incidents(
    portfolio: dict, *, positions: dict[str, dict], open_orders: list[dict],
    all_orders: list[dict], nested_orders: list[dict],
    exact_incident_orders: dict[str, dict],
) -> None:
    incident = portfolio.get("execution_incident") or {}
    if incident.get("status") != "open" or not isinstance(incident.get("events"), dict):
        return
    candidates = [*exact_incident_orders.values(), *open_orders, *all_orders]
    for event in list(incident["events"].values()):
        if not isinstance(event, dict):
            continue
        kind = event.get("kind")
        symbol = str(event.get("ticker") or "").upper()
        if not symbol or kind not in {"exit_submission", "oco_repair"}:
            continue
        if symbol not in positions:
            if not any(
                isinstance(order, dict) and str(order.get("symbol") or "").upper() == symbol
                for order in open_orders
            ):
                resolve_incident(portfolio, kind=kind, ticker=symbol)
            continue
        if kind == "oco_repair":
            owner = (portfolio.get("position_owners") or {}).get(symbol)
            state = ((portfolio.get("strategy_states") or {}).get(owner) or {}).get(symbol, {}) if owner else {}
            try:
                quantity = _decimal(positions[symbol].get("qty"))
            except ValueError:
                continue
            bracket_stop, _ = _bracket_levels(str(state.get("entry_order_id") or ""), nested_orders, quantity)
            if bracket_stop is not None or verified_recovery_oco(nested_orders, state, quantity):
                resolve_incident(portfolio, kind=kind, ticker=symbol)
            continue
        client_id = event.get("client_order_id")
        order_id = event.get("order_id")
        if not client_id:
            continue
        matches = [
            order for order in candidates
            if isinstance(order, dict)
            and order.get("client_order_id") == client_id
            and (not order_id or str(order.get("id") or "") == str(order_id))
            and str(order.get("symbol") or "").upper() == symbol
            and str(order.get("side") or "").lower() == (
                "buy" if _decimal((positions.get(symbol) or {}).get("qty") or 0) < 0 else "sell"
            )
            and str(order.get("status") or "").lower() in OPEN_ORDER_STATUSES
        ]
        if matches:
            resolve_incident(portfolio, kind=kind, ticker=symbol)


def reconcile_startup(
    portfolio: dict,
    *,
    positions: list[dict],
    all_orders: list[dict],
    nested_orders: list[dict],
    open_orders: list[dict],
    journal_path: Path,
    exact_entry_orders: dict[str, dict] | None = None,
    exact_incident_orders: dict[str, dict] | None = None,
    require_broker_stops: bool = False,
) -> dict:
    """Verify current holdings before any new Algo entry is allowed."""
    journal, journal_errors = _journal_entries(journal_path, portfolio.get("account_id"))
    fills_by_symbol, fill_errors = _filled_orders(all_orders)
    errors = journal_errors + fill_errors
    machine = RuleStrategyStateMachine(
        portfolio.setdefault("strategy_states", {}), portfolio.setdefault("position_owners", {}),
    )
    live_positions: dict[str, dict] = {}
    for position in positions:
        ticker = str(position.get("symbol") or "").upper()
        if not ticker or ticker in live_positions:
            errors.append("Broker positions include a duplicate or unnamed symbol")
            continue
        live_positions[ticker] = position

    recovered = []
    verified = []
    verified_entries: dict[str, str] = {}
    verified_quantities: dict[str, str] = {}
    unprotected = []
    uncertain: dict[str, str] = {}
    exact_entry_orders = exact_entry_orders or {}
    for ticker, position in sorted(live_positions.items()):
        try:
            qty = _decimal(position.get("qty") or 0)
        except ValueError:
            qty = Decimal(0)
        if qty == 0:
            uncertain[ticker] = "Broker position quantity is zero"
            machine.quarantine_position(ticker, uncertain[ticker])
            continue
        prior_owner = machine.position_owners.get(ticker)
        prior_state = (machine.states.get(prior_owner) or {}).get(ticker, {}) if prior_owner else {}
        if errors:
            uncertain[ticker] = "Broker or journal history is incomplete"
            if prior_state.get("entry_order_id"):
                # Preserve an already managed exit while blocking all entries.
                # A malformed unrelated row does not erase persisted proof.
                if prior_state.get("stop_price") is None:
                    unprotected.append(ticker)
            else:
                machine.quarantine_position(ticker, uncertain[ticker])
            continue
        episode, reason = _current_episode(position, fills_by_symbol.get(ticker, []))
        # Alpaca returns at most 500 orders per request. A long-held position
        # can outlive that page. Keep a verified persisted owner only when an
        # exact broker lookup proves its entry and no newer fill for this
        # symbol appears in the page; all other incomplete histories pause.
        if (
            reason
            and len(all_orders) >= 500
            and prior_owner
            and prior_state.get("entry_order_id")
            and prior_state.get("last_known_qty") is not None
            and not fills_by_symbol.get(ticker)
        ):
            exact = exact_entry_orders.get(prior_state["entry_order_id"])
            try:
                quantity_unchanged = _decimal(prior_state["last_known_qty"]) == qty
                exact_valid = (
                    isinstance(exact, dict)
                    and str(exact.get("id")) == prior_state["entry_order_id"]
                    and str(exact.get("symbol", "")).upper() == ticker
                    and exact.get("side") == ("buy" if qty > 0 else "sell")
                    and exact.get("status") == "filled"
                    and _decimal(exact.get("filled_qty") or 0) == abs(qty)
                    and abs(_decimal(exact.get("filled_avg_price")) - _decimal(position.get("avg_entry_price"))) <= Decimal("0.02")
                )
            except ValueError:
                exact_valid = False
                quantity_unchanged = False
            if quantity_unchanged and exact_valid:
                episode = [exact]
                reason = None
        if reason:
            uncertain[ticker] = reason
            if prior_state.get("entry_order_id") and reason.startswith("Broker history does not reach"):
                # The current broker page cannot re-prove an older entry;
                # existing stop management continues, but entries stay paused.
                if prior_state.get("stop_price") is None:
                    unprotected.append(ticker)
            else:
                machine.quarantine_position(ticker, reason)
            continue
        entry_side = "buy" if qty > 0 else "sell"
        entry_orders = [order for order in episode if order.get("side") == entry_side]
        if len(entry_orders) != 1:
            uncertain[ticker] = "Current episode has multiple opening orders; manual attribution is required"
            machine.quarantine_position(ticker, uncertain[ticker])
            continue
        entry = entry_orders[0]
        order_id = str(entry["id"])
        entry_class = str(entry.get("order_class") or "simple").lower()
        if entry_class not in {"simple", "bracket"}:
            uncertain[ticker] = f"Unsupported Algo entry order class: {entry_class}"
            machine.quarantine_position(ticker, uncertain[ticker])
            continue
        journal_row = journal.get(order_id)
        journal_proved = bool(
            journal_row
            and str(journal_row.get("ticker", "")).upper() == ticker
            and journal_row.get("strategy") in ALGO_DEFAULT_PARAMETERS
            and journal_row["client_order_id"] == entry.get("client_order_id")
        )
        pending_client = prior_state.get("entry_client_order_id") or prior_state.get("client_order_id")
        persisted_pending_proved = bool(
            not journal_proved
            and prior_owner
            and prior_state.get("phase") == ENTRY_PENDING
            and pending_client
            and pending_client == entry.get("client_order_id")
        )
        if not journal_proved and not persisted_pending_proved:
            uncertain[ticker] = "Current entry has no exact matching Algo journal order and strategy"
            machine.quarantine_position(ticker, uncertain[ticker])
            continue
        strategy = journal_row["strategy"] if journal_proved else prior_owner
        if prior_owner is not None and prior_owner != strategy:
            uncertain[ticker] = f"Persisted owner {prior_owner} conflicts with broker-verified {strategy}"
            machine.quarantine_position(ticker, uncertain[ticker])
            continue
        prior_state = (machine.states.get(strategy) or {}).get(ticker, {})
        prior_entry = prior_state.get("entry_order_id")
        if prior_entry and prior_entry != order_id and prior_state.get("phase") in {ENTRY_PENDING, EXIT_PENDING, "open"}:
            uncertain[ticker] = "Persisted entry ID differs from the current broker episode"
            machine.quarantine_position(ticker, uncertain[ticker])
            continue

        stop, target = _bracket_levels(order_id, nested_orders, qty)
        if prior_state.get("phase") == EXIT_PENDING and prior_owner == strategy:
            # Keep an in-flight exit pending; only attach its missing entry proof.
            prior_state["entry_order_id"] = order_id
            prior_state["entry_client_order_id"] = entry.get("client_order_id")
            prior_state["entry_source"] = "journal_broker_match"
            if prior_state.get("stop_price") is None:
                prior_state["stop_price"] = stop
            if prior_state.get("take_profit") is None:
                prior_state["take_profit"] = target
        else:
            try:
                machine.adopt_verified_position(
                    strategy, ticker,
                    entry_order_id=order_id,
                    entry_client_order_id=entry.get("client_order_id"),
                    source=("persisted_pending_client_id" if persisted_pending_proved else
                            "journal_broker_match" if prior_entry is None else "coordinator"),
                    stop_price=stop,
                    take_profit=target,
                    entry_action="buy" if qty > 0 else "short",
                    entry_at=entry.get("filled_at") or entry.get("submitted_at"),
                )
            except ValueError as exc:
                uncertain[ticker] = str(exc)
                machine.quarantine_position(ticker, uncertain[ticker])
                continue
        if prior_owner is None:
            recovered.append(ticker)
        verified.append(ticker)
        verified_entries[ticker] = order_id
        verified_quantities[ticker] = str(qty)
        state = machine.states[strategy][ticker]
        # A remembered software stop is not evidence that Alpaca still has
        # the bracket stop. A canceled child can otherwise leave a position
        # looking protected after a restart.
        replacement_oco = verified_recovery_oco(nested_orders, state, qty)
        broker_stop_missing = (
            (entry_class == "bracket" and stop is None and not replacement_oco)
            or (require_broker_stops and entry_class == "simple" and not replacement_oco)
        )
        if state.get("stop_price") is None or broker_stop_missing:
            unprotected.append(ticker)

    machine.reconcile(live_positions, open_orders)
    _resolve_execution_incidents(
        portfolio, positions=live_positions, open_orders=open_orders,
        all_orders=all_orders, nested_orders=nested_orders,
        exact_incident_orders=exact_incident_orders or {},
    )
    unowned = sorted(set(live_positions) - set(machine.position_owners))
    pause_reasons = []
    if errors:
        pause_reasons.append("broker or journal history is incomplete")
    if uncertain:
        pause_reasons.append("Algo position attribution needs review: " + ", ".join(sorted(uncertain)))
    if unowned:
        pause_reasons.append("unattributed Algo holdings: " + ", ".join(unowned))
    if unprotected:
        pause_reasons.append("Algo holdings lack a verified stop: " + ", ".join(sorted(unprotected)))
    incident_symbols = open_incident_symbols(portfolio)
    if incident_symbols:
        pause_reasons.append("Algo execution incident needs broker resolution: " + ", ".join(incident_symbols))
    pending = [
        ticker for ticker, strategy in machine.position_owners.items()
        if (machine.states.get(strategy) or {}).get(ticker, {}).get("phase") in {ENTRY_PENDING, EXIT_PENDING}
    ]
    if pending:
        pause_reasons.append("Algo orders are pending startup reconciliation: " + ", ".join(sorted(pending)))
    report = {
        "status": "verified" if not pause_reasons else "degraded",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "verified_positions": sorted(verified),
        "verified_entries": verified_entries,
        "verified_quantities": verified_quantities,
        "recovered_owners": sorted(recovered),
        "unowned_positions": unowned,
        "unprotected_positions": sorted(unprotected),
        "uncertain": uncertain,
        "errors": errors,
        "entries_paused": bool(pause_reasons),
        "entry_pause_reasons": pause_reasons,
    }
    portfolio["startup_reconciliation"] = report
    portfolio["unowned_positions"] = unowned
    portfolio["entries_paused"] = bool(pause_reasons)
    portfolio["entry_pause_reasons"] = pause_reasons
    return report


__all__ = ["reconcile_startup"]
