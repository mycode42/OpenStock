"""Focused tests for the continuous Algo scanner's data and exit safeguards."""

from datetime import date, datetime, timedelta, timezone
from dataclasses import replace
from zoneinfo import ZoneInfo

import pytest
import requests

from portfolio.config import ALGO_MODE_CONFIG, get_algo_runtime_settings
from strategies import universe as strategy_universe
import strategies.runner.scan as runner
import strategies.runner.data as runner_data
import strategies.runner.signal_generation as runner_signal_generation
from strategies.signals import Signal
from strategies.runner.data import fetch_latest_quotes


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
        runner, "get_algo_runtime_settings",
        lambda: replace(get_algo_runtime_settings(), overnight_entries_enabled=True),
    )
    monkeypatch.setattr(
        runner, "_current_portfolio",
        lambda mode: ({"equity": "100000"}, positions or {}, orders or []),
    )


def test_latest_quote_fetch_diagnostics_distinguish_missing_and_invalid():
    diagnostics = {}
    payload = {"quotes": {
        "AAPL": {"bp": 99.9, "ap": 100.1, "t": "2026-10-08T01:00:00Z"},
        "MSFT": {"bp": 0, "ap": 0, "t": "2026-10-08T01:00:00Z"},
        "TSLA": {"bp": 100, "ap": 101},
        "NVDA": None,
        "AMD": ["malformed"],
    }}

    quotes = fetch_latest_quotes(
        ["AAPL", "MSFT", "TSLA", "NVDA", "AMD"],
        mode="algo", feed="boats", diagnostics=diagnostics,
        request_fn=lambda endpoint, *, mode, params: payload,
    )

    assert set(quotes) == {"AAPL"}
    assert diagnostics["AAPL"]["status"] == "usable"
    assert diagnostics["MSFT"]["status"] == "invalid_bid_ask"
    assert diagnostics["TSLA"]["status"] == "invalid_timestamp"
    assert diagnostics["NVDA"]["status"] == "missing"
    assert diagnostics["AMD"]["status"] == "invalid_quote"
    assert diagnostics["AAPL"]["quote_timestamp"] == "2026-10-08T01:00:00+00:00"
    assert not ({"price", "bid", "ask"} & diagnostics["AAPL"].keys())

    failed_diagnostics = {}
    errors = []
    quotes = fetch_latest_quotes(
        ["AAPL", "MSFT"], mode="algo", feed="boats",
        errors=errors, diagnostics=failed_diagnostics,
        request_fn=lambda *args, **kwargs: (_ for _ in ()).throw(requests.Timeout()),
    )
    assert quotes == {}
    assert failed_diagnostics == {
        "AAPL": {"status": "request_error"},
        "MSFT": {"status": "request_error"},
    }
    assert errors == ["latest quotes boats: Timeout: "]


def test_algo_uses_the_configured_portfolio_universe():
    algo = strategy_universe.configured_universe("algo")

    assert algo["tickers"] == [
        "NVDA", "AVGO", "TSM", "MSFT", "AAPL", "GOOGL", "META", "AMZN", "AMD",
        "JPM", "V", "UNH", "JNJ", "PG", "KO",
        "SPY", "QQQ", "GLD", "TLT",
        "PLTR", "CRM", "PANW", "TSLA", "SPCX",
    ]
    assert algo["categories"]["PLTR"] == "tactical"
    assert algo["categories"]["SPY"] == "hedge"
    assert "universe_selection" not in ALGO_MODE_CONFIG
    assert ALGO_MODE_CONFIG["universe"]["tactical"]["max_per_stock_pct"] == 0.07
    assert ALGO_MODE_CONFIG["risk"]["max_tactical_pct"] == 0.10
    assert ALGO_MODE_CONFIG["risk"]["max_tactical_positions"] == 3


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

    def fetch_quotes(symbols, *, mode, feed, errors=None, diagnostics=None):
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
    assert result["entries_paused"] is False
    assert result["entry_pause_reasons"] == []
    assert result["trades"] == []
    assert result["signals"][0]["suppressed_reason"] == (
        "Algo order actions are restricted to regular market hours"
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
        lambda symbols, *, mode, feed, errors=None, diagnostics=None: {"AAPL": _quote(98)},
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
    assert result["trades"] == []
    assert result["signals"][0]["software_protection"] is True
    intent = states["trend_following"]["AAPL"]["pending_exit_intent"]
    assert intent["retryable"] is True
    assert intent["status"] == "deferred_until_regular_hours"
    assert intent["software_protection"] is True


def test_extended_hours_software_exit_is_deferred_with_active_broker_bracket(monkeypatch):
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
        lambda symbols, *, mode, feed, errors=None, diagnostics=None: {"AAPL": _quote(98)},
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
    intent = states["trend_following"]["AAPL"]["pending_exit_intent"]
    assert intent["status"] == "deferred_until_regular_hours"
    assert intent["retryable"] is True
    assert result["signals"][0]["software_protection"] is True
    assert result["signals"][0]["suppressed_reason"] == (
        "Algo order actions are restricted to regular market hours"
    )


@pytest.mark.parametrize("order_class", ["bracket", "oco"])
@pytest.mark.parametrize(
    ("session_name", "extended_hours", "frozen_now"),
    [
        ("regular", False, datetime(2026, 10, 8, 10, 0, tzinfo=ET)),
        ("overnight", True, datetime(2026, 10, 8, 3, 55, tzinfo=ET)),
        ("overnight", True, datetime(2026, 10, 8, 4, 0, tzinfo=ET)),
        ("premarket", True, datetime(2026, 10, 8, 4, 5, tzinfo=ET)),
    ],
)
def test_strategy_rule_sell_is_regular_hours_only_and_deferred_otherwise(
    monkeypatch, order_class, session_name, extended_hours, frozen_now,
):
    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen_now if tz is None else frozen_now.astimezone(tz)

    position = {"AAPL": {"symbol": "AAPL", "qty": "2", "avg_entry_price": "100"}}
    bracket_stop = [{
        "id": "stop-1", "symbol": "AAPL", "side": "sell", "type": "stop",
        "order_class": order_class, "status": "held",
    }]
    _mock_common(monkeypatch, positions=position, orders=bracket_stop)
    monkeypatch.setattr(runner, "datetime", FrozenClock)
    signal_bar_time = (frozen_now.astimezone(timezone.utc) - timedelta(minutes=5)).isoformat()
    previous_signal_bar_time = (frozen_now.astimezone(timezone.utc) - timedelta(minutes=10)).isoformat()
    monkeypatch.setattr(
        runner, "_fetch_bars",
        lambda symbols, *, timeframe, **kwargs: (
            {"AAPL": [{"t": signal_bar_time, "c": 100}]}
            if timeframe == "5Min" else
            {"SPY": [{"t": "2026-10-07T20:00:00Z", "c": 100}]}
        ),
    )
    quote = _quote(100)
    quote["timestamp"] = frozen_now.astimezone(timezone.utc)
    monkeypatch.setattr(
        runner, "_fetch_latest_quotes",
        lambda symbols, *, mode, feed, errors=None, diagnostics=None: {"AAPL": quote},
    )
    monkeypatch.setattr(
        runner, "rank_strategies",
        lambda bars, mode, **kwargs: {
            "regime": "trending_up", "reason": "test",
            "ranked": [{"strategy": "trend_following", "score": 0.8}],
            "active": ["trend_following"], "satellites": [],
        },
    )
    monkeypatch.setattr(runner, "apply_regime_advisory", lambda regime: regime)
    monkeypatch.setattr(
        runner_signal_generation, "evaluate_rule_signal",
        lambda *args, **kwargs: Signal("AAPL", "sell", "trend_following", "rule exit", strength=1.0),
    )
    states = {"trend_following": {"AAPL": {
        "phase": "open", "last_signal_bar": previous_signal_bar_time,
        "opened_at": "2026-10-07T14:00:00Z", "stop_price": 90, "take_profit": 120,
    }}}

    result = runner.scan(
        mode="algo", universe_result={"tickers": ["AAPL"]},
        position_owners={"AAPL": "trend_following"}, strategy_states=states,
        session_name=session_name, trading_date="2026-10-08",
        extended_hours=extended_hours,
        data_feed="boats" if extended_hours else "sip",
        quote_feed="boats" if extended_hours else "sip",
        overnight_tradable_tickers={"AAPL"} if extended_hours else None,
    )

    if extended_hours:
        assert result["trades"] == []
        intent = states["trend_following"]["AAPL"]["pending_exit_intent"]
        assert intent["status"] == "deferred_until_regular_hours"
        assert intent["retryable"] is True
        assert result["signals"][0]["suppressed_reason"] == (
            "Algo order actions are restricted to regular market hours"
        )
    else:
        assert len(result["trades"]) == 1, (result["signals"], result["data_status"], result["entries_paused"])
        assert result["trades"][0]["action"] == "sell"
        assert result["trades"][0]["qty"] == 2
        assert result["trades"][0]["cancel_open_orders"] is True
        assert not result["trades"][0].get("software_protection")
        assert not result["signals"][0].get("suppressed_reason")
    if session_name == "overnight" and frozen_now.hour == 4:
        assert (frozen_now.hour, frozen_now.minute) == (4, 0)
    if session_name == "premarket":
        assert (frozen_now.hour, frozen_now.minute) == (4, 5)


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
        lambda symbols, *, mode, feed, errors=None, diagnostics=None: {"AAPL": _quote(98)},
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


def test_scan_reports_stale_owned_quote_age_and_feed(monkeypatch):
    position = {"AAPL": {"symbol": "AAPL", "qty": "2"}}
    _mock_common(monkeypatch, positions=position, orders=[])

    def fetch_bars(symbols, *, timeframe, **kwargs):
        if timeframe == "5Min":
            return {"AAPL": [{"t": "2026-10-08T00:50:00Z", "c": 100}]}
        return {"SPY": [{"t": "2026-10-07T00:00:00Z", "c": 100}]}

    stale_quote = _quote(100)
    stale_quote["timestamp"] = FROZEN_NOW.astimezone(timezone.utc) - timedelta(seconds=91)
    monkeypatch.setattr(runner, "_fetch_bars", fetch_bars)
    monkeypatch.setattr(
        runner, "_fetch_latest_quotes",
        lambda symbols, *, mode, feed, errors=None, diagnostics=None: {"AAPL": stale_quote},
    )
    monkeypatch.setattr(
        runner, "rank_strategies",
        lambda bars, mode, **kwargs: {
            "regime": "trending_up", "reason": "test",
            "ranked": [], "active": [], "satellites": [],
        },
    )
    monkeypatch.setattr(runner, "apply_regime_advisory", lambda regime: regime)

    result = runner.scan(
        mode="algo", universe_result={"tickers": []},
        position_owners={"AAPL": "trend_following"},
        strategy_states={"trend_following": {"AAPL": {"phase": "open", "stop_price": 95}}},
        session_name="overnight", trading_date="2026-10-08",
        extended_hours=True, data_feed="boats", quote_feed="boats",
        overnight_tradable_tickers=set(),
    )

    diagnostic, = result["data_status"]["owned_position_quote_diagnostics"]
    assert diagnostic == {
        "symbol": "AAPL", "feed": "boats", "status": "stale",
        "quote_availability": "session_limited", "overnight_tradable": False,
        "quote_timestamp": (FROZEN_NOW.astimezone(timezone.utc) - timedelta(seconds=91)).isoformat(),
        "age_seconds": 91.0, "freshness_limit_seconds": 90,
    }
    assert result["data_status"]["quotes"] == "session_limited"
    assert result["data_status"]["quote_request_status"] == "succeeded"
    assert result["data_status"]["session_limited_quotes"] is True
    assert result["data_status"]["owned_positions_without_fresh_quotes"] == ["AAPL"]


def test_overnight_swing_signal_bar_within_extended_window_does_not_pause(monkeypatch):
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
        lambda symbols, *, mode, feed, errors=None, diagnostics=None: {"AAPL": _quote(100)},
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
        runner_signal_generation, "evaluate_rule_signal",
        lambda *args, **kwargs: Signal("AAPL", "buy", "momentum", "test", strength=1.0),
    )

    result = runner.scan(
        mode="algo", universe_result={"tickers": ["AAPL"]},
        position_owners={}, strategy_states={}, session_name="overnight",
        trading_date="2026-10-08", extended_hours=True,
        overnight_tradable_tickers={"AAPL"},
    )

    assert result["entries_paused"] is False
    assert result["data_status"]["historical_bars"] == "ok"
    assert result["data_status"]["fresh_signal_bar_count"] == 0
    assert result["data_status"]["overnight_swing_extended_age_signal_bar_count"] == 1
    assert result["trades"] == []
    # The overnight bar is within the swing-context window. Momentum still
    # needs daily history, so this scan emits no 5-minute fallback signal.
    assert result["signals"] == []


def test_historical_bar_fetch_follows_pagination(monkeypatch):
    calls = []

    def request(endpoint, *, mode, base=runner_data.DATA_BASE, params=None):
        calls.append(dict(params))
        if "page_token" not in params:
            return {"bars": {"AAPL": [{"t": "first"}]}, "next_page_token": "next"}
        return {"bars": {"AAPL": [{"t": "second"}]}, "next_page_token": None}

    monkeypatch.setattr(runner_data, "_request", request)

    bars = runner_data.fetch_bars(
        ["AAPL"], mode="algo", timeframe="1Day",
        start="2026-01-01", end="2026-10-08", limit=10000,
        request_fn=request,
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


def test_algo_scan_requires_coordinator_state():
    with pytest.raises(ValueError, match="coordinator"):
        runner.scan(mode="algo")
