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
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from zoneinfo import ZoneInfo

from portfolio.config import ALGO_PORTFOLIO_UNIVERSE


ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = ROOT / ".env"
RESEARCH_DIR = ROOT / "data" / "options" / "research" / "theta"
LOCK_PATH = RESEARCH_DIR / "theta_backfill.lock"
FIRST_DATA_DATE = date(2020, 1, 1)
REQUIRED_RESEARCH_SESSIONS = 1013
MAX_RESEARCH_DTE = 60
STRIKE_RANGE = 20
ET = ZoneInfo("America/New_York")


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


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False,
    ) as stream:
        temp = Path(stream.name)
        json.dump(payload, stream, sort_keys=True, separators=(",", ":"), allow_nan=False, default=_json_value)
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
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"), allow_nan=False, default=_json_value)
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
        chunks.append((cursor, chunk_end))
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


def _contract_key(row: dict) -> tuple[str, float, str]:
    expiration = str(row.get("expiration") or row.get("expiration_date") or "")[:10]
    strike = _finite(row.get("strike")) or 0.0
    right = str(row.get("right") or row.get("type") or "").lower()
    if right in {"c", "call"}:
        right = "call"
    elif right in {"p", "put"}:
        right = "put"
    return expiration, strike, right


def _download_stock_rows(symbol: str, *, start_date: date, end_date: date) -> list[dict]:
    """Use the existing research data source for daily spot and split history."""
    try:
        import yfinance as yf
    except ImportError as error:
        raise RuntimeError("yfinance is required for underlying spot history; install the research dependency group") from error
    frame = yf.Ticker(symbol).history(
        start=start_date.isoformat(),
        end=(end_date + timedelta(days=1)).isoformat(),
        auto_adjust=False,
        actions=True,
        repair=True,
        timeout=30,
    )
    output = []
    for stamp, row in frame.iterrows():
        try:
            day = stamp.date() if hasattr(stamp, "date") else date.fromisoformat(str(stamp)[:10])
        except (TypeError, ValueError):
            continue
        close = _finite(row.get("Close"))
        if close is None or close <= 0:
            continue
        output.append({
            "last_trade": datetime.combine(day, time(16, 0), tzinfo=ET).isoformat(),
            "close": close,
            "split_ratio": _finite(row.get("Stock Splits")) or 0.0,
        })
    return output


def _split_adjusted_history(
    raw_closes: dict[date, float], stock_rows: list[dict],
) -> tuple[dict[date, float], int]:
    splits = []
    for row in stock_rows:
        split_day = _row_date(row, "last_trade", "created", "timestamp")
        ratio = _finite(row.get("split_ratio"))
        if split_day and ratio and ratio > 0 and ratio != 1:
            splits.append((split_day, ratio))
    adjusted = {}
    for day, close in raw_closes.items():
        factor = math.prod(ratio for split_day, ratio in splits if split_day > day)
        adjusted[day] = close / factor if factor > 0 else close
    return adjusted, len(splits)


def _session_labels(closes: dict[date, float]) -> dict[date, tuple[str, str]]:
    """Create point-in-time trend and volatility labels from adjusted closes."""
    ordered = sorted(closes.items())
    log_returns: list[float] = []
    realized: list[float | None] = []
    for index, (_, close) in enumerate(ordered):
        if index:
            previous = ordered[index - 1][1]
            log_returns.append(math.log(close / previous))
        else:
            log_returns.append(0.0)
        sample = log_returns[max(1, index - 19):index + 1]
        if len(sample) >= 10:
            mean = sum(sample) / len(sample)
            variance = sum((value - mean) ** 2 for value in sample) / max(1, len(sample) - 1)
            realized.append(math.sqrt(max(0.0, variance) * 252.0))
        else:
            realized.append(None)

    labels = {}
    for index, (day, close) in enumerate(ordered):
        values = [value for _, value in ordered[max(0, index - 49):index + 1]]
        mean_50 = sum(values) / len(values)
        long_values = [value for _, value in ordered[max(0, index - 199):index + 1]]
        mean_200 = sum(long_values) / len(long_values)
        if len(long_values) >= 200 and close > mean_50 > mean_200:
            regime = "trending_up"
        elif len(long_values) >= 200 and close < mean_50 < mean_200:
            regime = "trending_down"
        else:
            regime = "range_bound"

        current_vol = realized[index]
        prior_vols = [value for value in realized[max(0, index - 251):index + 1] if value is not None]
        if current_vol is not None and len(prior_vols) >= 20:
            threshold = sorted(prior_vols)[max(0, math.ceil(len(prior_vols) * 0.75) - 1)]
            vol_state = "volatile" if current_vol >= threshold else "normal"
        else:
            vol_state = "normal"
        labels[day] = (regime, vol_state)
    return labels


def _compile_symbol(symbol: str, *, start_date: date, end_date: date) -> dict:
    symbol_dir = RESEARCH_DIR / symbol
    eod_dir, oi_dir = symbol_dir / "archive" / "eod", symbol_dir / "archive" / "open_interest"
    stock_path = symbol_dir / "archive" / "stock_eod.json.gz"
    stock_rows = _load_gzip_rows(stock_path)
    spot_by_date: dict[date, float] = {}
    for row in stock_rows:
        day = _row_date(row, "last_trade", "created", "timestamp")
        close = _finite(row.get("close"))
        if day and start_date <= day <= end_date and close and close > 0:
            spot_by_date[day] = close
    adjusted_closes, split_count = _split_adjusted_history(spot_by_date, stock_rows)
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
            "as_of": as_of.isoformat(),
            "feed": "thetadata_historical_eod",
            "regime": session_labels.get(day, ("range_bound", "normal"))[0],
            "volatility_state": session_labels.get(day, ("range_bound", "normal"))[1],
            "contracts": contracts,
        })

    dataset_path = symbol_dir / "chains.jsonl"
    dataset_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=dataset_path.parent,
        prefix=".chains.", suffix=".tmp", delete=False,
    ) as stream:
        temp = Path(stream.name)
        for row in rows:
            stream.write(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False))
            stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, dataset_path)
    manifest = {
        "provider": "ThetaData",
        "data_type": "historical option EOD NBBO and open interest",
        "status": "research_ready" if len(rows) >= REQUIRED_RESEARCH_SESSIONS else "collecting",
        "underlying": symbol,
        "dataset_path": str(dataset_path),
        "dataset_sessions": len(rows),
        "research_sessions_required": REQUIRED_RESEARCH_SESSIONS,
        "contracts_in_dataset": contracts_total,
        "contract_dte_filter": {"min": 0, "max": MAX_RESEARCH_DTE},
        "strike_range": STRIKE_RANGE,
        "split_events_applied": split_count,
        "underlying_close_adjustment": "split_adjusted for realized volatility; raw daily close retained as spot",
        "regime_labels": "50/200-session trend from split-adjusted underlying closes",
        "volatility_labels": "top quartile of trailing 252-session 20-day realized volatility",
        "quote_date_start": rows[0]["as_of"][:10] if rows else None,
        "quote_date_end": rows[-1]["as_of"][:10] if rows else None,
        "generated_at": datetime.now(ET).isoformat(),
    }
    _atomic_json(symbol_dir / "manifest.json", manifest)
    return manifest


def _fetch_symbol(client, symbol: str, *, start_date: date, end_date: date) -> dict:
    symbol_dir = RESEARCH_DIR / symbol
    archive = symbol_dir / "archive"
    eod_dir, oi_dir = archive / "eod", archive / "open_interest"
    eod_dir.mkdir(parents=True, exist_ok=True)
    oi_dir.mkdir(parents=True, exist_ok=True)

    stock_path = archive / "stock_eod.json.gz"
    stock = _download_stock_rows(symbol, start_date=start_date, end_date=end_date)
    _atomic_gzip_json(stock_path, {
        "provider": "yfinance_daily_bars_and_split_actions",
        "underlying": symbol,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "retrieved_at": datetime.now(ET).isoformat(),
        "rows": stock,
    })

    chunks = list(_month_chunks(start_date, end_date, newest_first=True))
    for index, (month_start, month_end) in enumerate(chunks, start=1):
        month_key = month_start.strftime("%Y-%m")
        eod_path, oi_path = eod_dir / f"{month_key}.json.gz", oi_dir / f"{month_key}.json.gz"
        # Completed months are immutable. Refresh only the active month so a
        # rerun extends the archive with recently completed sessions.
        refresh = month_end == end_date
        if eod_path.is_file() and oi_path.is_file() and not refresh:
            continue
        request = {
            "symbol": symbol,
            "start_date": month_start,
            "end_date": month_end,
            "expiration": "*",
            "max_dte": MAX_RESEARCH_DTE,
            "strike_range": STRIKE_RANGE,
        }
        # Value permits two account-wide requests. Fetch EOD quotes and OI
        # together to use that entitlement without queuing larger batches.
        with ThreadPoolExecutor(max_workers=2) as pool:
            eod_future = pool.submit(client.option_history_eod, **request)
            oi_future = pool.submit(client.option_history_open_interest, **request)
            eod = _records(eod_future.result())
            oi = _records(oi_future.result())
        for path, endpoint, rows in (
            (eod_path, "option_history_eod", eod),
            (oi_path, "option_history_open_interest", oi),
        ):
            _atomic_gzip_json(path, {
                "provider": f"thetadata_{endpoint}",
                "underlying": symbol,
                "start_date": month_start.isoformat(),
                "end_date": month_end.isoformat(),
                "retrieved_at": datetime.now(ET).isoformat(),
                "rows": rows,
            })
        print(f"ThetaData {symbol} {month_key}: EOD rows={len(eod)}, OI rows={len(oi)}")
        # Publish a usable recent-history dataset as soon as a ticker has
        # accumulated enough months, then refresh it periodically while the
        # deeper archive continues. Dataset replacement is atomic.
        if index >= 48 and (index in {48, 49} or index % 12 == 0):
            partial = _compile_symbol(symbol, start_date=start_date, end_date=end_date)
            print(
                f"ThetaData {symbol} partial compile: sessions={partial['dataset_sessions']}/"
                f"{REQUIRED_RESEARCH_SESSIONS}, status={partial['status']}"
            )

    manifest = _compile_symbol(symbol, start_date=start_date, end_date=end_date)
    print(
        f"ThetaData {symbol}: sessions={manifest['dataset_sessions']}/"
        f"{REQUIRED_RESEARCH_SESSIONS}, contracts={manifest['contracts_in_dataset']}, "
        f"status={manifest['status']}"
    )
    return manifest


def _portfolio_symbols() -> list[str]:
    symbols = list(dict.fromkeys(
        str(symbol).strip().upper()
        for group in ALGO_PORTFOLIO_UNIVERSE.values()
        for symbol in group.get("tickers", [])
    ))
    priority = ["SPY", "QQQ", "GLD", "TLT"]
    return sorted(symbols, key=lambda symbol: (priority.index(symbol) if symbol in priority else len(priority), symbols.index(symbol)))


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill daily ThetaData option chains for Algo symbols")
    parser.add_argument(
        "--underlying", action="append",
        help="symbol to collect (repeatable); defaults to the complete Algo portfolio universe",
    )
    parser.add_argument("--start-date", type=date.fromisoformat, default=FIRST_DATA_DATE)
    parser.add_argument(
        "--end-date", type=date.fromisoformat,
        default=datetime.now(ET).date() - timedelta(days=1),
    )
    args = parser.parse_args()
    if args.end_date < args.start_date:
        parser.error("--end-date must be on or after --start-date")
    symbols = [item.strip().upper() for item in args.underlying] if args.underlying else _portfolio_symbols()
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
            return _run_symbols(symbols, args.start_date, args.end_date)
    except Exception as error:
        message = str(error)
        try:
            key = _read_api_key()
            message = message.replace(key, "[redacted]")
        except Exception:
            pass
        print(f"ThetaData collector stopped: {type(error).__name__}: {message[:400]}")
        return 1


def _run_symbols(symbols: list[str], start_date: date, end_date: date) -> int:
    api_key = _read_api_key()
    client = _theta_client(api_key)
    manifests = []
    for symbol in dict.fromkeys(symbols):
        try:
            manifests.append(_fetch_symbol(
                client, symbol, start_date=start_date, end_date=end_date,
            ))
        except Exception as error:
            message = str(error).replace(api_key, "[redacted]")
            print(f"ThetaData {symbol} stopped: {type(error).__name__}: {message[:400]}")
            return 1
    print(json.dumps({
        "provider": "ThetaData",
        "underlyings": len(manifests),
        "research_ready": [row["underlying"] for row in manifests if row["status"] == "research_ready"],
        "collecting": [row["underlying"] for row in manifests if row["status"] != "research_ready"],
        "manifest_dir": str(RESEARCH_DIR),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
