from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.algo.backtest import AlgoPortfolioBacktest, _regular_session_bars
import src.algo.backtest as backtest_module
from src.algo.market_store import SQLiteBarStore
from src.algo.slippage_sensitivity import parse_slippage_grid
from src.algo.research.adaptive import walk_forward_windows
from src.algo.strategies.rule_pipeline import evaluate_rule_signal
from src.algo.strategies.registry import validate_parameter_profile
from src.algo.strategies.signals import Signal


def _utc(day: date, hour: int, minute: int) -> str:
    from zoneinfo import ZoneInfo

    local = datetime.combine(day, time(hour, minute), tzinfo=ZoneInfo("America/New_York"))
    return local.astimezone(timezone.utc).isoformat()


def test_market_store_upserts_plus_sip_bars(tmp_path):
    store = SQLiteBarStore(tmp_path / "bars.sqlite3")
    timestamp = "2026-10-05T13:30:00Z"
    first = {"AAPL": [{"t": timestamp, "o": 100, "h": 101, "l": 99, "c": 100, "v": 10}]}
    updated = {"AAPL": [{"t": timestamp, "o": 100, "h": 102, "l": 99, "c": 101, "v": 20}]}

    store.write_bars(first, feed="sip", timeframe="5Min", data_tier="algo_trader_plus")
    store.write_bars(updated, feed="sip", timeframe="5Min", data_tier="algo_trader_plus")

    sip = store.bars(["AAPL"], feed="sip", timeframe="5Min", start="2026-10-05", end="2026-10-05")["AAPL"]
    assert len(sip) == 1
    assert sip[0]["c"] == 101
    assert len(store.coverage()) == 1


def test_profile_can_change_rule_parameters_but_not_portfolio_risk():
    momentum = validate_parameter_profile({"momentum": {"lookbacks": [5, 10]}})
    assert momentum["momentum"]["lookbacks"] == [5, 10]
    assert validate_parameter_profile({
        "mean_reversion": {"signal_model": "opening_dip", "minimum_dip_pct": 0.01},
    })["mean_reversion"]["signal_model"] == "opening_dip"
    with pytest.raises(ValueError, match="Unknown or malformed strategy"):
        validate_parameter_profile({"risk": {"risk_per_trade_pct": 0.5}})
    with pytest.raises(ValueError, match="fast_period"):
        validate_parameter_profile({"trend_following": {"fast_period": 21, "slow_period": 9}})
    with pytest.raises(ValueError, match="signal_model"):
        validate_parameter_profile({"mean_reversion": {"signal_model": "llm"}})


def test_rolling_verification_windows_are_chronological_and_non_overlapping():
    sessions = [date(2026, 1, 5) + timedelta(days=index) for index in range(8)]
    windows = walk_forward_windows(sessions, train_days=3, test_days=2, step_days=2)

    assert [(item["train_start"], item["train_end"], item["test_start"], item["test_end"])
            for item in windows] == [
        ("2026-01-05", "2026-01-07", "2026-01-08", "2026-01-09"),
        ("2026-01-07", "2026-01-09", "2026-01-10", "2026-01-11"),
        ("2026-01-09", "2026-01-11", "2026-01-12", "2026-01-12"),
    ]
    with pytest.raises(ValueError, match="must be at least test_days"):
        walk_forward_windows(sessions, train_days=3, test_days=3, step_days=2)




def test_slippage_grid_is_sorted_and_rejects_invalid_values():
    assert parse_slippage_grid("5,0,10") == [0, 5, 10]
    with pytest.raises(ValueError):
        parse_slippage_grid("5,-1")


def test_regular_session_replay_rejects_boats_feed():
    with pytest.raises(ValueError, match="BOATS requires its own overnight model"):
        AlgoPortfolioBacktest(
            symbols=["AAPL"], intraday_bars={}, spy_daily_bars=[],
            start="2026-10-05", end="2026-10-05", feed="boats",
            data_adjustment="raw",
        )


def test_research_fetch_discards_unused_extended_and_invalid_bars():
    day = date(2026, 10, 5)
    bars, outside, invalid = _regular_session_bars({"AAPL": [
        {"t": _utc(day, 9, 30), "o": 100, "h": 101, "l": 99, "c": 100, "v": 10},
        {"t": _utc(day, 16, 0), "o": 0, "h": 0, "l": 0, "c": 0, "v": 10},
        {"t": _utc(day, 9, 35), "o": 100, "h": 99, "l": 99, "c": 100, "v": 10},
    ]})

    assert len(bars["AAPL"]) == 1
    assert outside == 1
    assert invalid == 1


def test_portfolio_replay_fills_next_bar_and_resolves_same_bar_bracket_conservatively(monkeypatch):
    replay_day = date(2026, 10, 5)
    daily_dates = []
    cursor = replay_day - timedelta(days=1)
    while len(daily_dates) < 25:
        if cursor.weekday() < 5:
            daily_dates.append(cursor)
        cursor -= timedelta(days=1)
    daily_dates.reverse()
    spy_daily = [
        {"t": f"{day.isoformat()}T20:00:00Z", "c": 100 + index * 0.25}
        for index, day in enumerate(daily_dates)
    ]

    bars = [
        {"t": _utc(replay_day, 9, 30), "o": 100, "h": 101.2, "l": 100, "c": 101, "v": 100},
        {"t": _utc(replay_day, 9, 35), "o": 102, "h": 102.5, "l": 101.8, "c": 102.2, "v": 100},
        {"t": _utc(replay_day, 9, 40), "o": 102.3, "h": 104, "l": 99, "c": 102.5, "v": 100},
    ]

    def deterministic_signal(strategy, ticker, history, parameters=None, position_qty=0, **kwargs):
        if strategy == "mean_reversion" and len(history) == 1:
            return Signal(ticker, "buy", strategy, "test trigger", 1.0)
        return None

    monkeypatch.setattr(backtest_module, "evaluate_rule_signal", deterministic_signal)
    replay = AlgoPortfolioBacktest(
        symbols=["AAPL"], intraday_bars={"AAPL": bars}, spy_daily_bars=spy_daily,
        start=replay_day.isoformat(), end=replay_day.isoformat(), feed="sip",
        data_adjustment="raw", initial_capital=100_000, slippage_bps=5,
    )

    report = replay.run()

    assert report["closed_trade_count"] == 1
    trade = report["closed_trades"][0]
    assert trade["signal_at"] == trade["entry_at"]
    assert trade["entry_at"] == _utc(replay_day, 9, 35)
    assert trade["entry_price"] > 102
    assert trade["exit_reason"] == "stop"
    assert trade["exit_price"] < 99.99
    assert report["open_position_count"] == 0
    assert report["entry_count"] == 1
    assert report["average_entries_per_session"] == 1
    assert report["average_holding_minutes"] == 5
    assert report["candidate_setup_count"] == 1
    assert report["candidate_ticker_day_count"] == 1


def test_portfolio_replay_models_short_entry_cover_and_entry_relative_return(monkeypatch):
    replay_day = date(2026, 10, 5)
    daily_dates = []
    cursor = replay_day - timedelta(days=1)
    while len(daily_dates) < 25:
        if cursor.weekday() < 5:
            daily_dates.append(cursor)
        cursor -= timedelta(days=1)
    daily_dates.reverse()
    spy_daily = [
        {"t": f"{day.isoformat()}T20:00:00Z", "c": 110 - index * 0.25}
        for index, day in enumerate(daily_dates)
    ]
    bars = [
        {"t": _utc(replay_day, 9, 30), "o": 100, "h": 100.5, "l": 99.5, "c": 100, "v": 100},
        {"t": _utc(replay_day, 9, 35), "o": 100, "h": 100.2, "l": 99.5, "c": 100, "v": 100},
        {"t": _utc(replay_day, 9, 40), "o": 99, "h": 99.5, "l": 97.5, "c": 98.5, "v": 100},
    ]

    def deterministic_signal(strategy, ticker, history, parameters=None, position_qty=0, **kwargs):
        if strategy != "mean_reversion":
            return None
        if position_qty == 0 and len(history) == 1:
            return Signal(ticker, "short", strategy, "test short trigger", 1.0)
        return None

    monkeypatch.setattr(backtest_module, "evaluate_rule_signal", deterministic_signal)
    replay = AlgoPortfolioBacktest(
        symbols=["AAPL"], intraday_bars={"AAPL": bars}, spy_daily_bars=spy_daily,
        start=replay_day.isoformat(), end=replay_day.isoformat(), feed="sip",
        data_adjustment="raw", initial_capital=100_000, slippage_bps=5,
    )

    report = replay.run()

    assert report["closed_trade_count"] == 1
    trade = report["closed_trades"][0]
    assert trade["direction"] == "short"
    assert trade["qty"] < 0
    assert trade["exit_reason"] == "target"
    assert trade["pnl"] > 0
    assert trade["return_pct"] == pytest.approx(
        round((trade["entry_price"] - trade["exit_price"]) / trade["entry_price"], 6),
    )


def test_opening_dip_research_rule_requires_window_dip_bounce_and_volume():
    day = date(2026, 10, 5)
    bars = [
        {"t": _utc(day, 9, 30), "o": 100, "h": 100.1, "l": 99.8, "c": 100, "v": 100},
        {"t": _utc(day, 9, 35), "o": 99.0, "h": 99.1, "l": 98.7, "c": 98.8, "v": 100},
        {"t": _utc(day, 9, 40), "o": 98.9, "h": 99.2, "l": 98.9, "c": 99.0, "v": 150},
    ]
    parameters = {
        "signal_model": "opening_dip", "minimum_dip_pct": 0.01,
        "minimum_volume_ratio": 1.2, "entry_window_minutes": 45,
        "minimum_bounce_pct": 0.001, "higher_low_bars": 1,
    }

    signal = evaluate_rule_signal("mean_reversion", "AAPL", bars, parameters)

    assert signal is not None
    assert signal.action == "buy"
    assert evaluate_rule_signal(
        "mean_reversion", "AAPL", bars,
        {**parameters, "minimum_dip_pct": 0.02},
    ) is None
    assert evaluate_rule_signal(
        "mean_reversion", "AAPL", bars,
        {**parameters, "entry_window_minutes": 5},
    ) is None


def test_research_controls_cannot_exceed_live_algo_ceilings():
    with pytest.raises(ValueError, match="between 1 and 10"):
        AlgoPortfolioBacktest(
            symbols=["AAPL"], intraday_bars={}, spy_daily_bars=[],
            start="2026-10-05", end="2026-10-05", feed="sip",
            data_adjustment="raw", research_controls={"max_entries_per_day": 41},
        )
    with pytest.raises(ValueError, match="between 1 and 5"):
        AlgoPortfolioBacktest(
            symbols=["AAPL"], intraday_bars={}, spy_daily_bars=[],
            start="2026-10-05", end="2026-10-05", feed="sip",
            data_adjustment="raw", research_controls={"max_positions_per_strategy": 6},
        )
    with pytest.raises(ValueError, match="live Algo cap of 1"):
        AlgoPortfolioBacktest(
            symbols=["AAPL"], intraday_bars={}, spy_daily_bars=[],
            start="2026-10-05", end="2026-10-05", feed="sip",
            data_adjustment="raw",
            research_controls={"max_entries_per_strategy_per_day": {"momentum": 3}},
        )


def test_research_daily_entry_cap_limits_combined_signals(monkeypatch):
    replay_day = date(2026, 10, 5)
    daily_dates = []
    cursor = replay_day - timedelta(days=1)
    while len(daily_dates) < 25:
        if cursor.weekday() < 5:
            daily_dates.append(cursor)
        cursor -= timedelta(days=1)
    daily_dates.reverse()
    spy_daily = [
        {"t": f"{day.isoformat()}T20:00:00Z", "c": 100 + index * 0.25}
        for index, day in enumerate(daily_dates)
    ]
    bars = [
        {"t": _utc(replay_day, hour, minute), "o": 100, "h": 100.2, "l": 99.8, "c": 100, "v": 100}
        for hour, minute in ((9, 30), (9, 35), (9, 40))
    ]

    def deterministic_signal(strategy, ticker, history, parameters=None, position_qty=0, **kwargs):
        return Signal(ticker, "buy", strategy, "test trigger", 0.9) if strategy == "mean_reversion" else None

    monkeypatch.setattr(backtest_module, "evaluate_rule_signal", deterministic_signal)
    replay = AlgoPortfolioBacktest(
        symbols=["MSFT", "AAPL"],
        intraday_bars={"MSFT": bars, "AAPL": bars},
        spy_daily_bars=spy_daily,
        start=replay_day.isoformat(), end=replay_day.isoformat(), feed="sip",
        data_adjustment="raw", research_controls={"max_entries_per_day": 1},
        strategies=["mean_reversion"],
    )

    report = replay.run()

    assert report["entry_count"] == 1
    assert report["max_entries_per_session"] == 1
    assert report["risk_rejections"]["research_max_entries_per_day"] >= 1


def test_strategy_specific_cooldown_only_blocks_the_exited_strategy(monkeypatch):
    replay_day = date(2026, 10, 5)
    spy_daily = [
        {"t": f"2026-09-{index + 1:02d}T20:00:00Z", "c": 100 + index}
        for index in range(25)
    ]
    replay = AlgoPortfolioBacktest(
        symbols=["AAPL"], intraday_bars={}, spy_daily_bars=spy_daily,
        start=replay_day.isoformat(), end=replay_day.isoformat(), feed="sip",
        data_adjustment="raw", strategies=["mean_reversion", "trend_following"],
        research_controls={"cooldown_minutes_by_strategy": {"mean_reversion": 60}},
    )
    replay.day_key = replay_day.isoformat()
    replay._current_regime = {"regime": "range_bound"}
    timestamp = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(backtest_module, "validate_trade", lambda **_: SimpleNamespace(approved=True))

    replay.last_exit_at_by_strategy[("AAPL", "mean_reversion")] = timestamp - timedelta(minutes=30)
    replay._queue_signal(
        "AAPL", {"ticker": "AAPL", "action": "buy", "strategy": "mean_reversion", "reason": "test"},
        {"c": 100}, timestamp,
    )
    assert replay.rejections["research_ticker_cooldown"] == 1

    replay._queue_signal(
        "AAPL", {"ticker": "AAPL", "action": "buy", "strategy": "trend_following", "reason": "test"},
        {"c": 100}, timestamp,
    )
    assert replay.pending_entries["AAPL"]["strategy"] == "trend_following"
