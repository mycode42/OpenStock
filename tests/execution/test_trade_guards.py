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


def test_off_hours_batch_is_blocked_before_exit_checks_or_submission():
    hooks = BatchHooks(
        risk_validator_available=True,
        rm_validate_trade=lambda **kwargs: None,
        rm_stage_approved_trade=lambda *args, **kwargs: None,
        get_latest_ask=lambda *args, **kwargs: 100,
        get_latest_bid=lambda *args, **kwargs: 100,
        get_latest_price=lambda *args, **kwargs: 100,
        validate_position_reduction=lambda *args, **kwargs: (True, ""),
        open_exit_orders_for_symbol=lambda *args, **kwargs: pytest.fail("off-hours exit guard reached"),
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


def _regular_schedule(on_date=None, mode="algo"):
    return SimpleNamespace(market_open=lambda now: True) if on_date is None else None


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


class _PremarketClock(datetime):
    @classmethod
    def now(cls, tz=None):
        frozen = datetime(2026, 10, 8, 5, 0, tzinfo=ET)
        return frozen if tz is None else frozen.astimezone(tz)


def _exit_hooks(*, schedule=_regular_schedule, open_exits=None, place_result=None, open_calls=None, place_calls=None):
    open_calls = [] if open_calls is None else open_calls
    place_calls = [] if place_calls is None else place_calls

    def open_exit_orders(ticker, *, mode, action):
        open_calls.append({"ticker": ticker, "action": action})
        return list(open_exits or [])

    def place(ticker, action, qty, **kwargs):
        place_calls.append({"ticker": ticker, "action": action, "qty": qty, **kwargs})
        return place_result or {"success": True, "status": "submitted", "order_id": "exit-1"}

    return BatchHooks(
        risk_validator_available=True,
        rm_validate_trade=lambda **kwargs: SimpleNamespace(approved=True, reason="", rule=""),
        rm_stage_approved_trade=lambda *args, **kwargs: None,
        get_latest_ask=lambda *args, **kwargs: 100,
        get_latest_bid=lambda *args, **kwargs: 100,
        get_latest_price=lambda *args, **kwargs: 100,
        validate_position_reduction=lambda *args, **kwargs: (True, ""),
        open_exit_orders_for_symbol=open_exit_orders,
        get_positions=lambda *args, **kwargs: {"AAPL": {"qty": 2}},
        place_order=place,
        market_tz=ET,
        log=logging.getLogger("test"),
        get_market_schedule=schedule,
    ), open_calls, place_calls


def _run_exit(trades, hooks, *, mode="algo", positions=None):
    return execute_trade_batch(
        trades=trades,
        decision_digest="test",
        args=SimpleNamespace(dry_run=False),
        mode=mode,
        positions=positions if positions is not None else {"AAPL": {"qty": 2}},
        rm_portfolio_state=None,
        hooks=hooks,
    )


def test_non_proxy_ticker_flagged_cash_reserve_is_still_risk_validated():
    calls = []

    def validate(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(approved=False, reason="exposure cap", rule="max_position_size")

    hooks, _, place_calls = _exit_hooks()
    hooks = hooks.__class__(**{**hooks.__dict__, "rm_validate_trade": validate})
    results, submitted, _ = execute_trade_batch(
        trades=[{
            "ticker": "AAPL", "action": "buy", "qty": 10, "order_type": "market",
            "strategy": "cash_reserve", "cash_reserve": True, "client_order_id": "cash-aapl",
        }],
        decision_digest="test",
        args=SimpleNamespace(dry_run=False),
        mode="algo",
        positions={},
        rm_portfolio_state={"equity": 100000},
        hooks=hooks,
    )

    assert len(calls) == 1 and calls[0]["ticker"] == "AAPL"
    assert place_calls == [] and submitted == 0
    assert results[0]["status"] == "blocked"
    assert results[0]["rule"] == "max_position_size"


@pytest.mark.parametrize(
    ("action", "qty", "positions", "equity", "expect_bypass"),
    [
        ("buy", 10, {}, 100000, True),
        ("buy", 900, {}, 100000, False),
        ("buy", 10, {}, 0, False),
        ("sell", 5, {"SGOV": {"qty": 10}}, 100000, True),
        ("sell", 50, {"SGOV": {"qty": 10}}, 100000, False),
        ("cover", 5, {"SGOV": {"qty": -10}}, 100000, False),
    ],
)
def test_cash_reserve_bypass_is_limited_to_proxy_buys_and_sells_within_cap(
    action, qty, positions, equity, expect_bypass,
):
    from portfolio.config import CASH_RESERVE

    proxy = CASH_RESERVE["proxy_symbol"]
    calls = []
    hooks, _, place_calls = _exit_hooks()
    hooks = hooks.__class__(**{
        **hooks.__dict__,
        "rm_validate_trade": lambda **kwargs: calls.append(kwargs) or SimpleNamespace(approved=True, reason="", rule=""),
    })

    execute_trade_batch(
        trades=[{
            "ticker": proxy, "action": action, "qty": qty, "order_type": "market",
            "strategy": "cash_reserve", "cash_reserve": True, "client_order_id": f"cash-{action}",
        }],
        decision_digest="test",
        args=SimpleNamespace(dry_run=False),
        mode="algo",
        positions=positions,
        rm_portfolio_state={"equity": equity},
        hooks=hooks,
    )

    assert (calls == []) is expect_bypass


def test_leveraged_etf_gate_is_removed_from_risk_policy_and_config():
    from execution import risk_policy
    from portfolio.config import get_mode_config

    assert not hasattr(risk_policy, "LEVERAGED_ETFS")
    assert "LEVERAGED_ETFS" not in risk_policy.__all__
    assert "allow_leveraged_etfs" not in get_mode_config("algo")["risk"]


def test_algo_broker_has_no_direct_order_submission_bypass():
    from broker.algo import AlgoAlpaca

    assert not hasattr(AlgoAlpaca, "submit_market_order")
    assert not hasattr(AlgoAlpaca, "submit_limit_order")


def test_protective_cover_legs_of_a_short_are_not_reserved_as_pending_buys():
    from execution import risk_account

    positions = {"AAPL": {"qty": -100.0, "current_price": 100.0}}
    cover_legs = [
        {"symbol": "AAPL", "side": "buy", "type": "stop", "order_class": "bracket", "qty": "100",
         "stop_price": "105", "position_intent": "buy_to_close", "status": "held"},
        {"symbol": "AAPL", "side": "buy", "type": "limit", "order_class": "bracket", "qty": "100",
         "limit_price": "90", "position_intent": "buy_to_close", "status": "held"},
    ]

    assert risk_account._pending_buy_exposure(cover_legs, positions) == {}


def test_cover_leg_without_intent_is_skipped_only_for_a_short_position():
    from execution import risk_account

    leg = {"symbol": "AAPL", "side": "buy", "type": "stop", "parent_order_id": "entry-1",
           "qty": "100", "stop_price": "105"}

    assert risk_account._pending_buy_exposure([leg], {"AAPL": {"qty": -100.0}}) == {}
    assert risk_account._pending_buy_exposure([leg], {"AAPL": {"qty": 0.0}}) == {"AAPL": pytest.approx(100 * 105 * 1.02)}


def test_opening_buy_is_still_reserved_and_short_cover_legs_do_not_reduce_available_cash(monkeypatch):
    from execution import risk_account

    opening_buy = {"symbol": "MSFT", "side": "buy", "type": "limit", "qty": "10",
                   "limit_price": "100", "position_intent": "buy_to_open", "status": "new"}
    assert risk_account._pending_buy_exposure([opening_buy], {}) == {"MSFT": pytest.approx(10 * 100 * 1.02)}

    short_position = {
        "symbol": "AAPL", "qty": "-100", "market_value": "-10000",
        "current_price": "100", "avg_entry_price": "100",
    }
    cover_legs = [
        {"symbol": "AAPL", "side": "buy", "type": "stop", "order_class": "bracket", "qty": "100",
         "stop_price": "105", "position_intent": "buy_to_close", "status": "held"},
        {"symbol": "AAPL", "side": "buy", "type": "limit", "order_class": "bracket", "qty": "100",
         "limit_price": "90", "position_intent": "buy_to_close", "status": "held"},
    ]

    def api(endpoint, mode):
        assert endpoint == "positions"
        return [short_position]

    state = risk_account.get_portfolio_state(
        mode="algo",
        api=api,
        get_universe_maps=lambda mode: ({"AAPL": "technology"}, {}, set(), {}),
        verify_account=lambda mode: (None, {"id": "acct", "equity": "100000", "cash": "30000", "last_equity": "100000"}),
        get_algo_orders_since=lambda since, mode: [],
        get_algo_fill_activities=lambda since, mode: [],
        get_open_orders=lambda mode: cover_legs,
        fetch_easy_to_borrow_symbols=lambda mode: {"AAPL"},
        find_weekly_equity_baseline=lambda **kwargs: None,
        estimate_weekly_pnl=lambda *args, **kwargs: 0.0,
    )

    assert state["available_cash"] == pytest.approx(30000)
    assert state["pending_buy_value_by_ticker"] == {}
