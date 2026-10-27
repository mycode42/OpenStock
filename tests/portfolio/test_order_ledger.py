"""The broker order ledger must page past Alpaca's 500-order limit and fail closed."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from portfolio.order_ledger import BrokerOrderLedger, OrderLedgerError


def _order(index: int, *, status: str = "filled") -> dict:
    submitted = (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=index)).isoformat().replace("+00:00", "Z")
    return {
        "id": f"order-{index:05d}", "symbol": "AAPL", "side": "buy", "status": status,
        "submitted_at": submitted,
        "filled_at": submitted if status == "filled" else None,
        "filled_qty": "1" if status == "filled" else "0",
    }


class FakeBroker:
    def __init__(self, orders: list[dict]) -> None:
        self.orders_by_id = {order["id"]: order for order in orders}
        self.calls: list[str | None] = []

    def orders_after(self, after, *, status="all", limit=500):
        self.calls.append(after)
        rows = sorted(self.orders_by_id.values(), key=lambda order: order["submitted_at"])
        if after:
            cutoff = datetime.fromisoformat(after.replace("Z", "+00:00"))
            rows = [
                order for order in rows
                if datetime.fromisoformat(order["submitted_at"].replace("Z", "+00:00")) > cutoff
            ]
        return rows[:limit]


def test_full_sync_pages_past_the_500_order_limit(tmp_path: Path):
    broker = FakeBroker([_order(index) for index in range(1203)])
    ledger = BrokerOrderLedger(tmp_path / "state.sqlite3")
    assert not ledger.history_complete("paper")

    ledger.sync(broker, account_id="paper", today=date(2026, 10, 27), open_orders=[])

    assert ledger.history_complete("paper")
    assert [order["id"] for order in ledger.orders("paper")] == sorted(broker.orders_by_id)
    assert broker.calls[0] is None


def test_incremental_sync_updates_recent_orders_from_an_overlapping_window(tmp_path: Path):
    broker = FakeBroker([_order(index) for index in range(10)])
    broker.orders_by_id["order-00009"] = _order(9, status="new")
    ledger = BrokerOrderLedger(tmp_path / "state.sqlite3")
    ledger.sync(broker, account_id="paper", today=date(2026, 10, 27), open_orders=[])
    assert ledger.orders("paper")[-1]["status"] == "new"

    broker.orders_by_id["order-00009"] = _order(9, status="filled")
    broker.orders_by_id["order-00010"] = _order(10)
    broker.calls.clear()
    ledger.sync(broker, account_id="paper", today=date(2026, 10, 27), open_orders=[])

    assert len(broker.calls) == 1 and broker.calls[0] is not None
    rows = ledger.orders("paper")
    assert rows[-2]["status"] == "filled"
    assert rows[-1]["id"] == "order-00010"


def test_new_session_day_repeats_a_full_sync(tmp_path: Path):
    broker = FakeBroker([_order(index) for index in range(3)])
    ledger = BrokerOrderLedger(tmp_path / "state.sqlite3")
    ledger.sync(broker, account_id="paper", today=date(2026, 10, 27), open_orders=[])
    broker.calls.clear()

    ledger.sync(broker, account_id="paper", today=date(2026, 10, 28), open_orders=[])

    assert broker.calls[0] is None


def test_failed_sync_rolls_back_and_leaves_history_incomplete(tmp_path: Path):
    class FailingBroker(FakeBroker):
        def orders_after(self, after, *, status="all", limit=500):
            if after is None:
                return [_order(index) for index in range(500)]
            raise ConnectionError("broker unreachable")

    ledger = BrokerOrderLedger(tmp_path / "state.sqlite3")
    with pytest.raises(ConnectionError):
        ledger.sync(FailingBroker([]), account_id="paper", today=date(2026, 10, 27), open_orders=[])

    assert not ledger.history_complete("paper")
    assert ledger.orders("paper") == []


def test_non_advancing_pagination_fails_closed(tmp_path: Path):
    class StuckBroker(FakeBroker):
        def orders_after(self, after, *, status="all", limit=500):
            return [_order(index) for index in range(500)]

    ledger = BrokerOrderLedger(tmp_path / "state.sqlite3")
    with pytest.raises(OrderLedgerError, match="did not advance"):
        ledger.sync(StuckBroker([]), account_id="paper", today=date(2026, 10, 27), open_orders=[])
    assert not ledger.history_complete("paper")


def test_protection_decision_from_an_earlier_entry_is_not_applied_to_a_new_one():
    from portfolio.protection.evidence import current_recovery

    old = {"entry_order_id": "old-entry", "client_order_id": "algo-oco-CRM-old", "order_id": "x"}
    assert current_recovery({"entry_order_id": "new-entry", "protection_recovery": old}) is None
    assert current_recovery({"entry_order_id": "old-entry", "protection_recovery": old}) == old
    assert current_recovery({"entry_order_id": "new-entry", "protection_recovery": {"client_order_id": None}}) is None
