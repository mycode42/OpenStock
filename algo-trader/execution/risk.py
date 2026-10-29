#!/usr/bin/env python3
"""
Trade risk validation — hard rules for Algo Trader orders.

Every proposed trade passes through validate_trade() before execution.
Rules are non-negotiable and code-enforced; no LLM can override them.

Usage (standalone check):
  poetry run python -m execution.risk --status
  poetry run python -m execution.risk --status --mode algo

Importable:
  from execution.risk import validate_trade, get_portfolio_state
"""

import logging
import math
import os
import sys
import json
import hashlib
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

import requests

from runtime.http import get_session

from portfolio.config import get_mode_config, is_cash_proxy
from execution.risk_models import RejectionReason, ValidationResult
from execution.risk_policy import build_universe_maps

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [execution.risk] %(levelname)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("execution.risk")

from broker.accounts import get_account_for_mode, verify_account_for_mode

from broker.endpoints import ALGO_TRADING_BASE as API_BASE
MARKET_TZ = ZoneInfo("America/New_York")

# Per-mode policy maps stay cached after their first build.
_universe_cache: dict[str, tuple] = {}


def _get_universe_maps(mode: str) -> tuple[dict, dict, set, dict]:
    return build_universe_maps(mode, get_mode_config, _universe_cache)


def _api(endpoint: str, mode: str = None) -> dict | list:
    headers = get_account_for_mode(mode).headers
    r = get_session().get(f"{API_BASE}/{endpoint}", headers=headers, timeout=10)
    r.raise_for_status()
    payload = r.json()
    if endpoint.startswith("orders") and isinstance(payload, list):
        # Cash-reserve ETF orders are not trade exposure.
        payload = [row for row in payload if not is_cash_proxy(row.get("symbol"))]
    return payload


from execution.risk_account import (
    _algo_session_bounds as _account_session_bounds,
    _count_algo_new_entries_today as _account_count_new_entries_today,
    _is_in_algo_session as _account_is_in_session,
    _pending_buy_exposure as _account_pending_buy_exposure,
    _pending_short_exposure as _account_pending_short_exposure,
    _easy_to_borrow_symbols as _fetch_easy_to_borrow_symbols,
    _get_algo_fill_activities as _fetch_algo_fill_activities,
    _get_algo_orders_since as _fetch_algo_orders_since,
    _get_open_orders as _fetch_open_orders,
    get_portfolio_state as _fetch_portfolio_state,
)


def _algo_session_bounds(session_date: str) -> tuple[datetime, datetime]:
    return _account_session_bounds(session_date)


def _is_in_algo_session(timestamp_value: str, session_date: str) -> bool:
    return _account_is_in_session(timestamp_value, session_date)


def _count_algo_new_entries_today(
    orders: list[dict], fills: list[dict], positions: dict[str, dict], session_date: str,
) -> int:
    return _account_count_new_entries_today(orders, fills, positions, session_date)


def _pending_buy_exposure(open_orders: list[dict], positions: dict[str, dict]) -> dict[str, float]:
    return _account_pending_buy_exposure(open_orders, positions)


def _pending_short_exposure(open_orders: list[dict], positions: dict[str, dict]) -> dict[str, float]:
    return _account_pending_short_exposure(open_orders, positions)


def _get_algo_orders_since(session_start: str, mode: str) -> list[dict]:
    return _fetch_algo_orders_since(session_start, mode, api=_api)


def _get_algo_fill_activities(session_start: str, mode: str) -> list[dict]:
    return _fetch_algo_fill_activities(session_start, mode, api=_api)


def _get_open_orders(mode: str) -> list[dict]:
    return _fetch_open_orders(mode, api=_api)


def _easy_to_borrow_symbols(mode: str) -> set[str]:
    return _fetch_easy_to_borrow_symbols(mode, api=_api)


def get_portfolio_state(mode: str = None) -> dict:
    return _fetch_portfolio_state(
        mode,
        api=_api,
        get_universe_maps=_get_universe_maps,
        verify_account=verify_account_for_mode,
        get_algo_orders_since=_get_algo_orders_since,
        get_algo_fill_activities=_get_algo_fill_activities,
        get_open_orders=_get_open_orders,
        fetch_easy_to_borrow_symbols=_easy_to_borrow_symbols,
        find_weekly_equity_baseline=_find_weekly_equity_baseline,
        estimate_weekly_pnl=_estimate_weekly_pnl,
    )


def stage_approved_trade(
    portfolio_state: dict,
    ticker: str,
    action: str,
    qty: int,
    entry_price: float,
    mode: str = None,
) -> dict:
    """Reserve an approved long or short entry before validating the next signal.

    The execution command can receive multiple strategy signals at once. Staging
    successful or dry-run entries prevents each signal from being checked
    against the same pre-batch cash, position, and sector totals.
    """
    state = deepcopy(portfolio_state)
    action = action.lower()
    if action not in {"buy", "short"} or qty <= 0 or entry_price <= 0:
        return state
    mode = mode or "algo"
    ticker = ticker.upper()
    trade_value = int(qty) * float(entry_price)
    equity = float(state.get("equity", 0) or 0)
    positions = state.setdefault("positions", {})
    existing = positions.get(ticker)
    previous_value = float((existing or {}).get("market_value", 0) or 0)
    previous_qty = float((existing or {}).get("qty", 0) or 0)
    signed_qty = int(qty) if action == "buy" else -int(qty)
    signed_value = trade_value if action == "buy" else -trade_value
    positions[ticker] = {
        **(existing or {}),
        "symbol": ticker,
        "qty": previous_qty + signed_qty,
        "market_value": previous_value + signed_value,
        "current_price": float(entry_price),
        "pct_of_equity": (previous_value + signed_value) / equity if equity else 0,
    }
    cash = float(state.get("cash", 0) or 0) + (trade_value if action == "short" else -trade_value)
    state["cash"] = cash
    state["cash_pct"] = cash / equity if equity else 0
    is_new_position = (existing is None or previous_qty == 0) and ticker not in set(
        state.get("pending_new_symbols") or []
    )
    if is_new_position:
        state["open_position_count"] = int(state.get("open_position_count", 0)) + 1
    available_cash = float(state.get("available_cash", cash) or 0) + (trade_value if action == "short" else -trade_value)
    state["available_cash"] = available_cash
    state["available_cash_pct"] = available_cash / equity if equity else 0
    previous_trade_count = state.get("trade_count_today")
    if previous_trade_count is not None:
        state["trade_count_today"] = int(previous_trade_count) + 1
    if state.get("entry_count_available", True):
        previous_entry_count = int(
            state.get("entry_count_today", previous_trade_count) or 0
        )
        if is_new_position:
            state["entry_count_today"] = previous_entry_count + 1

    ticker_sector, _, _, _ = _get_universe_maps(mode)
    sector = ticker_sector.get(ticker, "other")
    if sector != "other":
        allocation = state.setdefault("sector_alloc", {})
        allocation[sector] = float(allocation.get(sector, 0) or 0) + trade_value
    return state


def _snapshot_dir(mode: str) -> Path:
    """Return the Algo snapshot directory used by performance reporting."""
    from runtime.paths import DATA_DIR

    if mode != "algo":
        raise ValueError("Only the algo account route is supported")
    return DATA_DIR / "snapshots"


def _risk_baseline_dir(mode: str) -> Path:
    from runtime.paths import DATA_DIR

    return DATA_DIR / "risk_baselines" / mode


def capture_account_inception_baseline(mode: str, *, now: datetime | None = None) -> dict:
    """Anchor a new, unused paper account to its verified opening equity.

    This is an explicit one-time operation after an Alpaca paper-account reset.
    The first trade must not be allowed to move the baseline forward.
    """
    if mode != "algo":
        raise ValueError("An explicit algo account route is required")
    account, broker = verify_account_for_mode(mode)
    broker_id = str(broker.get("id") or "")
    if not broker_id:
        raise ValueError("Broker account has no ID")
    current = (now or datetime.now(timezone.utc)).astimezone(MARKET_TZ)
    session_day = current.date() + timedelta(days=1) if current.hour >= 20 else current.date()
    week_start = session_day - timedelta(days=session_day.weekday())
    try:
        created_at = datetime.fromisoformat(str(broker["created_at"]).replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            raise ValueError("Broker account creation time has no timezone")
        created_local = created_at.astimezone(MARKET_TZ)
        equity = float(broker["equity"])
        cash = float(broker["cash"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Broker account inception details are unavailable") from exc
    if created_local > current or (current - created_local) > timedelta(days=7):
        raise ValueError("Broker account is too old to capture an inception baseline")
    if not math.isfinite(equity) or equity <= 0 or not math.isfinite(cash) or abs(equity - cash) > 0.01:
        raise ValueError("New account equity must be positive and equal to cash")
    positions = _api("positions", mode=mode)
    orders = _api("orders?status=all&limit=1", mode=mode)
    if not isinstance(positions, list) or not isinstance(orders, list) or positions or orders:
        raise ValueError("Inception baseline requires zero positions and zero order history")

    baseline = {
        "snapshot_kind": "account_inception",
        "trading_mode": mode,
        "account_id": broker_id,
        "account_created_at": created_at.astimezone(timezone.utc).isoformat(),
        "timestamp": current.astimezone(timezone.utc).isoformat(),
        "week_start": week_start.isoformat(),
        "equity": equity,
    }
    directory = _risk_baseline_dir(mode)
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(broker_id.encode("utf-8")).hexdigest()[:24]
    path = directory / f"{digest}.json"
    def existing_baseline() -> dict:
        with path.open() as handle:
            existing = json.load(handle)
        if existing != baseline:
            # Timestamp differs on a later invocation; allow only the already
            # captured account, creation time, week and opening equity.
            fields = ("snapshot_kind", "trading_mode", "account_id", "account_created_at", "week_start", "equity")
            if any(existing.get(field) != baseline[field] for field in fields):
                raise ValueError("Existing account inception baseline differs; refusing overwrite")
        return existing
    if path.exists():
        return existing_baseline()
    temp_path = None
    try:
        with NamedTemporaryFile("w", dir=directory, prefix=".inception-", suffix=".tmp", delete=False) as handle:
            temp_path = Path(handle.name)
            json.dump(baseline, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # Create the final name only if absent. Concurrent invocations
            # cannot replace a baseline after an equity move.
            os.link(temp_path, path)
        except FileExistsError:
            return existing_baseline()
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return baseline


def _find_weekly_equity_baseline(
    *, mode: str, account_id: str | None, session_day: date | None = None,
) -> dict | None:
    """Return the account-bound weekly equity baseline and its provenance."""
    mode = mode or "algo"
    if not account_id:
        return None
    today = session_day or datetime.now(MARKET_TZ).date()
    week_start = today - timedelta(days=today.weekday())
    previous_week_start = week_start - timedelta(days=7)
    # A newly created, never-traded paper account has no preceding close.
    # Its explicit inception record is the first week-of-life baseline.
    for baseline_file in _risk_baseline_dir(mode).glob("*.json"):
        try:
            with baseline_file.open() as handle:
                baseline = json.load(handle)
            starting_equity = float(baseline["equity"])
            if (
                baseline.get("snapshot_kind") == "account_inception"
                and baseline.get("trading_mode") == mode
                and baseline.get("account_id") == account_id
                and baseline.get("week_start") == week_start.isoformat()
                and math.isfinite(starting_equity)
                and starting_equity > 0
            ):
                return {
                    "date": week_start.isoformat(),
                    "equity": starting_equity,
                    "kind": "account_inception",
                }
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
    baselines: list[tuple[date, float]] = []
    for snapshot_file in _snapshot_dir(mode).glob("*.json"):
        try:
            with open(snapshot_file) as handle:
                snapshot = json.load(handle)
            snapshot_date = datetime.strptime(str(snapshot["date"]), "%Y-%m-%d").date()
            baseline_equity = float(snapshot["equity"])
            if (
                previous_week_start <= snapshot_date < week_start
                and snapshot.get("trading_mode") == mode
                and snapshot.get("account_id") == account_id
                and snapshot.get("snapshot_kind") == "close"
                and math.isfinite(baseline_equity)
                and baseline_equity > 0
            ):
                baselines.append((snapshot_date, baseline_equity))
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
    if not baselines:
        return None
    baseline_date, baseline_equity = max(baselines, key=lambda row: row[0])
    return {
        "date": baseline_date.isoformat(),
        "equity": baseline_equity,
        "kind": "close_snapshot",
    }


def _estimate_weekly_pnl(
    current_equity: float,
    mode: str = "algo",
    *,
    account_id: str | None = None,
    session_day: date | None = None,
) -> float | None:
    """Return week-to-date P&L from the same account's tagged baseline."""
    if not math.isfinite(current_equity) or current_equity <= 0:
        return None
    baseline = _find_weekly_equity_baseline(
        mode=mode or "algo", account_id=account_id, session_day=session_day,
    )
    if baseline is None:
        return None
    return (current_equity - baseline["equity"]) / baseline["equity"]


def validate_trade(
    ticker: str,
    action: str,
    qty: int,
    entry_price: float,
    portfolio_state: Optional[dict] = None,
    mode: str = None,
    strategy: str | None = None,
) -> ValidationResult:
    """
    Validate a proposed trade against Algo's hard risk rules.

    Args:
        ticker:           Stock symbol (e.g. "NVDA").
        action:           "buy", "sell", "short", "cover", "hold".
        qty:              Number of shares.
        entry_price:      Expected fill price (used for position size calc).
        portfolio_state:  Pre-fetched state dict. If None, fetches live.
        mode:             Algo account route.

    Returns:
        ValidationResult with approved=True/False and a reason string.
    """
    mode = mode or "algo"
    mode_config = get_mode_config(mode)
    risk = mode_config["risk"]
    ticker_sector, ticker_max_pct, tactical_tickers, universe = _get_universe_maps(mode)

    action = action.lower()

    if action == "hold":
        return ValidationResult(approved=True, reason="Hold — no order to validate")

    if action in ("sell", "cover"):
        if qty <= 0:
            return ValidationResult(False, "Exit quantity must be positive.", "invalid_exit_quantity")
        try:
            if portfolio_state is None:
                live_positions = {
                    row["symbol"]: row for row in _api("positions", mode=mode)
                }
            else:
                live_positions = portfolio_state["positions"]
            held_qty = float((live_positions.get(ticker.upper()) or {}).get("qty", 0))
        except Exception as exc:
            return ValidationResult(False, f"Could not verify exit position: {exc}", "position_unavailable")
        if action == "sell" and (held_qty <= 0 or qty > held_qty):
            return ValidationResult(False, "Sell exceeds the verified long position.", "oversell")
        if action == "cover" and (held_qty >= 0 or qty > abs(held_qty)):
            return ValidationResult(False, "Cover exceeds the verified short position.", "overcover")
        return ValidationResult(True, "Verified position-reducing exit")

    if action not in ("buy", "short"):
        return ValidationResult(approved=False, reason=f"Unknown action '{action}'", rule="unknown_action")

    if action == "short":
        if portfolio_state is None:
            try:
                portfolio_state = get_portfolio_state(mode=mode)
            except Exception as exc:
                return ValidationResult(False, f"Could not verify shortability and portfolio state: {exc}", "portfolio_fetch_failed")

    try:
        entry_price = float(entry_price)
    except (TypeError, ValueError):
        entry_price = 0.0
    if qty <= 0 or not math.isfinite(entry_price) or entry_price <= 0:
        return ValidationResult(
            approved=False,
            reason="Entry quantity and reference price must both be positive.",
            rule="invalid_entry_parameters",
        )
    if ticker.upper() not in ticker_sector:
        return ValidationResult(
            False,
            f"{ticker.upper()} is outside the {mode} risk catalog; sector limits are unavailable.",
            "outside_risk_catalog",
        )

    if portfolio_state is None:
        try:
            portfolio_state = get_portfolio_state(mode=mode)
        except Exception as e:
            return ValidationResult(
                approved=False,
                reason=f"Could not fetch portfolio state: {e}",
                rule="portfolio_fetch_failed",
            )

    equity = portfolio_state["equity"]
    trade_value = qty * entry_price

    if not portfolio_state.get("pending_exposure_available", True):
        return ValidationResult(
            False, "Open buy/short exposure is unavailable; new entries are blocked.",
            "pending_exposure_unavailable",
        )

    # ── Rule 3: Daily loss circuit breaker ─────────────────────────────────
    daily_pnl = portfolio_state["daily_pnl_pct"]
    if daily_pnl <= -risk["daily_loss_limit"]:
        msg = (
            f"BLOCKED: Daily circuit breaker active — down "
            f"{abs(daily_pnl)*100:.1f}% today (limit {risk['daily_loss_limit']*100:.0f}%). "
            "No new entries today."
        )
        log.warning(msg)
        return ValidationResult(approved=False, reason=msg, rule="daily_circuit_breaker")

    # ── Rule 4: No new buys if portfolio down no_buy_if_down_pct ───────────
    if action == "buy" and daily_pnl <= -risk["no_buy_if_down_pct"]:
        msg = (
            f"BLOCKED: Portfolio down {abs(daily_pnl)*100:.1f}% today — "
            f"no new buys when down {risk['no_buy_if_down_pct']*100:.0f}%+ for the day."
        )
        log.warning(msg)
        return ValidationResult(approved=False, reason=msg, rule="no_buy_down_pct")

    # ── Rule 5: Weekly loss circuit breaker ────────────────────────────────
    weekly_pnl = portfolio_state["weekly_pnl_pct"]
    if weekly_pnl is None or not math.isfinite(float(weekly_pnl)):
        return ValidationResult(
            False, "Weekly loss baseline is missing or stale; new entries are blocked.",
            "weekly_baseline_unavailable",
        )
    if weekly_pnl <= -risk["weekly_loss_limit"]:
        msg = (
            f"BLOCKED: Weekly circuit breaker active — down "
            f"{abs(weekly_pnl)*100:.1f}% this week (limit {risk['weekly_loss_limit']*100:.0f}%). "
            "No new entries this week."
        )
        log.warning(msg)
        return ValidationResult(approved=False, reason=msg, rule="weekly_circuit_breaker")

    existing_position = portfolio_state["positions"].get(ticker.upper())
    existing_qty = float((existing_position or {}).get("qty", 0) or 0)
    if action == "buy" and existing_qty < 0:
        return ValidationResult(False, "Close the short position before opening a long position.", "opposing_position_open")
    if action == "short" and existing_qty > 0:
        return ValidationResult(False, "Close the long position before opening a short position.", "opposing_position_open")
    pending_buy = ticker.upper() in (portfolio_state.get("pending_buy_value_by_ticker") or {})
    pending_short = ticker.upper() in (portfolio_state.get("pending_short_value_by_ticker") or {})
    if action == "buy" and pending_short:
        return ValidationResult(False, "An opening short order is already pending for this ticker.", "opposing_order_pending")
    if action == "short" and pending_buy:
        return ValidationResult(False, "An opening long order is already pending for this ticker.", "opposing_order_pending")
    if abs(existing_qty) <= 1e-9 and ((action == "buy" and pending_buy) or (action == "short" and pending_short)):
        return ValidationResult(False, "An opening order is already pending for this ticker.", "entry_order_pending")
    is_new_position = abs(existing_qty) <= 1e-9 and not (pending_buy or pending_short)

    # ── Rule 6: Max trades / new entries per day ────────────────────────────
    # Algo's quota counts buys or shorts that open a flat ticker. Scale-ins
    # and exits do not use entry slots; Rule 9 still caps total size.
    if action in {"buy", "short"} and is_new_position:
        entry_count = portfolio_state.get(
            "entry_count_today", portfolio_state.get("trade_count_today")
        )
        count_available = portfolio_state.get("entry_count_available", entry_count is not None)
        if not count_available or entry_count is None:
            msg = "BLOCKED: Algo new-entry count unavailable; refusing an uncounted entry."
            log.warning(msg)
            return ValidationResult(approved=False, reason=msg, rule="entry_count_unavailable")
        if entry_count >= risk["max_trades_per_day"]:
            msg = (
                f"BLOCKED: Algo max {risk['max_trades_per_day']} new entries/day reached "
                f"({entry_count} already today)."
            )
            log.warning(msg)
            return ValidationResult(approved=False, reason=msg, rule="max_trades_per_day")

    # ── Rule 7: Max open positions ──────────────────────────────────────────
    if is_new_position and portfolio_state["open_position_count"] >= risk["max_open_positions"]:
        msg = (
            f"BLOCKED: Max {risk['max_open_positions']} open positions reached "
            f"({portfolio_state['open_position_count']} open). Close a position first."
        )
        log.warning(msg)
        return ValidationResult(approved=False, reason=msg, rule="max_open_positions")

    # ── Rule 8: Min cash reserve ────────────────────────────────────────────
    cash_available = float(portfolio_state.get("available_cash", portfolio_state["cash"]))
    cash_after = cash_available + trade_value if action == "short" else cash_available - trade_value
    cash_pct_after = (cash_after / equity) if equity else 0
    if action == "buy" and cash_pct_after < risk["min_cash_pct"]:
        msg = (
            f"BLOCKED: Trade would reduce cash to {cash_pct_after*100:.1f}% "
            f"(minimum {risk['min_cash_pct']*100:.0f}%). "
            f"Max trade value: ${cash_available - equity * risk['min_cash_pct']:,.0f}."
        )
        log.warning(msg)
        return ValidationResult(approved=False, reason=msg, rule="min_cash_reserve")

    # ── Rule 9: Max position size (per-ticker or mode default) ─────────────
    effective_max_pct = ticker_max_pct.get(ticker.upper(), risk["max_position_pct"])
    current_ticker_value = abs(float((existing_position or {}).get("market_value", 0) or 0))
    pending_value_map = (
        portfolio_state.get("pending_short_value_by_ticker", {}) if action == "short"
        else portfolio_state.get("pending_buy_value_by_ticker", {})
    )
    existing_value = current_ticker_value + float(pending_value_map.get(ticker.upper(), 0) or 0)
    new_total_value = existing_value + trade_value
    position_pct = new_total_value / equity if equity else 0

    if position_pct > effective_max_pct:
        msg = (
            f"BLOCKED: {ticker} position would reach {position_pct*100:.1f}% of equity "
            f"(max {effective_max_pct*100:.0f}%). "
            f"Max additional: ${equity * effective_max_pct - existing_value:,.0f}."
        )
        log.warning(msg)
        return ValidationResult(approved=False, reason=msg, rule="max_position_size")

    if action == "short":
        short_positions = {
            symbol for symbol, position in portfolio_state["positions"].items()
            if float((position or {}).get("qty", 0) or 0) < 0
        }
        short_positions.update(
            symbol for symbol in (portfolio_state.get("pending_short_value_by_ticker") or {})
            if symbol not in portfolio_state["positions"]
        )
        if is_new_position and ticker.upper() not in short_positions and len(short_positions) >= int(risk.get("max_short_positions", 0)):
            return ValidationResult(False, "Maximum short position count reached.", "max_short_positions")
        short_exposure = sum(
            abs(float((position or {}).get("market_value", 0) or 0))
            for position in portfolio_state["positions"].values()
            if float((position or {}).get("qty", 0) or 0) < 0
        ) + sum(float(value or 0) for value in (portfolio_state.get("pending_short_value_by_ticker") or {}).values())
        short_exposure += trade_value
        max_short_exposure = equity * float(risk.get("max_short_exposure_pct", 0))
        if short_exposure > max_short_exposure:
            return ValidationResult(
                False,
                f"Short exposure would reach {short_exposure / equity:.1%} of equity "
                f"(max {float(risk.get('max_short_exposure_pct', 0)):.0%}).",
                "max_short_exposure_pct",
            )

    # ── Rule 10: Max sector allocation ─────────────────────────────────────
    sector = ticker_sector.get(ticker.upper(), "other")
    if sector != "other":
        sector_cfg = universe.get(sector, {})
        sector_cap = sector_cfg.get("max_sector_pct", risk["max_sector_pct"])
        current_sector_value = portfolio_state["sector_alloc"].get(sector, 0)
        new_sector_value = current_sector_value + trade_value
        sector_pct = new_sector_value / equity if equity else 0

        if sector_pct > sector_cap:
            msg = (
                f"BLOCKED: {sector} sector would reach {sector_pct*100:.1f}% "
                f"(cap {sector_cap*100:.0f}%). "
                f"Current sector: ${current_sector_value:,.0f}."
            )
            log.warning(msg)
            return ValidationResult(approved=False, reason=msg, rule="max_sector_pct")

    # ── Rule 11: Tactical bucket cap ────────────────────────────────────────
    if tactical_tickers and risk["max_tactical_pct"] < 1.0:
        if ticker.upper() in tactical_tickers:
            max_tactical_positions = risk.get("max_tactical_positions")
            if max_tactical_positions is not None:
                tactical_symbols = {
                    symbol.upper()
                    for symbol, position in portfolio_state["positions"].items()
                    if (symbol.upper() in tactical_tickers)
                    and abs(float(position.get("qty", 0) or 0)) > 0
                }
                tactical_symbols.update(
                    symbol.upper()
                    for symbol, value in (portfolio_state.get("pending_buy_value_by_ticker") or {}).items()
                    if (symbol.upper() in tactical_tickers)
                    and float(value or 0) > 0
                )
                tactical_symbols.update(
                    symbol.upper()
                    for symbol, value in (portfolio_state.get("pending_short_value_by_ticker") or {}).items()
                    if (symbol.upper() in tactical_tickers)
                    and float(value or 0) > 0
                )
                if ticker.upper() not in tactical_symbols and len(tactical_symbols) >= max_tactical_positions:
                    msg = (
                        f"BLOCKED: Tactical position limit of {max_tactical_positions} reached "
                        f"({len(tactical_symbols)} active or pending)."
                    )
                    log.warning(msg)
                    return ValidationResult(False, msg, "max_tactical_positions")

            tactical_positions = [
                v for k, v in portfolio_state["positions"].items()
                if k in tactical_tickers
            ]
            total_tactical = sum(abs(float(p.get("market_value", 0) or 0)) for p in tactical_positions)
            total_tactical += sum(
                value for symbol, value in
                (portfolio_state.get("pending_buy_value_by_ticker") or {}).items()
                if symbol in tactical_tickers
            )
            total_tactical += sum(
                value for symbol, value in
                (portfolio_state.get("pending_short_value_by_ticker") or {}).items()
                if symbol in tactical_tickers
            )
            new_total = total_tactical + trade_value
            tactical_pct = new_total / equity if equity else 0

            if tactical_pct > risk["max_tactical_pct"]:
                msg = (
                    f"BLOCKED: High-risk / tactical allocation would reach {tactical_pct*100:.1f}% "
                    f"(max {risk['max_tactical_pct']*100:.0f}%)."
                )
                log.warning(msg)
                return ValidationResult(approved=False, reason=msg, rule="max_tactical_pct")

    # ── All rules passed ────────────────────────────────────────────────────
    approved_msg = (
        f"APPROVED: {action.upper()} {qty} {ticker} @ ${entry_price:.2f} "
        f"(${trade_value:,.0f} = {trade_value/equity*100:.1f}% of equity). "
        f"Daily P&L: {daily_pnl*100:+.2f}%, Cash after: {cash_pct_after*100:.1f}%."
    )
    log.info(approved_msg)
    return ValidationResult(approved=True, reason=approved_msg)


def print_status(portfolio_state: dict, mode: str = "algo") -> None:
    """Pretty-print current risk status to stdout."""
    mode = mode or "algo"
    mode_config = get_mode_config(mode)
    risk = mode_config["risk"]
    ticker_sector, _, _, universe = _get_universe_maps(mode)

    eq = portfolio_state["equity"]
    print(f"{'='*60}")
    print(f"  ALGO TRADER — RISK VALIDATOR STATUS")
    print(f"  Mode: {mode.upper()} — {mode_config['label']}")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")
    print(f"  Equity:          ${eq:>12,.2f}")
    print(f"  Cash:            ${portfolio_state['cash']:>12,.2f}  ({portfolio_state['cash_pct']*100:.1f}%)")
    reserved = portfolio_state.get("pending_buy_reservation", 0)
    if reserved:
        print(f"  Pending buys:    ${reserved:>12,.2f} reserved")
    print(f"  Daily P&L:        {portfolio_state['daily_pnl_pct']*100:>+11.2f}%")
    weekly_pnl = portfolio_state.get("weekly_pnl_pct")
    weekly_display = f"{weekly_pnl*100:+.2f}%" if weekly_pnl is not None else "unavailable"
    print(f"  Weekly P&L:       {weekly_display:>12}")
    print(f"  Open Positions:  {portfolio_state['open_position_count']:>13}  / {risk['max_open_positions']} max")
    entry_count = portfolio_state.get("entry_count_today")
    count_label = "New Entries Today"
    count_display = "unavailable" if entry_count is None or not portfolio_state.get("entry_count_available", True) else str(entry_count)
    print(f"  {count_label:<18}{count_display:>13}  / {risk['max_trades_per_day']} max")
    print()

    daily_cb = portfolio_state["daily_pnl_pct"] <= -risk["daily_loss_limit"]
    no_buy_cb = portfolio_state["daily_pnl_pct"] <= -risk["no_buy_if_down_pct"]
    weekly_cb = weekly_pnl is None or weekly_pnl <= -risk["weekly_loss_limit"]
    cash_ok = portfolio_state.get("available_cash_pct", portfolio_state["cash_pct"]) >= risk["min_cash_pct"]

    print(f"  CIRCUIT BREAKERS:")
    print(f"    Daily  (-{risk['daily_loss_limit']*100:.0f}%):  {'🔴 ACTIVE' if daily_cb else '🟢 OK'}")
    print(f"    No-buy (-{risk['no_buy_if_down_pct']*100:.0f}%):  {'🔴 ACTIVE' if no_buy_cb else '🟢 OK'}")
    weekly_status = "⚪ UNAVAILABLE" if weekly_pnl is None else ("🔴 ACTIVE" if weekly_cb else "🟢 OK")
    print(f"    Weekly (-{risk['weekly_loss_limit']*100:.0f}%): {weekly_status}")
    print(f"    Cash   (>{risk['min_cash_pct']*100:.0f}%):   {'🟢 OK' if cash_ok else '🔴 LOW'}")
    print()

    if portfolio_state["sector_alloc"]:
        print(f"  SECTOR ALLOCATIONS:")
        for sector, value in sorted(portfolio_state["sector_alloc"].items(), key=lambda x: -x[1]):
            pct = value / eq * 100 if eq else 0
            cap = universe.get(sector, {}).get("max_sector_pct", risk["max_sector_pct"]) * 100
            flag = "⚠️" if pct > cap * 0.9 else "  "
            print(f"    {flag} {sector:<20} ${value:>10,.0f}  ({pct:>5.1f}% / {cap:.0f}% cap)")
    print()


__all__ = [
    "RejectionReason",
    "ValidationResult",
    "capture_account_inception_baseline",
    "get_portfolio_state",
    "print_status",
    "stage_approved_trade",
    "validate_trade",
]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Algo Trader risk validator")
    parser.add_argument("--status", action="store_true", help="Show current risk status")
    parser.add_argument(
        "--mode",
        choices=["algo"],
        default="algo",
        help="Account route (only the Algo paper account is supported)",
    )
    args = parser.parse_args()

    mode = args.mode

    if args.status:
        try:
            state = get_portfolio_state(mode=mode)
            print_status(state, mode=mode)
        except Exception as e:
            print(f"Error fetching portfolio state: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        parser.print_help()
