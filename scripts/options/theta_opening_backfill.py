#!/usr/bin/env python3
"""Backfill one point-in-time ThetaData option chain near the market open."""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta
import fcntl
import gzip
import json
import os
from pathlib import Path
import tempfile
from typing import Any
from zoneinfo import ZoneInfo

from options.research.archive import session_labels
from options.research.history_store import OptionsHistoryStore
from scripts.options.theta_historical_backfill import (
    FIRST_DATA_DATE,
    LOCK_PATH,
    MAX_RESEARCH_DTE,
    RESEARCH_DIR,
    STRIKE_RANGE,
    _archive_covers_through,
    _atomic_gzip_json,
    _atomic_json,
    _contract_key,
    _download_stock_rows,
    _effective_start_date,
    _finite,
    _json_value,
    _load_gzip_rows,
    _month_chunks,
    _research_symbols,
    _read_api_key,
    _records,
    _row_date,
    _source_symbol_ranges,
    _split_adjusted_history,
    _theta_client,
)


ET = ZoneInfo("America/New_York")
OPENING_TIME = time(9, 35)
MINIMUM_CHAIN_CONTRACTS = 20


def _source_ranges_match(path: Path, ranges: list[tuple[str, date, date]]) -> bool:
    if not path.is_file():
        return False
    expected = [
        {"symbol": source, "start_date": start.isoformat(), "end_date": end.isoformat()}
        for source, start, end in ranges
    ]
    try:
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, EOFError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("source_symbol_ranges") == expected


def _quote_timestamp(row: dict, day: date) -> str:
    value = row.get("timestamp") or row.get("created")
    if value is None:
        return datetime.combine(day, OPENING_TIME, tzinfo=ET).isoformat()
    if isinstance(value, datetime):
        stamp = value if value.tzinfo else value.replace(tzinfo=ET)
        return stamp.isoformat()
    text = str(value).strip()
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return datetime.combine(day, OPENING_TIME, tzinfo=ET).isoformat()
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=ET)
    return stamp.isoformat()


def _download_open_quotes(
    client: Any,
    ranges: list[tuple[str, date, date]],
    session_dates: list[date],
) -> tuple[list[dict], list[dict]]:
    requests_to_make = [
        (source_symbol, day)
        for source_symbol, start, end in ranges
        for day in session_dates
        if start <= day <= end
    ]

    def request(source_symbol: str, day: date) -> tuple[list[dict], dict | None]:
        try:
            frame = client.option_at_time_quote(
                symbol=source_symbol,
                start_date=day,
                end_date=day,
                time_of_day=OPENING_TIME.isoformat(),
                expiration="*",
                max_dte=MAX_RESEARCH_DTE,
                strike_range=STRIKE_RANGE,
            )
        except Exception as error:
            details = getattr(error, "details", None)
            details = details() if callable(details) else details
            message = f"{error} {details or ''}"
            if (
                type(error).__name__.lower() == "nodatafounderror"
                or "no data found for: option_at_time_quote" in message.lower()
                or "wrong number of data fields" in message.lower()
            ):
                return [], {
                    "symbol": source_symbol,
                    "date": day.isoformat(),
                    "reason": message[:240],
                }
            raise
        return _records(frame), None

    if not requests_to_make:
        return [], []
    rows = []
    no_data_requests = []
    for source, day in requests_to_make:
        day_rows, no_data = request(source, day)
        rows.extend(day_rows)
        if no_data:
            no_data_requests.append(no_data)
            print(
                f"ThetaData opening skipped missing/malformed quote day "
                f"{no_data['symbol']} {no_data['date']}",
                flush=True,
            )
    return rows, no_data_requests


def _compile_opening_symbol(symbol: str, *, start_date: date, end_date: date) -> dict:
    symbol_dir = RESEARCH_DIR / symbol
    archive_dir = symbol_dir / "archive"
    opening_dir = archive_dir / "opening_quote"
    oi_dir = archive_dir / "open_interest"

    stock_rows = _load_gzip_rows(archive_dir / "stock_eod.json.gz")
    if not stock_rows or not any(row.get("open") is not None for row in stock_rows):
        stock_rows = _download_stock_rows(symbol, start_date=start_date, end_date=end_date)
        _atomic_gzip_json(archive_dir / "stock_eod.json.gz", {
            "provider": "yfinance_daily_bars_and_split_actions",
            "underlying": symbol,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "retrieved_at": datetime.now(ET).isoformat(),
            "rows": stock_rows,
        })

    open_by_date: dict[date, float] = {}
    close_by_date: dict[date, float] = {}
    for row in stock_rows:
        day = _row_date(row, "last_trade", "created", "timestamp")
        opening, close = _finite(row.get("open")), _finite(row.get("close"))
        if day and start_date <= day <= end_date:
            if opening and opening > 0:
                open_by_date[day] = opening
            if close and close > 0:
                close_by_date[day] = close
    adjusted_closes, split_count = _split_adjusted_history(close_by_date, stock_rows)
    close_labels = session_labels(adjusted_closes)

    open_rows_by_date: dict[date, list[dict]] = {}
    for path in sorted(opening_dir.glob("????-??.json.gz")) if opening_dir.exists() else ():
        for row in _load_gzip_rows(path):
            day = _row_date(row, "timestamp", "created", "date")
            if day and start_date <= day <= end_date:
                open_rows_by_date.setdefault(day, []).append(row)

    oi_by_date: dict[date, dict[tuple[str, float, str], int]] = {}
    for path in sorted(oi_dir.glob("????-??.json.gz")) if oi_dir.exists() else ():
        for row in _load_gzip_rows(path):
            day = _row_date(row, "timestamp", "date")
            try:
                value = int(row["open_interest"])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if day and start_date <= day <= end_date:
                oi_by_date.setdefault(day, {})[_contract_key(row)] = value

    snapshots = []
    total_contracts = 0
    for day in sorted(open_rows_by_date):
        spot = open_by_date.get(day)
        if not spot or spot <= 0:
            continue
        previous_days = [known_day for known_day in adjusted_closes if known_day < day]
        previous_day = max(previous_days) if previous_days else None
        previous_close = adjusted_closes[previous_day] if previous_day else spot
        previous_label = close_labels.get(previous_day) if previous_day else None
        # The same-day OI archive is an end-of-day observation and would leak
        # information unavailable at the opening snapshot. Use prior-session OI.
        oi_map = oi_by_date.get(previous_day, {}) if previous_day else {}
        contracts = []
        seen = set()
        for raw in open_rows_by_date[day]:
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
            contracts.append({
                "symbol": f"THETA:{symbol}:{expiration:%Y%m%d}:{right[0].upper()}:{strike:g}",
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
                "bid": bid,
                "ask": ask,
                "quote_at": _quote_timestamp(raw, day),
                "iv": None,
                "greeks": {},
            })
        if len(contracts) < MINIMUM_CHAIN_CONTRACTS:
            continue
        total_contracts += len(contracts)
        as_of = datetime.combine(day, OPENING_TIME, tzinfo=ET)
        snapshots.append({
            "underlying": symbol,
            "spot": spot,
            "underlying_close": previous_close,
            "as_of": as_of.isoformat(),
            "snapshot_phase": "opening",
            "target_time": OPENING_TIME.isoformat(),
            "feed": "thetadata_historical_opening",
            "regime": previous_label[0] if previous_label else "range_bound",
            "volatility_state": previous_label[1] if previous_label else "normal",
            "contracts": contracts,
        })

    history_store = OptionsHistoryStore()
    database_sync = history_store.write_snapshots(
        symbol, "theta_opening", snapshots, replace_source=True,
    )

    months = list(_month_chunks(start_date, end_date))
    archive_complete = all(
        _archive_covers_through(opening_dir / f"{month_start:%Y-%m}.json.gz", month_end)
        and _source_ranges_match(
            opening_dir / f"{month_start:%Y-%m}.json.gz",
            _source_symbol_ranges(symbol, month_start, month_end),
        )
        for month_start, month_end in months
    )
    manifest = {
        "provider": "ThetaData",
        "data_type": "historical opening option NBBO and open interest",
        "underlying": symbol,
        "snapshot_time_et": OPENING_TIME.isoformat(),
        "archive_start_date": start_date.isoformat(),
        "archive_end_date": end_date.isoformat(),
        "archive_complete": archive_complete,
        "archive_months_complete": sum(
            _archive_covers_through(opening_dir / f"{month_start:%Y-%m}.json.gz", month_end)
            and _source_ranges_match(
                opening_dir / f"{month_start:%Y-%m}.json.gz",
                _source_symbol_ranges(symbol, month_start, month_end),
            )
            for month_start, month_end in months
        ),
        "archive_months_expected": len(months),
        "dataset_path": str(history_store.path),
        "history_database_path": str(history_store.path),
        "database_sync_status": database_sync["status"],
        "database_sessions": database_sync["sessions"],
        "dataset_sessions": len(snapshots),
        "contracts_in_dataset": total_contracts,
        "contract_dte_filter": {"min": 0, "max": MAX_RESEARCH_DTE},
        "strike_range": STRIKE_RANGE,
        "underlying_spot": "daily opening price from yfinance; quote snapshot requested at 09:35 ET",
        "underlying_close": "previous split-adjusted session close to avoid same-day lookahead",
        "open_interest": "prior-session end-of-day open interest to avoid same-day lookahead",
        "split_events_applied": split_count,
        "quote_date_start": snapshots[0]["as_of"][:10] if snapshots else None,
        "quote_date_end": snapshots[-1]["as_of"][:10] if snapshots else None,
        "generated_at": datetime.now(ET).isoformat(),
    }
    _atomic_json(symbol_dir / "opening_manifest.json", manifest)
    return manifest


def _fetch_symbol(client: Any, symbol: str, *, start_date: date, end_date: date) -> dict:
    symbol_dir = RESEARCH_DIR / symbol
    archive_dir = symbol_dir / "archive"
    opening_dir = archive_dir / "opening_quote"
    opening_dir.mkdir(parents=True, exist_ok=True)

    stock_rows = _download_stock_rows(symbol, start_date=start_date, end_date=end_date)
    _atomic_gzip_json(archive_dir / "stock_eod.json.gz", {
        "provider": "yfinance_daily_bars_and_split_actions",
        "underlying": symbol,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "retrieved_at": datetime.now(ET).isoformat(),
        "rows": stock_rows,
    })

    chunks = list(_month_chunks(start_date, end_date, newest_first=True))
    for index, (month_start, month_end) in enumerate(chunks, start=1):
        path = opening_dir / f"{month_start:%Y-%m}.json.gz"
        source_ranges = _source_symbol_ranges(symbol, month_start, month_end)
        refresh = month_end == end_date
        if path.is_file() and _source_ranges_match(path, source_ranges):
            if not refresh or _archive_covers_through(path, month_end):
                continue

        session_dates = set()
        for stock_row in stock_rows:
            day = _row_date(stock_row, "last_trade", "created", "timestamp")
            opening = _finite(stock_row.get("open"))
            if day and month_start <= day <= month_end and opening and opening > 0:
                session_dates.add(day)
        session_dates = sorted(session_dates)
        rows, no_data_requests = _download_open_quotes(client, source_ranges, session_dates)
        _atomic_gzip_json(path, {
            "provider": "thetadata_option_at_time_quote",
            "underlying": symbol,
            "time_of_day": OPENING_TIME.isoformat(),
            "start_date": month_start.isoformat(),
            "end_date": month_end.isoformat(),
            "source_symbol_ranges": [
                {"symbol": ticker, "start_date": start.isoformat(), "end_date": end.isoformat()}
                for ticker, start, end in source_ranges
            ],
            "no_data_requests": no_data_requests,
            "retrieved_at": datetime.now(ET).isoformat(),
            "rows": rows,
        })
        print(f"ThetaData opening {symbol} {month_start:%Y-%m}: rows={len(rows)}", flush=True)

        if index in {1, 12, 24} or index % 12 == 0:
            partial = _compile_opening_symbol(symbol, start_date=start_date, end_date=end_date)
            print(
                f"ThetaData opening {symbol} partial: sessions={partial['dataset_sessions']}, "
                f"archive={partial['archive_months_complete']}/{partial['archive_months_expected']}",
                flush=True,
            )

    manifest = _compile_opening_symbol(symbol, start_date=start_date, end_date=end_date)
    print(
        f"ThetaData opening {symbol}: sessions={manifest['dataset_sessions']}, "
        f"contracts={manifest['contracts_in_dataset']}, archive_complete={manifest['archive_complete']}",
        flush=True,
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill ThetaData opening option-chain snapshots")
    parser.add_argument("--underlying", action="append", help="ticker to collect; repeatable")
    parser.add_argument(
        "--compile-only", action="store_true",
        help="rebuild opening snapshots directly in SQLite from local opening and open-interest archives",
    )
    parser.add_argument("--start-date", type=date.fromisoformat, default=FIRST_DATA_DATE)
    parser.add_argument(
        "--end-date", type=date.fromisoformat,
        default=datetime.now(ET).date() - timedelta(days=1),
    )
    args = parser.parse_args()
    if args.end_date < args.start_date:
        parser.error("--end-date must be on or after --start-date")

    symbols = args.underlying or _research_symbols()
    symbols = [symbol.strip().upper() for symbol in symbols]
    if not symbols or any(not item or not item.replace(".", "").isalnum() for item in symbols):
        parser.error("underlyings must be valid ticker symbols")

    RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("ThetaData backfill is already running; opening backfill skipped.")
            return 0
        if args.compile_only:
            manifests = []
            for symbol in symbols:
                start = _effective_start_date(symbol, args.start_date)
                manifests.append(_compile_opening_symbol(
                    symbol, start_date=start, end_date=args.end_date,
                ))
            print(json.dumps({
                "provider": "local archive compile",
                "underlyings": len(manifests),
                "archive_complete": [row["underlying"] for row in manifests if row["archive_complete"]],
                "incomplete": [row["underlying"] for row in manifests if not row["archive_complete"]],
            }, sort_keys=True))
            return 0
        api_key = _read_api_key()
        client = _theta_client(api_key)
        manifests = []
        failures = []
        for symbol in symbols:
            start = _effective_start_date(symbol, args.start_date)
            if start > args.start_date:
                print(f"ThetaData opening {symbol}: starting at {start}", flush=True)
            try:
                manifests.append(_fetch_symbol(client, symbol, start_date=start, end_date=args.end_date))
            except Exception as error:
                message = str(error).replace(api_key, "[redacted]")
                failures.append({"underlying": symbol, "error": f"{type(error).__name__}: {message[:300]}"})
                print(f"ThetaData opening {symbol} stopped: {failures[-1]['error']}", flush=True)

        print(json.dumps({
            "provider": "ThetaData",
            "snapshot_time_et": OPENING_TIME.isoformat(),
            "underlyings": len(manifests),
            "archive_complete": [row["underlying"] for row in manifests if row["archive_complete"]],
            "incomplete": [row["underlying"] for row in manifests if not row["archive_complete"]],
            "failures": failures,
        }, sort_keys=True))
        return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
