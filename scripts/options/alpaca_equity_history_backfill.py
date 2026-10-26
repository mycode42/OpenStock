#!/usr/bin/env python3
"""Archive Alpaca split-adjusted daily equity bars for the added research tickers."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from zoneinfo import ZoneInfo

import requests

from broker.options import OptionsPaperClient
from options.research.universe import history_start_date, history_underlyings, load_history_universe
from runtime.paths import DATA_DIR


ET = ZoneInfo("America/New_York")
OUTPUT_DIR = DATA_DIR / "options" / "research" / "alpaca"
PAGE_LIMIT = 10000


def _new_research_symbols() -> list[str]:
    universe = load_history_universe()
    existing = {
        str(symbol).strip().upper()
        for symbol in universe["categories"].get("existing_algo_portfolio", [])
    }
    return [symbol for symbol in history_underlyings(universe) if symbol not in existing]


def _atomic_gzip_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    compressed = gzip.compress(
        (json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8"),
        mtime=0,
    )
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
        temp_path = Path(stream.name)
        stream.write(compressed)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp_path, path)


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=f".{path.name}.", suffix=".tmp", delete=False,
    ) as stream:
        temp_path = Path(stream.name)
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp_path, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_current(manifest_path: Path, data_path: Path, *, start: date, end: date, feed: str, adjustment: str) -> bool:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        data_path.is_file()
        and manifest.get("status") == "complete"
        and manifest.get("requested_start_date") == start.isoformat()
        and manifest.get("requested_end_date") == end.isoformat()
        and manifest.get("feed") == feed
        and manifest.get("adjustment") == adjustment
    )


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


def _collect_symbol(
    client: OptionsPaperClient, symbol: str, *, start: date, end: date,
    feed: str, adjustment: str, refresh: bool,
) -> dict:
    symbol_dir = OUTPUT_DIR / symbol
    data_path = symbol_dir / "equity_bars.json.gz"
    manifest_path = symbol_dir / "equity_manifest.json"
    if not refresh and _is_current(
        manifest_path, data_path, start=start, end=end, feed=feed, adjustment=adjustment,
    ):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return {
            "underlying": symbol, "status": "unchanged",
            "bar_count": manifest.get("bar_count", 0),
            "data_path": str(data_path),
        }

    raw_bars = _fetch_bars(
        client, symbol, start=start, end=end, feed=feed, adjustment=adjustment,
    )
    by_timestamp = {}
    for row in raw_bars:
        stamp = str(row.get("t") or "")
        if len(stamp) < 10:
            continue
        try:
            session_date = date.fromisoformat(stamp[:10])
        except ValueError:
            continue
        if start <= session_date <= end:
            normalized = dict(row)
            normalized["underlying"] = symbol
            normalized["session_date"] = session_date.isoformat()
            by_timestamp[stamp] = normalized
    bars = [by_timestamp[key] for key in sorted(by_timestamp)]
    retrieved_at = datetime.now(ET).isoformat()
    payload = {
        "provider": "Alpaca",
        "data_type": "historical equity daily OHLCV bars",
        "underlying": symbol,
        "feed": feed,
        "adjustment": adjustment,
        "timeframe": "1Day",
        "requested_start_date": start.isoformat(),
        "requested_end_date": end.isoformat(),
        "retrieved_at": retrieved_at,
        "bar_count": len(bars),
        "first_session": bars[0]["session_date"] if bars else None,
        "last_session": bars[-1]["session_date"] if bars else None,
        "rows": bars,
    }
    _atomic_gzip_json(data_path, payload)
    manifest = {
        "provider": "Alpaca",
        "status": "complete" if bars else "empty",
        "underlying": symbol,
        "feed": feed,
        "adjustment": adjustment,
        "timeframe": "1Day",
        "requested_start_date": start.isoformat(),
        "requested_end_date": end.isoformat(),
        "first_session": payload["first_session"],
        "last_session": payload["last_session"],
        "bar_count": len(bars),
        "data_path": str(data_path),
        "data_sha256": _sha256(data_path),
        "generated_at": retrieved_at,
    }
    _atomic_json(manifest_path, manifest)
    return {
        "underlying": symbol, "status": manifest["status"],
        "bar_count": len(bars), "first_session": manifest["first_session"],
        "last_session": manifest["last_session"], "data_path": str(data_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill Alpaca split-adjusted daily equity bars for added options research tickers",
    )
    parser.add_argument("--symbol", action="append", help="ticker to collect; repeatable; defaults to the 93 additions")
    parser.add_argument("--start-date", type=date.fromisoformat, default=date(2020, 1, 1))
    parser.add_argument(
        "--end-date", type=date.fromisoformat,
        default=datetime.now(ET).date() - timedelta(days=1),
    )
    parser.add_argument("--feed", choices=("sip", "iex"), default="sip")
    parser.add_argument("--adjustment", choices=("raw", "split", "dividend", "spin-off", "all"), default="split")
    parser.add_argument("--refresh", action="store_true", help="redownload even when the manifest covers this request")
    parser.add_argument("--request-delay", type=float, default=0.1)
    args = parser.parse_args()
    if args.end_date < args.start_date:
        parser.error("--end-date must be on or after --start-date")
    if args.request_delay < 0:
        parser.error("--request-delay must be non-negative")

    symbols = [symbol.strip().upper() for symbol in args.symbol] if args.symbol else _new_research_symbols()
    symbols = list(dict.fromkeys(symbols))
    if not symbols or any(not symbol or not symbol.replace(".", "").isalnum() for symbol in symbols):
        parser.error("symbols must be valid ticker names")

    client = OptionsPaperClient()
    results = []
    failures = []
    for symbol in symbols:
        start = history_start_date(symbol, args.start_date)
        if start > args.end_date:
            failures.append({"underlying": symbol, "error": "configured history start is after requested end date"})
            continue
        try:
            result = _collect_symbol(
                client, symbol, start=start, end=args.end_date, feed=args.feed,
                adjustment=args.adjustment, refresh=args.refresh,
            )
            results.append(result)
            print(
                f"Alpaca equity history {symbol}: {result['status']} bars={result.get('bar_count', 0)} "
                f"first={result.get('first_session')} last={result.get('last_session')}",
                flush=True,
            )
        except Exception as error:
            failures.append({"underlying": symbol, "error": f"{type(error).__name__}: {str(error)[:300]}"})
            print(f"Alpaca equity history {symbol} failed: {failures[-1]['error']}", flush=True)
        if args.request_delay:
            time.sleep(args.request_delay)

    print(json.dumps({
        "provider": "Alpaca",
        "data_type": "historical equity daily OHLCV bars",
        "requested_symbols": len(symbols),
        "collected": sum(row["status"] == "complete" for row in results),
        "unchanged": sum(row["status"] == "unchanged" for row in results),
        "empty": sum(row["status"] == "empty" for row in results),
        "failed": failures,
        "output_dir": str(OUTPUT_DIR),
        "start_date": args.start_date.isoformat(),
        "end_date": args.end_date.isoformat(),
        "feed": args.feed,
        "adjustment": args.adjustment,
    }, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
