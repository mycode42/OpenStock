"""Safety checks for the Algo coordinator's durable ownership boundary."""

from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from portfolio import daemon
from portfolio.reconciliation import reconcile_startup
from portfolio.state_store import AlgoStateStore, StateCorruptionError, empty_portfolio
from strategies.state_machine import ENTRY_PENDING, EXIT_PENDING, OPEN, RuleStrategyStateMachine

def _seed(path: Path, portfolio: dict) -> AlgoStateStore:
    store = AlgoStateStore(path)
    store.load()
    store.save("REGULAR", portfolio)
    return store

def _owned_portfolio() -> dict:
    portfolio = empty_portfolio()
    portfolio["position_owners"] = {"AAPL": "swing_momentum"}
    portfolio["strategy_states"] = {
        "swing_momentum": {
            "AAPL": {
                "phase": OPEN,
                "strategy": "swing_momentum",
                "ticker": "AAPL",
                "owner": "swing_momentum",
                "entry_order_id": "entry-1",
                "entry_client_order_id": "rule-1",
                "entry_source": "coordinator",
                "stop_price": 95.0,
                "take_profit": 110.0,
            }
        }
    }
    return portfolio

def test_read_normalizes_legacy_strategy_ids_before_provenance_validation(tmp_path):
    store = AlgoStateStore(tmp_path / "algo_state.sqlite3")
    store.load()
    store.save("REGULAR", _owned_portfolio())

    with sqlite3.connect(store.db_path) as connection:
        state = json.loads(connection.execute(
            "SELECT state_json FROM strategy_state WHERE strategy='swing_momentum' AND ticker='AAPL'",
        ).fetchone()[0])
        state["strategy"] = "momentum"
        state["owner"] = "momentum"
        connection.execute(
            "UPDATE strategy_state SET strategy='momentum', state_json=? "
            "WHERE strategy='swing_momentum' AND ticker='AAPL'",
            (json.dumps(state),),
        )
        connection.execute(
            "UPDATE position_owner SET strategy='momentum' WHERE ticker='AAPL'",
        )
        # Existing provenance is immutable in production. Drop the guard only
        # in this fixture to represent a database written before the ID rename.
        connection.execute("DROP TRIGGER entry_provenance_immutable_update")
        connection.execute(
            "UPDATE entry_provenance SET strategy='momentum' WHERE order_id='entry-1'",
        )

    _, loaded = store.read()

    assert loaded["position_owners"] == {"AAPL": "swing_momentum"}
    assert loaded["strategy_states"]["swing_momentum"]["AAPL"]["owner"] == "swing_momentum"

def test_daily_backup_is_a_verified_copy_and_keeps_newest_copies(tmp_path):
    from datetime import date

    store = AlgoStateStore(tmp_path / "algo_state.sqlite3")
    store.load()
    store.save("REGULAR", _owned_portfolio())
    backups = tmp_path / "backups"

    written = store.backup_daily(backups, date(2026, 10, 26), keep=2)
    assert written == backups / "algo_state-2026-10-26.sqlite3"
    assert store.backup_daily(backups, date(2026, 10, 26), keep=2) is None
    store.backup_daily(backups, date(2026, 10, 27), keep=2)
    store.backup_daily(backups, date(2026, 10, 28), keep=2)

    assert sorted(path.name for path in backups.glob("algo_state-*.sqlite3")) == [
        "algo_state-2026-10-27.sqlite3", "algo_state-2026-10-28.sqlite3",
    ]
    market_state, restored = AlgoStateStore(backups / "algo_state-2026-10-28.sqlite3").read()
    assert market_state == "REGULAR"
    assert restored["strategy_states"] == _owned_portfolio()["strategy_states"]

def test_owner_and_strategy_state_changes_commit_together_and_provenance_is_immutable(tmp_path):
    snapshot = tmp_path / "algo_state.sqlite3"
    store = AlgoStateStore(snapshot)
    store.load()
    original = _owned_portfolio()
    store.save("REGULAR", original)

    conflicting = empty_portfolio()
    conflicting["position_owners"] = {"AAPL": "swing_trend_following"}
    conflicting["strategy_states"] = {
        "swing_trend_following": {
            "AAPL": {
                "phase": OPEN, "strategy": "swing_trend_following", "ticker": "AAPL",
                "owner": "swing_trend_following", "entry_order_id": "entry-1",
            }
        }
    }
    with pytest.raises(StateCorruptionError, match="Conflicting immutable Algo provenance"):
        store.save("REGULAR", conflicting)

    _, unchanged = store.read()
    assert unchanged["position_owners"] == {"AAPL": "swing_momentum"}
    assert unchanged["strategy_states"]["swing_momentum"]["AAPL"]["entry_order_id"] == "entry-1"
    with sqlite3.connect(store.db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM entry_provenance WHERE order_id='entry-1'")

def test_execution_lease_requires_exact_token_and_parent_pid(tmp_path):
    store = AlgoStateStore(tmp_path / "algo_state.sqlite3")
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
        {"trading_mode": "algo", "action": "buy", "ticker": "AVGO", "strategy": "swing_momentum", "order_id": "old", "client_order_id": "client-old", "submission_status": "submitted"},
        {"trading_mode": "algo", "action": "buy", "ticker": "AVGO", "strategy": "swing_trend_following", "order_id": "current", "client_order_id": "client-current", "submission_status": "submitted"},
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

    assert portfolio["position_owners"] == {"AVGO": "swing_trend_following"}
    assert report["recovered_owners"] == ["AVGO"]
    assert report["unowned_positions"] == ["ABNB"]
    assert report["entries_paused"] is True
    state = portfolio["strategy_states"]["swing_trend_following"]["AVGO"]
    assert state["entry_order_id"] == "current"
    assert state["stop_price"] == 369.33
    assert state["take_profit"] == 380.54

def test_capped_broker_history_uses_exact_persisted_entry_or_pauses(tmp_path):
    journal = tmp_path / "trade_journal.jsonl"
    journal.write_text(json.dumps({
        "trading_mode": "algo", "action": "buy", "ticker": "AAPL",
        "strategy": "swing_momentum", "order_id": "entry-1", "client_order_id": "rule-1",
        "status": "pending_new",
    }))
    recent_orders = [
        {"id": f"other-{index}", "symbol": "MSFT", "filled_qty": "0"}
        for index in range(500)
    ]
    exact = _order("entry-1", "AAPL", "buy", 10, "2026-09-01T14:00:00Z", 100)
    exact["client_order_id"] = "rule-1"
    portfolio = _owned_portfolio()
    portfolio["strategy_states"]["swing_momentum"]["AAPL"]["last_known_qty"] = 10
    portfolio["strategy_states"]["swing_momentum"]["AAPL"]["stop_price"] = 95

    report = reconcile_startup(
        portfolio,
        positions=[{"symbol": "AAPL", "qty": "10", "avg_entry_price": "100"}],
        all_orders=recent_orders, nested_orders=[], open_orders=[],
        journal_path=journal, exact_entry_orders={"entry-1": exact},
    )
    assert report["status"] == "verified"
    assert portfolio["position_owners"] == {"AAPL": "swing_momentum"}

    missing_exact = _owned_portfolio()
    missing_exact["strategy_states"]["swing_momentum"]["AAPL"]["last_known_qty"] = 10
    report = reconcile_startup(
        missing_exact,
        positions=[{"symbol": "AAPL", "qty": "10", "avg_entry_price": "100"}],
        all_orders=recent_orders, nested_orders=[], open_orders=[], journal_path=journal,
    )
    assert report["status"] == "degraded"
    assert report["entries_paused"] is True
    assert missing_exact["position_owners"] == {"AAPL": "swing_momentum"}

def test_pending_coordinator_entry_recovers_by_persisted_client_id_if_journal_write_was_lost(tmp_path):
    journal = tmp_path / "trade_journal.jsonl"
    journal.write_text("")
    portfolio = empty_portfolio()
    portfolio["position_owners"] = {"AAPL": "swing_momentum"}
    portfolio["strategy_states"] = {"swing_momentum": {"AAPL": {
        "phase": ENTRY_PENDING, "strategy": "swing_momentum", "ticker": "AAPL",
        "owner": "swing_momentum", "client_order_id": "client-entry-1",
        "entry_client_order_id": "client-entry-1", "stop_price": 95.0, "take_profit": 110.0,
    }}}
    entry = _order("entry-1", "AAPL", "buy", 10, "2026-10-08T14:00:00Z", 100)

    report = reconcile_startup(
        portfolio,
        positions=[{"symbol": "AAPL", "qty": "10", "avg_entry_price": "100"}],
        all_orders=[entry], nested_orders=[], open_orders=[], journal_path=journal,
    )

    assert report["status"] == "verified"
    state = portfolio["strategy_states"]["swing_momentum"]["AAPL"]
    assert state["phase"] == OPEN
    assert state["entry_order_id"] == "entry-1"
    assert state["entry_source"] == "persisted_pending_client_id"

def test_reconciliation_keeps_software_levels_when_broker_legs_are_gone(tmp_path):
    journal = tmp_path / "trade_journal.jsonl"
    journal.write_text(json.dumps({
        "trading_mode": "algo", "action": "buy", "ticker": "AAPL",
        "strategy": "swing_momentum", "order_id": "entry-1", "client_order_id": "rule-1",
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

    # Protection is the persisted software stop and target, not broker legs.
    assert report["status"] == "verified"
    assert report["unprotected_positions"] == []
    assert portfolio["strategy_states"]["swing_momentum"]["AAPL"]["stop_price"] == 95.0

@pytest.mark.parametrize("submission_status,client_id", [
    ("failed", "client-entry-1"),
    ("submitted", "someone-elses-client-id"),
])
def test_failed_or_mismatched_journal_buy_cannot_claim_broker_position(tmp_path, submission_status, client_id):
    journal = tmp_path / "trade_journal.jsonl"
    journal.write_text(json.dumps({
        "trading_mode": "algo", "action": "buy", "ticker": "AAPL",
        "strategy": "swing_momentum", "order_id": "entry-1", "client_order_id": client_id,
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
    trade = {"ticker": "AAPL", "strategy": "swing_momentum", "action": "buy", "signal_bar_time": "bar-1", "client_order_id": "client-1"}
    machine.prepare(trade)
    machine.resolve(trade, {"status": "submitted", "success": True, "order_id": "order-1"})
    machine.reconcile({"AAPL": {"qty": 10}}, [])

    assert not machine.allows("swing_momentum", "AAPL", "sell", "bar-1")
    assert machine.allows_protective_exit("swing_momentum", "AAPL")

def test_uncertain_broker_response_keeps_entry_pending():
    states = {}
    owners = {}
    machine = RuleStrategyStateMachine(states, owners)
    trade = {"ticker": "AAPL", "strategy": "swing_momentum", "action": "buy", "client_order_id": "client-1"}
    machine.prepare(trade)
    machine.resolve(trade, {"status": "failed", "error": "Submission outcome unknown"})

    assert states["swing_momentum"]["AAPL"]["phase"] == ENTRY_PENDING
    assert owners == {"AAPL": "swing_momentum"}

def test_daemon_degraded_bar_scan_pauses_buys_but_keeps_owned_exit(monkeypatch):
    portfolio = _owned_portfolio()
    portfolio["startup_reconciliation"] = {"status": "verified", "entries_paused": False}
    portfolio["universe"] = {"tickers": ["AAPL", "MSFT"], "overnight_tradable_tickers": ["AAPL", "MSFT"]}
    captured = {}

    def fake_scan(**kwargs):
        captured["feeds"] = (kwargs["data_feed"], kwargs["quote_feed"])
        return {
            "market_regime": {"regime": "trending_up", "active": ["swing_momentum"]},
            "signals": [], "scan_errors": ["BOATS historical bars unavailable"],
            "entries_paused": True, "entry_pause_reasons": ["bar fetch failed"],
            "unowned_positions": [], "data_status": {"historical_bars": "degraded"},
            "trades": [
                {"ticker": "MSFT", "strategy": "swing_momentum", "action": "buy", "client_order_id": "buy-msft"},
                {"ticker": "AAPL", "strategy": "swing_momentum", "action": "sell", "client_order_id": "sell-aapl"},
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
        regular_open=None, regular_close=None,
    )
    daemon._scan_strategy_portfolio(portfolio, window=window)

    assert captured["feeds"] == ("boats", "overnight")
    assert [(trade["ticker"], trade["action"]) for trade in captured["trades"]] == [("AAPL", "sell")]
    assert portfolio["entries_paused"] is True
    assert portfolio["last_scan_status"] == "degraded"
    assert portfolio["strategy_states"]["swing_momentum"]["AAPL"]["phase"] == EXIT_PENDING

def test_daemon_scan_returns_scan_report_after_order_submission(monkeypatch):
    portfolio = _owned_portfolio()
    portfolio["startup_reconciliation"] = {"status": "verified", "entries_paused": False}
    scan_report = {
        "universe": {"tickers": ["AAPL", "MSFT"]},
        "market_regime": {"regime": "trending_up", "active": ["swing_momentum"]},
        "signals": [], "scan_errors": [], "entries_paused": False,
        "entry_pause_reasons": [], "unowned_positions": [],
        "data_status": {"historical_bars": "ok"},
        "trades": [{
            "ticker": "MSFT", "strategy": "swing_momentum", "action": "buy",
            "qty": 1, "client_order_id": "buy-msft",
        }],
    }
    monkeypatch.setattr(daemon, "scan_strategy_portfolio", lambda **_kwargs: scan_report)
    monkeypatch.setattr(
        daemon, "execute_strategy_trades",
        lambda trades, **_kwargs: {"results": [{
            "ticker": "MSFT", "action": "buy", "status": "submitted",
            "success": True, "order_id": "entry-msft",
        }]},
    )
    window = SimpleNamespace(
        name="regular", trade_date="2026-10-08", extended_hours=False,
        bar_feed="sip", quote_feed="sip", regular_open=None, regular_close=None,
    )

    returned = daemon._scan_strategy_portfolio(portfolio, window=window)

    assert returned is scan_report
    assert returned["universe"]["tickers"] == ["AAPL", "MSFT"]
    assert portfolio["last_execution"]["results"][0]["order_id"] == "entry-msft"

def test_failed_algo_exit_stays_visible_and_pauses_new_entries_until_broker_proves_order(monkeypatch, tmp_path):
    portfolio = _owned_portfolio()
    portfolio["startup_reconciliation"] = {"status": "verified", "entries_paused": False}
    portfolio["universe"] = {"tickers": ["AAPL", "MSFT"], "overnight_tradable_tickers": []}
    submitted = []

    def fake_scan(**_kwargs):
        return {
            "market_regime": {"regime": "trending_up", "active": ["swing_momentum"]},
            "signals": [], "scan_errors": [], "entries_paused": False,
            "entry_pause_reasons": [], "unowned_positions": [],
            "data_status": {"historical_bars": "ok"},
            "trades": [
                {"ticker": "AAPL", "strategy": "swing_momentum", "action": "sell", "client_order_id": "sell-aapl"},
                {"ticker": "MSFT", "strategy": "swing_momentum", "action": "buy", "client_order_id": "buy-msft"},
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
        regular_open=None, regular_close=None,
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
        "trading_mode": "algo", "action": "buy", "ticker": "AAPL", "strategy": "swing_momentum",
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

def test_daemon_first_cycle_reconciles_and_clears_execution_lease(tmp_path, monkeypatch):
    snapshot = tmp_path / "algo_state.sqlite3"
    legacy_portfolio = empty_portfolio()
    legacy_portfolio["universe"] = {
        "tickers": ["NVDA", "SPCX", "TSLA"],
        "selection_date": "2026-10-08",
        "overnight_tradable_tickers": ["NVDA", "SPCX"],
    }
    _seed(snapshot, legacy_portfolio)
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

        def orders_after(self, after, *, status="all", limit=500):
            return []

    window = SimpleNamespace(
        name="regular", trade_date="2026-10-08", extended_hours=False,
        bar_feed="sip", quote_feed="sip",
        regular_open=None, regular_close=None,
    )
    monkeypatch.setattr(daemon, "_state_path", lambda: snapshot)
    # A process may start while host monotonic uptime is still below the scan
    # interval. The initial scan must not depend on machine uptime.
    monkeypatch.setattr(daemon.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(daemon, "get_trading_window", lambda now, calendar: window)
    monkeypatch.setattr(daemon, "load_algo_universe", lambda: {"tickers": ["AAPL"]})
    monkeypatch.setattr(daemon, "scan_strategy_portfolio", lambda **kwargs: {
        "market_regime": {"regime": "trending_up", "active": ["swing_momentum"]},
        "signals": [], "trades": [], "scan_errors": [], "entries_paused": False,
        "entry_pause_reasons": [], "unowned_positions": [],
        "data_status": {"historical_bars": "ok"},
    })

    daemon.run_loop(max_cycles=1, broker_client=FakeBroker())

    store = AlgoStateStore(snapshot)
    market_state, portfolio = store.read()
    assert market_state == "REGULAR"
    assert portfolio["account_id"] == "paper-algo"
    assert portfolio["universe"]["tickers"] == ["AAPL"]
    assert portfolio["startup_reconciliation"]["status"] == "verified"
    assert portfolio["entries_paused"] is False
    assert portfolio["last_scan_status"] == "ok"
    assert store.db_path.exists()
    assert not store.verify_coordinator_token("anything", 1)

def test_daemon_replaces_a_same_day_universe_from_the_old_fixed_source(tmp_path, monkeypatch):
    snapshot = tmp_path / "algo_state.sqlite3"
    seeded = empty_portfolio()
    seeded["account_id"] = "paper-algo"
    seeded["universe"] = {
        "tickers": ["NVDA", "AAPL"], "selection_date": "2026-10-08", "source": "fixed_catalog",
    }
    _seed(snapshot, seeded)
    (tmp_path / "trade_journal.jsonl").write_text("")
    loaded = []

    class FakeBroker:
        def calendar(self, *args):
            return []

        def account(self):
            return {"id": "paper-algo", "status": "ACTIVE"}

        def positions(self):
            return []

        def orders(self, status, *, nested=False):
            return []

        def orders_after(self, after, *, status="all", limit=500):
            return []

    window = SimpleNamespace(
        name="regular", trade_date="2026-10-08", extended_hours=False,
        bar_feed="sip", quote_feed="sip", regular_open=None, regular_close=None,
    )

    def fake_universe():
        loaded.append(True)
        return {"tickers": ["XLE"], "source": "eligible_universe", "selection_date": "2026-10-08"}

    monkeypatch.setattr(daemon, "_state_path", lambda: snapshot)
    monkeypatch.setattr(daemon.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(daemon, "get_trading_window", lambda now, calendar: window)
    monkeypatch.setattr(daemon, "load_algo_universe", fake_universe)
    monkeypatch.setattr(daemon, "scan_strategy_portfolio", lambda **kwargs: {
        "market_regime": {"regime": "range_bound", "active": []},
        "signals": [], "trades": [], "scan_errors": [], "entries_paused": False,
        "entry_pause_reasons": [], "unowned_positions": [], "data_status": {"historical_bars": "ok"},
    })

    daemon.run_loop(max_cycles=1, broker_client=FakeBroker())

    assert loaded == [True]
    _, persisted = AlgoStateStore(snapshot).read()
    assert persisted["universe"]["source"] == "eligible_universe"


def test_daemon_refuses_state_from_a_different_paper_account(tmp_path, monkeypatch):
    snapshot = tmp_path / "algo_state.sqlite3"
    portfolio = empty_portfolio()
    portfolio["account_id"] = "old-account"
    _seed(snapshot, portfolio)

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


def _exit_scan_window():
    return SimpleNamespace(
        name="regular", trade_date="2026-10-08", extended_hours=False,
        bar_feed="sip", quote_feed="sip", regular_open=None, regular_close=None,
    )


def _exit_scan_report():
    return {
        "market_regime": {"regime": "trending_up", "active": ["swing_momentum"]},
        "signals": [], "scan_errors": [], "entries_paused": False,
        "entry_pause_reasons": [], "unowned_positions": [],
        "data_status": {"historical_bars": "ok"},
        "trades": [
            {"ticker": "AAPL", "strategy": "swing_momentum", "action": "sell", "client_order_id": "sell-aapl"},
            {"ticker": "MSFT", "strategy": "swing_momentum", "action": "buy", "client_order_id": "buy-msft"},
        ],
    }


def test_retryable_exit_refusal_keeps_entries_open_and_persists_retry_intent(monkeypatch):
    portfolio = _owned_portfolio()
    portfolio["startup_reconciliation"] = {"status": "verified", "entries_paused": False}
    portfolio["universe"] = {"tickers": ["AAPL", "MSFT"], "overnight_tradable_tickers": []}
    monkeypatch.setattr(daemon, "scan_strategy_portfolio", lambda **_kwargs: _exit_scan_report())
    monkeypatch.setattr(
        daemon, "execute_strategy_trades",
        lambda trades, **_kwargs: {"results": [{
            "ticker": "AAPL", "action": "sell", "client_order_id": "sell-aapl",
            "status": "failed", "reason": "Protective broker orders can only be cancelled in regular market hours",
            "safe_to_retry": True,
        }]},
    )

    daemon._scan_strategy_portfolio(portfolio, window=_exit_scan_window())

    assert portfolio["entries_paused"] is False
    assert not any("exit submission" in reason for reason in portfolio["entry_pause_reasons"])
    assert not (portfolio.get("execution_incident") or {}).get("events")
    intent = portfolio["strategy_states"]["swing_momentum"]["AAPL"]["pending_exit_intent"]
    assert intent["retryable"] is True
    assert intent["status"] == "retryable_pre_submission"


def test_failed_exit_submission_opens_incident_and_pauses_entries(monkeypatch):
    portfolio = _owned_portfolio()
    portfolio["startup_reconciliation"] = {"status": "verified", "entries_paused": False, "unprotected_positions": []}
    portfolio["universe"] = {"tickers": ["AAPL", "MSFT"], "overnight_tradable_tickers": []}
    monkeypatch.setattr(daemon, "scan_strategy_portfolio", lambda **_kwargs: _exit_scan_report())
    monkeypatch.setattr(
        daemon, "execute_strategy_trades",
        lambda trades, **_kwargs: {"results": [{
            "ticker": "AAPL", "action": "sell", "client_order_id": "sell-aapl",
            "status": "failed", "reason": "broker rejected the exit",
        }]},
    )

    daemon._scan_strategy_portfolio(portfolio, window=_exit_scan_window())

    assert portfolio["startup_reconciliation"]["unprotected_positions"] == []
    assert portfolio["entries_paused"] is True
    assert portfolio["execution_incident"]["events"]["exit_submission:AAPL"]["client_order_id"] == "sell-aapl"

def test_status_command_reports_bound_state_without_name_error(monkeypatch, tmp_path, capsys):
    from portfolio import daemon as daemon_module

    state = {"runtime_mode": "algo", "universe": {"tickers": ["AAPL", "MSFT"]}}
    monkeypatch.setattr(daemon_module.AlgoStateStore, "read", lambda self: ("REGULAR", state))
    monkeypatch.setattr(
        daemon_module.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )

    daemon_module._status(tmp_path / "algo_state.sqlite3")

    printed = json.loads(capsys.readouterr().out)
    assert printed["market_state"] == "REGULAR"
    assert printed["service_active"] is True
    assert printed["universe"] == ["AAPL", "MSFT"]
    assert printed["portfolio"] == state
    assert printed["state_error"] is None


def test_status_command_reports_unreadable_state_instead_of_crashing(monkeypatch, tmp_path, capsys):
    from portfolio import daemon as daemon_module
    from portfolio.state_store import StateStoreError

    def unreadable(self):
        raise StateStoreError("state is corrupt")

    monkeypatch.setattr(daemon_module.AlgoStateStore, "read", unreadable)
    monkeypatch.setattr(
        daemon_module.subprocess, "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("no systemctl")),
    )

    with pytest.raises(SystemExit) as exit_info:
        daemon_module._status(tmp_path / "algo_state.sqlite3")

    assert exit_info.value.code == 2
    printed = json.loads(capsys.readouterr().out)
    assert printed["market_state"] == "STATE_ERROR"
    assert printed["state_error"] == "state is corrupt"
    assert printed["service_active"] is None
    assert printed["universe"] == []
