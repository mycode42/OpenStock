from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from equity_research.backtest import AlgoPortfolioBacktest
import equity_research.backtest as backtest_module
from strategies.llm_driven import (
    EventCalendar, LLMSignalGenerator, LLMStrategyConfig, ScheduledEvent,
)
from strategies.signals import Signal
from strategies.registry import validate_parameter_profile


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


def test_event_config_keeps_holding_bounded_without_weekly_or_portfolio_throttles():
    settings = validate_parameter_profile({"swing_llm_driven": {"enabled": True}})["swing_llm_driven"]
    assert settings["follow_through_sessions"] == 2
    assert "max_positions" not in settings
    for removed_setting in ("max_entries_per_week", "cooldown_hours_portfolio", "max_positions"):
        assert removed_setting not in settings
        with pytest.raises(ValueError, match=removed_setting):
            LLMStrategyConfig.from_dict({removed_setting: 1})


@pytest.mark.parametrize(
    ("gap", "move", "action"),
    [(0.03, 0.02, "buy"), (0.03, -0.025, "short")],
)
def test_inactive_llm_strategy_does_not_create_a_backtest_order(monkeypatch, gap, move, action):
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
    # The event candidate is absent from the regime's active strategy list.
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
            "regime": "range_bound", "active": [],
            "ranked": [{"strategy": "swing_llm_driven", "score": 0.5}],
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
        data_adjustment="raw", profile={"swing_llm_driven": {"enabled": True}},
        strategies=["swing_llm_driven"], event_calendar=EventCalendar([event]),
    )
    report = replay.run()

    assert report["entry_count_by_strategy"].get("swing_llm_driven", 0) == 0
    assert report["open_positions"] == []
    assert risk_calls == []




def test_generator_without_ai_snapshot_opens_no_calendar_reaction_entries():
    # Owned LLM positions build a generator without an AI snapshot; it must
    # protect holdings only and never open entries from calendar windows.
    event = ScheduledEvent(
        event_id="earn-AAPL-test", event_type="earnings", ticker="AAPL",
        scheduled_at=datetime(2026, 10, 20, 8, 0, tzinfo=ET),
        known_at=datetime(2026, 10, 19, 8, 0, tzinfo=ET),
        reaction_start_at=datetime(2026, 10, 20, 9, 30, tzinfo=ET),
        reaction_end_at=datetime(2026, 10, 23, 16, 0, tzinfo=ET),
        source="test fixture",
    )
    generator = LLMSignalGenerator(EventCalendar([event]), LLMStrategyConfig())
    as_of = datetime(2026, 10, 21, 10, 0, tzinfo=ET)
    assert generator.generate(
        "AAPL", [], as_of=as_of, universe={"AAPL"}, regime="trending_up",
        strategy_state={}, position_qty=0,
    ) is None
