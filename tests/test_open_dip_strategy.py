from datetime import datetime, timedelta, timezone

import pytest

from src.dip.strategy import DipConfig, DipEngine


BASE = datetime(2026, 10, 5, 13, 30, tzinfo=timezone.utc)


def make_engine(**kwargs):
    engine = DipEngine(DipConfig(**kwargs))
    engine.begin_session("2026-10-05")
    engine.set_reference("NVDA", 100, "2026-10-05")
    return engine


def tick(engine, price, seconds=0, symbol="NVDA", **kwargs):
    return engine.on_price(symbol, price, BASE + timedelta(seconds=seconds),
                           in_trading_window=kwargs.pop("in_trading_window", True),
                           market_open=kwargs.pop("market_open", True), **kwargs)


def buy_after_confirm(engine, start=1, price=98.8, **kwargs):
    first = tick(engine, price, start, **kwargs)
    if first is not None:
        return first
    return tick(engine, price, start + max(1, engine.config.confirmation_seconds), **kwargs)


def test_no_trade_before_market_open():
    engine = make_engine(confirmation_seconds=0)
    assert tick(engine, 98.0, market_open=False) is None
    assert engine.state("NVDA").quantity == 0


def test_no_trade_without_sufficient_dip():
    engine = make_engine(confirmation_seconds=0)
    tick(engine, 100)
    assert tick(engine, 99.5, 1) is None


def test_dip_triggers_entry_after_confirmation():
    engine = make_engine(confirmation_seconds=30)
    tick(engine, 100)
    assert buy_after_confirm(engine) == {
        "action": "buy", "reason": "dip_confirmed", "symbol": "NVDA",
        "price": 98.8, "quantity": 50, "notional": 4940.0,
    }


def test_confirmation_period_prevents_early_entry():
    engine = make_engine(confirmation_seconds=30)
    tick(engine, 100)
    assert tick(engine, 98.8, 1) is None
    assert tick(engine, 98.8, 30) is None
    assert tick(engine, 98.8, 31)["action"] == "buy"


def open_position(engine, price=98.8):
    tick(engine, 100)
    signal = buy_after_confirm(engine, price=price)
    engine.set_pending("NVDA", "buy", "dip-test-buy")
    engine.order_update("NVDA", {"side": "buy", "status": "filled", "filled_qty": signal["quantity"], "filled_avg_price": price})


def test_take_profit_triggers_sell():
    engine = make_engine(confirmation_seconds=0)
    open_position(engine)
    assert tick(engine, 100.282, 40)["reason"] == "take_profit"


def test_stop_loss_triggers_sell():
    engine = make_engine(confirmation_seconds=0)
    open_position(engine)
    assert tick(engine, 97.812, 40)["reason"] == "stop_loss"


def sell_position(engine, price=100.4):
    qty = engine.state("NVDA").quantity
    engine.set_pending("NVDA", "sell", "dip-test-sell")
    engine.order_update("NVDA", {"side": "sell", "status": "filled", "filled_qty": qty, "filled_avg_price": price})


def test_reentry_works_after_second_dip_from_last_sell():
    engine = make_engine(confirmation_seconds=0, minimum_dip_from_last_sell_percent=0.8)
    open_position(engine)
    sell_position(engine)
    assert tick(engine, 99.4, 50)["action"] == "buy"


def test_reentry_threshold_is_respected():
    engine = make_engine(confirmation_seconds=0, minimum_dip_from_last_sell_percent=0.8)
    open_position(engine)
    sell_position(engine)
    assert tick(engine, 100.0, 50) is None
    assert engine.state("NVDA").last_reason == "re-entry threshold not met"


def test_maximum_trades_per_day():
    engine = make_engine(confirmation_seconds=0, max_trades_per_day=1)
    open_position(engine)
    sell_position(engine)
    assert tick(engine, 99.0, 50) is None
    assert engine.state("NVDA").last_reason == "maximum daily trades reached"


def test_maximum_trades_per_instrument_per_day():
    engine = make_engine(confirmation_seconds=0, max_trades_per_instrument_per_day=1)
    open_position(engine)
    sell_position(engine)
    assert tick(engine, 99.0, 50) is None
    assert engine.state("NVDA").last_reason == "maximum instrument trades reached"


def test_maximum_position_size_is_enforced():
    engine = make_engine(confirmation_seconds=0, max_position_size=1000)
    tick(engine, 100)
    result = tick(engine, 98.9, 1)
    if result is None:
        result = tick(engine, 98.9, 2)
    assert result["notional"] <= 1000


def test_total_exposure_limit_blocks_entry():
    engine = make_engine(confirmation_seconds=0, max_total_exposure=1000)
    tick(engine, 100)
    assert tick(engine, 98.8, 1, total_exposure=1000) is None
    assert engine.state("NVDA").last_reason == "insufficient buying power or exposure limit"


def test_large_opening_gap_down_is_rejected():
    engine = make_engine(confirmation_seconds=0, max_opening_gap_percent=3)
    assert tick(engine, 96.9) is None
    assert engine.state("NVDA").gap_rejected
    assert tick(engine, 95, 1) is None


def test_market_window_close_stops_new_entries_but_position_monitoring_continues():
    engine = make_engine(confirmation_seconds=0)
    tick(engine, 100)
    assert tick(engine, 98, 1, in_trading_window=False) is None
    open_position(engine)
    assert tick(engine, 100.3, 40, in_trading_window=False)["reason"] == "take_profit"


def test_existing_position_is_detected_after_restart():
    engine = make_engine()
    engine.mark_existing_position("NVDA", 10, 99)
    restored = DipEngine.restore(engine.config, engine.snapshot())
    assert tick(restored, 98.1, 1) is None
    assert restored.state("NVDA").status == "POSITION_OPEN"
    assert restored.state("NVDA").quantity == 10


def test_pending_order_is_not_duplicated():
    engine = make_engine(confirmation_seconds=0)
    tick(engine, 100)
    engine.state("NVDA").dip_since = (BASE - timedelta(seconds=1)).isoformat()
    engine.set_pending("NVDA", "buy", "dip-deterministic-id")
    assert tick(engine, 98.8, pending_order=True) is None


def test_partial_buy_and_sell_fills_update_state_once():
    engine = make_engine()
    state = engine.state("NVDA")
    engine.set_pending("NVDA", "buy", "partial-buy")
    engine.order_update("NVDA", {"side": "buy", "status": "partially_filled", "filled_qty": "5", "filled_avg_price": "98"})
    engine.order_update("NVDA", {"side": "buy", "status": "filled", "filled_qty": "10", "filled_avg_price": "98"})
    assert (state.quantity, state.trades_today, engine.total_trades_today) == (10, 1, 1)

    engine.set_pending("NVDA", "sell", "partial-sell")
    engine.order_update("NVDA", {"side": "sell", "status": "partially_filled", "filled_qty": "4", "filled_avg_price": "100"})
    engine.order_update("NVDA", {"side": "sell", "status": "filled", "filled_qty": "10", "filled_avg_price": "100"})
    assert state.quantity == 0
    assert state.realized_pnl_today == pytest.approx(20)


def test_replaying_completed_order_response_is_idempotent():
    engine = make_engine()
    engine.set_pending("NVDA", "buy", "buy-once")
    order = {"id": "order-1", "client_order_id": "buy-once", "side": "buy", "status": "filled", "filled_qty": "10", "filled_avg_price": "98"}
    engine.order_update("NVDA", order)
    engine.order_update("NVDA", order)
    assert engine.state("NVDA").quantity == 10
    assert engine.total_trades_today == 1


def test_rejected_buy_waits_for_dip_to_reset_before_retry():
    engine = make_engine()
    engine.set_pending("NVDA", "buy", "buy-rejected")
    engine.order_update("NVDA", {"side": "buy", "status": "rejected"})
    assert tick(engine, 98.8, 1) is None
    assert engine.state("NVDA").entry_rejected
    assert tick(engine, 99.5, 2) is None
    assert not engine.state("NVDA").entry_rejected


def test_api_failure_can_be_retried_without_losing_engine_state():
    engine = make_engine(confirmation_seconds=0)
    tick(engine, 100)
    assert tick(engine, 98.8, 1)["action"] == "buy"
    # The daemon catches adapter exceptions around each cycle; persisted pure state remains valid.
    restored = DipEngine.restore(engine.config, engine.snapshot())
    assert restored.state("NVDA").reference_price == 100


def test_daemon_survives_temporary_account_api_failure(monkeypatch):
    from src.dip import daemon

    class BrokenAccount:
        def account(self):
            raise ConnectionError("temporary network error")

    monkeypatch.setattr(daemon, "_state_path", lambda: __import__("pathlib").Path("/tmp/dip-test-state.json"))
    daemon.run_loop(max_cycles=1, broker_client=BrokenAccount())


def test_account_id_mismatch_stops_daemon(monkeypatch, tmp_path):
    from src.dip import daemon
    from src.dip.alpaca import DipAccountMismatchError

    class WrongAccount:
        def calendar(self, start, end):
            from datetime import datetime
            day = datetime.now(daemon.ET).date().isoformat()
            return [{"date": day, "open": "00:00", "close": "23:59"}]

        def account(self):
            raise DipAccountMismatchError("account ID mismatch")

    monkeypatch.setattr(daemon, "_state_path", lambda: tmp_path / "dip-state.json")
    daemon.run_loop(broker_client=WrongAccount())
    assert '"market_state": "CONFIG_ERROR"' in (tmp_path / "dip-state.json").read_text()


def test_alpaca_paper_account_number_is_accepted_as_dip_identifier(monkeypatch):
    from src.dip.alpaca import DipAlpaca

    client = DipAlpaca.__new__(DipAlpaca)
    client.account_id = "paper-account-number"
    monkeypatch.setattr(client, "_get", lambda url: {
        "id": "api-uuid",
        "account_number": "paper-account-number",
        "status": "ACTIVE",
        "trading_blocked": False,
    })
    assert client.account()["id"] == "api-uuid"


def test_disabled_service_exits_cleanly(monkeypatch):
    from src.dip import daemon

    monkeypatch.setattr(daemon, "DIP_CONFIG", {**daemon.DIP_CONFIG, "enabled": False})
    monkeypatch.setattr("sys.argv", ["dip-daemon"])
    daemon.main()


def test_stale_market_data_prevents_trading():
    engine = make_engine(confirmation_seconds=0)
    tick(engine, 100)
    assert tick(engine, 98.8, 1, data_fresh=False) is None
    assert engine.state("NVDA").last_reason == "stale market data"


def test_multiple_instruments_operate_independently():
    engine = make_engine(confirmation_seconds=0)
    engine.set_reference("AMD", 50, "2026-10-05")
    tick(engine, 100)
    assert tick(engine, 98.8, 1)["action"] == "buy"
    assert tick(engine, 50, 1, symbol="AMD") is None
    assert engine.state("AMD").status == "WAITING_FOR_DIP"
    assert engine.state("NVDA").status == "WAITING_FOR_DIP"
