"""Continuous equity snapshots for the dedicated Momentum paper account."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from market.sessions import ET
from reporting import snapshot_store


TRADING_MODE = "momentum"
INTRADAY_INTERVAL = timedelta(minutes=5)


def build_snapshot(kind: str, now: datetime, account: dict, position_count: int, account_id: str) -> dict:
    """Shape a Momentum account point like the other paper-account snapshots."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Snapshot time must be timezone-aware")
    local = now.astimezone(ET)
    equity = float(account["equity"])
    last_equity = float(account.get("last_equity") or 0)
    daily_pnl = equity - last_equity if last_equity > 0 else None
    cash = float(account.get("cash") or 0)
    return {
        "date": local.strftime("%Y-%m-%d"),
        "timestamp": local.isoformat(),
        "snapshot_kind": kind,
        "trading_mode": TRADING_MODE,
        "account_name": "Momentum",
        "account_id": str(account.get("id") or account_id),
        "equity": equity,
        "cash": cash,
        "invested": equity - cash,
        "position_count": position_count,
        "daily_pnl": daily_pnl,
        "daily_pnl_pct": round(daily_pnl / last_equity * 100, 4) if daily_pnl is not None else None,
    }


def write_snapshot(database: Path | None, account_id: str, snapshot: dict) -> None:
    """Store a Momentum point in the shared account snapshot table."""
    with closing(snapshot_store.connect(database)) as connection:
        if snapshot["snapshot_kind"] == "intraday":
            snapshot_store.insert(connection, snapshot, trading_mode=TRADING_MODE, account_id=account_id)
        else:
            snapshot_store.store_close(connection, snapshot, trading_mode=TRADING_MODE, account_id=account_id)


class MomentumSnapshotSchedule:
    """Capture five-minute points and one close after each market session."""

    def __init__(self, client, database: Path | None = None):
        self.client = client
        self.database = database
        self.last_intraday: datetime | None = None
        self.close_day: str | None = None

    def tick(self, now: datetime, window) -> str | None:
        captured = None
        if self.last_intraday is None or now - self.last_intraday >= INTRADAY_INTERVAL:
            self.last_intraday = now
            self._capture("intraday", now)
            captured = "intraday"

        if (
            window is not None
            and window.name == "after_hours"
            and now >= window.regular_close
            and self.close_day != window.trade_date
        ):
            with closing(snapshot_store.connect(self.database)) as connection:
                recorded = snapshot_store.recorded_close(
                    connection,
                    trading_mode=TRADING_MODE,
                    account_id=self.client.account_id,
                    market_day=window.trade_date,
                )
            if recorded is not None:
                self.close_day = window.trade_date
            else:
                self._capture("close", now)
                self.close_day = window.trade_date
                captured = "close"
        return captured

    def _capture(self, kind: str, now: datetime) -> None:
        account = self.client.verify_account()
        account_id = str(account.get("id") or account.get("account_number") or "")
        if not account_id:
            raise ValueError("Momentum Alpaca account omitted its ID")
        positions = self.client.positions()
        snapshot = build_snapshot(kind, now, account, len(positions), account_id)
        write_snapshot(self.database, account_id, snapshot)
