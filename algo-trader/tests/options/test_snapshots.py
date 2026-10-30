from contextlib import closing
from datetime import datetime

import pytest

from market.sessions import ET, TradingWindow
from options.snapshots import OptionsSnapshotSchedule, build_snapshot
from reporting import snapshot_store


DAY = "2026-10-29"
ACCOUNT = "options-paper-test"


class FakeOptionsClient:
    account_id = ACCOUNT

    def __init__(self):
        self.fail_next = False

    def verify_account(self):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("broker unavailable")
        return {
            "id": ACCOUNT, "equity": "100500.00", "last_equity": "100000.00", "cash": "5000",
            "long_market_value": "95500", "short_market_value": "0",
        }

    def positions(self):
        return [{"symbol": "SPY261120C00500000"}]


def at(hour, minute=0):
    return datetime(2026, 10, 29, hour, minute, tzinfo=ET)


def window(name):
    return TradingWindow(
        name, DAY, name != "regular", "sip", "sip",
        at(9, 30), at(16, 0),
    )


def stored(database, kind=None):
    with closing(snapshot_store.connect(database)) as connection:
        return snapshot_store.load("options", ACCOUNT, kind=kind, connection=connection)


def test_snapshot_uses_the_algo_record_shape_with_daily_pnl():
    snapshot = build_snapshot("intraday", at(10), FakeOptionsClient().verify_account(), 1, ACCOUNT)

    assert snapshot["trading_mode"] == "options"
    assert snapshot["snapshot_kind"] == "intraday"
    assert snapshot["date"] == DAY
    assert snapshot["timestamp"] == "2026-10-29T10:00:00-04:00"
    assert snapshot["equity"] == 100500.0
    assert snapshot["daily_pnl"] == 500.0
    assert snapshot["daily_pnl_pct"] == 0.5
    assert snapshot["invested"] == 95500.0
    assert snapshot["position_count"] == 1


def test_intraday_points_are_taken_every_five_minutes_around_the_clock(tmp_path):
    database = tmp_path / "algo_state.sqlite3"
    schedule = OptionsSnapshotSchedule(FakeOptionsClient(), database=database)

    assert schedule.tick(at(3), None) == "intraday"
    assert schedule.tick(at(3, 3), window("premarket")) is None
    assert schedule.tick(at(3, 5), window("premarket")) == "intraday"
    assert schedule.tick(at(9, 30), window("regular")) == "intraday"

    assert [row["timestamp"] for row in stored(database, "intraday")] == [
        "2026-10-29T03:00:00-04:00",
        "2026-10-29T03:05:00-04:00",
        "2026-10-29T09:30:00-04:00",
    ]


def test_close_waits_for_the_regular_close(tmp_path):
    database = tmp_path / "algo_state.sqlite3"
    schedule = OptionsSnapshotSchedule(FakeOptionsClient(), database=database)

    schedule.tick(at(15, 50), window("regular"))
    assert stored(database, "close") == []


def test_close_is_written_once_after_the_regular_close(tmp_path):
    database = tmp_path / "algo_state.sqlite3"
    schedule = OptionsSnapshotSchedule(FakeOptionsClient(), database=database)

    assert schedule.tick(at(16, 5), window("after_hours")) == "close"
    assert schedule.tick(at(16, 7), window("after_hours")) is None

    closes = stored(database, "close")
    assert len(closes) == 1
    assert closes[0]["snapshot_kind"] == "close"
    assert closes[0]["equity"] == 100500.0


def test_failed_close_is_retried_on_the_next_poll(tmp_path):
    database = tmp_path / "algo_state.sqlite3"
    client = FakeOptionsClient()
    client.fail_next = True
    schedule = OptionsSnapshotSchedule(client, database=database)

    with pytest.raises(RuntimeError):
        schedule.tick(at(16, 5), window("after_hours"))
    assert schedule.tick(at(16, 6), window("after_hours")) == "close"
    assert len(stored(database, "close")) == 1


def test_existing_close_is_not_overwritten_after_a_restart(tmp_path):
    database = tmp_path / "algo_state.sqlite3"
    with closing(snapshot_store.connect(database)) as connection:
        existing = {"date": DAY, "snapshot_kind": "close", "equity": 99000.0, "timestamp": "2026-10-29T16:01:00-04:00"}
        snapshot_store.store_close(connection, existing, trading_mode="options", account_id=ACCOUNT)
    schedule = OptionsSnapshotSchedule(FakeOptionsClient(), database=database)

    schedule.tick(at(16, 5), window("after_hours"))
    assert schedule.close_day == DAY
    closes = stored(database, "close")
    assert [row["equity"] for row in closes] == [99000.0]
