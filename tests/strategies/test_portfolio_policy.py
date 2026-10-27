import json

import pytest

from execution import risk as risk_validator
import portfolio.config as portfolio_config
from portfolio.config import ALGO_MODE_CONFIG, STRATEGY_CONFIG, get_algo_configuration, get_mode_config
from strategies import mean_reversion, momentum, trend_following
from strategies.regime import rank_strategies
from strategies.signals import Signal
from strategies.runner.sizing import _make_trade, _update_trailing_stop
import strategies.rule_pipeline as rule_pipeline
from strategies.state_machine import COOLDOWN, ENTRY_PENDING, OPEN, RuleStrategyStateMachine


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


def test_algo_risk_profile_matches_live_coordinator_limits():
    assert ALGO_MODE_CONFIG["risk"]["max_open_positions"] == 18
    assert ALGO_MODE_CONFIG["risk"]["max_trades_per_day"] == 10
    assert ALGO_MODE_CONFIG["risk"]["max_position_pct"] == 0.07
    assert ALGO_MODE_CONFIG["risk"]["max_strategy_position_pct"] == 0.07
    assert ALGO_MODE_CONFIG["risk"]["allow_short_positions"] is True
    assert ALGO_MODE_CONFIG["risk"]["max_short_positions"] == 5
    assert ALGO_MODE_CONFIG["risk"]["max_short_exposure_pct"] == 0.20
    assert ALGO_MODE_CONFIG["risk"]["max_sector_pct"] == 0.40
    assert ALGO_MODE_CONFIG["risk"]["min_cash_pct"] == 0.10
    assert "max_open_positions" not in ALGO_MODE_CONFIG["risk"]["strategy_group_limits"]["swing"]
    assert ALGO_MODE_CONFIG["risk"]["strategy_group_limits"]["swing"]["max_entries_per_day"] == 6
    assert "max_open_positions" not in ALGO_MODE_CONFIG["risk"]["strategy_group_limits"]["intraday_core"]
    assert ALGO_MODE_CONFIG["risk"]["strategy_group_limits"]["intraday_core"]["max_entries_per_day"] == 4
    assert ALGO_MODE_CONFIG["risk"]["max_positions_per_strategy_by_strategy"]["swing_multi_factor"] == 7
    assert ALGO_MODE_CONFIG["risk"]["max_entries_per_strategy_per_day"]["swing_multi_factor"] == 2
    assert ALGO_MODE_CONFIG["risk"]["max_positions_per_strategy_by_strategy"]["swing_llm_driven"] == 7
    assert ALGO_MODE_CONFIG["risk"]["max_entries_per_strategy_per_day"]["swing_llm_driven"] == 5
    assert ALGO_MODE_CONFIG["risk"]["max_entries_per_strategy_per_day"]["intraday_mean_reversion"] == 4
    config = get_algo_configuration()
    assert config["general"]["overnight_entries_enabled"] is True
    assert config["strategies"]["intraday_trend_following"]["enabled"] is True
    assert config["strategies"]["intraday_momentum"]["enabled"] is True
    assert ALGO_MODE_CONFIG["risk"]["stop_loss_pct_by_strategy"]["swing_trend_following"] > 0.05
    assert ALGO_MODE_CONFIG["risk"]["stop_loss_pct_by_strategy"]["intraday_mean_reversion"] <= 0.012
    assert STRATEGY_CONFIG["extended_hours_max_spread_pct"] == 0.02
    with pytest.raises(ValueError, match="Only the algo account route"):
        get_mode_config("unknown")


def test_algo_stop_sizing_uses_horizon_specific_config():
    risk = ALGO_MODE_CONFIG["risk"]
    intraday = _make_trade({
        "ticker": "AAPL", "action": "buy", "strategy": "intraday_mean_reversion",
        "reason": "intraday setup", "risk_multiplier": 1.0,
    }, 100, 100_000, {}, risk)
    swing = _make_trade({
        "ticker": "AAPL", "action": "buy", "strategy": "swing_trend_following",
        "reason": "swing setup", "risk_multiplier": 1.0, "stop_pct": 0.01,
    }, 100, 100_000, {}, risk)

    assert intraday["stop_price"] == 98.9
    assert swing["stop_price"] == 94.0
    assert swing["trailing_stop_pct"] > intraday["trailing_stop_pct"]


def test_algo_trade_sizing_respects_ticker_universe_cap():
    mode_config = get_mode_config("algo")
    risk = mode_config["risk"]
    trade = _make_trade({
        "ticker": "PLTR", "action": "buy", "strategy": "swing_trend_following",
        "reason": "swing setup", "risk_multiplier": 1.0, "target_weight": 0.04,
    }, 200, 100_000, {}, risk, mode_universe=mode_config["universe"])

    assert trade is not None
    assert trade["qty"] * trade["entry_price"] <= (
        100_000 * mode_config["universe"]["tactical"]["max_per_stock_pct"]
    )
    state = {
        "equity": 100_000, "cash": 100_000, "available_cash": 100_000,
        "daily_pnl_pct": 0, "weekly_pnl_pct": 0,
        "positions": {}, "sector_alloc": {}, "open_position_count": 0,
        "entry_count_today": 0, "entry_count_available": True,
        "trade_count_today": 0, "pending_buy_value_by_ticker": {},
        "pending_exposure_available": True,
    }
    result = risk_validator.validate_trade(
        ticker=trade["ticker"], action=trade["action"], qty=trade["qty"],
        entry_price=trade["entry_price"], portfolio_state=state, mode="algo",
    )
    assert result.approved, result.reason


def test_algo_software_trails_tighten_for_both_directions():
    risk = ALGO_MODE_CONFIG["risk"]
    long_state = {"stop_price": 94.0}
    short_state = {"stop_price": 106.0}

    long_stop = _update_trailing_stop(
        long_state, strategy="swing_trend_following", qty=10, entry_price=100,
        current_price=104, mode_risk=risk,
    )
    short_stop = _update_trailing_stop(
        short_state, strategy="swing_trend_following", qty=-10, entry_price=100,
        current_price=96, mode_risk=risk,
    )

    assert long_stop > 94.0
    assert short_stop < 106.0


def test_algo_entry_count_ignores_scale_ins_to_existing_positions():
    orders = [_order("scale-in", "buy", "filled", 10, f"{SESSION_DATE}T14:00:00Z", 10)]
    fills = [_fill("scale-in", "buy", 10, "14:01")]

    count = risk_validator._count_algo_new_entries_today(
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

    count = risk_validator._count_algo_new_entries_today(
        orders, fills, {"AAPL": {"qty": 5}}, SESSION_DATE,
    )

    assert count == 2


def test_algo_entry_count_reserves_slot_for_pending_opening_order():
    orders = [_order("pending", "buy", "new", 10, f"{SESSION_DATE}T14:00:00Z")]

    count = risk_validator._count_algo_new_entries_today(orders, [], {}, SESSION_DATE)

    assert count == 1


def test_algo_order_timestamps_use_coordinator_trade_date():
    assert risk_validator._is_in_algo_session(
        "2026-10-08T02:15:00Z", "2026-10-08",
    )
    assert not risk_validator._is_in_algo_session(
        "2026-10-08T02:15:00Z", "2026-10-07",
    )


def test_algo_overnight_quota_keeps_sunday_and_monday_in_one_session():
    trade_date = "2026-10-12"
    assert risk_validator._is_in_algo_session("2026-10-12T00:30:00Z", trade_date)
    assert risk_validator._is_in_algo_session("2026-10-12T04:30:00Z", trade_date)
    assert not risk_validator._is_in_algo_session("2026-10-11T00:30:00Z", trade_date)
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
    assert risk_validator._count_algo_new_entries_today(
        orders, fills, {"AAPL": {"qty": 5}}, trade_date,
    ) == 2


def test_algo_order_history_uses_cursor_when_first_page_is_full(monkeypatch):
    first_page = [{"id": f"order-{index}"} for index in range(500)]
    calls = []

    def fake_api(endpoint, mode):
        calls.append(endpoint)
        return first_page if len(calls) == 1 else [{"id": "order-500"}]

    monkeypatch.setattr(risk_validator, "_api", fake_api)

    orders = risk_validator._get_algo_orders_since("2026-10-07T04:00:00Z", "algo")

    assert len(orders) == 501
    assert "after=2026-10-07T04:00:00Z" in calls[0]
    assert "after_order_id=order-499" in calls[1]


def test_algo_entry_count_does_not_count_exit_orders():
    orders = [_order("exit", "sell", "filled", 10, f"{SESSION_DATE}T14:00:00Z", 10)]
    fills = [_fill("exit", "sell", 10, "14:01")]

    count = risk_validator._count_algo_new_entries_today(orders, fills, {}, SESSION_DATE)

    assert count == 0


def test_algo_entry_quota_blocks_new_positions_when_history_is_unavailable():
    state = _portfolio_state(entry_count_today=None, entry_count_available=False)

    result = risk_validator.validate_trade("AAPL", "buy", 10, 100, state, mode="algo")

    assert not result.approved
    assert result.rule == "entry_count_unavailable"


def test_algo_risk_validator_blocks_the_tenth_shared_daily_entry():
    state = _portfolio_state(entry_count_today=10, trade_count_today=10)

    result = risk_validator.validate_trade("AAPL", "buy", 1, 100, state, mode="algo")

    assert not result.approved
    assert result.rule == "max_trades_per_day"


@pytest.mark.parametrize("action", ["buy", "short"])
def test_risk_validator_allows_llm_driven_to_own_a_position(action):
    state = _portfolio_state(
        shortability_available=True,
        easy_to_borrow_symbols=["AAPL"],
        account_shorting_enabled=True,
    )
    result = risk_validator.validate_trade(
        "AAPL", action, 1, 100, state, mode="algo", strategy="swing_llm_driven",
    )

    assert result.approved


def test_risk_validator_allows_multi_factor_to_own_a_core_entry():
    result = risk_validator.validate_trade(
        "AAPL", "buy", 1, 100, _portfolio_state(), mode="algo", strategy="swing_multi_factor",
    )

    assert result.approved


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

    result = risk_validator.validate_trade("AAPL", "buy", 10, 100, state, mode="algo")

    assert result.approved


def test_stage_approved_trade_counts_only_a_new_algo_position():
    state = _portfolio_state()
    first = risk_validator.stage_approved_trade(state, "AAPL", "buy", 10, 100, mode="algo")
    scale_in = risk_validator.stage_approved_trade(first, "AAPL", "buy", 10, 100, mode="algo")

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
    assert trend is not None and trend.action == "short"
    assert momentum_signal is not None and momentum_signal.action == "short"
    assert mean_signal is not None and mean_signal.action == "short"


def test_algo_opening_rip_short_uses_mirrored_patient_filters():
    from strategies.rule_pipeline import evaluate_rule_signal

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
        "intraday_mean_reversion", "AAPL", bars,
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


def test_regime_filter_prefers_trend_strategies_in_a_rising_market():
    prices = [100 * (1.005**index) for index in range(60)]
    regime = rank_strategies(_bars(prices), mode="algo")

    assert regime["regime"] == "trending_up"
    assert regime["active"][0] == "swing_trend_following"


@pytest.mark.parametrize("name", ["intraday_trend_following", "intraday_momentum"])
def test_intraday_strategies_use_configured_enable_and_risk_limits(name, monkeypatch, tmp_path):
    config_path = tmp_path / "algo-strategy-config.json"
    monkeypatch.setattr(portfolio_config, "ALGO_STRATEGY_CONFIG_PATH", config_path)
    config_path.write_text(json.dumps({"strategies": {name: {"enabled": False}}}))
    monkeypatch.setattr(
        rule_pipeline, "evaluate_strategy",
        lambda strategy, *_args, **_kwargs: Signal("AAPL", "buy", strategy, "test"),
    )
    assert rule_pipeline.evaluate_rule_signal(name, "AAPL", [], {"enabled": True}) is None

    config_path.write_text(json.dumps({"strategies": {name: {"enabled": True}}}))
    signal = rule_pipeline.evaluate_rule_signal(name, "AAPL", [], {"enabled": True})

    assert signal is not None
    assert signal.strategy == name
    assert signal.strategy_group == "intraday_core"
    configured = portfolio_config.get_algo_strategy_settings(name)
    assert signal.target_weight == pytest.approx(configured["max_position_pct"])
    assert signal.risk_multiplier == pytest.approx(configured["risk_multiplier"])


def test_state_machine_owns_pending_and_open_positions_until_reconciled():
    states = {}
    owners = {}
    machine = RuleStrategyStateMachine(
        states, owners, minimum_hold_minutes={"swing_momentum": 20},
    )
    trade = {
        "strategy": "swing_momentum",
        "ticker": "aapl",
        "action": "buy",
        "client_order_id": "momentum-aapl-1",
        "signal_bar_time": "2026-10-07T14:00:00Z",
    }

    machine.prepare(trade)
    assert states["swing_momentum"]["AAPL"]["phase"] == ENTRY_PENDING
    assert not machine.allows("intraday_mean_reversion", "AAPL", "buy", "2026-10-07T14:01:00Z")

    machine.reconcile({"AAPL": {"qty": 10}}, [])

    assert states["swing_momentum"]["AAPL"]["phase"] == OPEN
    assert owners["AAPL"] == "swing_momentum"
    assert not machine.allows("swing_momentum", "AAPL", "sell", "2026-10-07T14:05:00Z")
    assert machine.allows("swing_momentum", "AAPL", "sell", "2026-10-07T14:25:00Z")

    machine.reconcile({}, [])
    assert states["swing_momentum"]["AAPL"]["phase"] in {COOLDOWN, OPEN}
    assert "AAPL" not in owners


def test_state_machine_reconciles_short_direction_and_enforces_minimum_hold():
    states = {}
    owners = {}
    machine = RuleStrategyStateMachine(
        states, owners, minimum_hold_minutes={"swing_momentum": 20},
    )
    trade = {
        "strategy": "swing_momentum", "ticker": "AAPL", "action": "short",
        "client_order_id": "momentum-aapl-short-1",
        "signal_bar_time": "2026-10-07T14:00:00Z",
        "stop_price": 101.0, "take_profit": 98.0,
    }

    machine.prepare(trade)
    machine.reconcile({"AAPL": {"qty": -10}}, [])

    assert states["swing_momentum"]["AAPL"]["phase"] == OPEN
    assert owners["AAPL"] == "swing_momentum"
    assert not machine.allows("swing_momentum", "AAPL", "cover", "2026-10-07T14:20:00Z")
    assert machine.allows("swing_momentum", "AAPL", "cover", "2026-10-07T14:25:00Z")
    assert not machine.allows("swing_momentum", "AAPL", "sell", "2026-10-07T14:25:00Z")

    machine.prepare({**trade, "action": "cover", "signal_bar_time": "2026-10-07T14:25:00Z"})
    assert states["swing_momentum"]["AAPL"]["phase"] == "exit_pending"


def test_strategy_state_machine_persists_software_stop_and_target():
    states = {}
    owners = {}
    machine = RuleStrategyStateMachine(states, owners)
    trade = {
        "strategy": "intraday_mean_reversion",
        "ticker": "AAPL",
        "action": "buy",
        "client_order_id": "mean-reversion-aapl-1",
        "signal_bar_time": "2026-10-07T14:00:00Z",
        "stop_price": 98.0,
        "take_profit": 104.0,
    }

    machine.prepare(trade)

    assert states["intraday_mean_reversion"]["AAPL"]["stop_price"] == 98.0
    assert states["intraday_mean_reversion"]["AAPL"]["take_profit"] == 104.0


def test_registry_drops_profile_keys_the_signal_does_not_accept(monkeypatch, caplog):
    import logging
    from strategies import registry

    seen = []

    def fake_signal(ticker, bars, *, position_qty=0, fast_period=20, allow_long=True, allow_short=False):
        seen.append({"fast_period": fast_period, "allow_long": allow_long})
        return None

    monkeypatch.setattr(registry.ALGO_RULE_MODULES["intraday_trend_following"], "signal", fake_signal)
    registry._DROPPED_PROFILE_KEYS_LOGGED.clear()
    profile = {
        "enabled": True, "fast_period": 9, "confirmation_bars": 3,
        "minimum_slope_pct": 0.1, "bogus_setting": 1,
    }

    with caplog.at_level(logging.WARNING, logger="strategies.registry"):
        registry.evaluate("intraday_trend_following", "algo", "AAPL", [], parameters=profile, runtime_enabled_override=True)
        registry.evaluate("intraday_trend_following", "algo", "AAPL", [], parameters=profile, runtime_enabled_override=True)

    assert seen == [{"fast_period": 9, "allow_long": True}] * 2
    dropped_warnings = [r for r in caplog.records if "bogus_setting" in r.getMessage()]
    assert len(dropped_warnings) == 1


def test_promoted_profile_with_unknown_and_confirmation_keys_does_not_raise_on_real_signal():
    from strategies import registry

    profile = {
        "enabled": True, "fast_period": 9, "slow_period": 21,
        "confirmation_bars": 2, "minimum_slope_pct": 0.05, "unknown_key": "x",
    }

    assert registry.evaluate(
        "intraday_trend_following", "algo", "AAPL", [], parameters=profile, runtime_enabled_override=True,
    ) is None


def test_accepted_profile_keys_pass_through_unchanged():
    from strategies import registry
    from strategies.trend_following.intraday import signal as intraday_signal

    values = {"fast_period": 9, "slow_period": 21, "allow_long": False, "unknown": 1}

    accepted = registry._accepted_signal_kwargs(intraday_signal, values, "intraday_trend_following")

    assert accepted == {"fast_period": 9, "slow_period": 21, "allow_long": False}


@pytest.mark.parametrize(
    "config_cls_path",
    [
        "strategies.trend_following.swing:SwingTrendConfig",
        "strategies.momentum.swing:SwingMomentumConfig",
        "strategies.mean_reversion.swing:SwingMeanReversionConfig",
        "strategies.multi_factor.config:MultiFactorConfig",
    ],
)
def test_retired_profile_fields_are_gone_from_defaults(config_cls_path):
    import importlib
    from strategies.retired_profile_keys import RETIRED_PROFILE_KEYS

    module_name, class_name = config_cls_path.split(":")
    config_cls = getattr(importlib.import_module(module_name), class_name)

    assert not (RETIRED_PROFILE_KEYS & set(config_cls().to_dict()))


def test_stored_profiles_with_retired_fields_still_load_with_one_warning(caplog):
    import logging
    from strategies import registry
    from strategies import retired_profile_keys
    from strategies.mean_reversion.swing import SwingMeanReversionConfig

    retired_profile_keys._WARNED.clear()
    legacy = {
        "swing_trend_following": {"cooldown_days": 5, "max_positions": 7, "max_entries_per_day": 2, "fast_period": 20},
        "swing_mean_reversion": {"cooldown_days": 5, "max_positions": 7, "max_entries_per_day": 2, "lookback_days": 15},
    }

    with caplog.at_level(logging.WARNING, logger="strategies.retired_profile_keys"):
        first = registry.validate_parameter_profile(legacy)
        second = registry.validate_parameter_profile(legacy)

    assert first["swing_trend_following"]["fast_period"] == 20
    assert "cooldown_days" not in first["swing_trend_following"]
    assert "max_entries_per_day" not in first["swing_mean_reversion"]
    assert first == second
    warnings = [r for r in caplog.records if "retired" in r.getMessage()]
    assert len(warnings) == 2
    assert SwingMeanReversionConfig.from_dict({"cooldown_days": 4, "lookback_days": 15}).lookback_days == 15
