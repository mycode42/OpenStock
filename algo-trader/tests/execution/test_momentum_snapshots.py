from contextlib import closing
from datetime import datetime

from market.sessions import ET, TradingWindow
from execution.momentum_snapshots import MomentumSnapshotSchedule, build_snapshot
from reporting import snapshot_store


DAY = "2026-10-29"
ACCOUNT = "momentum-paper-test"


class FakeMomentumClient:
    account_id = ACCOUNT

    def verify_account(self):
        return {
            "id": ACCOUNT,
            "equity": "100500.00",
            "last_equity": "100000.00",
            "cash": "5000",
        }

    def positions(self):
        return [{"symbol": "SPY"}]


def at(hour, minute=0):
    return datetime(2026, 10, 29, hour, minute, tzinfo=ET)


def window(name):
    return TradingWindow(name, DAY, name != "regular", "sip", "sip", at(9, 30), at(16, 0))


def stored(database, kind=None):
    with closing(snapshot_store.connect(database)) as connection:
        return snapshot_store.load("momentum", ACCOUNT, kind=kind, connection=connection)


def test_momentum_snapshot_matches_shared_account_shape():
    snapshot = build_snapshot("intraday", at(10), FakeMomentumClient().verify_account(), 1, ACCOUNT)

    assert snapshot["trading_mode"] == "momentum"
    assert snapshot["snapshot_kind"] == "intraday"
    assert snapshot["date"] == DAY
    assert snapshot["timestamp"] == "2026-10-29T10:00:00-04:00"
    assert snapshot["equity"] == 100500.0
    assert snapshot["daily_pnl"] == 500.0
    assert snapshot["daily_pnl_pct"] == 0.5
    assert snapshot["invested"] == 95500.0
    assert snapshot["position_count"] == 1


def test_momentum_snapshots_continue_every_five_minutes_and_close_once(tmp_path):
    database = tmp_path / "algo_state.sqlite3"
    schedule = MomentumSnapshotSchedule(FakeMomentumClient(), database=database)

    assert schedule.tick(at(3), None) == "intraday"
    assert schedule.tick(at(3, 3), window("premarket")) is None
    assert schedule.tick(at(3, 5), window("premarket")) == "intraday"
    assert schedule.tick(at(16), window("regular")) == "intraday"
    assert schedule.tick(at(16, 5), window("after_hours")) == "close"
    assert schedule.tick(at(16, 6), window("after_hours")) is None

    assert [row["timestamp"] for row in stored(database, "intraday")] == [
        "2026-10-29T03:00:00-04:00",
        "2026-10-29T03:05:00-04:00",
        "2026-10-29T16:00:00-04:00",
        "2026-10-29T16:05:00-04:00",
    ]
    assert len(stored(database, "close")) == 1
