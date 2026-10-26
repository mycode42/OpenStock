"""Seed the live options IV journal from locally archived historical chains."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import fcntl
import gzip
import json
import math
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from broker.options import OptionsPaperClient
from options.config import load_options_config
from options.data import HistoricalChainSnapshotProvider
from options.journal import OptionsJournal
from runtime.atomic import atomic_write_json
from runtime.paths import DATA_DIR
from strategies.options.long_volatility import atm_implied_volatility


_SYMBOL = re.compile(r"^[A-Z0-9.]{1,10}$")
_ET = ZoneInfo("America/New_York")


def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _is_theta_iv_row(row: dict) -> bool:
    return str(row.get("feed") or "").lower() == "thetadata_historical_eod" or str(
        row.get("source") or ""
    ).lower().startswith("local_thetadata_")


def _is_cboe_iv_row(row: dict) -> bool:
    return str(row.get("feed") or "").lower() == "cboe_allaccess_historical" or str(
        row.get("source") or ""
    ).lower().startswith("local_cboe_")


def _cboe_atm_iv(
    path: Path,
    underlying: str,
    config: dict,
) -> dict | None:
    """Extract one liquid target-DTE ATM call/put IV observation from a file."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            document = json.load(stream)
        session_date = date.fromisoformat(str(document.get("quote_date", ""))[:10])
        response = document.get("response") or {}
        response_symbol = str(response.get("symbol") or underlying).upper()
        if response_symbol != underlying:
            return None
        spot = next(
            (
                value for key in ("underlying_close", "underlying_mid", "underlying_last_trade_price")
                if (value := _number(response.get(key))) is not None and value > 0
            ),
            None,
        )
        if spot is None:
            return None

        min_dte = int(config.get("min_dte", 7))
        max_dte = int(config.get("max_dte", 45))
        target_dte = int(config.get("target_dte", 30))
        expirations = set()
        option_rows = response.get("options") or []
        for row in option_rows:
            if not isinstance(row, dict):
                continue
            try:
                expiration = date.fromisoformat(str(row.get("expiry", ""))[:10])
            except (TypeError, ValueError):
                continue
            dte = (expiration - session_date).days
            if min_dte <= dte <= max_dte:
                expirations.add(expiration)
        expiration = min(
            expirations,
            key=lambda item: (abs((item - session_date).days - target_dte), item),
            default=None,
        )
        if expiration is None:
            return None

        min_open_interest = int(config.get("min_open_interest", 100))
        max_spread_pct = float(config.get("max_spread_pct", 0.15))
        candidates = {"C": [], "P": []}
        for row in option_rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("root") or underlying).upper() != underlying:
                continue
            if str(row.get("expiry") or "")[:10] != expiration.isoformat():
                continue
            right = str(row.get("option_type") or "").upper()
            if right in {"CALL", "PUT"}:
                right = "C" if right == "CALL" else "P"
            if right not in candidates:
                continue
            bid = _number(row.get("option_bid"))
            ask = _number(row.get("option_ask"))
            open_interest = _number(row.get("open_interest"))
            delta = _number(row.get("delta"))
            iv = _number(row.get("iv"))
            if iv is None:
                iv = _number(row.get("implied_volatility"))
            if iv is not None and iv > 5:
                iv /= 100.0
            if (
                bid is None or ask is None or ask < bid or bid <= 0
                or open_interest is None or open_interest < min_open_interest
                or delta is None or iv is None or iv <= 0
            ):
                continue
            midpoint = (bid + ask) / 2.0
            spread_pct = (ask - bid) / midpoint if midpoint > 0 else math.inf
            if spread_pct > max_spread_pct:
                continue
            target_delta = 0.50 if right == "C" else -0.50
            candidates[right].append((abs(delta - target_delta), iv))

        if not candidates["C"] or not candidates["P"]:
            return None
        call_iv = min(candidates["C"], key=lambda item: item[0])[1]
        put_iv = min(candidates["P"], key=lambda item: item[0])[1]
        atm_iv = (call_iv + put_iv) / 2.0
        if not math.isfinite(atm_iv) or atm_iv <= 0:
            return None
        return {
            "underlying": underlying,
            "session_date": session_date.isoformat(),
            "atm_iv": atm_iv,
            "spot": spot,
            "expiration": expiration.isoformat(),
            "dte": (expiration - session_date).days,
            "call_iv": call_iv,
            "put_iv": put_iv,
        }
    except (OSError, EOFError, json.JSONDecodeError, TypeError, ValueError):
        return None


def seed_cboe_iv_history(
    journal: OptionsJournal,
    underlying: str,
    *,
    before_date: date,
    config: dict,
    archive_root: Path | None = None,
) -> dict:
    """Import missing prior Cboe sessions once, without replacing journal rows."""
    symbol = str(underlying).upper()
    if not _SYMBOL.fullmatch(symbol):
        raise ValueError("underlying must be a simple uppercase option symbol")
    if isinstance(before_date, datetime):
        before_date = before_date.date()
    if not isinstance(before_date, date):
        raise TypeError("before_date must be a date")

    root = Path(archive_root) if archive_root is not None else DATA_DIR / "options" / "research"
    archive_dir = root / f"cboe_{symbol.lower()}_eod"
    marker = journal.directory / f"iv_history_cboe_{symbol.lower()}_bootstrap.json"
    lock_path = journal.directory / f"iv_history_cboe_{symbol.lower()}_bootstrap.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            if marker.exists():
                try:
                    previous = json.loads(marker.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    previous = {}
                if isinstance(previous, dict) and previous.get("status") == "seeded":
                    return {
                        "status": "already_seeded",
                        "underlying": symbol,
                        "imported": int(previous.get("imported", 0)),
                        "available": int(previous.get("available", 0)),
                    }

            files = sorted(archive_dir.glob("*.json.gz"))
            if not files:
                return {"status": "archive_missing", "underlying": symbol, "imported": 0, "available": 0}

            samples_by_date: dict[str, dict] = {}
            for path in files:
                sample = _cboe_atm_iv(path, symbol, config)
                if sample and date.fromisoformat(sample["session_date"]) < before_date:
                    samples_by_date[sample["session_date"]] = sample
            if not samples_by_date:
                return {"status": "no_usable_samples", "underlying": symbol, "imported": 0, "available": 0}

            existing_dates = {
                str(row.get("session_date"))
                for row in journal.rows("iv_history")
                if str(row.get("underlying") or "").upper() == symbol
            }
            inserted = 0
            for session in sorted(samples_by_date):
                if session in existing_dates:
                    continue
                sample = samples_by_date[session]
                journal.append("iv_history", {
                    "underlying": symbol,
                    "session_date": session,
                    "atm_iv": sample["atm_iv"],
                    "feed": "cboe_allaccess_historical",
                    "source": f"local_cboe_{symbol.lower()}_eod_archive",
                    "underlying_spot": sample["spot"],
                    "reference_expiration": sample["expiration"],
                    "reference_dte": sample["dte"],
                })
                inserted += 1

            report = {
                "status": "seeded",
                "underlying": symbol,
                "as_of_date": before_date.isoformat(),
                "first_session": min(samples_by_date),
                "last_session": max(samples_by_date),
                "available": len(samples_by_date),
                "imported": inserted,
                "source": "cboe_allaccess_historical",
                "seeded_at": datetime.now(timezone.utc).isoformat(),
            }
            atomic_write_json(marker, report)
            return report
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def seed_theta_iv_history(
    journal: OptionsJournal,
    underlying: str,
    *,
    before_date: date,
    config: dict,
    rate: float = 0.04,
    dividend_yield: float = 0.015,
    archive_path: Path | None = None,
) -> dict:
    """Seed missing IV sessions from Theta EOD chains and supersede Cboe rows."""
    symbol = str(underlying).upper()
    if not _SYMBOL.fullmatch(symbol):
        raise ValueError("underlying must be a simple uppercase option symbol")
    if isinstance(before_date, datetime):
        before_date = before_date.date()
    if not isinstance(before_date, date):
        raise TypeError("before_date must be a date")

    source_path = Path(archive_path) if archive_path is not None else None
    marker = journal.directory / f"iv_history_theta_{symbol.lower()}_bootstrap.json"
    lock_path = journal.directory / f"iv_history_theta_{symbol.lower()}_bootstrap.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            if marker.exists():
                try:
                    previous = json.loads(marker.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    previous = {}
                if isinstance(previous, dict) and previous.get("status") == "seeded":
                    return {
                        "status": "already_seeded",
                        "underlying": symbol,
                        "imported": int(previous.get("imported", 0)),
                        "replaced": int(previous.get("replaced", 0)),
                        "available": int(previous.get("available", 0)),
                    }

            if source_path is not None and not source_path.is_file():
                return {"status": "archive_missing", "underlying": symbol, "imported": 0, "available": 0}

            def snapshots():
                if source_path is not None:
                    with source_path.open("r", encoding="utf-8") as stream:
                        for line in stream:
                            if line.strip():
                                yield json.loads(line)
                    return
                from options.research.history_store import OptionsHistoryStore

                store = OptionsHistoryStore(initialize=False)
                if store.has_underlying(symbol):
                    yield from store.iter_effective_snapshots(symbol, phase="daily_close")

            existing_by_date: dict[str, list[dict]] = {}
            for row in journal.rows("iv_history"):
                if str(row.get("underlying") or "").upper() == symbol:
                    existing_by_date.setdefault(str(row.get("session_date") or ""), []).append(row)

            samples_by_date: dict[str, dict] = {}
            malformed_rows = 0
            min_dte = int(config.get("min_dte", 7))
            max_dte = int(config.get("max_dte", 45))
            for snapshot in snapshots():
                try:
                    if not isinstance(snapshot, dict):
                        malformed_rows += 1
                        continue
                    if str(snapshot.get("underlying") or "").upper() != symbol:
                        continue
                    as_of = datetime.fromisoformat(str(snapshot.get("as_of", "")).replace("Z", "+00:00"))
                    if as_of.tzinfo is None or as_of.utcoffset() is None:
                        malformed_rows += 1
                        continue
                    session = as_of.astimezone(_ET).date().isoformat()
                    if session >= before_date.isoformat():
                        continue

                    previous_rows = existing_by_date.get(session, [])
                    has_cboe = any(_is_cboe_iv_row(row) for row in previous_rows)
                    has_theta = any(_is_theta_iv_row(row) for row in previous_rows)
                    has_other_source = any(
                        not _is_cboe_iv_row(row) and not _is_theta_iv_row(row)
                        for row in previous_rows
                    )
                    if has_other_source or (has_theta and not has_cboe):
                        continue

                    provider = HistoricalChainSnapshotProvider(
                        [snapshot], rate=rate, dividend_yield=dividend_yield,
                    )
                    chain = provider.get_chain(
                        symbol, as_of=as_of, min_dte=min_dte, max_dte=max_dte,
                    )
                    atm_iv = atm_implied_volatility(chain, config)
                    if atm_iv is None:
                        continue
                    samples_by_date[session] = {
                        "atm_iv": atm_iv,
                        "spot": float(snapshot["spot"]),
                        "as_of": as_of.isoformat(),
                    }
                except (KeyError, TypeError, ValueError, OverflowError, json.JSONDecodeError):
                    malformed_rows += 1

            if not samples_by_date:
                return {
                    "status": "no_usable_samples",
                    "underlying": symbol,
                    "imported": 0,
                    "replaced": 0,
                    "available": 0,
                    "malformed_rows": malformed_rows,
                }

            imported = 0
            replaced = 0
            for session, sample in sorted(samples_by_date.items()):
                previous_rows = existing_by_date.get(session, [])
                has_cboe = any(_is_cboe_iv_row(row) for row in previous_rows)
                has_theta = any(_is_theta_iv_row(row) for row in previous_rows)
                has_other_source = any(
                    not _is_cboe_iv_row(row) and not _is_theta_iv_row(row)
                    for row in previous_rows
                )
                if has_other_source or (has_theta and not has_cboe):
                    continue
                journal.append("iv_history", {
                    "underlying": symbol,
                    "session_date": session,
                    "as_of": sample["as_of"],
                    "atm_iv": sample["atm_iv"],
                    "feed": "thetadata_historical_eod",
                    "source": "local_thetadata_eod_archive",
                    "underlying_spot": sample["spot"],
                })
                if has_cboe:
                    replaced += 1
                else:
                    imported += 1

            report = {
                "status": "seeded",
                "underlying": symbol,
                "as_of_date": before_date.isoformat(),
                "first_session": min(samples_by_date),
                "last_session": max(samples_by_date),
                "available": len(samples_by_date),
                "imported": imported,
                "replaced": replaced,
                "malformed_rows": malformed_rows,
                "source": "options_history_database" if source_path is None else "local_thetadata_historical_eod_archive",
                "seeded_at": datetime.now(timezone.utc).isoformat(),
            }
            atomic_write_json(marker, report)
            return report
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed options IV history from the local ThetaData archive")
    parser.add_argument("--underlying", action="append", help="underlying to seed; defaults to configured sleeve symbols")
    args = parser.parse_args()

    config = load_options_config()
    long_volatility = config["long_volatility"]
    symbols = args.underlying or long_volatility.get("underlyings", ["SPY"])
    client = OptionsPaperClient()
    journal = OptionsJournal(client.account_id)
    before_date = datetime.now(_ET).date()
    for symbol in symbols:
        result = seed_theta_iv_history(
            journal,
            str(symbol),
            before_date=before_date,
            config=long_volatility,
            rate=float(config.get("rate", 0.04)),
            dividend_yield=float(config.get("dividend_yield", 0.015)),
        )
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
