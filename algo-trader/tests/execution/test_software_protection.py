"""Software-managed protection: payload shape, session exit rules, duplicate and retry guards."""

from __future__ import annotations

import logging
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from execution import batch
from execution.batch import BatchHooks, execute_trade_batch
from execution.orders import OrderHooks, place_order
from market import calendar as market_calendar
from portfolio.protection.software import exit_limit_price, exit_order_policy


ET = ZoneInfo("America/New_York")
FORBIDDEN_KEYS = {"order_class", "stop_loss", "take_profit", "trail_percent", "stop_price", "parent_order_id"}


def _order_hooks(sent: list[tuple[dict, str]]) -> OrderHooks:
    def submit(order, order_type):
        sent.append((dict(order), order_type))
        return {"success": True, "order_id": "order-1", "broker_status": "new"}

    return OrderHooks(
        mode="algo",
        headers=lambda mode=None: {},
        verify_algo_execution_lease=lambda: None,
        lookup_client_order=lambda headers, client_order_id: None,
        get_nested_order=lambda headers, order_id: pytest.fail("no nested broker lookup is needed"),
        get_mode_config=lambda mode=None: {"risk": {"allow_short_positions": True}},
        get_market_schedule=lambda on_date=None, mode="algo": None,
        list_positions=lambda headers: [],
        position_qty=lambda position: 0,
        list_open_orders=lambda headers: [],
        submit_broker_order=submit,
    )


@pytest.mark.parametrize(
    ("action", "kwargs"),
    [
        ("buy", {"order_type": "limit", "limit_price": 100, "stop_price": 95, "take_profit": 110}),
        ("short", {"order_type": "limit", "limit_price": 100, "stop_price": 105, "take_profit": 90}),
        ("sell", {"order_type": "limit", "limit_price": 100}),
        ("cover", {"order_type": "limit", "limit_price": 101}),
    ],
)
def test_payload_builder_never_emits_bracket_oco_or_protective_legs(action, kwargs):
    sent: list[tuple[dict, str]] = []
    result = place_order("AAPL", action, 10, _order_hooks(sent), **kwargs)

    assert result["success"] is True
    assert len(sent) == 1
    payload, _ = sent[0]
    assert payload["type"] == "limit"
    assert not (FORBIDDEN_KEYS & set(payload))


def test_market_orders_are_refused_before_any_broker_post():
    sent: list[tuple[dict, str]] = []
    result = place_order("AAPL", "sell", 10, _order_hooks(sent), order_type="market")
    assert result["success"] is False
    assert "Market orders are disabled" in result["error"]
    assert sent == []


@pytest.mark.parametrize("order_type", ["bracket", "oco", "stop", "trailing_stop"])
def test_protective_order_types_are_rejected_before_any_broker_post(order_type):
    sent: list[tuple[dict, str]] = []
    result = place_order("AAPL", "sell", 10, _order_hooks(sent), order_type=order_type, stop_price=95, take_profit=110)

    assert result["success"] is False
    assert "software-managed" in result["error"] or "Unsupported order type" in result["error"]
    assert sent == []


def test_entry_with_only_a_stop_is_rejected_rather_than_sent_unprotected():
    sent: list[tuple[dict, str]] = []
    result = place_order("AAPL", "buy", 10, _order_hooks(sent), stop_price=95)

    assert result["success"] is False
    assert sent == []


def test_session_policy_uses_market_in_regular_and_limit_day_in_extended_sessions():
    assert exit_order_policy("regular") == {"order_type": "market", "time_in_force": "day", "extended_hours": False}
    for session in ("premarket", "after_hours", "overnight"):
        policy = exit_order_policy(session)
        assert policy["order_type"] == "limit"
        assert policy["extended_hours"] is True
        assert policy["time_in_force"] == "day"


def test_extended_exit_limits_cross_the_book_by_the_configured_band():
    assert exit_limit_price("sell", 100.0, 100.1, "premarket") == 99.5
    assert exit_limit_price("cover", 100.0, 100.0, "after_hours") == 100.5
    assert exit_limit_price("sell", 100.0, 100.1, "overnight") == 99.0
    assert exit_limit_price("cover", 100.0, 100.0, "overnight") == 101.0


@pytest.mark.parametrize("bad_quote", [None, 0, -1, float("nan")])
def test_extended_exit_refuses_an_unusable_quote(bad_quote):
    with pytest.raises(ValueError):
        exit_limit_price("sell", bad_quote, 100.0, "premarket")


class _PremarketClock(datetime):
    @classmethod
    def now(cls, tz=None):
        frozen = datetime(2026, 10, 8, 5, 0, tzinfo=ET)
        return frozen if tz is None else frozen.astimezone(tz)


def _premarket_schedule(on_date=None, mode="algo"):
    if on_date is None:
        return SimpleNamespace(market_open=lambda now: False)
    if on_date != date(2026, 10, 8):
        return None
    return market_calendar.MarketSchedule(
        date(2026, 10, 8),
        datetime(2026, 10, 8, 9, 30, tzinfo=ET),
        datetime(2026, 10, 8, 16, 0, tzinfo=ET),
    )


def _regular_schedule(on_date=None, mode="algo"):
    return SimpleNamespace(market_open=lambda now: True) if on_date is None else None


def _batch_hooks(*, schedule, open_exits=None, place_results=None, quote=100.0):
    places: list[dict] = []
    results = list(place_results or [])

    def place(ticker, action, qty, **kwargs):
        places.append({"ticker": ticker, "action": action, "qty": qty, **kwargs})
        if results:
            return results.pop(0)
        return {"success": True, "status": "submitted", "order_id": "exit-1"}

    hooks = BatchHooks(
        risk_validator_available=True,
        rm_validate_trade=lambda **kwargs: SimpleNamespace(approved=True, reason="", rule=""),
        rm_stage_approved_trade=lambda *args, **kwargs: None,
        get_latest_ask=lambda *args, **kwargs: quote,
        get_latest_bid=lambda *args, **kwargs: quote,
        get_latest_price=lambda *args, **kwargs: quote,
        validate_position_reduction=lambda *args, **kwargs: (True, ""),
        open_exit_orders_for_symbol=lambda ticker, *, mode, action: list(open_exits or []),
        get_positions=lambda *args, **kwargs: {"AAPL": {"qty": 2}},
        place_order=place,
        market_tz=ET,
        log=logging.getLogger("test"),
        get_market_schedule=schedule,
    )
    return hooks, places


def _run_exit(hooks, trade):
    return execute_trade_batch(
        trades=[trade],
        decision_digest="test",
        args=SimpleNamespace(dry_run=False),
        mode="algo",
        positions={"AAPL": {"qty": 2}},
        rm_portfolio_state=None,
        hooks=hooks,
    )


def test_regular_session_exit_submits_once_and_is_marketable_market_order(monkeypatch):
    hooks, places = _batch_hooks(schedule=_regular_schedule)
    results, submitted, _ = _run_exit(hooks, {
        "ticker": "AAPL", "action": "sell", "qty": 2, "order_type": "market",
        "strategy": "swing_trend_following", "client_order_id": "rule-sell-aapl",
    })

    assert submitted == 1
    assert results[0]["status"] == "submitted"
    assert places[0]["order_type"] == "market"


def test_extended_session_exit_is_priced_as_a_marketable_limit(monkeypatch):
    monkeypatch.setattr(batch, "datetime", _PremarketClock)
    hooks, places = _batch_hooks(schedule=_premarket_schedule, quote=100.0)
    results, submitted, _ = _run_exit(hooks, {
        "ticker": "AAPL", "action": "sell", "qty": 2, "order_type": "market",
        "strategy": "swing_trend_following", "client_order_id": "ext-sell-aapl",
    })

    assert submitted == 1
    assert places[0]["order_type"] == "limit"
    assert places[0]["limit_price"] == 99.5
    assert places[0]["extended_hours"] is True


def test_extended_exit_with_unavailable_quote_is_retryable_and_sends_nothing(monkeypatch):
    monkeypatch.setattr(batch, "datetime", _PremarketClock)
    hooks, places = _batch_hooks(schedule=_premarket_schedule)

    def no_quote(*args, **kwargs):
        raise RuntimeError("quote feed down")

    hooks = hooks.__class__(**{**hooks.__dict__, "get_latest_bid": no_quote})
    results, submitted, _ = _run_exit(hooks, {
        "ticker": "AAPL", "action": "sell", "qty": 2, "strategy": "swing_trend_following",
        "client_order_id": "ext-sell-aapl",
    })

    assert submitted == 0
    assert places == []
    assert results[0]["status"] == "failed"
    assert results[0]["safe_to_retry"] is True


def test_open_exit_at_broker_blocks_a_duplicate_exit_in_regular_hours():
    hooks, places = _batch_hooks(schedule=_regular_schedule, open_exits=[{"id": "exit-0", "side": "sell"}])
    results, submitted, _ = _run_exit(hooks, {
        "ticker": "AAPL", "action": "sell", "qty": 2, "order_type": "market",
        "strategy": "swing_trend_following", "client_order_id": "rule-sell-aapl",
    })

    assert submitted == 0
    assert places == []
    assert results[0]["rule"] == "exit_in_progress"
    assert results[0]["safe_to_retry"] is True


def test_open_exit_at_broker_blocks_a_duplicate_exit_in_extended_hours(monkeypatch):
    monkeypatch.setattr(batch, "datetime", _PremarketClock)
    hooks, places = _batch_hooks(schedule=_premarket_schedule, open_exits=[{"id": "exit-0", "side": "sell"}])
    results, submitted, _ = _run_exit(hooks, {
        "ticker": "AAPL", "action": "sell", "qty": 2, "strategy": "swing_trend_following",
        "client_order_id": "ext-sell-aapl",
    })

    assert submitted == 0
    assert places == []
    assert results[0]["rule"] == "exit_in_progress"


def test_failed_exit_is_retried_on_the_next_scan_once_no_exit_is_open():
    hooks, places = _batch_hooks(
        schedule=_regular_schedule,
        place_results=[{"success": False, "status": "failed", "error": "HTTP 503"}],
    )
    trade = {
        "ticker": "AAPL", "action": "sell", "qty": 2, "order_type": "market",
        "strategy": "swing_trend_following", "client_order_id": "rule-sell-aapl",
    }

    first, first_submitted, _ = _run_exit(hooks, trade)
    second, second_submitted, _ = _run_exit(hooks, trade)

    assert first[0]["success"] is False
    assert first_submitted == 0
    assert second_submitted == 1
    assert len(places) == 2
