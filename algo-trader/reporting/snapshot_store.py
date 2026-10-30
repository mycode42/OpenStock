"""Account equity snapshots, kept in the algo state database.

One row per snapshot: intraday points through the day and one close per trading day,
for the Algo account and the options paper account. Risk baselines, the performance
report and the Dashboard read from here.
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

from runtime.paths import DATA_DIR

KINDS = ("intraday", "close")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS account_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trading_mode TEXT NOT NULL,
    account_id TEXT NOT NULL,
    snapshot_kind TEXT NOT NULL CHECK (snapshot_kind IN ('intraday', 'close')),
    market_day TEXT NOT NULL,
    taken_at TEXT NOT NULL,
    equity REAL NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE (trading_mode, account_id, snapshot_kind, taken_at)
);
CREATE INDEX IF NOT EXISTS account_snapshots_day_idx
    ON account_snapshots (trading_mode, account_id, snapshot_kind, market_day);
"""


def database_path() -> Path:
    return DATA_DIR / "algo_state.sqlite3"


def connect(path: Path | None = None) -> sqlite3.Connection:
    connection = sqlite3.connect(path or database_path(), timeout=30)
    connection.executescript(_SCHEMA)
    return connection


def _taken_at(record: dict) -> str:
    return str(record.get("timestamp") or record["date"])


def _check(record: dict) -> float:
    if record.get("snapshot_kind") not in KINDS:
        raise ValueError(f"Unsupported snapshot kind: {record.get('snapshot_kind')!r}")
    equity = float(record["equity"])
    if not math.isfinite(equity):
        raise ValueError("Snapshot equity must be finite")
    return equity


def insert(connection: sqlite3.Connection, record: dict, *, trading_mode: str, account_id: str) -> bool:
    """Add one snapshot. Returns False when the same snapshot is already stored."""
    equity = _check(record)
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO account_snapshots
            (trading_mode, account_id, snapshot_kind, market_day, taken_at, equity, payload_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trading_mode, account_id, record["snapshot_kind"], str(record["date"])[:10],
            _taken_at(record), equity, json.dumps(record, sort_keys=True, allow_nan=False),
        ),
    )
    connection.commit()
    return cursor.rowcount == 1


def store_close(connection: sqlite3.Connection, record: dict, *, trading_mode: str, account_id: str) -> None:
    """Store the day's close, replacing any close already recorded for that day."""
    _check({**record, "snapshot_kind": "close"})
    connection.execute(
        """
        DELETE FROM account_snapshots
        WHERE trading_mode = ? AND account_id = ? AND snapshot_kind = 'close' AND market_day = ?
        """,
        (trading_mode, account_id, str(record["date"])[:10]),
    )
    insert(connection, {**record, "snapshot_kind": "close"}, trading_mode=trading_mode, account_id=account_id)


def recorded_close(connection: sqlite3.Connection, *, trading_mode: str, account_id: str, market_day: str) -> dict | None:
    row = connection.execute(
        """
        SELECT payload_json FROM account_snapshots
        WHERE trading_mode = ? AND account_id = ? AND snapshot_kind = 'close' AND market_day = ?
        ORDER BY taken_at DESC LIMIT 1
        """,
        (trading_mode, account_id, market_day),
    ).fetchone()
    return json.loads(row[0]) if row else None


def load(
    trading_mode: str, account_id: str | None = None, *, kind: str | None = None,
    connection: sqlite3.Connection | None = None,
) -> list[dict]:
    """Stored snapshots for one mode, optionally one account and one kind, oldest first."""
    query = "SELECT payload_json FROM account_snapshots WHERE trading_mode = ?"
    params: list = [trading_mode]
    if account_id is not None:
        query += " AND account_id = ?"
        params.append(account_id)
    if kind is not None:
        query += " AND snapshot_kind = ?"
        params.append(kind)
    query += " ORDER BY taken_at"
    owned = connection is None
    connection = connection or connect()
    try:
        return [json.loads(row[0]) for row in connection.execute(query, params)]
    finally:
        if owned:
            connection.close()


def read_files(directory: Path) -> tuple[list[dict], int]:
    """Read snapshot records from the old JSON and JSONL files. Returns (records, unreadable count)."""
    records: list[dict] = []
    unreadable = 0
    if not directory.exists():
        return records, unreadable
    for path in sorted(directory.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            unreadable += 1
            continue
        if isinstance(record, dict):
            records.append(record)
        else:
            unreadable += 1
    intraday = directory / "intraday"
    for path in sorted(intraday.glob("*.jsonl")) if intraday.exists() else []:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                unreadable += 1
                continue
            if isinstance(record, dict):
                records.append(record)
            else:
                unreadable += 1
    return records, unreadable


def missing_from_store(connection: sqlite3.Connection, records: list[dict], *, account_id: str | None = None) -> int:
    """How many records have no stored row with the same mode, account, kind and timestamp."""
    missing = 0
    for record in records:
        key_account = account_id or str(record.get("account_id") or "")
        row = connection.execute(
            """
            SELECT 1 FROM account_snapshots
            WHERE trading_mode = ? AND account_id = ? AND snapshot_kind = ? AND taken_at = ?
            """,
            (record.get("trading_mode"), key_account, record.get("snapshot_kind"), _taken_at(record)),
        ).fetchone()
        if row is None:
            missing += 1
    return missing
