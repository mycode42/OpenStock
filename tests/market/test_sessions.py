from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from execution import runner as execution_runner
from market.calendar import MarketSchedule, extended_session_name
from market.sessions import get_trading_window, next_session_start
from strategies.universe import _overnight_tradable


ET = ZoneInfo("America/New_York")


def calendar(*days):
    return [{"date": day, "open": "09:30", "close": "16:00"} for day in days]


@pytest.mark.parametrize(
    ("at", "expected", "trade_date", "bar_feed", "quote_feed"),
    [
        ("2026-10-07T03:00:00", "overnight", "2026-10-07", "boats", "boats"),
        ("2026-10-07T05:00:00", "premarket", "2026-10-07", "sip", "sip"),
        ("2026-10-07T12:00:00", "regular", "2026-10-07", "sip", "sip"),
        ("2026-10-07T17:00:00", "after_hours", "2026-10-07", "sip", "sip"),
        ("2026-10-06T21:00:00", "overnight", "2026-10-07", "boats", "boats"),
    ],
)
def test_session_windows_cover_the_alpaca_weekday_sessions(monkeypatch, at, expected, trade_date, bar_feed, quote_feed):
    monkeypatch.setenv("ALPACA_DATA_TIER", "algo_trader_plus")
    now = datetime.fromisoformat(at).replace(tzinfo=ET)
    rows = calendar("2026-10-07", "2026-10-08")

    result = get_trading_window(now, rows)

    assert result is not None
    assert (result.name, result.trade_date, result.bar_feed, result.quote_feed) == (
        expected, trade_date, bar_feed, quote_feed,
    )
    assert result.extended_hours is (expected != "regular")


def test_sunday_evening_opens_monday_overnight_but_friday_evening_is_closed():
    sunday_open = datetime(2026, 10, 11, 20, 30, tzinfo=ET)
    friday_close = datetime(2026, 10, 9, 20, 30, tzinfo=ET)
    rows = calendar("2026-10-12")

    assert get_trading_window(sunday_open, rows).trade_date == "2026-10-12"
    assert get_trading_window(friday_close, rows) is None


def test_market_holiday_does_not_start_the_prior_overnight_window():
    thursday_evening = datetime(2026, 11, 25, 20, 30, tzinfo=ET)

    assert get_trading_window(thursday_evening, calendar("2026-11-27")) is None


def test_closed_service_wakes_for_next_overnight_session():
    saturday = datetime(2026, 10, 10, 12, 0, tzinfo=ET)

    assert next_session_start(saturday, calendar("2026-10-12")) == datetime(2026, 10, 11, 20, 0, tzinfo=ET)


def test_overnight_assets_must_be_enabled_and_not_halted():
    assert _overnight_tradable({"overnight_tradable": True})
    assert _overnight_tradable({"attributes": ["overnight_tradable"]})
    assert not _overnight_tradable({"overnight_tradable": True, "overnight_halted": True})
    assert not _overnight_tradable({"tradable": True})


def _regular_schedules(*trading_dates):
    """Regular 09:30-16:00 ET sessions on the given ISO dates; other dates are holidays."""
    def schedule_for(on_date=None, mode="algo"):
        on_date = on_date or date(2026, 10, 8)
        if on_date.isoformat() not in trading_dates:
            return None
        return MarketSchedule(
            on_date,
            datetime.combine(on_date, time(9, 30), ET),
            datetime.combine(on_date, time(16, 0), ET),
        )
    return schedule_for


def test_extended_session_name_follows_et_windows_and_holidays():
    # 2026-10-09 (Fri) and 2026-10-10 (Sat) have no regular session.
    schedule_for = _regular_schedules("2026-10-07", "2026-10-08", "2026-10-12")
    at = lambda *parts: datetime(*parts, tzinfo=ET)

    assert extended_session_name(at(2026, 10, 7, 5, 0), schedule_for) == "premarket"
    assert extended_session_name(at(2026, 10, 7, 10, 0), schedule_for) is None
    assert extended_session_name(at(2026, 10, 7, 17, 0), schedule_for) == "after_hours"
    assert extended_session_name(at(2026, 10, 7, 21, 0), schedule_for) == "overnight"
    assert extended_session_name(at(2026, 10, 8, 2, 0), schedule_for) == "overnight"
    assert extended_session_name(at(2026, 10, 8, 4, 30), schedule_for) == "premarket"
    # Friday 20:00 would open Saturday, which is closed.
    assert extended_session_name(at(2026, 10, 9, 21, 0), schedule_for) is None
    assert extended_session_name(at(2026, 10, 10, 2, 0), schedule_for) is None
    # Sunday 20:00 opens Monday's overnight window.
    assert extended_session_name(at(2026, 10, 11, 21, 0), schedule_for) == "overnight"


class _FrozenPremarketClock(datetime):
    @classmethod
    def now(cls, tz=None):
        frozen = datetime(2026, 10, 8, 5, 0, tzinfo=ET)
        return frozen if tz is None else frozen.astimezone(tz)


def test_extended_hours_limit_reaches_broker_without_broker_protection(monkeypatch):
    sent = []

    class _Captured(Exception):
        """Stops the submission after the payload is captured; nothing is posted."""

    def capture(url, headers=None, json=None, timeout=None):
        sent.append(json)
        raise _Captured()

    monkeypatch.setattr(execution_runner, "_active_mode", "algo")
    monkeypatch.setattr(execution_runner, "_verify_algo_execution_lease", lambda: None)
    monkeypatch.setattr(execution_runner, "_headers", lambda *_: {})
    monkeypatch.setattr(execution_runner, "get_market_schedule", _regular_schedules("2026-10-08"))
    monkeypatch.setattr(execution_runner.orders, "datetime", _FrozenPremarketClock)
    monkeypatch.setattr(execution_runner.requests, "post", capture)

    with pytest.raises(_Captured):
        execution_runner.place_order(
            "AAPL", "buy", 3, order_type="limit", limit_price=225.10,
            stop_price=220, take_profit=235, extended_hours=True,
        )

    payload = sent[0]
    assert payload["type"] == "limit"
    assert payload["time_in_force"] == "day"
    assert payload["extended_hours"] is True
    assert {"order_class", "stop_loss", "take_profit"}.isdisjoint(payload)


def test_extended_hours_bracket_and_market_orders_never_reach_broker(monkeypatch):
    monkeypatch.setattr(execution_runner, "_active_mode", "algo")
    monkeypatch.setattr(
        execution_runner.requests, "post",
        lambda *args, **kwargs: pytest.fail("unsupported extended-hours order reached the broker"),
    )

    bracket = execution_runner.place_order(
        "AAPL", "buy", 3, order_type="bracket", limit_price=225.10,
        stop_price=220, take_profit=235, extended_hours=True,
    )
    market = execution_runner.place_order(
        "AAPL", "sell", 3, order_type="market", extended_hours=True,
    )

    assert not bracket["success"]
    assert "Extended-hours orders require" in bracket["error"]
    assert not market["success"]
    assert "Extended-hours orders require" in market["error"]
