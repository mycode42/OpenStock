"""
Account routing for multi-account Alpaca setup.

Two accounts:
  - "swing" (Primary):     ALPACA_API_KEY / ALPACA_API_SECRET — multi-day holds
  - "day"   (DayTrading):  ALPACA_DAY_API_KEY / ALPACA_DAY_API_SECRET — intraday, flattens EOD

Credentials are selected based on trading mode. All API helpers accept
an optional `mode` parameter; if omitted, the current trading mode
(from trading_mode.json) determines which account to use.

Swing and Day are separate portfolios. The order/position APIs do not tag
positions by strategy, so sharing one account would let Day's EOD flatten
close Swing positions. Missing mode-specific credentials fail closed.
"""

import os
from dataclasses import dataclass


@dataclass
class AlpacaAccount:
    """Credentials + metadata for a single Alpaca account."""
    name: str
    account_id: str
    api_key: str
    api_secret: str
    base_url: str = "https://paper-api.alpaca.markets/v2"

    @property
    def headers(self) -> dict:
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
            "Content-Type": "application/json",
        }


# Account registry — loaded from env vars
_ACCOUNTS: dict[str, AlpacaAccount] = {}


def _load_accounts() -> None:
    """Load account credentials from environment. Called once on import."""
    global _ACCOUNTS

    # Swing account (primary — ALPACA_API_KEY / ALPACA_API_SECRET)
    swing_key = os.environ.get("ALPACA_API_KEY", "")
    swing_secret = os.environ.get("ALPACA_API_SECRET", "")
    if swing_key and swing_secret:
        _ACCOUNTS["swing"] = AlpacaAccount(
            name="Swing",
            account_id=os.environ.get("ALPACA_ACCOUNT_ID", "unknown"),
            api_key=swing_key,
            api_secret=swing_secret,
        )

    # Day trading account (optional — ALPACA_DAY_API_KEY / ALPACA_DAY_API_SECRET)
    day_key = os.environ.get("ALPACA_DAY_API_KEY", "")
    day_secret = os.environ.get("ALPACA_DAY_API_SECRET", "")
    if day_key and day_secret:
        _ACCOUNTS["day"] = AlpacaAccount(
            name="DayTrading",
            account_id=os.environ.get("ALPACA_DAY_ACCOUNT_ID", "unknown"),
            api_key=day_key,
            api_secret=day_secret,
        )

    swing = _ACCOUNTS.get("swing")
    day = _ACCOUNTS.get("day")
    if swing and day:
        known_ids_match = (
            swing.account_id != "unknown"
            and day.account_id != "unknown"
            and swing.account_id == day.account_id
        )
        credentials_match = (
            swing.api_key == day.api_key
            and swing.api_secret == day.api_secret
        )
        if known_ids_match or credentials_match:
            raise ValueError(
                "Swing and Day are configured with the same Alpaca account. "
                "They must be separate because Day flatten closes every position in its account."
            )


def get_account_for_mode(mode: str = None) -> AlpacaAccount:
    """
    Get the correct Alpaca account for the given trading mode.

    Args:
        mode: "swing" or "day". If None, resolves from trading_mode.json.

    Returns:
        AlpacaAccount with credentials for the appropriate account.

    Raises:
        ValueError if the account for the given mode is not configured.
    """
    if not _ACCOUNTS:
        _load_accounts()

    if mode is None:
        from src.config import resolve_mode
        mode = resolve_mode()

    mode = mode.lower()
    if mode == "auto":
        mode = "swing"  # safe default when the agent has not selected a mode

    if mode in _ACCOUNTS:
        return _ACCOUNTS[mode]

    raise ValueError(
        f"No Alpaca account configured for mode '{mode}'. "
        "Configure the matching mode credentials in .env."
    )


def get_all_accounts() -> dict[str, AlpacaAccount]:
    """Return all configured accounts."""
    if not _ACCOUNTS:
        _load_accounts()
    return dict(_ACCOUNTS)


# Auto-load on import
_load_accounts()
