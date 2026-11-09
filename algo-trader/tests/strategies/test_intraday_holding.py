"""Intraday holdings close after a fixed number of sessions after entry, like the swing exits."""

from strategies.rule_pipeline import INTRADAY_MAX_HOLDING_SESSIONS, _sessions_after_entry, evaluate_rule_signal


def test_sessions_count_weekdays_after_the_entry_session():
    entry = "2026-11-09T15:00:00+00:00"  # Monday 10:00 ET
    assert _sessions_after_entry(entry, "2026-11-09T19:00:00+00:00") == 0
    assert _sessions_after_entry(entry, "2026-11-10T15:00:00+00:00") == 1
    assert _sessions_after_entry(entry, "2026-11-11T15:00:00+00:00") == 2
    # Saturday is not a session: Monday entry, Monday of the next week is five sessions later.
    assert _sessions_after_entry(entry, "2026-11-16T15:00:00+00:00") == 5


def test_missing_timestamps_give_no_count():
    assert _sessions_after_entry(None, "2026-11-10T15:00:00+00:00") is None
    assert _sessions_after_entry("not a time", "2026-11-10T15:00:00+00:00") is None


def test_intraday_position_is_closed_at_the_holding_limit():
    bar = {"t": "2026-11-12T15:00:00Z", "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1000}
    signal = evaluate_rule_signal(
        "intraday_momentum", "AAPL", [bar], parameters={},
        position_qty=10, entry_at="2026-11-09T15:00:00Z",
    )
    assert signal is not None
    assert signal.action == "sell"
    assert "maximum holding reached" in signal.reason
    assert INTRADAY_MAX_HOLDING_SESSIONS == 3
