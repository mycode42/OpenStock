"""Health cron decisions for active and closed Algo market sessions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os

import pytest

import portfolio.healthcheck as healthcheck
from portfolio.healthcheck import build_report
from portfolio.state_store import StateStoreError

NOW = datetime(2026, 10, 8, 14, 0, tzinfo=timezone.utc)  # 10:00 ET
CALENDAR = [{"date": "2026-10-08", "open": "09:30", "close": "16:00"}]

class FakeStore:
    def __init__(self, portfolio=None, market_state="REGULAR", error=None, protection=None, heartbeat="default"):
        self.portfolio = portfolio
        self.market_state = market_state
        self.error = error
        self.reads = 0
        self._protection = protection
        self._heartbeat = heartbeat

    def read(self):
        self.reads += 1
        if self.error:
            raise self.error
        return self.market_state, self.portfolio

    def read_protection(self):
        if self._protection is not None:
            return self._protection
        rows = {}
        for ticker, owner in ((self.portfolio or {}).get("position_owners") or {}).items():
            state = ((self.portfolio.get("strategy_states") or {}).get(owner) or {}).get(ticker) or {}
            if state.get("stop_price") is not None and state.get("take_profit") is not None:
                rows[ticker] = {
                    "phase": "open", "initial_stop": state["stop_price"], "trailing_stop": state["stop_price"],
                    "target": state["take_profit"],
                }
        return rows

    def read_heartbeat(self):
        if self._heartbeat != "default":
            return self._heartbeat
        return {"scanned_at": (self.portfolio or {}).get("last_scan_at"), "session": "regular", "pid": 1}

class FakeBroker:
    def __init__(self, *, calendar=CALENDAR, positions=None, orders=None, exact_orders=None,
                 exact_order_errors=None, error=None):
        self.calendar_rows = calendar
        self.position_rows = positions if positions is not None else []
        self.order_rows = orders if orders is not None else []
        self.exact_order_rows = exact_orders if exact_orders is not None else {
            str(order["id"]): order for order in self.order_rows if isinstance(order, dict) and order.get("id")
        }
        self.exact_order_errors = exact_order_errors or {}
        self.error = error
        self.calls = []

    def account(self):
        self.calls.append("account")
        if self.error:
            raise self.error
        return {"status": "ACTIVE"}

    def calendar(self, start, end):
        self.calls.append("calendar")
        return self.calendar_rows

    def positions(self):
        self.calls.append("positions")
        return self.position_rows

    def orders(self, status, *, nested=False):
        self.calls.append(("orders", status, nested))
        return self.order_rows

    def order(self, order_id, *, nested=False):
        self.calls.append(("order", order_id, nested))
        if order_id in self.exact_order_errors:
            raise self.exact_order_errors[order_id]
        if order_id not in self.exact_order_rows:
            raise LookupError(f"Broker order {order_id} unavailable")
        return self.exact_order_rows[order_id]

def portfolio(now=NOW):
    return {
        "runtime_mode": "algo",
        "last_scan_at": (now - timedelta(seconds=60)).isoformat(),
        "last_scan_status": "ok",
        "entries_paused": False,
        "entry_pause_reasons": [],
        "position_owners": {"AAPL": "swing_momentum"},
        "strategy_states": {"swing_momentum": {"AAPL": {
            "phase": "open", "entry_order_id": "entry-1", "stop_price": 95, "take_profit": 110,
        }}},
        "startup_reconciliation": {"status": "verified"},
    }

def _codes(report):
    return {item["code"] for item in report["problems"]}


def test_healthcheck_loads_only_broker_credentials_from_shared_env(tmp_path, monkeypatch):
    project_root = tmp_path / "algo-trader"
    project_root.mkdir()
    (tmp_path / ".env").write_text(
        "ALPACA_EQUITY_API_KEY=shared-key\n"
        "ALPACA_EQUITY_API_SECRET=shared-secret\n"
        "ALPACA_EQUITY_ACCOUNT_ID=shared-account\n"
        "ALPACA_OPTIONS_API_KEY=options-key\n"
        "ALPACA_OPTIONS_API_SECRET=options-secret\n"
        "ALPACA_OPTIONS_ACCOUNT_ID=options-account\n"
        "OPENCLAW_GATEWAY_TOKEN=unrelated-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(healthcheck, "ROOT", project_root)
    monkeypatch.setenv("ALPACA_EQUITY_API_KEY", "process-key")
    monkeypatch.delenv("ALPACA_EQUITY_API_SECRET", raising=False)
    monkeypatch.delenv("ALPACA_EQUITY_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("ALPACA_OPTIONS_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_OPTIONS_API_SECRET", raising=False)
    monkeypatch.delenv("ALPACA_OPTIONS_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)

    healthcheck._load_healthcheck_environment()

    assert os.environ["ALPACA_EQUITY_API_KEY"] == "process-key"
    assert os.environ["ALPACA_EQUITY_API_SECRET"] == "shared-secret"
    assert os.environ["ALPACA_EQUITY_ACCOUNT_ID"] == "shared-account"
    assert os.environ["ALPACA_OPTIONS_API_KEY"] == "options-key"
    assert os.environ["ALPACA_OPTIONS_API_SECRET"] == "options-secret"
    assert os.environ["ALPACA_OPTIONS_ACCOUNT_ID"] == "options-account"
    assert "OPENCLAW_GATEWAY_TOKEN" not in os.environ


def test_combined_report_includes_options_problems_and_preserves_equity_fields():
    equity = {"ok": True, "last_scan_status": "ok", "problems": [], "warnings": []}
    options = {
        "ok": False,
        "problems": [{"code": "cycle_stale", "detail": "No recent options cycle"}],
        "warnings": [],
    }

    report = healthcheck._combine_health_reports(equity, options)

    assert report["ok"] is False
    assert report["equity_ok"] is True
    assert report["last_scan_status"] == "ok"
    assert report["options"] == options
    assert report["problems"] == [{"code": "options_cycle_stale", "detail": "No recent options cycle"}]

def test_healthy_active_session_verifies_sqlite_broker_owner_and_software_protection():
    diagnostic = {
        "symbol": "AAPL", "feed": "boats", "status": "fresh",
        "quote_timestamp": "2026-10-08T13:59:00+00:00",
        "age_seconds": 60.0, "freshness_limit_seconds": 90,
    }
    state = portfolio()
    state["data_status"] = {"owned_position_quote_diagnostics": [diagnostic]}
    store = FakeStore(state)
    broker = FakeBroker(positions=[{"symbol": "AAPL", "qty": "10"}])

    report = build_report(now=NOW, store=store, broker=broker, service_active=True)

    assert report["ok"] is True
    assert report["market_session"] == "regular"
    assert report["protection_row_gaps"] == []
    assert report["broker_position_count"] == 1
    assert report["owned_position_quote_diagnostics"] == [diagnostic]
    assert store.reads == 1

@pytest.mark.parametrize("quote_status", ["session_limited", "degraded"])
def test_quiet_extended_session_quote_is_warning_not_service_failure(quote_status):
    now = datetime(2026, 10, 9, 0, 10, tzinfo=timezone.utc)  # 20:10 ET
    state = portfolio(now)
    state["entries_paused"] = True
    state["entry_pause_reasons"] = ["owned positions lack fresh protective quotes"]
    state["data_status"] = {
        "quotes": quote_status,
        "quote_request_status": "succeeded",
        "owned_position_quote_diagnostics": [{"symbol": "AAPL", "status": "stale"}],
    }
    broker = FakeBroker(
        calendar=[{"date": "2026-10-09", "open": "09:30", "close": "16:00"}],
        positions=[{"symbol": "AAPL", "qty": "10"}],
        orders=[{"id": "entry-1", "symbol": "AAPL", "order_class": "simple", "side": "buy", "status": "filled", "qty": "10"}],
    )

    report = build_report(now=now, store=FakeStore(state), broker=broker, service_active=True)

    assert report["market_session"] == "overnight"
    assert report["ok"] is True
    assert report["entries_paused"] is True
    assert report["session_limited_quotes"] is True
    assert {item["code"] for item in report["warnings"]} == {"session_limited_quotes"}
    assert "entries_paused" not in _codes(report)


def test_extended_session_quote_request_failure_remains_a_problem():
    now = datetime(2026, 10, 9, 0, 10, tzinfo=timezone.utc)
    state = portfolio(now)
    state["last_scan_status"] = "degraded"
    state["entries_paused"] = True
    state["entry_pause_reasons"] = ["owned positions lack fresh protective quotes"]
    state["scan_errors"] = ["latest quotes boats: Timeout"]
    state["data_status"] = {"quotes": "session_limited", "quote_request_status": "failed"}
    broker = FakeBroker(
        calendar=[{"date": "2026-10-09", "open": "09:30", "close": "16:00"}],
        positions=[{"symbol": "AAPL", "qty": "10"}],
        orders=[{"id": "entry-1", "symbol": "AAPL", "order_class": "simple", "side": "buy", "status": "filled", "qty": "10"}],
    )

    report = build_report(now=now, store=FakeStore(state), broker=broker, service_active=True)

    assert report["ok"] is False
    assert {"scan_degraded", "entries_paused"} <= _codes(report)
    assert not report["warnings"]


def test_closed_market_does_not_alert_on_old_scan_or_stale_pause():
    saturday = datetime(2026, 10, 10, 16, 0, tzinfo=timezone.utc)
    state = portfolio(NOW)
    state["last_scan_status"] = "degraded"
    state["entries_paused"] = True
    state["entry_pause_reasons"] = ["last session's data feed failed"]

    report = build_report(
        now=saturday, store=FakeStore(state, market_state="CLOSED"),
        broker=FakeBroker(calendar=[], positions=[]), service_active=True,
    )

    assert report["ok"] is True
    assert report["market_session"] == "closed"
    assert report["last_scan_age_seconds"] > 180
    assert not report["problems"]

def test_active_session_flags_stale_scan_degraded_status_and_entry_pause():
    state = portfolio(NOW - timedelta(minutes=10))
    state["last_scan_status"] = "degraded"
    state["entries_paused"] = True
    state["entry_pause_reasons"] = ["historical bars unavailable"]

    report = build_report(
        now=NOW, store=FakeStore(state), broker=FakeBroker(), service_active=True,
    )

    assert report["ok"] is False
    assert {"scan_stale", "scan_degraded", "entries_paused"} <= _codes(report)

def _warming_portfolio(now):
    state = portfolio(now)
    state["last_scan_status"] = "degraded"
    state["entries_paused"] = True
    state["entry_pause_reasons"] = [
        "waiting for completed intraday historical bars",
        "the Algo universe has no recent completed signal bars",
        "historical bar scan is warming_up",
    ]
    state["scan_errors"] = []
    state["data_status"] = {"historical_bars": "warming_up"}
    return state

@pytest.mark.parametrize("now,session", [
    (datetime(2026, 10, 8, 8, 6, tzinfo=timezone.utc), "premarket"),  # 04:06 ET
    (datetime(2026, 10, 8, 13, 36, tzinfo=timezone.utc), "regular"),  # 09:36 ET
    (datetime(2026, 10, 8, 0, 10, tzinfo=timezone.utc), "overnight"),  # 20:10 ET prior day
])
def test_expected_first_intraday_bar_warmup_does_not_alert_scan(now, session):
    report = build_report(
        now=now, store=FakeStore(_warming_portfolio(now)),
        broker=FakeBroker(positions=[]), service_active=True,
    )

    assert report["market_session"] == session
    assert report["ok"] is True
    assert "scan_degraded" not in _codes(report)
    assert "entries_paused" not in _codes(report)

def test_intraday_bar_warmup_alerts_after_session_grace_expires():
    now = datetime(2026, 10, 8, 0, 30, tzinfo=timezone.utc)  # 20:30 ET prior day

    report = build_report(
        now=now, store=FakeStore(_warming_portfolio(now)),
        broker=FakeBroker(positions=[]), service_active=True,
    )

    assert {"scan_degraded", "entries_paused"} <= _codes(report)

def test_empty_premarket_free_feed_pause_is_visible_without_false_health_alert():
    now = datetime(2026, 10, 8, 8, 26, tzinfo=timezone.utc)  # 04:26 ET
    state = _warming_portfolio(now)
    state["position_owners"] = {}
    state["strategy_states"] = {}
    state["entry_pause_reasons"] = [
        "no completed intraday historical bars are available yet",
        "the Algo universe has no recent completed signal bars",
        "the Algo universe has no fresh quotes",
        "historical bar scan is warming_up",
    ]
    state["data_status"]["quotes"] = "unavailable"

    report = build_report(
        now=now, store=FakeStore(state, market_state="PREMARKET"),
        broker=FakeBroker(positions=[], orders=[]), service_active=True,
    )
    assert report["ok"] is True
    assert report["entries_paused"] is True
    assert report["market_data_idle"] is True

    conflict = build_report(
        now=now, store=FakeStore(state, market_state="PREMARKET"),
        broker=FakeBroker(positions=[], orders=[{"id": "pending-entry", "symbol": "AAPL"}]),
        service_active=True,
    )
    assert conflict["ok"] is False
    assert "open_orders_without_positions" in _codes(conflict)
    assert conflict["market_data_idle"] is False

def test_intraday_bar_warmup_does_not_hide_missing_software_protection():
    now = datetime(2026, 10, 8, 0, 10, tzinfo=timezone.utc)
    broker = FakeBroker(positions=[{"symbol": "AAPL", "qty": "10"}])

    report = build_report(
        now=now, store=FakeStore(_warming_portfolio(now), protection={}), broker=broker, service_active=True,
    )

    assert "software_protection_missing" in _codes(report)
    assert "scan_degraded" not in _codes(report)
    assert "entries_paused" not in _codes(report)

def test_intraday_bar_warmup_does_not_hide_provenance_pause():
    now = datetime(2026, 10, 8, 0, 10, tzinfo=timezone.utc)
    state = _warming_portfolio(now)
    state["entry_pause_reasons"].append("Algo entry provenance is unresolved: AAPL")

    report = build_report(
        now=now, store=FakeStore(state), broker=FakeBroker(positions=[]), service_active=True,
    )

    assert {"scan_degraded", "entries_paused"} <= _codes(report)

def test_new_session_gets_scan_grace_but_inactive_service_still_alerts():
    just_opened = datetime(2026, 10, 8, 13, 31, tzinfo=timezone.utc)
    old_state = portfolio(NOW - timedelta(days=1))
    old_state["entries_paused"] = True
    old_state["entry_pause_reasons"] = ["prior session"]

    report = build_report(
        now=just_opened, store=FakeStore(old_state),
        broker=FakeBroker(positions=[]), service_active=False,
    )

    assert _codes(report) == {"service_inactive"}

def test_live_unowned_holding_and_unprotected_owned_position_alert():
    state = portfolio()
    state["strategy_states"]["swing_momentum"]["AAPL"]["stop_price"] = None
    broker = FakeBroker(positions=[{"symbol": "AAPL", "qty": "10"}, {"symbol": "TSLA", "qty": "2"}])

    report = build_report(now=NOW, store=FakeStore(state), broker=broker, service_active=True)

    assert report["ok"] is False
    assert {"unowned_positions", "software_stops_missing", "software_protection_missing"} <= _codes(report)
    assert report["unowned_positions"] == ["TSLA"]
    assert report["protection_row_gaps"] == ["AAPL"]

def test_overnight_simple_entry_relies_on_software_stop():
    now = datetime(2026, 10, 8, 0, 10, tzinfo=timezone.utc)  # 20:10 ET prior day
    state = portfolio(now)
    state["strategy_states"]["swing_momentum"]["AAPL"].update({"ticker": "AAPL", "take_profit": 105})
    broker = FakeBroker(positions=[{"symbol": "AAPL", "qty": "10"}])

    report = build_report(now=now, store=FakeStore(state, market_state="OVERNIGHT"), broker=broker, service_active=True)

    assert report["ok"] is True
    assert report["market_session"] == "overnight"
    assert report["protection_row_gaps"] == []

def test_owned_positions_without_protection_rows_alert_even_with_state_levels():
    state = portfolio()
    state["position_owners"] = {"CRM": "swing_momentum", "EEM": "swing_momentum"}
    state["strategy_states"] = {"swing_momentum": {
        "CRM": {"phase": "open", "entry_order_id": "crm-entry", "stop_price": 195, "take_profit": 210},
        "EEM": {"phase": "open", "entry_order_id": "eem-entry", "stop_price": 51, "take_profit": 60},
    }}
    store = FakeStore(state, protection={"CRM": {"phase": "open", "initial_stop": 195, "trailing_stop": 195, "target": 210}})
    broker = FakeBroker(positions=[{"symbol": "CRM", "qty": "10"}, {"symbol": "EEM", "qty": "20"}])

    report = build_report(now=NOW, store=store, broker=broker, service_active=True)

    assert report["ok"] is False
    assert "software_protection_missing" in _codes(report)
    assert report["protection_row_gaps"] == ["EEM"]

def test_stale_daemon_heartbeat_with_open_positions_raises_alarm():
    state = portfolio()
    stale = {"scanned_at": (NOW - timedelta(hours=1)).isoformat(), "session": "regular", "pid": 1}
    broker = FakeBroker(positions=[{"symbol": "AAPL", "qty": "10"}])

    report = build_report(now=NOW, store=FakeStore(state, heartbeat=stale), broker=broker, service_active=True)

    assert report["ok"] is False
    assert "daemon_heartbeat_stale" in _codes(report)
    assert report["daemon_heartbeat_alarm"]["kind"] == "daemon_heartbeat_stale"

def test_stale_daemon_heartbeat_without_open_positions_is_quiet():
    state = portfolio()
    state["position_owners"] = {}
    state["strategy_states"] = {}
    stale = {"scanned_at": (NOW - timedelta(hours=1)).isoformat(), "session": "regular", "pid": 1}
    broker = FakeBroker(positions=[])

    report = build_report(now=NOW, store=FakeStore(state, heartbeat=stale), broker=broker, service_active=True)

    assert "daemon_heartbeat_stale" not in _codes(report)

def test_missing_authoritative_sqlite_fails_closed_and_does_not_initialize_it():
    store = FakeStore(error=StateStoreError("database missing"))

    report = build_report(now=NOW, store=store, broker=FakeBroker(), service_active=True)

    assert report["ok"] is False
    assert "sqlite_state_unavailable" in _codes(report)
    assert store.reads == 1

def test_account_verification_failure_stops_other_broker_reads():
    broker = FakeBroker(error=RuntimeError("configured Algo account mismatch"))

    report = build_report(now=NOW, store=FakeStore(portfolio()), broker=broker, service_active=True)

    assert "broker_account_unavailable" in _codes(report)
    assert broker.calls == ["account"]

def test_open_execution_incident_remains_an_alert():
    state = portfolio()
    state["execution_incident"] = {"status": "open", "reason": "CRM exit submission failed"}

    report = build_report(
        now=NOW, store=FakeStore(state), broker=FakeBroker(positions=[]), service_active=True,
    )

    assert report["ok"] is False
    assert "execution_incident_open" in _codes(report)
    assert report["execution_incident_status"] == "open"
