"""Configured Swing, Day, and fixed Algo universes."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from src.accounts import get_account_for_mode
from src.config import ALGO_FACTOR_SECTOR_BY_SYMBOL, get_mode_config


ET = ZoneInfo("America/New_York")


def configured_universe(mode: str) -> dict:
    """Return the complete fixed ticker list and category map for a mode."""
    if mode not in {"swing", "day", "algo"}:
        raise ValueError("Configured universes are available for swing, day, and algo modes")
    mode_config = get_mode_config(mode)
    categories: dict[str, str] = {}
    tickers: list[str] = []
    for category, bucket in mode_config["universe"].items():
        for raw_ticker in bucket.get("tickers", []):
            ticker = str(raw_ticker).upper().strip()
            if not ticker:
                continue
            categories[ticker] = category
            if ticker not in tickers:
                tickers.append(ticker)

    result = {"tickers": tickers, "categories": categories}
    if mode == "algo":
        result["sector_by_symbol"] = {
            ticker: ALGO_FACTOR_SECTOR_BY_SYMBOL[ticker]
            for ticker in tickers if ticker in ALGO_FACTOR_SECTOR_BY_SYMBOL
        }
    return result


def _fetch_active_assets(mode: str) -> dict[str, dict]:
    """Fetch Alpaca asset flags used to authorize overnight-only entries."""
    account = get_account_for_mode(mode)
    response = requests.get(
        f"{account.base_url.rstrip('/')}/assets",
        headers=account.headers,
        params={"status": "active", "asset_class": "us_equity"},
        timeout=25,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError("Alpaca asset response was not a list")
    return {
        str(asset.get("symbol", "")).upper(): asset
        for asset in payload
        if isinstance(asset, dict) and asset.get("symbol")
    }


def _overnight_tradable(asset: dict | None) -> bool:
    if not asset:
        return False
    attributes = asset.get("attributes") or []
    listed = "overnight_tradable" in attributes if isinstance(attributes, list) else False
    return bool(asset.get("overnight_tradable") or listed) and not bool(asset.get("overnight_halted"))


def load_algo_universe() -> dict:
    """Load fixed Algo membership and Alpaca's current overnight flags.

    The catalog is the source of membership. Broker metadata is consulted only
    for BOATS eligibility; an asset-master outage therefore cannot silently
    replace the configured list or authorize overnight entries.
    """
    result = configured_universe("algo")
    result.update({
        "selection_date": datetime.now(ET).date().isoformat(),
        "timestamp": datetime.now(ET).isoformat(),
        "overnight_tradable_tickers": [],
        "overnight_metadata_status": "unavailable",
        "easy_to_borrow_tickers": [],
        "shortability_metadata_status": "unavailable",
    })
    try:
        assets = _fetch_active_assets("algo")
    except (requests.RequestException, ValueError) as exc:
        result["overnight_metadata_error"] = f"{type(exc).__name__}: {exc}"
        return result

    result["overnight_tradable_tickers"] = sorted(
        ticker for ticker in result["tickers"] if _overnight_tradable(assets.get(ticker))
    )
    result["overnight_metadata_status"] = "ok"
    result["easy_to_borrow_tickers"] = sorted(
        ticker for ticker in result["tickers"]
        if assets.get(ticker, {}).get("tradable") is True
        and assets.get(ticker, {}).get("shortable") is True
        and str(assets.get(ticker, {}).get("borrow_status", "")).lower() == "easy_to_borrow"
    )
    result["shortability_metadata_status"] = "ok"
    return result
