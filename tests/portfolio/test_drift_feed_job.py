from __future__ import annotations

import logging
from datetime import datetime
from types import SimpleNamespace

import pytest

from market.sessions import ET
from portfolio import daemon
from portfolio.state_store import AlgoStateStore, empty_portfolio


def _window(name: str = "after_hours", trade_date: str = "2026-10-27") -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        trade_date=trade_date,
        regular_close=datetime(2026, 10, 27, 16, 0, tzinfo=ET),
    )


def _at(hour: int, minute: int, day: int = 27) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=ET)


@pytest.fixture
def builder(monkeypatch):
    calls = []
    behaviour = {"error": None}

    def fake_builder(path, *, mode, account_id):
        calls.append((path, mode, account_id))
        if behaviour["error"] is not None:
            raise behaviour["error"]
        return {"sessions": 5, "realized_pnl_complete": True}

    monkeypatch.setattr(daemon, "write_live_drift_feed_for_account", fake_builder)
    return SimpleNamespace(calls=calls, behaviour=behaviour)


def test_runs_once_after_close_plus_ten_minutes(tmp_path, builder):
    portfolio = {}
    target = tmp_path / "live-drift-feed.json"
    window = _window()

    for when in (_at(16, 5), _at(16, 9)):
        daemon._run_drift_feed_job(portfolio, window, now=when, account_id="acct-1", path=target)
    assert builder.calls == []

    for when in (_at(16, 10), _at(16, 30), _at(17, 0), _at(19, 59)):
        daemon._run_drift_feed_job(portfolio, window, now=when, account_id="acct-1", path=target)

    assert builder.calls == [(target, "algo", "acct-1")]
    assert portfolio["drift_feed"]["status"] == "written"
    assert portfolio["drift_feed"]["date"] == "2026-10-27"
    assert portfolio["drift_feed"]["sessions"] == 5


def test_non_after_hours_windows_never_trigger_the_job(tmp_path, builder):
    portfolio = {}
    for name in ("regular", "premarket", "overnight"):
        daemon._run_drift_feed_job(
            portfolio, _window(name=name), now=_at(16, 30), account_id="acct-1",
            path=tmp_path / "feed.json",
        )
    assert builder.calls == []


def test_state_persists_across_restart_and_does_not_repeat(tmp_path, builder):
    state_path = tmp_path / "algo_state.sqlite3"
    target = tmp_path / "live-drift-feed.json"
    store = AlgoStateStore(state_path)
    _, portfolio = store.load()
    daemon._run_drift_feed_job(portfolio, _window(), now=_at(16, 15), account_id="acct-1", path=target)
    store.save("AFTER_HOURS", portfolio)
    assert len(builder.calls) == 1

    restarted = AlgoStateStore(state_path)
    _, restored = restarted.load()
    assert restored["drift_feed"]["status"] == "written"
    daemon._run_drift_feed_job(restored, _window(), now=_at(18, 0), account_id="acct-1", path=target)

    assert len(builder.calls) == 1


def test_new_session_runs_again_after_its_own_close(tmp_path, builder):
    portfolio = empty_portfolio()
    target = tmp_path / "feed.json"
    daemon._run_drift_feed_job(portfolio, _window(), now=_at(16, 10), account_id="acct-1", path=target)
    next_day = _window(trade_date="2026-10-28")
    daemon._run_drift_feed_job(
        portfolio, next_day, now=datetime(2026, 10, 28, 16, 10, tzinfo=ET),
        account_id="acct-1", path=target,
    )

    assert len(builder.calls) == 2
    assert portfolio["drift_feed"]["date"] == "2026-10-28"


def test_builder_exception_is_logged_and_does_not_raise(tmp_path, builder, caplog):
    builder.behaviour["error"] = RuntimeError("broker unavailable")
    portfolio = {}
    with caplog.at_level(logging.ERROR, logger=daemon.LOG.name):
        daemon._run_drift_feed_job(portfolio, _window(), now=_at(16, 10), account_id="acct-1",
                                   path=tmp_path / "feed.json")

    assert portfolio["drift_feed"]["status"] == "retrying"
    assert portfolio["drift_feed"]["last_error"] == "RuntimeError: broker unavailable"
    assert any("drift feed" in record.getMessage() for record in caplog.records)
    assert not (tmp_path / "feed.json").exists()


def test_retries_are_capped_per_day_and_success_stops_them(tmp_path, builder):
    target = tmp_path / "feed.json"
    portfolio = {}
    builder.behaviour["error"] = OSError("disk busy")
    for minute in range(10, 20):
        daemon._run_drift_feed_job(portfolio, _window(), now=_at(16, minute), account_id="acct-1", path=target)

    assert len(builder.calls) == daemon.DRIFT_FEED_MAX_ATTEMPTS == 3
    assert portfolio["drift_feed"]["status"] == "failed"
    assert portfolio["drift_feed"]["attempts"] == 3

    fresh = {}
    builder.behaviour["error"] = None
    daemon._run_drift_feed_job(fresh, _window(), now=_at(16, 10), account_id="acct-1", path=target)
    daemon._run_drift_feed_job(fresh, _window(), now=_at(16, 11), account_id="acct-1", path=target)
    assert len(builder.calls) == 4
    assert fresh["drift_feed"]["status"] == "written"
    assert "last_error" not in fresh["drift_feed"]


def test_failure_then_success_on_next_cycle(tmp_path, builder):
    target = tmp_path / "feed.json"
    portfolio = {}
    builder.behaviour["error"] = ValueError("bad response")
    daemon._run_drift_feed_job(portfolio, _window(), now=_at(16, 10), account_id="acct-1", path=target)
    builder.behaviour["error"] = None
    daemon._run_drift_feed_job(portfolio, _window(), now=_at(16, 10), account_id="acct-1", path=target)

    assert len(builder.calls) == 2
    assert portfolio["drift_feed"]["status"] == "written"
    assert portfolio["drift_feed"]["attempts"] == 2
