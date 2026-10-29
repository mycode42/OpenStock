"""Idle-cash planning, entry limit bands, intraday stop floors, and blocked-trade journaling."""

from datetime import datetime

import pytest

from reporting import journal_store

from tests.conftest import write_journal

from reporting import journal
from strategies.runner.cash_reserve import plan_cash_reserve
from strategies.runner.sizing import _make_trade, entry_limit_price, intraday_volatility_stop_pct

ON = {"enabled": True}


def test_cash_reserve_holds_when_disabled():
    plan = plan_cash_reserve(
        equity=100_000, cash=90_000, invested_value=0, proxy_price=100, proxy_qty=0,
        config={"enabled": False},
    )
    assert plan["action"] == "hold"


def test_cash_reserve_parks_only_spare_cash_above_reserve_and_below_gross_target():
    plan = plan_cash_reserve(
        equity=100_000, cash=95_000, invested_value=20_000, proxy_price=100,
        proxy_qty=0, config=ON,
    )
    assert plan["action"] == "buy"
    assert plan["qty"] * 100 <= 95_000 - 10_000  # never dips into the reserve


def test_cash_reserve_frees_cash_when_entries_need_it_and_never_sells_more_than_held():
    plan = plan_cash_reserve(
        equity=100_000, cash=8_000, invested_value=70_000, proxy_price=100,
        proxy_qty=50, config=ON,
    )
    assert plan["action"] == "sell"
    assert 0 < plan["qty"] <= 50


def test_cash_reserve_holds_when_exposure_is_already_at_target():
    plan = plan_cash_reserve(
        equity=100_000, cash=60_000, invested_value=90_000, proxy_price=100,
        proxy_qty=0, config=ON,
    )
    assert plan["action"] == "hold"


def test_entry_limit_never_chases_the_decision_price():
    assert entry_limit_price("buy", 100.0, 10) == 100.10
    assert entry_limit_price("short", 100.0, 10) == 99.90
    assert entry_limit_price("sell", 100.0, 10) is None


def test_intraday_stop_floor_scales_with_recent_volatility_and_is_absent_without_data():
    flat = [{"c": 100.0} for _ in range(40)]
    choppy = [{"c": 100.0 + (1 if index % 2 else -1)} for index in range(40)]

    assert intraday_volatility_stop_pct(flat, multiple=3.0, lookback=36) == 0
    assert intraday_volatility_stop_pct(choppy, multiple=3.0, lookback=36) > 0.01
    assert intraday_volatility_stop_pct(flat[:5], multiple=3.0, lookback=36) is None


def test_minimum_stop_widens_the_intraday_stop_and_shrinks_size():
    signal = {"ticker": "AAA", "action": "buy", "strategy": "intraday_mean_reversion", "reason": "x"}
    risk = {
        "risk_per_trade_pct": 0.005, "max_position_pct": 0.07, "max_strategy_position_pct": 0.07,
        "stop_loss_pct": 0.05, "stop_loss_pct_by_strategy": {"intraday_mean_reversion": 0.011},
    }
    base = _make_trade(signal, 100.0, 100_000, {}, risk)
    widened = _make_trade(signal, 100.0, 100_000, {}, risk, minimum_stop_pct=0.02)

    assert base["stop_price"] == pytest.approx(98.9)
    assert widened["stop_price"] == pytest.approx(98.0)
    assert widened["qty"] <= base["qty"]


def test_blocked_and_failed_journal_rows_keep_their_rule_and_reason(tmp_path, monkeypatch):
    path = tmp_path / "algo_state.sqlite3"
    write_journal(path, "")
    monkeypatch.setattr(journal_store, "DEFAULT_DATABASE", path)

    journal.append_trades({
        "mode": "algo", "timestamp": datetime(2026, 10, 27, 10, 0).isoformat(),
        "results": [{
            "ticker": "AAA", "action": "buy", "qty": 5, "status": "blocked",
            "reason": "Sector cap reached", "rule": "max_sector_pct", "strategy": "swing_momentum",
        }],
    })

    [row] = journal_store.read_entries(path)
    assert row["status"] == "blocked"
    assert row["rule"] == "max_sector_pct"
    assert row["error"] == "Sector cap reached"


def test_cash_proxy_is_invisible_to_scans_and_risk_inputs(monkeypatch):
    from portfolio import config
    from strategies.runner import data as scan_data

    positions = [{"symbol": "SGOV", "qty": "20"}, {"symbol": "AAPL", "qty": "3"}]
    orders = [{"symbol": "SGOV", "id": "x"}, {"symbol": "AAPL", "id": "y"}]
    account, visible, open_orders = scan_data.current_portfolio(
        "algo",
        request_fn=lambda endpoint, **kwargs: {
            "account": {"equity": "100"}, "positions": positions, "orders": orders,
        }[endpoint],
    )
    assert set(visible) == {"AAPL"}
    assert [order["id"] for order in open_orders] == ["y"]
    assert config.is_cash_proxy("sgov") and not config.is_cash_proxy("AAPL")


def test_cash_reserve_step_is_skipped_in_dry_run_and_outside_regular_session():
    from types import SimpleNamespace

    from portfolio import daemon

    class Broker:
        def __getattr__(self, name):
            raise AssertionError(f"broker must not be called: {name}")

    assert daemon._run_cash_reserve(Broker(), window=SimpleNamespace(name="regular"), dry_run=True, entry_notional=0) is None
    assert daemon._run_cash_reserve(Broker(), window=SimpleNamespace(name="premarket"), dry_run=False, entry_notional=0) is None


def test_entry_cash_hop_sells_reserve_only_for_the_shortfall_and_defers_entries():
    from strategies.runner.cash_reserve import plan_entry_cash

    hop = plan_entry_cash(
        equity=100_000, cash=5_000, entry_notional=8_000, min_cash_pct=0.10,
        proxy_price=100, proxy_qty=200,
    )
    # Shortfall = 8,000 + 10,000 floor - 5,000 = 13,000 -> 130 shares.
    assert hop == {"sell_qty": 130, "defer_entries": True}


def test_entry_cash_hop_does_nothing_when_raw_cash_funds_entries_above_the_floor():
    from strategies.runner.cash_reserve import plan_entry_cash

    hop = plan_entry_cash(
        equity=100_000, cash=50_000, entry_notional=8_000, min_cash_pct=0.10,
        proxy_price=100, proxy_qty=200,
    )
    assert hop == {"sell_qty": 0, "defer_entries": False}


def test_entries_are_not_starved_by_parking_while_they_are_pending():
    from strategies.runner.cash_reserve import plan_cash_reserve

    plan = plan_cash_reserve(
        equity=100_000, cash=60_000, invested_value=0, proxy_price=100, proxy_qty=0,
        config={"enabled": True}, entries_pending=True,
    )
    assert plan["action"] == "hold"
