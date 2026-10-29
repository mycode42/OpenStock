#!/usr/bin/env python3
"""Store Alpaca daily equity bars for the full research universe in market SQLite."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import gzip
import hashlib
import json
from pathlib import Path
import time
from zoneinfo import ZoneInfo

import requests

from broker.options import OptionsPaperClient
from options.research.universe import history_start_date, history_underlyings, load_history_universe
from market.store import SQLiteBarStore
from runtime.paths import DATA_DIR


ET = ZoneInfo("America/New_York")
OUTPUT_DIR = DATA_DIR / "options" / "research" / "alpaca"
THETA_DIR = DATA_DIR / "options" / "research" / "theta"
PAGE_LIMIT = 10000


def _research_symbols() -> list[str]:
    return history_underlyings(load_history_universe())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _coverage_map(store: SQLiteBarStore) -> dict[tuple[str, str, str, str], dict]:
    return {
        (row["feed"], row["adjustment"], row["timeframe"], row["symbol"]): row
        for row in store.coverage()
    }


def _legacy_split_bars(symbol: str, *, start: date, end: date, feed: str) -> list[dict] | None:
    """Read a completed pre-SQLite Alpaca archive when it matches this request."""
    data_path = OUTPUT_DIR / symbol / "equity_bars.json.gz"
    manifest_path = OUTPUT_DIR / symbol / "equity_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        with gzip.open(data_path, "rt", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, EOFError, gzip.BadGzipFile, json.JSONDecodeError):
        return None
    if (
        manifest.get("status") != "complete"
        or manifest.get("underlying") != symbol
        or manifest.get("feed") != feed
        or manifest.get("adjustment") != "split"
        or manifest.get("requested_start_date") != start.isoformat()
        or manifest.get("requested_end_date") != end.isoformat()
        or manifest.get("data_sha256") != _sha256(data_path)
        or not isinstance(payload, dict)
        or payload.get("underlying") != symbol
        or payload.get("feed") != feed
        or payload.get("adjustment") != "split"
        or payload.get("timeframe") != "1Day"
        or payload.get("requested_start_date") != start.isoformat()
        or payload.get("requested_end_date") != end.isoformat()
    ):
        return None
    rows = payload.get("rows")
    if not isinstance(rows, list):
        return None
    bars = [
        row for row in rows
        if isinstance(row, dict) and all(key in row for key in ("t", "o", "h", "l", "c", "v"))
    ]
    return bars or None


def _fetch_bars(
    client: OptionsPaperClient, symbol: str, *, start: date, end: date,
    feed: str, adjustment: str,
) -> list[dict]:
    for attempt in range(6):
        try:
            return client.stock_bars(
                symbol, start=start, end=end, limit=PAGE_LIMIT,
                adjustment=adjustment, feed=feed,
            )
        except requests.RequestException as error:
            response = getattr(error, "response", None)
            status = getattr(response, "status_code", None)
            if attempt == 5 or (status is not None and status < 500 and status != 429):
                raise
            time.sleep(min(2 ** attempt, 30))
    raise RuntimeError(f"Alpaca bars request failed repeatedly for {symbol}")


def _already_current(row: dict | None, *, end: date) -> bool:
    if not row or int(row.get("rows", 0)) <= 0:
        return False
    last = date.fromisoformat(str(row.get("last") or "")[:10])
    if last >= end:
        return True
    day = last + timedelta(days=1)
    while day <= end:
        if day.weekday() < 5:
            return False
        day += timedelta(days=1)
    return True


def _collect_adjustment(
    client: OptionsPaperClient, store: SQLiteBarStore,
    coverage: dict[tuple[str, str, str, str], dict], symbol: str, *,
    start: date, end: date, feed: str, adjustment: str, refresh: bool,
) -> dict:
    key = (feed, adjustment, "1Day", symbol)
    existing = coverage.get(key)
    if not refresh and _already_current(existing, end=end):
        return {
            "underlying": symbol, "adjustment": adjustment, "status": "current",
            "bar_count": existing["rows"], "source": "SQLite",
        }

    fetch_start = start
    if not refresh and existing:
        try:
            fetch_start = max(start, date.fromisoformat(str(existing["last"])[:10]) + timedelta(days=1))
        except (TypeError, ValueError):
            fetch_start = start

    bars = None
    source = "alpaca"
    if adjustment == "split" and not refresh:
        bars = _legacy_split_bars(symbol, start=start, end=end, feed=feed)
        if bars is not None:
            source = "legacy Alpaca archive"
    if bars is None:
        bars = _fetch_bars(
            client, symbol, start=fetch_start, end=end,
            feed=feed, adjustment=adjustment,
        )
    filter_start = start if source == "legacy Alpaca archive" else fetch_start
    by_timestamp = {}
    for row in bars:
        stamp = str(row.get("t") or "")
        if len(stamp) < 10:
            continue
        try:
            session_date = date.fromisoformat(stamp[:10])
        except ValueError:
            continue
        if filter_start <= session_date <= end:
            by_timestamp[stamp] = row
    bars = [by_timestamp[key] for key in sorted(by_timestamp)]
    if bars:
        store.write_bars(
            {symbol: bars}, feed=feed, timeframe="1Day", adjustment=adjustment,
            data_tier="algo_trader_plus", requested_start=start.isoformat(),
            requested_end=end.isoformat(),
        )
        first, last = bars[0]["t"], bars[-1]["t"]
        coverage[key] = {
            "feed": feed, "adjustment": adjustment, "timeframe": "1Day",
            "symbol": symbol, "rows": len(bars),
            "first": min(str(existing.get("first")), first) if existing else first,
            "last": max(str(existing.get("last")), last) if existing else last,
        }
        status = "imported" if source == "legacy Alpaca archive" else "complete"
    elif existing:
        return {
            "underlying": symbol, "adjustment": adjustment, "status": "current",
            "bar_count": existing["rows"], "last_session": str(existing["last"])[:10],
            "source": "SQLite (no newer session bars)",
        }
    else:
        status = "empty"
    return {
        "underlying": symbol, "adjustment": adjustment, "status": status,
        "bar_count": len(bars), "first_session": bars[0]["t"][:10] if bars else None,
        "last_session": bars[-1]["t"][:10] if bars else None, "source": source,
    }


def _prune_legacy_equity_json(symbols: list[str]) -> int:
    removed = 0
    for symbol in symbols:
        paths = (
            OUTPUT_DIR / symbol / "equity_bars.json.gz",
            OUTPUT_DIR / symbol / "equity_manifest.json",
            THETA_DIR / symbol / "archive" / "stock_eod.json.gz",
        )
        for path in paths:
            if path.exists():
                path.unlink()
                removed += 1
        try:
            (OUTPUT_DIR / symbol).rmdir()
        except OSError:
            pass
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill Alpaca daily equity bars for the full research universe into SQLite",
    )
    parser.add_argument("--symbol", action="append", help="ticker to collect; repeatable; defaults to all 117 symbols")
    parser.add_argument("--start-date", type=date.fromisoformat, default=date(2020, 1, 1))
    parser.add_argument(
        "--end-date", type=date.fromisoformat,
        default=datetime.now(ET).date() - timedelta(days=1),
    )
    parser.add_argument("--feed", choices=("sip", "iex"), default="sip")
    parser.add_argument("--adjustment", choices=("raw", "split", "both"), default="both")
    parser.add_argument("--refresh", action="store_true", help="redownload full requested history even when SQLite is current")
    parser.add_argument(
        "--prune-json-after-import", action="store_true",
        help="remove legacy equity JSON only after all 117 symbols are verified in SQLite",
    )
    parser.add_argument("--request-delay", type=float, default=0.1)
    args = parser.parse_args()
    if args.end_date < args.start_date:
        parser.error("--end-date must be on or after --start-date")
    if args.request_delay < 0:
        parser.error("--request-delay must be non-negative")

    all_symbols = _research_symbols()
    symbols = [symbol.strip().upper() for symbol in args.symbol] if args.symbol else all_symbols
    symbols = list(dict.fromkeys(symbols))
    if not symbols or any(not symbol or not symbol.replace(".", "").isalnum() for symbol in symbols):
        parser.error("symbols must be valid ticker names")
    adjustments = ["raw", "split"] if args.adjustment == "both" else [args.adjustment]
    if args.prune_json_after_import and (adjustments != ["raw", "split"] or set(symbols) != set(all_symbols)):
        parser.error("--prune-json-after-import requires both adjustments and the full 117-symbol universe")

    client = OptionsPaperClient()
    store = SQLiteBarStore()
    coverage = _coverage_map(store)
    results = []
    failures = []
    for symbol in symbols:
        start = history_start_date(symbol, args.start_date)
        if start > args.end_date:
            failures.append({"underlying": symbol, "error": "configured history start is after requested end date"})
            continue
        for adjustment in adjustments:
            try:
                result = _collect_adjustment(
                    client, store, coverage, symbol, start=start, end=args.end_date,
                    feed=args.feed, adjustment=adjustment, refresh=args.refresh,
                )
                results.append(result)
                print(
                    f"Alpaca equity history {symbol} {adjustment}: {result['status']} "
                f"bars={result.get('bar_count', 0)} first={result.get('first_session')} "
                    f"last={result.get('last_session')} source={result.get('source', 'SQLite')}",
                    flush=True,
                )
                if result["status"] == "empty":
                    failures.append({"underlying": symbol, "adjustment": adjustment, "error": "no bars returned"})
            except Exception as error:
                failures.append({
                    "underlying": symbol, "adjustment": adjustment,
                    "error": f"{type(error).__name__}: {str(error)[:300]}",
                })
                print(
                    f"Alpaca equity history {symbol} {adjustment} failed: {failures[-1]['error']}",
                    flush=True,
                )
            if args.request_delay:
                time.sleep(args.request_delay)

    removed_json = 0
    if args.prune_json_after_import and not failures:
        verified = _coverage_map(store)
        missing = []
        for symbol in symbols:
            for adjustment in adjustments:
                entry = verified.get((args.feed, adjustment, "1Day", symbol))
                if not _already_current(entry, end=args.end_date):
                    missing.append({"underlying": symbol, "adjustment": adjustment})
        if missing:
            failures.extend({**row, "error": "SQLite coverage did not reach requested end date"} for row in missing)
        else:
            removed_json = _prune_legacy_equity_json(symbols)

    print(json.dumps({
        "provider": "Alpaca",
        "data_type": "historical equity daily OHLCV bars",
        "requested_symbols": len(symbols),
        "collected": sum(row["status"] in {"complete", "imported"} for row in results),
        "current": sum(row["status"] == "current" for row in results),
        "empty": sum(row["status"] == "empty" for row in results),
        "failed": failures,
        "removed_legacy_json_files": removed_json,
        "database": str(store.path),
        "start_date": args.start_date.isoformat(),
        "end_date": args.end_date.isoformat(),
        "feed": args.feed,
        "adjustments": adjustments,
    }, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
