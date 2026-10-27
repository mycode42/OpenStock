"""Risk and liquidation gates that must fail visibly on uncertain broker state."""

import argparse
import json
import logging
from contextlib import nullcontext
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import requests

from execution import runner as execution_runner
from execution.batch import BatchHooks, execute_trade_batch
from execution import risk as risk_validator
from market import calendar as market_calendar

ET = ZoneInfo("America/New_York")

class Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture(autouse=True)
def _default_regular_market_schedule(monkeypatch):
    """Keep unrelated executor tests deterministic inside regular hours."""
    monkeypatch.setattr(
        execution_runner, "get_market_schedule",
        lambda **kwargs: SimpleNamespace(market_open=lambda now: True),
    )

def _state(**overrides):
    state = {
        "equity": 100_000.0,
        "cash": 25_000.0,
        "cash_pct": 0.25,
        "daily_pnl_pct": 0.0,
        "weekly_pnl_pct": 0.0,
        "positions": {},
        "sector_alloc": {},
        "trade_count_today": 0,
        "entry_count_today": 0,
        "entry_count_available": True,
        "open_position_count": 0,
    }
    state.update(overrides)
    return state

def test_market_schedule_uses_regular_session_hours(monkeypatch):
    monkeypatch.setattr(market_calendar, "get_account_for_mode", lambda mode: SimpleNamespace(headers={}))
    monkeypatch.setattr(
        market_calendar, "get_session",
        lambda: SimpleNamespace(get=lambda *args, **kwargs: Response([{"date": "2026-11-27", "open": "09:30", "close": "13:00"}])),
    )
    monkeypatch.setattr(market_calendar, "_SCHEDULE_CACHE", {})

    schedule = market_calendar.get_market_schedule(on_date=date(2026, 11, 27))

    assert schedule.opens_at == datetime(2026, 11, 27, 9, 30, tzinfo=ET)
    assert schedule.closes_at == datetime(2026, 11, 27, 13, 0, tzinfo=ET)
    assert schedule.market_open(datetime(2026, 11, 27, 12, 59, tzinfo=ET))
    assert not schedule.market_open(datetime(2026, 11, 27, 13, 0, tzinfo=ET))

def test_market_schedule_caches_successes_but_retries_failures(monkeypatch):
    monkeypatch.setattr(market_calendar, "get_account_for_mode", lambda mode: SimpleNamespace(headers={}))
    monkeypatch.setattr(market_calendar, "_SCHEDULE_CACHE", {})
    responses = [
        requests.ConnectionError("calendar unreachable"),
        Response([{"date": "2026-11-27", "open": "09:30", "close": "16:00"}]),
    ]
    calls = []

    def fake_get(*args, **kwargs):
        calls.append(kwargs)
        outcome = responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(market_calendar, "get_session", lambda: SimpleNamespace(get=fake_get))

    with pytest.raises(requests.ConnectionError):
        market_calendar.get_market_schedule(on_date=date(2026, 11, 27))
    assert market_calendar.get_market_schedule(on_date=date(2026, 11, 27)) is not None
    assert market_calendar.get_market_schedule(on_date=date(2026, 11, 27)) is not None
    assert len(calls) == 2

def test_algo_weekly_baseline_uses_prior_close_and_rejects_stale_history(tmp_path, monkeypatch):
    monkeypatch.setattr(risk_validator, "_snapshot_dir", lambda mode: tmp_path)
    monkeypatch.setattr(risk_validator, "_risk_baseline_dir", lambda mode: tmp_path / "inception")
    today = datetime.now(ET).date()
    week_start = today - timedelta(days=today.weekday())
    previous_friday = week_start - timedelta(days=3)
    current_week_close = week_start
    (tmp_path / f"{current_week_close}.json").write_text(json.dumps({
        "date": current_week_close.isoformat(), "equity": 100_000,
        "trading_mode": "algo", "account_id": "paper-algo", "snapshot_kind": "close",
    }))
    assert risk_validator._estimate_weekly_pnl(
        95_000, mode="algo", account_id="paper-algo", session_day=today,
    ) is None
    close_file = tmp_path / f"{previous_friday}.json"
    close_file.write_text(json.dumps({
        "date": previous_friday.isoformat(), "equity": 105_000,
        "trading_mode": "algo", "account_id": "paper-algo", "snapshot_kind": "close",
    }))
    assert risk_validator._estimate_weekly_pnl(
        95_000, mode="algo", account_id="paper-algo", session_day=today,
    ) == pytest.approx(95_000 / 105_000 - 1)
    assert risk_validator._estimate_weekly_pnl(
        95_000, mode="algo", account_id="another-account", session_day=today,
    ) is None
    close_file.unlink()
    assert risk_validator._estimate_weekly_pnl(
        95_000, mode="algo", account_id="paper-algo", session_day=today,
    ) is None
    rejected = risk_validator.validate_trade("AAPL", "buy", 1, 100, _state(weekly_pnl_pct=None), mode="algo")
    assert rejected.rule == "weekly_baseline_unavailable"

def test_new_account_inception_baseline_is_account_bound_and_cannot_move(tmp_path, monkeypatch):
    now = datetime(2026, 10, 8, 10, 0, tzinfo=ET)
    broker = {
        "id": "new-algo-id", "created_at": "2026-10-08T08:15:00Z",
        "equity": "1000000", "cash": "1000000",
    }
    monkeypatch.setattr(risk_validator, "_risk_baseline_dir", lambda mode: tmp_path / "baselines")
    monkeypatch.setattr(risk_validator, "_snapshot_dir", lambda mode: tmp_path / "snapshots")
    monkeypatch.setattr(
        risk_validator, "verify_account_for_mode",
        lambda mode: (SimpleNamespace(account_id="new-algo"), broker),
    )
    monkeypatch.setattr(risk_validator, "_api", lambda endpoint, mode: [])

    first = risk_validator.capture_account_inception_baseline("algo", now=now)
    assert first["equity"] == 1_000_000
    assert risk_validator._estimate_weekly_pnl(
        990_000, mode="algo", account_id="new-algo-id", session_day=now.date(),
    ) == pytest.approx(-0.01)
    assert risk_validator._estimate_weekly_pnl(
        990_000, mode="algo", account_id="old-algo-id", session_day=now.date(),
    ) is None

    broker["equity"] = broker["cash"] = "990000"
    with pytest.raises(ValueError, match="differs"):
        risk_validator.capture_account_inception_baseline("algo", now=now + timedelta(minutes=1))
    broker["equity"] = broker["cash"] = "1000000"
    monkeypatch.setattr(risk_validator, "_api", lambda endpoint, mode: [{}] if endpoint == "positions" else [])
    with pytest.raises(ValueError, match="zero positions"):
        risk_validator.capture_account_inception_baseline("algo", now=now)

def test_pending_buy_reserves_cash_and_sector_before_new_entry(monkeypatch):
    monkeypatch.setattr(risk_validator, "verify_account_for_mode", lambda mode: (
        SimpleNamespace(account_id="paper-swing"),
        {"id": "paper-swing", "equity": "100000", "cash": "25000", "last_equity": "100000"},
    ))
    def fake_api(endpoint, mode):
        if endpoint == "positions":
            return []
        raise AssertionError(endpoint)

    monkeypatch.setattr(risk_validator, "_api", fake_api)
    monkeypatch.setattr(risk_validator, "_get_algo_orders_since", lambda *args, **kwargs: [])
    monkeypatch.setattr(risk_validator, "_get_algo_fill_activities", lambda *args, **kwargs: [])
    monkeypatch.setattr(risk_validator, "_easy_to_borrow_symbols", lambda mode: set())
    monkeypatch.setattr(risk_validator, "_get_open_orders", lambda mode: [{
        "id": "pending", "symbol": "AAPL", "side": "buy", "qty": "50",
        "filled_qty": "0", "limit_price": "100",
    }])
    monkeypatch.setattr(risk_validator, "_estimate_weekly_pnl", lambda *args, **kwargs: 0.0)

    state = risk_validator.get_portfolio_state(mode="algo")

    assert state["pending_buy_reservation"] == pytest.approx(5_100)
    assert state["available_cash"] == pytest.approx(19_900)
    assert state["sector_alloc"]["core_tech"] == pytest.approx(5_100)
    assert state["open_position_count"] == 1
    blocked = risk_validator.validate_trade("MSFT", "buy", 100, 100, state, mode="algo")
    assert blocked.rule == "min_cash_reserve"

def test_failed_order_history_blocks_algo_entries(monkeypatch):
    monkeypatch.setattr(risk_validator, "verify_account_for_mode", lambda mode: (
        SimpleNamespace(account_id=f"paper-{mode}"),
        {"id": f"paper-{mode}", "equity": "100000", "cash": "90000", "last_equity": "100000"},
    ))
    def fake_api(endpoint, mode):
        if endpoint == "positions":
            return []
        raise AssertionError(endpoint)

    def broken_history(*args, **kwargs):
        raise RuntimeError("broker history unavailable")

    monkeypatch.setattr(risk_validator, "_api", fake_api)
    monkeypatch.setattr(risk_validator, "_get_algo_orders_since", broken_history)
    monkeypatch.setattr(risk_validator, "_get_open_orders", lambda mode: [])
    monkeypatch.setattr(risk_validator, "_estimate_weekly_pnl", lambda *args, **kwargs: 0.0)
    monkeypatch.setattr(risk_validator, "get_account_for_mode", lambda mode: SimpleNamespace(account_id=f"paper-{mode}"))
    for mode in ("algo",):
        state = risk_validator.get_portfolio_state(mode=mode)
        assert state["trade_count_today"] is None
        assert not state["entry_count_available"]
        blocked = risk_validator.validate_trade("AAPL", "buy", 1, 100, state, mode=mode)
        assert blocked.rule == "entry_count_unavailable"

def test_short_entries_are_enabled_but_require_borrow_confirmation():
    mode = "algo"
    assert risk_validator.get_mode_config(mode)["risk"]["allow_short_positions"]
    result = risk_validator.validate_trade("AAPL", "short", 1, 100, _state(), mode=mode)
    assert result.rule == "shortability_unavailable"
    assert "borrow availability" in result.reason


def test_covers_cannot_exceed_verified_short_position():
    short_state = _state(positions={"AAPL": {"qty": -5, "market_value": -500}})
    assert risk_validator.validate_trade("AAPL", "cover", 6, 100, short_state, mode="algo").rule == "overcover"
    assert risk_validator.validate_trade("AAPL", "cover", 5, 100, short_state, mode="algo").approved


def test_algo_short_entry_requires_easy_to_borrow_and_respects_gross_caps():
    state = _state(
        cash=25_000,
        shortability_available=True,
        easy_to_borrow_symbols=["AAPL"],
    )
    allowed = risk_validator.validate_trade("AAPL", "short", 40, 100, state, mode="algo")
    assert allowed.approved

    hard_to_borrow = risk_validator.validate_trade(
        "AAPL", "short", 40, 100,
        {**state, "easy_to_borrow_symbols": []}, mode="algo",
    )
    assert hard_to_borrow.rule == "not_easy_to_borrow"

    full_book = _state(
        positions={"AVGO": {"symbol": "AVGO", "qty": -180, "market_value": -18_000}},
        sector_alloc={"core_tech": 18_000},
        open_position_count=1,
        shortability_available=True,
        easy_to_borrow_symbols=["AAPL"],
    )
    over_cap = risk_validator.validate_trade("AAPL", "short", 30, 100, full_book, mode="algo")
    assert over_cap.rule == "max_short_exposure_pct"


def test_algo_short_entries_fail_closed_when_borrow_status_is_unavailable():
    result = risk_validator.validate_trade(
        "AAPL", "short", 1, 100,
        {**_state(), "shortability_available": False}, mode="algo",
    )
    assert result.rule == "shortability_unavailable"


def test_algo_tactical_satellites_are_limited_to_three_positions():
    risk_validator._universe_cache.pop("algo", None)
    state = _state(
        positions={
            "PLTR": {"symbol": "PLTR", "qty": 10, "market_value": 1_000},
            "CRM": {"symbol": "CRM", "qty": 10, "market_value": 1_000},
            "TSLA": {"symbol": "TSLA", "qty": 10, "market_value": 1_000},
        },
        sector_alloc={"tactical": 3_000},
        open_position_count=3,
    )

    blocked_new_name = risk_validator.validate_trade("PANW", "buy", 1, 100, state, mode="algo")
    allowed_scale_in = risk_validator.validate_trade("PLTR", "buy", 1, 100, state, mode="algo")

    assert blocked_new_name.rule == "max_tactical_positions"
    assert allowed_scale_in.approved

    short_state = _state(
        positions={
            "PLTR": {"symbol": "PLTR", "qty": -10, "market_value": -1_000},
            "CRM": {"symbol": "CRM", "qty": -10, "market_value": -1_000},
            "TSLA": {"symbol": "TSLA", "qty": -10, "market_value": -1_000},
        },
        sector_alloc={"tactical": 3_000},
        open_position_count=3,
        shortability_available=True,
        easy_to_borrow_symbols=["PANW"],
    )
    blocked_short_satellite = risk_validator.validate_trade(
        "PANW", "short", 1, 100, short_state, mode="algo",
    )
    assert blocked_short_satellite.rule == "max_tactical_positions"

def test_uncatalogued_entry_cannot_skip_sector_limits():
    for mode in ("algo",):
        result = risk_validator.validate_trade("ZZZZ", "buy", 1, 100, _state(), mode=mode)
        assert result.rule == "outside_risk_catalog"

def test_limit_bracket_keeps_entry_limit_and_sell_bracket_is_rejected(monkeypatch):
    monkeypatch.setattr(execution_runner, "_active_mode", "algo")
    submitted = []

    def submit(order, order_type):
        submitted.append(order)
        return {"success": True, "order_id": "bracket-1", "broker_status": "new"}

    monkeypatch.setattr(execution_runner, "_submit_broker_order", submit)
    buy = execution_runner.place_order(
        "AAPL", "buy", 10, order_type="limit", limit_price=100,
        stop_price=95, take_profit=110,
    )
    assert buy["success"]
    assert submitted == [{
        "symbol": "AAPL", "qty": "10", "side": "buy", "type": "limit",
        "time_in_force": "gtc", "order_class": "bracket",
        "stop_loss": {"stop_price": "95.0"},
        "take_profit": {"limit_price": "110.0"}, "limit_price": "100.0",
        "position_intent": "buy_to_open",
    }]

    sell = execution_runner.place_order(
        "AAPL", "sell", 10, order_type="market",
        stop_price=95, take_profit=110,
    )
    assert not sell["success"]
    assert "use OCO" in sell["error"]
    assert len(submitted) == 1


def test_algo_short_entry_submits_sell_side_bracket_with_mirrored_levels(monkeypatch):
    monkeypatch.setattr(execution_runner, "_active_mode", "algo")
    submitted = []

    def submit(order, order_type):
        submitted.append(order)
        return {"success": True, "order_id": "short-bracket-1", "broker_status": "new"}

    monkeypatch.setattr(execution_runner, "_submit_broker_order", submit)
    result = execution_runner.place_order(
        "AAPL", "short", 10, order_type="market", entry_price=100,
        stop_price=102, take_profit=96,
    )

    assert result["success"]
    assert submitted == [{
        "symbol": "AAPL", "qty": "10", "side": "sell", "type": "market",
        "time_in_force": "gtc", "order_class": "bracket",
        "stop_loss": {"stop_price": "102.0"},
        "take_profit": {"limit_price": "96.0"},
        "position_intent": "sell_to_open",
    }]

    invalid = execution_runner.place_order(
        "AAPL", "short", 10, order_type="market", entry_price=100,
        stop_price=96, take_profit=102,
    )
    assert not invalid["success"]
    assert len(submitted) == 1

    cover = execution_runner.place_order("AAPL", "cover", 10, order_type="market")
    assert cover["success"]
    assert submitted[-1]["side"] == "buy"
    assert submitted[-1]["position_intent"] == "buy_to_close"

def test_unknown_order_type_never_becomes_market_order(monkeypatch):
    monkeypatch.setattr(
        execution_runner, "_submit_broker_order",
        lambda *args: pytest.fail("unknown order type reached the broker"),
    )
    result = execution_runner.place_order("AAPL", "buy", 1, order_type="limt", limit_price=100)
    assert not result["success"]
    assert "Unsupported order type" in result["error"]
    for order_type, fields in (
        ("market", {"limit_price": 100}),
        ("market", {"stop_price": 95}),
        ("stop", {"stop_price": 95, "take_profit": 110}),
        ("trailing_stop", {"trail_percent": 2, "stop_price": 95}),
    ):
        mixed = execution_runner.place_order("AAPL", "buy", 1, order_type=order_type, **fields)
        assert not mixed["success"]


def test_extended_hours_exit_rejects_market_order_without_broker_submission(monkeypatch):
    monkeypatch.setattr(execution_runner, "_active_mode", "algo")
    monkeypatch.setattr(
        execution_runner, "_submit_broker_order",
        lambda *args: pytest.fail("unsupported extended-hours market order reached broker"),
    )

    result = execution_runner.place_order(
        "AAPL", "sell", 2, order_type="market", extended_hours=True,
    )

    assert not result["success"]
    assert "Extended-hours orders require a buy/sell/cover limit order" in result["error"]


def test_off_hours_batch_is_blocked_before_order_cancellation_or_submission():
    hooks = BatchHooks(
        risk_validator_available=True,
        rm_validate_trade=lambda **kwargs: None,
        rm_stage_approved_trade=lambda *args, **kwargs: None,
        get_latest_ask=lambda *args, **kwargs: 100,
        get_latest_bid=lambda *args, **kwargs: 100,
        get_latest_price=lambda *args, **kwargs: 100,
        validate_position_reduction=lambda *args, **kwargs: (True, ""),
        cancel_open_orders_for_symbol=lambda *args, **kwargs: pytest.fail("off-hours cancel reached"),
        get_positions=lambda *args, **kwargs: {},
        place_order=lambda *args, **kwargs: pytest.fail("off-hours order reached"),
        market_tz=ET,
        log=logging.getLogger("test"),
        get_market_schedule=lambda on_date=None, mode="algo": SimpleNamespace(market_open=lambda now: False) if on_date is None else None,
    )

    results, submitted, would_execute = execute_trade_batch(
        trades=[{"ticker": "AAPL", "action": "sell", "qty": 1, "strategy": "swing_momentum"}],
        decision_digest="test",
        args=SimpleNamespace(dry_run=False),
        mode="algo",
        positions={"AAPL": {"qty": 1}},
        rm_portfolio_state=None,
        hooks=hooks,
    )

    assert submitted == 0
    assert would_execute == 0
    assert results[0]["rule"] == "regular_hours_only"
    assert results[0]["safe_to_retry"] is True


def test_extended_hours_batch_blocks_market_and_bracket_trades_before_any_order(monkeypatch):
    from execution import batch

    class PremarketClock(datetime):
        @classmethod
        def now(cls, tz=None):
            frozen = datetime(2026, 10, 8, 5, 0, tzinfo=ET)
            return frozen if tz is None else frozen.astimezone(tz)

    monkeypatch.setattr(batch, "datetime", PremarketClock)
    hooks = BatchHooks(
        risk_validator_available=True,
        rm_validate_trade=lambda **kwargs: None,
        rm_stage_approved_trade=lambda *args, **kwargs: None,
        get_latest_ask=lambda *args, **kwargs: 100,
        get_latest_bid=lambda *args, **kwargs: 100,
        get_latest_price=lambda *args, **kwargs: 100,
        validate_position_reduction=lambda *args, **kwargs: (True, ""),
        cancel_open_orders_for_symbol=lambda *args, **kwargs: pytest.fail("extended batch cancelled protection"),
        get_positions=lambda *args, **kwargs: {},
        place_order=lambda *args, **kwargs: pytest.fail("unsupported extended-hours order reached placement"),
        market_tz=ET,
        log=logging.getLogger("test"),
        get_market_schedule=lambda on_date=None, mode="algo": (
            market_calendar.MarketSchedule(
                date(2026, 10, 8),
                datetime(2026, 10, 8, 9, 30, tzinfo=ET),
                datetime(2026, 10, 8, 16, 0, tzinfo=ET),
            ) if (on_date or date(2026, 10, 8)) == date(2026, 10, 8) else None
        ),
    )

    results, submitted, would_execute = execute_trade_batch(
        trades=[
            {"ticker": "AAPL", "action": "buy", "qty": 1, "order_type": "market", "extended_hours": True, "limit_price": 100},
            {"ticker": "MSFT", "action": "buy", "qty": 1, "order_type": "bracket", "extended_hours": True, "limit_price": 100, "stop_price": 95, "take_profit": 110},
        ],
        decision_digest="test",
        args=SimpleNamespace(dry_run=False),
        mode="algo",
        positions={},
        rm_portfolio_state=None,
        hooks=hooks,
    )

    assert submitted == 0
    assert would_execute == 0
    assert [row["rule"] for row in results] == ["extended_hours_limit_only", "extended_hours_limit_only"]


def test_off_hours_order_submission_fails_closed_before_post(monkeypatch):
    monkeypatch.setattr(execution_runner, "_verify_algo_execution_lease", lambda: None)
    monkeypatch.setattr(execution_runner, "_headers", lambda *_: {})
    monkeypatch.setattr(
        execution_runner, "get_market_schedule",
        lambda on_date=None, mode="algo": SimpleNamespace(market_open=lambda now: False) if on_date is None else None,
    )
    monkeypatch.setattr(
        execution_runner.requests, "post",
        lambda *args, **kwargs: pytest.fail("off-hours broker POST reached"),
    )

    result = execution_runner._submit_broker_order({
        "symbol": "AAPL", "side": "sell", "qty": "1", "type": "market",
        "time_in_force": "day", "client_order_id": "off-hours-id",
    }, "market")

    assert not result["success"]
    assert "restricted to regular market hours" in result["error"]

def test_algo_bracket_exit_never_cancels_broker_protection(monkeypatch):
    bracket_leg = {
        "id": "stop-1", "symbol": "CRM", "side": "sell", "type": "stop",
        "order_class": "bracket", "status": "held",
    }
    monkeypatch.setattr(execution_runner, "_headers", lambda mode: {})
    monkeypatch.setattr(execution_runner, "_list_open_orders", lambda headers: [bracket_leg])
    monkeypatch.setattr(execution_runner, "_cancel_orders", lambda *args: pytest.fail("bracket stop was canceled"))

    result = execution_runner.cancel_open_orders_for_symbol("CRM", mode="algo")

    assert not result["success"]
    assert "Cannot cancel" in result["error"]

def test_pending_cancel_leg_is_polled_without_repeated_delete(monkeypatch):
    pending = {
        "id": "target-1", "symbol": "CRM", "side": "sell", "type": "limit",
        "order_class": "bracket", "status": "pending_cancel",
    }
    monkeypatch.setattr(execution_runner, "_headers", lambda mode: {})
    monkeypatch.setattr(execution_runner, "_list_open_orders", lambda headers: [pending])
    monkeypatch.setattr(execution_runner, "_cancel_orders", lambda *args: pytest.fail("pending cancel resent"))
    monkeypatch.setattr(execution_runner.time, "sleep", lambda delay: None)

    result = execution_runner.cancel_open_orders_for_symbol("CRM", mode="algo")

    assert not result["success"]
    assert "pending_cancel" in result["error"]


def test_strategy_exit_does_not_submit_while_bracket_cancellation_is_ambiguous(
    tmp_path, monkeypatch, capsys,
):
    from reporting import journal as trade_journal

    bracket_leg = {
        "id": "stop-1", "symbol": "AAPL", "side": "sell", "type": "stop",
        "order_class": "bracket", "status": "pending_cancel",
    }
    decision_file = tmp_path / "sell.json"
    decision_file.write_text(json.dumps({"trades": [{
        "ticker": "AAPL", "action": "sell", "qty": 2,
        "entry_price": 100, "strategy": "swing_trend_following",
        "client_order_id": "stable-rule-sell", "cancel_open_orders": True,
    }]}))
    account = {"id": "paper-account", "equity": "100000", "cash": "90000", "last_equity": "100000"}
    monkeypatch.setattr(execution_runner, "get_account_for_mode", lambda mode: SimpleNamespace(name="Algo"))
    monkeypatch.setattr(execution_runner, "_verify_algo_execution_lease", lambda: None)
    monkeypatch.setattr(execution_runner, "verify_account_for_mode", lambda mode: (None, account))
    monkeypatch.setattr(execution_runner, "get_positions", lambda: {"AAPL": {"qty": "2", "current_price": 100}})
    monkeypatch.setattr(execution_runner, "get_daily_pnl", lambda account: 0.0)
    monkeypatch.setattr(execution_runner, "RISK_VALIDATOR_AVAILABLE", True)
    monkeypatch.setattr(execution_runner, "rm_get_portfolio_state", lambda mode: _state(positions={"AAPL": {"qty": 2}}))
    monkeypatch.setattr(execution_runner, "rm_validate_trade", lambda **kwargs: SimpleNamespace(
        approved=True, reason="", rule="",
    ))
    monkeypatch.setattr(execution_runner, "validate_position_reduction", lambda *args, **kwargs: (True, ""))
    monkeypatch.setattr(execution_runner, "_headers", lambda mode: {})
    monkeypatch.setattr(execution_runner, "_list_open_orders", lambda headers: [bracket_leg])
    monkeypatch.setattr(execution_runner.time, "sleep", lambda delay: None)
    monkeypatch.setattr(
        execution_runner, "place_order",
        lambda *args, **kwargs: pytest.fail("sell submitted before bracket cancellation was verified"),
    )
    monkeypatch.setattr(trade_journal, "append_trades", lambda output: 0)

    args = argparse.Namespace(
        file=str(decision_file), dry_run=False, flatten=False,
    )
    exit_code = execution_runner._run_main(args, "algo")
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["results"][0]["status"] == "failed"
    assert "pending_cancel" in output["results"][0]["reason"]


@pytest.mark.parametrize(("refreshed_qty", "should_submit"), [(2, True), (1, False)])
def test_strategy_exit_submits_only_after_bracket_cancel_and_position_refresh(
    tmp_path, monkeypatch, capsys, refreshed_qty, should_submit,
):
    from reporting import journal as trade_journal

    bracket_leg = {
        "id": "stop-1", "symbol": "AAPL", "side": "sell", "type": "stop",
        "order_class": "bracket", "status": "new",
    }
    decision_file = tmp_path / "sell.json"
    decision_file.write_text(json.dumps({"trades": [{
        "ticker": "AAPL", "action": "sell", "qty": 2,
        "entry_price": 100, "strategy": "swing_trend_following",
        "client_order_id": "stable-rule-sell", "cancel_open_orders": True,
    }]}))
    account = {"id": "paper-account", "equity": "100000", "cash": "90000", "last_equity": "100000"}
    events = []
    order_snapshots = iter([[bracket_leg], []])
    positions = iter([
        {"AAPL": {"qty": "2", "current_price": 100}},
        {"AAPL": {"qty": str(refreshed_qty), "current_price": 100}},
    ])
    monkeypatch.setattr(execution_runner, "get_account_for_mode", lambda mode: SimpleNamespace(name="Algo"))
    monkeypatch.setattr(execution_runner, "_verify_algo_execution_lease", lambda: None)
    monkeypatch.setattr(execution_runner, "verify_account_for_mode", lambda mode: (None, account))
    monkeypatch.setattr(
        execution_runner, "get_positions",
        lambda: (events.append("positions") or next(positions)),
    )
    monkeypatch.setattr(execution_runner, "get_daily_pnl", lambda account: 0.0)
    monkeypatch.setattr(execution_runner, "RISK_VALIDATOR_AVAILABLE", True)
    monkeypatch.setattr(execution_runner, "rm_get_portfolio_state", lambda mode: _state(positions={"AAPL": {"qty": 2}}))
    monkeypatch.setattr(execution_runner, "rm_validate_trade", lambda **kwargs: SimpleNamespace(
        approved=True, reason="", rule="",
    ))
    monkeypatch.setattr(execution_runner, "_headers", lambda mode: {})
    monkeypatch.setattr(
        execution_runner, "_list_open_orders",
        lambda headers: (events.append("orders") or next(order_snapshots)),
    )
    monkeypatch.setattr(
        execution_runner, "_cancel_orders",
        lambda headers, orders: (events.append("cancel") or (["stop-1"], [])),
    )
    monkeypatch.setattr(execution_runner.time, "sleep", lambda delay: None)

    def submit(ticker, action, qty, **kwargs):
        events.append("submit")
        return {"success": True, "order_id": "broker-sell", "broker_status": "new"}

    monkeypatch.setattr(execution_runner, "place_order", submit)
    monkeypatch.setattr(trade_journal, "append_trades", lambda output: 0)

    args = argparse.Namespace(
        file=str(decision_file), dry_run=False, flatten=False,
    )
    exit_code = execution_runner._run_main(args, "algo")
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert events == [
        "positions", "orders", "cancel", "orders", "positions",
        *( ["submit"] if should_submit else [] ),
    ]
    assert output["results"][0]["status"] == ("submitted" if should_submit else "blocked")
    if should_submit:
        assert output["results"][0]["client_order_id"] == "stable-rule-sell"
    else:
        assert "exceeds the 1 shares currently held" in output["results"][0]["reason"]


def test_protective_cancel_must_be_verified_even_when_explicitly_allowed(monkeypatch):
    bracket_leg = {
        "id": "stop-1", "symbol": "CRM", "side": "sell", "type": "stop",
        "order_class": "bracket", "status": "new",
    }
    monkeypatch.setattr(execution_runner, "_headers", lambda mode: {})
    monkeypatch.setattr(execution_runner, "_list_open_orders", lambda headers: [bracket_leg])
    monkeypatch.setattr(execution_runner, "_cancel_orders", lambda headers, orders: (["stop-1"], []))
    monkeypatch.setattr(execution_runner.time, "sleep", lambda delay: None)

    result = execution_runner.cancel_open_orders_for_symbol(
        "CRM", mode="algo", allow_protective=True,
    )

    assert not result["success"]
    assert "Open CRM orders remained after cancellation" in result["error"]

def test_flatten_cancels_pending_orders_then_confirms_zero_exposure(monkeypatch):
    events = []
    positions = iter([
        [{"symbol": "AAPL", "qty": "5"}],
        [{"symbol": "AAPL", "qty": "5"}],
        [],
    ])
    orders = iter([[{"id": "buy-pending", "symbol": "AAPL"}], [], []])
    monkeypatch.setattr(execution_runner, "_headers", lambda mode: {})
    monkeypatch.setattr(execution_runner, "_verify_algo_execution_lease", lambda: None)
    monkeypatch.setattr(execution_runner, "verify_account_for_mode", lambda mode: None)
    monkeypatch.setattr(execution_runner, "_list_positions", lambda headers: next(positions))
    monkeypatch.setattr(execution_runner, "_list_open_orders", lambda headers: next(orders))
    monkeypatch.setattr(execution_runner, "_cancel_orders", lambda headers, rows: (events.append("cancel") or ["buy-pending"], []))
    monkeypatch.setattr(execution_runner, "_submit_flatten_order", lambda headers, row: (events.append("close") or {"status": "submitted", "order_id": "close-1"}))
    monkeypatch.setattr(execution_runner, "get_market_schedule", lambda **kwargs: SimpleNamespace(market_open=lambda now: True))

    result = execution_runner._flatten_all_unlocked(mode="algo", poll_attempts=1, poll_interval=0)

    assert events == ["cancel", "close"]
    assert result["verified_flat"]
    assert result["success"]
    assert result["positions_closed"] == 1
    assert result["open_orders_remaining"] == []

def test_flatten_refuses_to_close_while_buy_cancellation_is_pending(monkeypatch):
    monkeypatch.setattr(execution_runner, "_headers", lambda mode: {})
    monkeypatch.setattr(execution_runner, "_verify_algo_execution_lease", lambda: None)
    monkeypatch.setattr(execution_runner, "verify_account_for_mode", lambda mode: None)
    monkeypatch.setattr(execution_runner, "get_market_schedule", lambda **kwargs: SimpleNamespace(market_open=lambda now: True))
    monkeypatch.setattr(execution_runner, "_list_positions", lambda headers: [{"symbol": "AAPL", "qty": "5"}])
    monkeypatch.setattr(execution_runner, "_list_open_orders", lambda headers: [{"id": "buy-pending", "symbol": "AAPL"}])
    monkeypatch.setattr(execution_runner, "_cancel_orders", lambda headers, rows: (["buy-pending"], []))
    monkeypatch.setattr(execution_runner, "_submit_flatten_order", lambda *args: pytest.fail("close must wait for cancellation"))

    result = execution_runner._flatten_all_unlocked(mode="algo", poll_attempts=1, poll_interval=0)

    assert result["status"] == "failed"
    assert not result["verified_flat"]
    assert result["open_orders_remaining"] == ["buy-pending"]

def test_executor_keeps_submission_separate_from_broker_fill(tmp_path, monkeypatch, capsys):
    from reporting import journal as trade_journal

    decision_file = tmp_path / "decision.json"
    decision_file.write_text(json.dumps({"trades": [{
        "ticker": "AAPL", "action": "buy", "qty": 1, "entry_price": 1,
    }]}))
    account = {"equity": "100000", "cash": "90000", "last_equity": "100000"}
    risk_references = []
    monkeypatch.setattr(execution_runner, "get_account_for_mode", lambda mode: SimpleNamespace(name="Algo"))
    monkeypatch.setattr(execution_runner, "_verify_algo_execution_lease", lambda: None)
    monkeypatch.setattr(execution_runner, "verify_account_for_mode", lambda mode: (None, account))
    monkeypatch.setattr(execution_runner, "get_positions", lambda: {})
    monkeypatch.setattr(execution_runner, "get_latest_ask", lambda ticker, mode: 200.0)
    monkeypatch.setattr(execution_runner, "rm_get_portfolio_state", lambda mode: _state())
    monkeypatch.setattr(execution_runner, "rm_validate_trade", lambda **kwargs: (
        risk_references.append(kwargs["entry_price"]) or SimpleNamespace(approved=True, reason="")
    ))
    monkeypatch.setattr(execution_runner, "rm_stage_approved_trade", lambda state, *args, **kwargs: state)
    monkeypatch.setattr(execution_runner, "place_order", lambda *args, **kwargs: {
        "success": True, "order_id": "broker-1", "broker_status": "pending_new",
        "filled_qty": "0", "filled_avg_price": None, "idempotent_replay": False,
    })
    monkeypatch.setattr(trade_journal, "append_trades", lambda output: 0)
    args = argparse.Namespace(file=str(decision_file), dry_run=False, flatten=False)

    assert execution_runner._run_main(args, "algo") == 0
    output = json.loads(capsys.readouterr().out)
    row = output["results"][0]
    assert (row["status"], row["submission_status"], row["broker_status"]) == (
        "submitted", "submitted", "pending_new",
    )
    assert output["submitted"] == 1
    assert output["filled_at_submission"] == 0
    assert risk_references == [200.0]

def test_direct_algo_order_requires_coordinator_lease(monkeypatch):
    monkeypatch.setattr(execution_runner, "_active_mode", "algo")
    monkeypatch.setattr(execution_runner, "_verify_algo_execution_lease", lambda: (_ for _ in ()).throw(PermissionError("no lease")))
    monkeypatch.setattr(execution_runner.requests, "post", lambda *args, **kwargs: pytest.fail("broker POST without lease"))

    with pytest.raises(PermissionError, match="no lease"):
        execution_runner.place_order("AAPL", "buy", 1, order_type="limit", limit_price=100)

def test_duplicate_client_order_id_reconciles_without_another_order(monkeypatch):
    monkeypatch.setattr(execution_runner, "_active_mode", "algo")
    monkeypatch.setattr(execution_runner, "_verify_algo_execution_lease", lambda: None)
    monkeypatch.setattr(execution_runner, "_headers", lambda *args: {})
    monkeypatch.setattr(execution_runner.requests, "post", lambda *args, **kwargs: Response({"message": "duplicate"}, 422))
    monkeypatch.setattr(execution_runner, "_lookup_client_order", lambda headers, client_order_id: {
        "id": "broker-1", "status": "filled", "symbol": "AAPL",
        "side": "buy", "qty": "1", "type": "market", "order_class": "simple",
        "client_order_id": "stable-id", "time_in_force": "day",
        "filled_qty": "1", "filled_avg_price": "100",
    })

    result = execution_runner._submit_broker_order({
        "symbol": "AAPL", "side": "buy", "qty": "1", "type": "market",
        "time_in_force": "day", "client_order_id": "stable-id",
    }, "market")

    assert result["success"]
    assert result["idempotent_replay"]
    assert result["broker_status"] == "filled"

def test_replayed_order_rejects_different_time_in_force(monkeypatch):
    monkeypatch.setattr(execution_runner, "_active_mode", "algo")
    monkeypatch.setattr(execution_runner, "_verify_algo_execution_lease", lambda: None)
    monkeypatch.setattr(execution_runner, "_headers", lambda *args: {})
    monkeypatch.setattr(execution_runner.requests, "post", lambda *args, **kwargs: Response({"message": "duplicate"}, 422))
    monkeypatch.setattr(execution_runner, "_lookup_client_order", lambda headers, client_order_id: {
        "id": "broker-1", "status": "filled", "symbol": "AAPL", "side": "buy",
        "qty": "1", "type": "market", "order_class": "simple",
        "client_order_id": "stable-id", "time_in_force": "gtc",
    })

    result = execution_runner._submit_broker_order({
        "symbol": "AAPL", "side": "buy", "qty": "1", "type": "market",
        "time_in_force": "day", "client_order_id": "stable-id",
    }, "market")

    assert not result["success"]
    assert result["order_id"] == "broker-1"
    assert "different broker order instructions" in result["error"]

def test_replayed_oco_rejects_different_stop_leg(monkeypatch):
    monkeypatch.setattr(execution_runner, "_active_mode", "algo")
    monkeypatch.setattr(execution_runner, "_verify_algo_execution_lease", lambda: None)
    monkeypatch.setattr(execution_runner, "_headers", lambda *args: {})
    monkeypatch.setattr(execution_runner.requests, "post", lambda *args, **kwargs: Response({"message": "duplicate"}, 422))
    monkeypatch.setattr(execution_runner, "_lookup_client_order", lambda headers, client_order_id: {
        "id": "oco-1", "status": "new", "symbol": "AAPL", "side": "sell",
        "qty": "2", "type": "limit", "limit_price": "105",
        "order_class": "oco", "client_order_id": "stable-oco", "time_in_force": "gtc",
    })
    monkeypatch.setattr(execution_runner, "_get_nested_order", lambda headers, order_id: {
        "id": "oco-1", "symbol": "AAPL", "side": "sell", "qty": "2",
        "order_class": "oco", "limit_price": "105",
        "legs": [{"type": "stop", "stop_price": "96"}],
    })

    result = execution_runner._submit_broker_order({
        "symbol": "AAPL", "side": "sell", "qty": "2", "type": "limit",
        "limit_price": "105", "order_class": "oco", "time_in_force": "gtc",
        "take_profit": {"limit_price": "105"}, "stop_loss": {"stop_price": "97"},
        "client_order_id": "stable-oco",
    }, "oco")

    assert not result["success"]
    assert result["order_id"] == "oco-1"
    assert "different broker order instructions" in result["error"]

def test_client_order_id_mismatch_preserves_broker_id_for_reconciliation(monkeypatch):
    monkeypatch.setattr(execution_runner, "_active_mode", "algo")
    monkeypatch.setattr(execution_runner, "_verify_algo_execution_lease", lambda: None)
    monkeypatch.setattr(execution_runner, "_headers", lambda *args: {})
    monkeypatch.setattr(execution_runner.requests, "post", lambda *args, **kwargs: Response({"message": "duplicate"}, 422))
    monkeypatch.setattr(execution_runner, "_lookup_client_order", lambda headers, client_order_id: {
        "id": "broker-2", "status": "pending_new", "symbol": "MSFT",
        "side": "buy", "qty": "1", "type": "market", "order_class": "simple",
    })

    result = execution_runner._submit_broker_order({
        "symbol": "AAPL", "side": "buy", "qty": "1", "type": "market",
        "time_in_force": "day", "client_order_id": "stable-id",
    }, "market")

    assert not result["success"]
    assert result["order_id"] == "broker-2"
