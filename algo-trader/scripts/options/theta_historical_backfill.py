#!/usr/bin/env python3
"""Download resumable ThetaData EOD option chains for Algo portfolio symbols."""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta
import fcntl
import gzip
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable
from zoneinfo import ZoneInfo

from market.store import SQLiteBarStore
from options.research.history_store import OptionsHistoryStore
from options.research.archive import session_labels as _classify_sessions
from options.research.universe import (
    history_source_ranges,
    history_start_date,
    history_underlyings,
    load_history_universe,
)


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)


ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = ROOT / ".env"
RESEARCH_DIR = ROOT / "data" / "options" / "research" / "theta"
LOCK_PATH = RESEARCH_DIR / "theta_backfill.lock"
FIRST_DATA_DATE = date(2020, 1, 1)
REQUIRED_RESEARCH_SESSIONS = 1013
MIN_EXPLORATORY_RESEARCH_SESSIONS = 60
MAX_RESEARCH_DTE = 60
STRIKE_RANGE = 20
ET = ZoneInfo("America/New_York")
# Per-symbol start dates and historical ticker aliases live in
# options/research/universe.json. Unlisted symbols use FIRST_DATA_DATE; explicit
# no-data responses are archived as empty months so sparse history cannot stall
# the whole universe.


def _read_api_key() -> str:
    key = os.environ.get("THETA_DATA_API_KEY", "").strip() or os.environ.get("THETADATA_API_KEY", "").strip()
    if key:
        return key
    if ENV_PATH.is_file():
        for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("export "):
                line = line[7:].lstrip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() not in {"THETA_DATA_API_KEY", "THETADATA_API_KEY"}:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            return value.strip()
    raise RuntimeError("ThetaData API key is missing; set THETA_DATA_API_KEY in .env")


def _theta_client(api_key: str):
    try:
        from thetadata import ThetaClient
    except ImportError as error:
        raise RuntimeError("ThetaData client is not installed; install the optional theta dependency group") from error
    return ThetaClient(api_key=api_key, dataframe_type="polars")


def _is_no_data_error(error: Exception) -> bool:
    """ThetaData represents a valid empty result with NoDataFoundError."""
    try:
        from thetadata.errors import NoDataFoundError
    except ImportError:
        return False
    return isinstance(error, NoDataFoundError)


def _records(frame: Any) -> list[dict]:
    if frame is None:
        return []
    if hasattr(frame, "to_dicts"):
        return frame.to_dicts()
    if hasattr(frame, "to_dict"):
        try:
            result = frame.to_dict(orient="records")
        except TypeError:
            result = frame.to_dict()
        if isinstance(result, list):
            return [row for row in result if isinstance(row, dict)]
        if isinstance(result, dict):
            keys = list(result)
            count = len(result[keys[0]]) if keys and hasattr(result[keys[0]], "__len__") else 0
            return [{key: result[key][index] for key in keys} for index in range(count)]
    if isinstance(frame, list):
        return [row for row in frame if isinstance(row, dict)]
    return []


def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "item"):
        try:
            return _json_value(value.item())
        except (TypeError, ValueError):
            pass
    return str(value)


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _nonnegative_int(value: Any) -> int | None:
    number = _finite(value)
    return int(number) if number is not None and number >= 0 else None


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False,
    ) as stream:
        temp = Path(stream.name)
        json.dump(_json_value(payload), stream, sort_keys=True, separators=(",", ":"), allow_nan=False, default=_json_value)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def _atomic_gzip_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as raw:
        temp = Path(raw.name)
    try:
        with gzip.open(temp, "wt", encoding="utf-8", compresslevel=6) as stream:
            json.dump(_json_value(payload), stream, sort_keys=True, separators=(",", ":"), allow_nan=False, default=_json_value)
            stream.write("\n")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _month_chunks(start: date, end: date, *, newest_first: bool = False):
    cursor = date(start.year, start.month, 1)
    chunks = []
    while cursor <= end:
        if cursor.month == 12:
            next_month = date(cursor.year + 1, 1, 1)
        else:
            next_month = date(cursor.year, cursor.month + 1, 1)
        chunk_end = min(end, next_month - timedelta(days=1))
        chunks.append((max(cursor, start), chunk_end))
        cursor = next_month
    yield from reversed(chunks) if newest_first else chunks


def _row_date(row: dict, *fields: str) -> date | None:
    for field in fields:
        value = row.get(field)
        if value is None:
            continue
        if isinstance(value, datetime):
            stamp = value.astimezone(ET) if value.tzinfo else value.replace(tzinfo=ET)
            return stamp.date()
        if isinstance(value, date):
            return value
        text = str(value).strip()
        try:
            stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if stamp.tzinfo:
                stamp = stamp.astimezone(ET)
            return stamp.date()
        except ValueError:
            try:
                return date.fromisoformat(text[:10])
            except ValueError:
                continue
    return None


def _load_gzip_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        payload = json.load(stream)
    rows = payload.get("rows", []) if isinstance(payload, dict) else []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _archive_covers_period(path: Path, start_date: date, end_date: date) -> bool:
    """Return whether an archive contains the complete requested date interval."""
    if not path.is_file():
        return False
    try:
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            payload = json.load(stream)
        archived_start = date.fromisoformat(str(payload.get("start_date", "")))
        archived_end = date.fromisoformat(str(payload.get("end_date", "")))
    except (OSError, EOFError, json.JSONDecodeError, TypeError, ValueError):
        return False
    return archived_start <= start_date and archived_end >= end_date


def _source_symbol_ranges(symbol: str, start_date: date, end_date: date) -> list[tuple[str, date, date]]:
    """Map canonical underlyings to the symbols ThetaData used at the time."""
    return history_source_ranges(symbol, start_date, end_date)


def _archive_matches_source_ranges(path: Path, source_ranges: list[tuple[str, date, date]]) -> bool:
    """Check whether an archive month was fetched with its expected source tickers."""
    if not path.is_file():
        return False
    expected = [
        {"symbol": symbol, "start_date": start.isoformat(), "end_date": end.isoformat()}
        for symbol, start, end in source_ranges
    ]
    try:
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, EOFError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("source_symbol_ranges") == expected


def _symbol_has_archive_gaps(symbol: str, *, start_date: date, end_date: date) -> bool:
    """Detect months missing either endpoint so reruns resume unfinished symbols first."""
    archive = RESEARCH_DIR / symbol / "archive"
    eod_dir, oi_dir = archive / "eod", archive / "open_interest"
    return any(
        not (eod_dir / f"{month_start:%Y-%m}.json.gz").is_file()
        or not (oi_dir / f"{month_start:%Y-%m}.json.gz").is_file()
        for month_start, _month_end in _month_chunks(start_date, end_date)
    )


def _effective_start_date(symbol: str, start_date: date) -> date:
    """Respect a symbol's earliest available option-history month."""
    return history_start_date(symbol, start_date)


def _contract_key(row: dict) -> tuple[str, float, str]:
    expiration = str(row.get("expiration") or row.get("expiration_date") or "")[:10]
    strike = _finite(row.get("strike")) or 0.0
    right = str(row.get("right") or row.get("type") or "").lower()
    if right in {"c", "call"}:
        right = "call"
    elif right in {"p", "put"}:
        right = "put"
    return expiration, strike, right


def _load_stock_rows_from_db(symbol: str, *, start_date: date, end_date: date) -> list[dict]:
    """Read raw spots and split-adjusted closes from the shared Alpaca SQLite store."""
    store = SQLiteBarStore()
    query = {
        "symbols": [symbol], "feed": "sip", "timeframe": "1Day",
        "start": start_date.isoformat(), "end": end_date.isoformat(),
    }
    raw_bars = store.bars(**query, adjustment="raw").get(symbol, [])
    split_bars = store.bars(**query, adjustment="split").get(symbol, [])

    def by_day(rows: list[dict]) -> dict[date, dict]:
        output = {}
        for row in rows:
            try:
                day = date.fromisoformat(str(row.get("t") or "")[:10])
            except ValueError:
                continue
            output[day] = row
        return output

    raw_by_day, split_by_day = by_day(raw_bars), by_day(split_bars)
    if not raw_by_day:
        raise RuntimeError(f"No raw daily equity bars in SQLite for {symbol}; run the Alpaca history backfill first")
    missing_split = sorted(set(raw_by_day) - set(split_by_day))
    if missing_split:
        raise RuntimeError(f"Missing split-adjusted daily bars in SQLite for {symbol}: {missing_split[0]}")
    output = []
    for day, raw in sorted(raw_by_day.items()):
        split = split_by_day[day]
        output.append({
            "last_trade": raw["t"],
            "open": raw["o"],
            "close": raw["c"],
            "volume": raw["v"],
            "split_adjusted_close": split["c"],
        })
    return output


def _session_labels(closes: dict[date, float]) -> dict[date, tuple[str, str]]:
    """Create point-in-time trend and volatility labels from adjusted closes."""
    return _classify_sessions(closes)


def _compile_symbol(symbol: str, *, start_date: date, end_date: date) -> dict:
    symbol_dir = RESEARCH_DIR / symbol
    eod_dir, oi_dir = symbol_dir / "archive" / "eod", symbol_dir / "archive" / "open_interest"
    stock_rows = _load_stock_rows_from_db(symbol, start_date=start_date, end_date=end_date)
    spot_by_date: dict[date, float] = {}
    equity_volume_by_date: dict[date, int] = {}
    adjusted_closes: dict[date, float] = {}
    for row in stock_rows:
        day = _row_date(row, "last_trade", "created", "timestamp")
        close = _finite(row.get("close"))
        adjusted_close = _finite(row.get("split_adjusted_close"))
        equity_volume = _nonnegative_int(row.get("volume"))
        if day and start_date <= day <= end_date and close and close > 0 and adjusted_close and adjusted_close > 0:
            spot_by_date[day] = close
            adjusted_closes[day] = adjusted_close
            if equity_volume is not None:
                equity_volume_by_date[day] = equity_volume
    split_count = 0
    session_labels = _session_labels(adjusted_closes)

    eod_by_date: dict[date, list[dict]] = {}
    oi_by_date: dict[date, dict[tuple[str, float, str], int]] = {}
    for path in sorted(eod_dir.glob("????-??.json.gz")) if eod_dir.exists() else ():
        for row in _load_gzip_rows(path):
            day = _row_date(row, "created", "date", "last_trade")
            if day and start_date <= day <= end_date:
                eod_by_date.setdefault(day, []).append(row)
    for path in sorted(oi_dir.glob("????-??.json.gz")) if oi_dir.exists() else ():
        for row in _load_gzip_rows(path):
            day = _row_date(row, "timestamp", "date")
            oi = row.get("open_interest")
            try:
                oi_value = int(oi) if oi is not None else None
            except (TypeError, ValueError, OverflowError):
                oi_value = None
            if day and start_date <= day <= end_date and oi_value is not None:
                oi_by_date.setdefault(day, {})[_contract_key(row)] = oi_value

    rows = []
    contracts_total = 0
    for day in sorted(eod_by_date):
        spot = spot_by_date.get(day)
        if not spot or spot <= 0:
            continue
        as_of = datetime.combine(day, time(17, 15), tzinfo=ET)
        oi_map = oi_by_date.get(day, {})
        contracts = []
        seen = set()
        for raw in eod_by_date[day]:
            key = _contract_key(raw)
            expiration_text, strike, right = key
            source_symbol = str(raw.get("symbol") or "").strip().upper()
            bid, ask = _finite(raw.get("bid")), _finite(raw.get("ask"))
            if (
                not source_symbol or not expiration_text or strike <= 0
                or right not in {"call", "put"} or bid is None or ask is None
                or bid < 0 or ask <= 0 or ask < bid or key in seen
            ):
                continue
            try:
                expiration = date.fromisoformat(expiration_text)
            except ValueError:
                continue
            dte = (expiration - day).days
            if dte < 0 or dte > MAX_RESEARCH_DTE:
                continue
            seen.add(key)
            # Theta's v3 rows identify contracts by (root, expiry, strike,
            # right); the `symbol` field can therefore be the root alone.
            # The research model needs a unique per-contract key for quotes.
            contract_symbol = f"THETA:{symbol}:{expiration:%Y%m%d}:{right[0].upper()}:{strike:g}"
            quote_day = _row_date(raw, "created") or day
            quote_stamp = datetime.combine(quote_day, time(17, 15), tzinfo=ET)
            contracts.append({
                "symbol": contract_symbol,
                "source_symbol": source_symbol,
                "underlying_symbol": symbol,
                "expiration_date": expiration_text,
                "type": right,
                "strike_price": strike,
                "size": 100,
                "style": "american",
                "status": "active",
                "tradable": True,
                "open_interest": oi_map.get(key),
                "volume": _nonnegative_int(raw.get("volume")),
                "bid": bid,
                "ask": ask,
                "quote_at": quote_stamp.isoformat(),
                "iv": None,
                "greeks": {},
            })
        if len(contracts) < 20:
            continue
        contracts_total += len(contracts)
        rows.append({
            "underlying": symbol,
            "spot": spot,
            "underlying_close": adjusted_closes.get(day, spot),
            "equity_volume": equity_volume_by_date.get(day),
            "as_of": as_of.isoformat(),
            "feed": "thetadata_historical_eod",
            "regime": session_labels.get(day, ("range_bound", "normal"))[0],
            "volatility_state": session_labels.get(day, ("range_bound", "normal"))[1],
            "contracts": contracts,
        })

    history_store = OptionsHistoryStore()
    database_sync = history_store.write_snapshots(
        symbol, "theta", rows, replace_source=True,
    )
    archive_chunks = list(_month_chunks(start_date, end_date))
    archive_complete = all(
        _archive_covers_period(eod_dir / f"{month_start:%Y-%m}.json.gz", month_start, month_end)
        and _archive_covers_period(oi_dir / f"{month_start:%Y-%m}.json.gz", month_start, month_end)
        for month_start, month_end in archive_chunks
    )
    standard_research_ready = len(rows) >= REQUIRED_RESEARCH_SESSIONS
    exploratory_research_ready = (
        archive_complete and len(rows) >= MIN_EXPLORATORY_RESEARCH_SESSIONS
    )
    manifest = {
        "provider": "ThetaData",
        "data_type": "historical option EOD NBBO and open interest",
        "status": (
            "research_ready" if standard_research_ready
            else "exploratory_ready" if exploratory_research_ready
            else "collecting"
        ),
        "archive_complete": archive_complete,
        "archive_start_date": start_date.isoformat(),
        "archive_end_date": end_date.isoformat(),
        "archive_months_complete": sum(
            _archive_covers_period(eod_dir / f"{month_start:%Y-%m}.json.gz", month_start, month_end)
            and _archive_covers_period(oi_dir / f"{month_start:%Y-%m}.json.gz", month_start, month_end)
            for month_start, month_end in archive_chunks
        ),
        "archive_months_expected": len(archive_chunks),
        "underlying": symbol,
        "dataset_path": str(history_store.path),
        "history_database_path": str(history_store.path),
        "database_sync_status": database_sync["status"],
        "database_sessions": database_sync.get("sessions", 0),
        "database_liquidity_coverage": {
            key: database_sync.get(key, 0)
            for key in (
                "equity_volume_sessions", "option_volume_sessions",
                "option_volume_sessions_80pct_coverage",
                "open_interest_sessions_80pct_coverage",
                "option_spread_sessions_80pct_coverage",
            )
        },
        "dataset_sessions": len(rows),
        "research_sessions_required": REQUIRED_RESEARCH_SESSIONS,
        "standard_research_ready": standard_research_ready,
        "exploratory_research_sessions_required": MIN_EXPLORATORY_RESEARCH_SESSIONS,
        "exploratory_research_ready": exploratory_research_ready,
        "contracts_in_dataset": contracts_total,
        "contract_dte_filter": {"min": 0, "max": MAX_RESEARCH_DTE},
        "strike_range": STRIKE_RANGE,
        "split_events_applied": split_count,
        "underlying_close_adjustment": "Alpaca split-adjusted SQLite close for realized volatility; raw close retained as spot",
        "split_adjustment_source": "market_bar SQLite table (Alpaca SIP daily bars)",
        "regime_labels": "50/200-session trend from split-adjusted underlying closes",
        "volatility_labels": "top quartile of trailing 252-session 20-day realized volatility",
        "quote_date_start": rows[0]["as_of"][:10] if rows else None,
        "quote_date_end": rows[-1]["as_of"][:10] if rows else None,
        "generated_at": datetime.now(ET).isoformat(),
    }
    _atomic_json(symbol_dir / "manifest.json", manifest)
    return manifest


def _fetch_symbol(
    client,
    symbol: str,
    *,
    start_date: date,
    end_date: date,
    compile_after: bool = True,
) -> dict | None:
    symbol_dir = RESEARCH_DIR / symbol
    archive = symbol_dir / "archive"
    eod_dir, oi_dir = archive / "eod", archive / "open_interest"
    eod_dir.mkdir(parents=True, exist_ok=True)
    oi_dir.mkdir(parents=True, exist_ok=True)

    chunks = list(_month_chunks(start_date, end_date, newest_first=True))
    for index, (month_start, month_end) in enumerate(chunks, start=1):
        month_key = month_start.strftime("%Y-%m")
        eod_path, oi_path = eod_dir / f"{month_key}.json.gz", oi_dir / f"{month_key}.json.gz"
        source_ranges = _source_symbol_ranges(symbol, month_start, month_end)
        if eod_path.is_file() and oi_path.is_file():
            # Repair archives created before historical ticker aliases were
            # configured so quotes are not silently fetched under the wrong root.
            needs_alias_repair = (
                any(source_symbol != symbol for source_symbol, _, _ in source_ranges)
                and (
                    not _archive_matches_source_ranges(eod_path, source_ranges)
                    or not _archive_matches_source_ranges(oi_path, source_ranges)
                )
            )
            archive_covers_month = (
                _archive_covers_period(eod_path, month_start, month_end)
                and _archive_covers_period(oi_path, month_start, month_end)
            )
            if not needs_alias_repair and archive_covers_month:
                continue
        eod, oi = [], []
        no_data_ranges = []
        for source_symbol, source_start, source_end in source_ranges:
            request = {
                "symbol": source_symbol,
                "start_date": source_start,
                "end_date": source_end,
                "expiration": "*",
                "max_dte": MAX_RESEARCH_DTE,
                "strike_range": STRIKE_RANGE,
            }
            # Value permits two account-wide requests. Fetch EOD quotes and OI
            # together to use that entitlement without queuing larger batches.
            with ThreadPoolExecutor(max_workers=2) as pool:
                eod_future = pool.submit(client.option_history_eod, **request)
                oi_future = pool.submit(client.option_history_open_interest, **request)
                for endpoint, future, rows in (
                    ("eod", eod_future, eod),
                    ("open_interest", oi_future, oi),
                ):
                    try:
                        rows.extend(_records(future.result()))
                    except Exception as error:
                        if not _is_no_data_error(error):
                            raise
                        no_data_ranges.append({
                            "endpoint": endpoint,
                            "source_symbol": source_symbol,
                            "start_date": source_start.isoformat(),
                            "end_date": source_end.isoformat(),
                        })
                        print(
                            f"ThetaData {symbol} {month_key} ({source_symbol} "
                            f"{source_start}..{source_end}): {endpoint} returned no data; "
                            "recording an empty archive for that endpoint"
                        )
        for path, endpoint, rows in (
            (eod_path, "option_history_eod", eod),
            (oi_path, "option_history_open_interest", oi),
        ):
            _atomic_gzip_json(path, {
                "provider": f"thetadata_{endpoint}",
                "underlying": symbol,
                "start_date": month_start.isoformat(),
                "end_date": month_end.isoformat(),
                "source_symbol_ranges": [
                    {"symbol": ticker, "start_date": start.isoformat(), "end_date": end.isoformat()}
                    for ticker, start, end in source_ranges
                ],
                "retrieved_at": datetime.now(ET).isoformat(),
                "no_data_ranges": no_data_ranges,
                "rows": rows,
            })
        print(f"ThetaData {symbol} {month_key}: EOD rows={len(eod)}, OI rows={len(oi)}")
        # Publish a usable recent-history dataset as soon as a ticker has
        # accumulated enough months, then refresh it periodically while the
        # deeper archive continues. Dataset replacement is atomic.
        if compile_after and index >= 48 and (index in {48, 49} or index % 12 == 0):
            partial = _compile_symbol(symbol, start_date=start_date, end_date=end_date)
            print(
                f"ThetaData {symbol} partial compile: sessions={partial['dataset_sessions']}/"
                f"{REQUIRED_RESEARCH_SESSIONS}, status={partial['status']}"
            )

    if not compile_after:
        return None
    manifest = _compile_symbol(symbol, start_date=start_date, end_date=end_date)
    print(
        f"ThetaData {symbol}: sessions={manifest['dataset_sessions']}/"
        f"{REQUIRED_RESEARCH_SESSIONS}, contracts={manifest['contracts_in_dataset']}, "
        f"status={manifest['status']}"
    )
    return manifest


def _research_symbols() -> list[str]:
    symbols = history_underlyings()
    priority = ["SPY", "QQQ", "GLD", "TLT"]
    return sorted(symbols, key=lambda symbol: (priority.index(symbol) if symbol in priority else len(priority), symbols.index(symbol)))


def _recent_session_window(end_date: date, session_count: int) -> tuple[date, date]:
    """Use the latest completed SPY sessions as the shared options priority window."""
    if session_count < 1:
        raise ValueError("recent session count must be positive")
    lookback_start = end_date - timedelta(days=max(30, session_count * 4))
    bars = SQLiteBarStore().bars(
        symbols=["SPY"], feed="sip", timeframe="1Day",
        start=lookback_start.isoformat(), end=end_date.isoformat(), adjustment="raw",
    ).get("SPY", [])
    sessions = sorted({
        date.fromisoformat(str(row.get("t", ""))[:10])
        for row in bars
        if row.get("t") and date.fromisoformat(str(row["t"])[:10]) <= end_date
    })
    if not sessions:
        raise RuntimeError(f"No SPY SIP daily bars available to plan the last {session_count} sessions")
    if len(sessions) < session_count:
        print(
            f"ThetaData priority window: only {len(sessions)} SPY sessions are available "
            f"through {sessions[-1]}; using all of them"
        )
        return sessions[0], sessions[-1]
    return sessions[-session_count], sessions[-1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill daily ThetaData option chains for Algo symbols")
    parser.add_argument(
        "--underlying", action="append",
        help="symbol to collect (repeatable); defaults to the options research history universe",
    )
    parser.add_argument("--start-date", type=date.fromisoformat, default=FIRST_DATA_DATE)
    parser.add_argument(
        "--recent-sessions", type=int, default=20,
        help="SPY trading sessions to fetch for every symbol before deeper history; use 0 to disable",
    )
    parser.add_argument(
        "--end-date", type=date.fromisoformat,
        default=datetime.now(ET).date() - timedelta(days=1),
    )
    args = parser.parse_args()
    if args.end_date < args.start_date:
        parser.error("--end-date must be on or after --start-date")
    if args.recent_sessions < 0:
        parser.error("--recent-sessions must be zero or greater")
    symbols = [item.strip().upper() for item in args.underlying] if args.underlying else _research_symbols()
    if not symbols or any(not item or not item.replace(".", "").isalnum() for item in symbols):
        parser.error("underlying symbols must be non-empty alphanumeric ticker names")

    try:
        RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
        with LOCK_PATH.open("w", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("ThetaData backfill is already running; this invocation skipped.")
                return 0
            return _run_symbols(
                symbols, args.start_date, args.end_date,
                recent_sessions=args.recent_sessions,
            )
    except Exception as error:
        message = str(error)
        try:
            key = _read_api_key()
            message = message.replace(key, "[redacted]")
        except Exception:
            pass
        print(f"ThetaData collector stopped: {type(error).__name__}: {message[:400]}")
        return 1


def _run_symbols(
    symbols: list[str],
    start_date: date,
    end_date: date,
    *,
    recent_sessions: int = 20,
    on_recent_complete: Callable[[], None] | None = None,
) -> int:
    api_key = _read_api_key()
    client = _theta_client(api_key)
    manifests = []
    ordered_symbols = list(dict.fromkeys(symbols))
    original_order = {symbol: index for index, symbol in enumerate(ordered_symbols)}
    ordered_symbols.sort(key=lambda symbol: (
        not _symbol_has_archive_gaps(
            symbol, start_date=_effective_start_date(symbol, start_date), end_date=end_date,
        ),
        original_order[symbol],
    ))
    if ordered_symbols != list(dict.fromkeys(symbols)):
        print(f"ThetaData resume order: {', '.join(ordered_symbols)}")
    universe = load_history_universe()
    notes = universe.get("history_notes", {})
    expectations = []
    exception_symbols = set()
    for symbol in ordered_symbols:
        symbol_start = _effective_start_date(symbol, start_date)
        source_ranges = _source_symbol_ranges(symbol, symbol_start, end_date)
        has_alias = any(source_symbol != symbol for source_symbol, _, _ in source_ranges)
        note = notes.get(symbol) if isinstance(notes, dict) else None
        if symbol_start > start_date or has_alias or note:
            exception_symbols.add(symbol)
            sources = " -> ".join(
                f"{source_symbol} {source_start}..{source_end}"
                for source_symbol, source_start, source_end in source_ranges
            )
            expectation = f"{symbol}: {sources}"
            if note:
                expectation += f" ({note})"
            expectations.append(expectation)
    if expectations:
        default_count = len(ordered_symbols) - len(exception_symbols)
        print(
            f"ThetaData history plan: default {start_date} for {default_count} symbols; "
            f"special history for {len(exception_symbols)} symbols: {'; '.join(expectations)}"
        )

    if recent_sessions:
        priority_start, priority_end = _recent_session_window(end_date, recent_sessions)
        print(
            f"ThetaData recent-history priority: {recent_sessions} SPY sessions "
            f"from {priority_start} through {priority_end}; collecting all symbols before older history"
        )
        priority_errors = []
        for symbol in ordered_symbols:
            symbol_start_date = _effective_start_date(symbol, start_date)
            symbol_priority_start = max(symbol_start_date, priority_start)
            if symbol_priority_start > priority_end:
                print(
                    f"ThetaData {symbol}: no public-history window by {priority_end}; "
                    "skipping recent priority pass"
                )
                continue
            try:
                _fetch_symbol(
                    client,
                    symbol,
                    start_date=symbol_priority_start,
                    end_date=priority_end,
                    compile_after=False,
                )
            except Exception as error:
                message = str(error).replace(api_key, "[redacted]")
                print(f"ThetaData recent pass {symbol} stopped: {type(error).__name__}: {message[:400]}")
                priority_errors.append(symbol)
        if priority_errors:
            print(f"ThetaData recent pass incomplete; will retry: {', '.join(priority_errors)}")
            return 1

        compile_errors = []
        for symbol in ordered_symbols:
            symbol_start_date = _effective_start_date(symbol, start_date)
            if symbol_start_date > priority_end:
                continue
            try:
                manifest = _compile_symbol(
                    symbol, start_date=symbol_start_date, end_date=priority_end,
                )
                print(
                    f"ThetaData {symbol} recent dataset: sessions={manifest['dataset_sessions']}, "
                    f"status={manifest['status']}"
                )
            except Exception as error:
                message = str(error).replace(api_key, "[redacted]")
                print(f"ThetaData recent compile {symbol} stopped: {type(error).__name__}: {message[:400]}")
                compile_errors.append(symbol)
        if compile_errors:
            print(f"ThetaData recent compile incomplete; will retry: {', '.join(compile_errors)}")
            return 1

        print("ThetaData recent-history pass complete; beginning deeper archive backfill")
        if on_recent_complete is not None:
            on_recent_complete()

    for symbol in ordered_symbols:
        try:
            symbol_start_date = _effective_start_date(symbol, start_date)
            if symbol_start_date > start_date:
                print(f"ThetaData {symbol}: starting archive at {symbol_start_date} (earliest available history)")
            manifests.append(_fetch_symbol(
                client, symbol, start_date=symbol_start_date, end_date=end_date,
            ))
        except Exception as error:
            message = str(error).replace(api_key, "[redacted]")
            print(f"ThetaData {symbol} stopped: {type(error).__name__}: {message[:400]}")
            return 1
    print(json.dumps({
        "provider": "ThetaData",
        "underlyings": len(manifests),
        "research_ready": [row["underlying"] for row in manifests if row["status"] == "research_ready"],
        "exploratory_ready": [row["underlying"] for row in manifests if row["status"] == "exploratory_ready"],
        "archive_complete": [row["underlying"] for row in manifests if row.get("archive_complete")],
        "collecting": [row["underlying"] for row in manifests if row["status"] == "collecting"],
        "manifest_dir": str(RESEARCH_DIR),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
