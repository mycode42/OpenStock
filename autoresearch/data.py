"""Algo research data loading and freshness management."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path

import requests

from .settings import (
    ADJUSTMENT, FEED, INITIAL_HISTORY_DATE, MIN_COVERED_UNIVERSE,
    RESEARCH_START, RESEARCH_WARMUP_DAYS, FORWARD_TRIAL_MAX_DAYS,
    SIP_EXTENDED_LOCAL_FEED, ET,
)
from .artifacts.files import _atomic_json
from autoresearch.backtest import _regular_session_bars
from market.store import SQLiteBarStore, fetch_alpaca_bars
from strategies.event_driven import EventCalendar
from strategies.multi_factor.features import aggregate_intraday_bars
from strategies.universe import configured_universe
from market.data import data_tier, regular_feed
from autoresearch.backtest.market_sessions import extended_session_bars


def _parse_date(value: str) -> date:
    return date.fromisoformat(str(value)[:10])


def load_algo_universe() -> tuple[list[str], dict]:
    """Research symbols: the 117-name candidate pool, traded point-in-time.

    Membership per session comes from the selector (see
    ``autoresearch.backtest.membership``), so the pool is only the candidate set.
    """
    from portfolio.config import candidate_pool_tickers

    symbols = candidate_pool_tickers()
    if not symbols:
        raise ValueError("Candidate pool is empty")
    return symbols, {
        "universe_source": "candidate_pool_point_in_time",
    }

def _bar_coverage(store: SQLiteBarStore) -> dict[str, dict]:
    return {
        row["symbol"]: row for row in store.coverage()
        if row["feed"] == FEED and row["adjustment"] == ADJUSTMENT and row["timeframe"] == "5Min"
    }


def _refresh_forward_session_feed(
    store: SQLiteBarStore,
    *,
    selected_symbols: list[str],
    end_date: date,
    remote_feed: str,
    local_feed: str,
    session: str,
    fetch_bars_fn,
) -> dict:
    """Backfill one forward-only session feed, then refresh its latest date."""
    floor = end_date - timedelta(days=FORWARD_TRIAL_MAX_DAYS)
    coverage = {
        row["symbol"]: row for row in store.coverage()
        if row["feed"] == local_feed and row["adjustment"] == ADJUSTMENT
        and row["timeframe"] == "5Min"
    }
    groups: dict[date, list[str]] = defaultdict(list)
    for symbol in selected_symbols:
        row = coverage.get(symbol)
        # Re-fetch the last stored date so a nightly run fills in the rest of
        # today's extended session and overwrites any partial historical bar.
        start_date = max(floor, _parse_date(row["last"])) if row else floor
        if start_date <= end_date:
            groups[start_date].append(symbol)

    rows_written = 0
    request_count = 0
    request_end_date = end_date + timedelta(days=1)
    request_end = datetime.combine(
        request_end_date, time.min, tzinfo=ET,
    ).astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    discarded_non_session_bars = 0
    for start_date, symbols in sorted(groups.items()):
        request_start = f"{start_date.isoformat()}T00:00:00Z"
        try:
            bars = fetch_bars_fn(
                symbols, timeframe="5Min", start=request_start, end=request_end,
                feed=remote_feed, adjustment=ADJUSTMENT, mode="algo",
            )
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if remote_feed == "boats" and status in {400, 403}:
                return {
                    "status": "unavailable", "feed": remote_feed,
                    "local_feed": local_feed, "rows_written": rows_written,
                    "request_batches": request_count,
                    "error": f"Alpaca returned HTTP {status} for the BOATS feed",
                    "symbols_with_existing_data": len(coverage),
                }
            raise
        filtered, outside, invalid = extended_session_bars(bars, session=session)
        discarded_non_session_bars += outside
        rows_written += store.write_bars(
            filtered, feed=local_feed, timeframe="5Min", adjustment=ADJUSTMENT,
            data_tier="algo_trader_plus", requested_start=request_start,
            requested_end=request_end,
        )
        request_count += 1
        if invalid:
            raise ValueError(
                f"{invalid} invalid {remote_feed} bars were returned for forward replay"
            )
    updated_coverage = {
        row["symbol"] for row in store.coverage()
        if row["feed"] == local_feed and row["adjustment"] == ADJUSTMENT
        and row["timeframe"] == "5Min"
    }
    return {
        "status": "available" if updated_coverage else "no_bars",
        "feed": remote_feed, "local_feed": local_feed,
        "rows_written": rows_written, "request_batches": request_count,
        "symbols_with_data": len(updated_coverage),
        "discarded_non_session_bars": discarded_non_session_bars,
        "history_floor": floor.isoformat(),
    }

def _refresh_incremental(
    store: SQLiteBarStore,
    *,
    selected_symbols: list[str],
    end_date: date,
    data_tier_fn=None,
    regular_feed_fn=None,
    fetch_bars_fn=None,
) -> dict:
    """Fetch regular SIP history plus recent extended/overnight trial bars."""
    data_tier_fn = data_tier_fn or data_tier
    regular_feed_fn = regular_feed_fn or regular_feed
    fetch_bars_fn = fetch_bars_fn or fetch_alpaca_bars
    if data_tier_fn() != "algo_trader_plus" or regular_feed_fn() != FEED:
        raise ValueError("Algo AutoResearch requires the Algo Trader Plus SIP feed")
    coverage = _bar_coverage(store)
    known = [symbol for symbol in selected_symbols if symbol in coverage]
    missing_history = sorted(set(selected_symbols) - set(known))
    groups: dict[date, list[str]] = defaultdict(list)
    for symbol in selected_symbols:
        last_date = _parse_date(coverage[symbol]["last"]) if symbol in coverage else INITIAL_HISTORY_DATE - timedelta(days=1)
        request_start = last_date + timedelta(days=1)
        if request_start <= end_date:
            groups[request_start].append(symbol)
    rows_written = 0
    requests_made = 0
    for start_date, symbols in sorted(groups.items()):
        request_start = f"{start_date.isoformat()}T00:00:00Z"
        request_end = f"{end_date.isoformat()}T23:59:59Z"
        bars = fetch_bars_fn(
            symbols, timeframe="5Min", start=request_start, end=request_end,
            feed=FEED, adjustment=ADJUSTMENT, mode="algo",
        )
        bars, _, _ = _regular_session_bars(bars)
        rows_written += store.write_bars(
            bars, feed=FEED, timeframe="5Min", adjustment=ADJUSTMENT,
            data_tier="algo_trader_plus", requested_start=request_start,
            requested_end=request_end,
        )
        requests_made += 1

    refreshed_coverage = _bar_coverage(store)
    covered_symbols = [symbol for symbol in selected_symbols if symbol in refreshed_coverage]
    if len(covered_symbols) < MIN_COVERED_UNIVERSE:
        raise ValueError(
            f"Only {len(covered_symbols)} selected research symbols have stored split-adjusted SIP history "
            f"after refresh; at least {MIN_COVERED_UNIVERSE} are required"
        )

    from strategies.regime_features import NON_EQUITY_CONTEXT_SYMBOLS

    forward_trial_symbols = [
        symbol for symbol in selected_symbols
        if symbol not in NON_EQUITY_CONTEXT_SYMBOLS
    ]
    sip_extended = _refresh_forward_session_feed(
        store, selected_symbols=forward_trial_symbols, end_date=end_date,
        remote_feed=FEED, local_feed=SIP_EXTENDED_LOCAL_FEED,
        session="extended", fetch_bars_fn=fetch_bars_fn,
    )
    boats = _refresh_forward_session_feed(
        store, selected_symbols=forward_trial_symbols, end_date=end_date,
        remote_feed="boats", local_feed="boats", session="overnight",
        fetch_bars_fn=fetch_bars_fn,
    )

    spy_daily = next((row for row in store.coverage() if (
        row["feed"] == FEED and row["adjustment"] == ADJUSTMENT
        and row["timeframe"] == "1Day" and row["symbol"] == "SPY"
    )), None)
    daily_rows_written = 0
    if spy_daily is None:
        from portfolio.config import ALGO_REGIME_WARMUP_CALENDAR_DAYS

        daily_start = (
            RESEARCH_START - timedelta(days=ALGO_REGIME_WARMUP_CALENDAR_DAYS)
        ).isoformat()
    else:
        last_daily = _parse_date(spy_daily["last"])
        daily_start = (last_daily + timedelta(days=1)).isoformat()
    if _parse_date(daily_start) <= end_date:
        request_start = f"{daily_start}T00:00:00Z"
        request_end = f"{end_date.isoformat()}T23:59:59Z"
        daily_bars = fetch_bars_fn(
            ["SPY"], timeframe="1Day", start=request_start, end=request_end,
            feed=FEED, adjustment=ADJUSTMENT, mode="algo",
        )
        daily_rows_written = store.write_bars(
            daily_bars, feed=FEED, timeframe="1Day", adjustment=ADJUSTMENT,
            data_tier="algo_trader_plus", requested_start=request_start,
            requested_end=request_end,
        )
    return {
        "feed": FEED, "adjustment": ADJUSTMENT, "requested_through": end_date.isoformat(),
        "symbols_refreshed": sum(len(values) for values in groups.values()),
        "missing_selected_history": missing_history,
        "request_batches": requests_made,
        "intraday_rows_written": rows_written,
        "forward_session_data": {
            "extended_hours": sip_extended,
            "overnight": boats,
        },
        "spy_daily_rows_written": daily_rows_written,
    }

def _load_daily_history(
    store: SQLiteBarStore,
    symbols: list[str],
    *,
    start: str,
    end: str,
) -> dict[str, list[dict]]:
    """Aggregate one ticker at a time to keep 5-minute data memory bounded."""
    output = {}
    for symbol in dict.fromkeys(symbols):
        intraday = store.bars(
            [symbol], feed=FEED, timeframe="5Min", start=start, end=end,
            adjustment=ADJUSTMENT,
        )
        output[symbol] = aggregate_intraday_bars(intraday).get(symbol, [])
        del intraday
    spy_daily = store.bars(
        ["SPY"], feed=FEED, timeframe="1Day", start=start, end=end,
        adjustment=ADJUSTMENT,
    ).get("SPY", [])
    output["SPY"] = [{"t": bar["t"], "c": bar["c"]} for bar in spy_daily]
    return output

def _latest_dates(store: SQLiteBarStore, symbols: list[str]) -> tuple[date, date, list[str]]:
    coverage = _bar_coverage(store)
    available = [symbol for symbol in symbols if symbol in coverage]
    missing = sorted(set(symbols) - set(available))
    if len(available) < MIN_COVERED_UNIVERSE:
        raise ValueError(f"Only {len(available)} fixed Algo-universe tickers have SIP bars")
    # Keep the earliest date in the local store. Recently listed members simply
    # begin contributing once their own history exists.
    start = min(_parse_date(coverage[symbol]["first"]) for symbol in available)
    end = min(_parse_date(coverage[symbol]["last"]) for symbol in available)
    spy_daily = next((row for row in store.coverage() if (
        row["feed"] == FEED and row["adjustment"] == ADJUSTMENT
        and row["timeframe"] == "1Day" and row["symbol"] == "SPY"
    )), None)
    if spy_daily is None:
        raise ValueError("SPY split-adjusted SIP daily history is missing")
    end = min(end, _parse_date(spy_daily["last"]))
    start = max(start, _parse_date(spy_daily["first"]))
    return start, end, missing

def _prepare_event_calendar(
    *, start: date, end: date, db: str, output_dir: Path,
) -> tuple[EventCalendar | None, dict]:
    from autoresearch.historical_events import build_historical_event_calendar

    calendar_path = output_dir / "event-calendar-historical.csv"
    metadata_path = output_dir / "event-calendar-historical.json"
    try:
        report = build_historical_event_calendar(
            start=start, end=end, db=db, output=calendar_path,
        )
        _atomic_json(metadata_path, report)
        return EventCalendar.from_csv(calendar_path), report
    except Exception as exc:
        # A previously built calendar is safe to reuse only if it covers this
        # exact research interval; otherwise event profiles are omitted below.
        try:
            previous = json.loads(metadata_path.read_text())
            if (
                calendar_path.is_file()
                and date.fromisoformat(previous["range_start"]) <= start
                and date.fromisoformat(previous["range_end"]) >= end
            ):
                calendar = EventCalendar.from_csv(calendar_path)
                return calendar, {
                    **previous,
                    "status": "reused_cached_calendar_after_source_error",
                    "source_error": f"{type(exc).__name__}: {exc}",
                }
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None, {
            "status": "unavailable",
            "range_start": start.isoformat(), "range_end": end.isoformat(),
            "calendar_path": str(calendar_path.resolve()),
            "error": f"{type(exc).__name__}: {exc}",
        }

