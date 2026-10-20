"""Free daily macro inputs for the Algo regime detector.

FRED provides CBOE VIX and 3-month VIX closes plus a daily high-yield spread
series. Observations are cached additively in the existing local market store.
"""

from __future__ import annotations

import csv
import io
import logging
import math
from datetime import date, datetime, timedelta, timezone
from threading import Lock

import requests

from src.algo.market_store import DEFAULT_DB, SQLiteBarStore


LOG = logging.getLogger("algo_regime_macro")
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_SERIES = {
    "VIXCLS": "vix",
    "VXVCLS": "vix3m",
    "BAMLH0A0HYM2": "high_yield_spread",
}
_DEFAULT_STORE_LOCK = Lock()
_DEFAULT_STORE: SQLiteBarStore | None = None


def _store_or_default(store: SQLiteBarStore | None) -> SQLiteBarStore:
    if store is not None:
        return store
    global _DEFAULT_STORE
    with _DEFAULT_STORE_LOCK:
        if _DEFAULT_STORE is None:
            _DEFAULT_STORE = SQLiteBarStore(DEFAULT_DB)
        return _DEFAULT_STORE


def parse_fred_csv(payload: str, *, today: date | None = None) -> dict[str, dict[str, float]]:
    """Parse public FRED graph CSV, dropping missing and future observations."""
    current_day = today or datetime.now(timezone.utc).date()
    reader = csv.DictReader(io.StringIO(payload))
    if not reader.fieldnames or "observation_date" not in reader.fieldnames:
        raise ValueError("FRED CSV omitted observation_date")
    missing = set(FRED_SERIES) - set(reader.fieldnames)
    if missing:
        raise ValueError(f"FRED CSV omitted series: {', '.join(sorted(missing))}")
    output: dict[str, dict[str, float]] = {series: {} for series in FRED_SERIES}
    for row in reader:
        try:
            observed = date.fromisoformat(str(row.get("observation_date", ""))[:10])
        except ValueError:
            continue
        if observed > current_day:
            continue
        for series in FRED_SERIES:
            raw = str(row.get(series, "")).strip()
            if raw in {"", "."}:
                continue
            try:
                value = float(raw)
            except ValueError:
                continue
            if math.isfinite(value):
                output[series][observed.isoformat()] = value
    if not output["VIXCLS"]:
        raise ValueError("FRED CSV contained no usable VIX observations")
    return output


def refresh_fred_regime_series(
    store: SQLiteBarStore | None = None, *,
    now: datetime | None = None, force: bool = False, max_age_hours: float = 12,
    request_get=None,
) -> dict:
    """Refresh the cache at most twice daily and return source diagnostics."""
    target = _store_or_default(store)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    last_fetch = target.latest_macro_fetch(provider="fred")
    if last_fetch and not force:
        try:
            previous = datetime.fromisoformat(last_fetch.replace("Z", "+00:00"))
            if previous.tzinfo is None:
                previous = previous.replace(tzinfo=timezone.utc)
            age = current - previous.astimezone(timezone.utc)
            if timedelta(0) <= age < timedelta(hours=max_age_hours):
                return {"status": "cached", "last_fetch": last_fetch, "age_seconds": age.total_seconds()}
        except (TypeError, ValueError):
            pass
    getter = request_get or requests.get
    response = getter(
        FRED_CSV_URL, params={"id": ",".join(FRED_SERIES)},
        headers={"User-Agent": "swarm-trader-regime/1.0"}, timeout=12,
    )
    response.raise_for_status()
    parsed = parse_fred_csv(response.text, today=current.date())
    written = target.write_macro_observations(parsed, provider="fred")
    latest = {series: max(values) if values else None for series, values in parsed.items()}
    LOG.info("Cached FRED regime data: rows=%d latest=%s", written, latest)
    return {"status": "refreshed", "rows_written": written, "latest_dates": latest}


def load_fred_regime_series(
    store: SQLiteBarStore | None = None, *, refresh: bool = False,
    now: datetime | None = None, force: bool = False,
) -> tuple[dict[str, list[dict]], dict]:
    """Load cached inputs, optionally refreshing; network errors keep cache usable."""
    target = _store_or_default(store)
    diagnostics = {"status": "offline_cache"}
    if refresh:
        try:
            diagnostics = refresh_fred_regime_series(target, now=now, force=force)
        except (OSError, requests.RequestException, ValueError) as exc:
            diagnostics = {"status": "stale_fallback", "error": type(exc).__name__}
            LOG.warning("FRED regime refresh failed; using cached observations: %s", type(exc).__name__)
    stored = target.macro_series(list(FRED_SERIES), provider="fred")
    return {
        feature_name: stored.get(series_id, [])
        for series_id, feature_name in FRED_SERIES.items()
    }, diagnostics


__all__ = [
    "FRED_CSV_URL", "FRED_SERIES", "load_fred_regime_series",
    "parse_fred_csv", "refresh_fred_regime_series",
]
