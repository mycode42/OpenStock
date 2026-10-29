import json
from datetime import datetime, timedelta, timezone

from strategies.llm_driven import ai_runner


def _bar(timestamp: datetime, close: float, *, volume: int = 100) -> dict:
    return {
        "t": timestamp.isoformat().replace("+00:00", "Z"),
        "o": close - 0.1,
        "h": close + 0.2,
        "l": close - 0.2,
        "c": close,
        "v": volume,
        "vw": close - 0.05,
    }


def test_market_regime_context_calculates_live_price_vwap_and_momentum():
    now = datetime(2026, 10, 29, 14, 0, tzinfo=timezone.utc)
    intraday = [
        _bar(now - timedelta(minutes=30 - index), 100 + index * 0.1)
        for index in range(31)
    ]
    daily = [
        _bar(datetime(2026, 10, day, 20, 0, tzinfo=timezone.utc), 95 + index)
        for index, day in enumerate((21, 22, 23, 26, 27, 28))
    ]
    quotes = {
        symbol: {"price": price, "timestamp": now - timedelta(seconds=10)}
        for symbol, price in (("SPY", 103.2), ("QQQ", 203.2))
    }
    quotes["SPY"]["timestamp"] = now + timedelta(seconds=2)

    context, diagnostics = ai_runner._market_regime_context(
        now=now,
        quotes=quotes,
        intraday_bars={"SPY": intraday, "QQQ": [
            {**bar, "o": bar["o"] + 100, "h": bar["h"] + 100,
             "l": bar["l"] + 100, "c": bar["c"] + 100, "vw": bar["vw"] + 100}
            for bar in intraday
        ]},
        daily_bars={"SPY": daily, "QQQ": [
            {**bar, "c": bar["c"] + 100} for bar in daily
        ]},
    )

    spy = context["SPY"]
    assert spy["prices"]["current"] == 103.2
    assert spy["prices"]["change_1d"] > 0
    assert spy["prices"]["change_5d"] > 0
    assert spy["intraday"]["vwap"] is not None
    assert spy["intraday"]["price_vs_vwap_pct"] > 0
    assert spy["intraday"]["momentum_30m_pct"] > 0
    assert len(spy["intraday"]["bars_1min"]) == 30
    assert diagnostics["symbols"]["SPY"]["price_source"] == "fresh_quote"
    assert diagnostics["status"] == "ok"


def test_run_analysis_uses_current_live_universe_and_passes_market_context(monkeypatch, tmp_path):
    tickers = ["AAPL", "SPY", "QQQ"]
    context = {"SPY": {"prices": {"current": 100}}}
    captured = {}
    monkeypatch.setattr(ai_runner, "load_algo_universe", lambda: {"tickers": tickers})
    monkeypatch.setattr(ai_runner, "get_alpaca_account", lambda **_kwargs: {"equity": "100000"})
    monkeypatch.setattr(ai_runner, "get_alpaca_positions", lambda **_kwargs: [])
    monkeypatch.setattr(ai_runner, "convert_to_portfolio", lambda *_args: {"cash": 100000})
    monkeypatch.setattr(
        ai_runner, "_fetch_market_regime_context",
        lambda: (context, {"status": "ok", "symbols": {}}),
    )

    def run_graph(**kwargs):
        captured.update(kwargs)
        return {
            "decisions": {
                ticker: {"action": "watch", "confidence": 50, "reason": "Signals are mixed."}
                for ticker in kwargs["tickers"]
            },
            "analyst_signals": {},
        }

    monkeypatch.setattr(ai_runner, "run_event_analysis", run_graph)
    monkeypatch.setenv("LLM_MODEL", "test-model")

    output = tmp_path / "signals.json"
    report = ai_runner.run_analysis(output_path=output)

    snapshot = json.loads(output.read_text())
    assert snapshot["universe"] == tickers
    assert report["universe_count"] == len(tickers)
    assert captured["tickers"] == tickers
    assert captured["ticker_data"] == context
    assert report["market_data_diagnostics"]["status"] == "ok"
