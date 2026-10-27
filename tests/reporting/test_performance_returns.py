import json
import logging
from decimal import Decimal

import pytest

from autoresearch.validation import drift_monitor
from reporting import performance
from reporting.fills import calculate_realized_pnl


def _snapshot(day: str, equity: float, account_id: str = "acct-1") -> dict:
    return {
        "date": day, "equity": equity, "account_id": account_id,
        "trading_mode": "algo", "account_name": "Algo",
    }


def _event(when: str, strategy_pnl: dict, *, covered: bool = True) -> dict:
    return {
        "basis_status": "covered" if covered else "unknown",
        "transaction_time": when,
        "strategy_pnl": strategy_pnl,
    }


SNAPSHOTS = [
    _snapshot("2026-10-19", 100_000.0),
    _snapshot("2026-10-20", 101_000.0),
    _snapshot("2026-10-21", 100_500.0),
    _snapshot("2026-10-22", 100_500.0),
    _snapshot("2026-10-23", 99_000.0),
]


def test_builds_one_return_per_fill_split_by_strategy():
    events = [
        # 11:00 ET on Tuesday 2026-10-20, booked against Monday's close.
        _event("2026-10-20T15:00:00+00:00", {"swing_momentum": "1000.00", "swing_llm": "-200"}),
        # 12:00 ET on Thursday 2026-10-22, booked against Wednesday's close.
        _event("2026-10-22T16:00:00+00:00", {"swing_momentum": "-500"}),
    ]

    report = performance.build_strategy_daily_returns(events, SNAPSHOTS)

    assert report["sessions"] == 5
    assert report["skipped"] == {
        "uncovered_fills": 0, "unreadable_time": 0,
        "after_last_snapshot": 0, "before_prior_session": 0,
    }
    momentum = report["strategies"]["swing_momentum"]
    assert [row["date"] for row in momentum] == ["2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"]
    returns = [row["return"] for row in momentum]
    assert returns == pytest.approx([0.01, 0.0, -500 / 100_500, 0.0], abs=1e-8)
    llm = report["strategies"]["swing_llm"]
    assert [row["return"] for row in llm] == [-0.002, 0.0, 0.0, 0.0]
    # Each return times the prior-session equity gives back the realized dollars.
    dollars = sum(r * base for r, base in zip(returns, [100_000, 101_000, 100_500, 100_500]))
    assert dollars == pytest.approx(500, abs=0.01)


def test_uncovered_and_out_of_range_fills_are_counted_not_dropped_silently():
    events = [
        _event("2026-10-20T15:00:00+00:00", {"swing_momentum": "10"}, covered=False),
        _event("2026-10-30T15:00:00+00:00", {"swing_momentum": "10"}),
        _event("2026-10-18T15:00:00+00:00", {"swing_momentum": "10"}),
        {"basis_status": "covered", "transaction_time": "not-a-time", "strategy_pnl": {}},
    ]

    report = performance.build_strategy_daily_returns(events, SNAPSHOTS)

    assert report["skipped"]["uncovered_fills"] == 1
    assert report["skipped"]["after_last_snapshot"] == 1
    assert report["skipped"]["before_prior_session"] == 1
    assert report["skipped"]["unreadable_time"] == 1
    assert report["strategies"] == {}


def test_live_feed_round_trips_into_drift_monitor(tmp_path):
    report = performance.build_strategy_daily_returns(
        [_event("2026-10-20T15:00:00+00:00", {"swing_momentum": "1000"})], SNAPSHOTS,
    )
    feed = tmp_path / "live-feed.json"

    performance.write_live_drift_feed(feed, report)

    payload = json.loads(feed.read_text())
    assert payload["schema"] == drift_monitor.LIVE_FEED_SCHEMA
    assert drift_monitor.load_live_drift_feed(feed) == report["strategies"]


def test_unreadable_snapshot_files_are_skipped_and_counted(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(performance, "DATA_DIR", tmp_path)
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir()
    (snapshot_dir / "2026-10-19.json").write_text(json.dumps(_snapshot("2026-10-19", 100_000.0)))
    (snapshot_dir / "2026-10-20.json").write_text(json.dumps(_snapshot("2026-10-20", 101_000.0)))
    (snapshot_dir / "2026-10-21.json").write_text("{not json")
    (snapshot_dir / "2026-10-22.json").write_text(json.dumps([1, 2, 3]))
    (snapshot_dir / "2026-10-23.json").write_bytes(b"\xff\xfe\x00")

    with caplog.at_level(logging.WARNING, logger="perf_v2"):
        snapshots, skipped = performance.load_snapshots_with_skips(mode="algo", account_id="acct-1")

    assert [row["date"] for row in snapshots] == ["2026-10-19", "2026-10-20"]
    assert skipped == 3
    assert "Skipped 3 of 5 snapshot file(s)" in caplog.text
    assert performance.load_snapshots(mode="algo", account_id="acct-1") == snapshots


def test_fill_round_trip_has_no_duplicated_realized_pnl():
    activities = [
        {"id": "a1", "activity_type": "FILL", "order_id": "B1", "symbol": "NVDA", "side": "buy",
         "qty": "100", "price": "10", "transaction_time": "2026-10-01T14:00:00Z"},
        {"id": "a2", "activity_type": "FILL", "order_id": "S1", "symbol": "NVDA", "side": "sell",
         "qty": "40", "price": "12", "transaction_time": "2026-10-02T14:00:00Z"},
        {"id": "a3", "activity_type": "FILL", "order_id": "S2", "symbol": "NVDA", "side": "sell",
         "qty": "60", "price": "13", "transaction_time": "2026-10-03T14:00:00Z"},
    ]
    journal = [
        {"trading_mode": "algo", "account_id": "acct-1", "order_id": "B1", "strategy": "swing_momentum",
         "ticker": "NVDA", "action": "buy", "status": "submitted"},
        {"trading_mode": "algo", "account_id": "acct-1", "order_id": "S1", "strategy": "swing_momentum",
         "ticker": "NVDA", "action": "sell", "status": "submitted"},
        {"trading_mode": "algo", "account_id": "acct-1", "order_id": "S2", "strategy": "swing_momentum",
         "ticker": "NVDA", "action": "sell", "status": "submitted"},
    ]
    # A broker enrichment row for the same order must not add a second submission.
    journal_with_replay = journal + [{**journal[2], "broker_status": "filled", "filled_qty": "60"}]

    report = calculate_realized_pnl(activities, journal_with_replay, mode="algo", current_positions=[])

    assert Decimal(report["realized_pnl"]) == Decimal("260")
    assert Decimal(report["strategy_pnl"]["swing_momentum"]) == Decimal("260")
    assert sum(Decimal(event["realized_pnl"]) for event in report["realized_fills"]) == Decimal("260")
    assert [Decimal(event["realized_pnl"]) for event in report["realized_fills"]] == [Decimal("80"), Decimal("180")]

    returns = performance.build_strategy_daily_returns(
        report["realized_fills"],
        [_snapshot("2026-10-01", 100_000.0), _snapshot("2026-10-02", 100_000.0),
         _snapshot("2026-10-05", 100_000.0)],
    )
    attributed = sum(
        row["return"] * 100_000 for rows in returns["strategies"].values() for row in rows
    )
    assert attributed == pytest.approx(260, abs=0.01)
