"""Broker order history kept in the Algo state database.

Alpaca returns at most 500 orders per request, so reconciliation cannot rely on
one request once the account has more history than that. The ledger pages
through orders by submission time, upserts them by order ID, and reads the full
history back locally.

Each reconciliation cycle fetches only an overlapping window from the last
cursor, so status changes to recent orders are picked up. Once per ET trading
day it re-reads the whole history from inception. The history is complete only
after the first full sync, so reconciliation never treats a partial ledger as
complete.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


PAGE_SIZE = 500
CURSOR_OVERLAP = timedelta(days=2)

SCHEMA = """
CREATE TABLE IF NOT EXISTS broker_order_ledger (
    account_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    status TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    filled_at TEXT,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, order_id)
);
CREATE INDEX IF NOT EXISTS broker_order_ledger_submitted
    ON broker_order_ledger (account_id, submitted_at);
CREATE TABLE IF NOT EXISTS broker_order_ledger_meta (
    account_id TEXT PRIMARY KEY,
    cursor_submitted_at TEXT,
    last_full_sync_date TEXT,
    synced_at TEXT NOT NULL
);
"""


class OrderLedgerError(RuntimeError):
    """Broker order history cannot be recorded safely."""


def _utc_iso(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        raise OrderLedgerError("Broker order omitted its submission time")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OrderLedgerError(f"Broker order has an invalid submission time: {text!r}") from exc
    if parsed.tzinfo is None:
        raise OrderLedgerError(f"Broker order submission time lacks a timezone: {text!r}")
    return parsed.astimezone(timezone.utc).isoformat()


class BrokerOrderLedger:
    """Upsert-only order history scoped to one broker account."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        with closing(self._connect()) as connection:
            connection.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _meta(self, account_id: str) -> sqlite3.Row | None:
        with closing(self._connect()) as connection:
            return connection.execute(
                "SELECT * FROM broker_order_ledger_meta WHERE account_id = ?", (account_id,),
            ).fetchone()

    def history_complete(self, account_id: str) -> bool:
        """True once a full sync from account inception has succeeded."""
        meta = self._meta(account_id)
        return meta is not None and meta["last_full_sync_date"] is not None

    def _upsert(self, connection: sqlite3.Connection, account_id: str, orders: list[dict], now: str) -> None:
        for order in orders:
            if not isinstance(order, dict) or not order.get("id") or not order.get("symbol"):
                raise OrderLedgerError("Broker order history contains an unnamed order")
            connection.execute(
                """
                INSERT INTO broker_order_ledger
                    (account_id, order_id, symbol, side, status, submitted_at, filled_at, payload_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, order_id) DO UPDATE SET
                    symbol = excluded.symbol,
                    side = excluded.side,
                    status = excluded.status,
                    submitted_at = excluded.submitted_at,
                    filled_at = excluded.filled_at,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    account_id,
                    str(order["id"]),
                    str(order["symbol"]).upper(),
                    str(order.get("side") or "").lower(),
                    str(order.get("status") or "").lower(),
                    _utc_iso(order.get("submitted_at") or order.get("created_at")),
                    _utc_iso(order["filled_at"]) if order.get("filled_at") else None,
                    json.dumps(order, sort_keys=True, allow_nan=False),
                    now,
                ),
            )

    def sync(self, broker, *, account_id: str, today: date, open_orders: list[dict]) -> None:
        """Pull new or changed orders from the broker and record them.

        Raises ``OrderLedgerError`` or a broker exception on any failure, and the
        caller must treat that as an uncertain reconciliation.
        """
        meta = self._meta(account_id)
        full = meta is None or meta["last_full_sync_date"] != today.isoformat()
        if meta is None or meta["cursor_submitted_at"] is None:
            full = True
        after = None if full else (
            datetime.fromisoformat(meta["cursor_submitted_at"]) - CURSOR_OVERLAP
        ).astimezone(timezone.utc).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        newest = meta["cursor_submitted_at"] if meta is not None and not full else None
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                while True:
                    page = broker.orders_after(after, status="all", limit=PAGE_SIZE)
                    if not isinstance(page, list):
                        raise OrderLedgerError("Broker order history page is malformed")
                    self._upsert(connection, account_id, page, now)
                    if page:
                        last = max(_utc_iso(order.get("submitted_at") or order.get("created_at")) for order in page)
                        newest = last if newest is None else max(newest, last)
                    if len(page) < PAGE_SIZE:
                        break
                    if last == after:
                        raise OrderLedgerError("Broker order pagination did not advance")
                    after = last
                self._upsert(connection, account_id, open_orders, now)
                connection.execute(
                    """
                    INSERT INTO broker_order_ledger_meta
                        (account_id, cursor_submitted_at, last_full_sync_date, synced_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(account_id) DO UPDATE SET
                        cursor_submitted_at = excluded.cursor_submitted_at,
                        last_full_sync_date = excluded.last_full_sync_date,
                        synced_at = excluded.synced_at
                    """,
                    (
                        account_id,
                        newest,
                        today.isoformat() if full else (meta["last_full_sync_date"] if meta else None),
                        now,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def orders(self, account_id: str) -> list[dict]:
        """Return every recorded order for the account, oldest submission first."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT payload_json FROM broker_order_ledger WHERE account_id = ? "
                "ORDER BY submitted_at, order_id",
                (account_id,),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]


__all__ = ["BrokerOrderLedger", "OrderLedgerError", "PAGE_SIZE"]
