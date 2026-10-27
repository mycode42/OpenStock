from portfolio.config import STRATEGY_CONFIG, quote_stale_limit_seconds


def test_regular_session_keeps_the_strict_quote_window():
    assert quote_stale_limit_seconds("regular") == STRATEGY_CONFIG["quote_stale_seconds"] == 90


def test_extended_sessions_use_the_wider_quote_window():
    for session in ("premarket", "after_hours", "overnight"):
        assert quote_stale_limit_seconds(session) == STRATEGY_CONFIG["extended_quote_stale_seconds"] == 300


def test_unknown_session_falls_back_to_the_strict_window():
    assert quote_stale_limit_seconds(None) == 90
