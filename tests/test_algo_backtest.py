from datetime import date, datetime, time, timedelta, timezone

import pytest

from src.algo.backtest import AlgoPortfolioBacktest
from src.algo.market_store import SQLiteBarStore
from src.algo.slippage_sensitivity import parse_slippage_grid
from src.algo.walk_forward import walk_forward_windows
from src.strategies.registry import validate_parameter_profile
from src.strategies.signals import Signal


def _utc(day: date, hour: int, minute: int) -> str:
    from zoneinfo import ZoneInfo

    local = datetime.combine(day, time(hour, minute), tzinfo=ZoneInfo("America/New_York"))
    return local.astimezone(timezone.utc).isoformat()


def test_market_store_keeps_basic_and_plus_feeds_separate_and_upserts(tmp_path):
    store = SQLiteBarStore(tmp_path / "bars.sqlite3")
    timestamp = "2026-10-05T13:30:00Z"
    basic = {"AAPL": [{"t": timestamp, "o": 100, "h": 101, "l": 99, "c": 100, "v": 10}]}
    plus = {"AAPL": [{"t": timestamp, "o": 100, "h": 102, "l": 99, "c": 101, "v": 20}]}

    store.write_bars(basic, feed="iex", timeframe="5Min", data_tier="basic")
    store.write_bars(plus, feed="sip", timeframe="5Min", data_tier="algo_trader_plus")
    store.write_bars(plus, feed="sip", timeframe="5Min", data_tier="algo_trader_plus")

    iex = store.bars(["AAPL"], feed="iex", timeframe="5Min", start="2026-10-05", end="2026-10-05")["AAPL"]
    sip = store.bars(["AAPL"], feed="sip", timeframe="5Min", start="2026-10-05", end="2026-10-05")["AAPL"]
    assert len(iex) == len(sip) == 1
    assert iex[0]["c"] == 100
    assert sip[0]["c"] == 101
    assert len(store.coverage()) == 2


def test_profile_can_change_rule_parameters_but_not_portfolio_risk():
    assert validate_parameter_profile({"momentum": {"lookback": 15}}) == {"momentum": {"lookback": 15}}
    with pytest.raises(ValueError, match="Unknown or malformed strategy"):
        validate_parameter_profile({"risk": {"risk_per_trade_pct": 0.5}})
    with pytest.raises(ValueError, match="fast_period"):
        validate_parameter_profile({"trend_following": {"fast_period": 21, "slow_period": 9}})


def test_walk_forward_windows_are_chronological_and_non_overlapping():
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

    def deterministic_signal(strategy, mode, ticker, history, position_qty=0, parameters=None):
        if strategy == "momentum" and len(history) == 1:
            return Signal(ticker, "buy", "momentum", "test trigger", 1.0)
        return None

    monkeypatch.setattr("src.algo.backtest.evaluate_strategy", deterministic_signal)
    replay = AlgoPortfolioBacktest(
        symbols=["AAPL"], intraday_bars={"AAPL": bars}, spy_daily_bars=spy_daily,
        start=replay_day.isoformat(), end=replay_day.isoformat(), feed="iex",
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
