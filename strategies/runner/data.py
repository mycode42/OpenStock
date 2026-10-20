"""Alpaca market data access and response normalization for strategy scans."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

from broker.accounts import get_account_for_mode
from market.data import regular_feed
from market.quotes import QuoteParseError, parse_quote

DATA_BASE = "https://data.alpaca.markets/v2"
TRADE_BASE = "https://paper-api.alpaca.markets/v2"


def _request(endpoint: str, *, mode: str, base: str = DATA_BASE, params: dict | None = None):
    response = requests.get(
        f"{base}/{endpoint}",
        headers=get_account_for_mode(mode).headers,
        params=params,
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def fetch_bars(
    symbols: list[str], *, mode: str, timeframe: str, start: str, end: str,
    limit: int, feed: str | None = None, adjustment: str | None = None, request_fn=None,
) -> dict[str, list[dict]]:
    if not symbols:
        return {}
    feed = feed or regular_feed()
    request_fn = request_fn or _request
    output: dict[str, list[dict]] = {}
    for offset in range(0, len(symbols), 50):
        chunk = symbols[offset:offset + 50]
        page_token = None
        seen_tokens: set[str] = set()
        while True:
            params = {
                "symbols": ",".join(chunk),
                "timeframe": timeframe,
                "start": start,
                "end": end,
                "limit": limit,
                "sort": "asc",
                "feed": feed,
            }
            if adjustment:
                params["adjustment"] = adjustment
            if page_token:
                params["page_token"] = page_token
            payload = request_fn("stocks/bars", mode=mode, params=params)
            if not isinstance(payload, dict):
                raise ValueError("Alpaca historical bars response is invalid")
            bars_payload = payload.get("bars") or {}
            if not isinstance(bars_payload, dict):
                raise ValueError("Alpaca historical bars response is invalid")
            for ticker, bars in bars_payload.items():
                if isinstance(bars, list):
                    output.setdefault(ticker, []).extend(bars)
            page_token = payload.get("next_page_token")
            if not page_token:
                break
            if not isinstance(page_token, str) or page_token in seen_tokens:
                raise ValueError("Alpaca historical bars pagination token repeated or invalid")
            seen_tokens.add(page_token)
    return output


def fetch_latest_quotes(
    symbols: list[str], *, mode: str, feed: str | None = None, errors: list[str] | None = None,
    diagnostics: dict[str, dict] | None = None, request_fn=None,
) -> dict[str, dict]:
    if not symbols:
        return {}
    feed = feed or regular_feed()
    request_fn = request_fn or _request
    output: dict[str, dict] = {}
    for offset in range(0, len(symbols), 50):
        chunk = symbols[offset:offset + 50]
        try:
            payload = request_fn(
                "stocks/quotes/latest",
                mode=mode,
                params={"symbols": ",".join(chunk), "feed": feed},
            )
            if not isinstance(payload, dict):
                raise ValueError("Alpaca latest quotes response is invalid")
            quotes = payload.get("quotes") or {}
            if not isinstance(quotes, dict):
                raise ValueError("Alpaca latest quotes response is invalid")
        except (requests.RequestException, ValueError) as exc:
            if errors is not None:
                errors.append(f"latest quotes {feed}: {type(exc).__name__}: {exc}")
            if diagnostics is not None:
                for ticker in chunk:
                    diagnostics[ticker] = {"status": "request_error"}
            continue
        for ticker in chunk:
            quote = quotes.get(ticker)
            if quote is None:
                if diagnostics is not None:
                    diagnostics[ticker] = {"status": "missing"}
                continue
            if not isinstance(quote, dict):
                if diagnostics is not None:
                    diagnostics[ticker] = {"status": "invalid_quote"}
                continue
            try:
                parsed = parse_quote(quote)
            except QuoteParseError as exc:
                if diagnostics is not None:
                    diagnostics[ticker] = {
                        "status": exc.reason,
                    }
                continue
            timestamp = parsed["timestamp"]
            if diagnostics is not None:
                diagnostics[ticker] = {
                    "status": "usable",
                    "quote_timestamp": timestamp.isoformat(),
                }
            previous = output.get(ticker)
            if previous is None or timestamp > previous["timestamp"]:
                output[ticker] = {
                    "price": parsed["price"],
                    "bid": parsed["bid"],
                    "ask": parsed["ask"],
                    "timestamp": timestamp,
                }
    return output


def _completed_daily(bars: list[dict], today: str) -> list[dict]:
    return [bar for bar in bars if str(bar.get("t", ""))[:10] < today]


def _completed_intraday(bars: list[dict], *, now: datetime, minutes: int = 5) -> list[dict]:
    """Drop the currently forming bar so signals only use closed candles."""
    now_utc = now.astimezone(timezone.utc)
    completed = []
    for bar in bars:
        try:
            timestamp = datetime.fromisoformat(str(bar["t"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        if timestamp + timedelta(minutes=minutes) <= now_utc:
            completed.append(bar)
    return completed


def _latest_bar_age_seconds(bars: list[dict], now: datetime) -> float | None:
    if not bars:
        return None
    try:
        timestamp = datetime.fromisoformat(str(bars[-1]["t"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return (now.astimezone(timezone.utc) - timestamp).total_seconds()


def current_portfolio(mode: str, *, request_fn=None) -> tuple[dict, dict[str, dict], list[dict]]:
    request_fn = request_fn or _request
    account = request_fn("account", mode=mode, base=TRADE_BASE)
    positions_raw = request_fn("positions", mode=mode, base=TRADE_BASE)
    open_orders = request_fn("orders", mode=mode, base=TRADE_BASE, params={"status": "open", "limit": 500})
    return account, {position["symbol"]: position for position in positions_raw}, open_orders

