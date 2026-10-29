import json
import os
from datetime import datetime, timedelta, timezone

from options import healthcheck


OPTIONS_ACCOUNT_ID = "options-paper-test"


class FakeOptionsClient:
    account_id = OPTIONS_ACCOUNT_ID

    def verify_account(self):
        return {"id": OPTIONS_ACCOUNT_ID, "status": "ACTIVE", "trading_blocked": False}

    def positions(self):
        return [
            {"symbol": "AAPL261120C00100000", "asset_class": "us_option"},
            {"symbol": "SPY", "asset_class": "us_equity"},
        ]

    def orders(self, status):
        assert status == "open"
        return [{"id": "open-option-order"}]


class FakeAlgoClient:
    def account(self):
        return {"id": "equity-paper-test", "status": "ACTIVE", "trading_blocked": False}

    def calendar(self, _start, _end):
        return [{"date": "2026-10-29", "open": "09:30", "close": "16:00"}]


def _write_cycle_records(tmp_path, now, *, age=timedelta(minutes=10)):
    cycle_at = now - age
    risk_snapshot = tmp_path / "risk_snapshot.json"
    risk_snapshot.write_text(json.dumps({
        "schema_version": 1,
        "account_id": OPTIONS_ACCOUNT_ID,
        "snapshot_at": cycle_at.isoformat(),
        "summary": {"data_quality": []},
    }))
    journal = tmp_path / "risk_daily.jsonl"
    journal.write_text(json.dumps({
        "logged_at": cycle_at.isoformat(),
        "account_id": OPTIONS_ACCOUNT_ID,
        "data_quality": [],
    }) + "\n")
    return risk_snapshot, journal


def _build(tmp_path, now, *, cycle_age=timedelta(minutes=10)):
    risk_snapshot, journal = _write_cycle_records(tmp_path, now, age=cycle_age)
    return healthcheck.build_report(
        now=now,
        options_client=FakeOptionsClient(),
        algo_client=FakeAlgoClient(),
        service_active=True,
        risk_snapshot_path=risk_snapshot,
        journal_path=journal,
    )


def test_options_healthcheck_passes_with_fresh_cycle_and_broker_data(tmp_path):
    now = datetime(2026, 10, 29, 15, 0, tzinfo=timezone.utc)

    report = _build(tmp_path, now)

    assert report["ok"] is True
    assert report["service_active"] is True
    assert report["market_session"] == "regular"
    assert report["cycle_expected"] is True
    assert report["broker_position_count"] == 1
    assert report["open_option_order_count"] == 1
    assert report["risk_snapshot_status"] == "ok"
    assert report["problems"] == []


def test_options_healthcheck_fails_for_stale_cycle_during_scan_window(tmp_path):
    now = datetime(2026, 10, 29, 15, 0, tzinfo=timezone.utc)

    report = _build(tmp_path, now, cycle_age=timedelta(minutes=50))

    assert report["ok"] is False
    assert any(item["code"] == "cycle_stale" for item in report["problems"])


def test_options_healthcheck_treats_stale_cycle_as_expected_after_hours(tmp_path):
    now = datetime(2026, 10, 29, 21, 0, tzinfo=timezone.utc)

    report = _build(tmp_path, now, cycle_age=timedelta(hours=7))

    assert report["ok"] is True
    assert report["market_session"] == "after_hours"
    assert report["cycle_expected"] is False
    assert any(item["code"] == "cycle_stale" for item in report["warnings"])


def test_healthcheck_loads_only_options_and_equity_broker_credentials(tmp_path, monkeypatch):
    project_root = tmp_path / "OpenStock" / "algo-trader"
    project_root.mkdir(parents=True)
    env_path = project_root.parent / ".env"
    env_path.write_text(
        "ALPACA_OPTIONS_API_KEY=options-key\n"
        "ALPACA_OPTIONS_API_SECRET=options-secret\n"
        "ALPACA_OPTIONS_ACCOUNT_ID=options-account\n"
        "ALPACA_EQUITY_API_KEY=equity-key\n"
        "ALPACA_EQUITY_API_SECRET=equity-secret\n"
        "ALPACA_EQUITY_ACCOUNT_ID=equity-account\n"
        "ALGO_REPORT_TELEGRAM_TO=private-destination\n"
    )
    monkeypatch.setattr(healthcheck, "ROOT", project_root)
    required = (
        "ALPACA_OPTIONS_API_KEY", "ALPACA_OPTIONS_API_SECRET", "ALPACA_OPTIONS_ACCOUNT_ID",
        "ALPACA_EQUITY_API_KEY", "ALPACA_EQUITY_API_SECRET", "ALPACA_EQUITY_ACCOUNT_ID",
    )
    for name in (*required, "ALGO_REPORT_TELEGRAM_TO"):
        monkeypatch.delenv(name, raising=False)

    healthcheck._load_healthcheck_environment()

    assert all(os.environ.get(name) for name in required)
    assert "ALGO_REPORT_TELEGRAM_TO" not in os.environ
