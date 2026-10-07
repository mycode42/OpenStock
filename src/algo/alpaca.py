"""Paper-only Alpaca REST adapter dedicated to ALPACA_DIP_* credentials."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import requests


PAPER_BASE = "https://paper-api.alpaca.markets/v2"
DATA_BASE = "https://data.alpaca.markets/v2"


class DipAccountMismatchError(RuntimeError):
    """Raised when the Dip keys resolve to a different paper account ID."""


class DipAlpaca:
    def __init__(self, session: requests.Session | None = None) -> None:
        self.key = os.environ.get("ALPACA_DIP_API_KEY", "")
        self.secret = os.environ.get("ALPACA_DIP_API_SECRET", "")
        self.account_id = os.environ.get("ALPACA_DIP_ACCOUNT_ID", "")
        if not (self.key and self.secret and self.account_id):
            raise RuntimeError("Dip requires ALPACA_DIP_API_KEY, ALPACA_DIP_API_SECRET, and ALPACA_DIP_ACCOUNT_ID")
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
            raise DipAccountMismatchError("Configured Dip account identifier does not match the Alpaca paper account response")
        if account.get("status") != "ACTIVE" or account.get("trading_blocked"):
            raise RuntimeError("Dip Alpaca paper account is not active for trading")
        return account

    def clock(self) -> dict:
        return self._get(f"{PAPER_BASE}/clock")

    def calendar(self, start: str, end: str) -> list[dict]:
        return self._get(f"{PAPER_BASE}/calendar", params={"start": start, "end": end})

    def previous_close(self, symbol: str, session_day: str) -> float:
        start = (datetime.fromisoformat(session_day) - timedelta(days=12)).date().isoformat()
        bars = self._get(
            f"{DATA_BASE}/stocks/{symbol}/bars",
            params={"timeframe": "1Day", "start": start, "end": session_day, "limit": 20, "feed": "iex"},
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
    ) -> dict[str, tuple[float, datetime]]:
        trades = self._get(
            f"{DATA_BASE}/stocks/trades/latest",
            params={"symbols": ",".join(symbols), "feed": "iex"},
        ).get("trades", {})
        prices = {}
        now = datetime.now(timezone.utc)
        quote_fallback_symbols = []
        for symbol in symbols:
            trade = trades.get(symbol) or {}
            if not trade:
                quote_fallback_symbols.append(symbol)
                continue
            timestamp = datetime.fromisoformat(trade["t"].replace("Z", "+00:00"))
            price = float(trade["p"])
            if price <= 0:
                quote_fallback_symbols.append(symbol)
                continue
            prices[symbol] = (price, timestamp)
            age = (now - timestamp).total_seconds()
            if age < 0 or age > stale_after_seconds:
                quote_fallback_symbols.append(symbol)

        if quote_fallback_symbols:
            # IEX's latest-trade endpoint excludes some prints (including odd
            # lots). A current quote can still provide a usable mark in that
            # case, but the daemon's normal freshness check remains in force.
            try:
                quotes = self._get(
                    f"{DATA_BASE}/stocks/quotes/latest",
                    params={"symbols": ",".join(quote_fallback_symbols), "feed": "iex"},
                ).get("quotes", {})
            except Exception:
                # Keep any trade values so fresh symbols can still be handled;
                # stale values will be rejected by the daemon as usual.
                quotes = {}

            for symbol in quote_fallback_symbols:
                quote = quotes.get(symbol) or {}
                try:
                    bid = float(quote.get("bp", 0) or 0)
                    ask = float(quote.get("ap", 0) or 0)
                    timestamp = datetime.fromisoformat(quote["t"].replace("Z", "+00:00"))
                except (KeyError, TypeError, ValueError):
                    continue
                if bid <= 0 or ask < bid:
                    continue
                previous = prices.get(symbol)
                if previous is None or timestamp > previous[1]:
                    prices[symbol] = ((bid + ask) / 2, timestamp)
        return prices

    def positions(self) -> list[dict]:
        return self._get(f"{PAPER_BASE}/positions")

    def orders(self, status: str = "open") -> list[dict]:
        return self._get(f"{PAPER_BASE}/orders", params={"status": status, "limit": 500})

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
            raise ValueError("Invalid Dip order")
        return self._post(f"{PAPER_BASE}/orders", {
            "symbol": symbol,
            "qty": str(qty),
            "side": side,
            "type": "market",
            "time_in_force": "day",
            "client_order_id": client_order_id,
        })

    def order(self, order_id: str) -> dict:
        return self._get(f"{PAPER_BASE}/orders/{order_id}")

    @staticmethod
    def current_utc() -> datetime:
        return datetime.now(timezone.utc)
