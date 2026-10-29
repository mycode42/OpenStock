"""Broker-backed account snapshots and order exposure calculations."""

from __future__ import annotations

import logging
import math

from portfolio.config import is_cash_proxy
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from urllib.parse import quote
from zoneinfo import ZoneInfo

from portfolio.config import get_mode_config

log = logging.getLogger("execution.risk")
MARKET_TZ = ZoneInfo("America/New_York")
ApiRequest = Callable[..., dict | list]


def _algo_session_bounds(session_date: str) -> tuple[datetime, datetime]:
    """The Algo trade date runs from the prior 20:00 ET to 20:00 ET."""
    trade_day = date.fromisoformat(session_date)
    start_day = trade_day - timedelta(days=1)
    return (
        datetime(start_day.year, start_day.month, start_day.day, 20, tzinfo=MARKET_TZ),
        datetime(trade_day.year, trade_day.month, trade_day.day, 20, tzinfo=MARKET_TZ),
    )


def _is_in_algo_session(timestamp_value: str, session_date: str) -> bool:
    try:
        timestamp = datetime.fromisoformat(str(timestamp_value).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        start, end = _algo_session_bounds(session_date)
        return start <= timestamp.astimezone(MARKET_TZ) < end
    except (TypeError, ValueError):
        return False


def _get_algo_orders_since(session_start: str, mode: str, *, api: ApiRequest) -> list[dict]:
    """Fetch Algo orders since the ET session start using broker cursors."""
    orders: list[dict] = []
    after_order_id = None
    for _ in range(100):
        if after_order_id:
            endpoint = (
                "orders?status=all&limit=500&direction=asc&after_order_id="
                f"{quote(after_order_id, safe='')}"
            )
        else:
            endpoint = f"orders?status=all&after={session_start}&limit=500&direction=asc"
        page = api(endpoint, mode=mode)
        if not isinstance(page, list):
            raise ValueError("Algo order response was not a list")
        orders.extend(item for item in page if isinstance(item, dict))
        if len(page) < 500:
            return orders
        after_order_id = str(page[-1].get("id") or "")
        if not after_order_id:
            raise ValueError("Algo order pagination omitted its last order ID")
    raise RuntimeError("Algo order pagination exceeded 100 pages")


def _count_algo_new_entries_today(
    orders: list[dict],
    fills: list[dict],
    positions: dict[str, dict],
    session_date: str,
) -> int:
    """Count today's filled or pending long and short openings using signed qty."""
    fills_by_order: dict[str, float] = {}
    today_fills: list[tuple[str, datetime, str, float, str]] = []
    filled_delta: dict[str, float] = {}

    for fill in fills:
        ticker = str(fill.get("symbol", "")).upper()
        side = str(fill.get("side", "")).lower()
        if not ticker or side not in {"buy", "sell"}:
            continue
        raw_timestamp = fill.get("transaction_time") or fill.get("date")
        if not raw_timestamp:
            continue
        try:
            timestamp = datetime.fromisoformat(str(raw_timestamp).replace("Z", "+00:00"))
            qty = max(0.0, float(fill.get("qty", fill.get("quantity", 0)) or 0))
        except (TypeError, ValueError):
            continue
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        if not _is_in_algo_session(raw_timestamp, session_date):
            continue
        if qty <= 0:
            continue
        order_id = str(fill.get("order_id") or fill.get("id") or "")
        today_fills.append((ticker, timestamp, side, qty, order_id))
        if order_id:
            fills_by_order[order_id] = fills_by_order.get(order_id, 0.0) + qty
        signed_qty = qty if side == "buy" else -qty
        filled_delta[ticker] = filled_delta.get(ticker, 0.0) + signed_qty

    today_order_ids: set[str] = set()
    events_by_ticker: dict[str, list[tuple[datetime, str, float, bool, str]]] = {}

    for order in orders:
        ticker = str(order.get("symbol", "")).upper()
        side = str(order.get("side", "")).lower()
        submitted_at = order.get("submitted_at") or order.get("created_at")
        if not ticker or side not in {"buy", "sell"} or not submitted_at:
            continue
        try:
            timestamp = datetime.fromisoformat(str(submitted_at).replace("Z", "+00:00"))
            order_qty = max(0.0, float(order.get("qty", 0) or 0))
            reported_filled = max(0.0, float(order.get("filled_qty", 0) or 0))
        except (TypeError, ValueError):
            continue
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        if not _is_in_algo_session(submitted_at, session_date):
            continue

        order_id = str(order.get("id") or "")
        status = str(order.get("status", "")).lower()
        pending = status not in {
            "canceled", "expired", "rejected", "filled", "done_for_day",
            "replaced", "stopped", "suspended", "calculated",
        }
        filled_qty = max(fills_by_order.get(order_id, 0.0), reported_filled)
        if filled_qty <= 0 and not pending:
            continue
        missing_fill_qty = max(0.0, reported_filled - fills_by_order.get(order_id, 0.0))
        if missing_fill_qty:
            delta = missing_fill_qty if side == "buy" else -missing_fill_qty
            filled_delta[ticker] = filled_delta.get(ticker, 0.0) + delta
        projected_qty = max(filled_qty, order_qty if pending else filled_qty)
        events_by_ticker.setdefault(ticker, []).append((timestamp, side, projected_qty, True, order_id))
        if order_id:
            today_order_ids.add(order_id)

    # Prior-session orders (including protective bracket legs) can fill today.
    for ticker, timestamp, side, qty, order_id in today_fills:
        if order_id and order_id in today_order_ids:
            continue
        events_by_ticker.setdefault(ticker, []).append((timestamp, side, qty, False, order_id))

    entries = 0
    for ticker, events in events_by_ticker.items():
        current_qty = float((positions.get(ticker) or {}).get("qty", 0) or 0)
        running_qty = current_qty - filled_delta.get(ticker, 0.0)
        for _, side, qty, countable, order_id in sorted(events, key=lambda event: event[0]):
            if countable and abs(running_qty) < 1e-9:
                entries += 1
            running_qty += qty if side == "buy" else -qty
    return entries


def _pending_short_exposure(open_orders: list[dict], positions: dict[str, dict]) -> dict[str, float]:
    """Reserve remaining opening sell-short orders by ticker."""
    exposure: dict[str, float] = {}
    terminal = {"canceled", "expired", "rejected", "filled", "done_for_day", "replaced", "stopped"}
    for order in open_orders:
        if str(order.get("side", "")).lower() != "sell":
            continue
        symbol = str(order.get("symbol") or "").upper()
        if not symbol:
            raise ValueError("Open sell order has no symbol")
        intent = str(order.get("position_intent") or "").lower()
        position_qty = float((positions.get(symbol) or {}).get("qty", 0) or 0)
        if intent == "sell_to_close" or (not intent and position_qty > 0):
            continue
        if str(order.get("status", "")).lower() in terminal:
            continue
        quantity = max(0.0, float(order.get("qty") or 0) - float(order.get("filled_qty") or 0))
        if quantity <= 0:
            continue
        prices = [
            float(value) for value in (
                order.get("limit_price"), order.get("filled_avg_price"),
                (positions.get(symbol) or {}).get("current_price"),
            ) if value not in (None, "") and float(value) > 0
        ]
        if not prices:
            raise ValueError(f"Open short order for {symbol} has no usable price")
        exposure[symbol] = exposure.get(symbol, 0.0) + quantity * max(prices) * 1.02
    return exposure


def _easy_to_borrow_symbols(mode: str, *, api: ApiRequest) -> set[str]:
    """Read Alpaca's current easy-to-borrow symbols; unknown/HTB names fail closed."""
    assets = api("assets?status=active&asset_class=us_equity", mode=mode)
    if not isinstance(assets, list):
        raise ValueError("Alpaca asset response was not a list")
    return {
        str(asset.get("symbol", "")).upper()
        for asset in assets
        if isinstance(asset, dict)
        and asset.get("symbol")
        and asset.get("tradable") is True
        and asset.get("shortable") is True
        and str(asset.get("borrow_status", "")).lower() == "easy_to_borrow"
    }


def _get_algo_fill_activities(session_start: str, mode: str, *, api: ApiRequest) -> list[dict]:
    """Fetch every Algo fill activity since the current New York session began."""
    activities: list[dict] = []
    page_token = None
    for _ in range(100):
        endpoint = (
            f"account/activities/FILL?after={session_start}"
            "&direction=asc&page_size=100"
        )
        if page_token:
            endpoint += f"&page_token={quote(page_token, safe='')}"
        page = api(endpoint, mode=mode)
        if not isinstance(page, list):
            raise ValueError("Algo fill activity response was not a list")
        activities.extend(item for item in page if isinstance(item, dict))
        if len(page) < 100:
            return activities
        page_token = str(page[-1].get("id") or "")
        if not page_token:
            raise ValueError("Algo fill activity pagination omitted its next page ID")
    raise RuntimeError("Algo fill activity pagination exceeded 100 pages")


def _get_open_orders(mode: str, *, api: ApiRequest) -> list[dict]:
    """Read all open orders, including orders submitted on an earlier date."""
    orders: list[dict] = []
    after_order_id = None
    for _ in range(100):
        endpoint = "orders?status=open&limit=500&direction=asc"
        if after_order_id:
            endpoint += f"&after_order_id={quote(after_order_id, safe='')}"
        page = api(endpoint, mode=mode)
        if not isinstance(page, list):
            raise ValueError("Open order response was not a list")
        orders.extend(item for item in page if isinstance(item, dict))
        if len(page) < 500:
            return orders
        after_order_id = str(page[-1].get("id") or "")
        if not after_order_id:
            raise ValueError("Open order pagination omitted its next page ID")
    raise RuntimeError("Open order pagination exceeded 100 pages")


def _pending_buy_exposure(open_orders: list[dict], positions: dict[str, dict]) -> dict[str, float]:
    """Reserve the still-unfilled value of every open opening buy order.

    Buy-to-cover orders (including the protective stop/target legs of a short)
    are skipped: they reduce a short whose exposure is already counted in
    positions, so reserving them again would double count it.

    For a market buy without a usable broker price, the amount is unknown; the
    caller must block new entries rather than assume that the order costs zero.
    """
    exposure: dict[str, float] = {}
    for order in open_orders:
        if str(order.get("side", "")).lower() != "buy":
            continue
        symbol = str(order.get("symbol") or "").upper()
        if not symbol:
            raise ValueError("Open buy order has no symbol")
        intent = str(order.get("position_intent") or "").lower()
        position_qty = float((positions.get(symbol) or {}).get("qty", 0) or 0)
        if intent == "buy_to_close" or (not intent and position_qty < 0 and (
            order.get("parent_order_id") or str(order.get("order_class") or "").lower() in {"bracket", "oco"}
        )):
            continue
        filled_qty = max(0.0, float(order.get("filled_qty") or 0))
        raw_qty = order.get("qty")
        if raw_qty not in (None, ""):
            remaining_qty = max(0.0, float(raw_qty) - filled_qty)
            if remaining_qty == 0:
                continue
            prices = [
                float(value)
                for value in (
                    order.get("limit_price"), order.get("stop_price"),
                    order.get("filled_avg_price"),
                    (positions.get(symbol) or {}).get("current_price"),
                )
                if value not in (None, "") and float(value) > 0
            ]
            if not prices:
                raise ValueError(f"Open market buy for {symbol} has no usable price")
            value = remaining_qty * max(prices) * 1.02
        else:
            notional = float(order.get("notional") or 0)
            if notional <= 0:
                raise ValueError(f"Open buy for {symbol} has neither quantity nor notional")
            fill_price = float(order.get("filled_avg_price") or 0)
            value = max(0.0, notional - filled_qty * fill_price) * 1.02
        if not math.isfinite(value):
            raise ValueError(f"Open buy for {symbol} has nonfinite exposure")
        exposure[symbol] = exposure.get(symbol, 0.0) + value
    return exposure


def get_portfolio_state(
    mode: str = None,
    *,
    api: ApiRequest,
    get_universe_maps: Callable[[str], tuple[dict, dict, set, dict]],
    verify_account: Callable[[str], tuple[object, dict]],
    get_algo_orders_since: Callable[..., list[dict]],
    get_algo_fill_activities: Callable[..., list[dict]],
    get_open_orders: Callable[[str], list[dict]],
    fetch_easy_to_borrow_symbols: Callable[[str], set[str]],
    find_weekly_equity_baseline: Callable[..., dict | None],
    estimate_weekly_pnl: Callable[..., float | None],
) -> dict:
    """
    Fetch live portfolio state from Alpaca.

    Args:
        mode: Algo account route; defaults to Algo.

    Returns a dict with:
      equity and its daily/weekly dollar baselines and P&L, cash, cash_pct,
      daily_pnl_pct, weekly_pnl_pct,
      positions (symbol → position dict),
      sector_alloc (sector → $ value),
      trade_count_today (all nonterminal long and short entry orders),
      entry_count_today (new-position entries counted in the Algo session),
      open_position_count.
    """
    mode = mode or "algo"
    risk = get_mode_config(mode)["risk"]
    ticker_sector, _, _, _ = get_universe_maps(mode)

    # A risk snapshot must belong to the configured, active paper account.
    # The executor verifies too, but standalone status and strategy callers
    # must not evaluate a closed or replaced account.
    _, account = verify_account(mode)
    all_positions = api("positions", mode=mode)
    # The cash reserve ETF is cash in all but name: its value counts as free cash,
    # and it is excluded from exposure, sector, and position limits.
    reserve_value = sum(
        float(p.get("market_value", 0) or 0) for p in all_positions if is_cash_proxy(p.get("symbol"))
    )
    positions_raw = [p for p in all_positions if not is_cash_proxy(p.get("symbol"))]

    equity = float(account.get("equity", 0))
    cash = float(account.get("cash", 0)) + reserve_value
    last_equity = float(account.get("last_equity", equity))

    daily_pnl_pct = ((equity - last_equity) / last_equity) if last_equity else 0.0
    daily_baseline_equity = (
        last_equity if math.isfinite(last_equity) and last_equity > 0 else None
    )
    daily_pnl_dollars = (
        equity - daily_baseline_equity if daily_baseline_equity is not None else None
    )
    cash_pct = (cash / equity) if equity else 0.0

    positions: dict[str, dict] = {}
    sector_alloc: dict[str, float] = {}

    for p in positions_raw:
        sym = p["symbol"]
        market_value = float(p.get("market_value", 0))
        positions[sym] = {
            "symbol": sym,
            "qty": float(p.get("qty", 0)),
            "market_value": market_value,
            "current_price": float(p.get("current_price", 0)),
            "avg_entry_price": float(p.get("avg_entry_price", 0)),
            "unrealized_plpc": float(p.get("unrealized_plpc", 0)),
            "pct_of_equity": (market_value / equity) if equity else 0,
        }
        sector = ticker_sector.get(sym, "other")
        sector_alloc[sector] = sector_alloc.get(sector, 0) + abs(market_value)

    now_et = datetime.now(MARKET_TZ)
    session_day = now_et.date() + timedelta(days=1) if now_et.hour >= 20 else now_et.date()
    session_date = session_day.isoformat()
    session_start_et, _ = _algo_session_bounds(session_date)
    session_start = session_start_et.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    entry_count_today = None
    entry_count_available = True
    entry_count_error = None
    try:
        orders = get_algo_orders_since(session_start, mode=mode)
        entry_orders_today = [
            o for o in orders
            if str(o.get("side", "")).lower() in {"buy", "sell"}
            and (
                _is_in_algo_session(o.get("submitted_at", ""), session_date)
            )
            and (
                o.get("status") not in ("canceled", "expired", "rejected")
                or float(o.get("filled_qty") or 0) > 0
            )
            and (
                str(o.get("side", "")).lower() == "buy"
                or str(o.get("position_intent", "")).lower() == "sell_to_open"
                or (
                    str(o.get("position_intent", "")).lower() not in {"sell_to_close", "buy_to_close"}
                    and float((positions.get(str(o.get("symbol") or "").upper()) or {}).get("qty", 0) or 0) <= 0
                )
            )
        ]
        trade_count_today = len(entry_orders_today)
        fills = get_algo_fill_activities(session_start, mode=mode)
        entry_count_today = _count_algo_new_entries_today(
            orders, fills, positions, session_date,
        )
        trade_count_today = entry_count_today
    except Exception as exc:
        log.warning("Could not read %s order history: %s", mode, exc)
        trade_count_today = None
        entry_count_today = None
        entry_count_available = False
        entry_count_error = f"Could not read today's {mode} order history"

    pending_buy_value_by_ticker: dict[str, float] = {}
    pending_short_value_by_ticker: dict[str, float] = {}
    pending_exposure_available = True
    pending_exposure_error = None
    easy_to_borrow_symbols: set[str] = set()
    shortability_available = False
    try:
        open_orders = get_open_orders(mode)
        pending_buy_value_by_ticker = _pending_buy_exposure(
            open_orders, positions,
        )
        pending_short_value_by_ticker = _pending_short_exposure(open_orders, positions)
        for symbol, value in pending_buy_value_by_ticker.items():
            sector = ticker_sector.get(symbol, "other")
            sector_alloc[sector] = sector_alloc.get(sector, 0.0) + value
        for symbol, value in pending_short_value_by_ticker.items():
            sector = ticker_sector.get(symbol, "other")
            sector_alloc[sector] = sector_alloc.get(sector, 0.0) + value
    except Exception as exc:
        log.warning("Could not value %s pending entry orders: %s", mode, exc)
        pending_exposure_available = False
        pending_exposure_error = f"Could not value {mode} open buy/short orders"

    try:
        easy_to_borrow_symbols = fetch_easy_to_borrow_symbols(mode)
        shortability_available = True
    except Exception as exc:
        log.warning("Could not verify %s shortability: %s", mode, exc)

    pending_buy_reservation = sum(pending_buy_value_by_ticker.values())
    available_cash = cash - pending_buy_reservation
    pending_new_symbols = {
        symbol for symbol in pending_buy_value_by_ticker
        if float((positions.get(symbol) or {}).get("qty", 0)) == 0
    }
    pending_new_symbols.update(
        symbol for symbol in pending_short_value_by_ticker
        if float((positions.get(symbol) or {}).get("qty", 0)) == 0
    )

    broker_account_id = str(account.get("id") or account.get("account_number") or "")
    weekly_baseline = find_weekly_equity_baseline(
        mode=mode, account_id=broker_account_id, session_day=session_day,
    )
    weekly_baseline_equity = weekly_baseline["equity"] if weekly_baseline else None
    weekly_pnl_pct = estimate_weekly_pnl(
        equity, mode=mode, account_id=broker_account_id, session_day=session_day,
    )
    weekly_pnl_dollars = (
        equity - weekly_baseline_equity
        if weekly_baseline_equity is not None else None
    )

    return {
        "account_id": broker_account_id,
        "account_shorting_enabled": bool(account.get("shorting_enabled", False)),
        "equity": equity,
        "daily_baseline_equity": daily_baseline_equity,
        "daily_pnl_dollars": daily_pnl_dollars,
        "cash": cash,
        "cash_pct": cash_pct,
        "available_cash": available_cash,
        "available_cash_pct": available_cash / equity if equity else 0.0,
        "pending_buy_reservation": pending_buy_reservation,
        "pending_buy_value_by_ticker": pending_buy_value_by_ticker,
        "pending_short_value_by_ticker": pending_short_value_by_ticker,
        "pending_new_symbols": sorted(pending_new_symbols),
        "pending_exposure_available": pending_exposure_available,
        "pending_exposure_error": pending_exposure_error,
        "daily_pnl_pct": daily_pnl_pct,
        "weekly_pnl_pct": weekly_pnl_pct,
        "weekly_baseline_equity": weekly_baseline_equity,
        "weekly_baseline_date": weekly_baseline["date"] if weekly_baseline else None,
        "weekly_baseline_kind": weekly_baseline["kind"] if weekly_baseline else None,
        "weekly_pnl_dollars": weekly_pnl_dollars,
        "positions": positions,
        "sector_alloc": sector_alloc,
        "shortability_available": shortability_available,
        "easy_to_borrow_symbols": sorted(easy_to_borrow_symbols),
        "trade_count_today": trade_count_today,
        "entry_count_today": entry_count_today,
        "entry_count_available": entry_count_available,
        "entry_count_error": entry_count_error,
        "open_position_count": len([p for p in positions.values() if p["qty"] != 0]) + len(pending_new_symbols),
    }
