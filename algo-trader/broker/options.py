"""Alpaca options adapter pinned to its paper trading endpoint."""

from __future__ import annotations

from datetime import date
import os
import uuid

import requests
from dotenv import load_dotenv
from market.quotes import parse_quote


load_dotenv()
from broker.endpoints import OPTIONS_TRADING_BASE
from broker.order_guard import check_order
DATA_BASE = "https://data.alpaca.markets"


class OptionsAccountMismatchError(RuntimeError):
    """Options credentials resolve to a different account than configured."""


class OptionsPaperClient:
    """Account-specific option trading and data access; no live endpoint exists."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self.key = os.getenv("ALPACA_OPTIONS_API_KEY", "").strip()
        self.secret = os.getenv("ALPACA_OPTIONS_API_SECRET", "").strip()
        self.account_id = os.getenv("ALPACA_OPTIONS_ACCOUNT_ID", "").strip()
        if not (self.key and self.secret and self.account_id):
            raise RuntimeError(
                "Set ALPACA_OPTIONS_API_KEY, ALPACA_OPTIONS_API_SECRET, and ALPACA_OPTIONS_ACCOUNT_ID"
            )
        algo_id = os.getenv("ALPACA_EQUITY_ACCOUNT_ID", "").strip()
        if algo_id and self.account_id == algo_id:
            raise OptionsAccountMismatchError("OPTIONS account ID must be distinct from the Algo account ID")
        self.session = session or requests.Session()
        self.headers = {
            "APCA-API-KEY-ID": self.key,
            "APCA-API-SECRET-KEY": self.secret,
            "Content-Type": "application/json",
        }

    def _get(self, base: str, path: str, *, params: dict | None = None):
        response = self.session.get(f"{base}/{path.lstrip('/')}", headers=self.headers, params=params, timeout=15)
        response.raise_for_status()
        return response.json()

    def verify_account(self) -> dict:
        account = self._get(OPTIONS_TRADING_BASE, "account")
        ids = {str(account.get(key, "")).strip() for key in ("id", "account_number") if account.get(key)}
        if self.account_id not in ids:
            raise OptionsAccountMismatchError("OPTIONS credentials do not resolve to ALPACA_OPTIONS_ACCOUNT_ID")
        if str(account.get("status", "")).upper() != "ACTIVE" or account.get("trading_blocked"):
            raise RuntimeError("Options Alpaca paper account is not active and unblocked")
        return account

    def list_contracts(
        self,
        underlying: str,
        *,
        expiration_date_gte: str,
        expiration_date_lte: str,
        style: str = "american",
    ) -> list[dict]:
        output = []
        token = None
        for _ in range(30):
            params = {
                "underlying_symbols": underlying.upper(),
                "expiration_date_gte": expiration_date_gte,
                "expiration_date_lte": expiration_date_lte,
                "status": "active",
                "style": style,
                "limit": 10000,
            }
            if token:
                params["page_token"] = token
            payload = self._get(OPTIONS_TRADING_BASE, "options/contracts", params=params)
            rows = payload.get("option_contracts", []) if isinstance(payload, dict) else payload
            if not isinstance(rows, list):
                raise ValueError("Alpaca option contracts response is malformed")
            output.extend(row for row in rows if isinstance(row, dict))
            token = payload.get("next_page_token") if isinstance(payload, dict) else None
            if not token:
                return output
        raise RuntimeError("Alpaca option contract pagination exceeded 30 pages")

    def contract(self, symbol: str) -> dict:
        return self._get(OPTIONS_TRADING_BASE, f"options/contracts/{symbol.upper()}")

    def option_snapshots(self, symbols: list[str], *, feed: str) -> dict[str, dict]:
        if not symbols:
            return {}
        if len(symbols) > 100:
            raise ValueError("Alpaca option snapshots accept at most 100 symbols per request")
        payload = self._get(
            DATA_BASE,
            "v1beta1/options/snapshots",
            params={"symbols": ",".join(symbols), "feed": feed},
        )
        rows = payload.get("snapshots", {}) if isinstance(payload, dict) else {}
        if not isinstance(rows, dict):
            raise ValueError("Alpaca option snapshots response is malformed")
        return rows

    def underlying_price(self, symbol: str, *, max_age_seconds: int = 120) -> float:
        payload = self._get(
            DATA_BASE, f"v2/stocks/{symbol.upper()}/quotes/latest",
            params={"feed": "sip"},
        )
        quote = payload.get("quote") or {}
        parsed = parse_quote(quote)
        from datetime import datetime, timezone
        age = (datetime.now(timezone.utc) - parsed["timestamp"]).total_seconds()
        if age < 0 or age > max_age_seconds:
            raise ValueError(f"underlying quote for {symbol} is stale ({age:.0f}s)")
        if parsed["bid"] <= 0 or parsed["ask"] < parsed["bid"]:
            raise ValueError(f"no valid underlying quote for {symbol}")
        return float(parsed["price"])

    def stock_bars(
        self, symbol: str, *, start: date, end: date,
        limit: int = 100, adjustment: str = "raw", feed: str = "sip",
    ) -> list[dict]:
        if not 1 <= int(limit) <= 10000:
            raise ValueError("stock bar page limit must be between 1 and 10000")
        params = {
            "timeframe": "1Day", "start": start.isoformat(), "end": end.isoformat(),
            "limit": int(limit), "feed": feed, "sort": "asc",
        }
        if adjustment != "raw":
            params["adjustment"] = adjustment
        output = []
        page_token = None
        seen_tokens = set()
        for _ in range(1000):
            request_params = dict(params)
            if page_token:
                request_params["page_token"] = page_token
            payload = self._get(
                DATA_BASE,
                f"v2/stocks/{symbol.upper()}/bars",
                params=request_params,
            )
            bars = payload.get("bars", []) if isinstance(payload, dict) else payload
            if not isinstance(bars, list):
                raise ValueError("Alpaca stock bars response is malformed")
            output.extend(row for row in bars if isinstance(row, dict))
            next_token = payload.get("next_page_token") if isinstance(payload, dict) else None
            if not next_token:
                return output
            next_token = str(next_token)
            if next_token in seen_tokens:
                raise RuntimeError("Alpaca stock bars pagination repeated a page token")
            seen_tokens.add(next_token)
            page_token = next_token
        raise RuntimeError("Alpaca stock bars pagination exceeded 1000 pages")

    def market_calendar(self, on_date: date) -> dict | None:
        """Return the paper account's regular-session schedule for one date."""
        rows = self._get(
            OPTIONS_TRADING_BASE, "calendar",
            params={"start": on_date.isoformat(), "end": on_date.isoformat()},
        )
        if not isinstance(rows, list):
            raise ValueError("Alpaca market calendar response is malformed")
        if not rows:
            return None
        if len(rows) != 1 or str(rows[0].get("date")) != on_date.isoformat():
            raise ValueError("Alpaca market calendar returned an unexpected session")
        return rows[0]

    def positions(self) -> list[dict]:
        result = self._get(OPTIONS_TRADING_BASE, "positions")
        if not isinstance(result, list):
            raise ValueError("Alpaca options positions response is malformed")
        return result

    def orders(self, status: str = "open") -> list[dict]:
        result = self._get(OPTIONS_TRADING_BASE, "orders", params={"status": status, "asset_class": "us_option", "nested": "true", "limit": 500})
        if not isinstance(result, list):
            raise ValueError("Alpaca options orders response is malformed")
        return result

    def order_by_client_id(self, client_order_id: str) -> dict | None:
        response = self.session.get(
            f"{OPTIONS_TRADING_BASE}/orders:by_client_order_id",
            headers=self.headers,
            params={"client_order_id": client_order_id},
            timeout=15,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def submit_order(self, payload: dict) -> dict:
        """Submit only to paper; lookup idempotency key after ambiguous transport errors."""
        body = dict(payload)
        check_order(body, multiplier=100.0)
        body.setdefault("client_order_id", f"options-{uuid.uuid4().hex[:24]}")
        try:
            response = self.session.post(
                f"{OPTIONS_TRADING_BASE}/orders", headers=self.headers, json=body, timeout=15,
            )
        except requests.RequestException as exc:
            try:
                existing = self.order_by_client_id(body["client_order_id"])
            except requests.RequestException as lookup_exc:
                raise RuntimeError(f"order submission outcome unknown; lookup failed: {lookup_exc}") from exc
            if existing is not None:
                return existing
            raise RuntimeError(f"order submission outcome unknown; no order found: {exc}") from exc
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or not result.get("id"):
            raise ValueError("Alpaca options order response omitted its broker order ID")
        return result

    def recent_option_activities(self, *, after: str) -> list[dict]:
        """Paper exercise/assignment NTAs can appear the next day; poll on every run."""
        payload = self._get(
            OPTIONS_TRADING_BASE,
            "account/activities",
            params={"activity_types": "OPASN,OPEXP,OPEXC", "after": after},
        )
        if not isinstance(payload, list):
            return []
        return payload


__all__ = ["OptionsAccountMismatchError", "OptionsPaperClient", "OPTIONS_TRADING_BASE", "DATA_BASE"]
