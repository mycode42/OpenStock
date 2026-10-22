"""Build a point-in-time historical event calendar from public primary sources.

Earnings events use EDGAR 8-K Item 2.02 acceptance timestamps. CPI/NFP dates
come from annual BLS release schedules and FOMC decision dates from Federal
Reserve historical meeting pages. Reaction windows are anchored to the first
stored regular-session SPY bar at or after each public event timestamp.
"""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, time, timedelta, timezone
from html.parser import HTMLParser
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from typing import Iterable, Iterator
from zoneinfo import ZoneInfo

import requests

from market.store import DEFAULT_DB, SQLiteBarStore
from market.sec import (
    SEC_ARCHIVE_URL, SEC_SUBMISSIONS_URL,
    get_sec_json, get_sec_response, tickers_to_ciks,
)
from strategies.event_driven.calendar import CSV_FIELDS, EventCalendar
from strategies.event_driven.models import ScheduledEvent
from strategies.universe import configured_universe


LOG = logging.getLogger("historical_events")
from runtime.atomic import atomic_write_json
from runtime.paths import ROOT
ET = ZoneInfo("America/New_York")
UTC = timezone.utc
BLS_YEAR_URL = "https://www.bls.gov/schedule/{year}/home.htm"
FED_YEAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomchistorical{year}.htm"
FED_CALENDARS_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
REACTION_WINDOW = timedelta(days=2)
_RETROSPECTIVE_CUTOFF_DAYS = 45
_SOURCE_PRIORITY = {"8-K": 0, "10-Q": 1, "10-K": 2}
DEFAULT_OUTPUT = ROOT / "data" / "research" / "event_calendar_historical.csv"
SOURCE_CACHE = ROOT / "data" / "research" / "event_sources"

class _TableRows(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "tr" and self._row is None:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None:
            self._row.append(" ".join(" ".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
            self._cell = None


class _Headings(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.headings: list[str] = []
        self._heading: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"h4", "h5"}:
            self._heading = []

    def handle_data(self, data: str) -> None:
        if self._heading is not None:
            self._heading.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"h4", "h5"} and self._heading is not None:
            self.headings.append(" ".join(" ".join(self._heading).split()))
            self._heading = None


class _FedCalendarRows(HTMLParser):
    """Extract current archive rows that pair a year, month and meeting date."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, str | int]] = []
        self.year: int | None = None
        self.heading: list[str] | None = None
        self.row: dict[str, str | int] | None = None
        self.field: str | None = None
        self.field_text: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        classes = dict(attrs).get("class", "").split()
        if tag in {"h4", "h5"}:
            self.heading = []
        if tag == "div" and "fomc-meeting" in classes:
            self.row = {"year": self.year}
        if tag == "div" and self.row is not None:
            if "fomc-meeting__month" in classes:
                self.field, self.field_text = "month", []
            elif "fomc-meeting__date" in classes:
                self.field, self.field_text = "date", []

    def handle_data(self, data: str) -> None:
        if self.heading is not None:
            self.heading.append(data)
        if self.field is not None:
            self.field_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"h4", "h5"} and self.heading is not None:
            heading = " ".join(" ".join(self.heading).split())
            year_match = re.match(r"(\d{4}) FOMC Meetings", heading)
            if year_match:
                self.year = int(year_match.group(1))
            self.heading = None
        if tag == "div" and self.field is not None and self.row is not None:
            self.row[self.field] = " ".join(" ".join(self.field_text).split())
            self.field = None
            self.field_text = []
        if tag == "div" and self.row is not None and self.row.get("month") and self.row.get("date"):
            self.rows.append(self.row)
            self.row = None


def _get(url: str, *, sec: bool = False, timeout: int = 30) -> requests.Response:
    if sec:
        return get_sec_response(url, timeout=timeout)
    headers = {"User-Agent": "AlgoTraderResearch/1.6"}
    response = requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response


def _fetch_cached_text(url: str, path: Path, *, refresh_after: timedelta) -> str:
    if path.is_file() and datetime.now(UTC) - datetime.fromtimestamp(path.stat().st_mtime, UTC) < refresh_after:
        return path.read_text(encoding="utf-8")
    response = _get(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(response.text, encoding="utf-8")
    return response.text


def _json_payload(url: str, *, cache_path: Path | None = None, sec: bool = False) -> dict:
    if cache_path is not None and cache_path.is_file() and not sec:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    if sec:
        return get_sec_json(url, cache_path=cache_path, timeout=30)
    response = _get(url, sec=sec)
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object from {url}")
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(cache_path, payload, indent=None, sort_keys=True)
    return payload


def _parse_et_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("event timestamp has no timezone")
    return parsed.astimezone(ET)


def _first_bar_at_or_after(event_at: datetime, bar_times: list[datetime]) -> datetime | None:
    from bisect import bisect_left

    index = bisect_left(bar_times, event_at.astimezone(UTC))
    return bar_times[index].astimezone(ET) if index < len(bar_times) else None


def _event(
    *, event_id: str, event_type: str, ticker: str, scheduled_at: datetime,
    known_at: datetime, source: str, bar_times: list[datetime], end_date: date,
) -> ScheduledEvent | None:
    reaction_start = _first_bar_at_or_after(scheduled_at, bar_times)
    if reaction_start is None or reaction_start.date() > end_date:
        return None
    return ScheduledEvent(
        event_id=event_id, event_type=event_type, ticker=ticker,
        scheduled_at=scheduled_at.astimezone(ET), known_at=known_at.astimezone(ET),
        reaction_start_at=reaction_start,
        reaction_end_at=reaction_start + REACTION_WINDOW,
        source=source,
    )


def _ticker_ciks(tickers: set[str]) -> tuple[dict[str, str], dict]:
    mapping = tickers_to_ciks(tickers)
    return mapping, {"tickers_requested": len(tickers), "tickers_mapped": len(mapping), "unmapped_tickers": sorted(tickers - set(mapping))}


def _submission_files(payload: dict) -> Iterator[dict]:
    filings = payload.get("filings", {})
    if not isinstance(filings, dict):
        return []
    recent = filings.get("recent")
    if isinstance(recent, dict):
        yield recent
    files = filings.get("files") or []
    for item in files:
        if isinstance(item, dict) and item.get("name"):
            yield item


def _load_company_submissions(cik: str) -> list[dict]:
    """Fetch current and archived submissions, caching older immutable files."""
    root = SOURCE_CACHE / "sec" / "submissions"
    main_path = root / f"CIK{cik}.json"
    main = _json_payload(SEC_SUBMISSIONS_URL.format(cik=cik), sec=True)
    atomic_write_json(main_path, main, indent=None, sort_keys=True)
    recent = main.get("filings", {}).get("recent")
    records: list[dict] = [recent] if isinstance(recent, dict) else []
    for index, meta in enumerate(_submission_files(main)):
        if index == 0:
            continue
        filename = str(meta["name"])
        cache = root / filename
        if cache.exists():
            older = json.loads(cache.read_text(encoding="utf-8"))
        else:
            older = _json_payload(SEC_ARCHIVE_URL.format(filename=filename), sec=True)
            atomic_write_json(cache, older, indent=None, sort_keys=True)
        recent = older.get("filings", {}).get("recent")
        if isinstance(recent, dict):
            records.append(recent)
    return records


def _earnings_events(
    tickers: set[str], *, bar_times: list[datetime], start_date: date, end_date: date,
    announcement_only: bool = True,
) -> tuple[list[ScheduledEvent], dict]:
    # ETFs file reports but do not publish portfolio-company earnings.
    earnings_tickers = tickers - {"SPY", "QQQ", "GLD", "TLT"}
    cik_by_ticker, coverage = _ticker_ciks(earnings_tickers)
    events = []
    counts = {
        "companies_queried": 0, "item_2_02_filings": 0,
        "qualifying_filings": 0, "events_written": 0,
        "missing_acceptance_time": 0,
    }
    for ticker, cik in sorted(cik_by_ticker.items()):
        counts["companies_queried"] += 1
        filings = _load_company_submissions(cik)
        for filing in _qualifying_earnings_filings(filings, announcement_only=announcement_only):
            filed = date.fromisoformat(filing["filing_date"])
            if not start_date <= filed <= end_date:
                continue
            counts["qualifying_filings"] += 1
            form = filing["source_type"]
            if form == "8-K":
                counts["item_2_02_filings"] += 1
            try:
                known_at = _parse_et_timestamp(filing["acceptance_date_time"])
            except ValueError:
                counts["missing_acceptance_time"] += 1
                continue
            accession = filing["accession"]
            accession_path = accession.replace("-", "")
            source_label = "8-K Item 2.02" if form == "8-K" else form
            event_id = (
                f"sec-8k-2.02-{ticker}-{accession}"
                if form == "8-K" else f"sec-{form.lower()}-{ticker}-{accession}"
            )
            source = (
                f"SEC EDGAR {source_label}; CIK {cik}; accession {accession}; "
                f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_path}/{accession_path}-index.htm"
            )
            event = _event(
                event_id=event_id, event_type="earnings",
                ticker=ticker, scheduled_at=known_at, known_at=known_at,
                source=source, bar_times=bar_times, end_date=end_date,
            )
            if event is not None:
                events.append(event)
                counts["events_written"] += 1
    return events, {
        **coverage, **counts,
        "announcement_only": announcement_only,
        "configured_universe_ticker_count": len(tickers),
        "etfs_excluded_from_earnings": sorted(tickers - earnings_tickers),
    }


def _qualifying_earnings_filings(
    filings: list[dict], *, announcement_only: bool = True,
) -> list[dict[str, str]]:
    """Select one timely earnings filing per report period.

    By default, only 8-K Item 2.02 announcements qualify. Setting
    ``announcement_only=False`` admits 10-Q/K as fallback events. Duplicate
    announcements for a period keep the earliest filing date, and the
    45-day cutoff removes filings too far after their reported period.
    """
    best: dict[str, tuple[int, str, dict[str, str]]] = {}
    seen_accessions: set[str] = set()
    candidates: list[dict[str, str]] = []
    statutory_periods: set[str] = set()
    for filing_rows in filings:
        if not isinstance(filing_rows, dict):
            continue
        values = {
            name: filing_rows.get(name, [])
            for name in ("accessionNumber", "form", "items", "filingDate", "reportDate", "acceptanceDateTime")
        }
        required = (
            values["accessionNumber"], values["form"], values["filingDate"],
            values["reportDate"], values["acceptanceDateTime"],
        )
        if not all(isinstance(value, list) for value in required):
            continue
        length = min(map(len, required), default=0)
        for index in range(length):
            accession = str(values["accessionNumber"][index] or "")
            form = str(values["form"][index] or "")
            if not accession or accession in seen_accessions:
                continue
            if form not in _SOURCE_PRIORITY:
                continue
            filing_date = str(values["filingDate"][index] or "")
            report_period = str(values["reportDate"][index] or "")
            acceptance_date_time = str(values["acceptanceDateTime"][index] or "")
            try:
                date.fromisoformat(filing_date)
                date.fromisoformat(report_period)
                _parse_et_timestamp(acceptance_date_time)
            except ValueError:
                continue
            if form in {"10-Q", "10-K"}:
                statutory_periods.add(report_period)
            if announcement_only and form != "8-K":
                continue
            items_col = values["items"]
            items = {
                part.strip() for part in str(
                    items_col[index] if isinstance(items_col, list) and index < len(items_col) else "",
                ).split(",")
            }
            if form == "8-K" and "2.02" not in items:
                continue
            seen_accessions.add(accession)
            candidates.append({
                "accession": accession,
                "source_type": form,
                "filing_date": filing_date,
                "report_period": report_period,
                "acceptance_date_time": acceptance_date_time,
            })

    # On an 8-K, SEC reportDate is the date of the reported event. Use the
    # nearest preceding 10-Q/K period end as the earnings report period where
    # available, so an announcement and its later statutory filing dedupe.
    sorted_statutory_periods = sorted(statutory_periods)
    for candidate in candidates:
        if candidate["source_type"] == "8-K":
            filed = date.fromisoformat(candidate["filing_date"])
            prior_periods = [
                period for period in sorted_statutory_periods
                if date.fromisoformat(period) <= filed
            ]
            if prior_periods:
                candidate["report_period"] = prior_periods[-1]
        lag = (
            date.fromisoformat(candidate["filing_date"])
            - date.fromisoformat(candidate["report_period"])
        ).days
        if lag >= _RETROSPECTIVE_CUTOFF_DAYS:
            continue
        form = candidate["source_type"]
        key = (_SOURCE_PRIORITY[form], candidate["filing_date"])
        report_period = candidate["report_period"]
        if report_period not in best or key < best[report_period][:2]:
            best[report_period] = (key[0], key[1], candidate)
    return [filing for _, _, filing in best.values()]


def _parse_bls_date(value: str) -> date | None:
    normalized = " ".join(value.split())
    for pattern in ("%A, %B %d, %Y", "%A, %B %d, %Y %I:%M %p"):
        try:
            return datetime.strptime(normalized, pattern).date()
        except ValueError:
            pass
    return None


def _bls_events(
    *, years: Iterable[int], bar_times: list[datetime], end_date: date,
) -> tuple[list[ScheduledEvent], dict]:
    events = []
    pages = 0
    rows_seen = 0
    for year in years:
        url = BLS_YEAR_URL.format(year=year)
        refresh = timedelta(hours=6) if year == datetime.now(ET).year else timedelta(days=30)
        html = _fetch_cached_text(url, SOURCE_CACHE / "bls" / f"{year}.html", refresh_after=refresh)
        pages += 1
        parser = _TableRows()
        parser.feed(html)
        for row in parser.rows:
            if len(row) < 3:
                continue
            event_day = _parse_bls_date(row[0])
            release = row[2]
            if event_day is None or event_day > end_date:
                continue
            if "Consumer Price Index for" in release:
                event_type = "cpi"
            elif "Employment Situation for" in release:
                event_type = "nfp"
            else:
                continue
            rows_seen += 1
            try:
                release_time = datetime.strptime(row[1], "%I:%M %p").time()
            except ValueError:
                continue
            scheduled = datetime.combine(event_day, release_time, tzinfo=ET)
            event = _event(
                event_id=f"bls-{event_type}-{event_day:%Y%m%d}",
                event_type=event_type, ticker="SPY", scheduled_at=scheduled,
                # This strategy reacts only after release; making the event
                # visible at release time avoids retrospective calendar look-ahead.
                known_at=scheduled, source=url, bar_times=bar_times, end_date=end_date,
            )
            if event is not None:
                events.append(event)
    return events, {"annual_pages_fetched": pages, "release_rows_matched": rows_seen, "events_written": len(events)}


_FED_HEADING = re.compile(
    r"^([A-Za-z]+)(?:/([A-Za-z]+))?\s+(\d{1,2})(?:\s*-\s*(\d{1,2}))?\s+Meeting\s*-\s*(\d{4})$",
)


def _meeting_decision_date(heading: str) -> date | None:
    match = _FED_HEADING.match(heading.strip())
    if not match:
        return None
    first_month, second_month, first_day, last_day, year = match.groups()
    month = second_month or first_month
    try:
        month_num = datetime.strptime(month, "%B").month
    except ValueError:
        month_num = datetime.strptime(month, "%b").month
    day_num = int(last_day or first_day)
    return date(int(year), month_num, day_num)


def _fed_events(
    *, years: Iterable[int], bar_times: list[datetime], end_date: date,
) -> tuple[list[ScheduledEvent], dict]:
    events = []
    selected_years = sorted(set(years))
    pages = 0
    meetings_seen = 0
    for year in selected_years:
        if year >= 2021:
            continue
        url = FED_YEAR_URL.format(year=year)
        refresh = timedelta(hours=6) if year == datetime.now(ET).year else timedelta(days=30)
        html = _fetch_cached_text(url, SOURCE_CACHE / "fed" / f"{year}.html", refresh_after=refresh)
        pages += 1
        parser = _Headings()
        parser.feed(html)
        for heading in parser.headings:
            meeting_day = _meeting_decision_date(heading)
            if meeting_day is None or meeting_day > end_date:
                continue
            meetings_seen += 1
            scheduled = datetime.combine(meeting_day, time(14, 0), tzinfo=ET)
            event = _event(
                event_id=f"fomc-{meeting_day:%Y%m%d}", event_type="fomc", ticker="SPY",
                scheduled_at=scheduled, known_at=scheduled, source=url,
                bar_times=bar_times, end_date=end_date,
            )
            if event is not None:
                events.append(event)
    modern_years = {year for year in selected_years if year >= 2021}
    if modern_years:
        url = FED_CALENDARS_URL
        html = _fetch_cached_text(
            url, SOURCE_CACHE / "fed" / "fomc-calendars.html",
            refresh_after=timedelta(hours=6),
        )
        pages += 1
        parser = _FedCalendarRows()
        parser.feed(html)
        for row in parser.rows:
            year = int(row["year"])
            if year not in modern_years:
                continue
            month_parts = str(row["month"]).split("/")
            day_match = re.search(r"(\d{1,2})\s*-\s*(\d{1,2})", str(row["date"]))
            if not day_match:
                continue
            first_day, final_day = map(int, day_match.groups())
            month_name = month_parts[-1] if len(month_parts) > 1 or final_day < first_day else month_parts[0]
            month_value = None
            for pattern in ("%B", "%b"):
                try:
                    month_value = datetime.strptime(month_name.strip(), pattern).month
                    break
                except ValueError:
                    continue
            if month_value is None:
                continue
            meeting_day = date(year, month_value, final_day)
            if meeting_day > end_date:
                continue
            meetings_seen += 1
            scheduled = datetime.combine(meeting_day, time(14, 0), tzinfo=ET)
            event = _event(
                event_id=f"fomc-{meeting_day:%Y%m%d}", event_type="fomc", ticker="SPY",
                scheduled_at=scheduled, known_at=scheduled, source=url,
                bar_times=bar_times, end_date=end_date,
            )
            if event is not None:
                events.append(event)
    return events, {"annual_pages_fetched": pages, "meetings_matched": meetings_seen, "events_written": len(events)}


def _write_calendar(path: Path, events: list[ScheduledEvent]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for event in events:
                writer.writerow(event.to_dict())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def build_historical_event_calendar(
    *, start: date, end: date, db: str | Path = DEFAULT_DB,
    output: str | Path = DEFAULT_OUTPUT, announcement_only: bool = True,
) -> dict:
    """Fetch public event histories and publish a backtest-ready calendar.

    Earnings are limited to SEC 8-K Item 2.02 announcements by default;
    ``announcement_only=False`` restores 10-Q/K fallback events.
    """
    if end < start:
        raise ValueError("event calendar end date must not be before start date")
    store = SQLiteBarStore(db)
    spy_bars = store.bars(
        ["SPY"], feed="sip", timeframe="5Min", start=start.isoformat(),
        end=end.isoformat(), adjustment="split",
    ).get("SPY", [])
    bar_times = sorted({
        datetime.fromisoformat(str(bar["t"]).replace("Z", "+00:00")).astimezone(UTC)
        for bar in spy_bars
    })
    if not bar_times:
        raise ValueError("SPY Plus SIP 5-minute bars are required to align historical event reactions")

    tickers = set(configured_universe("algo")["tickers"])
    earnings, earnings_report = _earnings_events(
        tickers, bar_times=bar_times, start_date=start, end_date=end,
        announcement_only=announcement_only,
    )
    years = range(start.year, end.year + 1)
    macro_bls, bls_report = _bls_events(years=years, bar_times=bar_times, end_date=end)
    macro_fed, fed_report = _fed_events(years=years, bar_times=bar_times, end_date=end)
    events = sorted(
        [*earnings, *macro_bls, *macro_fed],
        key=lambda event: (event.scheduled_at, event.event_id),
    )
    # EventCalendar validates unique IDs and time ordering before publication.
    calendar = EventCalendar(events)
    path = Path(output)
    _write_calendar(path, events)
    verified = EventCalendar.from_csv(path)
    diagnostics = verified.diagnostics()
    return {
        "status": "completed",
        "calendar_path": str(path.resolve()),
        "range_start": start.isoformat(), "range_end": end.isoformat(),
        **diagnostics,
        "earnings_source": earnings_report,
        "bls_source": bls_report,
        "fed_source": fed_report,
        "point_in_time_policy": {
            "earnings_known_at": (
                "SEC acceptanceDateTime; filed Item 2.02 8-K"
                if announcement_only else
                "SEC acceptanceDateTime; 8-K Item 2.02 preferred, with 10-Q/K fallback"
            ),
            "macro_known_at": "scheduled release timestamp; post-release-only and conservative",
            "reaction_start": "first stored regular-session SPY 5-minute bar at or after event time",
            "earnings_caveat": "EDGAR acceptance may lag the issuer's initial press release; reactions start no earlier than filing availability",
            "universe_caveat": "uses current fixed Algo symbols; historical universe membership and delisted constituents are not reconstructed",
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build SEC/BLS/Fed historical events for Algo backtests")
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default=None, help="defaults to the last stored SPY SIP session")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument(
        "--allow-statutory-filings", dest="announcement_only", action="store_false",
        help="allow 10-Q/K earnings filings when no 8-K announcement exists for the period",
    )
    parser.set_defaults(announcement_only=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = SQLiteBarStore(args.db)
    coverage = next((row for row in store.coverage() if (
        row["symbol"] == "SPY" and row["feed"] == "sip" and row["timeframe"] == "5Min"
        and row["adjustment"] == "split"
    )), None)
    if coverage is None:
        raise SystemExit("SPY SIP 5-minute split-adjusted history is missing")
    end = date.fromisoformat(args.end) if args.end else date.fromisoformat(coverage["last"][:10])
    report = build_historical_event_calendar(
        start=date.fromisoformat(args.start), end=end, db=args.db, output=args.output,
        announcement_only=args.announcement_only,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
