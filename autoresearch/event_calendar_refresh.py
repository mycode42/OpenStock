"""Refresh the live Algo event calendar from free scheduled-event sources.

Earnings dates and pre/post-market classifications come from Nasdaq's public
calendar endpoint. Macro dates come from the published BLS schedule and the
Federal Reserve's tentative meeting calendar. The generator writes atomically
and leaves the previous calendar in place when a source, account calendar, or
coordinator-state read fails.
"""

from __future__ import annotations

import argparse
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta
import logging
import os
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

from src.algo.alpaca import AlgoAlpaca
from src.algo.strategies.event_driven.calendar import CSV_FIELDS, EventCalendar
from src.algo.strategies.event_driven.models import ScheduledEvent
from src.algo.strategies.universe import configured_universe


LOG = logging.getLogger("algo_event_calendar")
ET = ZoneInfo("America/New_York")
NASDAQ_EARNINGS_URL = "https://api.nasdaq.com/api/calendar/earnings"
NASDAQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; AlgoTraderEventCalendar/1.0)",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}
BLS_SOURCE = "https://www.bls.gov/schedule/2026/home.htm"
FED_SOURCE = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

# Release dates published in the official BLS and Federal Reserve schedules.
# Add BLS dates as they are published; the report warns when a type has no
# upcoming date, while the live scanner independently expires stale snapshots.
MACRO_RELEASES = (
    ("cpi", date(2026, 10, 14), time(8, 30), BLS_SOURCE),
    ("cpi", date(2026, 11, 10), time(8, 30), BLS_SOURCE),
    ("cpi", date(2026, 12, 10), time(8, 30), BLS_SOURCE),
    ("nfp", date(2026, 11, 6), time(8, 30), BLS_SOURCE),
    ("nfp", date(2026, 12, 4), time(8, 30), BLS_SOURCE),
    ("fomc", date(2026, 10, 28), time(14, 0), FED_SOURCE),
    ("fomc", date(2026, 12, 9), time(14, 0), FED_SOURCE),
    ("fomc", date(2027, 1, 27), time(14, 0), FED_SOURCE),
    ("fomc", date(2027, 3, 17), time(14, 0), FED_SOURCE),
    ("fomc", date(2027, 4, 28), time(14, 0), FED_SOURCE),
    ("fomc", date(2027, 6, 9), time(14, 0), FED_SOURCE),
    ("fomc", date(2027, 7, 28), time(14, 0), FED_SOURCE),
    ("fomc", date(2027, 9, 15), time(14, 0), FED_SOURCE),
    ("fomc", date(2027, 10, 27), time(14, 0), FED_SOURCE),
    ("fomc", date(2027, 12, 8), time(14, 0), FED_SOURCE),
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _coordinator_universe() -> list[str]:
    return configured_universe("algo")["tickers"]


def _earnings_rows(day: date) -> list[dict]:
    response = requests.get(
        NASDAQ_EARNINGS_URL,
        params={"date": day.isoformat()},
        headers=NASDAQ_HEADERS,
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ValueError(f"Nasdaq returned an invalid earnings response for {day}")
    rows = data.get("rows") or []
    if not isinstance(rows, list):
        raise ValueError(f"Nasdaq returned invalid earnings rows for {day}")
    return [row for row in rows if isinstance(row, dict)]


def _query_earnings(days: list[date]) -> dict[date, list[dict]]:
    results: dict[date, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_earnings_rows, day): day for day in days}
        for future in as_completed(futures):
            day = futures[future]
            results[day] = future.result()
    return results


def _earnings_event(
    row: dict,
    event_day: date,
    *,
    universe: set[str],
    session_days: list[date],
    known_at: datetime,
) -> ScheduledEvent | None:
    ticker = str(row.get("symbol") or "").strip().upper()
    time_code = str(row.get("time") or "").strip().lower()
    if ticker not in universe:
        return None
    if "pre-market" in time_code:
        scheduled_at = datetime.combine(event_day, time(8, 0), tzinfo=ET)
        reaction_day = event_day
    elif "after-hours" in time_code:
        scheduled_at = datetime.combine(event_day, time(16, 0), tzinfo=ET)
        reaction_day = next((day for day in session_days if day > event_day), None)
        if reaction_day is None:
            return None
    else:
        # Do not guess a reaction session when Nasdaq has not supplied timing.
        return None
    if scheduled_at <= known_at.astimezone(ET):
        return None
    reaction_start = datetime.combine(reaction_day, time(9, 30), tzinfo=ET)
    reaction_end = datetime.combine(reaction_day, time(16, 0), tzinfo=ET) + timedelta(days=2)
    return ScheduledEvent(
        event_id=f"earnings-{ticker}-{event_day:%Y%m%d}",
        event_type="earnings",
        ticker=ticker,
        scheduled_at=scheduled_at,
        known_at=known_at,
        reaction_start_at=reaction_start,
        reaction_end_at=reaction_end,
        source=f"Nasdaq earnings calendar ({time_code}): {NASDAQ_EARNINGS_URL}?date={event_day.isoformat()}",
    )


def _macro_events(*, known_at: datetime) -> list[ScheduledEvent]:
    events = []
    for event_type, event_day, release_time, source in MACRO_RELEASES:
        scheduled_at = datetime.combine(event_day, release_time, tzinfo=ET)
        if scheduled_at <= known_at.astimezone(ET):
            continue
        reaction_start = datetime.combine(event_day, time(9, 30), tzinfo=ET)
        reaction_end = datetime.combine(event_day, time(16, 0), tzinfo=ET) + timedelta(days=2)
        if event_type == "fomc":
            reaction_start = scheduled_at
            reaction_end = datetime.combine(event_day, time(16, 0), tzinfo=ET) + timedelta(days=2)
        events.append(ScheduledEvent(
            event_id=f"{event_type}-{event_day:%Y%m%d}",
            event_type=event_type,
            ticker="SPY",
            scheduled_at=scheduled_at,
            known_at=known_at,
            reaction_start_at=reaction_start,
            reaction_end_at=reaction_end,
            source=source,
        ))
    return events


def _write_calendar(path: Path, events: list[ScheduledEvent]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for event in events:
                writer.writerow(event.to_dict())
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def refresh_calendar(
    *,
    output_path: Path | None = None,
    now: datetime | None = None,
    horizon_days: int = 35,
) -> dict:
    """Fetch current events and atomically publish a validated calendar."""
    if horizon_days < 7 or horizon_days > 60:
        raise ValueError("horizon_days must be between 7 and 60")
    now = now or datetime.now(ET)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(ET)
    universe = set(_coordinator_universe())
    through = now.date() + timedelta(days=horizon_days)
    earnings_days = [
        now.date() + timedelta(days=offset)
        for offset in range(horizon_days + 1)
        if (now.date() + timedelta(days=offset)).weekday() < 5
    ]

    # Include enough exchange sessions to resolve after-hours reports near the
    # end of the earnings scan horizon.
    alpaca = AlgoAlpaca()
    calendar = alpaca.calendar(now.date().isoformat(), (through + timedelta(days=14)).isoformat())
    if not isinstance(calendar, list) or not calendar:
        raise RuntimeError("Alpaca returned no market sessions for event timing")
    session_days = sorted({
        date.fromisoformat(str(item["date"]))
        for item in calendar
        if isinstance(item, dict) and item.get("date")
    })
    if not session_days:
        raise RuntimeError("Alpaca market calendar contained no valid session dates")

    daily_rows = _query_earnings(earnings_days)
    now_utc = now.astimezone(ZoneInfo("UTC"))
    all_earnings_rows = [row for rows in daily_rows.values() for row in rows]
    in_universe_rows = [
        row for row in all_earnings_rows
        if str(row.get("symbol") or "").strip().upper() in universe
    ]
    earnings = [
        event
        for day, rows in daily_rows.items()
        for row in rows
        if (event := _earnings_event(
            row, day, universe=universe, session_days=session_days, known_at=now_utc,
        )) is not None
    ]
    events = _macro_events(known_at=now_utc) + earnings
    events.sort(key=lambda item: (item.scheduled_at, item.ticker, item.event_id))
    EventCalendar(events)  # Validate unique event IDs and point-in-time timestamps before writing.
    if not events:
        raise RuntimeError("No future macro or timed earnings events were found; previous calendar retained")

    path = output_path or (_repo_root() / "data" / "event_calendar.csv")
    archive_path = (
        path.parent / "event_calendar_snapshots"
        / f"{now_utc:%Y%m%dT%H%M%SZ}.csv"
    )
    _write_calendar(archive_path, events)
    _write_calendar(path, events)
    # Verify the on-disk schema and timestamps before reporting success.
    loaded = EventCalendar.from_csv(path)
    diagnostics = loaded.diagnostics(now_utc)
    coverage_warnings = diagnostics.get("macro_coverage_warnings", [])
    return {
        **diagnostics,
        "universe_count": len(universe),
        "earnings_days_scanned": len(earnings_days),
        "earnings_rows_received": len(all_earnings_rows),
        "earnings_rows_in_universe": len(in_universe_rows),
        "timed_earnings_events": len(earnings),
        "macro_coverage_warnings": coverage_warnings,
        "skipped_in_universe_earnings_without_usable_timing": len(in_universe_rows) - len(earnings),
        "out_of_universe_earnings_rows": len(all_earnings_rows) - len(in_universe_rows),
        "calendar_path": str(path),
        "snapshot_path": str(archive_path),
        "refreshed_at": now_utc.isoformat(),
    }


def main() -> int:
    load_dotenv(_repo_root() / ".env")
    parser = argparse.ArgumentParser(description="Refresh scheduled events for the live Algo strategy")
    parser.add_argument("--horizon-days", type=int, default=35)
    args = parser.parse_args()
    result = refresh_calendar(horizon_days=args.horizon_days)
    print(result)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [algo_event_calendar] %(levelname)s %(message)s")
    raise SystemExit(main())
