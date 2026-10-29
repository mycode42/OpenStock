"""One-shot migration from broker bracket legs to software protection rows.

Dry-run by default. For each held, owned position with a verifiable bracket or
OCO exit, the migration:

1. builds the protection row from the persisted state, using the broker's stop
   and target (both must already equal the persisted levels);
2. writes the row and reads it back to verify stop, target, and quantity;
3. only then cancels the broker's exit legs.

Any failed check leaves that position's broker legs untouched and is reported.

Usage:
  poetry run python -m portfolio.protection.migration            # dry run
  poetry run python -m portfolio.protection.migration --apply    # write and cancel legs
"""

from __future__ import annotations

import argparse
import copy
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from portfolio.config import get_mode_config
from portfolio.protection.software import protection_row_from_state
from portfolio.state_store import AlgoStateStore


STOP_TYPES = {"stop", "stop_limit"}


def _decimal(value: object) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _exit_legs(broker, ticker: str, position_qty: Decimal, expected_ids: set[str]) -> tuple[dict | None, list[str], str | None]:
    """Find the exact exit legs of the entry bracket or OCO for a held position.

    Returns (parent, leg_ids, reason). ``reason`` is set when the legs cannot
    be proven, in which case nothing may be migrated for the position.
    """
    open_orders = [
        order for order in broker.orders("open")
        if str(order.get("symbol", "")).upper() == ticker
    ]
    if any(not order.get("parent_order_id") for order in open_orders):
        return None, [], "unexpected open broker order that is not an exit leg"
    parent_ids = {str(order["parent_order_id"]) for order in open_orders}
    if not parent_ids:
        # No broker exit legs: the position is already protected in software only.
        return None, [], "no broker exit legs; already software-protected"
    if len(parent_ids) != 1:
        return None, [], f"expected one exit parent, found {len(parent_ids)}"
    parent_id = next(iter(parent_ids))
    if parent_id not in expected_ids:
        return None, [], "exit parent is not a persisted Algo order for this position"
    parent = broker.order(parent_id, nested=True)
    if not isinstance(parent, dict) or str(parent.get("id")) != parent_id:
        return None, [], "exit parent could not be read exactly"
    order_class = str(parent.get("order_class") or "").lower()
    legs = [leg for leg in parent.get("legs") or [] if isinstance(leg, dict)]
    if order_class == "bracket":
        stops = [leg for leg in legs if str(leg.get("type", "")).lower() in STOP_TYPES]
        targets = [leg for leg in legs if str(leg.get("type", "")).lower() == "limit"]
    elif order_class == "oco":
        stops = [leg for leg in legs if str(leg.get("type", "")).lower() in STOP_TYPES]
        targets = [parent] if str(parent.get("type", "")).lower() == "limit" else []
    else:
        return None, [], f"unsupported exit parent class: {order_class or 'simple'}"
    if len(stops) != 1 or len(targets) != 1:
        return None, [], "exit legs are not exactly one stop and one target"
    stop_leg, target_leg = stops[0], targets[0]
    for leg in (stop_leg, target_leg):
        if _decimal(leg.get("qty")) != abs(position_qty):
            return None, [], "exit leg does not cover the full position"
    if order_class == "oco":
        # Cancelling the OCO parent cancels its stop child with it.
        leg_ids = [parent_id]
    else:
        leg_ids = [str(stop_leg["id"]), str(target_leg["id"])]
    return {
        "stop": _decimal(stop_leg.get("stop_price")),
        "target": _decimal(target_leg.get("limit_price")),
    }, leg_ids, None


def plan_migration(store: AlgoStateStore, broker, *, now: datetime, risk: dict) -> list[dict]:
    """Describe, without writing, what the migration would do for each position."""
    _, portfolio = store.read()
    owners = portfolio.get("position_owners") or {}
    states = portfolio.get("strategy_states") or {}
    plans: list[dict] = []
    for position in broker.positions():
        ticker = str(position.get("symbol", "")).upper()
        quantity = _decimal(position.get("qty"))
        plan = {"ticker": ticker, "status": "skipped", "reason": "", "leg_ids": []}
        plans.append(plan)
        strategy = owners.get(ticker)
        if not strategy or quantity is None or quantity == 0:
            plan["reason"] = "not an owned, non-zero Algo position"
            continue
        state = (states.get(strategy) or {}).get(ticker) or {}
        expected_ids = {str(value) for value in (
            state.get("entry_order_id"), ((state.get("protection_recovery") or {}).get("order_id"))
        ) if value}
        try:
            parent_levels, leg_ids, reason = _exit_legs(broker, ticker, quantity, expected_ids)
        except Exception as exc:
            plan["reason"] = f"broker exit legs unavailable: {type(exc).__name__}: {exc}"
            continue
        if reason:
            plan["reason"] = reason
            continue
        persisted_stop = _decimal(state.get("stop_price"))
        persisted_target = _decimal(state.get("take_profit"))
        if persisted_stop is None or persisted_target is None:
            plan["reason"] = "persisted stop or target is missing"
            continue
        if parent_levels["stop"] != persisted_stop or parent_levels["target"] != persisted_target:
            plan["reason"] = "broker stop or target differs from the persisted levels"
            continue
        row = protection_row_from_state(
            ticker, state, strategy=strategy,
            broker_quantity=position.get("qty"),
            broker_entry_price=position.get("avg_entry_price"),
            risk=risk, now=now,
        )
        if row is None:
            plan["reason"] = "protection row cannot be built from the persisted state"
            continue
        plan.update({"status": "ready", "reason": "", "leg_ids": leg_ids, "row": row})
    return plans


def apply_migration(store: AlgoStateStore, broker, plans: list[dict]) -> list[dict]:
    """Write each ready row, verify it, then cancel that position's legs."""
    results: list[dict] = []
    for plan in plans:
        if plan["status"] != "ready":
            results.append({"ticker": plan["ticker"], "status": "skipped", "reason": plan["reason"]})
            continue
        ticker = plan["ticker"]
        row = plan["row"]
        try:
            store.upsert_protection(row)
            stored = store.read_protection().get(ticker) or {}
            if (
                stored.get("initial_stop") != row["initial_stop"]
                or stored.get("target") != row["target"]
                or stored.get("quantity") != row["quantity"]
                or stored.get("side") != row["side"]
            ):
                raise RuntimeError("stored protection row does not match the planned levels")
        except Exception as exc:
            results.append({
                "ticker": ticker, "status": "failed", "stage": "write_verify",
                "reason": f"{type(exc).__name__}: {exc}", "legs_left_in_place": plan["leg_ids"],
            })
            continue
        cancelled: list[str] = []
        errors: list[str] = []
        for leg_id in plan["leg_ids"]:
            try:
                broker.cancel_order(leg_id)
                cancelled.append(leg_id)
            except Exception as exc:
                errors.append(f"{leg_id}: {type(exc).__name__}: {exc}")
        results.append({
            "ticker": ticker,
            "status": "migrated" if not errors else "partial",
            "stop": row["initial_stop"],
            "target": row["target"],
            "legs_cancelled": cancelled,
            "cancel_errors": errors,
        })
    return results


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    from broker.algo import AlgoAlpaca
    from runtime.paths import ROOT
    from portfolio.daemon import _state_path

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write protection rows and cancel legs (default: dry run)")
    parser.add_argument("--db", type=Path, default=None, help="state database path (default: the daemon's)")
    args = parser.parse_args(argv)
    load_dotenv(ROOT / ".env")
    store = AlgoStateStore(args.db or _state_path())
    broker = AlgoAlpaca()
    now = datetime.now().astimezone()
    risk = copy.deepcopy(get_mode_config("algo")["risk"])
    plans = plan_migration(store, broker, now=now, risk=risk)
    output = {"mode": "apply" if args.apply else "dry_run", "positions": []}
    if args.apply:
        output["positions"] = apply_migration(store, broker, plans)
    else:
        for plan in plans:
            entry = {key: value for key, value in plan.items() if key != "row"}
            if "row" in plan:
                entry["row_preview"] = {
                    key: plan["row"][key]
                    for key in ("side", "quantity", "initial_stop", "target", "trailing_stop_pct", "trailing_activation_pct")
                }
            output["positions"].append(entry)
    print(json.dumps(output, indent=2, sort_keys=True, default=str))
    failed = any(item.get("status") in {"failed", "partial"} for item in output["positions"])
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
