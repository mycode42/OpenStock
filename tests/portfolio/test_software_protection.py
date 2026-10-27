"""Software protection: trigger parity per session, DB recovery, heartbeat alarm, migration."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from portfolio import daemon
from portfolio.protection import migration
from portfolio.protection.software import (
    build_protection_rows,
    heartbeat_alarm,
    protection_row_from_state,
)
from portfolio.state_store import AlgoStateStore, empty_portfolio
from strategies.runner import signal_generation
from strategies.runner.sizing import _update_trailing_stop


NOW = datetime(2026, 10, 8, 14, 0, tzinfo=timezone.utc)
STRATEGY = "swing_trend_following"
RISK = {
    "stop_loss_pct_by_strategy": {STRATEGY: 0.05},
    "trailing_stop_pct_by_strategy": {STRATEGY: 0.04},
    "trailing_activation_pct_by_strategy": {STRATEGY: 0.03},
}
SESSIONS = ("regular", "premarket", "after_hours", "overnight")


def _owned_portfolio(stop=95.0, target=110.0, side_qty=10) -> dict:
    portfolio = empty_portfolio()
    portfolio["position_owners"] = {"AAPL": STRATEGY}
    portfolio["strategy_states"] = {STRATEGY: {"AAPL": {
        "phase": "open", "strategy": STRATEGY, "ticker": "AAPL", "owner": STRATEGY,
        "entry_order_id": "entry-1", "entry_client_order_id": "rule-1", "entry_source": "coordinator",
        "stop_price": stop, "take_profit": target,
    }}}
    return portfolio


def _store(tmp_path, portfolio=None) -> AlgoStateStore:
    store = AlgoStateStore(tmp_path / "algo_state.sqlite3")
    store.load()
    store.save("REGULAR", portfolio or _owned_portfolio())
    return store


def _context(session: str, *, bid: float, ask: float | None = None, age_seconds: float = 5.0, state: dict | None = None, qty=10):
    quote_time = NOW - timedelta(seconds=age_seconds)
    stored = state if state is not None else _owned_portfolio()["strategy_states"]
    return SimpleNamespace(
        session_name=session,
        latest_quotes={"AAPL": {"timestamp": quote_time, "price": bid, "bid": bid, "ask": bid if ask is None else ask}},
        quote_check_time=NOW,
        strategy_states=stored,
        risk_config=RISK,
        rank_score={STRATEGY: 1.0},
        lifecycle=None,
    ), {"avg_entry_price": 100.0}, qty


def _evaluate(session: str, *, bid: float, state: dict, qty=10, age_seconds: float = 5.0, ask: float | None = None):
    context, position, quantity = _context(session, bid=bid, ask=ask, age_seconds=age_seconds, state={STRATEGY: {"AAPL": state}}, qty=qty)
    return signal_generation._protective_exit_option(context, "AAPL", STRATEGY, position, quantity, [])


def _long_state(**overrides) -> dict:
    state = {"phase": "open", "stop_price": 95.0, "take_profit": 110.0, "entry_order_id": "entry-1"}
    state.update(overrides)
    return state


@pytest.mark.parametrize("session", SESSIONS)
def test_long_stop_triggers_on_the_bid_at_the_persisted_level(session):
    assert _evaluate(session, bid=95.0, state=_long_state())[0]["reason"] == "software stop-loss reached"
    assert _evaluate(session, bid=95.01, state=_long_state()) == []


@pytest.mark.parametrize("session", SESSIONS)
def test_long_target_triggers_at_or_above_the_persisted_target(session):
    assert _evaluate(session, bid=110.0, state=_long_state())[0]["reason"] == "software take-profit reached"
    assert _evaluate(session, bid=109.99, state=_long_state()) == []


@pytest.mark.parametrize("session", SESSIONS)
def test_long_trailing_stop_ratchets_after_activation_and_then_triggers(session):
    state = _long_state()
    assert _evaluate(session, bid=102.0, state=state) == []
    assert state["stop_price"] == 95.0  # 2% favourable move is below the 3% activation

    assert _evaluate(session, bid=106.0, state=state) == []
    assert state["stop_price"] == 101.76  # 106 * (1 - 0.04)

    assert _evaluate(session, bid=101.8, state=state) == []
    assert _evaluate(session, bid=101.7, state=state)[0]["reason"] == "software stop-loss reached"


def test_short_levels_use_the_ask_and_mirror_the_long_rules():
    short_state = {"phase": "open", "stop_price": 105.0, "take_profit": 90.0, "entry_order_id": "entry-1"}
    assert _evaluate("regular", bid=0, ask=105.0, state=short_state, qty=-10)[0]["reason"] == "software short stop-loss reached"
    assert _evaluate("regular", bid=0, ask=104.99, state=short_state, qty=-10) == []
    assert _evaluate("regular", bid=0, ask=90.0, state=short_state, qty=-10)[0]["reason"] == "software short take-profit reached"


def test_stale_quotes_are_not_evaluated_and_the_limit_is_session_specific():
    # 120 seconds old: beyond the regular limit (90s) but within the extended limit (300s).
    assert _evaluate("regular", bid=94.0, state=_long_state(), age_seconds=120) == []
    assert _evaluate("premarket", bid=94.0, state=_long_state(), age_seconds=120)[0]["reason"] == "software stop-loss reached"


def test_trailing_helper_matches_the_row_builder_inputs():
    state = {"stop_price": 95.0}
    stop = _update_trailing_stop(state, strategy=STRATEGY, qty=10, entry_price=100.0, current_price=106.0, mode_risk=RISK)
    assert stop == 101.76


def test_protection_row_from_state_carries_levels_and_trailing_parameters():
    row = protection_row_from_state(
        "AAPL", _long_state(trailing_peak_price=106.0), strategy=STRATEGY,
        broker_quantity="10", broker_entry_price="100", risk=RISK, now=NOW,
    )
    assert row["side"] == "long"
    assert row["quantity"] == "10"
    assert row["initial_stop"] == "95.0"
    assert row["target"] == "110.0"
    assert row["hard_stop_pct"] == "0.05"
    assert row["trailing_stop_pct"] == "0.04"
    assert row["trailing_activation_pct"] == "0.03"
    assert row["best_price"] == "106.0"


@pytest.mark.parametrize("state", [
    {"phase": "open", "stop_price": None, "take_profit": 110.0},
    {"phase": "open", "stop_price": 95.0, "take_profit": None},
    {"phase": "open", "stop_price": 115.0, "take_profit": 110.0},
])
def test_incomplete_or_inverted_levels_never_become_a_protection_row(state):
    assert protection_row_from_state("AAPL", state, strategy=STRATEGY, broker_quantity="10", broker_entry_price="100", risk=RISK, now=NOW) is None


def test_protection_rows_survive_a_restart_and_keep_exit_markers(tmp_path):
    store = _store(tmp_path)
    rows, gaps = build_protection_rows(
        _owned_portfolio(), [{"symbol": "AAPL", "qty": "10", "avg_entry_price": "100"}], risk=RISK, now=NOW,
    )
    assert gaps == []
    store.sync_protection(rows)
    store.set_exit_marker("AAPL", "rule-exit-1", "order-9", NOW.isoformat())
    store.write_heartbeat(NOW, "regular", 4242)

    restarted = AlgoStateStore(tmp_path / "algo_state.sqlite3")
    recovered = restarted.read_protection()["AAPL"]
    assert recovered["initial_stop"] == "95.0"
    assert recovered["target"] == "110.0"
    assert recovered["phase"] == "exit_pending"
    assert recovered["exit_client_order_id"] == "rule-exit-1"
    assert restarted.read_heartbeat()["scanned_at"] == NOW.isoformat()

    restarted.sync_protection(rows)
    assert restarted.read_protection()["AAPL"]["exit_client_order_id"] == "rule-exit-1"

    restarted.clear_exit_marker("AAPL")
    assert restarted.read_protection()["AAPL"]["exit_client_order_id"] is None


def test_sync_drops_rows_for_positions_that_are_no_longer_held(tmp_path):
    store = _store(tmp_path)
    rows, _ = build_protection_rows(_owned_portfolio(), [{"symbol": "AAPL", "qty": "10", "avg_entry_price": "100"}], risk=RISK, now=NOW)
    store.sync_protection(rows)
    store.sync_protection([])
    assert store.read_protection() == {}


def test_existing_database_gains_protection_tables_on_first_use(tmp_path):
    store = _store(tmp_path)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("DROP TABLE position_protection")
        connection.execute("DROP TABLE daemon_heartbeat")
    assert store.read_protection() == {}
    assert store.read_heartbeat() is None
    with sqlite3.connect(store.db_path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"position_protection", "daemon_heartbeat", "strategy_state", "coordinator"} <= tables


def test_heartbeat_alarm_fires_only_when_positions_are_open_and_the_scan_is_stale():
    scan_interval = 60
    fresh = {"scanned_at": (NOW - timedelta(seconds=100)).isoformat()}
    stale = {"scanned_at": (NOW - timedelta(seconds=181)).isoformat()}

    assert heartbeat_alarm(heartbeat=stale, open_position_count=0, now=NOW, scan_interval_seconds=scan_interval) is None
    assert heartbeat_alarm(heartbeat=fresh, open_position_count=2, now=NOW, scan_interval_seconds=scan_interval) is None
    alarm = heartbeat_alarm(heartbeat=stale, open_position_count=2, now=NOW, scan_interval_seconds=scan_interval)
    assert alarm["kind"] == "daemon_heartbeat_stale"
    assert alarm["limit_seconds"] == 180
    assert heartbeat_alarm(heartbeat=None, open_position_count=1, now=NOW, scan_interval_seconds=scan_interval)["kind"] == "daemon_heartbeat_missing"


class FakeMigrationBroker:
    """Stands in for AlgoAlpaca: one bracket on AAPL with a stop and a target leg."""

    def __init__(self, *, stop="95", target="110", leg_qty="10", fail_cancel=()):
        self.parent = {"id": "entry-1", "symbol": "AAPL", "order_class": "bracket", "legs": [
            {"id": "stop-1", "type": "stop", "side": "sell", "qty": leg_qty, "stop_price": stop, "status": "new"},
            {"id": "target-1", "type": "limit", "side": "sell", "qty": leg_qty, "limit_price": target, "status": "new"},
        ]}
        self.open = [
            {"id": "stop-1", "symbol": "AAPL", "parent_order_id": "entry-1", "side": "sell", "status": "new"},
            {"id": "target-1", "symbol": "AAPL", "parent_order_id": "entry-1", "side": "sell", "status": "new"},
        ]
        self.fail_cancel = set(fail_cancel)
        self.cancelled: list[str] = []

    def positions(self):
        return [{"symbol": "AAPL", "qty": "10", "avg_entry_price": "100"}]

    def orders(self, status="open", *, nested=False, symbols=None):
        return list(self.open)

    def order(self, order_id, *, nested=False):
        assert order_id == "entry-1" and nested is True
        return self.parent

    def cancel_order(self, order_id):
        if order_id in self.fail_cancel:
            raise RuntimeError("cancel refused")
        self.cancelled.append(order_id)


MIGRATION_NOW = NOW


def test_migration_dry_run_writes_nothing_and_cancels_nothing(tmp_path):
    store = _store(tmp_path)
    broker = FakeMigrationBroker()

    plans = migration.plan_migration(store, broker, now=MIGRATION_NOW, risk=RISK)

    assert plans[0]["status"] == "ready"
    assert plans[0]["leg_ids"] == ["stop-1", "target-1"]
    assert store.read_protection() == {}
    assert broker.cancelled == []


def test_migration_apply_writes_and_verifies_the_row_before_cancelling_legs(tmp_path):
    store = _store(tmp_path)
    broker = FakeMigrationBroker()
    plans = migration.plan_migration(store, broker, now=MIGRATION_NOW, risk=RISK)

    results = migration.apply_migration(store, broker, plans)

    assert results == [{
        "ticker": "AAPL", "status": "migrated", "stop": "95.0", "target": "110.0",
        "legs_cancelled": ["stop-1", "target-1"], "cancel_errors": [],
    }]
    assert store.read_protection()["AAPL"]["initial_stop"] == "95.0"
    assert broker.cancelled == ["stop-1", "target-1"]


def test_migration_leaves_legs_in_place_when_levels_disagree(tmp_path):
    store = _store(tmp_path)
    broker = FakeMigrationBroker(stop="94")

    plans = migration.plan_migration(store, broker, now=MIGRATION_NOW, risk=RISK)
    results = migration.apply_migration(store, broker, plans)

    assert plans[0]["status"] == "skipped"
    assert "differs from the persisted levels" in plans[0]["reason"]
    assert results[0]["status"] == "skipped"
    assert store.read_protection() == {}
    assert broker.cancelled == []


def test_migration_leaves_legs_in_place_when_the_row_write_fails(tmp_path, monkeypatch):
    store = _store(tmp_path)
    broker = FakeMigrationBroker()
    plans = migration.plan_migration(store, broker, now=MIGRATION_NOW, risk=RISK)

    def failing_write(row):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "upsert_protection", failing_write)
    results = migration.apply_migration(store, broker, plans)

    assert results[0]["status"] == "failed"
    assert results[0]["stage"] == "write_verify"
    assert results[0]["legs_left_in_place"] == ["stop-1", "target-1"]
    assert broker.cancelled == []


def test_migration_reports_partial_cancellation_after_the_row_is_safely_written(tmp_path):
    store = _store(tmp_path)
    broker = FakeMigrationBroker(fail_cancel={"target-1"})
    plans = migration.plan_migration(store, broker, now=MIGRATION_NOW, risk=RISK)

    results = migration.apply_migration(store, broker, plans)

    assert results[0]["status"] == "partial"
    assert results[0]["legs_cancelled"] == ["stop-1"]
    assert "target-1" in results[0]["cancel_errors"][0]
    assert store.read_protection()["AAPL"]["initial_stop"] == "95.0"


def test_migration_skips_positions_with_unexpected_open_orders(tmp_path):
    store = _store(tmp_path)
    broker = FakeMigrationBroker()
    broker.open.append({"id": "manual-1", "symbol": "AAPL", "side": "sell", "status": "new"})

    plans = migration.plan_migration(store, broker, now=MIGRATION_NOW, risk=RISK)

    assert plans[0]["status"] == "skipped"
    assert "not an exit leg" in plans[0]["reason"]


def test_sync_software_protection_writes_rows_and_releases_stale_exit_markers(tmp_path):
    store = _store(tmp_path)
    store.write_heartbeat(NOW, "regular", 1)
    portfolio = _owned_portfolio()
    store.set_exit_marker("AAPL", "gone-order", "order-1", NOW.isoformat())

    class Broker:
        def positions(self):
            return [{"symbol": "AAPL", "qty": "10", "avg_entry_price": "100"}]

        def orders(self, status):
            return []

    gaps = daemon._sync_software_protection(store, portfolio, Broker(), now=NOW)

    assert gaps == []
    row = store.read_protection()["AAPL"]
    assert row["exit_client_order_id"] is None
    assert row["initial_stop"] == "95.0"


def test_entries_paused_never_block_exits_in_the_scan(monkeypatch):
    portfolio = _owned_portfolio()
    portfolio["startup_reconciliation"] = {
        "status": "degraded", "entries_paused": True,
        "entry_pause_reasons": ["Algo holdings lack a verified stop: MSFT"],
        "protection_gaps": ["MSFT"],
    }
    portfolio["universe"] = {"tickers": ["AAPL", "MSFT"], "overnight_tradable_tickers": []}
    scan_report = {
        "market_regime": {"regime": "trending_up", "active": [STRATEGY]},
        "signals": [], "scan_errors": [], "entries_paused": False, "entry_pause_reasons": [],
        "unowned_positions": [], "data_status": {"historical_bars": "ok"},
        "trades": [
            {"ticker": "AAPL", "strategy": STRATEGY, "action": "sell", "client_order_id": "sell-aapl"},
            {"ticker": "MSFT", "strategy": STRATEGY, "action": "buy", "client_order_id": "buy-msft"},
        ],
    }
    submitted: list[list[dict]] = []
    monkeypatch.setattr(daemon, "scan_strategy_portfolio", lambda **_kwargs: scan_report)
    monkeypatch.setattr(
        daemon, "execute_strategy_trades",
        lambda trades, **_kwargs: submitted.append(list(trades)) or {"results": [
            {"ticker": "AAPL", "action": "sell", "client_order_id": "sell-aapl", "status": "submitted", "order_id": "x"},
        ]},
    )
    window = SimpleNamespace(
        name="regular", trade_date="2026-10-08", extended_hours=False,
        bar_feed="sip", quote_feed="sip", regular_open=None, regular_close=None,
    )

    daemon._scan_strategy_portfolio(portfolio, window=window)

    assert [trade["action"] for trade in submitted[0]] == ["sell"]
    assert portfolio["entries_paused"] is True
