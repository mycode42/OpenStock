from __future__ import annotations

import json
from contextlib import closing

import pytest

from reporting import performance, snapshot_store


def _snapshot(day: str, equity: float) -> dict:
    return {
        "date": day, "equity": equity, "account_id": "acct-1",
        "trading_mode": "algo", "account_name": "Algo",
    }


@pytest.fixture
def snapshot_dir(tmp_path, monkeypatch):
    database = tmp_path / "algo_state.sqlite3"
    monkeypatch.setattr(snapshot_store, "database_path", lambda: database)
    with closing(snapshot_store.connect()) as connection:
        for index, day in enumerate(["2026-10-26", "2026-10-27"]):
            snapshot_store.store_close(connection, _snapshot(day, 100_000.0 + index), trading_mode="algo", account_id="acct-1")
    monkeypatch.setattr(performance, "load_trade_journal", lambda mode, account_id=None: [])
    monkeypatch.setattr(performance, "reconcile_trade_orders", lambda trades, mode: (trades, {}))
    return database


def test_writes_feed_atomically_to_given_path(tmp_path, snapshot_dir, monkeypatch):
    monkeypatch.setattr(
        performance, "get_account_fill_report",
        lambda mode, trades: {"realized_fills": [], "realized_pnl_complete": True},
    )
    target = tmp_path / "research" / "live-drift-feed.json"

    result = performance.write_live_drift_feed_for_account(target, mode="algo", account_id="acct-1")

    payload = json.loads(target.read_text())
    assert payload["schema"]
    assert payload["sessions"] == 2
    assert payload["strategies"] == {}
    assert result == {"sessions": 2, "realized_pnl_complete": True}
    assert sorted(p.name for p in target.parent.iterdir()) == ["live-drift-feed.json"]


def test_broker_failure_leaves_no_partial_feed(tmp_path, snapshot_dir, monkeypatch):
    def broken(mode, trades):
        raise ConnectionError("fills endpoint down")

    monkeypatch.setattr(performance, "get_account_fill_report", broken)
    target = tmp_path / "live-drift-feed.json"

    with pytest.raises(ConnectionError):
        performance.write_live_drift_feed_for_account(target, mode="algo", account_id="acct-1")

    assert not target.exists()
    assert list(tmp_path.iterdir()) == [snapshot_dir]
