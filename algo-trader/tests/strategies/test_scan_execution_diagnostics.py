import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from strategies.runner.scan_execution import execute_scan_candidates


def test_stale_quote_is_logged_with_a_stable_reason_code(caplog):
    now = datetime.now(timezone.utc)
    scope = SimpleNamespace(
        entry_pause_reasons=[], today="2026-11-06", strategy_states={},
        owner_map={}, risk_config={}, mode="manual", open_orders=[],
        positions={}, coordinator_config=None, equity=100_000, session_name="regular",
        regular_open=None, regular_close=None, extended_hours=False,
        pending_by_symbol={}, mode_config={}, now=now, scan_errors=[],
    )
    market = SimpleNamespace(regime_name="range_bound", regime={})
    quotes = SimpleNamespace(
        latest_quotes={"AAPL": {
            "price": 100.0, "timestamp": now - timedelta(minutes=5),
        }},
        freshness_limit=60,
    )
    generated = [{
        "ticker": "AAPL", "strategy": "intraday_mean_reversion",
        "action": "buy", "signal_bar_time": "2026-11-06T14:55:00Z",
    }]

    caplog.set_level("INFO", logger="strategy_runner")
    result = execute_scan_candidates(
        scope, market, quotes, generated,
        [{"ticker": "AAPL", "strategy": "intraday_mean_reversion",
          "reason_code": "signal_generated"}],
    )

    assert result.trades == []
    decision = result.decision_diagnostics[0]
    assert decision["reason_code"] == "quote_stale"
    assert 299.0 <= decision["quote_age_seconds"] <= 301.0
    event_record = next(
        record for record in caplog.records
        if json.loads(record.getMessage()).get("event") == "equity_scan_decisions"
    )
    event = json.loads(event_record.getMessage())
    assert event["proposal_count"] == 0
    assert event["execution_decisions"] == result.decision_diagnostics
    assert event["signal_decisions"][0]["reason_code"] == "signal_generated"
