import json
import sqlite3
from pathlib import Path

from equity_research import weekly_review as wr


def _versions_db(path: Path) -> Path:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE profile_versions (version_id TEXT PRIMARY KEY, strategy TEXT, regime TEXT, "
            "status TEXT, candidate_id TEXT, activated_at TEXT)"
        )
        connection.executemany(
            "INSERT INTO profile_versions VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("v1", "swing_momentum", "range_bound", "superseded", "old", "2026-10-01"),
                ("v2", "swing_momentum", "range_bound", "active", "new", "2026-10-20"),
                ("v3", "vrp_like", "volatile", "active", "x", "2026-10-21"),
            ],
        )
    return path


def _summary(pass_flag: bool) -> dict:
    return {
        "checks": {"overall_pass": pass_flag},
        "fold_consistency": {"fold_count": 26, "positive_fold_fraction": 0.23},
        "average_fitness_decay": 0.29,
        "block_bootstrap": {"net_return_pct_p50": -0.4, "probability_of_loss_pct": 56.5},
    }


def test_summarize_keeps_operator_fields():
    out = wr.summarize_verification(_summary(False))
    assert out["overall_pass"] is False and out["fold_count"] == 26
    assert wr.summarize_verification(None)["overall_pass"] is None


def test_build_review_compares_full_history_with_recent(tmp_path: Path):
    db = _versions_db(tmp_path / "research.sqlite3")
    full, recent = tmp_path / "full", tmp_path / "recent"
    (full / "swing_momentum").mkdir(parents=True)
    (recent / "swing_momentum").mkdir(parents=True)
    (full / "swing_momentum" / "latest-walk-forward-summary.json").write_text(json.dumps(_summary(False)))
    (recent / "swing_momentum" / "latest-walk-forward-summary.json").write_text(json.dumps(_summary(True)))
    review = wr.build_review(database=db, full_root=full, recent_root=recent, run_id="run-1")
    assert review["status"] == "pending_decision"
    by_strategy = {entry["strategy"]: entry for entry in review["entries"]}
    entry = by_strategy["swing_momentum"]
    assert entry["previous_version_id"] == "v1" and entry["live_version_id"] == "v2"
    assert entry["full_history"]["overall_pass"] is False and entry["recent"]["overall_pass"] is True
    assert entry["decision"] is None


def test_keep_records_decision_and_leaves_profile_untouched(tmp_path: Path):
    db = _versions_db(tmp_path / "research.sqlite3")
    path = tmp_path / "latest.json"
    review = wr.build_review(database=db, full_root=tmp_path, recent_root=tmp_path, run_id="r")
    wr.write_review(review, path)
    entry = wr.keep("swing_momentum", "range_bound", path=path)
    assert entry["decision"]["action"] == "kept"
    stored = wr.load_review(path)
    statuses = {(e["strategy"], e["regime"]): e["decision"] is None for e in stored["entries"]}
    assert statuses[("swing_momentum", "range_bound")] is False
    assert stored["status"] == "pending_decision"  # the other live profile is still undecided


def test_keep_unknown_profile_raises(tmp_path: Path):
    path = tmp_path / "latest.json"
    wr.write_review({"schema": wr.SCHEMA, "status": "pending_decision", "entries": []}, path)
    try:
        wr.keep("nope", "range_bound", path=path)
    except KeyError:
        return
    raise AssertionError("expected KeyError")


def test_demoted_candidate_is_blocked_from_repromotion(tmp_path: Path):
    db = _versions_db(tmp_path / "research.sqlite3")
    assert wr.blocked_candidate_ids(db) == {}
    with sqlite3.connect(db) as connection:
        connection.executescript(wr._BLOCK_SCHEMA)
        connection.execute(
            "INSERT INTO algo_demoted_candidates VALUES ('swing_momentum', 'range_bound', 'new', 'x')"
        )
    assert wr.blocked_candidate_ids(db) == {("swing_momentum", "range_bound"): {"new"}}
