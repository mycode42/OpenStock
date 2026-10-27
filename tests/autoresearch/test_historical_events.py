from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from autoresearch import historical_events
from autoresearch.historical_events import _FedCalendarRows, _meeting_decision_date
from strategies.llm_driven import EventCalendar, LLMSignalGenerator, LLMStrategyConfig


ET = ZoneInfo("America/New_York")


def test_fed_calendar_rows_capture_year_and_cross_month_meetings():
    parser = _FedCalendarRows()
    parser.feed("""
      <h4><a>2024 FOMC Meetings</a></h4>
      <div class="row fomc-meeting">
        <div class="fomc-meeting__month"><strong>Apr/May</strong></div>
        <div class="fomc-meeting__date">30-1</div>
      </div>
    """)

    assert parser.rows == [{"year": 2024, "month": "Apr/May", "date": "30-1"}]


def test_historical_fed_headings_accept_abbreviated_month_names():
    assert _meeting_decision_date("Feb/Mar 28-1 Meeting - 2023").isoformat() == "2023-03-01"


def _filing(form, filed, accession, *, report_period="2025-01-31", items=""):
    return {
        "accessionNumber": [accession],
        "form": [form],
        "items": [items],
        "filingDate": [filed],
        "reportDate": [report_period],
        "acceptanceDateTime": [f"{filed}T15:00:00-05:00"],
    }


def _earnings_events(monkeypatch, filings, *, announcement_only=True):
    monkeypatch.setattr(historical_events, "_ticker_ciks", lambda tickers: ({"TEST": "0000000001"}, {}))
    monkeypatch.setattr(historical_events, "_load_company_submissions", lambda cik: filings)
    return historical_events._earnings_events(
        {"TEST"},
        bar_times=[datetime(2025, 2, 20, 20, 0, tzinfo=timezone.utc)],
        start_date=date(2025, 2, 1), end_date=date(2025, 3, 31),
        announcement_only=announcement_only,
    )[0]


def _signal_for_earnings_events(events):
    event_day = date(2025, 2, 20)
    bars = []
    cursor = event_day - timedelta(days=35)
    while cursor < event_day:
        if cursor.weekday() < 5:
            bars.append({"date": cursor.isoformat(), "o": 100, "h": 101, "l": 99, "c": 100, "v": 1000})
        cursor += timedelta(days=1)
    bars.append({"date": event_day.isoformat(), "o": 103, "h": 106, "l": 102, "c": 105.06, "v": 2500})
    generator = LLMSignalGenerator(EventCalendar(events), LLMStrategyConfig(enabled=True))
    return generator.generate(
        "TEST", bars,
        as_of=datetime.combine(event_day + timedelta(days=1), time(10), tzinfo=ET),
        universe={"TEST"}, regime="trending_up", strategy_state={},
    )


def test_earnings_calendar_accepts_8k_announcement(monkeypatch):
    events = _earnings_events(monkeypatch, [
        _filing("8-K", "2025-02-20", "0001", items="2.02, 9.01"),
    ])

    assert len(events) == 1
    assert "8-K Item 2.02" in events[0].source


def test_earnings_calendar_ignores_10q_by_default(monkeypatch):
    events = _earnings_events(monkeypatch, [
        _filing("10-Q", "2025-02-20", "0002"),
    ])

    assert events == []
    assert _signal_for_earnings_events(events) is None


def test_earnings_calendar_allows_10q_when_announcement_only_is_false(monkeypatch):
    events = _earnings_events(monkeypatch, [
        _filing("10-Q", "2025-02-20", "0002"),
    ], announcement_only=False)

    assert len(events) == 1
    assert "10-Q" in events[0].source


def test_duplicate_8k_earnings_filings_keep_earliest(monkeypatch):
    events = _earnings_events(monkeypatch, [
        _filing("8-K", "2025-02-24", "0004", items="2.02"),
        _filing("8-K", "2025-02-20", "0003", items="2.02"),
    ])

    assert len(events) == 1
    assert "0003" in events[0].source


def test_8k_announcement_takes_priority_over_statutory_filing(monkeypatch):
    events = _earnings_events(monkeypatch, [
        _filing("8-K", "2025-02-20", "0001", items="2.02"),
        _filing("10-Q", "2025-02-24", "0002"),
    ], announcement_only=False)

    assert len(events) == 1
    assert "8-K Item 2.02" in events[0].source


def test_earnings_calendar_keeps_the_45_day_retrospective_cutoff(monkeypatch):
    events = _earnings_events(monkeypatch, [
        _filing("10-Q", "2025-03-17", "0005"),
    ], announcement_only=False)

    assert events == []
