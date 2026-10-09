import pytest

import risk_manager
from src.config import ALGO_MODE_CONFIG, MODES, STRATEGY_CONFIG, get_mode_config
from src.strategies import mean_reversion, momentum, opening_range_breakout, pairs, trend_following
from src.strategies.regime import rank_strategies
from src.strategies.signals import Signal
from src.strategies.runner import _make_trade, _update_trailing_stop
import src.strategies.rule_pipeline as rule_pipeline
from src.strategies.state_machine import COOLDOWN, ENTRY_PENDING, OPEN, RuleStrategyStateMachine


SESSION_DATE = "2026-10-07"


def _order(order_id, side, status, qty, submitted_at, filled_qty="0"):
    return {
        "id": order_id,
        "symbol": "AAPL",
        "side": side,
        "status": status,
        "qty": str(qty),
        "filled_qty": str(filled_qty),
        "submitted_at": submitted_at,
    }


def _fill(order_id, side, qty, time):
    return {
        "id": f"fill-{order_id}",
        "order_id": order_id,
        "symbol": "AAPL",
        "side": side,
        "qty": str(qty),
        "transaction_time": f"{SESSION_DATE}T{time}:00Z",
    }


def _portfolio_state(**overrides):
    state = {
        "equity": 100_000.0,
        "cash": 95_000.0,
        "cash_pct": 0.95,
        "daily_pnl_pct": 0.0,
        "weekly_pnl_pct": 0.0,
        "positions": {},
        "sector_alloc": {},
        "trade_count_today": 0,
        "entry_count_today": 0,
        "entry_count_available": True,
        "open_position_count": 0,
    }
    state.update(overrides)
    return state


def _bars(closes, volumes=None):
    volumes = volumes or [1_000] * len(closes)
    return [
        {"c": close, "h": close, "l": close, "v": volume}
        for close, volume in zip(closes, volumes)
    ]


def test_algo_risk_profile_increases_throughput_without_changing_swing_or_day():
    assert ALGO_MODE_CONFIG["risk"]["max_open_positions"] == 16
    assert ALGO_MODE_CONFIG["risk"]["max_trades_per_day"] == 10
    assert ALGO_MODE_CONFIG["risk"]["max_position_pct"] == 0.05
    assert ALGO_MODE_CONFIG["risk"]["allow_short_positions"] is True
    assert ALGO_MODE_CONFIG["risk"]["max_short_positions"] == 5
    assert ALGO_MODE_CONFIG["risk"]["max_short_exposure_pct"] == 0.20
    assert ALGO_MODE_CONFIG["risk"]["max_sector_pct"] == 0.40
    assert ALGO_MODE_CONFIG["risk"]["min_cash_pct"] == 0.15
    assert ALGO_MODE_CONFIG["risk"]["strategy_group_limits"]["swing"]["max_open_positions"] == 10
    assert ALGO_MODE_CONFIG["risk"]["strategy_group_limits"]["swing"]["max_entries_per_day"] == 3
    assert ALGO_MODE_CONFIG["risk"]["strategy_group_limits"]["intraday_core"]["max_open_positions"] == 6
    assert ALGO_MODE_CONFIG["risk"]["strategy_group_limits"]["intraday_core"]["max_entries_per_day"] == 3
    assert "multi_factor" not in ALGO_MODE_CONFIG["risk"]["max_positions_per_strategy_by_strategy"]
    assert "event_driven" not in ALGO_MODE_CONFIG["risk"]["max_positions_per_strategy_by_strategy"]
    assert STRATEGY_CONFIG["overnight_entries_enabled"] is False
    assert ALGO_MODE_CONFIG["risk"]["stop_loss_pct_by_strategy"]["trend_following"] > 0.05
    assert ALGO_MODE_CONFIG["risk"]["stop_loss_pct_by_strategy"]["mean_reversion"] <= 0.012
    assert STRATEGY_CONFIG["enable_intraday_trend"] is False
    assert STRATEGY_CONFIG["enable_intraday_momentum"] is False
    assert MODES["swing"]["risk"]["max_open_positions"] == 12
    assert MODES["swing"]["risk"]["max_trades_per_day"] == 4
    assert MODES["day"]["risk"]["max_open_positions"] == 8
    assert MODES["day"]["risk"]["max_trades_per_day"] == 20
    assert STRATEGY_CONFIG["extended_hours_max_spread_pct"] == 0.02
    with pytest.raises(ValueError, match="Unknown mode"):
        get_mode_config("unknown")


def test_algo_stop_sizing_uses_horizon_specific_config():
    risk = ALGO_MODE_CONFIG["risk"]
    intraday = _make_trade({
        "ticker": "AAPL", "action": "buy", "strategy": "mean_reversion",
        "reason": "intraday setup", "risk_multiplier": 1.0,
    }, 100, 100_000, {}, risk)
    swing = _make_trade({
        "ticker": "AAPL", "action": "buy", "strategy": "trend_following",
        "reason": "swing setup", "risk_multiplier": 1.0, "stop_pct": 0.01,
    }, 100, 100_000, {}, risk)

    assert intraday["stop_price"] == 98.9
    assert swing["stop_price"] == 94.0
    assert swing["trailing_stop_pct"] > intraday["trailing_stop_pct"]


def test_algo_software_trails_tighten_for_both_directions():
    risk = ALGO_MODE_CONFIG["risk"]
    long_state = {"stop_price": 94.0}
    short_state = {"stop_price": 106.0}

    long_stop = _update_trailing_stop(
        long_state, strategy="trend_following", qty=10, entry_price=100,
        current_price=104, mode_risk=risk,
    )
    short_stop = _update_trailing_stop(
        short_state, strategy="trend_following", qty=-10, entry_price=100,
        current_price=96, mode_risk=risk,
    )

    assert long_stop > 94.0
    assert short_stop < 106.0


def test_algo_entry_count_ignores_scale_ins_to_existing_positions():
    orders = [_order("scale-in", "buy", "filled", 10, f"{SESSION_DATE}T14:00:00Z", 10)]
    fills = [_fill("scale-in", "buy", 10, "14:01")]

    count = risk_manager._count_algo_new_entries_today(
        orders, fills, {"AAPL": {"qty": 20}}, SESSION_DATE,
    )

    assert count == 0


def test_algo_entry_count_counts_reentry_after_filled_exit():
    orders = [
        _order("entry-1", "buy", "filled", 10, f"{SESSION_DATE}T14:00:00Z", 10),
        _order("entry-2", "buy", "filled", 5, f"{SESSION_DATE}T15:00:00Z", 5),
    ]
    fills = [
        _fill("entry-1", "buy", 10, "14:01"),
        _fill("exit-1", "sell", 10, "14:30"),
        _fill("entry-2", "buy", 5, "15:01"),
    ]

    count = risk_manager._count_algo_new_entries_today(
        orders, fills, {"AAPL": {"qty": 5}}, SESSION_DATE,
    )

    assert count == 2


def test_algo_entry_count_reserves_slot_for_pending_opening_order():
    orders = [_order("pending", "buy", "new", 10, f"{SESSION_DATE}T14:00:00Z")]

    count = risk_manager._count_algo_new_entries_today(orders, [], {}, SESSION_DATE)

    assert count == 1


def test_algo_order_timestamps_use_new_york_session_date():
    assert risk_manager._is_market_session_date(
        "2026-10-08T02:15:00Z", "2026-10-07",
    )
    assert not risk_manager._is_market_session_date(
        "2026-10-08T05:00:00Z", "2026-10-07",
    )


def test_algo_overnight_quota_keeps_sunday_and_monday_in_one_session():
    trade_date = "2026-10-12"
    assert risk_manager._is_in_algo_session("2026-10-12T00:30:00Z", trade_date)
    assert risk_manager._is_in_algo_session("2026-10-12T04:30:00Z", trade_date)
    assert not risk_manager._is_in_algo_session("2026-10-11T00:30:00Z", trade_date)
    orders = [
        _order("sunday-entry", "buy", "filled", 10, "2026-10-12T00:30:00Z", 10),
        _order("monday-reentry", "buy", "filled", 5, "2026-10-12T04:30:00Z", 5),
    ]
    fills = [
        {"id": "fill-1", "order_id": "sunday-entry", "symbol": "AAPL", "side": "buy",
         "qty": "10", "transaction_time": "2026-10-12T00:31:00Z"},
        {"id": "fill-2", "order_id": "sunday-exit", "symbol": "AAPL", "side": "sell",
         "qty": "10", "transaction_time": "2026-10-12T02:00:00Z"},
        {"id": "fill-3", "order_id": "monday-reentry", "symbol": "AAPL", "side": "buy",
         "qty": "5", "transaction_time": "2026-10-12T04:31:00Z"},
    ]
    assert risk_manager._count_algo_new_entries_today(
        orders, fills, {"AAPL": {"qty": 5}}, trade_date,
    ) == 2


def test_algo_order_history_uses_cursor_when_first_page_is_full(monkeypatch):
    first_page = [{"id": f"order-{index}"} for index in range(500)]
    calls = []

    def fake_api(endpoint, mode):
        calls.append(endpoint)
        return first_page if len(calls) == 1 else [{"id": "order-500"}]

    monkeypatch.setattr(risk_manager, "_api", fake_api)

    orders = risk_manager._get_algo_orders_since("2026-10-07T04:00:00Z", "algo")

    assert len(orders) == 501
    assert "after=2026-10-07T04:00:00Z" in calls[0]
    assert "after_order_id=order-499" in calls[1]


def test_algo_entry_count_does_not_count_exit_orders():
    orders = [_order("exit", "sell", "filled", 10, f"{SESSION_DATE}T14:00:00Z", 10)]
    fills = [_fill("exit", "sell", 10, "14:01")]

    count = risk_manager._count_algo_new_entries_today(orders, fills, {}, SESSION_DATE)

    assert count == 0


def test_algo_entry_quota_blocks_new_positions_when_history_is_unavailable():
    state = _portfolio_state(entry_count_today=None, entry_count_available=False)

    result = risk_manager.validate_trade("AAPL", "buy", 10, 100, state, mode="algo")

    assert not result.approved
    assert result.rule == "entry_count_unavailable"


def test_algo_risk_manager_blocks_the_tenth_shared_daily_entry():
    state = _portfolio_state(entry_count_today=10, trade_count_today=10)

    result = risk_manager.validate_trade("AAPL", "buy", 1, 100, state, mode="algo")

    assert not result.approved
    assert result.rule == "max_trades_per_day"


@pytest.mark.parametrize("action", ["buy", "short"])
@pytest.mark.parametrize("strategy", ["multi_factor", "event_driven"])
def test_risk_manager_rejects_satellite_position_ownership(action, strategy):
    result = risk_manager.validate_trade(
        "AAPL", action, 1, 100, _portfolio_state(), mode="algo", strategy=strategy,
    )

    assert not result.approved
    assert result.rule == "satellite_confirmation_only"


def test_algo_entry_quota_does_not_block_scale_ins_at_limit_or_missing_history():
    position = {"AAPL": {"qty": 10, "market_value": 1_000.0}}
    state = _portfolio_state(
        cash=90_000.0,
        positions=position,
        sector_alloc={"core_tech": 1_000.0},
        trade_count_today=40,
        entry_count_today=None,
        entry_count_available=False,
        open_position_count=1,
    )

    result = risk_manager.validate_trade("AAPL", "buy", 10, 100, state, mode="algo")

    assert result.approved


def test_stage_approved_trade_counts_only_a_new_algo_position():
    state = _portfolio_state()
    first = risk_manager.stage_approved_trade(state, "AAPL", "buy", 10, 100, mode="algo")
    scale_in = risk_manager.stage_approved_trade(first, "AAPL", "buy", 10, 100, mode="algo")

    assert first["entry_count_today"] == 1
    assert scale_in["entry_count_today"] == 1
    assert scale_in["trade_count_today"] == 2


def test_trend_following_generates_a_fast_over_slow_crossover():
    bars = _bars([100] * 21 + [110])

    signal = trend_following.signal("AAPL", bars, fast_period=9, slow_period=21)

    assert signal is not None
    assert (signal.ticker, signal.action, signal.strategy) == ("AAPL", "buy", "trend_following")


def test_core_rules_emit_mirrored_short_entries():
    trend_bars = _bars([110] * 21 + [100])
    trend = trend_following.signal(
        "AAPL", trend_bars, fast_period=9, slow_period=21, allow_short=True,
    )
    momentum_bars = _bars([100] * 20 + [94], [1_000] * 20 + [2_000])
    momentum_signal = momentum.signal(
        "AAPL", momentum_bars, lookback=5, minimum_return=0.04, allow_short=True,
    )
    mean_bars = _bars([100] * 20 + [110, 109])
    mean_signal = mean_reversion.signal("AAPL", mean_bars, allow_short=True)
    opening = [{"h": 100, "l": 95, "c": 99, "v": 100} for _ in range(6)]
    orb = opening_range_breakout.signal(
        "AAPL", opening + [{"h": 99, "l": 93, "c": 94, "v": 200}], allow_short=True,
    )

    assert trend is not None and trend.action == "short"
    assert momentum_signal is not None and momentum_signal.action == "short"
    assert mean_signal is not None and mean_signal.action == "short"
    assert orb is not None and orb.action == "short"


def test_algo_opening_rip_short_uses_mirrored_patient_filters():
    from src.strategies.rule_pipeline import evaluate_rule_signal

    bars = [
        {"t": f"2026-10-07T13:{minute:02d}:00Z", "o": opening, "h": high,
         "l": low, "c": close, "v": volume}
        for minute, opening, high, low, close, volume in (
            (30, 100, 100.5, 99.8, 100, 100),
            (35, 100, 102, 99.9, 101, 100),
            (40, 101, 103, 100.8, 102.1, 100),
            (45, 102, 102.7, 101.8, 102.1, 100),
            (50, 102.1, 102.4, 101.7, 102, 200),
        )
    ]

    signal = evaluate_rule_signal(
        "mean_reversion", "AAPL", bars,
    )

    assert signal is not None
    assert signal.action == "short"
    assert "opening rip" in signal.reason


def test_momentum_requires_return_and_volume_confirmation():
    closes = [100] * 20 + [106]
    bars = _bars(closes, [1_000] * 20 + [2_000])

    signal = momentum.signal("AAPL", bars, lookback=20, minimum_return=0.04)

    assert signal is not None
    assert signal.action == "buy"
    assert "volume 2.00x" in signal.reason


def test_mean_reversion_requires_a_lower_band_reversal():
    bars = _bars([100] * 17 + [97, 94, 91, 90, 90.2])

    signal = mean_reversion.signal("AAPL", bars)

    assert signal is not None
    assert signal.action == "buy"
    assert "lower-band reversal" in signal.reason


def test_opening_range_breakout_requires_a_close_and_volume_above_range():
    opening = [{"h": 100, "l": 95, "c": 99, "v": 100} for _ in range(6)]
    bars = opening + [{"h": 102, "l": 99, "c": 101, "v": 200}]

    signal = opening_range_breakout.signal("AAPL", bars)

    assert signal is not None
    assert signal.action == "buy"
    assert signal.strategy == "opening_range_breakout"


def test_pairs_module_reports_spread_signal_without_submitting_orders():
    left = [100 + index * 0.1 for index in range(60)]
    right = [2 * value for value in left]
    right[-1] *= 1.02

    signal = pairs.candidate("AAA", _bars(left), "BBB", _bars(right), window=60)

    assert signal is not None
    assert signal.strategy == "pairs_stat_arb"
    assert signal.long_ticker == "AAA"
    assert signal.short_ticker == "BBB"


def test_regime_filter_prefers_trend_strategies_in_a_rising_market():
    prices = [100 * (1.005**index) for index in range(60)]
    regime = rank_strategies(_bars(prices), mode="algo")

    assert regime["regime"] == "trending_up"
    assert regime["active"][0] == "trend_following"
    assert "opening_range_breakout" in regime["active"]


@pytest.mark.parametrize(
    ("name", "flag"),
    [("intraday_trend", "enable_intraday_trend"),
     ("intraday_momentum", "enable_intraday_momentum")],
)
def test_legacy_five_minute_satellites_are_opt_in_and_smaller(name, flag, monkeypatch):
    monkeypatch.setitem(STRATEGY_CONFIG, flag, False)
    monkeypatch.setattr(
        rule_pipeline, "evaluate_strategy",
        lambda strategy, *_args, **_kwargs: Signal("AAPL", "buy", strategy, "test"),
    )
    assert rule_pipeline.evaluate_rule_signal(name, "AAPL", [], {"enabled": True}) is None

    monkeypatch.setitem(STRATEGY_CONFIG, flag, True)
    signal = rule_pipeline.evaluate_rule_signal(name, "AAPL", [], {"enabled": True})

    assert signal is not None
    assert signal.strategy == name
    assert signal.strategy_group == "intraday_satellite"
    assert signal.target_weight == pytest.approx(0.01)
    assert signal.risk_multiplier == pytest.approx(0.25)


def test_state_machine_owns_pending_and_open_positions_until_reconciled():
    states = {}
    owners = {}
    machine = RuleStrategyStateMachine(
        states, owners, minimum_hold_minutes={"momentum": 20},
    )
    trade = {
        "strategy": "momentum",
        "ticker": "aapl",
        "action": "buy",
        "client_order_id": "momentum-aapl-1",
        "signal_bar_time": "2026-10-07T14:00:00Z",
    }

    machine.prepare(trade)
    assert states["momentum"]["AAPL"]["phase"] == ENTRY_PENDING
    assert not machine.allows("mean_reversion", "AAPL", "buy", "2026-10-07T14:01:00Z")

    machine.reconcile({"AAPL": {"qty": 10}}, [])

    assert states["momentum"]["AAPL"]["phase"] == OPEN
    assert owners["AAPL"] == "momentum"
    assert not machine.allows("momentum", "AAPL", "sell", "2026-10-07T14:05:00Z")
    assert machine.allows("momentum", "AAPL", "sell", "2026-10-07T14:25:00Z")

    machine.reconcile({}, [])
    assert states["momentum"]["AAPL"]["phase"] in {COOLDOWN, OPEN}
    assert "AAPL" not in owners


def test_state_machine_reconciles_short_direction_and_enforces_minimum_hold():
    states = {}
    owners = {}
    machine = RuleStrategyStateMachine(
        states, owners, minimum_hold_minutes={"momentum": 20},
    )
    trade = {
        "strategy": "momentum", "ticker": "AAPL", "action": "short",
        "client_order_id": "momentum-aapl-short-1",
        "signal_bar_time": "2026-10-07T14:00:00Z",
        "stop_price": 101.0, "take_profit": 98.0,
    }

    machine.prepare(trade)
    machine.reconcile({"AAPL": {"qty": -10}}, [])

    assert states["momentum"]["AAPL"]["phase"] == OPEN
    assert owners["AAPL"] == "momentum"
    assert not machine.allows("momentum", "AAPL", "cover", "2026-10-07T14:20:00Z")
    assert machine.allows("momentum", "AAPL", "cover", "2026-10-07T14:25:00Z")
    assert not machine.allows("momentum", "AAPL", "sell", "2026-10-07T14:25:00Z")

    machine.prepare({**trade, "action": "cover", "signal_bar_time": "2026-10-07T14:25:00Z"})
    assert states["momentum"]["AAPL"]["phase"] == "exit_pending"


def test_strategy_state_machine_persists_software_stop_and_target():
    states = {}
    owners = {}
    machine = RuleStrategyStateMachine(states, owners)
    trade = {
        "strategy": "mean_reversion",
        "ticker": "AAPL",
        "action": "buy",
        "client_order_id": "mean-reversion-aapl-1",
        "signal_bar_time": "2026-10-07T14:00:00Z",
        "stop_price": 98.0,
        "take_profit": 104.0,
    }

    machine.prepare(trade)

    assert states["mean_reversion"]["AAPL"]["stop_price"] == 98.0
    assert states["mean_reversion"]["AAPL"]["take_profit"] == 104.0
