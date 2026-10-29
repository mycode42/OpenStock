"""Per-underlying risk-free rate and dividend yield with provenance.

Pricing paths call ``MarketInputs`` lookups, which read only in-memory state
loaded from the cache file and config. Network access happens solely in
``refresh_market_inputs``, which the paper cycle calls once per session before
scanning. Fallback order: fresh cache, then config, then the packaged constant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import csv
import io
import json
import logging
import math
from pathlib import Path
from typing import Callable, Mapping

import requests

from market.regime import FRED_CSV_URL
from runtime.atomic import atomic_write_json
from runtime.paths import DATA_DIR, ROOT


LOG = logging.getLogger(__name__)
CACHE_PATH = DATA_DIR / "options" / "market_inputs.json"
RATE_SERIES_ID = "DGS3MO"
RATE_SOURCE = f"fred:{RATE_SERIES_ID}"
DEFAULT_RATE = 0.04
DEFAULT_DIVIDEND_YIELD = 0.015
CACHE_MAX_AGE_DAYS = 5
SCHEMA_VERSION = 1
ALPACA_DATA_BASE = "https://data.alpaca.markets"
ALPACA_CORPORATE_ACTIONS_URL = f"{ALPACA_DATA_BASE}/v1/corporate-actions"
ALPACA_STOCK_SNAPSHOTS_URL = f"{ALPACA_DATA_BASE}/v2/stocks/snapshots"
DIVIDEND_LOOKBACK_DAYS = 365
DIVIDEND_SOURCE_ALPACA = "alpaca:corporate_actions.cash_dividends"
DIVIDEND_SOURCE_YFINANCE = "yfinance:trailingAnnualDividendYield"
DIVIDEND_SNAPSHOT_SCHEMA = "options_dividend_snapshot_v1"


@dataclass(frozen=True)
class MarketValue:
    value: float
    source: str
    as_of: str | None


def _finite_fraction(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and 0.0 <= number < 1.0 else None


def _load_cache(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        LOG.warning("options market-input cache is unreadable; using config fallback")
        return {}
    return payload if isinstance(payload, dict) and payload.get("schema_version") == SCHEMA_VERSION else {}


class MarketInputs:
    """Resolves rate and dividend yield for one underlying as of a date."""

    def __init__(
        self,
        config: Mapping,
        *,
        cache_path: Path | None = CACHE_PATH,
        max_age_days: int = CACHE_MAX_AGE_DAYS,
        dividend_snapshot: Mapping[str, MarketValue] | None = None,
    ) -> None:
        self.config_rate = _finite_fraction(config.get("rate"))
        self.config_dividend_yield = _finite_fraction(config.get("dividend_yield"))
        self.config_dividend_yields = {
            str(symbol).upper(): _finite_fraction(value)
            for symbol, value in (config.get("dividend_yields") or {}).items()
        }
        self.max_age_days = int(max_age_days)
        self.cache_path = cache_path
        self._cache = _load_cache(cache_path)
        self._dividend_snapshot = dict(dividend_snapshot or {})

    def _fresh_cache_entry(self, entry: object, as_of: date) -> tuple[float, str] | None:
        if not isinstance(entry, dict):
            return None
        value = _finite_fraction(entry.get("value"))
        observed = _parse_day(entry.get("observed"))
        fetched = _parse_day(entry.get("fetched_on"))
        if value is None or observed is None or fetched is None:
            return None
        # Never use an observation dated after the requested date, and drop stale fetches.
        if observed > as_of or (as_of - fetched).days > self.max_age_days:
            return None
        return value, str(entry.get("source") or "cache")

    def rate(self, as_of: date) -> MarketValue:
        cached = self._fresh_cache_entry(self._cache.get("rate"), as_of)
        if cached is not None:
            entry = self._cache["rate"]
            return MarketValue(cached[0], f"cache:{cached[1]}", str(entry.get("observed")))
        if self.config_rate is not None:
            return MarketValue(self.config_rate, "config:rate", None)
        return MarketValue(DEFAULT_RATE, "default:rate", None)

    def dividend_yield(self, underlying: str, as_of: date) -> MarketValue:
        symbol = str(underlying).upper()
        cached = self._fresh_cache_entry((self._cache.get("dividend_yield") or {}).get(symbol), as_of)
        if cached is not None:
            entry = self._cache["dividend_yield"][symbol]
            return MarketValue(cached[0], f"cache:{cached[1]}", str(entry.get("observed")))
        snapshot = self._dividend_snapshot.get(symbol)
        if snapshot is not None:
            return snapshot
        per_symbol = self.config_dividend_yields.get(symbol)
        if per_symbol is not None:
            return MarketValue(per_symbol, "config:dividend_yields", None)
        if self.config_dividend_yield is not None:
            return MarketValue(self.config_dividend_yield, "config:dividend_yield", None)
        return MarketValue(DEFAULT_DIVIDEND_YIELD, "default:dividend_yield", None)


def research_market_inputs(execution: Mapping) -> MarketInputs:
    """Config and dated dividend snapshot for research replays; never reads the live cache or network."""
    snapshot_path = execution.get("dividend_snapshot_path")
    snapshot = load_dividend_snapshot(ROOT / str(snapshot_path)) if snapshot_path else {}
    return MarketInputs({
        "rate": execution.get("risk_free_rate", DEFAULT_RATE),
        "dividend_yield": execution.get("dividend_yield", DEFAULT_DIVIDEND_YIELD),
        "dividend_yields": execution.get("dividend_yields") or {},
    }, cache_path=None, dividend_snapshot=snapshot)


def _parse_day(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def parse_fred_series_csv(payload: str, series_id: str, *, today: date) -> tuple[date, float]:
    """Latest valid observation of one FRED series, as (observed date, percent)."""
    reader = csv.DictReader(io.StringIO(payload))
    fields = reader.fieldnames or []
    date_key = "observation_date" if "observation_date" in fields else "DATE"
    if date_key not in fields or series_id not in fields:
        raise ValueError(f"FRED CSV omitted {series_id}")
    latest: tuple[date, float] | None = None
    for row in reader:
        raw_value = (row.get(series_id) or "").strip()
        if raw_value in {"", "."}:
            continue
        observed = _parse_day(row.get(date_key))
        if observed is None or observed > today:
            continue
        percent = float(raw_value)
        if not math.isfinite(percent):
            continue
        latest = (observed, percent)
    if latest is None:
        raise ValueError(f"FRED series {series_id} has no usable observation")
    return latest


def _fetch_fred_series(series_id: str, *, today: date, timeout: float = 10.0) -> tuple[date, float]:
    response = requests.get(FRED_CSV_URL, params={"id": series_id}, timeout=timeout)
    response.raise_for_status()
    return parse_fred_series_csv(response.text, series_id, today=today)


def refresh_market_inputs(
    config: Mapping,
    *,
    cache_path: Path | None = CACHE_PATH,
    today: date | None = None,
    fetch: Callable[..., tuple[date, float]] = _fetch_fred_series,
) -> dict:
    """Refresh the cached rate once per day; network failures keep the prior cache or config fallback."""
    current_day = today or datetime.now(timezone.utc).date()
    existing = _load_cache(cache_path)
    rate_entry = existing.get("rate") if isinstance(existing.get("rate"), dict) else {}
    if rate_entry.get("fetched_on") == current_day.isoformat():
        return {"status": "already_refreshed", "source": rate_entry.get("source")}
    try:
        observed, percent = fetch(RATE_SERIES_ID, today=current_day)
        value = percent / 100.0
        if not 0.0 <= value < 1.0:
            raise ValueError("risk-free rate outside plausible range")
    except (OSError, requests.RequestException, ValueError) as exc:
        LOG.warning("market-input refresh failed; using cache or config: %s", type(exc).__name__)
        return {"status": "fallback", "error": type(exc).__name__}
    payload = {
        "schema_version": SCHEMA_VERSION,
        "rate": {
            "value": value,
            "observed": observed.isoformat(),
            "fetched_on": current_day.isoformat(),
            "source": RATE_SOURCE,
        },
        "dividend_yield": existing.get("dividend_yield") or {},
    }
    if cache_path is not None:
        atomic_write_json(cache_path, payload)
    return {"status": "refreshed", "source": RATE_SOURCE, "observed": observed.isoformat(), "value": value}


def trailing_dividend_yield(rows: list[dict], *, close: float | None, as_of: date) -> tuple[float, int] | None:
    """Cash dividends with ex-dates in (as_of - 365 days, as_of] per share, divided by the latest close."""
    if close is None or not math.isfinite(close) or close <= 0:
        return None
    start = as_of - timedelta(days=DIVIDEND_LOOKBACK_DAYS)
    total = 0.0
    count = 0
    for row in rows:
        ex_date = _parse_day(row.get("ex_date"))
        rate = row.get("rate")
        if ex_date is None or not start < ex_date <= as_of:
            continue
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate < 0:
            continue
        total += float(rate)
        count += 1
    value = total / close
    return (value, count) if 0.0 <= value < 1.0 else None


def fetch_cash_dividends(
    symbol: str, *, as_of: date, headers: Mapping[str, str], get: Callable | None = None, timeout: float = 15.0,
) -> list[dict]:
    """Alpaca corporate actions (cash dividends) for one symbol, following pagination."""
    getter = get or requests.get
    start = as_of - timedelta(days=DIVIDEND_LOOKBACK_DAYS)
    rows: list[dict] = []
    token: str | None = None
    for _ in range(50):
        params: dict = {
            "symbols": symbol, "types": "cash_dividend", "start": start.isoformat(),
            "end": as_of.isoformat(), "limit": 1000, "sort": "asc",
        }
        if token:
            params["page_token"] = token
        response = getter(ALPACA_CORPORATE_ACTIONS_URL, headers=dict(headers), params=params, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("corporate actions payload is not an object")
        rows.extend((payload.get("corporate_actions") or {}).get("cash_dividends") or [])
        token = payload.get("next_page_token")
        if not token:
            return rows
    raise ValueError("corporate actions pagination did not terminate")


def fetch_latest_closes(
    symbols: list[str], *, headers: Mapping[str, str], get: Callable | None = None, timeout: float = 15.0,
) -> dict[str, float]:
    """Latest daily-bar close per symbol from Alpaca stock snapshots (IEX feed)."""
    getter = get or requests.get
    response = getter(
        ALPACA_STOCK_SNAPSHOTS_URL, headers=dict(headers),
        params={"symbols": ",".join(symbols), "feed": "iex"}, timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("stock snapshots payload is not an object")
    closes: dict[str, float] = {}
    for symbol, snapshot in payload.items():
        bar = snapshot.get("dailyBar") if isinstance(snapshot, dict) else None
        close = bar.get("c") if isinstance(bar, dict) else None
        if isinstance(close, (int, float)) and not isinstance(close, bool) and math.isfinite(close) and close > 0:
            closes[str(symbol).upper()] = float(close)
    return closes


def _yfinance_trailing_yield(symbol: str) -> float | None:
    """Fallback only: yfinance trailingAnnualDividendYield is a fraction (dividendYield is percent)."""
    try:
        import yfinance
    except ImportError:
        return None
    try:
        info = yfinance.Ticker(symbol).info or {}
    except Exception as exc:  # yfinance raises assorted network and parsing errors
        LOG.warning("yfinance dividend lookup failed for %s: %s", symbol, type(exc).__name__)
        return None
    return _finite_fraction(info.get("trailingAnnualDividendYield"))


def resolve_dividend_yields(
    symbols: list[str], *, as_of: date, headers: Mapping[str, str], get: Callable | None = None,
    yfinance_lookup: Callable[[str], float | None] = _yfinance_trailing_yield,
) -> dict[str, dict]:
    """Trailing 12-month cash yield per symbol: Alpaca first, yfinance if Alpaca has no usable answer.

    Symbols that neither source covers are omitted, so the config fallbacks apply.
    """
    try:
        closes = fetch_latest_closes(symbols, headers=headers, get=get)
    except (requests.RequestException, ValueError) as exc:
        LOG.warning("Alpaca close fetch failed; dividend yields use yfinance or config: %s", type(exc).__name__)
        closes = {}
    table: dict[str, dict] = {}
    for symbol in symbols:
        resolved: tuple[float, str, int | None] | None = None
        try:
            rows = fetch_cash_dividends(symbol, as_of=as_of, headers=headers, get=get)
            result = trailing_dividend_yield(rows, close=closes.get(symbol), as_of=as_of)
            if result is not None:
                resolved = (result[0], DIVIDEND_SOURCE_ALPACA, result[1])
        except (requests.RequestException, ValueError) as exc:
            LOG.warning("Alpaca dividend lookup failed for %s: %s", symbol, type(exc).__name__)
        if resolved is None:
            value = yfinance_lookup(symbol)
            if value is not None:
                resolved = (value, DIVIDEND_SOURCE_YFINANCE, None)
        if resolved is not None:
            table[symbol] = {
                "value": resolved[0], "source": resolved[1],
                "observed": as_of.isoformat(), "dividend_rows": resolved[2],
            }
    return table


def configured_dividend_symbols(config: Mapping) -> list[str]:
    """Every underlying any options sleeve can trade, plus explicit per-symbol config overrides."""
    symbols: set[str] = set()
    for section in ("vrp", "llm_driven", "directional_overlay", "long_volatility"):
        for symbol in (config.get(section) or {}).get("underlyings") or []:
            symbols.add(str(symbol).upper())
    symbols.update(str(symbol).upper() for symbol in (config.get("dividend_yields") or {}))
    return sorted(symbol for symbol in symbols if symbol)


def refresh_dividend_yields(
    symbols: list[str], *, as_of: date, headers: Mapping[str, str],
    cache_path: Path | None = CACHE_PATH, resolver: Callable = resolve_dividend_yields,
) -> dict:
    """Write resolved dividend yields into the live cache once per session; the rate block is preserved."""
    existing = _load_cache(cache_path)
    block = dict(existing.get("dividend_yield") or {}) if isinstance(existing.get("dividend_yield"), dict) else {}
    stamp = as_of.isoformat()
    if symbols and all(isinstance(block.get(s), dict) and block[s].get("fetched_on") == stamp for s in symbols):
        return {"status": "already_refreshed"}
    table = resolver(symbols, as_of=as_of, headers=headers)
    if not table:
        return {"status": "fallback", "resolved": 0}
    block.update({symbol: {**entry, "fetched_on": stamp} for symbol, entry in table.items()})
    payload = {**existing, "schema_version": SCHEMA_VERSION, "dividend_yield": block}
    if cache_path is not None:
        atomic_write_json(cache_path, payload)
    return {"status": "refreshed", "resolved": len(table), "sources": sorted({e["source"] for e in table.values()})}


def write_dividend_snapshot(table: Mapping[str, dict], *, as_of: date, path: Path) -> dict:
    """Dated, committed dividend table that research reads so sweeps never touch the network."""
    payload = {
        "schema": DIVIDEND_SNAPSHOT_SCHEMA,
        "as_of": as_of.isoformat(),
        "lookback_days": DIVIDEND_LOOKBACK_DAYS,
        "dividend_yields": {
            symbol: {"value": entry["value"], "source": entry["source"], "as_of": entry["observed"]}
            for symbol, entry in sorted(table.items())
        },
    }
    atomic_write_json(path, payload)
    return payload


def load_dividend_snapshot(path: Path) -> dict[str, MarketValue]:
    """Read a dated dividend snapshot; a missing or malformed file fails loudly rather than silently drifting."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"dividend snapshot is unreadable: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != DIVIDEND_SNAPSHOT_SCHEMA:
        raise ValueError(f"dividend snapshot has an unexpected schema: {path}")
    values: dict[str, MarketValue] = {}
    for symbol, entry in (payload.get("dividend_yields") or {}).items():
        value = _finite_fraction(entry.get("value")) if isinstance(entry, dict) else None
        if value is not None:
            values[str(symbol).upper()] = MarketValue(
                value, f"snapshot:{entry.get('source')}", str(entry.get("as_of")),
            )
    return values


__all__ = [
    "CACHE_PATH", "DEFAULT_DIVIDEND_YIELD", "DEFAULT_RATE", "MarketInputs", "MarketValue",
    "load_dividend_snapshot", "parse_fred_series_csv", "refresh_dividend_yields",
    "configured_dividend_symbols", "refresh_market_inputs", "research_market_inputs",
    "resolve_dividend_yields",
    "trailing_dividend_yield", "write_dividend_snapshot",
]
