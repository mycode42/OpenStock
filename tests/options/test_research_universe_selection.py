import json
from datetime import date, timedelta

from options.research.history_store import OptionsHistoryStore
from options.research.universe_selection import (
    _apply_sector_overlay,
    _evaluate_hard_filters,
    _read_history,
    _universe_symbols,
    apply_portfolio_caps,
    build_eligible_cache,
)


def _write_archive(path, symbol, *, start=date(2026, 9, 1), sessions=25, daily_return=0.001):
    with path.open("w", encoding="utf-8") as stream:
        for index in range(sessions):
            session = start + timedelta(days=index)
            spot = 100 * (1 + daily_return) ** index
            expiration = session + timedelta(days=30)
            row = {
                "underlying": symbol,
                "as_of": f"{session.isoformat()}T16:00:00-04:00",
                "spot": spot,
                "underlying_close": spot,
                "equity_volume": 500000,
                "volatility_state": "normal",
                "contracts": [{
                    "symbol": f"{symbol}-OPTION",
                    "underlying_symbol": symbol,
                    "expiration_date": expiration.isoformat(),
                    "type": "call",
                    "strike_price": spot,
                    "bid": 1.0,
                    "ask": 1.1,
                    "quote_at": f"{session.isoformat()}T16:00:00-04:00",
                    "open_interest": 2000,
                    "volume": 100,
                }],
            }
            stream.write(json.dumps(row) + "\n")


def test_history_import_persists_daily_liquidity_and_hard_filter_metrics(tmp_path):
    database = tmp_path / "history.sqlite3"
    archive = tmp_path / "SPY.jsonl"
    _write_archive(archive, "SPY")
    store = OptionsHistoryStore(database)
    assert store.import_jsonl(archive, underlying="SPY", source_kind="theta")["status"] == "imported"
    coverage = store.coverage_summary("SPY")
    assert coverage["combined_sessions"] == 25
    assert coverage["equity_volume_sessions"] == 25
    assert coverage["option_volume_sessions"] == 25
    assert coverage["option_volume_sessions_80pct_coverage"] == 25
    assert coverage["open_interest_sessions_80pct_coverage"] == 25
    assert coverage["option_spread_sessions_80pct_coverage"] == 25

    import sqlite3

    connection = sqlite3.connect(database)
    try:
        history = _read_history(connection, "SPY", limit=25)
    finally:
        connection.close()
    rules = {
        "lookback_sessions": 20,
        "min_valid_sessions": 20,
        "minimum_coverage": 0.8,
        "hard_filters": {
            "activity_mode": "volume_or_oi",
            "min_average_option_volume_60d": 50,
            "min_latest_total_open_interest_60d": 1000,
            "min_average_equity_volume": 100000,
            "min_average_equity_dollar_volume": 10000000,
            "max_median_option_spread_fraction": 0.25,
            "max_equity_spread_fraction": None,
        },
    }
    decision = _evaluate_hard_filters("SPY", history, as_of_date=history[-1]["session_date"], rules=rules)
    assert decision["hard_eligible"] is True
    assert decision["average_option_volume_60d_20d"] == 100
    assert decision["latest_total_open_interest_60d"] == 2000
    assert decision["average_equity_volume_20d"] == 500000


def test_portfolio_caps_limit_sector_and_correlation_cluster_exposure():
    start = date(2026, 1, 1)
    prices = {"AAA": 100.0, "AAB": 120.0, "BBB": 80.0}
    history = {symbol: [] for symbol in prices}
    for index in range(35):
        session = (start + timedelta(days=index)).isoformat()
        prices["AAA"] *= 1.01
        prices["AAB"] *= 1.01
        prices["BBB"] *= 1.02 if index % 2 else 0.99
        for symbol, price in prices.items():
            history[symbol].append({"session_date": session, "close": price, "spot": price})

    result = apply_portfolio_caps(
        ["AAA", "AAB", "BBB"],
        sector_by_symbol={"AAA": "technology", "AAB": "technology", "BBB": "energy"},
        history=history,
        as_of_date=history["AAA"][-1]["session_date"],
        config={
            "max_sector_exposure": 0.5,
            "max_correlation_cluster_exposure": 0.4,
            "correlation_lookback_sessions": 30,
            "correlation_threshold": 0.85,
        },
    )
    assert max(result["sector_exposure"].values()) <= 0.5
    assert max(result["cluster_exposure"].values()) <= 0.4
    assert result["binding"]


def test_walk_forward_universe_defaults_to_the_original_24():
    original = _universe_symbols("original_24")
    history = _universe_symbols("history_117")
    assert len(original) == 24
    assert len(history) == 117
    assert set(original) < set(history)


def test_regime_overlay_changes_composition_against_sector_only_counterfactual():
    start = date(2026, 1, 1)
    history = {symbol: [] for symbol in ("SPY", "XLE", "XLV", "QQQ")}
    for index in range(30):
        session = (start + timedelta(days=index)).isoformat()
        closes = {
            "SPY": 120 - index,
            "XLE": 100 * 1.02 ** index,
            "XLV": 100 * 1.003 ** index,
            "QQQ": 100 * 1.01 ** index,
        }
        for symbol, close in closes.items():
            history[symbol].append({
                "session_date": session,
                "close": close,
                "spot": close,
                "volatility_state": "volatile" if symbol == "SPY" else "normal",
            })
    rules = {
        "regime": {
            "trend_lookback_sessions": 20,
            "sector_momentum_lookback_sessions": 20,
            "top_sector_buckets": 1,
            "max_eligible_tickers": 10,
            "always_eligible": [],
            "risk_off_high_vol_buckets": ["healthcare"],
            "risk_off_normal_vol_buckets": ["healthcare"],
            "hard_only_when_proxy_data_missing": False,
        },
        "bucket_proxies": {"energy": "XLE", "healthcare": "XLV", "technology": "QQQ"},
        "ticker_buckets": {"XOM": "energy", "LLY": "healthcare", "NVDA": "technology"},
    }
    universe = {"categories": {}}
    hard_rows = [{"ticker": symbol} for symbol in ("XOM", "LLY", "NVDA")]
    as_of = history["SPY"][-1]["session_date"]
    actual, state = _apply_sector_overlay(
        [dict(row) for row in hard_rows], history=history, as_of_date=as_of,
        rules=rules, universe=universe,
    )
    neutral, _ = _apply_sector_overlay(
        [dict(row) for row in hard_rows], history=history, as_of_date=as_of,
        rules=rules, universe=universe, ignore_regime=True,
    )
    assert state["market_state"]["trend"] == "risk_off"
    assert state["market_state"]["volatility"] == "high"
    assert [row["ticker"] for row in actual] == ["LLY"]
    assert [row["ticker"] for row in neutral] == ["XOM"]


def test_cache_uses_one_timestamped_soft_sector_selection(tmp_path, monkeypatch):
    from options.research import universe_selection as selector

    symbols = ["SPY", "QQQ", "XLE", "XOM"]
    monkeypatch.setattr(selector, "_universe_symbols", lambda: symbols)
    database = tmp_path / "history.sqlite3"
    for symbol, daily_return in (("SPY", 0.001), ("QQQ", 0.0015), ("XLE", 0.003), ("XOM", 0.0025)):
        archive = tmp_path / f"{symbol}.jsonl"
        _write_archive(archive, symbol, daily_return=daily_return)
        OptionsHistoryStore(database).import_jsonl(archive, underlying=symbol, source_kind="theta")

    rules_path = tmp_path / "rules.json"
    rules = json.loads(selector.DEFAULT_RULES_PATH.read_text())
    rules.update({"lookback_sessions": 20, "min_valid_sessions": 20, "minimum_coverage": 0.8})
    rules["hard_filters"].update({
        "min_average_option_volume_60d": 0,
        "min_latest_total_open_interest_60d": 0,
        "min_average_equity_volume": 0,
        "min_average_equity_dollar_volume": 0,
        "max_median_option_spread_fraction": 0.25,
    })
    rules["regime"].update({
        "trend_lookback_sessions": 20,
        "sector_momentum_lookback_sessions": 20,
        "top_sector_buckets": 1,
        "max_eligible_tickers": 10,
        "always_eligible": ["SPY"],
        "hard_only_when_proxy_data_missing": False,
    })
    rules["bucket_proxies"] = {"technology": "QQQ", "energy": "XLE"}
    rules["ticker_buckets"] = {"SPY": "hedges", "QQQ": "technology", "XLE": "energy", "XOM": "energy"}
    rules["portfolio_caps"].update({"correlation_lookback_sessions": 20})
    rules_path.write_text(json.dumps(rules))

    result = build_eligible_cache(
        database=database, rules_path=rules_path, cache_dir=tmp_path / "cache",
    )
    assert result["status"] == "ready"
    assert result["as_of_date"] == "2026-09-25"
    assert set(result["eligible_symbols"]) == {"SPY", "XLE", "XOM"}
    assert (tmp_path / "cache" / "eligible-2026-09-25.json").is_file()
