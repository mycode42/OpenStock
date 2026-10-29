"""Algo equity universe: static categories, live selection, and broker metadata."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from broker.accounts import get_account_for_mode
from portfolio.config import ALGO_FACTOR_SECTOR_BY_SYMBOL, get_candidate_universe, get_mode_config


ET = ZoneInfo("America/New_York")


def configured_universe(mode: str = "algo") -> dict:
    """Return the configured Algo ticker list and category map."""
    if mode != "algo":
        raise ValueError("Only the algo account route is supported")
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


def current_selector_rules_sha256() -> str:
    """Hash of the selector rules in force now; a change forces a universe reload."""
    from options.research.universe_selection import _rules_hash, load_rules

    return _rules_hash(load_rules())


def selected_universe(now: datetime | None = None) -> dict:
    """Live membership: the selector's eligible subset of the 117-name pool.

    Raises ``EligibleUniverseError`` when the selection is stale or too small.
    The caller pauses new entries in that case; owned positions are still managed.
    """
    from options.research.universe_selection import live_eligible_universe

    selection = live_eligible_universe(now=now)
    static_categories = configured_universe("algo")["categories"]
    pool_categories = {
        ticker: category
        for category, bucket in get_candidate_universe().items()
        for ticker in bucket["tickers"]
    }
    tickers: list[str] = []
    for raw in selection["eligible_symbols"]:
        ticker = str(raw).upper().strip()
        if ticker and ticker not in tickers:
            tickers.append(ticker)
    categories = {
        ticker: static_categories.get(ticker) or pool_categories.get(ticker, "unknown")
        for ticker in tickers
    }
    return {
        "tickers": tickers,
        "categories": categories,
        "sector_by_symbol": {
            ticker: ALGO_FACTOR_SECTOR_BY_SYMBOL.get(ticker, categories[ticker]) for ticker in tickers
        },
        "source": "eligible_universe",
        "selector_as_of": selection["as_of_date"],
        "selector_rules_sha256": selection["rules_sha256"],
        "selector_snapshot": selection["snapshot_location"],
    }


def load_algo_universe() -> dict:
    """Load the selected live universe and Alpaca's current overnight flags.

    Membership comes from the selector. Broker metadata is consulted only for
    BOATS and shortability; an asset-master outage cannot change membership or
    authorize overnight entries.
    """
    result = selected_universe()
    result.update({
        "selection_date": datetime.now(ET).date().isoformat(),
        "timestamp": datetime.now(ET).isoformat(),
        "overnight_metadata_status": "unavailable",
        "easy_to_borrow_tickers": [],
        "shortability_metadata_status": "unavailable",
    })
    try:
        assets = _fetch_active_assets("algo")
    except (requests.RequestException, ValueError) as exc:
        result["overnight_metadata_error"] = f"{type(exc).__name__}: {exc}"
        return result

    result["overnight_metadata_status"] = "ok"
    result["easy_to_borrow_tickers"] = sorted(
        ticker for ticker in result["tickers"]
        if assets.get(ticker, {}).get("tradable") is True
        and assets.get(ticker, {}).get("shortable") is True
        and str(assets.get(ticker, {}).get("borrow_status", "")).lower() == "easy_to_borrow"
    )
    result["shortability_metadata_status"] = "ok"
    return result
