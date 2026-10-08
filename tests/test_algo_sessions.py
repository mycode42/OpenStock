from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.algo.sessions import get_trading_window, next_session_start
from src.strategies.universe import _overnight_tradable


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


def test_extended_hours_executor_sends_plain_limit_day_order(monkeypatch):
    import execute_trades

    submitted = {}

    class Response:
        status_code = 201

        @staticmethod
        def json():
            return {
                "id": "order-1", "status": "accepted", "symbol": "AAPL",
                "side": "buy", "qty": "3", "type": "limit",
                "order_class": "simple", "limit_price": "225.1",
            }

    def fake_post(url, *, headers, json, timeout):
        submitted.update(json)
        return Response()

    monkeypatch.setattr(execute_trades, "_active_mode", "algo")
    monkeypatch.setattr(execute_trades, "_verify_algo_execution_lease", lambda: None)
    monkeypatch.setattr(execute_trades, "_headers", lambda *_: {})
    monkeypatch.setattr(execute_trades.requests, "post", fake_post)

    result = execute_trades.place_order(
        "AAPL", "buy", 3, order_type="limit", limit_price=225.10,
        stop_price=220, take_profit=235, extended_hours=True,
    )

    assert result["success"]
    assert submitted == {
        "symbol": "AAPL", "qty": "3", "side": "buy", "type": "limit",
        "time_in_force": "day", "limit_price": "225.1", "extended_hours": True,
    }
