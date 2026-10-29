"""Serialized cancellation and account-wide position flattening."""

import fcntl
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import requests


MARKET_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class LiquidationHooks:
    headers: Callable
    verify_algo_execution_lease: Callable
    verify_account_for_mode: Callable
    list_positions: Callable
    list_open_orders: Callable
    position_qty: Callable
    cancel_orders: Callable
    submit_flatten_order: Callable
    get_order_status: Callable


@contextmanager
def account_execution_lock(mode: str, lock_dir: Path, timeout_seconds: float = 10.0):
    """Serialize validation, order submission, and flattening per account."""
    lock_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_dir / f"{mode}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(fd, 0o600)
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Could not acquire {mode} account execution lock")
                time.sleep(0.1)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def flatten_all_unlocked(
    hooks: LiquidationHooks,
    dry_run: bool = False,
    mode: str = "algo",
    poll_attempts: int = 10,
    poll_interval: float = 1.0,
) -> dict:
    """Cancel account orders, close residual positions, then verify flatness.

    The caller must hold the per-account execution lock. An ambiguous close
    submission is reconciled here; it is not blindly retried in this run.
    """
    headers = hooks.headers(mode)
    results: list[dict] = []
    errors: list[str] = []
    cancelled_ids: list[str] = []
    positions: list[dict] = []
    open_orders: list[dict] = []
    initial_count = 0

    def report(status: str) -> dict:
        remaining = []
        for position in positions:
            try:
                quantity = hooks.position_qty(position)
            except ValueError:
                quantity = "invalid"
            if quantity != 0:
                remaining.append({"ticker": str(position.get("symbol") or ""), "qty": str(quantity)})
        open_ids = [str(order.get("id") or "unknown") for order in open_orders]
        verified_flat = not remaining and not open_ids and status in ("flattened", "already_flat")
        return {
            "timestamp": datetime.now(MARKET_TZ).isoformat(),
            "trading_mode": mode,
            "mode": "dry_run" if dry_run else "live",
            "status": status,
            "success": verified_flat,
            "verified_flat": verified_flat,
            "positions_closed": max(0, initial_count - len(remaining)),
            "positions_remaining": remaining,
            "open_orders_remaining": open_ids,
            "orders_cancelled": cancelled_ids,
            "results": results,
            "errors": errors,
        }

    try:
        if not dry_run:
            hooks.verify_algo_execution_lease()
            hooks.verify_account_for_mode(mode)
        positions = hooks.list_positions(headers)
        open_orders = hooks.list_open_orders(headers)
        initial_count = sum(hooks.position_qty(position) != 0 for position in positions)
        if dry_run:
            results.extend(
                {
                    "ticker": position["symbol"],
                    "side": "sell" if hooks.position_qty(position) > 0 else "buy",
                    "qty": str(abs(hooks.position_qty(position))),
                    "status": "would_flatten",
                }
                for position in positions
                if hooks.position_qty(position) != 0
            )
            return report("dry_run")

        for attempt in range(poll_attempts):
            if not open_orders:
                break
            cancelled, cancel_errors = hooks.cancel_orders(headers, open_orders)
            cancelled_ids.extend(cancelled)
            errors.extend(cancel_errors)
            if attempt + 1 < poll_attempts:
                time.sleep(poll_interval)
            open_orders = hooks.list_open_orders(headers)
        if open_orders:
            errors.append("Open orders remained after cancellation retries")
            positions = hooks.list_positions(headers)
            return report("failed")

        # Orders may fill while cancellation is being reconciled.
        positions = hooks.list_positions(headers)
        if not any(hooks.position_qty(position) != 0 for position in positions):
            return report("already_flat" if initial_count == 0 else "flattened")

        for position in positions:
            if hooks.position_qty(position) != 0:
                results.append(hooks.submit_flatten_order(headers, position))

        for attempt in range(poll_attempts):
            if attempt:
                time.sleep(poll_interval)
            positions = hooks.list_positions(headers)
            open_orders = hooks.list_open_orders(headers)
            if not any(hooks.position_qty(position) != 0 for position in positions) and not open_orders:
                return report("flattened")
            for result in results:
                order_id = result.get("order_id")
                if order_id:
                    try:
                        result["broker_status"] = hooks.get_order_status(headers, order_id)
                    except requests.RequestException as exc:
                        errors.append(f"Could not confirm close order {order_id}: {exc}")
        errors.append("Broker did not confirm zero positions and zero open orders")
        return report("failed")
    except (requests.RequestException, ValueError, KeyError, TimeoutError) as exc:
        errors.append(str(exc))
        return report("failed")


def flatten_all(
    hooks: LiquidationHooks,
    lock_dir: Path,
    dry_run: bool = False,
    mode: str = "algo",
    poll_attempts: int = 10,
    poll_interval: float = 1.0,
) -> dict:
    with account_execution_lock(mode, lock_dir):
        return flatten_all_unlocked(hooks, dry_run, mode, poll_attempts, poll_interval)
