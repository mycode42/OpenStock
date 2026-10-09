from datetime import date, timedelta
import math

from src.algo import auto_research
from src.algo.factor_research import _feature_panel, train_factor_model
from src.algo.walk_forward import _read_profiles, build_parser as build_walk_forward_parser


def _sessions(start: date, count: int) -> list[date]:
    result = []
    current = start
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return result


def _synthetic_daily_history(session_count: int = 2700) -> dict[str, list[dict]]:
    sessions = _sessions(date(2016, 1, 4), session_count)
    output = {}
    for symbol_index in range(26):
        name = f"T{symbol_index:02d}"
        rows = []
        close = 25.0 + symbol_index
        for index, session in enumerate(sessions):
            daily_return = (
                (symbol_index - 12) * 0.00008
                + 0.0015 * math.sin(index / (4 + symbol_index % 9) + symbol_index)
                + 0.0008 * math.sin(index / 31 + symbol_index * 0.7)
            )
            close *= 1 + daily_return
            rows.append({"date": session.isoformat(), "c": close})
        output[name] = rows
    spy_close = 200.0
    spy = []
    for index, session in enumerate(sessions):
        spy_close *= 1 + 0.0002 + 0.003 * math.sin(index / 22)
        spy.append({"date": session.isoformat(), "c": spy_close})
    output["SPY"] = spy
    return output


def test_auto_research_profiles_are_bounded_and_have_strategy_cooldowns():
    profiles = _read_profiles("autoresearch/algo-daily-profiles.json")
    assert len(profiles) == 12
    assert all(profile["controls"]["max_entries_per_day"] == 15 for profile in profiles)
    assert all("cooldown_minutes_by_strategy" in profile["controls"] for profile in profiles)
    expected = {
        "mean_reversion", "momentum", "opening_range_breakout", "trend_following",
        "multi_factor", "event_driven", "swing_mean_reversion",
    }
    assert all(set(profile["strategies"]) == expected for profile in profiles)
    by_name = {profile["name"]: profile for profile in profiles}
    baseline_overlay = by_name["baseline"]["parameters"]["satellite_overlay"]
    assert baseline_overlay["multi_factor_enabled"] is False
    assert baseline_overlay["event_driven_enabled"] is False
    assert by_name["multi_factor_satellite"]["parameters"]["satellite_overlay"]["multi_factor_enabled"]
    assert by_name["event_driven_satellite"]["parameters"]["satellite_overlay"]["event_driven_enabled"]
    assert by_name["combined_six_strategy_satellites"]["parameters"]["event_driven"]["enabled"]
    assert by_name["combined_isolated_style_balanced_with_satellites"]["parameters"]["multi_factor"]["direction_mode"] == "regime_dependent"


def test_isolated_optimizer_uses_one_direction_and_simple_candidate_sets():
    assert auto_research.ISOLATED_MAX_CANDIDATE_TICKER_DAYS_PER_SESSION == 3
    assert auto_research.COMBINED_MAX_CANDIDATE_TICKER_DAYS_PER_SESSION == 15
    for strategy in auto_research.CORE_STRATEGIES:
        for direction in ("long", "short"):
            profiles = auto_research._isolated_direction_profiles(strategy, direction)
            assert len(profiles) == 3
            assert profiles[0]["name"] == "baseline"
            assert all(profile["strategies"] == [strategy] for profile in profiles)
            for profile in profiles:
                settings = profile["parameters"][strategy]
                if strategy in {"trend_following", "momentum", "swing_mean_reversion"}:
                    assert settings["direction_mode"] == f"{direction}_only"
                else:
                    assert settings["allow_long"] is (direction == "long")
                    assert settings["allow_short"] is (direction == "short")
                assert profile["controls"]["max_entries_per_day"] == 15


def test_combined_replay_compares_core_with_all_six_as_overlays(monkeypatch):
    calls = []

    class Store:
        def bars(self, symbols, **kwargs):
            return {symbol: [] for symbol in symbols}

    class Replay:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def run(self):
            return {"net_return_pct": 0, "entry_count": 0}

    monkeypatch.setattr(auto_research, "AlgoPortfolioBacktest", Replay)
    report = auto_research._combined_portfolio_replay(
        Store(), symbols=["AAPL"], start=date(2024, 1, 1), end=date(2024, 1, 2),
        event_calendar=object(),
    )

    assert set(report["results"]) == {"four_core_rules", "six_strategy_satellite_portfolio"}
    assert set(calls[0]["strategies"]) == {
        "mean_reversion", "momentum", "opening_range_breakout", "trend_following",
    }
    assert set(calls[1]["strategies"]) == set(calls[0]["strategies"]) | {"multi_factor", "event_driven"}
    assert calls[1]["profile"]["satellite_overlay"] == {
        "multi_factor_enabled": True, "event_driven_enabled": True,
    }


def test_walk_forward_exposes_a_minimum_satellite_sample_gate():
    args = build_walk_forward_parser().parse_args([
        "--symbols", "AAPL", "--start", "2025-01-01", "--end", "2025-02-01",
        "--profiles", "autoresearch/algo-daily-profiles.json",
    ])

    assert args.min_satellite_confirmed_entries == 3


def test_walk_forward_summary_exposes_promotion_gate_and_costs(tmp_path):
    gate = {"promotable": False, "checks": {"neighbor_parameter_stability": False}}
    costs = {"spread_bps": 3, "commission_per_share_each_side": 0.005}
    summary = auto_research._summarize_walk_forward(
        {"folds": [], "promotion_gate": gate, "transaction_cost_model": costs},
        artifact=tmp_path / "walk-forward.json",
    )
    assert summary["promotion_gate"] == gate
    assert summary["transaction_cost_model"] == costs


def test_auto_research_uses_the_configured_fixed_algo_universe():
    symbols, metadata = auto_research.load_algo_universe()

    assert len(symbols) == 22
    assert symbols[:9] == ["NVDA", "AVGO", "TSM", "MSFT", "AAPL", "GOOGL", "META", "AMZN", "AMD"]
    assert symbols[-3:] == ["PLTR", "CRM", "PANW"]
    assert metadata == {"universe_source": "configured_fixed"}


def test_auto_research_backfills_symbols_missing_from_the_market_store(monkeypatch):
    end_date = date(2026, 10, 8)
    symbols = auto_research.load_algo_universe()[0]
    missing_symbol = symbols[-1]
    coverage = [
        {
            "symbol": symbol, "feed": "sip", "adjustment": "split", "timeframe": "5Min",
            "first": "2016-01-04", "last": end_date.isoformat(),
        }
        for symbol in symbols[:-1]
    ]
    coverage.append({
        "symbol": "SPY", "feed": "sip", "adjustment": "split", "timeframe": "1Day",
        "first": "2016-01-04", "last": end_date.isoformat(),
    })

    class Store:
        def coverage(self):
            return coverage

        def write_bars(self, *args, **kwargs):
            return 0

    calls = []
    monkeypatch.setattr(auto_research, "data_tier", lambda: "algo_trader_plus")
    monkeypatch.setattr(auto_research, "regular_feed", lambda: "sip")
    monkeypatch.setattr(
        auto_research, "fetch_alpaca_bars",
        lambda requested, **kwargs: calls.append((list(requested), kwargs)) or {},
    )

    report = auto_research._refresh_incremental(
        Store(), selected_symbols=symbols, end_date=end_date,
    )

    assert report["missing_selected_history"] == [missing_symbol]
    assert calls[0][0] == [missing_symbol]
    assert calls[0][1]["start"] == "2015-01-01T00:00:00Z"


def test_factor_features_are_as_of_and_forward_labels_are_purged():
    history = _synthetic_daily_history(500)
    sessions = [date.fromisoformat(row["date"]) for row in history["SPY"]]
    panel = _feature_panel(history, session_dates=sessions, min_universe=20)
    assert panel
    first = panel[0]
    assert (first["label_end"] - first["date"]).days >= 27
    assert (panel[1]["date"] - first["date"]).days >= 27

    changed = {symbol: [dict(row) for row in rows] for symbol, rows in history.items()}
    ticker = next(symbol for symbol in changed if symbol != "SPY")
    for row in changed[ticker]:
        if row["date"] > first["date"].isoformat():
            row["c"] *= 3
    revised = _feature_panel(changed, session_dates=sessions, min_universe=20)[0]
    assert revised["zscores"][ticker] == first["zscores"][ticker]
    assert revised["returns"][ticker] != first["returns"][ticker]


def test_ridge_factor_candidate_reports_chronological_holdouts():
    report = train_factor_model(_synthetic_daily_history(), min_universe=20)
    weights = report["current_candidate_weights"]
    assert set(weights) == {"momentum_1m", "momentum_3m", "momentum_6m", "low_volatility"}
    assert all(value >= 0 for value in weights.values())
    assert abs(sum(weights.values()) - 1.0) < 1e-6
    assert report["selected_ridge_alpha"] in {0.01, 0.1, 1.0, 10.0}
    assert report["frozen_holdout_ic"]["sample_count"] >= 8
    assert report["adaptive_prequential_ic"]["sample_count"] >= 8
    assert "strictly before" in report["purge_rule"]
    assert report["current_training_label_cutoff"] < report["session_end"]
