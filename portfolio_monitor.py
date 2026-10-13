#!/usr/bin/env python3
"""
Portfolio Monitor — Swarm Trader stop-loss enforcer and health check.

Designed to run on cron (e.g., 9:30 AM and 3:30 PM ET).  Checks every open
position against its hard stop and trailing stop from the active trading mode.
Auto-sells anything that breaches either stop.  In day mode, auto-flattens all
positions if it's past the flatten_by time.  Compares daily performance to SPY.

Usage:
  poetry run python portfolio_monitor.py              # Full check + auto-sell
  poetry run python portfolio_monitor.py --dry-run     # Report only, no sells
  poetry run python portfolio_monitor.py --mode day  # Day trading mode
  poetry run python portfolio_monitor.py --mode day --dry-run
"""

import argparse
import hashlib
import json
import logging
import math
import os
import sys
import time
from email.utils import parsedate_to_datetime
from urllib.parse import quote
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(override=True)

import requests
import yfinance as yf

from src.config import get_mode_config
from src.market_data import regular_feed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [portfolio_monitor] %(levelname)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("portfolio_monitor")
MARKET_TZ = ZoneInfo("America/New_York")

from src.accounts import get_account_for_mode, verify_account_for_mode
from src.market_calendar import get_day_schedule

API_BASE = "https://paper-api.alpaca.markets/v2"

# Module-level mode — set once when run_monitor() starts, used by helpers
_active_mode: str = "swing"


def _headers() -> dict:
    """Get API headers for the currently active trading mode."""
    return get_account_for_mode(_active_mode).headers


# ---------------------------------------------------------------------------
# Alpaca helpers
# ---------------------------------------------------------------------------

def _get(endpoint: str) -> dict | list:
    r = requests.get(f"{API_BASE}/{endpoint}", headers=_headers(), timeout=10)
    r.raise_for_status()
    return r.json()


def get_account() -> dict:
    return _get("account")


def get_positions() -> list[dict]:
    return _get("positions")


def place_market_close(symbol: str, qty: float, reason: str, dry_run: bool) -> dict:
    """Submit one verified sell/cover that reduces the held position."""
    action = "sell" if qty > 0 else "cover"
    verb = "sell" if action == "sell" else "cover"
    if dry_run:
        msg = f"DRY-RUN: Would {verb} {abs(qty)} {symbol} — {reason}"
        log.info(msg)
        return {"status": "dry_run", "symbol": symbol, "qty": abs(qty), "action": action, "reason": reason}

    from execute_trades import (
        _submit_broker_order, account_execution_lock, cancel_open_orders_for_symbol,
    )

    try:
        with account_execution_lock(_active_mode):
            verify_account_for_mode(_active_mode)
            if _active_mode == "day":
                risk = get_mode_config("day")["risk"]
                schedule = get_day_schedule(
                    flatten_by=risk.get("flatten_by", "15:45"),
                    flatten_lead_minutes=risk.get("flatten_lead_minutes", 15),
                    entry_cutoff_minutes=risk.get("entry_cutoff_minutes", 15),
                )
                now = datetime.now(MARKET_TZ)
                if schedule is None or not schedule.market_open(now) or schedule.flatten_due(now):
                    return {"status": "failed", "symbol": symbol, "qty": abs(qty), "action": action,
                            "reason": reason, "error": "Day stop close requires an open session before the flatten deadline"}
            cancelled = cancel_open_orders_for_symbol(symbol, mode=_active_mode)
            if not cancelled.get("success"):
                return {"status": "failed", "symbol": symbol, "qty": abs(qty), "action": action,
                        "reason": reason, "error": cancelled.get("error")}
            fresh = next((p for p in get_positions() if str(p.get("symbol", "")).upper() == symbol.upper()), None)
            held_qty = Decimal(str((fresh or {}).get("qty", 0)))
            if held_qty == 0:
                return {"status": "already_flat", "symbol": symbol, "qty": 0, "action": action, "reason": reason}
            if (action == "sell" and held_qty < 0) or (action == "cover" and held_qty > 0):
                return {"status": "failed", "symbol": symbol, "qty": abs(qty), "action": action,
                        "reason": reason, "error": "Position direction changed before stop close; refusing the order"}
            close_qty = abs(held_qty)
            event = reason.split(":", 1)[0].strip().lower()
            identity = f"{_active_mode}:{datetime.now(MARKET_TZ).date()}:{symbol}:{action}:{close_qty}:{event}"
            client_order_id = f"stop-{hashlib.sha256(identity.encode()).hexdigest()[:20]}"
            order = {
                "symbol": symbol,
                "qty": format(close_qty, "f"),
                "side": "sell" if action == "sell" else "buy",
                "position_intent": "sell_to_close" if action == "sell" else "buy_to_close",
                "type": "market",
                "time_in_force": "day",
                "client_order_id": client_order_id,
            }
            result = _submit_broker_order(order, "market", mode=_active_mode)
            if not result.get("success"):
                return {"status": "failed", "symbol": symbol, "qty": str(close_qty), "action": action,
                        "reason": reason, "client_order_id": client_order_id, **result}
            log.info("Submitted %s %s %s for %s, order %s", close_qty, symbol, verb, reason, result["order_id"])
            return {"status": "submitted", "symbol": symbol, "qty": str(close_qty), "action": action,
                    "reason": reason, "client_order_id": client_order_id, **result}
    except (requests.RequestException, ValueError, TimeoutError) as exc:
        return {"status": "failed", "symbol": symbol, "qty": abs(qty), "action": action, "reason": reason, "error": str(exc)}


# ---------------------------------------------------------------------------
# Price data helpers
# ---------------------------------------------------------------------------

# Protection data is scoped to one monitor invocation: never reuse a prior run.
DATA_BASE = "https://data.alpaca.markets/v2"


class StopDataError(Exception):
    """Sanitized provider/validation failure, safe for machine-readable output."""


def _stop_data_get(url: str, params: dict, deadline: float) -> dict:
    """Read-only GET, at most three attempts within a shared 30s budget."""
    for attempt in range(3):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise StopDataError("request_budget_exhausted")
        try:
            response = requests.get(
                url, headers=_headers(), params=params,
                timeout=(min(3, remaining / 2), min(5, remaining / 2)),
            )
        except requests.RequestException:
            if attempt == 2:
                raise StopDataError("provider_transport_failed") from None
            delay = 0.5 * (2 ** attempt)
        else:
            if time.monotonic() >= deadline:
                raise StopDataError("request_budget_exhausted")
            status = response.status_code
            if status == 200:
                try:
                    payload = response.json()
                except ValueError:
                    raise StopDataError("malformed_json") from None
                if not isinstance(payload, dict):
                    raise StopDataError("malformed_response")
                return payload
            if status not in {429, 500, 502, 503, 504} or attempt == 2:
                raise StopDataError(f"provider_http_{status}")
            delay = 0.5 * (2 ** attempt)
            retry_after = response.headers.get("Retry-After")
            if retry_after is not None:
                try:
                    delay = float(retry_after)
                except (TypeError, ValueError):
                    try:
                        until = parsedate_to_datetime(retry_after)
                        if until.tzinfo is None:
                            raise ValueError
                        delay = max(0, (until - datetime.now(timezone.utc)).total_seconds())
                    except (TypeError, ValueError, OverflowError):
                        raise StopDataError("invalid_retry_after") from None
                # Do not truncate a provider wait and retry too soon. Defer instead.
                if not math.isfinite(delay) or delay < 0 or delay > 5:
                    raise StopDataError("retry_after_exceeds_budget")
            if status == 429 and retry_after is None:
                reset = response.headers.get("X-RateLimit-Reset")
                if reset is not None:
                    try:
                        delay = max(delay, float(reset) - datetime.now(timezone.utc).timestamp())
                    except (ValueError, TypeError):
                        raise StopDataError("invalid_rate_limit_reset") from None
                    if not math.isfinite(delay) or delay > 5:
                        raise StopDataError("retry_after_exceeds_budget")
        if delay >= deadline - time.monotonic():
            raise StopDataError("request_budget_exhausted")
        time.sleep(delay)
    raise StopDataError("provider_retry_exhausted")


class SessionExtrema:
    """Alpaca SIP completed 1Min bars from the actual regular-session open.

    Missing minutes may mean no eligible trades (or a halt), not an outage.
    We cannot prove extrema from an incomplete grid: mark protection degraded,
    never fabricate bars or turn unknown protection into a not-triggered result.
    """

    def __init__(self, now: datetime | None = None):
        self.now = (now or datetime.now(MARKET_TZ)).astimezone(MARKET_TZ)
        self.cache: dict[str, dict] = {}
        self.schedule = None
        self.schedule_loaded = False
        self.schedule_error = None

    def get(self, symbol: str) -> dict:
        symbol = symbol.upper()
        if symbol in self.cache:
            cached = self.cache[symbol]
            if cached["status"] == "ok" and datetime.now(MARKET_TZ) - self.now > timedelta(seconds=60):
                cached.update(status="failed", code="snapshot_expired", high=None, low=None)
            return cached
        result = {"symbol": symbol, "source": "alpaca", "feed": None,
                  "status": "degraded", "code": None, "high": None, "low": None,
                  "session_date": self.now.date().isoformat(), "as_of": None,
                  "bars_received": 0, "expected_bars": 0}
        self.cache[symbol] = result
        try:
            result["feed"] = regular_feed()
            if not self.schedule_loaded:
                self.schedule_loaded = True
                try:
                    self.schedule = get_day_schedule(
                        flatten_by="15:45", on_date=self.now.date(), mode=_active_mode,
                    )
                except Exception:
                    self.schedule_error = "session_calendar_unavailable"
            if self.schedule_error:
                raise StopDataError(self.schedule_error)
            schedule = self.schedule
            if schedule is None:
                result["code"] = "no_regular_session"
                return result
            if (schedule.trading_date != self.now.date()
                    or schedule.opens_at.tzinfo is None or schedule.closes_at.tzinfo is None
                    or schedule.closes_at <= schedule.opens_at):
                raise StopDataError("invalid_session_calendar")
            if not schedule.opens_at <= self.now < schedule.closes_at:
                result["code"] = "outside_regular_session"
                return result
            completed_until = self.now.replace(second=0, microsecond=0)
            count = int((completed_until - schedule.opens_at).total_seconds() // 60)
            result["expected_bars"] = count
            if count <= 0:
                result["code"] = "no_completed_bar_yet"
                return result
            params = {"timeframe": "1Min", "start": schedule.opens_at.isoformat(),
                      "end": (completed_until - timedelta(microseconds=1)).isoformat(),
                      "limit": 1000, "feed": result["feed"], "sort": "asc",
                      "adjustment": "raw"}
            deadline = time.monotonic() + 30
            bars = []
            tokens = set()
            for page in range(10):
                payload = _stop_data_get(
                    f"{DATA_BASE}/stocks/{quote(symbol, safe='')}/bars", params, deadline,
                )
                if (payload.get("symbol") != symbol or not isinstance(payload.get("bars"), list)
                        or "next_page_token" not in payload
                        or payload.get("currency", "USD") != "USD"):
                    raise StopDataError("malformed_response")
                bars.extend(payload["bars"])
                if len(bars) > count:
                    raise StopDataError("unexpected_bar_count")
                token = payload["next_page_token"]
                if token is None:
                    break
                if not isinstance(token, str) or not token or token in tokens:
                    raise StopDataError("invalid_pagination")
                tokens.add(token)
                params = {**params, "page_token": token}
            else:
                raise StopDataError("pagination_limit_exceeded")
            result["bars_received"] = len(bars)
            timestamps = []
            highs, lows = [], []
            for bar in bars:
                if not isinstance(bar, dict):
                    raise StopDataError("malformed_bar")
                try:
                    timestamp = datetime.fromisoformat(bar["t"].replace("Z", "+00:00"))
                    if timestamp.tzinfo is None:
                        raise ValueError
                    timestamp = timestamp.astimezone(MARKET_TZ)
                    values = [bar[k] for k in ("o", "h", "l", "c")]
                    if any(isinstance(v, bool) or not isinstance(v, (int, float))
                           or not math.isfinite(v) or v <= 0 for v in values):
                        raise ValueError
                    o, h, l, c = values
                    if not l <= min(o, c) <= max(o, c) <= h:
                        raise ValueError
                    for key in ("v", "n"):
                        value = bar[key]
                        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                            raise ValueError
                    vw = bar["vw"]
                    if (isinstance(vw, bool) or not isinstance(vw, (int, float))
                            or not math.isfinite(vw) or vw <= 0):
                        raise ValueError
                except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
                    raise StopDataError("malformed_bar") from None
                if (timestamp.date() != self.now.date() or timestamp.second or timestamp.microsecond
                        or not schedule.opens_at <= timestamp < completed_until):
                    raise StopDataError("bar_outside_completed_session")
                if timestamps and timestamp <= timestamps[-1]:
                    raise StopDataError("duplicate_or_unsorted_bar")
                timestamps.append(timestamp)
                highs.append(h)
                lows.append(l)
            if timestamps:
                result["as_of"] = timestamps[-1].isoformat()
            expected = [schedule.opens_at + timedelta(minutes=i) for i in range(count)]
            if timestamps != expected:
                result["code"] = "no_bars" if not timestamps else "incomplete_session_coverage"
                return result
            # Long request/pagination work must not turn a formerly fresh snapshot stale.
            actual_now = datetime.now(MARKET_TZ)
            if (actual_now - self.now > timedelta(seconds=60)
                    or not schedule.opens_at <= actual_now < schedule.closes_at):
                raise StopDataError("snapshot_expired")
            result.update(status="ok", code="complete", high=max(highs), low=min(lows))
        except StopDataError as exc:
            result.update(status="failed", code=str(exc))
        except Exception:
            result.update(status="failed", code="stop_data_unavailable")
        return result


def get_intraday_high(symbol: str) -> float | None:
    """Compatibility helper; monitor itself shares a run-local cache."""
    return SessionExtrema().get(symbol)["high"]


def get_intraday_low(symbol: str) -> float | None:
    return SessionExtrema().get(symbol)["low"]


def get_spy_daily_return() -> float | None:
    """Get SPY's return for today using yfinance."""
    try:
        spy = yf.Ticker("SPY")
        hist = spy.history(period="2d", interval="1d")
        if len(hist) < 2:
            log.warning("Yahoo Finance returned fewer than two daily bars for SPY")
            return None
        prev_close = float(hist["Close"].iloc[-2])
        last_close = float(hist["Close"].iloc[-1])
        return (last_close - prev_close) / prev_close
    except Exception as e:
        log.warning(f"Could not fetch SPY return: {e}")
        return None


# ---------------------------------------------------------------------------
# Stop-loss checks
# ---------------------------------------------------------------------------

def check_hard_stop(position: dict, stop_loss_pct: float) -> tuple[bool, str]:
    """
    Check if position has breached the hard stop from avg_entry_price.

    Returns (triggered, reason_string).
    """
    avg_entry = float(position.get("avg_entry_price", 0))
    current_price = float(position.get("current_price", 0))
    if not all(math.isfinite(v) and v > 0 for v in (avg_entry, current_price)):
        return False, ""

    pct_change = (current_price - avg_entry) / avg_entry
    adverse_move = pct_change if float(position.get("qty", 0) or 0) < 0 else -pct_change
    if adverse_move >= stop_loss_pct:
        return True, (
            f"Hard stop triggered: {adverse_move*100:.1f}% adverse move from entry "
            f"(entry ${avg_entry:.2f}, current ${current_price:.2f}, "
            f"threshold {stop_loss_pct*100:.1f}%)"
        )
    return False, ""


def check_trailing_stop(position: dict, intraday_extreme: float | None, trailing_stop_pct: float) -> tuple[bool, str]:
    """
    Check a long against today's high or a short against today's low.

    Returns (triggered, reason_string).
    """
    if intraday_extreme is None:
        return False, ""

    current_price = float(position.get("current_price", 0))
    if not all(math.isfinite(v) and v > 0 for v in (intraday_extreme, current_price)):
        return False, ""

    is_short = float(position.get("qty", 0) or 0) < 0
    adverse_move = (
        (current_price - intraday_extreme) / intraday_extreme
        if is_short else
        (intraday_extreme - current_price) / intraday_extreme
    )
    if adverse_move >= trailing_stop_pct:
        extreme_label = "low" if is_short else "high"
        return True, (
            f"Trailing stop triggered: {adverse_move*100:.1f}% adverse move from today's {extreme_label} "
            f"(${intraday_extreme:.2f}, current ${current_price:.2f}, "
            f"threshold {trailing_stop_pct*100:.1f}%)"
        )
    return False, ""


# ---------------------------------------------------------------------------
# Main monitor logic
# ---------------------------------------------------------------------------

def run_monitor(dry_run: bool = False, mode: str = None) -> dict:
    """
    Check all positions against stops, sell violators, and print summary.

    Args:
        dry_run: If True, report only — do not execute sells.
        mode:    "swing" or "day". Reads TRADING_MODE env if not specified.

    Returns a result dict with actions taken.
    """
    if mode is None:
        mode = os.environ.get("TRADING_MODE", "swing")

    # Set module-level mode so all API helpers route to the correct account
    global _active_mode
    _active_mode = mode

    mode_config = get_mode_config(mode)
    mode_risk = mode_config["risk"]
    stop_loss_pct = mode_risk["stop_loss_pct"]
    trailing_stop_pct = mode_risk["trailing_stop_pct"]

    log.info(f"Portfolio monitor starting (mode={mode}, dry_run={dry_run}, account={get_account_for_mode(mode).name})")

    # Day liquidation is time-sensitive. Run it before account reporting,
    # benchmark downloads, or intraday-high lookups can delay the close.
    calendar_error = None
    if mode_risk.get("flatten_eod", False):
        try:
            schedule = get_day_schedule(
                flatten_by=mode_risk.get("flatten_by", "15:45"),
                flatten_lead_minutes=mode_risk.get("flatten_lead_minutes", 15),
                entry_cutoff_minutes=mode_risk.get("entry_cutoff_minutes", 15),
            )
            flatten_due = schedule is None or schedule.flatten_due(datetime.now(MARKET_TZ))
        except Exception as exc:
            calendar_error = f"Day market calendar unavailable: {exc}"
            log.error(calendar_error)
            return {
                "timestamp": datetime.now(MARKET_TZ).isoformat(),
                "trading_mode": mode,
                "mode": "dry_run" if dry_run else "live",
                "status": "failed",
                "success": False,
                "eod_flatten": False,
                "eod_flatten_attempted": False,
                "actions": [],
                "warnings": [calendar_error],
            }
        if flatten_due:
            from execute_trades import flatten_all
            try:
                flatten_result = flatten_all(dry_run=dry_run, mode=mode)
            except Exception as exc:
                flatten_result = {"status": "failed", "verified_flat": False, "results": [], "errors": [str(exc)]}
            for error in flatten_result.get("errors", []):
                log.error("Day flatten: %s", error)
            return {
                "timestamp": datetime.now(MARKET_TZ).isoformat(),
                "trading_mode": mode,
                "mode": "dry_run" if dry_run else "live",
                "status": "dry_run" if dry_run else flatten_result.get("status", "failed"),
                "success": bool(flatten_result.get("success")) if not dry_run else True,
                "eod_flatten": bool(flatten_result.get("verified_flat")),
                "eod_flatten_attempted": True,
                "flatten_result": flatten_result,
                "actions": [
                    {**row, "stop_type": "eod_flatten"}
                    for row in flatten_result.get("results", [])
                ],
                "warnings": flatten_result.get("errors", []),
            }

    account = get_account()
    positions = get_positions()

    equity = float(account.get("equity", 0))
    cash = float(account.get("cash", 0))
    last_equity = float(account.get("last_equity", equity))
    daily_pnl = equity - last_equity
    daily_pnl_pct = (daily_pnl / last_equity) if last_equity else 0

    actions = []
    warnings = []
    open_positions = [p for p in positions if float(p.get("qty", 0)) != 0]

    extrema = SessionExtrema()
    stop_data = {}

    # ── Per-position stop checks ─────────────────────────────────────────────
    if calendar_error:
        warnings.append(calendar_error)
    for pos in open_positions:
        symbol = pos["symbol"]
        qty = float(pos["qty"])
        is_short = qty < 0

        data = extrema.get(symbol)
        stop_data[symbol] = data
        prices = (float(pos.get("avg_entry_price", 0)), float(pos.get("current_price", 0)))
        if not math.isfinite(qty) or not all(math.isfinite(v) and v > 0 for v in prices):
            data = {**data, "status": "failed", "code": "invalid_position_price_or_quantity",
                    "high": None, "low": None}
            stop_data[symbol] = data
            warnings.append(f"{symbol}: stop checks unavailable ({data['code']})")
            continue
        intraday_extreme = data["low"] if is_short else data["high"]
        if data["status"] != "ok":
            warnings.append(f"{symbol}: trailing stop check unavailable ({data['status']}: {data['code']})")

        hard_triggered, hard_reason = check_hard_stop(pos, stop_loss_pct)
        if hard_triggered:
            result = place_market_close(symbol, qty, hard_reason, dry_run)
            actions.append({**result, "stop_type": "hard_stop"})
            continue

        trail_triggered, trail_reason = (
            check_trailing_stop(pos, intraday_extreme, trailing_stop_pct)
            if data["status"] == "ok" else (False, "")
        )
        if trail_triggered:
            result = place_market_close(symbol, qty, trail_reason, dry_run)
            actions.append({**result, "stop_type": "trailing_stop"})
            continue

        # Warn if approaching stops (within 20% of threshold)
        avg_entry = float(pos.get("avg_entry_price", 0))
        current_price = float(pos.get("current_price", 0))
        warn_buffer = stop_loss_pct * 0.2  # warn at 80% of stop depth

        if avg_entry > 0 and current_price > 0:
            pct_from_entry = (current_price - avg_entry) / avg_entry
            adverse_move = pct_from_entry if is_short else -pct_from_entry
            if adverse_move >= stop_loss_pct - warn_buffer:
                warnings.append(
                    f"⚠️  {symbol}: approaching hard stop at "
                    f"{adverse_move*100:.1f}% adverse from entry (stop at {stop_loss_pct*100:.1f}%)"
                )

        if intraday_extreme and intraday_extreme > 0 and current_price > 0:
            trail_warn = trailing_stop_pct * 0.2
            trail_adverse_move = (
                (current_price - intraday_extreme) / intraday_extreme
                if is_short else
                (intraday_extreme - current_price) / intraday_extreme
            )
            if trail_adverse_move >= trailing_stop_pct - trail_warn:
                extreme_name = "low" if is_short else "high"
                warnings.append(
                    f"⚠️  {symbol}: approaching trailing stop at "
                    f"{trail_adverse_move*100:.1f}% adverse from today's {extreme_name} "
                    f"(stop at {trailing_stop_pct*100:.1f}%)"
                )

    spy_return = get_spy_daily_return()
    _print_summary(
        equity=equity, cash=cash, daily_pnl=daily_pnl, daily_pnl_pct=daily_pnl_pct,
        spy_return=spy_return, positions=open_positions, actions=actions, warnings=warnings,
        dry_run=dry_run, mode=mode, stop_loss_pct=stop_loss_pct, trailing_stop_pct=trailing_stop_pct,
        stop_data=stop_data,
    )

    action_failed = bool(calendar_error) or any(a.get("status") == "failed" for a in actions)
    data_failed = any(d["status"] != "ok" for d in stop_data.values())
    return {
        "timestamp": datetime.now(MARKET_TZ).isoformat(),
        "trading_mode": mode,
        "mode": "dry_run" if dry_run else "live",
        "equity": equity,
        "daily_pnl_pct": round(daily_pnl_pct * 100, 2),
        "spy_return_pct": round(spy_return * 100, 2) if spy_return is not None else None,
        "positions_checked": len(open_positions),
        "stops_triggered": len(actions),
        "status": "failed" if action_failed else "degraded" if data_failed else "ok",
        "success": not action_failed and not data_failed,
        "exit_code": 2 if action_failed or data_failed else 0,
        "stop_data": stop_data,
        "eod_flatten": False,
        "actions": actions,
        "warnings": warnings,
    }


def _print_summary(
    equity: float,
    cash: float,
    daily_pnl: float,
    daily_pnl_pct: float,
    spy_return: float | None,
    positions: list[dict],
    actions: list[dict],
    warnings: list[str],
    dry_run: bool,
    mode: str,
    stop_loss_pct: float,
    trailing_stop_pct: float,
    stop_data: dict,
) -> None:
    """Pretty-print the monitor summary to stdout."""
    mode_config = get_mode_config(mode)
    now = datetime.now(MARKET_TZ).strftime("%Y-%m-%d %H:%M %Z")

    print(f"{'='*65}")
    mode_tag = " [DRY RUN]" if dry_run else ""
    print(f"  SWARM TRADER — PORTFOLIO MONITOR{mode_tag}")
    print(f"  Mode: {mode.upper()} — {mode_config['label']}")
    print(f"  Stops: -{stop_loss_pct*100:.1f}% hard  /  -{trailing_stop_pct*100:.1f}% trailing")
    print(f"  {now}")
    print(f"{'='*65}")
    print(f"  Equity:     ${equity:>13,.2f}")
    print(f"  Cash:       ${cash:>13,.2f}  ({cash/equity*100:.1f}% of equity)")

    print(f"  Daily P&L:  ${daily_pnl:>+13,.2f}  ({daily_pnl_pct*100:+.2f}%)")

    if spy_return is not None:
        alpha = daily_pnl_pct - spy_return
        beat = "BEATING" if alpha > 0 else "LAGGING"
        print(f"  SPY Today:   {spy_return*100:>+13.2f}%")
        print(f"  Alpha:       {alpha*100:>+13.2f}%  ({beat} SPY)")
    else:
        print(f"  SPY Today:   (unavailable)")

    print(f"  Positions:  {len(positions):>13}")
    print()

    if positions:
        print(f"  {'Symbol':<8} {'Qty':>5} {'Price':>9} {'Entry':>9} {'From Entry':>11} {'Value':>12} {'Intraday Ext.':>13}")
        print(f"  {'-'*71}")
        warn_pct = stop_loss_pct * 0.8  # flag approaching threshold
        for pos in sorted(positions, key=lambda x: float(x.get("market_value", 0)), reverse=True):
            sym = pos["symbol"]
            qty = int(float(pos["qty"]))
            price = float(pos.get("current_price", 0))
            entry = float(pos.get("avg_entry_price", 0))
            value = float(pos.get("market_value", 0))
            plpc = float(pos.get("unrealized_plpc", 0)) * 100
            data = stop_data.get(sym, {})
            intraday_extreme = data.get("low" if float(pos["qty"]) < 0 else "high")
            extreme_str = f"${intraday_extreme:>9.2f}" if intraday_extreme else "     N/A"
            flag = " 🔴" if plpc / 100 <= -stop_loss_pct else (" ⚠️" if plpc / 100 <= -warn_pct else "")
            print(f"  {sym:<8} {qty:>5} ${price:>8.2f} ${entry:>8.2f} {plpc:>+10.1f}% ${value:>10,.0f} {extreme_str}{flag}")
    print()

    if actions:
        print(f"  STOP-LOSS ACTIONS TAKEN ({len(actions)}):")
        for a in actions:
            tag = "[DRY-RUN] " if dry_run else ""
            stop_type = a.get("stop_type", "").upper()
            print(f"    {tag}{stop_type}: {a.get('symbol', a.get('ticker', '?'))} — {str(a.get('reason', ''))[:80]}")
        print()

    if warnings:
        print(f"  APPROACHING STOPS:")
        for w in warnings:
            print(f"    {w}")
        print()

    if not actions and not warnings:
        print("  All positions within stop thresholds.")
        print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def monitor_exit_code(result: dict) -> int:
    """Incomplete protection is a nonzero result even if hard checks succeed."""
    return 0 if result.get("success") else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Swarm Trader Portfolio Monitor")
    parser.add_argument(
        "--dry-run", "--no-sell",
        action="store_true",
        help="Report only — do not execute sells",
    )
    parser.add_argument(
        "--mode",
        choices=["swing", "day"],
        default=None,
        help="Trading mode (overrides TRADING_MODE env, default: swing)",
    )
    args = parser.parse_args()

    mode = args.mode or os.environ.get("TRADING_MODE", "swing")

    try:
        result = run_monitor(dry_run=args.dry_run, mode=mode)
    except Exception as e:
        log.error(f"Monitor failed: {e}", exc_info=True)
        sys.exit(1)
    print(json.dumps(result, indent=2))
    sys.exit(monitor_exit_code(result))
