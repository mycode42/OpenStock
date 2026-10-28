from datetime import date, timedelta
from pathlib import Path

from equity_research.validation import history_holdout as hh


def _sessions(after: date, count: int = 200):
    out, day = [], after
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def _candidate(cid: str = "c1"):
    return {"candidate_id": cid, "strategy": "swing_momentum", "regime": "range_bound", "parameters": {"top_n": 2}}


def _sim(beats: bool):
    def simulate(item, start, end):
        cand = {"net_realized_pnl_after_simulated_costs": 120.0 if beats else 50.0,
                "marked_pnl_after_estimated_exit_costs": 90.0 if beats else -10.0}
        base = {"net_realized_pnl_after_simulated_costs": 60.0,
                "marked_pnl_after_estimated_exit_costs": 30.0}
        return {"candidate": cand, "baseline": base}
    return simulate


def _run(db: Path, *, end: date, beats: bool, after_end: date):
    return hh.evaluate(
        db, sessions_after=lambda start: [d for d in _sessions(after_end) if d >= start],
        end=end, simulate=_sim(beats),
    )


def test_waits_until_42_fresh_sessions_exist(tmp_path: Path):
    db = tmp_path / "r.sqlite3"
    end = date(2026, 10, 28)
    hh.enqueue(db, candidate=_candidate(), staged_end=end)
    thin = lambda start: [d for d in _sessions(end)[:10] if d >= start]
    assert hh.evaluate(db, sessions_after=thin, end=end, simulate=_sim(True)) == []
    assert hh.pending(db)[0]["status"] == "pending"
    assert hh.holdout_end(db, "swing_momentum", "range_bound") is None


def test_promotes_when_candidate_beats_live_and_rotates_holdout(tmp_path: Path):
    db = tmp_path / "r.sqlite3"
    end = date(2026, 10, 28)
    hh.enqueue(db, candidate=_candidate(), staged_end=end)
    decided = _run(db, end=end, beats=True, after_end=end)
    assert len(decided) == 1 and decided[0]["status"] == "promoted"
    evidence = decided[0]["evidence"]
    assert evidence["holdout_sessions"] == 42
    assert evidence["holdout_start"] > end.isoformat()  # strictly after staging
    assert hh.holdout_end(db, "swing_momentum", "range_bound").isoformat() == evidence["holdout_end"]


def test_rejects_when_candidate_does_not_beat_live_but_still_rotates(tmp_path: Path):
    db = tmp_path / "r.sqlite3"
    end = date(2026, 10, 28)
    hh.enqueue(db, candidate=_candidate(), staged_end=end)
    decided = _run(db, end=end, beats=False, after_end=end)
    assert decided[0]["status"] == "rejected"
    assert hh.holdout_end(db, "swing_momentum", "range_bound") is not None


def test_second_candidate_gets_the_next_untouched_block(tmp_path: Path):
    db = tmp_path / "r.sqlite3"
    end = date(2026, 10, 28)
    hh.enqueue(db, candidate=_candidate("c1"), staged_end=end)
    hh.enqueue(db, candidate=_candidate("c2"), staged_end=end)
    decided = _run(db, end=end, beats=True, after_end=end)
    assert [d["candidate_id"] for d in decided] == ["c1", "c2"]
    first, second = decided[0]["evidence"], decided[1]["evidence"]
    assert second["holdout_start"] > first["holdout_end"]  # no overlap between holdouts


def test_enqueue_is_idempotent(tmp_path: Path):
    db = tmp_path / "r.sqlite3"
    assert hh.enqueue(db, candidate=_candidate(), staged_end=date(2026, 10, 28)) is True
    assert hh.enqueue(db, candidate=_candidate(), staged_end=date(2026, 10, 28)) is False
