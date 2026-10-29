#!/usr/bin/env python3
"""
Order execution — validates and submits approved decisions through Alpaca.

Input: JSON on stdin or --file with format:
{
  "trades": [
    {"ticker": "NVDA", "action": "buy", "qty": 50, "reasoning": "..."},
    {"ticker": "NVDA", "action": "buy", "qty": 20, "stop_price": 900, "take_profit": 1050, "reasoning": "software stop and target"},
    {"ticker": "NVDA", "action": "buy", "qty": 10, "order_type": "limit", "limit_price": 920, "reasoning": "..."},
    {"ticker": "NVDA", "action": "sell", "qty": 10, "reasoning": "exit existing position"},
    {"ticker": "NVDA", "action": "short", "qty": 10, "stop_price": 920, "take_profit": 850, "reasoning": "breakdown below VWAP"}
  ]
}

Order types: market (default), limit. Stops, targets, and trailing stops are
software-managed; no bracket, OCO, or stop order is sent to the broker.
Actions: buy, sell, hold, short, cover

Usage:
  echo '{"trades":[...]}' | poetry run python -m execution
  poetry run python -m execution --file decisions.json
  poetry run python -m execution --file decisions.json --dry-run
  poetry run python -m execution --flatten               # Sell all positions at market
  poetry run python -m execution --flatten --dry-run     # Preview flatten
  poetry run python -m execution --mode algo --file decisions.json
"""

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [execution] %(levelname)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("execution.runner")

from broker.accounts import get_account_for_mode, verify_account_for_mode
from market.calendar import get_market_schedule
from execution import batch, broker, liquidation, orders
from execution.liquidation import LiquidationHooks
from execution.orders import OrderHooks

from portfolio.config import ALGO_SERVICE_CONFIG, get_mode_config

from runtime.paths import DATA_DIR, ROOT

REPO_ROOT = ROOT
API_BASE = broker.API_BASE
DATA_BASE = broker.DATA_BASE
MARKET_TZ = ZoneInfo("America/New_York")
LOCK_DIR = DATA_DIR / "locks"

# Module-level mode — set once in main(), used by helpers
_active_mode: str = "algo"


def _headers(mode: str = None) -> dict:
    """Get API headers for the currently active trading mode."""
    return get_account_for_mode(mode or _active_mode).headers


# risk validator — required for every non-hold order
try:
    from execution.risk import (
        validate_trade as rm_validate_trade,
        get_portfolio_state as rm_get_portfolio_state,
        stage_approved_trade as rm_stage_approved_trade,
    )

    RISK_VALIDATOR_AVAILABLE = True
    log.info("risk validator loaded")
except ImportError:
    RISK_VALIDATOR_AVAILABLE = False
    log.error("execution.risk is unavailable — all orders will be blocked")


def get_account():
    return broker.get_account(_headers())


def get_positions():
    return broker.get_positions(_headers())


def get_latest_price(ticker: str) -> float:
    return broker.get_latest_price(ticker, _headers())


def get_latest_ask(ticker: str, *, mode: str, feed: str | None = None) -> float:
    return broker.get_latest_quote(ticker, "ask", _headers(mode), feed=feed)


def get_latest_bid(ticker: str, *, mode: str, feed: str | None = None) -> float:
    return broker.get_latest_quote(ticker, "bid", _headers(mode), feed=feed)


def account_execution_lock(mode: str, timeout_seconds: float = 10.0):
    return liquidation.account_execution_lock(mode, LOCK_DIR, timeout_seconds)


def _list_open_orders(headers: dict) -> list[dict]:
    return broker.list_open_orders(headers)


def _list_positions(headers: dict) -> list[dict]:
    return broker.list_positions(headers)


def _open_order_ids(orders_to_cancel: list[dict]) -> list[str]:
    return broker.open_order_ids(orders_to_cancel)


def _cancel_orders(headers: dict, orders_to_cancel: list[dict]) -> tuple[list[str], list[str]]:
    if _active_mode == "algo" and not _regular_market_open():
        return [], ["Algo order cancellations are restricted to regular market hours"]
    return broker.cancel_orders(headers, orders_to_cancel)


def _position_qty(position: dict) -> Decimal:
    return broker.position_qty(position)


def _submit_flatten_order(headers: dict, position: dict) -> dict:
    if _active_mode == "algo" and not _regular_market_open():
        return {
            "ticker": str(position.get("symbol") or "").upper(),
            "status": "failed",
            "error": "Algo order submissions are restricted to regular market hours",
        }
    return broker.submit_flatten_order(headers, position)


def _regular_market_open() -> bool:
    """Require a current Alpaca calendar window before Algo order actions."""
    try:
        schedule = get_market_schedule(mode="algo")
        return bool(
            schedule is not None
            and schedule.market_open(datetime.now(MARKET_TZ))
        )
    except Exception as exc:
        log.warning("Could not verify regular market hours; blocking Algo order action: %s", exc)
        return False


def _get_order_status(headers: dict, order_id: str) -> str:
    return broker.get_order_status(headers, order_id)


def _lookup_client_order(headers: dict, client_order_id: str) -> dict | None:
    return broker.lookup_client_order(headers, client_order_id)


def _get_nested_order(headers: dict, order_id: str) -> dict:
    return broker.get_nested_order(headers, order_id)


def _prices_equal(left: object, right: object) -> bool:
    return orders.prices_equal(left, right)


def _replay_matches_order(order: dict, broker_order: dict, headers: dict) -> bool:
    return orders.replay_matches_order(order, broker_order, headers, _get_nested_order)


def _order_hooks(mode: str = None) -> OrderHooks:
    return OrderHooks(
        mode=mode or _active_mode,
        headers=_headers,
        verify_algo_execution_lease=_verify_algo_execution_lease,
        lookup_client_order=_lookup_client_order,
        get_nested_order=_get_nested_order,
        get_mode_config=get_mode_config,
        get_market_schedule=get_market_schedule,
        list_positions=_list_positions,
        position_qty=_position_qty,
        list_open_orders=_list_open_orders,
        submit_broker_order=_submit_broker_order,
    )


def _submit_broker_order(order: dict, order_type: str, mode: str | None = None) -> dict:
    return orders.submit_broker_order(order, order_type, mode, _order_hooks(mode))


def _liquidation_hooks() -> LiquidationHooks:
    return LiquidationHooks(
        headers=_headers,
        verify_algo_execution_lease=_verify_algo_execution_lease,
        verify_account_for_mode=verify_account_for_mode,
        list_positions=_list_positions,
        list_open_orders=_list_open_orders,
        position_qty=_position_qty,
        cancel_orders=_cancel_orders,
        submit_flatten_order=_submit_flatten_order,
        get_order_status=_get_order_status,
    )


def _flatten_all_unlocked(
    dry_run: bool = False,
    mode: str = "algo",
    poll_attempts: int = 10,
    poll_interval: float = 1.0,
) -> dict:
    return liquidation.flatten_all_unlocked(
        _liquidation_hooks(),
        dry_run,
        mode,
        poll_attempts,
        poll_interval,
    )


def flatten_all(
    dry_run: bool = False,
    mode: str = None,
    poll_attempts: int = 10,
    poll_interval: float = 1.0,
) -> dict:
    mode = mode or _active_mode
    with account_execution_lock(mode):
        return _flatten_all_unlocked(dry_run, mode, poll_attempts, poll_interval)


def place_order(
    ticker,
    action,
    qty,
    order_type="market",
    stop_price=None,
    take_profit=None,
    limit_price=None,
    trail_percent=None,
    entry_price=None,
    client_order_id=None,
    extended_hours=False,
):
    return orders.place_order(
        ticker,
        action,
        qty,
        _order_hooks(),
        order_type=order_type,
        stop_price=stop_price,
        take_profit=take_profit,
        limit_price=limit_price,
        trail_percent=trail_percent,
        entry_price=entry_price,
        client_order_id=client_order_id,
        extended_hours=extended_hours,
    )


def open_exit_orders_for_symbol(ticker: str, *, mode: str, action: str) -> list[dict]:
    """Open broker orders on the closing side of a position, used to block duplicate exits."""
    closing_side = "sell" if action == "sell" else "buy"
    return [
        order for order in _list_open_orders(_headers(mode))
        if str(order.get("symbol", "")).upper() == str(ticker).upper()
        and str(order.get("side", "")).lower() == closing_side
    ]


def get_daily_pnl(account: dict) -> float:
    """Return today's P&L as a fraction of starting equity (negative = loss)."""
    equity = float(account.get("equity", 0))
    last_equity = float(account.get("last_equity", equity))
    if last_equity <= 0:
        return 0.0
    return (equity - last_equity) / last_equity


def _verify_algo_execution_lease() -> None:
    """Permit Algo writes only from the currently leased coordinator process."""
    from portfolio.state_store import AlgoStateStore

    state_path = Path(ALGO_SERVICE_CONFIG["state_db"])
    if not state_path.is_absolute():
        state_path = REPO_ROOT / state_path
    token = os.environ.get("ALGO_COORDINATOR_TOKEN")
    if not AlgoStateStore(state_path).verify_coordinator_token(token, os.getppid()):
        raise PermissionError("Live Algo orders require the active coordinator lease")


def validate_position_reduction(ticker, action, qty, positions):
    """Verify fresh position-reducing quantities after order cancellation."""
    pos = positions.get(ticker, {})
    current_shares = float(pos.get("qty", 0))

    if action == "sell" and current_shares <= 0:
        return False, f"No long position in {ticker} to sell"
    if action == "sell" and qty > current_shares:
        return False, f"Sell quantity {qty} exceeds the {current_shares:g} shares currently held in {ticker}"
    if action == "cover" and current_shares >= 0:
        return False, f"No short position in {ticker} to cover"
    if action == "cover" and qty > abs(current_shares):
        return False, f"Cover quantity {qty} exceeds the {abs(current_shares):g} short shares in {ticker}"

    return True, ""


def _run_main(args: argparse.Namespace, mode: str) -> int:
    mode_config = get_mode_config(mode)
    mode_risk = mode_config["risk"]

    # Set module-level mode so all API helpers route to the correct account
    global _active_mode
    _active_mode = mode

    acct = get_account_for_mode(mode)
    log.info(f"Trading mode: {mode.upper()} — {mode_config['label']} (account: {acct.name})")

    # Confirm the credentials resolve to the configured, active paper account
    # before any live order. Keep the broker payload for the risk snapshot.
    broker_account = None
    if not args.dry_run:
        _verify_algo_execution_lease()
        _, broker_account = verify_account_for_mode(mode)

    # Handle a verified Algo account flatten separately from entry decisions.
    if args.flatten:
        result = _flatten_all_unlocked(dry_run=args.dry_run, mode=mode)
        print(json.dumps(result, indent=2))
        return 0 if args.dry_run or result["success"] else 2

    # Read decisions
    if args.file:
        with open(args.file) as f:
            decisions = json.load(f)
    else:
        decisions = json.load(sys.stdin)

    trades = decisions.get("trades", [])
    if not trades:
        print(json.dumps({"status": "no_trades", "message": "No trades to execute"}))
        return 0
    decision_id = decisions.get("decision_id") or decisions.get("timestamp") or hashlib.sha256(json.dumps(decisions, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    decision_digest = hashlib.sha256(str(decision_id).encode("utf-8")).hexdigest()[:16]

    # Get current state
    account = broker_account or get_account()
    positions = get_positions()
    daily_pnl_pct = get_daily_pnl(account)

    daily_loss_limit = mode_risk["daily_loss_limit"]

    if daily_pnl_pct <= -daily_loss_limit:
        log.warning(f"Circuit breaker ACTIVE: down {abs(daily_pnl_pct)*100:.1f}% today " f"(limit {daily_loss_limit*100:.0f}%). New buy/short entries blocked.")

    # Pre-fetch V2 portfolio state once (shared across all trade validations)
    rm_portfolio_state = None
    if RISK_VALIDATOR_AVAILABLE:
        try:
            rm_portfolio_state = rm_get_portfolio_state(mode=mode)
            log.info("risk validator portfolio state loaded")
        except Exception as e:
            log.warning(f"Could not pre-fetch V2 portfolio state: {e}. Will validate per-trade.")

    results, submitted_count, would_execute_count = batch.execute_trade_batch(
        trades=trades,
        decision_digest=decision_digest,
        args=args,
        mode=mode,
        positions=positions,
        rm_portfolio_state=rm_portfolio_state,
        hooks=batch.BatchHooks(
            risk_validator_available=RISK_VALIDATOR_AVAILABLE,
            rm_validate_trade=rm_validate_trade,
            rm_stage_approved_trade=rm_stage_approved_trade,
            get_latest_ask=get_latest_ask,
            get_latest_bid=get_latest_bid,
            get_latest_price=get_latest_price,
            validate_position_reduction=validate_position_reduction,
            open_exit_orders_for_symbol=open_exit_orders_for_symbol,
            get_positions=get_positions,
            place_order=place_order,
            market_tz=MARKET_TZ,
            log=log,
            get_market_schedule=get_market_schedule,
        ),
    )

    trades_by_client_id = {
        str(trade.get("client_order_id")): trade
        for trade in trades if trade.get("client_order_id")
    }
    for row in results:
        source = trades_by_client_id.get(str(row.get("client_order_id") or ""), {})
        for key in ("research_profile_id", "research_profile_regime", "market_regime"):
            if key in source:
                row[key] = source[key]

    for row in results:
        row.setdefault("submission_status", row.get("status"))
        row.setdefault("broker_status", None)

    output = {
        "timestamp": datetime.now().isoformat(),
        "trading_mode": mode,
        "account_id": (str(broker_account.get("id") or broker_account.get("account_number") or "") if broker_account else None),
        "mode": "dry_run" if args.dry_run else "live",
        "daily_pnl_pct": round(daily_pnl_pct * 100, 2),
        "circuit_breaker_active": daily_pnl_pct <= -daily_loss_limit,
        "total_trades": len(trades),
        "submitted": submitted_count,
        "filled_at_submission": sum(r.get("broker_status") == "filled" for r in results),
        "would_execute": would_execute_count,
        "blocked": len([r for r in results if r.get("status") == "blocked"]),
        "results": results,
    }

    print(json.dumps(output, indent=2))

    # Auto-log to trade journal
    if not args.dry_run:
        try:
            from reporting.journal import append_trades

            logged = append_trades(output)
            print(f"📓 Logged {logged} trades to journal", file=sys.stderr)
        except Exception as e:
            print(f"⚠️ Journal logging failed: {e}", file=sys.stderr)

    return 0
