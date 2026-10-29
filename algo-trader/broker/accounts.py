"""Credentials and verification for the Algo Alpaca paper account."""

import os
from dataclasses import dataclass

import requests

from broker.endpoints import ALGO_TRADING_BASE


@dataclass
class AlpacaAccount:
    """Credentials and metadata for a single Alpaca account."""

    name: str
    account_id: str
    api_key: str
    api_secret: str
    base_url: str = ALGO_TRADING_BASE

    @property
    def headers(self) -> dict:
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
            "Content-Type": "application/json",
        }


_ACCOUNT: AlpacaAccount | None = None


def get_account_for_mode(mode: str | None = None) -> AlpacaAccount:
    """Return the dedicated Algo paper account; reject removed routes."""
    global _ACCOUNT
    if mode is not None and mode.lower() != "algo":
        raise ValueError("Only the algo account route is supported")
    if _ACCOUNT is None:
        api_key = os.environ.get("ALPACA_EQUITY_API_KEY", "")
        api_secret = os.environ.get("ALPACA_EQUITY_API_SECRET", "")
        if not api_key or not api_secret:
            raise ValueError("Configure ALPACA_EQUITY_API_KEY and ALPACA_EQUITY_API_SECRET in .env")
        _ACCOUNT = AlpacaAccount(
            name="Algo",
            account_id=os.environ.get("ALPACA_EQUITY_ACCOUNT_ID", "unknown"),
            api_key=api_key,
            api_secret=api_secret,
        )
    return _ACCOUNT


def verify_account_for_mode(mode: str | None = None) -> tuple[AlpacaAccount, dict]:
    """Verify the configured route matches an active, unblocked Alpaca account."""
    configured = get_account_for_mode(mode)
    if not configured.account_id or configured.account_id == "unknown":
        raise ValueError("Algo account ID is not configured")

    response = requests.get(
        f"{configured.base_url}/account",
        headers=configured.headers,
        timeout=10,
    )
    response.raise_for_status()
    broker = response.json()
    broker_ids = {str(broker.get(key)) for key in ("id", "account_number") if broker.get(key)}
    if configured.account_id not in broker_ids:
        raise ValueError("Algo credentials resolve to a different Alpaca account")
    if str(broker.get("status", "")).upper() != "ACTIVE":
        raise ValueError("Algo Alpaca account is not active")
    if broker.get("trading_blocked"):
        raise ValueError("Algo Alpaca account is trading blocked")
    return configured, broker
