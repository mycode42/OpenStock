import json
from pathlib import Path

from strategies.catalog import STRATEGY_CATALOG, SWING_STRATEGIES, WEEKLY_STRATEGIES
from strategies.hourly_bars import HourlyAggregator, hourly_bars
from strategies.portfolio_coordinator import PortfolioCoordinatorConfig
from strategies.registry import ALGO_DEFAULT_PARAMETERS
from strategies.rule_pipeline import evaluate_rule_signal
from portfolio.config import AlgoRuntimeSettings

WEEKLY = ("weekly_trend_following", "weekly_momentum", "weekly_mean_reversion")
SETTINGS = Path(__file__).resolve().parents[2] / "portfolio" / "algo-strategy-config.default.json"
HOURLY_PER_SESSION = 7


def _five_minute_session(day: str, base: float = 100.0) -> list[dict]:
    """Regular-session five-minute bars for one EDT day (UTC-4): 09:30 to 15:55 ET, 78 bars."""
    from datetime import datetime, timedelta, timezone
    start = datetime.fromisoformat(f"{day}T13:30:00").replace(tzinfo=timezone.utc)
    bars = []
    for index in range(78):
        stamp = start + timedelta(minutes=5 * index)
        price = base + index * 0.01
        bars.append({"t": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": price, "h": price + 0.05, "l": price - 0.05, "c": price + 0.02, "v": 100})
    return bars


def test_session_builds_seven_hourly_bars_anchored_at_the_open():
    bars = hourly_bars(_five_minute_session("2026-10-05"))
    assert len(bars) == HOURLY_PER_SESSION
    assert [bar["t"] for bar in bars][:2] == ["2026-10-05T13:30:00Z", "2026-10-05T14:30:00Z"]
    assert bars[0]["n"] == 12 and bars[-1]["n"] == 6
    assert bars[0]["o"] == 100.0 and bars[0]["v"] == 1200


def test_aggregator_emits_a_bucket_as_soon_as_its_last_five_minute_bar_arrives():
    aggregator = HourlyAggregator()
    session = _five_minute_session("2026-10-05")
    emitted = [aggregator.add(bar) for bar in session[:12]]
    assert emitted[-1] is not None and emitted[-1]["t"] == "2026-10-05T13:30:00Z"
    assert all(item is None for item in emitted[:-1])
    assert len(aggregator.history()) == 1


def test_partial_hour_is_not_emitted_until_it_completes():
    aggregator = HourlyAggregator()
    for bar in _five_minute_session("2026-10-05")[:6]:
        assert aggregator.add(bar) is None
    assert aggregator.history() == []


def test_weekly_maximum_holding_counts_sessions_after_entry():
    bars = [{"t": "2026-10-15T14:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]
    assert evaluate_rule_signal(
        "weekly_momentum", "AAPL", bars, {"maximum_holding_sessions": 10},
        position_qty=10, entry_at="2026-10-01T14:00:00Z",
    ).reason.startswith("held 10 sessions")
    assert evaluate_rule_signal(
        "weekly_momentum", "AAPL", bars, {"maximum_holding_sessions": 10},
        position_qty=10, entry_at="2026-10-02T14:00:00Z",
    ) is None


def test_weekly_strategies_sit_in_their_own_tier_and_not_in_swing():
    assert WEEKLY_STRATEGIES == frozenset(WEEKLY)
    assert not set(WEEKLY) & SWING_STRATEGIES
    for name in WEEKLY:
        assert STRATEGY_CATALOG[name].horizon == "weekly"
        assert STRATEGY_CATALOG[name].bar_interval == "1Hour"
        assert STRATEGY_CATALOG[name].coordinator_group == "weekly"
    config = PortfolioCoordinatorConfig()
    assert config.tier_of("weekly_momentum") == "weekly"
    assert config.tier_of("swing_momentum") == "swing"
    assert config.tier_of("intraday_momentum") == "intraday"


def test_weekly_windows_are_ten_to_twenty_sessions_and_holds_are_three_to_ten():
    params = {name: ALGO_DEFAULT_PARAMETERS[name] for name in WEEKLY}
    sessions = HOURLY_PER_SESSION
    assert 10 * sessions <= params["weekly_trend_following"]["slow_period"] <= 20 * sessions
    assert 10 * sessions <= params["weekly_momentum"]["lookback"] <= 20 * sessions
    assert 10 * sessions <= params["weekly_mean_reversion"]["reference_bars"] <= 20 * sessions
    for name in WEEKLY:
        assert 3 <= params[name]["maximum_holding_sessions"] <= 10


def test_weekly_strategies_are_enabled_for_live_trading():
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))["strategies"]
    for name in WEEKLY:
        assert settings[name]["enabled"] is True
        assert settings[name]["horizon"] == "weekly"
        assert settings[name]["bar_interval"] == "1Hour"
    assert not hasattr(AlgoRuntimeSettings(), "weekly_momentum_enabled")


def test_live_hourly_cache_seeds_from_the_store_then_fetches_only_the_gap(monkeypatch):
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from strategies.runner import scan_market

    day_one = _five_minute_session("2026-10-05")
    day_two = _five_minute_session("2026-10-06")
    calls = []

    class FakeStore:
        def __init__(self, path=None):
            pass

        def bars(self, symbols, **kwargs):
            assert kwargs["adjustment"] == "split" and kwargs["feed"] == "sip"
            return {"AAPL": day_one}

    def fake_fetch(symbols, **kwargs):
        calls.append((list(symbols), kwargs["start"], kwargs["adjustment"]))
        cutoff = datetime.fromisoformat(kwargs["start"])
        return {"AAPL": [bar for bar in day_one + day_two
                         if datetime.fromisoformat(bar["t"].replace("Z", "+00:00")) >= cutoff]}

    monkeypatch.setattr(scan_market, "SQLiteBarStore", FakeStore)
    monkeypatch.setattr(scan_market, "_fetch_bars", fake_fetch)
    monkeypatch.setattr(scan_market, "_HOURLY_STATE", {})
    monkeypatch.setattr(scan_market, "_HOURLY_LAST", {})
    now = datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc)
    scope = SimpleNamespace(strategy_symbols=["AAPL"], mode="algo", data_feed="sip", now=now)

    first = scan_market._weekly_hourly_bars(scope)
    assert len(calls) == 1 and calls[0][2] == "split"
    assert calls[0][1] > "2026-10-05T13:30:00"  # the gap starts at the store's last bar, not the lookback
    assert first["AAPL"] == hourly_bars(day_one + day_two)

    second = scan_market._weekly_hourly_bars(scope)
    assert second["AAPL"] == first["AAPL"]
