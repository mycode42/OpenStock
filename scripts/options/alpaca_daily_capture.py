#!/usr/bin/env python3
"""Capture one daily OPRA option-chain snapshot per configured underlying."""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta
import fcntl
import json
import math
import os
from pathlib import Path
import tempfile
import time as sleep_time
from typing import Any
from zoneinfo import ZoneInfo

import requests

from broker.options import OptionsPaperClient
from market.quotes import parse_timestamp
from options.research.archive import RESEARCH_ROOT, merge_symbol_dataset, session_labels
from portfolio.config import ALGO_PORTFOLIO_UNIVERSE


ET = ZoneInfo("America/New_York")
MAX_DTE = 60
STRIKES_PER_SIDE = 20
MINIMUM_CHAIN_CONTRACTS = 20
MAX_QUOTE_BATCH = 100
CAPTURE_LOCK = RESEARCH_ROOT / ".alpaca-daily-capture.lock"


def _as_date(value: Any) -> date | None:
    try:
        stamp = parse_timestamp(value, context="Alpaca timestamp")
        return stamp.astimezone(ET).date()
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _open_interest(row: dict) -> int | None:
    try:
        value = int(float(row.get("open_interest")))
    except (TypeError, ValueError, OverflowError):
        return None
    return value if value >= 0 else None


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _sanitize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def _session_close(client: OptionsPaperClient, session_date: date) -> tuple[datetime, datetime] | None:
    schedule = client.market_calendar(session_date)
    if schedule is None:
        return None
    open_at = datetime.combine(session_date, time.fromisoformat(str(schedule["open"])), tzinfo=ET)
    close_at = datetime.combine(session_date, time.fromisoformat(str(schedule["close"])), tzinfo=ET)
    return open_at, close_at


def _underlying_history(
    client: OptionsPaperClient, symbol: str, session_date: date,
) -> tuple[float, dict[date, float]]:
    bars = client.stock_bars(
        symbol,
        start=session_date - timedelta(days=500),
        end=session_date + timedelta(days=1),
        limit=600,
        adjustment="split",
    )
    closes = {}
    for row in bars:
        day, close = _as_date(row.get("t")), _number(row.get("c"))
        if day and close and close > 0 and day <= session_date:
            closes[day] = close
    spot = closes.pop(session_date, None)
    if spot is None:
        raise RuntimeError(f"Alpaca has no completed {session_date} stock bar for {symbol}")
    return spot, closes


def _select_contracts(contracts: list[dict], *, spot: float, session_date: date) -> list[dict]:
    by_expiration: dict[str, list[tuple[float, dict]]] = {}
    max_expiration = session_date + timedelta(days=MAX_DTE)
    for row in contracts:
        try:
            expiration = date.fromisoformat(str(row.get("expiration_date")))
            strike = float(row.get("strike_price"))
        except (TypeError, ValueError, OverflowError):
            continue
        if not session_date <= expiration <= max_expiration or not math.isfinite(strike) or strike <= 0:
            continue
        if str(row.get("type", "")).lower() not in {"call", "put"}:
            continue
        by_expiration.setdefault(expiration.isoformat(), []).append((strike, row))

    selected = []
    for expiry_rows in by_expiration.values():
        distinct_strikes = sorted({strike for strike, _row in expiry_rows})
        below = sorted((strike for strike in distinct_strikes if strike <= spot), reverse=True)[:STRIKES_PER_SIDE]
        above = sorted(strike for strike in distinct_strikes if strike > spot)[:STRIKES_PER_SIDE]
        strikes = set(below + above)
        selected.extend(row for strike, row in expiry_rows if strike in strikes)
    return selected


def _fetch_snapshots(client: OptionsPaperClient, symbols: list[str], *, feed: str) -> dict[str, dict]:
    output: dict[str, dict] = {}
    for offset in range(0, len(symbols), MAX_QUOTE_BATCH):
        batch = symbols[offset:offset + MAX_QUOTE_BATCH]
        for attempt in range(6):
            try:
                output.update(client.option_snapshots(batch, feed=feed))
                break
            except requests.RequestException as error:
                response = getattr(error, "response", None)
                status = getattr(response, "status_code", None)
                if attempt == 5 or (status is not None and status < 500 and status != 429):
                    raise
                retry_after = getattr(response, "headers", {}).get("Retry-After") if response is not None else None
                try:
                    delay = min(30.0, max(1.0, float(retry_after))) if retry_after else min(30.0, 2.0 ** attempt)
                except (TypeError, ValueError):
                    delay = min(30.0, 2.0 ** attempt)
                sleep_time.sleep(delay)
    return output


def _snapshot_day(row: dict) -> date:
    stamp = datetime.fromisoformat(str(row["as_of"]).replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("Alpaca snapshot as_of must include a timezone")
    return stamp.astimezone(ET).date()


def _write_daily_archive(path: Path, snapshot: dict) -> None:
    """Append a new session, replacing that session on safe same-day reruns."""
    path.parent.mkdir(parents=True, exist_ok=True)
    target_day = _snapshot_day(snapshot)
    if not path.is_file() or path.stat().st_size == 0:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temp = Path(stream.name)
            stream.write(json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        return

    # The normal daily path is append-only. A rerun for the last session
    # replaces only the final line; a historical/manual capture uses a
    # streamed rewrite so the archive remains date ordered without loading it.
    with path.open("rb") as source:
        source.seek(0, os.SEEK_END)
        position = source.tell() - 1
        while position >= 0:
            source.seek(position)
            if source.read(1) != b"\n":
                break
            position -= 1
        end = position + 1
        while position >= 0:
            source.seek(position)
            if source.read(1) == b"\n":
                break
            position -= 1
        source.seek(position + 1)
        last_row = json.loads(source.read(end - position - 1))
    last_day = _snapshot_day(last_row)
    if target_day > last_day:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return

    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent,
        prefix=f".{path.name}.", suffix=".tmp", delete=False,
    ) as stream:
        temp = Path(stream.name)
        inserted = False
        with path.open("r", encoding="utf-8") as source:
            for line_number, line in enumerate(source, 1):
                if not line.strip():
                    continue
                row = json.loads(line)
                day = _snapshot_day(row)
                if day == target_day:
                    if not inserted:
                        stream.write(json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
                        inserted = True
                    continue
                if day > target_day and not inserted:
                    stream.write(json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
                    inserted = True
                stream.write(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False))
                stream.write("\n")
        if not inserted:
            stream.write(json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def capture_symbol(
    client: OptionsPaperClient,
    symbol: str,
    *,
    session_date: date,
    open_at: datetime,
    close_at: datetime,
    feed: str,
) -> dict:
    spot, closes = _underlying_history(client, symbol, session_date)
    contracts = client.list_contracts(
        symbol,
        expiration_date_gte=session_date.isoformat(),
        expiration_date_lte=(session_date + timedelta(days=MAX_DTE)).isoformat(),
        style="american",
    )
    selected = _select_contracts(contracts, spot=spot, session_date=session_date)
    if not selected:
        raise RuntimeError(f"Alpaca returned no contracts in the {MAX_DTE}-DTE strike window for {symbol}")
    symbol_by_contract = {
        str(row.get("symbol") or "").upper(): row for row in selected if row.get("symbol")
    }
    snapshots = _fetch_snapshots(client, list(symbol_by_contract), feed=feed)
    snapshot_as_of = close_at
    rows = []
    for option_symbol, contract in symbol_by_contract.items():
        raw = snapshots.get(option_symbol) or snapshots.get(option_symbol.lower()) or {}
        quote = raw.get("latestQuote") or raw.get("latest_quote") or raw.get("quote") or {}
        try:
            quote_at = parse_timestamp(quote.get("t"), context=f"{option_symbol} quote")
            bid, ask = _number(quote.get("bp")), _number(quote.get("ap"))
        except (TypeError, ValueError):
            continue
        if (
            _as_date(quote.get("t")) != session_date
            or quote_at < open_at.astimezone(quote_at.tzinfo)
            or quote_at > close_at.astimezone(quote_at.tzinfo)
            or bid is None or ask is None or bid < 0 or ask <= 0 or ask < bid
        ):
            continue
        raw_greeks = raw.get("greeks") if isinstance(raw.get("greeks"), dict) else {}
        raw_iv = raw.get("impliedVolatility", raw.get("implied_volatility"))
        if raw_iv is None:
            raw_iv = raw_greeks.get("iv", raw_greeks.get("impliedVolatility", raw_greeks.get("implied_volatility")))
        rows.append({
            "symbol": option_symbol,
            "underlying_symbol": symbol,
            "expiration_date": str(contract.get("expiration_date"))[:10],
            "type": str(contract.get("type", "")).lower(),
            "strike_price": float(contract.get("strike_price")),
            "size": int(float(contract.get("size") or 100)),
            "style": str(contract.get("style") or "american").lower(),
            "status": str(contract.get("status") or "active").lower(),
            "tradable": bool(contract.get("tradable", True)),
            "open_interest": _open_interest(contract),
            "open_interest_date": contract.get("open_interest_date"),
            "bid": bid,
            "ask": ask,
            "quote_at": quote_at.isoformat(),
            "iv": _number(raw_iv),
            "greeks": _sanitize(raw_greeks),
        })

    if len(rows) < MINIMUM_CHAIN_CONTRACTS:
        raise RuntimeError(
            f"Alpaca returned only {len(rows)} same-session two-sided quotes for {symbol}; "
            f"need at least {MINIMUM_CHAIN_CONTRACTS}"
        )

    closes[session_date] = spot
    labels = session_labels(closes).get(session_date, ("range_bound", "normal"))
    snapshot = {
        "underlying": symbol,
        "spot": spot,
        "underlying_close": spot,
        "as_of": snapshot_as_of.isoformat(),
        "feed": f"alpaca_{feed}_daily_close",
        "regime": labels[0],
        "volatility_state": labels[1],
        "contracts": rows,
    }

    archive_path = RESEARCH_ROOT / "alpaca" / symbol / "chains.jsonl"
    _write_daily_archive(archive_path, snapshot)
    merged = merge_symbol_dataset(symbol)
    return {
        "underlying": symbol,
        "quote_count": len(rows),
        "open_interest_coverage": sum(row["open_interest"] is not None for row in rows),
        "as_of": snapshot["as_of"],
        "combined_sessions": merged["combined_sessions"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive daily Alpaca OPRA chains for options research")
    parser.add_argument("--underlying", action="append", help="ticker to capture; repeatable")
    parser.add_argument("--feed", choices=("opra", "indicative"), default=os.getenv("ALPACA_OPTIONS_FEED", "opra").strip().lower())
    args = parser.parse_args()
    if args.feed != "opra":
        parser.error("research archive capture requires the OPRA feed")
    symbols = args.underlying or list(dict.fromkeys(
        str(symbol).strip().upper()
        for group in ALGO_PORTFOLIO_UNIVERSE.values()
        for symbol in group.get("tickers", [])
    ))
    if not symbols or any(not symbol or not symbol.replace(".", "").isalnum() for symbol in symbols):
        parser.error("underlyings must be valid ticker symbols")

    CAPTURE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with CAPTURE_LOCK.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Alpaca options history capture is already running; this invocation skipped.")
            return 0
        try:
            client = OptionsPaperClient()
            now = datetime.now(ET)
            session = _session_close(client, now.date())
            if session is None:
                print(f"Alpaca options history capture skipped: {now.date()} is not a market session.")
                return 0
            open_at, close_at = session
            if now < close_at + timedelta(minutes=5):
                print(
                    f"Alpaca options history capture skipped: wait until "
                    f"{(close_at + timedelta(minutes=5)).isoformat()} for the completed session bar."
                )
                return 0

            results, failures = [], []
            for symbol in symbols:
                try:
                    result = capture_symbol(
                        client, symbol, session_date=now.date(), open_at=open_at,
                        close_at=close_at, feed=args.feed,
                    )
                    results.append(result)
                    print(
                        f"Alpaca options archive {symbol}: quotes={result['quote_count']} "
                        f"OI={result['open_interest_coverage']} as_of={result['as_of']}",
                        flush=True,
                    )
                except Exception as error:
                    failures.append({"underlying": symbol, "error": f"{type(error).__name__}: {str(error)[:300]}"})
                    print(f"Alpaca options archive {symbol} failed: {failures[-1]['error']}", flush=True)

            summary = {
                "session_date": now.date().isoformat(),
                "provider": "Alpaca OPRA",
                "captured": len(results),
                "failed": failures,
                "results": results,
            }
            print(json.dumps(summary, sort_keys=True))
            return 1 if failures else 0
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


if __name__ == "__main__":
    raise SystemExit(main())
