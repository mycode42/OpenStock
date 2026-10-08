"""Risk and liquidation gates that must fail visibly on uncertain broker state."""

import argparse
import json
from contextlib import nullcontext
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import execute_trades
import portfolio_monitor
import risk_manager
from src import day_recovery, market_calendar

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

def test_early_close_moves_day_entry_and_flatten_deadlines(monkeypatch):
    monkeypatch.setattr(market_calendar, "get_account_for_mode", lambda mode: SimpleNamespace(headers={}))
    monkeypatch.setattr(
        market_calendar.requests, "get",
        lambda *args, **kwargs: Response([{"date": "2026-11-27", "open": "09:30", "close": "13:00"}]),
    )

    schedule = market_calendar.get_day_schedule(flatten_by="15:45", on_date=date(2026, 11, 27))

    assert schedule.flatten_at == datetime(2026, 11, 27, 12, 45, tzinfo=ET)
    assert schedule.entry_cutoff_at == datetime(2026, 11, 27, 12, 30, tzinfo=ET)
    assert schedule.entries_allowed(datetime(2026, 11, 27, 12, 29, tzinfo=ET))
    assert not schedule.entries_allowed(datetime(2026, 11, 27, 12, 30, tzinfo=ET))
    assert schedule.flatten_due(datetime(2026, 11, 27, 12, 45, tzinfo=ET))

def test_day_entry_blocks_when_calendar_unavailable_or_cutoff_passed(monkeypatch):
    monkeypatch.setattr(risk_manager, "get_day_schedule", lambda **kwargs: None)
    closed = risk_manager.validate_trade("AAPL", "buy", 1, 100, _state(), mode="day")
    assert closed.rule == "day_entry_cutoff"

    def unavailable(**kwargs):
        raise RuntimeError("calendar unavailable")

    monkeypatch.setattr(risk_manager, "get_day_schedule", unavailable)
    unknown = risk_manager.validate_trade("AAPL", "buy", 1, 100, _state(), mode="day")
    assert unknown.rule == "day_calendar_unavailable"

def test_day_entry_requires_today_verified_flat_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(day_recovery, "RECOVERY_PATH", tmp_path / "day_recovery.json")
    session_open = datetime.now(ET) - timedelta(minutes=5)
    monkeypatch.setattr(risk_manager, "get_day_schedule", lambda **kwargs: SimpleNamespace(
        entries_allowed=lambda now: True, opens_at=session_open,
    ))
    monkeypatch.setattr(risk_manager, "get_account_for_mode", lambda mode: SimpleNamespace(account_id="paper-day"))
    monkeypatch.setattr(risk_manager, "day_recovery_confirmed", day_recovery.day_recovery_confirmed)

    before = risk_manager.validate_trade("AAPL", "buy", 1, 100, _state(), mode="day")
    assert before.rule == "day_recovery_unconfirmed"

    day_recovery.record_day_recovery("paper-day")
    after = risk_manager.validate_trade("AAPL", "buy", 1, 100, _state(), mode="day")
    assert after.approved
    assert not day_recovery.day_recovery_confirmed("another-account", session_open_at=session_open)
    assert not day_recovery.day_recovery_confirmed(
        "paper-day", datetime.now(ET) + timedelta(days=1), session_open_at=session_open,
    )
    assert not day_recovery.day_recovery_confirmed(
        "paper-day", session_open_at=datetime.now(ET) + timedelta(minutes=5),
    )

def test_algo_weekly_baseline_uses_prior_close_and_rejects_stale_history(tmp_path, monkeypatch):
    monkeypatch.setattr(risk_manager, "_snapshot_dir", lambda mode: tmp_path)
    monkeypatch.setattr(risk_manager, "_risk_baseline_dir", lambda mode: tmp_path / "inception")
    today = datetime.now(ET).date()
    week_start = today - timedelta(days=today.weekday())
    previous_friday = week_start - timedelta(days=3)
    current_week_close = week_start
    (tmp_path / f"{current_week_close}.json").write_text(json.dumps({
        "date": current_week_close.isoformat(), "equity": 100_000,
        "trading_mode": "algo", "account_id": "paper-algo", "snapshot_kind": "close",
    }))
    assert risk_manager._estimate_weekly_pnl(
        95_000, mode="algo", account_id="paper-algo", session_day=today,
    ) is None
    close_file = tmp_path / f"{previous_friday}.json"
    close_file.write_text(json.dumps({
        "date": previous_friday.isoformat(), "equity": 105_000,
        "trading_mode": "algo", "account_id": "paper-algo", "snapshot_kind": "close",
    }))
    assert risk_manager._estimate_weekly_pnl(
        95_000, mode="algo", account_id="paper-algo", session_day=today,
    ) == pytest.approx(95_000 / 105_000 - 1)
    assert risk_manager._estimate_weekly_pnl(
        95_000, mode="algo", account_id="another-account", session_day=today,
    ) is None
    close_file.unlink()
    assert risk_manager._estimate_weekly_pnl(
        95_000, mode="algo", account_id="paper-algo", session_day=today,
    ) is None
    rejected = risk_manager.validate_trade("AAPL", "buy", 1, 100, _state(weekly_pnl_pct=None), mode="algo")
    assert rejected.rule == "weekly_baseline_unavailable"

def test_new_account_inception_baseline_is_account_bound_and_cannot_move(tmp_path, monkeypatch):
    now = datetime(2026, 10, 8, 10, 0, tzinfo=ET)
    broker = {
        "id": "new-algo-id", "created_at": "2026-10-08T08:15:00Z",
        "equity": "1000000", "cash": "1000000",
    }
    monkeypatch.setattr(risk_manager, "_risk_baseline_dir", lambda mode: tmp_path / "baselines")
    monkeypatch.setattr(risk_manager, "_snapshot_dir", lambda mode: tmp_path / "snapshots")
    monkeypatch.setattr(
        risk_manager, "verify_account_for_mode",
        lambda mode: (SimpleNamespace(account_id="new-algo"), broker),
    )
    monkeypatch.setattr(risk_manager, "_api", lambda endpoint, mode: [])

    first = risk_manager.capture_account_inception_baseline("algo", now=now)
    assert first["equity"] == 1_000_000
    assert risk_manager._estimate_weekly_pnl(
        990_000, mode="algo", account_id="new-algo-id", session_day=now.date(),
    ) == pytest.approx(-0.01)
    assert risk_manager._estimate_weekly_pnl(
        990_000, mode="algo", account_id="old-algo-id", session_day=now.date(),
    ) is None

    broker["equity"] = broker["cash"] = "990000"
    with pytest.raises(ValueError, match="differs"):
        risk_manager.capture_account_inception_baseline("algo", now=now + timedelta(minutes=1))
    broker["equity"] = broker["cash"] = "1000000"
    monkeypatch.setattr(risk_manager, "_api", lambda endpoint, mode: [{}] if endpoint == "positions" else [])
    with pytest.raises(ValueError, match="zero positions"):
        risk_manager.capture_account_inception_baseline("algo", now=now)

def test_pending_buy_reserves_cash_and_sector_before_new_entry(monkeypatch):
    monkeypatch.setattr(risk_manager, "verify_account_for_mode", lambda mode: (
        SimpleNamespace(account_id="paper-swing"),
        {"id": "paper-swing", "equity": "100000", "cash": "25000", "last_equity": "100000"},
    ))
    def fake_api(endpoint, mode):
        if endpoint == "positions":
            return []
        raise AssertionError(endpoint)

    monkeypatch.setattr(risk_manager, "_api", fake_api)
    monkeypatch.setattr(risk_manager, "_get_algo_orders_since", lambda *args, **kwargs: [])
    monkeypatch.setattr(risk_manager, "_get_open_orders", lambda mode: [{
        "id": "pending", "symbol": "AAPL", "side": "buy", "qty": "50",
        "filled_qty": "0", "limit_price": "100",
    }])
    monkeypatch.setattr(risk_manager, "_estimate_weekly_pnl", lambda *args, **kwargs: 0.0)

    state = risk_manager.get_portfolio_state(mode="swing")

    assert state["pending_buy_reservation"] == pytest.approx(5_100)
    assert state["available_cash"] == pytest.approx(19_900)
    assert state["sector_alloc"]["core_tech"] == pytest.approx(5_100)
    assert state["open_position_count"] == 1
    blocked = risk_manager.validate_trade("MSFT", "buy", 10, 100, state, mode="swing")
    assert blocked.rule == "min_cash_reserve"

def test_failed_order_history_blocks_swing_and_day_entries(monkeypatch):
    monkeypatch.setattr(risk_manager, "verify_account_for_mode", lambda mode: (
        SimpleNamespace(account_id=f"paper-{mode}"),
        {"id": f"paper-{mode}", "equity": "100000", "cash": "90000", "last_equity": "100000"},
    ))
    def fake_api(endpoint, mode):
        if endpoint == "positions":
            return []
        raise AssertionError(endpoint)

    def broken_history(*args, **kwargs):
        raise RuntimeError("broker history unavailable")

    monkeypatch.setattr(risk_manager, "_api", fake_api)
    monkeypatch.setattr(risk_manager, "_get_algo_orders_since", broken_history)
    monkeypatch.setattr(risk_manager, "_get_open_orders", lambda mode: [])
    monkeypatch.setattr(risk_manager, "_estimate_weekly_pnl", lambda *args, **kwargs: 0.0)
    monkeypatch.setattr(risk_manager, "get_account_for_mode", lambda mode: SimpleNamespace(account_id=f"paper-{mode}"))
    monkeypatch.setattr(risk_manager, "get_day_schedule", lambda **kwargs: SimpleNamespace(
        entries_allowed=lambda now: True, opens_at=datetime.now(ET) - timedelta(minutes=5),
    ))
    monkeypatch.setattr(risk_manager, "day_recovery_confirmed", lambda account_id, **kwargs: True)

    for mode in ("swing", "day"):
        state = risk_manager.get_portfolio_state(mode=mode)
        assert state["trade_count_today"] is None
        assert not state["entry_count_available"]
        blocked = risk_manager.validate_trade("AAPL", "buy", 1, 100, state, mode=mode)
        assert blocked.rule == "entry_count_unavailable"

def test_short_entries_and_overcovers_are_blocked():
    assert risk_manager.validate_trade("AAPL", "short", 1, 100, _state(), mode="swing").rule == "short_disabled"
    short_state = _state(positions={"AAPL": {"qty": -5, "market_value": -500}})
    assert risk_manager.validate_trade("AAPL", "cover", 6, 100, short_state, mode="swing").rule == "overcover"
    assert risk_manager.validate_trade("AAPL", "cover", 5, 100, short_state, mode="swing").approved


def test_algo_tactical_satellites_are_limited_to_two_positions():
    risk_manager._universe_cache.pop("algo", None)
    state = _state(
        positions={
            "PLTR": {"symbol": "PLTR", "qty": 10, "market_value": 1_000},
            "CRM": {"symbol": "CRM", "qty": 10, "market_value": 1_000},
        },
        sector_alloc={"tactical": 2_000},
        open_position_count=2,
    )

    blocked_new_name = risk_manager.validate_trade("PANW", "buy", 1, 100, state, mode="algo")
    allowed_scale_in = risk_manager.validate_trade("PLTR", "buy", 1, 100, state, mode="algo")

    assert blocked_new_name.rule == "max_tactical_positions"
    assert allowed_scale_in.approved

def test_uncatalogued_entry_cannot_skip_sector_limits():
    for mode in ("swing", "day", "algo"):
        result = risk_manager.validate_trade("ZZZZ", "buy", 1, 100, _state(), mode=mode)
        assert result.rule == "outside_risk_catalog"

def test_limit_bracket_keeps_entry_limit_and_sell_bracket_is_rejected(monkeypatch):
    monkeypatch.setattr(execute_trades, "_active_mode", "swing")
    submitted = []

    def submit(order, order_type):
        submitted.append(order)
        return {"success": True, "order_id": "bracket-1", "broker_status": "new"}

    monkeypatch.setattr(execute_trades, "_submit_broker_order", submit)
    buy = execute_trades.place_order(
        "AAPL", "buy", 10, order_type="limit", limit_price=100,
        stop_price=95, take_profit=110,
    )
    assert buy["success"]
    assert submitted == [{
        "symbol": "AAPL", "qty": "10", "side": "buy", "type": "limit",
        "time_in_force": "gtc", "order_class": "bracket",
        "stop_loss": {"stop_price": "95.0"},
        "take_profit": {"limit_price": "110.0"}, "limit_price": "100.0",
    }]

    sell = execute_trades.place_order(
        "AAPL", "sell", 10, order_type="market",
        stop_price=95, take_profit=110,
    )
    assert not sell["success"]
    assert "use OCO" in sell["error"]
    assert len(submitted) == 1

def test_unknown_order_type_never_becomes_market_order(monkeypatch):
    monkeypatch.setattr(
        execute_trades, "_submit_broker_order",
        lambda *args: pytest.fail("unknown order type reached the broker"),
    )
    result = execute_trades.place_order("AAPL", "buy", 1, order_type="limt", limit_price=100)
    assert not result["success"]
    assert "Unsupported order type" in result["error"]
    for order_type, fields in (
        ("market", {"limit_price": 100}),
        ("market", {"stop_price": 95}),
        ("stop", {"stop_price": 95, "take_profit": 110}),
        ("trailing_stop", {"trail_percent": 2, "stop_price": 95}),
    ):
        mixed = execute_trades.place_order("AAPL", "buy", 1, order_type=order_type, **fields)
        assert not mixed["success"]

def test_algo_bracket_exit_never_cancels_broker_protection(monkeypatch):
    bracket_leg = {
        "id": "stop-1", "symbol": "CRM", "side": "sell", "type": "stop",
        "order_class": "bracket", "status": "held",
    }
    monkeypatch.setattr(execute_trades, "_headers", lambda mode: {})
    monkeypatch.setattr(execute_trades, "_list_open_orders", lambda headers: [bracket_leg])
    monkeypatch.setattr(execute_trades, "_cancel_orders", lambda *args: pytest.fail("bracket stop was canceled"))

    result = execute_trades.cancel_open_orders_for_symbol("CRM", mode="algo")

    assert not result["success"]
    assert "Cannot cancel" in result["error"]

def test_pending_cancel_leg_is_polled_without_repeated_delete(monkeypatch):
    pending = {
        "id": "target-1", "symbol": "CRM", "side": "sell", "type": "limit",
        "order_class": "bracket", "status": "pending_cancel",
    }
    monkeypatch.setattr(execute_trades, "_headers", lambda mode: {})
    monkeypatch.setattr(execute_trades, "_list_open_orders", lambda headers: [pending])
    monkeypatch.setattr(execute_trades, "_cancel_orders", lambda *args: pytest.fail("pending cancel resent"))
    monkeypatch.setattr(execute_trades.time, "sleep", lambda delay: None)

    result = execute_trades.cancel_open_orders_for_symbol("CRM", mode="algo")

    assert not result["success"]
    assert "pending_cancel" in result["error"]

def test_flatten_cancels_pending_orders_then_confirms_zero_exposure(monkeypatch):
    events = []
    positions = iter([
        [{"symbol": "AAPL", "qty": "5"}],
        [{"symbol": "AAPL", "qty": "5"}],
        [],
    ])
    orders = iter([[{"id": "buy-pending", "symbol": "AAPL"}], [], []])
    monkeypatch.setattr(execute_trades, "_headers", lambda mode: {})
    monkeypatch.setattr(execute_trades, "get_account_for_mode", lambda mode: SimpleNamespace(account_id="paper-day"))
    monkeypatch.setattr(execute_trades, "record_day_recovery", lambda account_id: {"verified_flat": True})
    monkeypatch.setattr(execute_trades, "verify_account_for_mode", lambda mode: None)
    monkeypatch.setattr(execute_trades, "_list_positions", lambda headers: next(positions))
    monkeypatch.setattr(execute_trades, "_list_open_orders", lambda headers: next(orders))
    monkeypatch.setattr(execute_trades, "_cancel_orders", lambda headers, rows: (events.append("cancel") or ["buy-pending"], []))
    monkeypatch.setattr(execute_trades, "_submit_flatten_order", lambda headers, row: (events.append("close") or {"status": "submitted", "order_id": "close-1"}))
    monkeypatch.setattr(execute_trades, "get_day_schedule", lambda **kwargs: SimpleNamespace(market_open=lambda now: True))

    result = execute_trades._flatten_all_unlocked(mode="day", poll_attempts=1, poll_interval=0)

    assert events == ["cancel", "close"]
    assert result["verified_flat"]
    assert result["positions_closed"] == 1
    assert result["open_orders_remaining"] == []

def test_flatten_refuses_to_close_while_buy_cancellation_is_pending(monkeypatch):
    monkeypatch.setattr(execute_trades, "_headers", lambda mode: {})
    monkeypatch.setattr(execute_trades, "verify_account_for_mode", lambda mode: None)
    monkeypatch.setattr(execute_trades, "get_day_schedule", lambda **kwargs: SimpleNamespace(market_open=lambda now: True))
    monkeypatch.setattr(execute_trades, "_list_positions", lambda headers: [{"symbol": "AAPL", "qty": "5"}])
    monkeypatch.setattr(execute_trades, "_list_open_orders", lambda headers: [{"id": "buy-pending", "symbol": "AAPL"}])
    monkeypatch.setattr(execute_trades, "_cancel_orders", lambda headers, rows: (["buy-pending"], []))
    monkeypatch.setattr(execute_trades, "_submit_flatten_order", lambda *args: pytest.fail("close must wait for cancellation"))

    result = execute_trades._flatten_all_unlocked(mode="day", poll_attempts=1, poll_interval=0)

    assert result["status"] == "failed"
    assert not result["verified_flat"]
    assert result["open_orders_remaining"] == ["buy-pending"]

def test_day_flatten_after_close_preserves_existing_stop(monkeypatch):
    monkeypatch.setattr(execute_trades, "_headers", lambda mode: {})
    monkeypatch.setattr(execute_trades, "verify_account_for_mode", lambda mode: None)
    monkeypatch.setattr(execute_trades, "get_day_schedule", lambda **kwargs: SimpleNamespace(market_open=lambda now: False))
    monkeypatch.setattr(execute_trades, "_list_positions", lambda headers: [{"symbol": "AAPL", "qty": "5"}])
    monkeypatch.setattr(execute_trades, "_list_open_orders", lambda headers: [{
        "id": "stop-1", "symbol": "AAPL", "side": "sell", "type": "stop", "status": "new",
    }])
    monkeypatch.setattr(
        execute_trades, "_cancel_orders",
        lambda *args: pytest.fail("a closed-market guard must preserve protective orders"),
    )
    monkeypatch.setattr(
        execute_trades, "_submit_flatten_order",
        lambda *args: pytest.fail("a closed-market guard cannot close the position"),
    )

    result = execute_trades._flatten_all_unlocked(mode="day", poll_attempts=1, poll_interval=0)

    assert result["status"] == "failed"
    assert result["positions_remaining"] == [{"ticker": "AAPL", "qty": "5"}]
    assert result["open_orders_remaining"] == ["stop-1"]

def test_day_guard_returns_nonzero_when_due_flatten_is_incomplete(monkeypatch, capsys):
    monkeypatch.setattr(execute_trades, "get_account_for_mode", lambda mode: SimpleNamespace(name="Day"))
    monkeypatch.setattr(execute_trades, "verify_account_for_mode", lambda mode: (None, {}))
    monkeypatch.setattr(execute_trades, "get_day_schedule", lambda **kwargs: SimpleNamespace(flatten_due=lambda now: True))
    monkeypatch.setattr(execute_trades, "_flatten_all_unlocked", lambda **kwargs: {"status": "failed", "verified_flat": False, "success": False})
    args = argparse.Namespace(dry_run=False, flatten_if_due=True, flatten=False)

    exit_code = execute_trades._run_main(args, "day")

    assert exit_code == 2
    assert json.loads(capsys.readouterr().out)["status"] == "failed"

def test_monitor_reports_incomplete_flatten_before_optional_analytics(monkeypatch):
    monkeypatch.setattr(portfolio_monitor, "get_account_for_mode", lambda mode: SimpleNamespace(name="Day"))
    monkeypatch.setattr(portfolio_monitor, "get_day_schedule", lambda **kwargs: SimpleNamespace(flatten_due=lambda now: True))
    monkeypatch.setattr(execute_trades, "flatten_all", lambda **kwargs: {"status": "failed", "verified_flat": False, "results": [], "errors": ["still long"]})
    monkeypatch.setattr(portfolio_monitor, "get_spy_daily_return", lambda: pytest.fail("analytics delayed liquidation"))
    monkeypatch.setattr(portfolio_monitor, "get_account", lambda: pytest.fail("account reporting delayed liquidation"))

    result = portfolio_monitor.run_monitor(mode="day")

    assert result["status"] == "failed"
    assert result["eod_flatten_attempted"]
    assert not result["eod_flatten"]

def test_monitor_ignores_yesterday_intraday_high(monkeypatch):
    import pandas as pd

    now = datetime.now(ET)
    yesterday = now - timedelta(days=1)
    history = pd.DataFrame(
        {"High": [200.0, 105.0]},
        index=pd.DatetimeIndex([
            yesterday.replace(hour=15, minute=0),
            now.replace(hour=9, minute=31),
        ]),
    )
    monkeypatch.setattr(portfolio_monitor.yf, "Ticker", lambda symbol: SimpleNamespace(
        history=lambda **kwargs: history,
    ))
    assert portfolio_monitor.get_intraday_high("AAPL") == 105.0

    history = history.iloc[:1]
    assert portfolio_monitor.get_intraday_high("AAPL") is None

def test_stop_monitor_uses_idempotent_executor_submission(monkeypatch):
    submitted = []
    monkeypatch.setattr(portfolio_monitor, "_active_mode", "day")
    monkeypatch.setattr(portfolio_monitor, "verify_account_for_mode", lambda mode: None)
    monkeypatch.setattr(portfolio_monitor, "get_day_schedule", lambda **kwargs: SimpleNamespace(
        market_open=lambda now: True, flatten_due=lambda now: False,
    ))
    monkeypatch.setattr(portfolio_monitor, "get_positions", lambda: [{"symbol": "AAPL", "qty": "2.5"}])
    monkeypatch.setattr(execute_trades, "account_execution_lock", lambda mode: nullcontext())
    monkeypatch.setattr(execute_trades, "cancel_open_orders_for_symbol", lambda ticker, mode: {"success": True})

    def submit(order, order_type, mode):
        submitted.append((order, order_type, mode))
        return {"success": True, "order_id": "broker-stop", "broker_status": "pending_new"}

    monkeypatch.setattr(execute_trades, "_submit_broker_order", submit)

    result = portfolio_monitor.place_market_sell("AAPL", 2, "hard stop", dry_run=False)
    replay = portfolio_monitor.place_market_sell("AAPL", 2, "hard stop", dry_run=False)

    assert result["status"] == "submitted"
    assert replay["status"] == "submitted"
    assert result["broker_status"] == "pending_new"
    assert submitted[0][0]["qty"] == "2.5"
    assert submitted[0][2] == "day"
    assert submitted[0][0]["client_order_id"] == submitted[1][0]["client_order_id"]

def test_day_stop_monitor_does_not_cancel_after_close(monkeypatch):
    monkeypatch.setattr(portfolio_monitor, "_active_mode", "day")
    monkeypatch.setattr(portfolio_monitor, "verify_account_for_mode", lambda mode: None)
    monkeypatch.setattr(portfolio_monitor, "get_day_schedule", lambda **kwargs: SimpleNamespace(
        market_open=lambda now: False, flatten_due=lambda now: True,
    ))
    monkeypatch.setattr(execute_trades, "account_execution_lock", lambda mode: nullcontext())
    monkeypatch.setattr(
        execute_trades, "cancel_open_orders_for_symbol",
        lambda *args, **kwargs: pytest.fail("Day stop canceled after close"),
    )
    result = portfolio_monitor.place_market_sell("AAPL", 2, "hard stop", dry_run=False)
    assert result["status"] == "failed"

def test_executor_keeps_submission_separate_from_broker_fill(tmp_path, monkeypatch, capsys):
    import trade_journal

    decision_file = tmp_path / "decision.json"
    decision_file.write_text(json.dumps({"trades": [{
        "ticker": "AAPL", "action": "buy", "qty": 1, "entry_price": 1,
    }]}))
    account = {"equity": "100000", "cash": "90000", "last_equity": "100000"}
    risk_references = []
    monkeypatch.setattr(execute_trades, "get_account_for_mode", lambda mode: SimpleNamespace(name="Swing"))
    monkeypatch.setattr(execute_trades, "verify_account_for_mode", lambda mode: (None, account))
    monkeypatch.setattr(execute_trades, "get_positions", lambda: {})
    monkeypatch.setattr(execute_trades, "get_latest_ask", lambda ticker, mode: 200.0)
    monkeypatch.setattr(execute_trades, "rm_get_portfolio_state", lambda mode: _state())
    monkeypatch.setattr(execute_trades, "rm_validate_trade", lambda **kwargs: (
        risk_references.append(kwargs["entry_price"]) or SimpleNamespace(approved=True, reason="")
    ))
    monkeypatch.setattr(execute_trades, "rm_stage_approved_trade", lambda state, *args, **kwargs: state)
    monkeypatch.setattr(execute_trades, "place_order", lambda *args, **kwargs: {
        "success": True, "order_id": "broker-1", "broker_status": "pending_new",
        "filled_qty": "0", "filled_avg_price": None, "idempotent_replay": False,
    })
    monkeypatch.setattr(trade_journal, "append_trades", lambda output: 0)
    args = argparse.Namespace(file=str(decision_file), dry_run=False, flatten=False, flatten_if_due=False)

    assert execute_trades._run_main(args, "swing") == 0
    output = json.loads(capsys.readouterr().out)
    row = output["results"][0]
    assert (row["status"], row["submission_status"], row["broker_status"]) == (
        "submitted", "submitted", "pending_new",
    )
    assert output["submitted"] == 1
    assert output["filled_at_submission"] == 0
    assert risk_references == [200.0]

def test_direct_algo_order_requires_coordinator_lease(monkeypatch):
    monkeypatch.setattr(execute_trades, "_active_mode", "algo")
    monkeypatch.setattr(execute_trades, "_verify_algo_execution_lease", lambda: (_ for _ in ()).throw(PermissionError("no lease")))
    monkeypatch.setattr(execute_trades.requests, "post", lambda *args, **kwargs: pytest.fail("broker POST without lease"))

    with pytest.raises(PermissionError, match="no lease"):
        execute_trades.place_order("AAPL", "buy", 1, order_type="limit", limit_price=100, extended_hours=True)

def test_duplicate_client_order_id_reconciles_without_another_order(monkeypatch):
    monkeypatch.setattr(execute_trades, "_active_mode", "swing")
    monkeypatch.setattr(execute_trades, "_headers", lambda *args: {})
    monkeypatch.setattr(execute_trades.requests, "post", lambda *args, **kwargs: Response({"message": "duplicate"}, 422))
    monkeypatch.setattr(execute_trades, "_lookup_client_order", lambda headers, client_order_id: {
        "id": "broker-1", "status": "filled", "symbol": "AAPL",
        "side": "buy", "qty": "1", "type": "market", "order_class": "simple",
        "client_order_id": "stable-id", "time_in_force": "day",
        "filled_qty": "1", "filled_avg_price": "100",
    })

    result = execute_trades._submit_broker_order({
        "symbol": "AAPL", "side": "buy", "qty": "1", "type": "market",
        "time_in_force": "day", "client_order_id": "stable-id",
    }, "market")

    assert result["success"]
    assert result["idempotent_replay"]
    assert result["broker_status"] == "filled"

def test_replayed_order_rejects_different_time_in_force(monkeypatch):
    monkeypatch.setattr(execute_trades, "_active_mode", "swing")
    monkeypatch.setattr(execute_trades, "_headers", lambda *args: {})
    monkeypatch.setattr(execute_trades.requests, "post", lambda *args, **kwargs: Response({"message": "duplicate"}, 422))
    monkeypatch.setattr(execute_trades, "_lookup_client_order", lambda headers, client_order_id: {
        "id": "broker-1", "status": "filled", "symbol": "AAPL", "side": "buy",
        "qty": "1", "type": "market", "order_class": "simple",
        "client_order_id": "stable-id", "time_in_force": "gtc",
    })

    result = execute_trades._submit_broker_order({
        "symbol": "AAPL", "side": "buy", "qty": "1", "type": "market",
        "time_in_force": "day", "client_order_id": "stable-id",
    }, "market")

    assert not result["success"]
    assert result["order_id"] == "broker-1"
    assert "different broker order instructions" in result["error"]

def test_replayed_oco_rejects_different_stop_leg(monkeypatch):
    monkeypatch.setattr(execute_trades, "_active_mode", "swing")
    monkeypatch.setattr(execute_trades, "_headers", lambda *args: {})
    monkeypatch.setattr(execute_trades.requests, "post", lambda *args, **kwargs: Response({"message": "duplicate"}, 422))
    monkeypatch.setattr(execute_trades, "_lookup_client_order", lambda headers, client_order_id: {
        "id": "oco-1", "status": "new", "symbol": "AAPL", "side": "sell",
        "qty": "2", "type": "limit", "limit_price": "105",
        "order_class": "oco", "client_order_id": "stable-oco", "time_in_force": "gtc",
    })
    monkeypatch.setattr(execute_trades, "_get_nested_order", lambda headers, order_id: {
        "id": "oco-1", "symbol": "AAPL", "side": "sell", "qty": "2",
        "order_class": "oco", "limit_price": "105",
        "legs": [{"type": "stop", "stop_price": "96"}],
    })

    result = execute_trades._submit_broker_order({
        "symbol": "AAPL", "side": "sell", "qty": "2", "type": "limit",
        "limit_price": "105", "order_class": "oco", "time_in_force": "gtc",
        "take_profit": {"limit_price": "105"}, "stop_loss": {"stop_price": "97"},
        "client_order_id": "stable-oco",
    }, "oco")

    assert not result["success"]
    assert result["order_id"] == "oco-1"
    assert "different broker order instructions" in result["error"]

def test_client_order_id_mismatch_preserves_broker_id_for_reconciliation(monkeypatch):
    monkeypatch.setattr(execute_trades, "_active_mode", "swing")
    monkeypatch.setattr(execute_trades, "_headers", lambda *args: {})
    monkeypatch.setattr(execute_trades.requests, "post", lambda *args, **kwargs: Response({"message": "duplicate"}, 422))
    monkeypatch.setattr(execute_trades, "_lookup_client_order", lambda headers, client_order_id: {
        "id": "broker-2", "status": "pending_new", "symbol": "MSFT",
        "side": "buy", "qty": "1", "type": "market", "order_class": "simple",
    })

    result = execute_trades._submit_broker_order({
        "symbol": "AAPL", "side": "buy", "qty": "1", "type": "market",
        "time_in_force": "day", "client_order_id": "stable-id",
    }, "market")

    assert not result["success"]
    assert result["order_id"] == "broker-2"
