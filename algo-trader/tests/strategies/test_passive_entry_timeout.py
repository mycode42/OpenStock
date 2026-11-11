"""A passive entry that rests unfilled is cancelled after the timeout, and the ticker is released to retry at the
marketable band. Pending entries no longer pause the whole book."""

from datetime import datetime, timedelta, timezone

from portfolio.daemon import expire_stale_passive_entries
from strategies.state_machine import (
    ENTRY_PENDING, FLAT, PASSIVE_ENTRY_TIMEOUT_SECONDS, RuleStrategyStateMachine,
)

NOW = datetime(2026, 11, 11, 15, 0, tzinfo=timezone.utc)


def _pending(strategy="swing_momentum", ticker="LLY", age_seconds=1200, action="buy", order_id="o1",
             reference=100.0, pricing="passive", bar="2026-11-11T14:00:00+00:00"):
    stamp = (NOW - timedelta(seconds=age_seconds)).isoformat()
    return {
        strategy: {ticker: {
            "phase": ENTRY_PENDING, "owner": strategy, "entry_action": action, "pending_action": action,
            "order_id": order_id, "entry_order_id": order_id, "updated_at": stamp, "entry_client_order_id": "c1",
            "entry_reference_price": reference, "entry_pricing": pricing, "last_signal_bar": bar,
        }},
    }


def _portfolio(states, owners=None):
    return {"strategy_states": states, "position_owners": dict(owners or {"LLY": "swing_momentum"})}


class FakeBroker:
    def __init__(self, orders, after_cancel_status="canceled", positions=(), cancel_error=None,
                 bid=100.0, ask=100.02):
        self._quote = {"bid": bid, "ask": ask}
        self._orders = list(orders)
        self._status = after_cancel_status
        self._positions = list(positions)
        self._cancel_error = cancel_error
        self.cancelled = []

    def orders(self, status="open", **_):
        return list(self._orders)

    def positions(self):
        return list(self._positions)

    def latest_quotes(self, symbols, feed=None):
        return {symbol: dict(self._quote) for symbol in symbols}

    def cancel_order(self, order_id):
        if self._cancel_error:
            raise self._cancel_error
        self.cancelled.append(order_id)

    def order(self, order_id):
        return {"id": order_id, "status": self._status}


def test_an_unfilled_passive_entry_is_stale_only_after_the_timeout():
    states = _pending(age_seconds=PASSIVE_ENTRY_TIMEOUT_SECONDS - 60)
    machine = RuleStrategyStateMachine(states, {"LLY": "swing_momentum"})
    open_orders = [{"id": "o1", "status": "new", "symbol": "LLY"}]
    assert machine.stale_entry_orders(open_orders, NOW) == []
    states = _pending(age_seconds=PASSIVE_ENTRY_TIMEOUT_SECONDS + 60)
    machine = RuleStrategyStateMachine(states, {"LLY": "swing_momentum"})
    stale = machine.stale_entry_orders(open_orders, NOW)
    assert [(item["strategy"], item["ticker"]) for item in stale] == [("swing_momentum", "LLY")]


def test_partly_filled_entries_and_exits_are_not_timed_out():
    machine = RuleStrategyStateMachine(_pending(), {"LLY": "swing_momentum"})
    assert machine.stale_entry_orders([{"id": "o1", "status": "partially_filled"}], NOW) == []
    exits = RuleStrategyStateMachine(_pending(action="sell"), {"LLY": "swing_momentum"})
    assert exits.stale_entry_orders([{"id": "o1", "status": "new"}], NOW) == []


def test_a_released_ticker_is_flat_without_cooldown_and_chases_next_time():
    states = _pending()
    machine = RuleStrategyStateMachine(states, {"LLY": "swing_momentum"})
    machine.release_timed_out_entry("swing_momentum", "LLY", NOW, chase=True)
    state = states["swing_momentum"]["LLY"]
    assert state["phase"] == FLAT
    assert state["cooldown_until"] is None
    assert state["chase_after_timeout"] is True
    assert "LLY" not in machine.position_owners


def test_expiry_cancels_and_releases_when_the_broker_confirms_no_shares():
    portfolio = _portfolio(_pending(age_seconds=1200))
    broker = FakeBroker([{"id": "o1", "status": "new", "symbol": "LLY"}])
    results = expire_stale_passive_entries(portfolio, broker, now=NOW)
    assert broker.cancelled == ["o1"]
    assert results[0]["status"] == "released"
    assert results[0]["retry_at_band"] is True
    assert portfolio["strategy_states"]["swing_momentum"]["LLY"]["phase"] == FLAT


def test_retry_at_the_band_only_while_the_market_is_inside_it():
    # Band is 10 bp: a buy on a $100 signal may retry up to $100.10.
    inside = _portfolio(_pending(age_seconds=1200, reference=100.0))
    results = expire_stale_passive_entries(inside, FakeBroker([{"id": "o1", "status": "new"}], ask=100.05), now=NOW)
    assert results[0]["retry_at_band"] is True
    outside = _portfolio(_pending(age_seconds=1200, reference=100.0))
    results = expire_stale_passive_entries(outside, FakeBroker([{"id": "o1", "status": "new"}], ask=100.50), now=NOW)
    assert results[0]["retry_at_band"] is False
    assert outside["strategy_states"]["swing_momentum"]["LLY"]["chase_after_timeout"] is False


def test_a_retry_that_also_times_out_is_not_retried_again():
    chased = _portfolio(_pending(age_seconds=1200, pricing="chase_after_timeout"))
    results = expire_stale_passive_entries(chased, FakeBroker([{"id": "o1", "status": "new"}]), now=NOW)
    assert results[0]["retry_at_band"] is False


def test_a_timed_out_signal_bar_is_not_reentered_unless_the_retry_is_allowed():
    states = _pending(age_seconds=1200, bar="2026-11-11T14:00:00+00:00")
    machine = RuleStrategyStateMachine(states, {"LLY": "swing_momentum"})
    machine.release_timed_out_entry("swing_momentum", "LLY", NOW, chase=False)
    assert not machine.allows("swing_momentum", "LLY", "buy", "2026-11-11T14:00:00+00:00")
    assert machine.allows("swing_momentum", "LLY", "buy", "2026-11-11T14:05:00+00:00")
    retry = RuleStrategyStateMachine(_pending(age_seconds=1200, bar="2026-11-11T14:00:00+00:00"), {"LLY": "swing_momentum"})
    retry.release_timed_out_entry("swing_momentum", "LLY", NOW, chase=True)
    assert retry.allows("swing_momentum", "LLY", "buy", "2026-11-11T14:00:00+00:00")


def test_expiry_keeps_the_entry_when_shares_arrived_before_the_cancel_took_effect():
    portfolio = _portfolio(_pending(age_seconds=1200))
    broker = FakeBroker([{"id": "o1", "status": "new", "symbol": "LLY"}],
                        positions=[{"symbol": "LLY", "qty": "2"}])
    results = expire_stale_passive_entries(portfolio, broker, now=NOW)
    assert results[0]["status"] == "held_after_cancel"
    assert portfolio["strategy_states"]["swing_momentum"]["LLY"]["phase"] == ENTRY_PENDING


def test_a_failed_cancel_leaves_the_entry_for_reconciliation():
    portfolio = _portfolio(_pending(age_seconds=1200))
    broker = FakeBroker([{"id": "o1", "status": "new", "symbol": "LLY"}], cancel_error=RuntimeError("503"))
    results = expire_stale_passive_entries(portfolio, broker, now=NOW)
    assert results[0]["status"] == "cancel_error"
    assert portfolio["strategy_states"]["swing_momentum"]["LLY"]["phase"] == ENTRY_PENDING
