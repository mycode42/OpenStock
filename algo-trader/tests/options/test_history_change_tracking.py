"""Writes to the options history must bump the per-underlying version used to key the chain cache."""

import sqlite3

from options.research.history_store import OptionsHistoryStore


def _store(tmp_path):
    return OptionsHistoryStore(tmp_path / "history.sqlite3")


def _source(connection, source_id="src1", underlying="SPY"):
    connection.execute(
        "INSERT INTO options_history_source (source_id, underlying, source_kind, source_path, source_sha256, "
        "source_size, source_mtime_ns, status, session_count, imported_at) "
        "VALUES (?, ?, 'alpaca', 'p', 'h', 1, 1, 'complete', 1, 't')",
        (source_id, underlying),
    )


def _snapshot(connection, snapshot_id=1, source_id="src1", underlying="SPY", session="2026-10-27"):
    connection.execute(
        "INSERT INTO options_history_snapshot (snapshot_id, source_id, underlying, session_date, as_of, phase, "
        "feed, spot, underlying_close, regime, volatility_state, events_json, equity_signals_json, "
        "event_recommendations_json, dividends_json, iv_surface_json, expirations_json, row_sha256) "
        "VALUES (?, ?, ?, ?, '2026-10-27T16:00:00Z', 'daily_close', 'sip', 500, 500, 'r', 'v', '[]', '[]', '[]', "
        "'{}', '[]', '[]', 'x')",
        (snapshot_id, source_id, underlying, session),
    )


def _version(store, underlying="SPY"):
    return store.data_version(underlying)


def test_snapshot_insert_update_delete_each_bump_the_version(tmp_path):
    store = _store(tmp_path)
    connection = sqlite3.connect(store.path)
    _source(connection)
    connection.commit()
    start = _version(store)
    _snapshot(connection)
    connection.commit()
    after_insert = _version(store)
    connection.execute("UPDATE options_history_snapshot SET spot = 501 WHERE snapshot_id = 1")
    connection.commit()
    after_update = _version(store)
    connection.execute("DELETE FROM options_history_snapshot WHERE snapshot_id = 1")
    connection.commit()
    after_delete = _version(store)
    assert start < after_insert < after_update < after_delete


def test_other_underlyings_are_not_bumped(tmp_path):
    store = _store(tmp_path)
    connection = sqlite3.connect(store.path)
    _source(connection, "srcQ", "QQQ")
    _snapshot(connection, snapshot_id=2, source_id="srcQ", underlying="QQQ")
    connection.commit()
    assert _version(store, "QQQ") >= 1
    assert _version(store, "SPY") == 0


def test_databases_created_without_triggers_get_them_on_demand(tmp_path):
    store = _store(tmp_path)
    connection = sqlite3.connect(store.path)
    connection.executescript(
        "DROP TRIGGER IF EXISTS options_history_snapshot_track_insert; "
        "DROP TRIGGER IF EXISTS options_history_snapshot_track_update; "
        "DROP TRIGGER IF EXISTS options_history_snapshot_track_delete;"
    )
    connection.commit()
    store.ensure_change_tracking()
    _source(connection)
    _snapshot(connection)
    connection.commit()
    assert _version(store) >= 1
