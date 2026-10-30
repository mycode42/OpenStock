"""The LLM book honours SELL decisions for owned tickers outside the universe, and nothing else."""

from datetime import datetime, timedelta, timezone

from strategies.llm_driven.ai import LLMSignalBook, publish_llm_signal_snapshot
from strategies.llm_driven.owned import owned_long_positions

NOW = datetime(2026, 10, 29, 15, 0, tzinfo=timezone.utc)


def _snapshot(path, decisions, owned):
    publish_llm_signal_snapshot(path, {
        "schema_version": 1, "strategy": "swing_llm_driven", "run_id": "run-1",
        "generated_at": NOW.isoformat(), "expires_at": (NOW + timedelta(minutes=90)).isoformat(),
        "universe": sorted({*decisions, *owned}), "owned_tickers": owned,
        "decisions": {
            ticker: {"action": action, "confidence": 80.0, "reason": "test", "run_id": "run-1", "generated_at": NOW.isoformat()}
            for ticker, action in decisions.items()
        },
    }, now=NOW)
    return path


def test_owned_ticker_outside_the_universe_may_be_sold_but_not_bought(tmp_path):
    path = _snapshot(tmp_path / "snap.sqlite3", {"HELD": "sell", "NEW": "buy"}, ["HELD"])
    book = LLMSignalBook(path, now=NOW)
    assert book.recommendation("HELD", universe={"AAPL"}).action == "sell"
    assert book.recommendation("NEW", universe={"AAPL"}) is None


def test_a_sell_for_a_universe_member_is_unchanged(tmp_path):
    path = _snapshot(tmp_path / "snap.sqlite3", {"AAPL": "buy"}, [])
    book = LLMSignalBook(path, now=NOW)
    assert book.recommendation("AAPL", universe={"AAPL"}).action == "buy"


def test_owned_longs_come_from_the_ownership_table_only(tmp_path):
    import sqlite3
    state = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(state)
    connection.execute("CREATE TABLE position_owner (ticker TEXT, strategy TEXT)")
    connection.executemany("INSERT INTO position_owner VALUES (?, ?)", [("HELD", "swing_llm_driven"), ("OTHER", "swing_momentum")])
    connection.commit(); connection.close()
    positions = [
        {"symbol": "HELD", "qty": "5", "avg_entry_price": "100", "unrealized_plpc": "0.02"},
        {"symbol": "OTHER", "qty": "3", "avg_entry_price": "50", "unrealized_plpc": "0"},
        {"symbol": "HELD", "qty": "-2", "avg_entry_price": "100", "unrealized_plpc": "0"},
    ]
    owned = owned_long_positions(positions, state_path=state)
    assert owned == [{"ticker": "HELD", "qty": 5.0, "avg_entry_price": 100.0, "unrealized_plpc": 0.02}]
