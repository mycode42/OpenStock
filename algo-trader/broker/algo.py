"""Paper-only Alpaca REST adapter dedicated to the Algo account credentials."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import requests

from market.data import regular_feed
from market.quotes import QuoteParseError, parse_quote


from broker.endpoints import ALGO_TRADING_BASE as PAPER_BASE
DATA_BASE = "https://data.alpaca.markets/v2"


class AlgoAccountMismatchError(RuntimeError):
    """Raised when the Algo keys resolve to a different paper account ID."""


class AlgoAlpaca:
    def __init__(self, session: requests.Session | None = None) -> None:
        self.key = os.environ.get("ALPACA_EQUITY_API_KEY", "")
        self.secret = os.environ.get("ALPACA_EQUITY_API_SECRET", "")
        self.account_id = os.environ.get("ALPACA_EQUITY_ACCOUNT_ID", "")
        if not (self.key and self.secret and self.account_id):
            raise RuntimeError("Algo requires ALPACA_EQUITY_API_KEY, ALPACA_EQUITY_API_SECRET, and ALPACA_EQUITY_ACCOUNT_ID")
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

    def _patch(self, url: str, payload: dict):
        response = self.session.patch(url, headers=self.headers, json=payload, timeout=10)
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
                parsed = parse_quote(quote)
            except QuoteParseError:
                continue
            output[symbol] = parsed
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

    def orders_after(self, after: str | None, *, status: str = "all", limit: int = 500) -> list[dict]:
        """Oldest-first page of orders submitted after a timestamp, for ledger sync."""
        params: dict = {"status": status, "limit": limit, "direction": "asc"}
        if after:
            params["after"] = after
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

    def order(self, order_id: str, *, nested: bool = False) -> dict:
        url = f"{PAPER_BASE}/orders/{order_id}"
        return self._get(url, params={"nested": "true"}) if nested else self._get(url)

    def cancel_order(self, order_id: str) -> None:
        """Cancel one open order in the Algo Paper account; used only by the protection migration."""
        if not order_id:
            raise ValueError("Algo cancellation requires an order ID")
        response = self.session.delete(f"{PAPER_BASE}/orders/{order_id}", headers=self.headers, timeout=10)
        if response.status_code not in (200, 204):
            response.raise_for_status()

    @staticmethod
    def current_utc() -> datetime:
        return datetime.now(timezone.utc)
