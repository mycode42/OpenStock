"""Intraday and close equity snapshots for the Alpaca options paper account.

Records use the same shape as the Algo account snapshots in reporting/performance.py and
are stored in the same table, keyed by the options account ID.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from market.sessions import ET
from reporting import snapshot_store


INTRADAY_INTERVAL = timedelta(minutes=5)
TRADING_MODE = "options"


def build_snapshot(kind: str, now: datetime, account: dict, position_count: int, account_id: str) -> dict:
    """Shape one options account point like an Algo snapshot."""
    local = now.astimezone(ET)
    equity = float(account["equity"])
    last_equity = float(account.get("last_equity") or 0)
    daily_pnl = equity - last_equity if last_equity > 0 else None
    return {
        "date": local.strftime("%Y-%m-%d"),
        "timestamp": local.isoformat(),
        "snapshot_kind": kind,
        "trading_mode": TRADING_MODE,
        "account_id": str(account.get("id") or account_id),
        "equity": equity,
        "cash": float(account.get("cash") or 0),
        "invested": float(account.get("long_market_value") or 0) + abs(float(account.get("short_market_value") or 0)),
        "position_count": position_count,
        "daily_pnl": daily_pnl,
        "daily_pnl_pct": round(daily_pnl / last_equity * 100, 4) if daily_pnl is not None else None,
    }


def write_snapshot(database: Path | None, account_id: str, snapshot: dict) -> None:
    """Store one options snapshot under the options account ID."""
    with closing(snapshot_store.connect(database)) as connection:
        if snapshot["snapshot_kind"] == "intraday":
            snapshot_store.insert(connection, snapshot, trading_mode=TRADING_MODE, account_id=account_id)
        else:
            snapshot_store.store_close(connection, snapshot, trading_mode=TRADING_MODE, account_id=account_id)


class OptionsSnapshotSchedule:
    """Decides which snapshot is due on each poll of the options daemon.

    Intraday points come every five minutes, around the clock. The close is taken once
    per trading day, on the first poll after the regular close.
    """

    def __init__(self, client, database: Path | None = None):
        self.client = client
        self.database = database
        self.last_intraday: datetime | None = None
        self.close_day: str | None = None

    def tick(self, now: datetime, window) -> str | None:
        """Capture what is due on this poll and return its kind, if any."""
        captured = None
        if self.last_intraday is None or now - self.last_intraday >= INTRADAY_INTERVAL:
            self.last_intraday = now
            self._capture("intraday", now)
            captured = "intraday"
        if window is not None and now >= window.regular_close and self.close_day != window.trade_date:
            with closing(snapshot_store.connect(self.database)) as connection:
                recorded = snapshot_store.recorded_close(
                    connection, trading_mode=TRADING_MODE, account_id=self.client.account_id, market_day=window.trade_date,
                )
            if recorded is not None:
                self.close_day = window.trade_date
            else:
                # Recorded only after a successful write, so a failed close is retried on the next poll.
                self._capture("close", now)
                self.close_day = window.trade_date
                captured = "close"
        return captured

    def _capture(self, kind: str, now: datetime) -> None:
        account = self.client.verify_account()
        positions = self.client.positions()
        snapshot = build_snapshot(kind, now, account, len(positions), self.client.account_id)
        write_snapshot(self.database, self.client.account_id, snapshot)
