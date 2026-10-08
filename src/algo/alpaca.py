"""Paper-only Alpaca REST adapter dedicated to the Algo account credentials."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import requests

from src.market_data import regular_feed


PAPER_BASE = "https://paper-api.alpaca.markets/v2"
DATA_BASE = "https://data.alpaca.markets/v2"


class AlgoAccountMismatchError(RuntimeError):
    """Raised when the Algo keys resolve to a different paper account ID."""


class AlgoAlpaca:
    def __init__(self, session: requests.Session | None = None) -> None:
        self.key = os.environ.get("ALPACA_ALGO_API_KEY", "")
        self.secret = os.environ.get("ALPACA_ALGO_API_SECRET", "")
        self.account_id = os.environ.get("ALPACA_ALGO_ACCOUNT_ID", "")
        if not (self.key and self.secret and self.account_id):
            raise RuntimeError("Algo requires ALPACA_ALGO_API_KEY, ALPACA_ALGO_API_SECRET, and ALPACA_ALGO_ACCOUNT_ID")
        self.session = session or requests.Session()
        self.headers = {
            "APCA-API-KEY-ID": self.key,
            "APCA-API-SECRET-KEY": self.secret,
            "Content-Type": "application/json",
        }

    def _get(self, url: str, **kwargs):
        response = self.session.get(url, headers=self.headers, timeout=10, **kwargs)
        response.raise_for_status()
        return response.json()

    def _post(self, url: str, payload: dict):
        response = self.session.post(url, headers=self.headers, json=payload, timeout=10)
        response.raise_for_status()
        return response.json()

    def account(self) -> dict:
        account = self._get(f"{PAPER_BASE}/account")
        configured_id = self.account_id.strip()
        # Alpaca exposes both its UUID `id` and human-facing `account_number`.
        # Accept either representation while still binding keys to the intended
        # account returned by the fixed paper endpoint.
        if configured_id not in {
            str(account.get("id", "")).strip(),
            str(account.get("account_number", "")).strip(),
        }:
            raise AlgoAccountMismatchError("Configured Algo account identifier does not match the Alpaca paper account response")
        if account.get("status") != "ACTIVE" or account.get("trading_blocked"):
            raise RuntimeError("Algo Alpaca paper account is not active for trading")
        return account

    def clock(self) -> dict:
        return self._get(f"{PAPER_BASE}/clock")

    def calendar(self, start: str, end: str) -> list[dict]:
        return self._get(f"{PAPER_BASE}/calendar", params={"start": start, "end": end})

    def previous_close(self, symbol: str, session_day: str) -> float:
        start = (datetime.fromisoformat(session_day) - timedelta(days=12)).date().isoformat()
        bars = self._get(
            f"{DATA_BASE}/stocks/{symbol}/bars",
            params={"timeframe": "1Day", "start": start, "end": session_day, "limit": 20, "feed": regular_feed()},
        ).get("bars", [])
        eligible = [b for b in bars if str(b.get("t", ""))[:10] < session_day]
        if not eligible:
            raise RuntimeError(f"No previous daily close available for {symbol}")
        return float(eligible[-1]["c"])

    def latest_price(self, symbol: str) -> tuple[float, datetime]:
        return self.latest_prices([symbol])[symbol]

    def latest_prices(
        self,
        symbols: list[str],
        *,
        stale_after_seconds: int = 60,
        feed: str | None = None,
    ) -> dict[str, tuple[float, datetime]]:
        quotes = self.latest_quotes(symbols, feed=feed or regular_feed())
        now = datetime.now(timezone.utc)
        return {
            symbol: (quote["price"], quote["timestamp"])
            for symbol, quote in quotes.items()
            if 0 <= (now - quote["timestamp"]).total_seconds() <= stale_after_seconds
        }

    def latest_quotes(self, symbols: list[str], *, feed: str | None = None) -> dict[str, dict]:
        """Return current bid/ask marks from the requested Alpaca data feed."""
        if not symbols:
            return {}
        feed = feed or regular_feed()
        raw = self._get(
            f"{DATA_BASE}/stocks/quotes/latest",
            params={"symbols": ",".join(symbols), "feed": feed},
        ).get("quotes", {})
        output = {}
        for symbol in symbols:
            quote = raw.get(symbol) or {}
            try:
                bid = float(quote.get("bp", 0) or 0)
                ask = float(quote.get("ap", 0) or 0)
                timestamp = datetime.fromisoformat(quote["t"].replace("Z", "+00:00"))
            except (KeyError, TypeError, ValueError):
                continue
            if bid <= 0 or ask < bid:
                continue
            output[symbol] = {
                "price": (bid + ask) / 2,
                "bid": bid,
                "ask": ask,
                "timestamp": timestamp,
            }
        return output

    def positions(self) -> list[dict]:
        return self._get(f"{PAPER_BASE}/positions")

    def orders(self, status: str = "open", *, nested: bool = False, symbols: str | None = None) -> list[dict]:
        params = {"status": status, "limit": 500}
        if nested:
            params["nested"] = "true"
        if symbols:
            params["symbols"] = symbols
        return self._get(f"{PAPER_BASE}/orders", params=params)

    def order_by_client_id(self, client_order_id: str) -> dict | None:
        response = self.session.get(
            f"{PAPER_BASE}/orders:by_client_order_id",
            headers=self.headers,
            params={"client_order_id": client_order_id},
            timeout=10,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def submit_market_order(self, symbol: str, side: str, qty: int, client_order_id: str) -> dict:
        if side not in {"buy", "sell"} or qty <= 0:
            raise ValueError("Invalid Algo order")
        return self._post(f"{PAPER_BASE}/orders", {
            "symbol": symbol,
            "qty": str(qty),
            "side": side,
            "type": "market",
            "time_in_force": "day",
            "client_order_id": client_order_id,
        })

    def submit_limit_order(
        self, symbol: str, side: str, qty: int, limit_price: float,
        client_order_id: str, *, extended_hours: bool,
    ) -> dict:
        if side not in {"buy", "sell"} or qty <= 0 or limit_price <= 0:
            raise ValueError("Invalid Algo limit order")
        return self._post(f"{PAPER_BASE}/orders", {
            "symbol": symbol,
            "qty": str(qty),
            "side": side,
            "type": "limit",
            "time_in_force": "day",
            "limit_price": str(round(float(limit_price), 2)),
            "extended_hours": bool(extended_hours),
            "client_order_id": client_order_id,
        })

    def order(self, order_id: str, *, nested: bool = False) -> dict:
        url = f"{PAPER_BASE}/orders/{order_id}"
        return self._get(url, params={"nested": "true"}) if nested else self._get(url)

    @staticmethod
    def current_utc() -> datetime:
        return datetime.now(timezone.utc)
