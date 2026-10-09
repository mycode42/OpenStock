"""Focused tests for the continuous Algo scanner's data and exit safeguards."""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
import requests

from src.config import ALGO_MODE_CONFIG, get_mode_config
from src.strategies import runner, universe as strategy_universe
from src.strategies.signals import Signal


ET = ZoneInfo("America/New_York")
FROZEN_NOW = datetime(2026, 10, 7, 21, 0, tzinfo=ET)


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW if tz is None else FROZEN_NOW.astimezone(tz)


def _quote(price: float) -> dict:
    return {
        "price": price,
        "bid": price - 0.1,
        "ask": price + 0.1,
        "timestamp": FROZEN_NOW.astimezone(timezone.utc),
    }


def _mock_common(monkeypatch, *, positions=None, orders=None):
    monkeypatch.setattr(runner, "datetime", FrozenDateTime)
    monkeypatch.setattr(
        runner, "_current_portfolio",
        lambda mode: ({"equity": "100000"}, positions or {}, orders or []),
    )
    monkeypatch.setattr(runner.pairs, "candidates", lambda *args, **kwargs: [])


def test_swing_and_day_use_their_fixed_configured_universes():
    swing = strategy_universe.configured_universe("swing")
    day = strategy_universe.configured_universe("day")

    assert swing["tickers"] == [
        "NVDA", "AVGO", "TSM", "MSFT", "AAPL", "GOOGL", "META", "AMZN",
        "PLTR", "AMD", "CRM", "SNOW", "NET", "PANW",
        "JPM", "V", "UNH", "JNJ", "PG", "KO",
        "COIN", "MSTR", "RKLB", "SMCI", "SPY", "QQQ", "GLD", "TLT",
    ]
    assert day["tickers"] == [
        "NVDA", "AVGO", "TSM", "AMD", "MSFT", "AAPL", "META", "GOOGL", "AMZN",
        "PLTR", "TQQQ", "COIN", "SPY", "QQQ",
    ]
    assert swing["categories"]["COIN"] == "tactical"
    assert day["categories"]["TQQQ"] == "momentum"
    assert {
        key: bucket["max_sector_pct"]
        for key, bucket in get_mode_config("swing")["universe"].items()
    } == {
        "core_tech": 0.30,
        "growth": 0.25,
        "value_dividend": 0.25,
        "tactical": 0.03,
        "hedge": 0.15,
    }
    assert {
        key: bucket["max_sector_pct"]
        for key, bucket in get_mode_config("day")["universe"].items()
    } == {
        "mega_cap": 0.50,
        "momentum": 0.35,
        "etf_direction": 0.30,
    }
    assert {
        key: bucket["max_sector_pct"]
        for key, bucket in ALGO_MODE_CONFIG["universe"].items()
    } == {
        "core_tech": 0.40,
        "diversifiers": 0.30,
        "hedge": 0.20,
        "tactical": 0.04,
    }


def test_algo_uses_the_configured_22_symbol_universe():
    algo = strategy_universe.configured_universe("algo")

    assert algo["tickers"] == [
        "NVDA", "AVGO", "TSM", "MSFT", "AAPL", "GOOGL", "META", "AMZN", "AMD",
        "JPM", "V", "UNH", "JNJ", "PG", "KO",
        "SPY", "QQQ", "GLD", "TLT",
        "PLTR", "CRM", "PANW",
    ]
    assert algo["categories"]["PLTR"] == "tactical"
    assert algo["categories"]["SPY"] == "hedge"
    assert "universe_selection" not in ALGO_MODE_CONFIG
    assert ALGO_MODE_CONFIG["universe"]["tactical"]["max_per_stock_pct"] == 0.02
    assert ALGO_MODE_CONFIG["risk"]["max_tactical_pct"] == 0.04
    assert ALGO_MODE_CONFIG["risk"]["max_tactical_positions"] == 2


@pytest.mark.parametrize("mode", ["swing", "day"])
def test_swing_and_day_scans_use_only_their_configured_universe(monkeypatch, mode):
    _mock_common(monkeypatch)
    monkeypatch.setattr(
        runner, "load_algo_universe",
        lambda: (_ for _ in ()).throw(AssertionError("Algo universe loader called")),
    )
    monkeypatch.setattr(runner, "_fetch_bars", lambda *args, **kwargs: {})
    monkeypatch.setattr(runner, "_fetch_latest_quotes", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        runner, "rank_strategies",
        lambda bars, mode, **kwargs: {"regime": "range_bound", "reason": "test", "ranked": [], "active": []},
    )

    result = runner.scan(mode=mode)

    assert result["universe"]["tickers"] == strategy_universe.configured_universe(mode)["tickers"]


def test_overnight_scan_uses_plus_boats_bars_and_quotes(monkeypatch):
    _mock_common(monkeypatch)
    monkeypatch.setattr(runner, "feeds_for_session", lambda session: ("boats", "boats"))
    monkeypatch.setattr(runner, "historical_delay_seconds", lambda feed, session: 0)
    bar_calls = []
    quote_feeds = []

    def fetch_bars(symbols, *, mode, timeframe, start, end, limit, feed="sip", adjustment=None):
        bar_calls.append({
            "timeframe": timeframe, "start": start, "end": end,
            "feed": feed, "adjustment": adjustment,
        })
        if timeframe == "5Min":
            return {"AAPL": [{"t": "2026-10-08T00:55:00Z", "c": 100, "h": 101, "l": 99, "v": 1000}]}
        return {"SPY": [{"t": "2026-10-07T20:00:00Z", "c": 100}]}

    def fetch_quotes(symbols, *, mode, feed, errors=None):
        quote_feeds.append(feed)
        return {"AAPL": _quote(100)}

    monkeypatch.setattr(runner, "_fetch_bars", fetch_bars)
    monkeypatch.setattr(runner, "_fetch_latest_quotes", fetch_quotes)
    monkeypatch.setattr(
        runner, "rank_strategies",
        lambda bars, mode, **kwargs: {
            "regime": "trending_up", "reason": "test",
            "ranked": [{"strategy": "momentum", "score": 0.8}],
            "active": ["momentum"], "satellites": [],
        },
    )
    monkeypatch.setattr(runner, "apply_regime_advisory", lambda regime: regime)
    monkeypatch.setattr(
        runner, "generate_swing_momentum_signals",
        lambda *args, **kwargs: ({"AAPL": Signal("AAPL", "buy", "momentum", "test entry", 0.8)}, {}),
    )

    result = runner.scan(
        mode="algo",
        universe_result={"tickers": ["AAPL"]},
        position_owners={}, strategy_states={},
        session_name="overnight", trading_date="2026-10-08",
        extended_hours=True, data_feed="overnight", quote_feed="boats",
        overnight_tradable_tickers={"AAPL"},
    )

    intraday = next(call for call in bar_calls if call["timeframe"] == "5Min")
    daily = next(call for call in bar_calls if call["timeframe"] == "1Day")
    assert intraday["feed"] == "boats"
    assert daily["feed"] == "sip"
    assert daily["adjustment"] == "split"
    assert datetime.fromisoformat(intraday["end"]) <= FROZEN_NOW
    assert quote_feeds == ["boats"]
    assert result["data_status"]["historical_delay_seconds"] == 0
    assert result["data_status"]["historical_bars"] == "ok"
    assert result["entries_paused"] is True
    assert "overnight entries are disabled by configuration" in result["entry_pause_reasons"]
    assert result["trades"] == []
    assert any(
        item["action"] == "buy"
        and "overnight entries are disabled by configuration" in item["suppressed_reason"]
        for item in result["signals"]
    )


def test_owned_stop_exit_survives_failed_historical_requests(monkeypatch):
    position = {"AAPL": {"symbol": "AAPL", "qty": "2"}}
    _mock_common(monkeypatch, positions=position, orders=[])
    monkeypatch.setattr(
        runner, "_fetch_bars",
        lambda *args, **kwargs: (_ for _ in ()).throw(requests.HTTPError("historical data unavailable")),
    )
    monkeypatch.setattr(
        runner, "_fetch_latest_quotes",
        lambda symbols, *, mode, feed, errors=None: {"AAPL": _quote(98)},
    )
    states = {"trend_following": {"AAPL": {
        "phase": "open", "stop_price": 97.95, "take_profit": 105.0,
        "last_signal_bar": None,
    }}}
    owners = {"AAPL": "trend_following"}

    result = runner.scan(
        mode="algo",
        universe_result={"tickers": []},
        position_owners=owners, strategy_states=states,
        session_name="overnight", trading_date="2026-10-08",
        extended_hours=True, data_feed="boats", quote_feed="overnight",
        overnight_tradable_tickers={"AAPL"},
    )

    assert result["data_status"]["historical_bars"] == "degraded"
    assert result["entries_paused"] is True
    assert result["scan_errors"]
    assert len(result["trades"]) == 1
    assert result["trades"][0]["action"] == "sell"
    assert result["trades"][0]["qty"] == 2
    assert result["signals"][0]["software_protection"] is True


def test_extended_hours_exit_keeps_broker_stop_in_place(monkeypatch):
    position = {"AAPL": {"symbol": "AAPL", "qty": "2"}}
    bracket_stop = [{
        "id": "stop-1", "symbol": "AAPL", "side": "sell", "type": "stop",
        "order_class": "bracket", "status": "held",
    }]
    _mock_common(monkeypatch, positions=position, orders=bracket_stop)
    monkeypatch.setattr(
        runner, "_fetch_bars",
        lambda *args, **kwargs: (_ for _ in ()).throw(requests.HTTPError("historical data unavailable")),
    )
    monkeypatch.setattr(
        runner, "_fetch_latest_quotes",
        lambda symbols, *, mode, feed, errors=None: {"AAPL": _quote(98)},
    )
    states = {"trend_following": {"AAPL": {
        "phase": "open", "stop_price": 97.95, "take_profit": 105.0,
    }}}
    result = runner.scan(
        mode="algo", universe_result={"tickers": []},
        position_owners={"AAPL": "trend_following"}, strategy_states=states,
        session_name="overnight", trading_date="2026-10-08",
        extended_hours=True, data_feed="boats", quote_feed="overnight",
        overnight_tradable_tickers={"AAPL"},
    )
    assert result["trades"] == []
    assert "broker protective order remains active" in result["signals"][0]["suppressed_reason"]


def test_regular_software_stop_can_close_against_broker_bracket(monkeypatch):
    position = {"AAPL": {"symbol": "AAPL", "qty": "2", "avg_entry_price": "100"}}
    bracket_stop = [{
        "id": "stop-1", "symbol": "AAPL", "side": "sell", "type": "stop",
        "order_class": "bracket", "status": "held",
    }]
    _mock_common(monkeypatch, positions=position, orders=bracket_stop)
    monkeypatch.setattr(
        runner, "_fetch_bars",
        lambda *args, **kwargs: (_ for _ in ()).throw(requests.HTTPError("historical data unavailable")),
    )
    monkeypatch.setattr(
        runner, "_fetch_latest_quotes",
        lambda symbols, *, mode, feed, errors=None: {"AAPL": _quote(98)},
    )
    states = {"trend_following": {"AAPL": {
        "phase": "open", "stop_price": 99.0, "take_profit": 105.0,
    }}}

    result = runner.scan(
        mode="algo", universe_result={"tickers": []},
        position_owners={"AAPL": "trend_following"}, strategy_states=states,
        session_name="regular", trading_date="2026-10-07",
        extended_hours=False, data_feed="sip", quote_feed="sip",
    )

    assert len(result["trades"]) == 1, result["signals"]
    assert result["trades"][0]["action"] == "sell"
    assert result["trades"][0]["cancel_open_orders"] is True
    assert result["trades"][0]["software_protection"] is True


def test_stale_overnight_signal_bar_pauses_new_entries(monkeypatch):
    _mock_common(monkeypatch)
    monkeypatch.setattr(
        runner, "_fetch_bars",
        lambda symbols, *, timeframe, **kwargs: (
            {"AAPL": [{"t": "2026-10-08T00:00:00Z", "c": 100}]}
            if timeframe == "5Min" else
            {"SPY": [{"t": "2026-10-07T20:00:00Z", "c": 100}]}
        ),
    )
    monkeypatch.setattr(
        runner, "_fetch_latest_quotes",
        lambda symbols, *, mode, feed, errors=None: {"AAPL": _quote(100)},
    )
    monkeypatch.setattr(
        runner, "rank_strategies",
        lambda bars, mode, **kwargs: {
            "regime": "trending_up", "reason": "test",
            "ranked": [{"strategy": "momentum", "score": 0.9}], "active": ["momentum"],
        },
    )
    monkeypatch.setattr(runner, "apply_regime_advisory", lambda regime: regime)
    monkeypatch.setattr(
        runner, "evaluate_rule_signal",
        lambda *args, **kwargs: Signal("AAPL", "buy", "momentum", "test", strength=1.0),
    )

    result = runner.scan(
        mode="algo", universe_result={"tickers": ["AAPL"]},
        position_owners={}, strategy_states={}, session_name="overnight",
        trading_date="2026-10-08", extended_hours=True,
        overnight_tradable_tickers={"AAPL"},
    )

    assert result["entries_paused"] is True
    assert result["data_status"]["historical_bars"] == "stale"
    assert result["trades"] == []
    # Algo Momentum is cross-sectional and daily now. With no daily history it
    # emits no 5-minute fallback signal while the stale-data pause is active.
    assert result["signals"] == []


def test_historical_bar_fetch_follows_pagination(monkeypatch):
    calls = []

    def request(endpoint, *, mode, base=runner.DATA_BASE, params=None):
        calls.append(dict(params))
        if "page_token" not in params:
            return {"bars": {"AAPL": [{"t": "first"}]}, "next_page_token": "next"}
        return {"bars": {"AAPL": [{"t": "second"}]}, "next_page_token": None}

    monkeypatch.setattr(runner, "_request", request)

    bars = runner._fetch_bars(
        ["AAPL"], mode="algo", timeframe="1Day",
        start="2026-01-01", end="2026-10-08", limit=10000,
    )

    assert [bar["t"] for bar in bars["AAPL"]] == ["first", "second"]
    assert calls[1]["page_token"] == "next"


def test_algo_universe_keeps_fixed_membership_and_overnight_asset_flags(monkeypatch):
    fixed = strategy_universe.configured_universe("algo")["tickers"]
    monkeypatch.setattr(
        strategy_universe, "_fetch_active_assets",
        lambda mode: {
            "SPY": {"overnight_tradable": True},
            "NVDA": {"attributes": ["overnight_tradable"]},
        },
    )

    result = strategy_universe.load_algo_universe()

    assert result["tickers"] == fixed
    assert result["overnight_tradable_tickers"] == ["NVDA", "SPY"]
    assert not {"dynamic", "selection_source", "selected_count", "target_count"}.intersection(result)


def test_one_shot_algo_cli_is_removed(monkeypatch):
    monkeypatch.setattr(runner.sys, "argv", ["strategy-runner", "--mode", "algo", "--execute"])

    with pytest.raises(SystemExit) as exit_status:
        runner.main()

    assert exit_status.value.code == 2


def test_algo_scan_requires_coordinator_state():
    with pytest.raises(ValueError, match="coordinator"):
        runner.scan(mode="algo")
