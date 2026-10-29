"""Shared, rate-limited SEC EDGAR access and ticker/CIK mapping."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time

import requests
from dotenv import load_dotenv

from runtime.atomic import atomic_write_json
from runtime.paths import CACHE_DIR, ROOT

load_dotenv(ROOT / ".env")


SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_ARCHIVE_URL = "https://data.sec.gov/submissions/{filename}"
SEC_MIN_INTERVAL_SECONDS = 0.12
TICKER_MAP_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
_TICKER_MAP_MEMORY_TTL_SECONDS = 60 * 60
_SEC_REQUEST_LOCK = threading.Lock()
_TICKER_MAP_LOCK = threading.Lock()
_LAST_SEC_REQUEST = 0.0
_TICKER_MAP: dict[str, str] | None = None
_TICKER_MAP_LOADED_AT = 0.0
_TICKER_MAP_PATH = CACHE_DIR / "sec" / "company_tickers.json"


def _respect_rate_limit() -> None:
    """Serialize request start times across SEC consumers in this process."""
    global _LAST_SEC_REQUEST
    with _SEC_REQUEST_LOCK:
        elapsed = time.monotonic() - _LAST_SEC_REQUEST
        if elapsed < SEC_MIN_INTERVAL_SECONDS:
            time.sleep(SEC_MIN_INTERVAL_SECONDS - elapsed)
        _LAST_SEC_REQUEST = time.monotonic()


def get_sec_response(url: str, *, timeout: int = 30) -> requests.Response:
    """Issue one rate-limited SEC request and raise on HTTP errors."""
    _respect_rate_limit()
    response = requests.get(
        url,
        headers={
            "User-Agent": os.environ.get("SEC_USER_AGENT", "GLORFT mordecai@naboo.lan"),
            "Accept": "application/json, text/html",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    return response


def _read_fresh_json(path: Path, ttl_seconds: int) -> dict | None:
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        if (datetime.now(timezone.utc) - modified).total_seconds() > ttl_seconds:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def get_sec_json(
    url: str,
    *,
    cache_path: str | Path | None = None,
    ttl_seconds: int = 0,
    timeout: int = 30,
) -> dict:
    """Fetch a JSON object, optionally serving a fresh atomic disk cache."""
    path = Path(cache_path) if cache_path is not None else None
    if path is not None and ttl_seconds > 0:
        cached = _read_fresh_json(path, ttl_seconds)
        if cached is not None:
            return cached
    payload = get_sec_response(url, timeout=timeout).json()
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object from SEC endpoint {url}")
    if path is not None:
        atomic_write_json(path, payload, indent=None, sort_keys=True)
    return payload


def ticker_cik_map() -> dict[str, str]:
    """Return an uppercase ticker-to-zero-padded-CIK map with a shared cache."""
    global _TICKER_MAP, _TICKER_MAP_LOADED_AT
    now = time.monotonic()
    with _TICKER_MAP_LOCK:
        if _TICKER_MAP is not None and now - _TICKER_MAP_LOADED_AT < _TICKER_MAP_MEMORY_TTL_SECONDS:
            return _TICKER_MAP
        payload = get_sec_json(
            SEC_TICKERS_URL,
            cache_path=_TICKER_MAP_PATH,
            ttl_seconds=TICKER_MAP_CACHE_TTL_SECONDS,
            timeout=30,
        )
        mapping: dict[str, str] = {}
        for item in payload.values():
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("ticker") or "").strip().upper()
            cik = item.get("cik_str")
            if symbol and cik is not None:
                mapping[symbol] = str(cik).zfill(10)
        _TICKER_MAP = mapping
        _TICKER_MAP_LOADED_AT = time.monotonic()
        return mapping


def resolve_cik(ticker: str) -> str | None:
    """Resolve one ticker through the shared SEC company-ticker registry."""
    return ticker_cik_map().get(str(ticker).strip().upper())


def tickers_to_ciks(tickers: set[str]) -> dict[str, str]:
    """Resolve only requested tickers from the shared SEC company registry."""
    mapping = ticker_cik_map()
    return {
        symbol: mapping[symbol]
        for symbol in {str(ticker).strip().upper() for ticker in tickers}
        if symbol in mapping
    }


__all__ = [
    "SEC_ARCHIVE_URL", "SEC_SUBMISSIONS_URL", "SEC_TICKERS_URL",
    "get_sec_json", "get_sec_response", "resolve_cik", "ticker_cik_map",
    "tickers_to_ciks",
]
