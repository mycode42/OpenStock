from src.algo.research.historical_events import _FedCalendarRows, _meeting_decision_date


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
