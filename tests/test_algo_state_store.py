"""Safety checks for the Algo coordinator's durable ownership boundary."""

from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.algo import daemon
from src.algo.reconciliation import reconcile_startup
from src.algo.state_store import AlgoStateStore, StateCorruptionError, empty_portfolio
from src.strategies.state_machine import ENTRY_PENDING, EXIT_PENDING, OPEN, RuleStrategyStateMachine

def _legacy(path: Path, portfolio: dict) -> bytes:
    original = json.dumps({"market_state": "REGULAR", "strategy_portfolio": portfolio}).encode()
    path.write_bytes(original)
    return original

def _owned_portfolio() -> dict:
    portfolio = empty_portfolio()
    portfolio["position_owners"] = {"AAPL": "momentum"}
    portfolio["strategy_states"] = {
        "momentum": {
            "AAPL": {
                "phase": OPEN,
                "strategy": "momentum",
                "ticker": "AAPL",
                "owner": "momentum",
                "entry_order_id": "entry-1",
                "entry_client_order_id": "rule-1",
                "entry_source": "coordinator",
                "stop_price": 95.0,
                "take_profit": 110.0,
            }
        }
    }
    return portfolio

def test_sqlite_migration_preserves_legacy_snapshot_and_all_strategy_states(tmp_path):
    snapshot = tmp_path / "algo_state.json"
    portfolio = _owned_portfolio()
    original = _legacy(snapshot, portfolio)

    store = AlgoStateStore(snapshot)
    market_state, loaded = store.load()

    assert market_state == "REGULAR"
    assert loaded["position_owners"] == portfolio["position_owners"]
    assert loaded["strategy_states"] == portfolio["strategy_states"]
    assert loaded["entries_paused"] is True
    assert snapshot.read_bytes() == original
    assert store.db_path.is_file()
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM entry_provenance").fetchone()[0] == 1

def test_corrupt_legacy_state_fails_closed_without_creating_sqlite(tmp_path):
    snapshot = tmp_path / "algo_state.json"
    snapshot.write_text("{not valid json")
    store = AlgoStateStore(snapshot)

    with pytest.raises(StateCorruptionError, match="Cannot read existing Algo JSON state"):
        store.load()
    assert not store.db_path.exists()

def test_owner_and_strategy_state_changes_commit_together_and_provenance_is_immutable(tmp_path):
    snapshot = tmp_path / "algo_state.json"
    store = AlgoStateStore(snapshot)
    store.load()
    original = _owned_portfolio()
    store.save("REGULAR", original)

    conflicting = empty_portfolio()
    conflicting["position_owners"] = {"AAPL": "trend_following"}
    conflicting["strategy_states"] = {
        "trend_following": {
            "AAPL": {
                "phase": OPEN, "strategy": "trend_following", "ticker": "AAPL",
                "owner": "trend_following", "entry_order_id": "entry-1",
            }
        }
    }
    with pytest.raises(StateCorruptionError, match="Conflicting immutable Algo provenance"):
        store.save("REGULAR", conflicting)

    _, unchanged = store.read()
    assert unchanged["position_owners"] == {"AAPL": "momentum"}
    assert unchanged["strategy_states"]["momentum"]["AAPL"]["entry_order_id"] == "entry-1"
    with sqlite3.connect(store.db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM entry_provenance WHERE order_id='entry-1'")

def test_execution_lease_requires_exact_token_and_parent_pid(tmp_path):
    store = AlgoStateStore(tmp_path / "algo_state.json")
    store.load()
    assert not store.verify_coordinator_token("token", 111)

    store.activate_coordinator("token", 111)
    assert store.verify_coordinator_token("token", 111)
    assert not store.verify_coordinator_token("wrong", 111)
    assert not store.verify_coordinator_token("token", 112)
    store.clear_coordinator("wrong", 111)
    assert store.verify_coordinator_token("token", 111)
    store.clear_coordinator("token", 111)
    assert not store.verify_coordinator_token("token", 111)

def _order(order_id, symbol, side, qty, when, price):
    return {
        "id": order_id, "symbol": symbol, "side": side,
        "filled_qty": str(qty), "filled_at": when,
        "filled_avg_price": str(price), "status": "filled",
        "client_order_id": f"client-{order_id}",
    }

def test_startup_recovers_only_the_current_flat_to_open_episode_and_bracket(tmp_path):
    journal = tmp_path / "trade_journal.jsonl"
    journal.write_text("\n".join(json.dumps(row) for row in [
        {"trading_mode": "algo", "action": "buy", "ticker": "AVGO", "strategy": "momentum", "order_id": "old", "client_order_id": "client-old", "submission_status": "submitted"},
        {"trading_mode": "algo", "action": "buy", "ticker": "AVGO", "strategy": "trend_following", "order_id": "current", "client_order_id": "client-current", "submission_status": "submitted"},
    ]))
    orders = [
        _order("old", "AVGO", "buy", 13, "2026-10-07T14:00:00Z", 370),
        _order("sold", "AVGO", "sell", 13, "2026-10-07T15:00:00Z", 371),
        _order("current", "AVGO", "buy", 13, "2026-10-07T16:00:00Z", 373.24),
        _order("unknown", "ABNB", "buy", 5, "2026-10-07T17:00:00Z", 100),
    ]
    nested = [{
        "id": "current", "order_class": "bracket",
        "legs": [
            {"side": "sell", "type": "stop", "status": "held", "qty": "13", "stop_price": "369.33"},
            {"side": "sell", "type": "limit", "status": "new", "qty": "13", "limit_price": "380.54"},
        ],
    }]
    portfolio = empty_portfolio()
    report = reconcile_startup(
        portfolio,
        positions=[
            {"symbol": "AVGO", "qty": "13", "avg_entry_price": "373.24"},
            {"symbol": "ABNB", "qty": "5", "avg_entry_price": "100"},
        ],
        all_orders=orders, nested_orders=nested, open_orders=[], journal_path=journal,
    )

    assert portfolio["position_owners"] == {"AVGO": "trend_following"}
    assert report["recovered_owners"] == ["AVGO"]
    assert report["unowned_positions"] == ["ABNB"]
    assert report["entries_paused"] is True
    state = portfolio["strategy_states"]["trend_following"]["AVGO"]
    assert state["entry_order_id"] == "current"
    assert state["stop_price"] == 369.33
    assert state["take_profit"] == 380.54

def test_capped_broker_history_uses_exact_persisted_entry_or_pauses(tmp_path):
    journal = tmp_path / "trade_journal.jsonl"
    journal.write_text(json.dumps({
        "trading_mode": "algo", "action": "buy", "ticker": "AAPL",
        "strategy": "momentum", "order_id": "entry-1", "client_order_id": "rule-1",
        "status": "pending_new",
    }))
    recent_orders = [
        {"id": f"other-{index}", "symbol": "MSFT", "filled_qty": "0"}
        for index in range(500)
    ]
    exact = _order("entry-1", "AAPL", "buy", 10, "2026-09-01T14:00:00Z", 100)
    exact["client_order_id"] = "rule-1"
    portfolio = _owned_portfolio()
    portfolio["strategy_states"]["momentum"]["AAPL"]["last_known_qty"] = 10
    portfolio["strategy_states"]["momentum"]["AAPL"]["stop_price"] = 95

    report = reconcile_startup(
        portfolio,
        positions=[{"symbol": "AAPL", "qty": "10", "avg_entry_price": "100"}],
        all_orders=recent_orders, nested_orders=[], open_orders=[],
        journal_path=journal, exact_entry_orders={"entry-1": exact},
    )
    assert report["status"] == "verified"
    assert portfolio["position_owners"] == {"AAPL": "momentum"}

    missing_exact = _owned_portfolio()
    missing_exact["strategy_states"]["momentum"]["AAPL"]["last_known_qty"] = 10
    report = reconcile_startup(
        missing_exact,
        positions=[{"symbol": "AAPL", "qty": "10", "avg_entry_price": "100"}],
        all_orders=recent_orders, nested_orders=[], open_orders=[], journal_path=journal,
    )
    assert report["status"] == "degraded"
    assert report["entries_paused"] is True
    assert missing_exact["position_owners"] == {"AAPL": "momentum"}

def test_pending_coordinator_entry_recovers_by_persisted_client_id_if_journal_write_was_lost(tmp_path):
    journal = tmp_path / "trade_journal.jsonl"
    journal.write_text("")
    portfolio = empty_portfolio()
    portfolio["position_owners"] = {"AAPL": "momentum"}
    portfolio["strategy_states"] = {"momentum": {"AAPL": {
        "phase": ENTRY_PENDING, "strategy": "momentum", "ticker": "AAPL",
        "owner": "momentum", "client_order_id": "client-entry-1",
        "entry_client_order_id": "client-entry-1", "stop_price": 95.0,
    }}}
    entry = _order("entry-1", "AAPL", "buy", 10, "2026-10-08T14:00:00Z", 100)

    report = reconcile_startup(
        portfolio,
        positions=[{"symbol": "AAPL", "qty": "10", "avg_entry_price": "100"}],
        all_orders=[entry], nested_orders=[], open_orders=[], journal_path=journal,
    )

    assert report["status"] == "verified"
    state = portfolio["strategy_states"]["momentum"]["AAPL"]
    assert state["phase"] == OPEN
    assert state["entry_order_id"] == "entry-1"
    assert state["entry_source"] == "persisted_pending_client_id"

def test_reconciliation_does_not_treat_old_software_stop_as_live_bracket_stop(tmp_path):
    journal = tmp_path / "trade_journal.jsonl"
    journal.write_text(json.dumps({
        "trading_mode": "algo", "action": "buy", "ticker": "AAPL",
        "strategy": "momentum", "order_id": "entry-1", "client_order_id": "rule-1",
        "submission_status": "submitted",
    }))
    entry = _order("entry-1", "AAPL", "buy", 10, "2026-10-08T14:00:00Z", 100)
    entry.update({"order_class": "bracket", "client_order_id": "rule-1"})
    portfolio = _owned_portfolio()

    report = reconcile_startup(
        portfolio,
        positions=[{"symbol": "AAPL", "qty": "10", "avg_entry_price": "100"}],
        all_orders=[entry],
        nested_orders=[{"id": "entry-1", "order_class": "bracket", "legs": [
            {"id": "stop-1", "side": "sell", "type": "stop", "status": "canceled",
             "qty": "10", "stop_price": "95"},
            {"id": "target-1", "side": "sell", "type": "limit", "status": "pending_cancel",
             "qty": "10", "limit_price": "110"},
        ]}],
        open_orders=[], journal_path=journal,
    )

    assert report["status"] == "degraded"
    assert report["unprotected_positions"] == ["AAPL"]
    assert portfolio["strategy_states"]["momentum"]["AAPL"]["stop_price"] == 95.0

@pytest.mark.parametrize("submission_status,client_id", [
    ("failed", "client-entry-1"),
    ("submitted", "someone-elses-client-id"),
])
def test_failed_or_mismatched_journal_buy_cannot_claim_broker_position(tmp_path, submission_status, client_id):
    journal = tmp_path / "trade_journal.jsonl"
    journal.write_text(json.dumps({
        "trading_mode": "algo", "action": "buy", "ticker": "AAPL",
        "strategy": "momentum", "order_id": "entry-1", "client_order_id": client_id,
        "submission_status": submission_status, "status": submission_status,
    }))
    entry = _order("entry-1", "AAPL", "buy", 10, "2026-10-08T14:00:00Z", 100)
    portfolio = empty_portfolio()

    report = reconcile_startup(
        portfolio,
        positions=[{"symbol": "AAPL", "qty": "10", "avg_entry_price": "100"}],
        all_orders=[entry], nested_orders=[], open_orders=[], journal_path=journal,
    )

    assert report["entries_paused"] is True
    assert report["unowned_positions"] == ["AAPL"]
    assert portfolio["position_owners"] == {}

def test_protective_exit_uses_fresh_quote_even_on_the_last_signal_bar():
    states = {}
    owners = {}
    machine = RuleStrategyStateMachine(states, owners)
    trade = {"ticker": "AAPL", "strategy": "momentum", "action": "buy", "signal_bar_time": "bar-1", "client_order_id": "client-1"}
    machine.prepare(trade)
    machine.resolve(trade, {"status": "submitted", "success": True, "order_id": "order-1"})
    machine.reconcile({"AAPL": {"qty": 10}}, [])

    assert not machine.allows("momentum", "AAPL", "sell", "bar-1")
    assert machine.allows_protective_exit("momentum", "AAPL")

def test_uncertain_broker_response_keeps_entry_pending():
    states = {}
    owners = {}
    machine = RuleStrategyStateMachine(states, owners)
    trade = {"ticker": "AAPL", "strategy": "momentum", "action": "buy", "client_order_id": "client-1"}
    machine.prepare(trade)
    machine.resolve(trade, {"status": "failed", "error": "Submission outcome unknown"})

    assert states["momentum"]["AAPL"]["phase"] == ENTRY_PENDING
    assert owners == {"AAPL": "momentum"}

def test_daemon_degraded_bar_scan_pauses_buys_but_keeps_owned_exit(monkeypatch):
    portfolio = _owned_portfolio()
    portfolio["startup_reconciliation"] = {"status": "verified", "entries_paused": False}
    portfolio["universe"] = {"tickers": ["AAPL", "MSFT"], "overnight_tradable_tickers": ["AAPL", "MSFT"]}
    captured = {}

    def fake_scan(**kwargs):
        captured["feeds"] = (kwargs["data_feed"], kwargs["quote_feed"])
        return {
            "market_regime": {"regime": "trending_up", "active": ["momentum"]},
            "signals": [], "scan_errors": ["BOATS historical bars unavailable"],
            "entries_paused": True, "entry_pause_reasons": ["bar fetch failed"],
            "unowned_positions": [], "data_status": {"historical_bars": "degraded"},
            "trades": [
                {"ticker": "MSFT", "strategy": "momentum", "action": "buy", "client_order_id": "buy-msft"},
                {"ticker": "AAPL", "strategy": "momentum", "action": "sell", "client_order_id": "sell-aapl"},
            ],
        }

    def fake_execute(trades, **kwargs):
        captured["trades"] = trades
        return {"results": [{"ticker": "AAPL", "action": "sell", "success": True, "status": "submitted", "order_id": "exit-1"}]}

    monkeypatch.setattr(daemon, "scan_strategy_portfolio", fake_scan)
    monkeypatch.setattr(daemon, "execute_strategy_trades", fake_execute)
    window = SimpleNamespace(
        name="overnight", trade_date="2026-10-08", extended_hours=True,
        bar_feed="boats", quote_feed="overnight",
    )
    daemon._scan_strategy_portfolio(portfolio, window=window)

    assert captured["feeds"] == ("boats", "overnight")
    assert [(trade["ticker"], trade["action"]) for trade in captured["trades"]] == [("AAPL", "sell")]
    assert portfolio["entries_paused"] is True
    assert portfolio["last_scan_status"] == "degraded"
    assert portfolio["strategy_states"]["momentum"]["AAPL"]["phase"] == EXIT_PENDING

def test_failed_algo_exit_stays_visible_and_pauses_new_entries_until_broker_proves_order(monkeypatch, tmp_path):
    portfolio = _owned_portfolio()
    portfolio["startup_reconciliation"] = {"status": "verified", "entries_paused": False}
    portfolio["universe"] = {"tickers": ["AAPL", "MSFT"], "overnight_tradable_tickers": []}
    submitted = []

    def fake_scan(**_kwargs):
        return {
            "market_regime": {"regime": "trending_up", "active": ["momentum"]},
            "signals": [], "scan_errors": [], "entries_paused": False,
            "entry_pause_reasons": [], "unowned_positions": [],
            "data_status": {"historical_bars": "ok"},
            "trades": [
                {"ticker": "AAPL", "strategy": "momentum", "action": "sell", "client_order_id": "sell-aapl"},
                {"ticker": "MSFT", "strategy": "momentum", "action": "buy", "client_order_id": "buy-msft"},
            ],
        }

    def fake_execute(trades, **_kwargs):
        submitted.extend(trades)
        return {"results": [{
            "ticker": "AAPL", "action": "sell", "client_order_id": "sell-aapl",
            "status": "failed", "reason": "protective order could not be canceled",
        }]}

    monkeypatch.setattr(daemon, "scan_strategy_portfolio", fake_scan)
    monkeypatch.setattr(daemon, "execute_strategy_trades", fake_execute)
    window = SimpleNamespace(
        name="regular", trade_date="2026-10-08", extended_hours=False,
        bar_feed="sip", quote_feed="sip",
    )
    daemon._scan_strategy_portfolio(portfolio, window=window)

    assert [trade["action"] for trade in submitted] == ["sell"]
    assert portfolio["last_scan_status"] == "degraded"
    assert portfolio["entries_paused"] is True
    assert portfolio["execution_incident"]["status"] == "open"
    assert portfolio["execution_incident"]["events"]["exit_submission:AAPL"]["client_order_id"] == "sell-aapl"

    monkeypatch.setattr(daemon, "scan_strategy_portfolio", lambda **kwargs: {**fake_scan(**kwargs), "trades": []})
    daemon._scan_strategy_portfolio(portfolio, window=window)
    assert portfolio["last_execution"] is None
    assert portfolio["execution_incident"]["status"] == "open"
    assert portfolio["entries_paused"] is True

    journal = tmp_path / "trade_journal.jsonl"
    journal.write_text(json.dumps({
        "trading_mode": "algo", "action": "buy", "ticker": "AAPL", "strategy": "momentum",
        "order_id": "entry-1", "client_order_id": "rule-1", "submission_status": "submitted",
    }))
    entry = _order("entry-1", "AAPL", "buy", 10, "2026-10-08T14:00:00Z", 100)
    entry.update({"order_class": "bracket", "client_order_id": "rule-1"})
    nested = [{"id": "entry-1", "order_class": "bracket", "legs": [
        {"side": "sell", "type": "stop", "status": "held", "qty": "10", "stop_price": "95"},
        {"side": "sell", "type": "limit", "status": "new", "qty": "10", "limit_price": "110"},
    ]}]
    accepted_sell = {"id": "exit-1", "client_order_id": "sell-aapl", "symbol": "AAPL", "side": "sell", "status": "new", "filled_qty": "0"}
    reconcile_startup(
        portfolio, positions=[{"symbol": "AAPL", "qty": "10", "avg_entry_price": "100"}],
        all_orders=[entry, accepted_sell], nested_orders=nested,
        open_orders=[accepted_sell], journal_path=journal,
    )
    assert portfolio["execution_incident"]["status"] == "resolved"

def test_daemon_first_cycle_migrates_reconciles_and_clears_execution_lease(tmp_path, monkeypatch):
    snapshot = tmp_path / "algo_state.json"
    _legacy(snapshot, empty_portfolio())
    (tmp_path / "trade_journal.jsonl").write_text("")

    class FakeBroker:
        def calendar(self, *args):
            return []

        def account(self):
            return {"id": "paper-algo", "status": "ACTIVE"}

        def positions(self):
            return []

        def orders(self, status, *, nested=False):
            return []

    window = SimpleNamespace(
        name="regular", trade_date="2026-10-08", extended_hours=False,
        bar_feed="sip", quote_feed="sip",
    )
    monkeypatch.setattr(daemon, "_state_path", lambda: snapshot)
    # A process may start while host monotonic uptime is still below the scan
    # interval. The initial scan must not depend on machine uptime.
    monkeypatch.setattr(daemon.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(daemon, "get_trading_window", lambda now, calendar: window)
    monkeypatch.setattr(daemon, "select_universe", lambda *args, **kwargs: {"tickers": ["AAPL"], "selected_count": 1, "target_count": 1})
    monkeypatch.setattr(daemon, "scan_strategy_portfolio", lambda **kwargs: {
        "market_regime": {"regime": "trending_up", "active": ["momentum"]},
        "signals": [], "trades": [], "scan_errors": [], "entries_paused": False,
        "entry_pause_reasons": [], "unowned_positions": [],
        "data_status": {"historical_bars": "ok"},
    })

    daemon.run_loop(max_cycles=1, broker_client=FakeBroker())

    store = AlgoStateStore(snapshot)
    market_state, portfolio = store.read()
    assert market_state == "REGULAR"
    assert portfolio["account_id"] == "paper-algo"
    assert portfolio["startup_reconciliation"]["status"] == "verified"
    assert portfolio["entries_paused"] is False
    assert portfolio["last_scan_status"] == "ok"
    assert store.db_path.exists()
    assert not store.verify_coordinator_token("anything", 1)

def test_daemon_refuses_state_from_a_different_paper_account(tmp_path, monkeypatch):
    snapshot = tmp_path / "algo_state.json"
    portfolio = empty_portfolio()
    portfolio["account_id"] = "old-account"
    _legacy(snapshot, portfolio)

    class FakeBroker:
        def calendar(self, *args):
            return []

        def account(self):
            return {"id": "new-account", "status": "ACTIVE"}

        def positions(self):
            pytest.fail("mismatched state reached broker position reconciliation")

        def orders(self, *args, **kwargs):
            pytest.fail("mismatched state reached broker order reconciliation")

    window = SimpleNamespace(name="regular", trade_date="2026-10-08")
    monkeypatch.setattr(daemon, "_state_path", lambda: snapshot)
    monkeypatch.setattr(daemon, "get_trading_window", lambda now, calendar: window)

    daemon.run_loop(max_cycles=1, broker_client=FakeBroker())

    market_state, persisted = AlgoStateStore(snapshot).read()
    assert market_state == "CONFIG_ERROR"
    assert persisted["account_id"] == "old-account"
