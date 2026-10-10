from datetime import date, datetime, timezone

from src.algo.research.event_calendar_refresh import _earnings_event, _macro_events
from src.strategies.event_driven.config import EventStrategyConfig
from src.strategies.registry import ALGO_DEFAULT_PARAMETERS


def test_after_hours_earnings_use_next_alpaca_session():
    event = _earnings_event(
        {"symbol": "NVDA", "time": "time-after-hours"},
        date(2026, 10, 9),
        universe={"NVDA"},
        session_days=[date(2026, 10, 12)],
        known_at=datetime(2026, 10, 8, 15, tzinfo=timezone.utc),
    )

    assert event is not None
    assert event.scheduled_at.isoformat() == "2026-10-09T16:00:00-04:00"
    assert event.reaction_start_at.isoformat() == "2026-10-12T09:30:00-04:00"
    assert event.reaction_end_at.isoformat() == "2026-10-14T16:00:00-04:00"


def test_unspecified_earnings_timing_is_not_guessed():
    event = _earnings_event(
        {"symbol": "NVDA", "time": "time-not-supplied"},
        date(2026, 10, 9),
        universe={"NVDA"},
        session_days=[date(2026, 10, 12)],
        known_at=datetime(2026, 10, 8, 15, tzinfo=timezone.utc),
    )

    assert event is None


def test_algo_event_satellite_is_enabled_but_standalone_default_is_off():
    assert EventStrategyConfig().enabled is False
    assert ALGO_DEFAULT_PARAMETERS["event_driven"]["enabled"] is True


def test_published_macro_events_are_point_in_time_visible_and_spy_targeted():
    known_at = datetime(2026, 10, 8, 15, tzinfo=timezone.utc)

    events = _macro_events(known_at=known_at)

    assert len(events) == 15
    assert {event.ticker for event in events} == {"SPY"}
    assert all(event.known_at <= event.scheduled_at for event in events)
    assert all(event.reaction_start_at <= event.reaction_end_at for event in events)
    assert sum(event.event_type == "fomc" and event.scheduled_at.year == 2027 for event in events) == 8
