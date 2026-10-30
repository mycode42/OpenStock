import json
from contextlib import closing

from reporting import snapshot_store


def _write(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def _record(day, timestamp, equity, kind="intraday", account="acct-1"):
    return {
        "date": day, "timestamp": timestamp, "equity": equity, "snapshot_kind": kind,
        "trading_mode": "algo", "account_id": account,
    }


def test_import_is_idempotent_and_verifies_every_record(tmp_path):
    directory = tmp_path / "snapshots"
    _write(directory / "intraday" / "2026-10-29.jsonl", [
        json.dumps(_record("2026-10-29", "2026-10-29T10:00:00-04:00", 100_000)),
        json.dumps(_record("2026-10-29", "2026-10-29T10:05:00-04:00", 100_010)),
    ])
    (directory / "2026-10-29.json").write_text(json.dumps(_record("2026-10-29", "2026-10-29T16:00:00-04:00", 100_020, kind="close")))
    database = tmp_path / "algo_state.sqlite3"

    with closing(snapshot_store.connect(database)) as connection:
        records, unreadable = snapshot_store.read_files(directory)
        assert len(records) == 3 and unreadable == 0
        inserted = sum(snapshot_store.insert(connection, r, trading_mode="algo", account_id="acct-1") for r in records)
        assert inserted == 3
        assert snapshot_store.missing_from_store(connection, records) == 0
        again = sum(snapshot_store.insert(connection, r, trading_mode="algo", account_id="acct-1") for r in records)
        assert again == 0

    assert [row["equity"] for row in snapshot_store.load("algo", "acct-1", connection=connect(database))] == [100_000, 100_010, 100_020]


def test_unreadable_files_are_counted_and_missing_rows_are_reported(tmp_path):
    directory = tmp_path / "snapshots"
    _write(directory / "intraday" / "2026-10-29.jsonl", [json.dumps(_record("2026-10-29", "2026-10-29T10:00:00-04:00", 100_000)), "{not json"])
    (directory / "2026-10-28.json").write_text("not json at all")
    database = tmp_path / "algo_state.sqlite3"

    records, unreadable = snapshot_store.read_files(directory)
    assert len(records) == 1
    assert unreadable == 2
    with closing(snapshot_store.connect(database)) as connection:
        assert snapshot_store.missing_from_store(connection, records) == 1


def test_close_is_stored_once_per_day_and_replaced_on_force(tmp_path):
    database = tmp_path / "algo_state.sqlite3"
    with closing(snapshot_store.connect(database)) as connection:
        snapshot_store.store_close(connection, _record("2026-10-29", "2026-10-29T16:00:00-04:00", 100_000, kind="close"), trading_mode="algo", account_id="acct-1")
        snapshot_store.store_close(connection, _record("2026-10-29", "2026-10-29T16:01:00-04:00", 100_500, kind="close"), trading_mode="algo", account_id="acct-1")
        recorded = snapshot_store.recorded_close(connection, trading_mode="algo", account_id="acct-1", market_day="2026-10-29")
    assert recorded["equity"] == 100_500
    assert [row["equity"] for row in snapshot_store.load("algo", "acct-1", kind="close", connection=connect(database))] == [100_500]


def connect(database):
    return snapshot_store.connect(database)
