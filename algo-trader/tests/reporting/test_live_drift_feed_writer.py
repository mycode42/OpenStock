from __future__ import annotations

import json

import pytest

from reporting import performance


def _snapshot(day: str, equity: float) -> dict:
    return {
        "date": day, "equity": equity, "account_id": "acct-1",
        "trading_mode": "algo", "account_name": "Algo",
    }


@pytest.fixture
def snapshot_dir(tmp_path, monkeypatch):
    root = tmp_path / "snapshots"
    root.mkdir()
    for index, day in enumerate(["2026-10-26", "2026-10-27"]):
        (root / f"{day}.json").write_text(json.dumps(_snapshot(day, 100_000.0 + index)))
    monkeypatch.setattr(performance, "_snapshot_dir", lambda mode: root)
    monkeypatch.setattr(performance, "load_trade_journal", lambda mode, account_id=None: [])
    monkeypatch.setattr(performance, "reconcile_trade_orders", lambda trades, mode: (trades, {}))
    return root


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
