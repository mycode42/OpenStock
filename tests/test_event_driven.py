from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.algo.backtest import AlgoPortfolioBacktest
from src.algo.state_store import empty_portfolio, validate_portfolio
from src.strategies.event_driven import EventCalendar, EventSignalGenerator, EventStrategyConfig, ScheduledEvent
from src.strategies.registry import validate_parameter_profile


ET = ZoneInfo("America/New_York")


def _at(day: date, hour: int, minute: int) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=ET)


def _event(*, ticker="AAPL", event_type="earnings", day=date(2025, 9, 5), known_hour=8, known_minute=30):
    scheduled = _at(day, known_hour, known_minute)
    start = _at(day, 9, 30) if known_hour < 9 else scheduled
    return ScheduledEvent(
        event_id=f"{event_type}-{ticker}-{day.isoformat()}", event_type=event_type,
        ticker=ticker, scheduled_at=scheduled, known_at=scheduled,
        reaction_start_at=start, reaction_end_at=start + timedelta(minutes=90),
        source="test-fixture",
    )


def _reaction_bars(day=date(2025, 9, 5)):
    records = []
    for minute, opening, high, low, close, volume in (
        (30, 100.0, 101.0, 99.0, 100.5, 100),
        (35, 100.6, 102.0, 100.0, 101.5, 100),
        (40, 101.5, 103.0, 101.0, 102.5, 100),
        (45, 102.5, 104.0, 102.0, 103.5, 200),
        (50, 103.5, 104.5, 103.0, 104.2, 120),
    ):
        timestamp = _at(day, 9, minute).astimezone(timezone.utc).isoformat()
        records.append({
            "t": timestamp, "o": opening, "h": high, "l": low,
            "c": close, "v": volume, "vw": close - 0.1,
        })
    return records


def test_calendar_filters_on_known_at_and_reaction_window():
    event = _event()
    calendar = EventCalendar([event])

    assert not calendar.active_for("AAPL", _at(date(2025, 9, 5), 8, 0))
    assert calendar.active_for("AAPL", _at(date(2025, 9, 5), 9, 50)) == (event,)
    assert not calendar.active_for("MSFT", _at(date(2025, 9, 5), 9, 50))


def test_calendar_freshness_allows_long_weekends_and_expires_stale_snapshots():
    event = _event(day=date(2025, 9, 5), known_hour=16)
    calendar = EventCalendar([event])

    weekend_check = _at(date(2025, 9, 8), 8, 0)
    stale_check = _at(date(2025, 9, 10), 10, 0)

    assert calendar.freshness(weekend_check)["fresh"] is True
    assert calendar.freshness(stale_check)["fresh"] is False


def test_historical_example_calendar_loads_and_enforces_macro_proxy():
    path = Path(__file__).resolve().parents[1] / "examples" / "event_calendar_2025.csv"
    calendar = EventCalendar.from_csv(path)

    assert len(calendar.events) == 4
    assert sum(event.is_earnings for event in calendar.events) == 1
    with pytest.raises(ValueError, match="macro event rows must target SPY"):
        _event(event_type="nfp", ticker="AAPL")


def test_earnings_reaction_requires_gap_range_break_volume_and_is_size_capped():
    event = _event()
    generator = EventSignalGenerator(
        EventCalendar([event]), EventStrategyConfig.from_dict({"enabled": True}),
    )
    bars = _reaction_bars()
    as_of = _at(date(2025, 9, 5), 9, 50)

    signal = generator.generate(
        "AAPL", bars[:4], as_of=as_of, universe={"AAPL"}, regime="trending_up",
        previous_close=97.0, strategy_state={},
    )

    assert signal is not None
    assert signal.action == "buy"
    assert signal.strategy == "event_driven"
    assert signal.event_id == event.event_id
    assert signal.stop_pct == 0.02
    assert signal.target_weight == 0.025
    assert generator.generate(
        "AAPL", bars[:4], as_of=as_of, universe={"AAPL"}, regime="volatile",
        previous_close=97.0, strategy_state={},
    ) is None


def test_event_proposal_cooldown_applies_across_tickers():
    first_event = _event(ticker="AAPL")
    second_event = _event(ticker="MSFT")
    generator = EventSignalGenerator(
        EventCalendar([first_event, second_event]),
        EventStrategyConfig.from_dict({"enabled": True}),
    )
    now = _at(date(2025, 9, 5), 9, 50)
    state = {}
    first = generator.generate(
        "AAPL", _reaction_bars()[:4], as_of=now, universe={"AAPL", "MSFT"},
        regime="trending_up", previous_close=97.0, strategy_state=state,
    )
    assert first is not None
    generator.record_proposal(first, as_of=now, strategy_state=state)

    second = generator.generate(
        "MSFT", _reaction_bars()[:4], as_of=now, universe={"AAPL", "MSFT"},
        regime="trending_up", previous_close=97.0, strategy_state=state,
    )

    assert second is None
    assert generator.diagnostics()["filter_rejected"]["portfolio_cooldown"] == 1

    portfolio = empty_portfolio()
    portfolio["strategy_states"] = {"event_driven": state}
    validate_portfolio(portfolio)


def test_event_profile_validates_limits_and_flags():
    profile = validate_parameter_profile({"event_driven": {"enabled": True}})
    assert profile["event_driven"]["max_entries_per_week"] == 3
    with pytest.raises(ValueError, match="max_entries_per_week"):
        validate_parameter_profile({"event_driven": {"max_entries_per_week": 8}})


def test_event_calendar_replay_uses_risk_manager_and_only_next_bar_fill():
    day = date(2025, 9, 5)
    event = _event(day=day)
    prior_day_bar = {
        "t": _at(day - timedelta(days=1), 15, 55).astimezone(timezone.utc).isoformat(),
        "o": 96.8, "h": 97.2, "l": 96.5, "c": 97.0, "v": 100,
    }
    bars = [prior_day_bar, *_reaction_bars(day)]
    daily_days = []
    cursor = day - timedelta(days=1)
    while len(daily_days) < 25:
        if cursor.weekday() < 5:
            daily_days.append(cursor)
        cursor -= timedelta(days=1)
    daily_days.reverse()
    spy_daily = [
        {"t": f"{session.isoformat()}T20:00:00Z", "c": 100 + index * 0.2}
        for index, session in enumerate(daily_days)
    ]
    profile = {"event_driven": {"enabled": True}}
    replay = AlgoPortfolioBacktest(
        symbols=["AAPL"], intraday_bars={"AAPL": bars}, spy_daily_bars=spy_daily,
        start=day.isoformat(), end=day.isoformat(), feed="sip", data_adjustment="raw",
        profile=profile, strategies=["event_driven"], event_calendar=EventCalendar([event]),
    )

    report = replay.run()

    assert report["entry_count"] == 1
    assert report["entry_count_by_strategy"]["event_driven"] == 1
    trade = report["open_positions"][0]
    assert trade["ticker"] == "AAPL"
    assert trade["strategy"] == "event_driven"
    assert trade["entry_at"] == _at(day, 9, 50).astimezone(timezone.utc).isoformat()
    assert report["event_strategy_diagnostics"]["signals"] == 1
