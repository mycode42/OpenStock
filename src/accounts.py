"""
Account routing for multi-account Alpaca setup.

Account routes:
  - "swing" (Primary):     ALPACA_API_KEY / ALPACA_API_SECRET — multi-day holds
  - "day"   (DayTrading):  ALPACA_DAY_API_KEY / ALPACA_DAY_API_SECRET — intraday, flattens EOD
  - "algo"  (Algo):         ALPACA_ALGO_API_KEY / ALPACA_ALGO_API_SECRET — explicit strategy portfolio route

Credentials are selected based on trading mode. All API helpers accept
an optional `mode` parameter; if omitted, the current trading mode
(from trading_mode.json) determines which account to use.

Swing, Day, and Algo are separate portfolios. The order/position APIs do not
tag positions by strategy, so sharing accounts would let account-wide actions
such as Day's EOD flatten affect other strategies. Missing mode-specific
credentials fail closed. Algo is not selected by the Swing/Day mode resolver.
"""

import os
from dataclasses import dataclass

import requests


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

    # Dedicated Algo paper account.
    algo_key = os.environ.get("ALPACA_ALGO_API_KEY", "")
    algo_secret = os.environ.get("ALPACA_ALGO_API_SECRET", "")
    if algo_key and algo_secret:
        _ACCOUNTS["algo"] = AlpacaAccount(
            name="Algo",
            account_id=os.environ.get("ALPACA_ALGO_ACCOUNT_ID", "unknown"),
            api_key=algo_key,
            api_secret=algo_secret,
        )

    configured = list(_ACCOUNTS.items())
    for index, (left_mode, left) in enumerate(configured):
        for right_mode, right in configured[index + 1:]:
            known_ids_match = (
                left.account_id != "unknown"
                and right.account_id != "unknown"
                and left.account_id == right.account_id
            )
            credentials_match = left.api_key == right.api_key and left.api_secret == right.api_secret
            if known_ids_match or credentials_match:
                raise ValueError(
                    f"{left.name} and {right.name} are configured with the same Alpaca account. "
                    "Trading modes must remain isolated because account-wide position actions "
                    "can affect every strategy routed there."
                )


def get_account_for_mode(mode: str = None) -> AlpacaAccount:
    """
    Get the correct Alpaca account for the given trading mode.

    Args:
        mode: "swing", "day", or explicit "algo" service route. If None,
              resolves from trading_mode.json (Swing/Day only).

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


def verify_account_for_mode(mode: str) -> tuple[AlpacaAccount, dict]:
    """Verify an explicit route against Alpaca before a mode places orders.

    Credential configuration alone cannot prove which paper account Alpaca
    accepted. Refuse trading if the returned account does not match the
    configured account ID, or if Alpaca reports that trading is unavailable.
    """
    configured = get_account_for_mode(mode)
    if not configured.account_id or configured.account_id == "unknown":
        raise ValueError(f"{mode} account ID is not configured")

    response = requests.get(
        f"{configured.base_url}/account",
        headers=configured.headers,
        timeout=10,
    )
    response.raise_for_status()
    broker = response.json()
    broker_ids = {str(broker.get(key)) for key in ("id", "account_number") if broker.get(key)}
    if configured.account_id not in broker_ids:
        raise ValueError(f"{mode} credentials resolve to a different Alpaca account")
    if str(broker.get("status", "")).upper() != "ACTIVE":
        raise ValueError(f"{mode} Alpaca account is not active")
    if broker.get("trading_blocked"):
        raise ValueError(f"{mode} Alpaca account is trading blocked")
    return configured, broker


# Auto-load on import
_load_accounts()
