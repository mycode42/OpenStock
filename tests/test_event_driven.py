from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.algo.backtest import AlgoPortfolioBacktest
import src.algo.backtest as backtest_module
from src.strategies.event_driven import (
    EventCalendar, EventSignalGenerator, EventStrategyConfig, ScheduledEvent,
)
from src.strategies.event_driven.reactions import daily_event_reaction
from src.strategies.registry import validate_parameter_profile


ET = ZoneInfo("America/New_York")


def _event(day: date, ticker: str = "AAPL", event_type: str = "earnings") -> ScheduledEvent:
    scheduled = datetime.combine(day, time(8, 30), tzinfo=ET)
    reaction_start = datetime.combine(day, time(9, 30), tzinfo=ET)
    return ScheduledEvent(
        event_id=f"{event_type}-{ticker}-{day.isoformat()}", event_type=event_type,
        ticker=ticker, scheduled_at=scheduled, known_at=scheduled,
        reaction_start_at=reaction_start,
        reaction_end_at=reaction_start + timedelta(days=3), source="test fixture",
    )


def _daily_bars(day: date, *, gap: float = 0.03, move: float = 0.02) -> list[dict]:
    bars = []
    cursor = day - timedelta(days=35)
    close = 100.0
    while cursor < day:
        if cursor.weekday() < 5:
            bars.append({
                "date": cursor.isoformat(), "o": close, "h": close + 0.5,
                "l": close - 0.5, "c": close, "v": 1_000,
            })
        cursor += timedelta(days=1)
    opened = close * (1 + gap)
    final = opened * (1 + move)
    bars.append({
        "date": day.isoformat(), "o": opened, "h": max(opened, final) + 1,
        "l": min(opened, final) - 1, "c": final, "v": 2_500,
    })
    return bars


def _as_of(day: date) -> datetime:
    return datetime.combine(day + timedelta(days=1), time(10), tzinfo=ET)


def test_daily_earnings_continuation_emits_risk_sized_long():
    day = date(2025, 9, 4)
    event = _event(day)
    signal = daily_event_reaction(
        event, "AAPL", _daily_bars(day), as_of=_as_of(day),
        config=EventStrategyConfig(enabled=True),
    )

    assert signal is not None
    assert signal.action == "buy"
    assert signal.strategy_group == "swing_satellite"
    assert signal.exit_mode == "follow_through"
    assert signal.maximum_holding_sessions == 2
    assert signal.target_weight == pytest.approx(0.0125)
    assert signal.event_id == event.event_id


def test_event_outcome_can_emit_shorts_and_reaction_fades_in_any_regime():
    day = date(2025, 9, 4)
    event = _event(day)
    generator = EventSignalGenerator(
        EventCalendar([event]), EventStrategyConfig(enabled=True),
    )
    short = generator.generate(
        "AAPL", _daily_bars(day, gap=0.03, move=-0.025), as_of=_as_of(day),
        universe={"AAPL"}, regime="trending_up", strategy_state={},
    )
    long = daily_event_reaction(
        event, "AAPL", _daily_bars(day, gap=-0.03, move=0.025),
        as_of=_as_of(day), config=EventStrategyConfig(enabled=True),
    )

    assert short is not None and short.action == "short"
    assert short.exit_mode == "reaction"
    assert short.maximum_holding_sessions == 1
    assert short.stop_pct == pytest.approx(0.025)
    assert long is not None and long.action == "buy"
    assert long.exit_mode == "reaction"


def test_event_signal_uses_only_completed_unique_daily_rows():
    day = date(2025, 9, 4)
    event = _event(day)
    bars = _daily_bars(day)
    same_session = datetime.combine(day, time(16), tzinfo=ET)
    assert daily_event_reaction(
        event, "AAPL", bars, as_of=same_session,
        config=EventStrategyConfig(enabled=True),
    ) is None
    assert daily_event_reaction(
        event, "AAPL", [*bars, bars[-1]], as_of=_as_of(day),
        config=EventStrategyConfig(enabled=True),
    ) is None


def test_event_calendar_records_cooldown_after_a_proposal():
    day = date(2025, 9, 4)
    event = _event(day)
    generator = EventSignalGenerator(
        EventCalendar([event]), EventStrategyConfig(enabled=True),
    )
    state = {}
    signal = generator.generate(
        "AAPL", _daily_bars(day), as_of=_as_of(day), universe={"AAPL"},
        regime="trending_up", strategy_state=state,
    )
    assert signal is not None
    generator.record_proposal(signal, as_of=_as_of(day), strategy_state=state)
    assert generator.generate(
        "AAPL", _daily_bars(day), as_of=_as_of(day), universe={"AAPL"},
        regime="trending_up", strategy_state=state,
    ) is None


def test_event_config_and_profile_keep_frequency_and_holding_bounded():
    settings = validate_parameter_profile({"event_driven": {"enabled": True}})["event_driven"]
    assert settings["follow_through_sessions"] == 2
    assert settings["max_positions"] == 2
    with pytest.raises(ValueError, match="max_entries_per_week"):
        EventStrategyConfig.from_dict({"max_entries_per_week": 8})


@pytest.mark.parametrize(
    ("gap", "move", "action"),
    [(0.03, 0.02, "buy"), (0.03, -0.025, "short")],
)
def test_daily_event_proposal_reaches_the_backtest_risk_manager(monkeypatch, gap, move, action):
    event_day = date(2025, 9, 4)
    replay_day = date(2025, 9, 5)
    event = _event(event_day)
    bars = []
    cursor = event_day - timedelta(days=35)
    close = 100.0
    while cursor < event_day:
        if cursor.weekday() < 5:
            bars.append({
                "t": datetime.combine(cursor, time(15, 55), tzinfo=ET).isoformat(),
                "o": close, "h": close + 0.5, "l": close - 0.5,
                "c": close, "v": 1_000,
            })
        cursor += timedelta(days=1)
    event_daily = _daily_bars(event_day, gap=gap, move=move)[-1]
    bars.append({
        "t": datetime.combine(event_day, time(15, 55), tzinfo=ET).isoformat(),
        **{key: event_daily[key] for key in ("o", "h", "l", "c", "v")},
    })
    # Swing satellites are ineligible during the first 15 minutes; the event
    # setup must still reach the coordinator and risk manager after that gate.
    for minute, price in ((45, 103.5), (50, 104.0)):
        bars.append({
            "t": datetime.combine(replay_day, time(9, minute), tzinfo=ET).isoformat(),
            "o": price, "h": price + 0.2, "l": price - 0.2, "c": price, "v": 100,
        })
    spy_daily = [
        {"t": f"2025-08-{index + 1:02d}T20:00:00Z", "c": 100 + index * 0.1}
        for index in range(25)
    ]
    monkeypatch.setattr(
        backtest_module, "rank_strategies",
        lambda *_args, **_kwargs: {
            "regime": "range_bound", "active": ["event_driven"],
            "ranked": [{"strategy": "event_driven", "score": 0.5}],
        },
    )
    risk_calls = []
    validate = backtest_module.validate_trade
    monkeypatch.setattr(
        backtest_module, "validate_trade",
        lambda **kwargs: risk_calls.append(kwargs) or validate(**kwargs),
    )

    replay = AlgoPortfolioBacktest(
        symbols=["AAPL"], intraday_bars={"AAPL": bars}, spy_daily_bars=spy_daily,
        start=replay_day.isoformat(), end=replay_day.isoformat(), feed="sip",
        data_adjustment="raw", profile={"event_driven": {"enabled": True}},
        strategies=["event_driven"], event_calendar=EventCalendar([event]),
    )
    report = replay.run()

    assert report["entry_count_by_strategy"]["event_driven"] == 1
    assert report["open_positions"][0]["strategy"] == "event_driven"
    assert risk_calls and risk_calls[0]["action"] == action
    assert risk_calls[0]["mode"] == "algo"
