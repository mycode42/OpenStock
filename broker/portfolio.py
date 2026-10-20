"""Read-only Alpaca helpers for the Event Driven analyst snapshot."""

import requests

from src.accounts import get_account_for_mode


ALPACA_BASE_URL = "https://paper-api.alpaca.markets/v2"


def _get_headers(mode: str = "algo") -> dict:
    """Get credentials for the dedicated Algo paper account."""
    return get_account_for_mode(mode).headers


def get_alpaca_account(mode: str = "algo") -> dict:
    """Fetch the paper account used by the Event Driven analysis."""
    response = requests.get(
        f"{ALPACA_BASE_URL}/account",
        headers=_get_headers(mode),
        timeout=10,
    )
    response.raise_for_status()
    return response.json()


def get_alpaca_positions(mode: str = "algo") -> list[dict]:
    """Fetch positions from the dedicated Algo paper account."""
    response = requests.get(
        f"{ALPACA_BASE_URL}/positions",
        headers=_get_headers(mode),
        timeout=10,
    )
    response.raise_for_status()
    return response.json()


def convert_to_portfolio(
    alpaca_positions: list[dict],
    alpaca_account: dict,
    tickers: list[str] | None = None,
) -> dict:
    """Adapt broker positions to the analyst graph's portfolio structure."""
    positions_by_symbol = {position["symbol"]: position for position in alpaca_positions}
    all_tickers = list(positions_by_symbol)
    for ticker in tickers or []:
        if ticker not in all_tickers:
            all_tickers.append(ticker)

    positions = {}
    for ticker in all_tickers:
        position = positions_by_symbol.get(ticker, {})
        quantity = int(float(position.get("qty", 0)))
        entry_price = float(position.get("avg_entry_price", 0))
        positions[ticker] = {
            "long": quantity if quantity > 0 else 0,
            "short": abs(quantity) if quantity < 0 else 0,
            "long_cost_basis": entry_price if quantity > 0 else 0.0,
            "short_cost_basis": entry_price if quantity < 0 else 0.0,
            "short_margin_used": 0.0,
        }

    return {
        "cash": float(alpaca_account.get("cash", 0)),
        "margin_requirement": 0.5,
        "margin_used": 0.0,
        "positions": positions,
        "realized_gains": {ticker: {"long": 0.0, "short": 0.0} for ticker in all_tickers},
    }
