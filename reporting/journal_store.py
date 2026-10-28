"""Trade journal stored in the Algo state database.

The table is append-only and de-duplicates on the same keys as the JSONL journal:
one submission per broker order ID or stable client order ID. Writes run in one
transaction, and any failure raises so the caller never silently loses a trade.
"""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3

from runtime.paths import DATA_DIR

DEFAULT_DATABASE = DATA_DIR / "algo_state.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trading_mode TEXT,
    account_id TEXT,
    order_id TEXT,
    client_order_id TEXT,
    payload_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS trade_journal_order_idx ON trade_journal(order_id);
CREATE INDEX IF NOT EXISTS trade_journal_client_idx ON trade_journal(client_order_id);
"""


def order_keys(entry: dict) -> set[tuple[str, str, str]]:
    mode = f"{entry.get('trading_mode') or 'unknown'}:{entry.get('account_id') or 'legacy'}"
    keys = set()
    if entry.get("order_id"):
        keys.add((mode, "order", str(entry["order_id"])))
    if entry.get("client_order_id"):
        keys.add((mode, "client", str(entry["client_order_id"])))
    return keys


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=30)
    connection.executescript(_SCHEMA)
    return connection


def append_entries(path: Path | str, entries: list[dict], *, recorded_at: str) -> list[dict]:
    """Append journal entries that are not already recorded. Returns the entries actually written."""
    if not entries:
        return []
    with closing(_connect(Path(path))) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        existing: set[tuple[str, str, str]] = set()
        # Rows without an order or client ID have no key; an identical payload is a repeat.
        existing_payloads: set[str] = set()
        for (payload,) in connection.execute("SELECT payload_json FROM trade_journal"):
            entry = json.loads(payload)
            existing.update(order_keys(entry))
            existing_payloads.add(json.dumps(entry, sort_keys=True))
        written: list[dict] = []
        for entry in entries:
            keys = order_keys(entry)
            if not keys and json.dumps(entry, sort_keys=True) in existing_payloads:
                continue
            known_order = (
                f"{entry.get('trading_mode') or 'unknown'}:{entry.get('account_id') or 'legacy'}",
                "order", str(entry.get("order_id")),
            )
            # Same rule as the JSONL writer: a replay carrying a new broker order ID
            # is kept as an enrichment event; an exact repeat is skipped.
            if keys & existing and (not entry.get("order_id") or known_order in existing):
                continue
            connection.execute(
                "INSERT INTO trade_journal (trading_mode, account_id, order_id, client_order_id, payload_json, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    entry.get("trading_mode"), entry.get("account_id"), entry.get("order_id"),
                    entry.get("client_order_id"), json.dumps(entry, sort_keys=True), recorded_at,
                ),
            )
            existing.update(keys)
            existing_payloads.add(json.dumps(entry, sort_keys=True))
            written.append(entry)
        return written


def read_entries(path: Path | str) -> list[dict]:
    """Every journal entry in append order. A missing database reads as an empty journal."""
    if not Path(path).exists():
        return []
    with closing(_connect(Path(path))) as connection:
        return [json.loads(payload) for (payload,) in connection.execute(
            "SELECT payload_json FROM trade_journal ORDER BY id")]
