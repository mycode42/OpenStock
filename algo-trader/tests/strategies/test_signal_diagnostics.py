"""Observable rejection reasons for empty equity-signal scans."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import strategies.runner.scan_signals as scan_signals
import strategies.runner.signal_generation as signal_generation


def _signal_context(*, bars):
    return signal_generation.SignalGenerationContext(
        mode="algo",
        strategy_symbols=["AAPL"],
        positions={},
        bars_by_symbol={"AAPL": bars},
        owner_map={},
        track_owners=True,
        now=datetime(2026, 11, 6, tzinfo=timezone.utc),
        session_name="regular",
        regime={"regime": "range_bound", "active": ["intraday_mean_reversion"]},
        multi_factor_target_signals={},
        llm_generator=None,
        llm_strategy_enabled=False,
        strategy_states={},
        daily_bars={},
        now_utc=datetime(2026, 11, 6, tzinfo=timezone.utc),
        quote_check_time=datetime(2026, 11, 6, tzinfo=timezone.utc),
        selected_symbols=["AAPL"],
        latest_quotes={},
        risk_config={},
        rank_score={},
        lifecycle=None,
        multi_factor_signals={},
        momentum_signals={},
        runtime_settings=SimpleNamespace(),
        pending_by_symbol={},
        universe={},
        fresh_signal_bar_symbols={"AAPL"},
        overnight_swing_signal_bar_symbols=set(),
        scan_errors=[],
        entry_pause_reasons=[],
        signal_diagnostics=[],
    )


def test_missing_bars_produce_a_structured_strategy_symbol_rejection(monkeypatch):
    monkeypatch.setattr(signal_generation, "evaluate_rule_signal", lambda *args, **kwargs: None)
    context = _signal_context(bars=[])

    generated = signal_generation.generate_strategy_signals(context)

    assert generated == []
    diagnostic = context.signal_diagnostics[0]
    assert diagnostic["ticker"] == "AAPL"
    assert diagnostic["strategy"] == "intraday_mean_reversion"
    assert diagnostic["reason_code"] == "missing_bars"
    assert diagnostic["reason"] == "no 5Min bars were available to evaluate this candidate"
    assert diagnostic["timeframe"] == "5Min"
    assert diagnostic["bar_count"] == 0
    assert diagnostic["last_bar_time"] is None
    assert diagnostic["signal_source"] == "rule_evaluation"
    assert diagnostic["regime"] == "range_bound"
    assert diagnostic["position_qty"] == 0.0
    assert diagnostic["last_close"] is None
    assert diagnostic["effective_parameters"]["signal_model"] == "opening_dip"


def test_evaluator_no_signal_is_distinguished_from_missing_data(monkeypatch):
    monkeypatch.setattr(signal_generation, "evaluate_rule_signal", lambda *args, **kwargs: None)
    context = _signal_context(bars=[{"t": "2026-11-06T15:55:00Z"}])

    signal_generation.generate_strategy_signals(context)

    diagnostic = context.signal_diagnostics[0]
    assert diagnostic["reason_code"] == "no_signal"
    assert diagnostic["reason"] == "rule_evaluation returned no qualifying signal"
    assert diagnostic["bar_count"] == 1
    assert diagnostic["last_bar_time"] == "2026-11-06T15:55:00Z"


def test_zero_signal_scan_emits_structured_log_event(monkeypatch, caplog):
    monkeypatch.setattr(scan_signals, "generate_swing_momentum_signals", lambda *args, **kwargs: ({}, {}))

    def no_signal_with_reason(context):
        context.signal_diagnostics.append({
            "ticker": "AAPL", "strategy": "intraday_mean_reversion",
            "reason_code": "missing_bars", "reason": "no bars",
        })
        return []

    monkeypatch.setattr(scan_signals, "generate_strategy_signals", no_signal_with_reason)
    scope = SimpleNamespace(
        runtime_settings=SimpleNamespace(swing_multi_factor_enabled=False),
        owner_map={}, positions={}, strategy_states={}, selected_symbols=["AAPL"],
        today="2026-11-06", mode="algo",
        risk_config={
            "stop_loss_pct_by_strategy": {"swing_momentum": 0.1},
            "trailing_stop_pct_by_strategy": {"swing_momentum": 0.1},
            "trailing_activation_pct_by_strategy": {"swing_momentum": 0.1},
        },
        strategy_symbols=["AAPL"], track_owners=True, coordinator_config=None,
        now=datetime(2026, 11, 6, tzinfo=timezone.utc), regular_close=None,
        session_name="regular", llm_strategy_enabled=False, now_utc=None,
        pending_by_symbol={}, universe={}, scan_errors=[], entry_pause_reasons=[],
        lifecycle=None, multi_factor_parameter_source=None,
    )
    market = SimpleNamespace(
        regime={"ranked": [], "regime": "range_bound"}, regime_name="range_bound",
        daily_bars={}, bars_by_symbol={}, multi_factor_parameters={},
        llm_generator=None, fresh_signal_bar_symbols=set(),
        overnight_swing_signal_bar_symbols=set(),
    )
    quotes = SimpleNamespace(latest_quotes={})

    caplog.set_level("INFO", logger=scan_signals.__name__)
    result = scan_signals.generate_scan_signals(scope, market, quotes)

    assert result.generated == []
    event_record = next(
        record for record in caplog.records
        if record.getMessage().startswith('{')
        and json.loads(record.getMessage()).get("event") == "equity_signal_scan_no_signals"
    )
    event = json.loads(event_record.getMessage())
    assert event["trading_date"] == "2026-11-06"
    assert event["regime"] == "range_bound"
    assert event["entry_pause_reasons"] == []
    assert event["scan_errors"] == []
    assert event["signal_diagnostics"] == result.signal_diagnostics
    assert event["signal_diagnostics"][0]["reason_code"] == "missing_bars"
