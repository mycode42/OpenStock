"""Regression checks for account routing and submission versus fill reporting."""

import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from broker import accounts
from reporting import journal as trade_journal
from reporting import performance as tracker
from reporting import monitor as monitor_report


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_account_verification_fails_closed_on_wrong_account_or_block(monkeypatch):
    route = accounts.AlpacaAccount("Algo", "algo-id", "algo-key", "algo-secret")
    monkeypatch.setattr(accounts, "_ACCOUNT", route)

    monkeypatch.setattr(accounts.requests, "get", lambda *args, **kwargs: FakeResponse({
        "id": "other-id", "status": "ACTIVE", "trading_blocked": False,
    }))
    with pytest.raises(ValueError, match="different Alpaca account"):
        accounts.verify_account_for_mode("algo")

    monkeypatch.setattr(accounts.requests, "get", lambda *args, **kwargs: FakeResponse({
        "id": "algo-id", "status": "ACTIVE", "trading_blocked": True,
    }))
    with pytest.raises(ValueError, match="trading blocked"):
        accounts.verify_account_for_mode("algo")

    monkeypatch.setattr(accounts.requests, "get", lambda *args, **kwargs: FakeResponse({
        "id": "algo-id", "status": "ACTIVE", "trading_blocked": False,
    }))
    selected, _ = accounts.verify_account_for_mode("algo")
    assert selected.api_key == "algo-key"


def test_submitted_order_is_not_counted_as_filled_until_broker_confirms(monkeypatch, tmp_path):
    journal = tmp_path / "journal.jsonl"
    monkeypatch.setattr(trade_journal, "JOURNAL_PATH", journal)
    logged = trade_journal.append_trades({
        "timestamp": "2026-10-08T10:00:00-04:00",
        "trading_mode": "algo",
        "mode": "live",
        "results": [{
            "ticker": "AAPL", "action": "buy", "qty": 2,
            "status": "pending_new", "success": True,
            "order_id": "order-1", "strategy": "momentum",
        }],
    })
    assert logged == 1
    row = json.loads(journal.read_text().strip())
    assert row["status"] == "submitted"
    assert row["broker_status"] == "pending_new"
    assert trade_journal.compute_stats([row])["filled"] == 0

    route = accounts.AlpacaAccount("Algo", "algo-id", "algo-key", "algo-secret")
    monkeypatch.setattr(tracker, "verify_account_for_mode", lambda mode: (route, {"id": "algo-id"}))
    monkeypatch.setattr(tracker.requests, "get", lambda *args, **kwargs: FakeResponse([{
        "id": "order-1", "status": "filled", "filled_qty": "2", "filled_avg_price": "100.25",
    }]))
    enriched, health = tracker.reconcile_trade_orders([row], "algo")
    assert health["matched"] == 1
    assert trade_journal.compute_stats(enriched)["filled"] == 1
    assert tracker.compute_strategy_stats(enriched)["momentum"]["filled"] == 1


def test_replayed_client_id_enriches_order_without_double_counting(monkeypatch, tmp_path):
    monkeypatch.setattr(trade_journal, "JOURNAL_PATH", tmp_path / "journal.jsonl")
    first = {"trading_mode": "algo", "mode": "live", "results": [{
        "ticker": "AAPL", "action": "buy", "qty": 2, "status": "submitted",
        "client_order_id": "stable-1", "strategy": "momentum",
    }]}
    second = {"trading_mode": "algo", "mode": "live", "results": [{
        "ticker": "AAPL", "action": "buy", "qty": 2, "status": "submitted",
        "client_order_id": "stable-1", "order_id": "broker-1", "strategy": "momentum",
    }]}
    assert trade_journal.append_trades(first) == 1
    assert trade_journal.append_trades(second) == 1
    assert trade_journal.append_trades(second) == 0
    rows = trade_journal.read_journal()
    assert len(rows) == 2
    merged = trade_journal.dedupe_journal_entries(rows)
    assert len(merged) == 1
    assert merged[0]["order_id"] == "broker-1"
    assert trade_journal.compute_stats(rows)["submitted"] == 1


def test_account_reset_keeps_old_journal_and_snapshots_out_of_current_report(monkeypatch, tmp_path):
    monkeypatch.setattr(trade_journal, "JOURNAL_PATH", tmp_path / "journal.jsonl")
    monkeypatch.setattr(tracker, "TRADE_JOURNAL_PATH", tmp_path / "journal.jsonl")
    monkeypatch.setattr(tracker, "_snapshot_dir", lambda mode: tmp_path / "snapshots")
    (tmp_path / "snapshots").mkdir()
    for account_id, equity in (("old-id", 99_000), ("new-id", 1_000_000)):
        payload = {
            "trading_mode": "algo", "account_id": account_id, "mode": "live",
            "results": [{
                "ticker": "AAPL", "action": "buy", "qty": 1,
                "status": "submitted", "client_order_id": "same-id",
                "order_id": f"{account_id}-order", "strategy": "momentum",
            }],
        }
        assert trade_journal.append_trades(payload) == 1
        (tmp_path / "snapshots" / f"{account_id}.json").write_text(json.dumps({
            "date": "2026-10-08", "trading_mode": "algo", "account_id": account_id,
            "snapshot_kind": "close", "equity": equity,
        }))

    assert len(trade_journal.dedupe_journal_entries(trade_journal.read_journal())) == 2
    current_trades = tracker.load_trade_journal(mode="algo", account_id="new-id")
    current_snapshots = tracker.load_snapshots(mode="algo", account_id="new-id")
    assert len(current_trades) == len(current_snapshots) == 1
    assert current_trades[0]["order_id"] == "new-id-order"
    assert current_snapshots[0]["equity"] == 1_000_000


def test_close_snapshot_requires_broker_close_and_skips_holidays(monkeypatch, tmp_path):
    et = ZoneInfo("America/New_York")
    frozen = datetime(2026, 10, 8, 16, 5, tzinfo=et)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen.astimezone(tz) if tz else frozen

    monkeypatch.setattr(tracker, "datetime", FrozenDateTime)
    monkeypatch.setattr(tracker, "_snapshot_dir", lambda mode: tmp_path)
    monkeypatch.setattr(tracker, "get_portfolio", lambda mode: {
        "account_name": "Algo", "account_id": "paper-algo", "equity": 100000, "cash": 50000,
        "invested": 50000, "position_count": 1, "daily_pnl": 0,
        "daily_pnl_pct": 0,
    })
    monkeypatch.setattr(tracker.yf, "Ticker", lambda symbol: SimpleNamespace(
        history=lambda **kwargs: SimpleNamespace(empty=True)
    ))
    monkeypatch.setattr(tracker, "get_market_schedule", lambda **kwargs: SimpleNamespace(
        closes_at=datetime(2026, 10, 8, 16, 0, tzinfo=et)
    ))
    saved = tracker.take_snapshot(mode="algo", snapshot_kind="close", force=True)
    assert saved["snapshot_kind"] == "close"
    assert saved["account_id"] == "paper-algo"
    assert json.loads((tmp_path / "2026-10-08.json").read_text())["snapshot_kind"] == "close"

    monkeypatch.setattr(tracker, "get_market_schedule", lambda **kwargs: None)
    skipped = tracker.take_snapshot(mode="algo", snapshot_kind="close", force=True)
    assert skipped["skipped"] is True


def test_algo_monitor_excludes_previous_account_journal_rows():
    rows = [
        {"trading_mode": "algo", "account_id": "old-id", "timestamp": "2026-10-08T08:00:00", "order_id": "old-order"},
        {"trading_mode": "algo", "account_id": "new-id", "timestamp": "2026-10-08T09:00:00", "order_id": "new-order"},
    ]
    updates, statuses = monitor_report._trade_updates(
        rows, None, len(rows),
        [{"id": "old-order", "status": "filled"}, {"id": "new-order", "status": "new"}],
        {}, today="2026-10-08", account_id="new-id",
    )
    assert [row["order_id"] for row in updates] == ["new-order"]
    assert statuses == {"new-order": "new"}
