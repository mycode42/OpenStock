#!/usr/bin/env python3
"""Collect Cboe SPY historical EOD option chains within the trial point cap."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import fcntl
import gzip
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time as time_module
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo
import base64


ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = ROOT / ".env"
MARKET_DB = ROOT / "data" / "algo_market.sqlite3"
RESEARCH_DIR = ROOT / "data" / "options" / "research"
ARCHIVE_DIR = RESEARCH_DIR / "cboe_spy_eod"
STATE_PATH = RESEARCH_DIR / "cboe_spy_eod_state.json"
LOCK_PATH = RESEARCH_DIR / "cboe_spy_eod.lock"
DATASET_PATH = RESEARCH_DIR / "chains.jsonl"
MANIFEST_PATH = RESEARCH_DIR / "cboe_spy_eod_manifest.json"

API_ROOT = "https://api.livevol.com/v1"
TOKEN_URL = "https://id.livevol.com/connect/token"
POINTS_PER_DAY = 500
CHAIN_REQUEST_POINTS = 3
INITIAL_PROBE_POINTS = 3  # The successful SPY entitlement probe before this collector.
QUOTA_WINDOW = timedelta(hours=25)  # A full daily reset window plus a safety hour.
TRIAL_LAST_RUN_DATE = date(2026, 11, 2)  # Trial was activated around 2026-10-20.
FIRST_EQUITY_SESSION = date(2016, 1, 4)
MAX_RESEARCH_DTE = 60
REQUIRED_RESEARCH_SESSIONS = 1013
ET = ZoneInfo("America/New_York")
UTC = timezone.utc


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True, separators=(",", ":"), allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def _read_env() -> dict[str, str]:
    values = dict(os.environ)
    if ENV_PATH.is_file():
        for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if value[:1] in {"'", '"'} and value[-1:] == value[:1]:
                value = value[1:-1]
            elif "#" in value:
                value = value.split("#", 1)[0].rstrip()
            values.setdefault(key, value)
    return values


def _credentials() -> tuple[str, str]:
    env = _read_env()
    client_id = env.get("CBOE_API_CLIENT_ID", "").strip()
    client_secret = env.get("CBOE_API_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise RuntimeError("Cboe client credentials are missing from the server environment")
    return client_id, client_secret


def _get_token() -> str:
    client_id, client_secret = _credentials()
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
    request = Request(
        TOKEN_URL,
        data=urlencode({"grant_type": "client_credentials"}).encode(),
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "OpenStock-CboeBackfill/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read())
    except HTTPError as error:
        raise RuntimeError(f"Cboe authentication returned HTTP {error.code}") from None
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Cboe authentication failed ({type(error).__name__})") from None
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not token:
        raise RuntimeError("Cboe authentication response did not contain an access token")
    return str(token)


def _load_spy_closes() -> dict[date, float]:
    if not MARKET_DB.is_file():
        raise RuntimeError(f"SPY daily-close database is missing: {MARKET_DB}")
    db_uri = f"file:{MARKET_DB}?mode=ro"
    db = sqlite3.connect(db_uri, uri=True)
    try:
        rows = db.execute(
            """SELECT timestamp, close FROM market_bar
               WHERE symbol = 'SPY' AND feed = 'sip' AND adjustment = 'split'
                 AND timeframe = '1Day'
               ORDER BY timestamp"""
        )
        closes = {}
        for stamp, close in rows:
            try:
                day = date.fromisoformat(str(stamp)[:10])
                value = float(close)
                if day >= FIRST_EQUITY_SESSION and value > 0 and math.isfinite(value):
                    closes[day] = value
            except (TypeError, ValueError):
                continue
        return closes
    finally:
        db.close()


def _read_state(now: datetime) -> dict:
    if STATE_PATH.is_file():
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(state, dict):
                return state
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "quota_window_started_at": now.isoformat(),
        "points_used_in_window": INITIAL_PROBE_POINTS,
        "quota_exhausted": False,
        "successful_requests": 0,
        "failed_requests": 0,
        "first_collected_at": now.isoformat(),
        "last_result": "initialized_with_prior_3_point_probe",
    }


def _save_state(state: dict) -> None:
    _atomic_json(STATE_PATH, state)


def _reset_quota_window_if_due(state: dict, now: datetime) -> dict:
    raw = state.get("quota_window_started_at")
    try:
        started = datetime.fromisoformat(str(raw)) if raw else None
    except ValueError:
        started = None
    if started is None or started.tzinfo is None or now - started >= QUOTA_WINDOW:
        state.update({
            "quota_window_started_at": now.isoformat(),
            "points_used_in_window": 0,
            "quota_exhausted": False,
            "last_result": "quota_window_reset_after_25_hours",
        })
    return state


def _request_json(url: str, token: str) -> tuple[dict | list, int]:
    request = Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "OpenStock-CboeBackfill/1.0",
    })
    try:
        with urlopen(request, timeout=60) as response:
            status = response.status
            payload = json.loads(response.read())
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace").lower()
        if error.code in {402, 403, 429} and any(
            marker in body for marker in ("point", "quota", "credit", "daily allowance")
        ):
            raise RuntimeError(f"QUOTA_HTTP_{error.code}") from None
        if error.code == 429:
            raise RuntimeError("RATE_LIMIT_HTTP_429") from None
        raise RuntimeError(f"HTTP_{error.code}") from None
    except (URLError, TimeoutError) as error:
        raise RuntimeError(f"NETWORK_{type(error).__name__}") from None
    except json.JSONDecodeError:
        raise RuntimeError("INVALID_JSON_RESPONSE") from None
    if not isinstance(payload, (dict, list)):
        raise RuntimeError("UNEXPECTED_JSON_SHAPE")
    return payload, status


def _option_rows(payload):
    if isinstance(payload, dict):
        options = payload.get("options")
        if isinstance(options, list):
            for option in options:
                if isinstance(option, dict):
                    yield option
            return
        for value in payload.values():
            yield from _option_rows(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from _option_rows(value)


def _store_raw(day: date, payload: dict | list) -> int:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    destination = ARCHIVE_DIR / f"{day.isoformat()}.json.gz"
    temp = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    entry = {
        "quote_date": day.isoformat(),
        "retrieved_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "provider": "cboe_allaccess_historical",
        "response": payload,
    }
    with gzip.open(temp, "wt", encoding="utf-8", compresslevel=6) as stream:
        json.dump(entry, stream, separators=(",", ":"), allow_nan=False)
    os.replace(temp, destination)
    return sum(1 for _ in _option_rows(payload))


def _existing_archive_dates() -> set[date]:
    result = set()
    for path in ARCHIVE_DIR.glob("????-??-??.json.gz") if ARCHIVE_DIR.exists() else ():
        try:
            result.add(date.fromisoformat(path.name[:10]))
        except ValueError:
            continue
    return result


def _fetch_chain(day: date, token: str) -> dict | list:
    query = urlencode({"root": "SPY", "symbol": "SPY", "date": day.isoformat()})
    payload, status = _request_json(
        f"{API_ROOT}/live/allaccess/market/option-and-underlying-quotes?{query}", token,
    )
    if status != 200:
        raise RuntimeError(f"HTTP_{status}")
    return payload


def _as_of_for(day: date) -> datetime:
    # Keep the UTC date aligned with the SPY session date for DTE calculations.
    return datetime.combine(day, time(16, 59, 59), tzinfo=ET).astimezone(UTC)


def _quote_at(day: date, raw_timestamp, fallback: datetime) -> str:
    if isinstance(raw_timestamp, str) and raw_timestamp.strip():
        value = raw_timestamp.strip()
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ET)
            return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
        except ValueError:
            try:
                parsed_time = time.fromisoformat(value)
                parsed = datetime.combine(day, parsed_time, tzinfo=ET)
                return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
            except ValueError:
                pass
    return fallback.isoformat().replace("+00:00", "Z")


def _finite_number(value, default=None):
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _research_contract(option: dict, day: date, as_of: datetime) -> dict | None:
    symbol = str(option.get("option") or "").upper()
    expiry_text = str(option.get("expiry") or "")[:10]
    right_code = str(option.get("option_type") or "").upper()
    strike = _finite_number(option.get("strike"))
    if not symbol or not expiry_text or right_code not in {"C", "P"} or not strike or strike <= 0:
        return None
    try:
        expiry = date.fromisoformat(expiry_text)
    except ValueError:
        return None
    dte = (expiry - day).days
    if dte < 0 or dte > MAX_RESEARCH_DTE:
        return None
    interest_value = _finite_number(option.get("open_interest"))
    open_interest = int(interest_value) if interest_value is not None else None
    implied_volatility = option.get("mid_iv")
    if implied_volatility is None:
        implied_volatility = option.get("iv")
    iv = _finite_number(implied_volatility)
    greeks = {
        key: value for key in ("delta", "gamma", "vega", "theta", "rho")
        if (value := _finite_number(option.get(key))) is not None
    }
    return {
        "symbol": symbol,
        "underlying_symbol": "SPY",
        "expiration_date": expiry_text,
        "type": "call" if right_code == "C" else "put",
        "strike_price": strike,
        "size": 100,
        "style": "american",
        "status": "active",
        "tradable": True,
        "open_interest": open_interest,
        "bid": _finite_number(option.get("option_bid"), 0.0),
        "ask": _finite_number(option.get("option_ask"), 0.0),
        "quote_at": _quote_at(day, option.get("timestamp"), as_of),
        "iv": iv,
        "greeks": greeks,
    }


def _read_archive_response(day: date) -> dict | list:
    path = ARCHIVE_DIR / f"{day.isoformat()}.json.gz"
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        entry = json.load(stream)
    return entry.get("response", {}) if isinstance(entry, dict) else {}


def _compile_research_dataset(closes: dict[date, float]) -> tuple[int, int]:
    archive_days = sorted(_existing_archive_dates(), reverse=True)
    rows = []
    total_contracts = 0
    for day in archive_days:
        spot = closes.get(day)
        if spot is None:
            continue
        payload = _read_archive_response(day)
        as_of = _as_of_for(day)
        contracts = [
            contract for option in _option_rows(payload)
            if (contract := _research_contract(option, day, as_of)) is not None
        ]
        if len(contracts) < 20:
            continue
        rows.append({
            "underlying": "SPY",
            "spot": spot,
            "underlying_close": spot,
            "as_of": as_of.isoformat().replace("+00:00", "Z"),
            "feed": "cboe_allaccess_historical",
            "contracts": contracts,
        })
        total_contracts += len(contracts)
        if len(rows) >= REQUIRED_RESEARCH_SESSIONS:
            break
    if len(rows) < REQUIRED_RESEARCH_SESSIONS:
        _atomic_json(MANIFEST_PATH, {
            "provider": "Cboe All Access API",
            "status": "collecting",
            "archived_sessions": len(archive_days),
            "research_ready_sessions": len(rows),
            "research_sessions_required": REQUIRED_RESEARCH_SESSIONS,
            "latest_quote_date": max(archive_days).isoformat() if archive_days else None,
            "earliest_quote_date": min(archive_days).isoformat() if archive_days else None,
            "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        })
        return len(rows), total_contracts
    rows.reverse()
    DATASET_PATH.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=DATASET_PATH.parent,
        prefix=".chains.", suffix=".tmp", delete=False,
    ) as stream:
        temp_path = Path(stream.name)
        for row in rows:
            stream.write(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False))
            stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp_path, DATASET_PATH)
    _atomic_json(MANIFEST_PATH, {
        "provider": "Cboe All Access API",
        "status": "research_ready",
        "dataset_path": str(DATASET_PATH),
        "dataset_sessions": len(rows),
        "research_sessions_required": REQUIRED_RESEARCH_SESSIONS,
        "contracts_in_dataset": total_contracts,
        "contract_dte_filter": {"min": 0, "max": MAX_RESEARCH_DTE},
        "quote_date_start": rows[0]["as_of"][:10],
        "quote_date_end": rows[-1]["as_of"][:10],
        "underlying_close_source": "local SPY split-adjusted SIP daily bars",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    })
    return len(rows), total_contracts


def _run_locked() -> int:
    now = datetime.now(UTC)
    local_day = now.astimezone(ET).date()
    if local_day > TRIAL_LAST_RUN_DATE:
        print("Cboe trial collection cutoff reached; no API request made.")
        return 0

    closes = _load_spy_closes()
    candidate_days = sorted(
        (day for day in closes if FIRST_EQUITY_SESSION <= day < local_day),
        reverse=True,
    )
    existing = _existing_archive_dates()
    missing_days = [day for day in candidate_days if day not in existing]
    state = _reset_quota_window_if_due(_read_state(now), now)
    points_used = max(0, int(state.get("points_used_in_window", 0)))
    if state.get("quota_exhausted"):
        print("Cboe reported the point quota exhausted for this window; waiting for the next reset window.")
        _save_state(state)
        return 0
    calls_available = max(0, (POINTS_PER_DAY - points_used) // CHAIN_REQUEST_POINTS)
    if not missing_days:
        ready, contracts = _compile_research_dataset(closes)
        print(f"Cboe archive complete for local SPY history: research_ready_sessions={ready}, contracts={contracts}.")
        _save_state(state)
        return 0
    if calls_available <= 0:
        print(f"Cboe point budget used for this window: {points_used}/{POINTS_PER_DAY}; awaiting the next reset window.")
        ready, contracts = _compile_research_dataset(closes)
        print(f"Archive status: stored={len(existing)}, research_ready_sessions={ready}, contracts={contracts}.")
        _save_state(state)
        return 0

    state["quota_window_started_at"] = state.get("quota_window_started_at") or now.isoformat()
    state["last_batch_started_at"] = now.isoformat()
    state["last_result"] = "collecting"
    _save_state(state)

    try:
        token = _get_token()
    except RuntimeError as error:
        state["last_result"] = str(error)
        state["failed_requests"] = int(state.get("failed_requests", 0)) + 1
        _save_state(state)
        print(f"Cboe collector stopped: {error}.")
        return 1

    attempted = 0
    saved = 0
    last_date = None
    for day in missing_days[:calls_available]:
        last_date = day
        # Reserve the points before making the request, so an interrupted request
        # is still accounted for conservatively on the next invocation.
        points_used += CHAIN_REQUEST_POINTS
        state["points_used_in_window"] = points_used
        state["last_requested_date"] = day.isoformat()
        state["last_result"] = "request_in_flight"
        _save_state(state)
        try:
            payload = _fetch_chain(day, token)
            count = _store_raw(day, payload)
        except RuntimeError as error:
            message = str(error)
            state["failed_requests"] = int(state.get("failed_requests", 0)) + 1
            if message.startswith("QUOTA_"):
                state["quota_exhausted"] = True
            state["last_result"] = message
            _save_state(state)
            print(f"Cboe collector stopped on {day.isoformat()}: {message}.")
            break
        except (OSError, TypeError, ValueError) as error:
            state["failed_requests"] = int(state.get("failed_requests", 0)) + 1
            state["last_result"] = f"LOCAL_{type(error).__name__}"
            _save_state(state)
            print(f"Cboe collector stopped on {day.isoformat()}: local {type(error).__name__}.")
            break
        saved += 1
        attempted += 1
        state["successful_requests"] = int(state.get("successful_requests", 0)) + 1
        state["last_completed_date"] = day.isoformat()
        state["last_result"] = "ok"
        _save_state(state)
        if attempted == 1 or attempted % 20 == 0:
            print(f"Cboe backfill progress: {attempted}/{calls_available}, date={day.isoformat()}, option_records={count}.")
        # Cboe also rate-limits request frequency independently of point usage.
        time_module.sleep(10.0)

    ready, contracts = _compile_research_dataset(closes)
    state["last_batch_finished_at"] = datetime.now(UTC).isoformat()
    state["last_result"] = "quota_window_filled" if points_used + CHAIN_REQUEST_POINTS > POINTS_PER_DAY else state.get("last_result", "ok")
    state["points_used_in_window"] = points_used
    state["last_batch_saved"] = saved
    state["research_ready_sessions"] = ready
    _save_state(state)
    print(
        f"Cboe batch complete: saved={saved}, points_accounted={points_used}/{POINTS_PER_DAY}, "
        f"last_date={last_date.isoformat() if last_date else 'none'}, "
        f"research_ready_sessions={ready}/{REQUIRED_RESEARCH_SESSIONS}, contracts={contracts}."
    )
    return 0


def main() -> int:
    RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Cboe backfill is already running; this invocation skipped.")
            return 0
        return _run_locked()


if __name__ == "__main__":
    raise SystemExit(main())
