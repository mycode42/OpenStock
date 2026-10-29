"""Alpaca HTTP operations used by the trade executor."""

import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation

import requests

from runtime.http import get_session

from market.data import regular_feed
from portfolio.config import get_algo_execution_settings
from broker.order_guard import check_order
from market.quotes import parse_price_timestamp, parse_quote


from broker.endpoints import ALGO_TRADING_BASE as API_BASE
DATA_BASE = "https://data.alpaca.markets/v2"


def get_account(headers: dict) -> dict:
    response = get_session().get(f"{API_BASE}/account", headers=headers, timeout=10)
    response.raise_for_status()
    return response.json()


def list_positions(headers: dict) -> list[dict]:
    response = get_session().get(f"{API_BASE}/positions", headers=headers, timeout=10)
    response.raise_for_status()
    positions = response.json()
    if not isinstance(positions, list):
        raise ValueError("Alpaca positions response was not a list")
    return positions


def get_positions(headers: dict) -> dict[str, dict]:
    return {position["symbol"]: position for position in list_positions(headers)}


def get_latest_price(ticker: str, headers: dict) -> float:
    """Fetch a recent SIP trade for risk sizing."""
    response = get_session().get(
        f"{DATA_BASE}/stocks/{ticker}/trades/latest",
        headers=headers,
        params={"feed": regular_feed()},
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    trade = payload.get("trade") or (payload.get("trades") or {}).get(ticker) or {}
    price, timestamp = parse_price_timestamp(
        trade.get("p"), trade.get("t"), context=f"Latest Alpaca trade for {ticker}",
    )
    age = (datetime.now(timestamp.tzinfo) - timestamp).total_seconds()
    if age < 0 or age > 90:
        raise ValueError(f"Latest Alpaca trade for {ticker} is stale ({age:.0f}s)")
    return price


def get_latest_quote(ticker: str, side: str, headers: dict, *, feed: str | None = None) -> float:
    """Fetch and validate a fresh ask or bid from the selected market feed."""
    if side not in {"ask", "bid"}:
        raise ValueError("side must be ask or bid")
    response = get_session().get(
        f"{DATA_BASE}/stocks/{ticker}/quotes/latest",
        headers=headers,
        params={"feed": feed or regular_feed()},
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    quote = payload.get("quote") if isinstance(payload, dict) else None
    parsed = parse_quote(quote, side=side, assume_utc=False)
    price, timestamp = parsed["price"], parsed["timestamp"]
    age = (datetime.now(timestamp.tzinfo) - timestamp).total_seconds()
    if age < 0 or age > 90:
        raise ValueError(f"Latest Alpaca {side} for {ticker} is stale ({age:.0f}s)")
    return price


def list_open_orders(headers: dict) -> list[dict]:
    """Read every open order, including pending entries and stop legs."""
    orders: list[dict] = []
    cursor = None
    for _ in range(100):
        params = {"status": "open", "limit": 500, "direction": "asc"}
        if cursor:
            params["after_order_id"] = cursor
        response = get_session().get(f"{API_BASE}/orders", headers=headers, params=params, timeout=10)
        response.raise_for_status()
        page = response.json()
        if not isinstance(page, list):
            raise ValueError("Alpaca open orders response was not a list")
        orders.extend(page)
        if len(page) < 500:
            return orders
        cursor = str(page[-1].get("id") or "")
        if not cursor:
            raise ValueError("Alpaca open order page omitted its cursor")
    raise RuntimeError("Alpaca open order list exceeded 100 pages")


def open_order_ids(orders: list[dict]) -> list[str]:
    ids = []
    for order in orders:
        order_id = str(order.get("id") or "")
        if not order_id:
            raise ValueError("Alpaca open order has no ID")
        ids.append(order_id)
    return ids


def cancel_orders(headers: dict, orders: list[dict]) -> tuple[list[str], list[str]]:
    cancelled, errors = [], []
    for order_id in open_order_ids(orders):
        try:
            response = get_session().delete(f"{API_BASE}/orders/{order_id}", headers=headers, timeout=10)
            # 404/422 may mean the order filled or finished during cancellation.
            # The caller re-reads open orders before deciding it is gone.
            if response.status_code in (200, 202, 204, 404, 422):
                cancelled.append(order_id)
            else:
                errors.append(f"Cancel {order_id}: HTTP {response.status_code}")
        except requests.RequestException as exc:
            errors.append(f"Cancel {order_id}: {exc}")
    return cancelled, errors


def position_qty(position: dict) -> Decimal:
    try:
        quantity = Decimal(str(position.get("qty", "0")))
    except InvalidOperation as exc:
        raise ValueError(f"Invalid position quantity for {position.get('symbol')}") from exc
    if not quantity.is_finite():
        raise ValueError(f"Nonfinite position quantity for {position.get('symbol')}")
    return quantity


def submit_flatten_order(headers: dict, position: dict) -> dict:
    symbol = str(position["symbol"]).upper()
    quantity = position_qty(position)
    if not quantity:
        raise ValueError(f"Cannot flatten zero-quantity {symbol} position")
    client_order_id = f"flat-{uuid.uuid4().hex}"
    # Market closes are not sent. A marketable limit priced off the live quote is.
    band = float(get_algo_execution_settings()["marketable_limit_band_bps"]) / 10_000.0
    if quantity > 0:
        limit_price = round(get_latest_quote(symbol, "bid", headers) * (1 - band), 2)
    else:
        limit_price = round(get_latest_quote(symbol, "ask", headers) * (1 + band), 2)
    order = {
        "symbol": symbol,
        "qty": str(abs(quantity)),
        "side": "sell" if quantity > 0 else "buy",
        "position_intent": "sell_to_close" if quantity > 0 else "buy_to_close",
        "type": "limit",
        "limit_price": str(limit_price),
        "time_in_force": "day",
        "client_order_id": client_order_id,
    }
    check_order(order)
    try:
        response = requests.post(f"{API_BASE}/orders", headers=headers, json=order, timeout=10)
    except requests.RequestException as exc:
        # Do not retry an ambiguous POST: it may have reached Alpaca.
        return {
            "ticker": symbol,
            "status": "failed",
            "client_order_id": client_order_id,
            "error": f"Close submission outcome unknown: {exc}",
        }
    if response.status_code not in (200, 201):
        return {
            "ticker": symbol,
            "status": "failed",
            "client_order_id": client_order_id,
            "error": f"HTTP {response.status_code}: {response.text[:200]}",
        }
    data = response.json()
    if not data.get("id"):
        return {
            "ticker": symbol,
            "status": "failed",
            "client_order_id": client_order_id,
            "error": "Close order response omitted its Alpaca ID",
        }
    return {
        "ticker": symbol,
        "side": order["side"],
        "qty": order["qty"],
        "status": "submitted",
        "submission_status": "submitted",
        "broker_status": data.get("status"),
        "order_id": data["id"],
        "client_order_id": client_order_id,
    }


def get_order_status(headers: dict, order_id: str) -> str:
    response = get_session().get(f"{API_BASE}/orders/{order_id}", headers=headers, timeout=10)
    response.raise_for_status()
    return str(response.json().get("status") or "").lower()


def lookup_client_order(headers: dict, client_order_id: str) -> dict | None:
    response = get_session().get(
        f"{API_BASE}/orders:by_client_order_id",
        headers=headers,
        params={"client_order_id": client_order_id},
        timeout=10,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Alpaca client order lookup returned invalid data")
    return data


def get_nested_order(headers: dict, order_id: str) -> dict:
    response = get_session().get(
        f"{API_BASE}/orders/{order_id}",
        headers=headers,
        params={"nested": "true"},
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or str(data.get("id") or "") != str(order_id):
        raise ValueError("Exact nested Alpaca order lookup returned a different order")
    return data
