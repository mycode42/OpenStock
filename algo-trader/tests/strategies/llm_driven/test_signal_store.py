"""The AI signal snapshot is published to SQLite as one validated row, and readers see only the newest whole run."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from strategies.llm_driven import ai
from strategies.llm_driven.ai import LLMSignalBook, publish_llm_signal_snapshot, read_latest_llm_signal_payload

NOW = datetime(2026, 10, 29, 15, 0, tzinfo=timezone.utc)


def _payload(run_id, generated=NOW, minutes=90):
    return {
        "schema_version": 1, "strategy": "swing_llm_driven", "run_id": run_id,
        "generated_at": generated.isoformat(), "expires_at": (generated + timedelta(minutes=minutes)).isoformat(),
        "universe": ["AAPL"], "owned_tickers": [],
        "decisions": {"AAPL": {"action": "buy", "confidence": 80.0, "reason": "test", "run_id": run_id, "generated_at": generated.isoformat()}},
    }


def test_the_newest_published_run_is_the_one_readers_load(tmp_path):
    db = tmp_path / "signals.sqlite3"
    publish_llm_signal_snapshot(db, _payload("run-1"), now=NOW)
    publish_llm_signal_snapshot(db, _payload("run-2", NOW + timedelta(minutes=5)), now=NOW)
    assert read_latest_llm_signal_payload(db)["run_id"] == "run-2"
    book = LLMSignalBook(db, now=NOW + timedelta(minutes=6))
    assert book.status == "ready" and book.run_id == "run-2"


def test_a_rejected_run_is_never_stored(tmp_path):
    db = tmp_path / "signals.sqlite3"
    publish_llm_signal_snapshot(db, _payload("run-1"), now=NOW)
    expired = _payload("run-2", NOW - timedelta(hours=3))
    with pytest.raises(ValueError):
        publish_llm_signal_snapshot(db, expired, now=NOW)
    assert read_latest_llm_signal_payload(db)["run_id"] == "run-1"
    with sqlite3.connect(db) as connection:
        assert connection.execute(f"SELECT COUNT(*) FROM {ai.SNAPSHOT_TABLE}").fetchone()[0] == 1


def test_no_database_or_empty_database_is_missing_not_invalid(tmp_path):
    assert read_latest_llm_signal_payload(tmp_path / "absent.sqlite3") is None
    assert LLMSignalBook(tmp_path / "absent.sqlite3", now=NOW).status == "missing"
    empty = tmp_path / "empty.sqlite3"
    sqlite3.connect(empty).close()
    assert read_latest_llm_signal_payload(empty) is None
    assert LLMSignalBook(empty, now=NOW).status == "missing"


def test_history_is_capped_at_the_retention_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(ai, "SNAPSHOT_RETENTION", 3)
    db = tmp_path / "signals.sqlite3"
    for index in range(5):
        publish_llm_signal_snapshot(db, _payload(f"run-{index}", NOW + timedelta(minutes=index)), now=NOW)
    with sqlite3.connect(db) as connection:
        assert [row[0] for row in connection.execute(f"SELECT run_id FROM {ai.SNAPSHOT_TABLE} ORDER BY id")] == ["run-2", "run-3", "run-4"]
