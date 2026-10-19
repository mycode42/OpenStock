"""Portfolio-monitor checks that do not depend on trailing-stop market data."""
from types import SimpleNamespace

import pytest

import portfolio_monitor as pm


def configure_monitor(monkeypatch, positions):
    closes = []
    monkeypatch.setattr(pm, "get_account_for_mode", lambda mode: SimpleNamespace(name="Mock"))
    # Deliberately omit trailing-stop configuration: the monitor no longer uses it.
    monkeypatch.setattr(pm, "get_mode_config", lambda mode: {
        "label": "Mock",
        "risk": {"stop_loss_pct": 0.10, "flatten_eod": False},
    })
    monkeypatch.setattr(pm, "get_account", lambda: {"equity": 1000, "cash": 100, "last_equity": 1000})
    monkeypatch.setattr(pm, "get_positions", lambda: positions)
    monkeypatch.setattr(pm, "get_spy_daily_return", lambda: None)
    monkeypatch.setattr(pm, "place_market_close", lambda symbol, qty, reason, dry_run: (
        closes.append((symbol, qty, reason, dry_run)) or {
            "status": "dry_run", "symbol": symbol, "qty": qty, "reason": reason,
        }
    ))
    monkeypatch.setattr(pm.requests, "get", lambda *args, **kwargs: pytest.fail("unexpected market-data request"))
    return closes


def position(price, entry=100):
    return {
        "symbol": "AAPL", "qty": "1", "current_price": str(price),
        "avg_entry_price": str(entry), "market_value": "100", "unrealized_plpc": "0",
    }


def test_monitor_skips_trailing_check_and_market_data(monkeypatch):
    closes = configure_monitor(monkeypatch, [position(price=95)])

    result = pm.run_monitor(dry_run=True, mode="swing")

    assert result["status"] == "ok"
    assert result["success"]
    assert result["actions"] == []
    assert "stop_data" not in result
    assert closes == []


def test_hard_stop_still_closes_breached_position(monkeypatch):
    closes = configure_monitor(monkeypatch, [position(price=89)])

    result = pm.run_monitor(dry_run=True, mode="swing")

    assert result["success"]
    assert [action["stop_type"] for action in result["actions"]] == ["hard_stop"]
    assert len(closes) == 1 and closes[0][0] == "AAPL"


def test_invalid_position_still_fails_hard_stop_check(monkeypatch):
    closes = configure_monitor(monkeypatch, [position(price=0)])

    result = pm.run_monitor(dry_run=True, mode="swing")

    assert result["status"] == "degraded"
    assert not result["success"]
    assert result["exit_code"] == 2
    assert closes == []
