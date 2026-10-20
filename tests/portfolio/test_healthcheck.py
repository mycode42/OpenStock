"""Health cron decisions for active and closed Algo market sessions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from portfolio.healthcheck import build_report
from portfolio.state_store import StateStoreError

NOW = datetime(2026, 10, 8, 14, 0, tzinfo=timezone.utc)  # 10:00 ET
CALENDAR = [{"date": "2026-10-08", "open": "09:30", "close": "16:00"}]

class FakeStore:
    def __init__(self, portfolio=None, market_state="REGULAR", error=None):
        self.portfolio = portfolio
        self.market_state = market_state
        self.error = error
        self.reads = 0

    def read(self):
        self.reads += 1
        if self.error:
            raise self.error
        return self.market_state, self.portfolio

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
        "position_owners": {"AAPL": "momentum"},
        "strategy_states": {"momentum": {"AAPL": {
            "phase": "open", "entry_order_id": "entry-1", "stop_price": 95,
        }}},
        "startup_reconciliation": {"status": "verified"},
    }

def _codes(report):
    return {item["code"] for item in report["problems"]}

def test_healthy_active_session_verifies_sqlite_broker_owner_and_bracket_stop():
    diagnostic = {
        "symbol": "AAPL", "feed": "boats", "status": "fresh",
        "quote_timestamp": "2026-10-08T13:59:00+00:00",
        "age_seconds": 60.0, "freshness_limit_seconds": 90,
    }
    state = portfolio()
    state["data_status"] = {"owned_position_quote_diagnostics": [diagnostic]}
    store = FakeStore(state)
    broker = FakeBroker(
        positions=[{"symbol": "AAPL", "qty": "10"}],
        orders=[{
            "id": "entry-1", "symbol": "AAPL", "order_class": "bracket", "legs": [
                {"side": "sell", "type": "stop", "status": "held", "qty": "10", "filled_qty": "0"},
                {"side": "sell", "type": "limit", "status": "new", "qty": "10"},
            ],
        }],
    )

    report = build_report(now=NOW, store=store, broker=broker, service_active=True)

    assert report["ok"] is True
    assert report["market_session"] == "regular"
    assert report["bracket_entries_checked"] == 1
    assert report["broker_position_count"] == 1
    assert report["owned_position_quote_diagnostics"] == [diagnostic]
    assert broker.calls == ["account", "calendar", "positions", ("orders", "all", True), ("order", "entry-1", True)]
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


def test_capped_history_page_uses_exact_nested_entry_lookup():
    entry = {
        "id": "entry-1", "symbol": "AAPL", "order_class": "bracket",
        "legs": [{"side": "sell", "type": "stop", "status": "held", "qty": "10", "filled_qty": "0"}],
    }
    broker = FakeBroker(
        positions=[{"symbol": "AAPL", "qty": "10"}],
        orders=[{"id": f"recent-{number}", "symbol": "OTHER"} for number in range(500)],
        exact_orders={"entry-1": entry},
    )

    report = build_report(now=NOW, store=FakeStore(portfolio()), broker=broker, service_active=True)

    assert report["ok"] is True
    assert report["broker_stop_coverage_unknown"] == []
    assert report["bracket_entries_checked"] == 1
    assert ("order", "entry-1", True) in broker.calls

def test_failed_exact_entry_lookup_cannot_fall_back_to_capped_history_row():
    entry = {
        "id": "entry-1", "symbol": "AAPL", "order_class": "bracket",
        "legs": [{"side": "sell", "type": "stop", "status": "held", "qty": "10", "filled_qty": "0"}],
    }
    broker = FakeBroker(
        positions=[{"symbol": "AAPL", "qty": "10"}], orders=[entry],
        exact_order_errors={"entry-1": RuntimeError("exact broker lookup timed out")},
    )

    report = build_report(now=NOW, store=FakeStore(portfolio()), broker=broker, service_active=True)

    assert report["ok"] is False
    assert "orders_unavailable" in _codes(report)
    assert report["broker_stop_coverage_unknown"] == ["AAPL"]
    assert report["bracket_entries_checked"] == 0

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

def test_intraday_bar_warmup_does_not_hide_missing_broker_stop():
    now = datetime(2026, 10, 8, 0, 10, tzinfo=timezone.utc)
    broker = FakeBroker(
        positions=[{"symbol": "AAPL", "qty": "10"}],
        orders=[{"id": "entry-1", "symbol": "AAPL", "order_class": "bracket", "legs": []}],
    )

    report = build_report(
        now=now, store=FakeStore(_warming_portfolio(now)), broker=broker, service_active=True,
    )

    assert "broker_bracket_stops_missing" in _codes(report)
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

def test_live_unowned_holding_and_unprotected_known_bracket_alert():
    state = portfolio()
    state["strategy_states"]["momentum"]["AAPL"]["stop_price"] = None
    broker = FakeBroker(
        positions=[{"symbol": "AAPL", "qty": "10"}, {"symbol": "TSLA", "qty": "2"}],
        orders=[{"id": "entry-1", "symbol": "AAPL", "order_class": "bracket", "legs": []}],
    )

    report = build_report(now=NOW, store=FakeStore(state), broker=broker, service_active=True)

    assert report["ok"] is False
    assert {"unowned_positions", "software_stops_missing", "broker_bracket_stops_missing"} <= _codes(report)
    assert report["unowned_positions"] == ["TSLA"]
    assert report["broker_bracket_stops_missing"] == ["AAPL"]

def test_overnight_simple_entry_relies_on_software_stop():
    now = datetime(2026, 10, 8, 0, 10, tzinfo=timezone.utc)  # 20:10 ET prior day
    state = portfolio(now)
    state["strategy_states"]["momentum"]["AAPL"].update({"ticker": "AAPL", "take_profit": 105})
    broker = FakeBroker(
        positions=[{"symbol": "AAPL", "qty": "10"}],
        orders=[{"id": "entry-1", "symbol": "AAPL", "order_class": "simple", "legs": []}],
    )

    report = build_report(now=now, store=FakeStore(state, market_state="OVERNIGHT"), broker=broker, service_active=True)

    assert report["ok"] is True
    assert report["market_session"] == "overnight"
    assert report["bracket_entries_checked"] == 0
    assert report["broker_bracket_stops_missing"] == []

def test_canceled_bracket_stops_and_pending_cancel_profit_legs_alert_even_with_software_stops():
    state = portfolio()
    state["position_owners"] = {"CRM": "momentum", "EEM": "momentum"}
    state["strategy_states"] = {"momentum": {
        "CRM": {"phase": "open", "entry_order_id": "crm-entry", "stop_price": 195},
        "EEM": {"phase": "open", "entry_order_id": "eem-entry", "stop_price": 51},
    }}
    broker = FakeBroker(
        positions=[{"symbol": "CRM", "qty": "10"}, {"symbol": "EEM", "qty": "20"}],
        orders=[
            {"id": "crm-entry", "symbol": "CRM", "order_class": "bracket", "legs": [
                {"side": "sell", "type": "stop", "status": "canceled", "qty": "10"},
                {"side": "sell", "type": "limit", "status": "pending_cancel", "qty": "10"},
            ]},
            {"id": "eem-entry", "symbol": "EEM", "order_class": "bracket", "legs": [
                {"side": "sell", "type": "stop", "status": "pending_cancel", "qty": "20"},
                {"side": "sell", "type": "limit", "status": "pending_cancel", "qty": "20"},
            ]},
        ],
    )

    report = build_report(now=NOW, store=FakeStore(state), broker=broker, service_active=True)

    assert report["ok"] is False
    assert "software_stops_missing" not in _codes(report)
    assert {"broker_bracket_stops_missing", "broker_exit_pending_cancel"} <= _codes(report)
    assert report["broker_bracket_stops_missing"] == ["CRM", "EEM"]
    assert report["broker_pending_cancel_exits"] == ["CRM:limit", "EEM:limit", "EEM:stop"]

def test_partial_bracket_stop_quantity_does_not_count_as_full_protection():
    broker = FakeBroker(
        positions=[{"symbol": "AAPL", "qty": "10"}],
        orders=[{"id": "entry-1", "symbol": "AAPL", "order_class": "bracket", "legs": [
            {"side": "sell", "type": "stop", "status": "held", "qty": "10", "filled_qty": "3"},
        ]}],
    )

    report = build_report(now=NOW, store=FakeStore(portfolio()), broker=broker, service_active=True)

    assert report["broker_bracket_stops_missing"] == ["AAPL"]

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

def test_open_execution_incident_and_failed_repair_remain_alerts():
    state = portfolio()
    state["execution_incident"] = {"status": "open", "reason": "CRM exit submission failed"}
    state["protection_recovery_status"] = {
        "status": "degraded", "errors": ["CRM stop coverage remains unverified"],
    }

    report = build_report(
        now=NOW, store=FakeStore(state), broker=FakeBroker(positions=[]), service_active=True,
    )

    assert report["ok"] is False
    assert {"execution_incident_open", "protection_recovery_degraded"} <= _codes(report)
    assert report["execution_incident_status"] == "open"
    assert report["protection_recovery_status"] == "degraded"
