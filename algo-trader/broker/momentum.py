"""Read-only Alpaca client for the separate Momentum paper account."""

from __future__ import annotations

import os

import requests


PAPER_TRADING_BASE = "https://paper-api.alpaca.markets/v2"


class MomentumAccountMismatchError(RuntimeError):
    """Raised when the Momentum credentials do not resolve to the configured account."""


class MomentumPaperClient:
    """Account snapshots for the dedicated Momentum paper account; no order methods are exposed."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self.key = os.getenv("ALPACA_MOMENTUM_API_KEY", "").strip()
        self.secret = os.getenv("ALPACA_MOMENTUM_API_SECRET", "").strip()
        self.account_id = os.getenv("ALPACA_MOMENTUM_ACCOUNT_ID", "").strip()
        if not self.key or not self.secret:
            raise RuntimeError("Set ALPACA_MOMENTUM_API_KEY and ALPACA_MOMENTUM_API_SECRET")

        base = (os.getenv("ALPACA_MOMENTUM_BASE_URL") or PAPER_TRADING_BASE).strip().rstrip("/")
        if base != PAPER_TRADING_BASE:
            raise RuntimeError("Momentum account snapshots only support the Alpaca paper endpoint")

        self.session = session or requests.Session()
        self.headers = {
            "APCA-API-KEY-ID": self.key,
            "APCA-API-SECRET-KEY": self.secret,
        }

    def _get(self, route: str):
        response = self.session.get(
            f"{PAPER_TRADING_BASE}/{route.lstrip('/')}",
            headers=self.headers,
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

    def verify_account(self) -> dict:
        account = self._get("account")
        identifiers = {
            str(account.get(field, "")).strip()
            for field in ("id", "account_number")
            if account.get(field)
        }
        if not identifiers:
            raise RuntimeError("Momentum Alpaca account omitted its ID")
        if self.account_id and self.account_id not in identifiers:
            raise MomentumAccountMismatchError("Configured Momentum account ID does not match Alpaca")

        other_account_ids = {
            os.getenv("ALPACA_EQUITY_ACCOUNT_ID", "").strip(),
            os.getenv("ALPACA_OPTIONS_ACCOUNT_ID", "").strip(),
        } - {""}
        if identifiers & other_account_ids:
            raise MomentumAccountMismatchError("Momentum credentials resolve to an Equity or Options account")
        if str(account.get("status", "")).upper() != "ACTIVE" or account.get("trading_blocked"):
            raise RuntimeError("Momentum Alpaca paper account is not active")
        self.account_id = str(account.get("id") or account.get("account_number"))
        return account

    def positions(self) -> list[dict]:
        positions = self._get("positions")
        if not isinstance(positions, list):
            raise ValueError("Momentum Alpaca positions response is malformed")
        return positions
