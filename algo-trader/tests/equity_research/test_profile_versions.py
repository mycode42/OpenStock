from contextlib import closing
from copy import deepcopy
import json
import sqlite3

import pytest

from equity_research import profile_versions as pv
from equity_research.artifacts.research_ledger import ResearchLedger
from equity_research.validation import forward_validation as fv
from portfolio.profile_store import write_json_atomic
from strategies.registry import ALGO_DEFAULT_PARAMETERS

STRATEGY = "swing_momentum"
REGIME = "range_bound"


@pytest.fixture
def paths(tmp_path):
    root = tmp_path / "research"
    return {
        "database": root / "research.sqlite3",
        "promoted": root / "promoted-profile.json",
        "lock": root / "forward-validation.json",
    }


@pytest.fixture
def store(paths):
    return pv.ProfileStore(
        paths["database"], promoted_path=paths["promoted"], lock_path=paths["lock"],
    )


def _params(minimum_score_spread: float) -> dict:
    return {**deepcopy(ALGO_DEFAULT_PARAMETERS[STRATEGY]), "minimum_score_spread": minimum_score_spread}


def _trial(parameters: dict, candidate: str, *, trial_id: str | None = None) -> dict:
    return {
        "trial_id": trial_id or f"trial-{candidate}",
        "candidate_id": candidate,
        "strategy": STRATEGY,
        "regime": REGIME,
        "parameters": parameters,
        "latest_simulation": {
            "candidate_metrics": {"net_realized_pnl_after_simulated_costs": 120.0, "closed_trades": 31},
            "baseline_metrics": {"net_realized_pnl_after_simulated_costs": 10.0},
            "acceptance": {"decision": "accept", "reasons": [], "metrics": {"t_stat": 2.5}},
            "deflation_trial_count": 3,
        },
    }


def _promote_and_activate(store, trial, promoted):
    record = fv._promote(trial, trial["latest_simulation"], promoted, store)
    if record is not None:
        store.activate([record["version_id"]])
    return record


def _active_rows(database, strategy=STRATEGY, regime=REGIME):
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute(
            "SELECT version_id, status, source, candidate_id, parent_version_id, restores_version_id "
            "FROM profile_versions WHERE strategy=? AND regime=? ORDER BY created_at, rowid",
            (strategy, regime),
        ).fetchall()


def test_schema_creates_tables_and_enforces_one_active_version(paths, store):
    with closing(sqlite3.connect(paths["database"])) as connection:
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        assert {"profile_versions", "profile_events", "profile_versions_one_active"} <= names
        columns = {row[1] for row in connection.execute("PRAGMA table_info(profile_versions)")}
        assert {"parameters_json", "fitness_json", "status", "source", "candidate_id",
                "parent_version_id", "release_id", "activated_at", "deactivated_at"} <= columns
        connection.execute(
            "INSERT INTO profile_versions (version_id, strategy, regime, status, source, created_at) "
            "VALUES ('a', ?, ?, 'active', 'manual', 'now')", (STRATEGY, REGIME),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO profile_versions (version_id, strategy, regime, status, source, created_at) "
                "VALUES ('b', ?, ?, 'active', 'manual', 'now')", (STRATEGY, REGIME),
            )
        connection.commit()


def test_promote_twice_keeps_one_active_version_with_lineage(paths, store):
    promoted = {}
    first = _promote_and_activate(store, _trial(_params(0.04), "cand-a"), promoted)
    second = _promote_and_activate(store, _trial(_params(0.05), "cand-b"), promoted)

    rows = _active_rows(paths["database"])
    assert [(row[1], row[2], row[3]) for row in rows] == [
        ("superseded", "promotion", "cand-a"),
        ("active", "promotion", "cand-b"),
    ]
    assert rows[1][4] == first["version_id"]  # parent of B is A
    assert rows[0][4] is None
    history = store.history(STRATEGY, REGIME)
    assert [event["action"] for event in history["events"]] == ["promote", "promote"]
    replaced = history["events"][1]["details"]["replaced_parameters"]
    assert replaced["minimum_score_spread"] == 0.04
    assert second["release_id"] == promoted["release_id"]
    assert promoted["regime_profile_ids"][STRATEGY][REGIME] == "cand-b"


def test_promotion_is_idempotent_per_candidate(paths, store):
    promoted = {}
    trial = _trial(_params(0.04), "cand-a")
    first = _promote_and_activate(store, trial, promoted)
    release_before = promoted["release_id"]

    again = _promote_and_activate(store, trial, promoted)

    assert first is not None and again is None
    assert promoted["release_id"] == release_before
    assert len(_active_rows(paths["database"])) == 1
    assert len(store.history(STRATEGY, REGIME)["events"]) == 1


def test_crash_after_projection_is_rolled_forward_on_reconcile(paths, store, monkeypatch):
    promoted = {}
    record = fv._promote(_trial(_params(0.04), "cand-a"), {}, promoted, store)
    write_json_atomic(paths["promoted"], promoted)

    def crash(connection, pending, *, stamp):
        raise RuntimeError("simulated crash before commit")

    monkeypatch.setattr(pv, "_activate", crash)
    with pytest.raises(RuntimeError):
        store.activate([record["version_id"]])
    monkeypatch.undo()

    # The failed transaction rolled back: the row is still pending and nothing is active.
    assert [row[1] for row in _active_rows(paths["database"])] == ["pending"]

    recovered = pv.ProfileStore(
        paths["database"], promoted_path=paths["promoted"], lock_path=paths["lock"],
    )
    result = recovered.reconcile(json.loads(paths["promoted"].read_text()))

    assert result == {"completed": [record["version_id"]], "abandoned": []}
    assert [row[1] for row in _active_rows(paths["database"])] == ["active"]
    # A later cycle replaying the same trial must not create a second release.
    assert fv._promote(_trial(_params(0.04), "cand-a"), {}, json.loads(paths["promoted"].read_text()), recovered) is None


def test_crash_before_projection_abandons_pending_and_allows_restage(paths, store):
    promoted = {}
    trial = _trial(_params(0.04), "cand-a")
    fv._promote(trial, {}, promoted, store)  # staged, JSON never written

    result = store.reconcile({})

    assert len(result["abandoned"]) == 1 and result["completed"] == []
    assert _promote_and_activate(store, trial, {}) is not None
    # The abandoned attempt stays in the record; the re-staged version is the only active one.
    assert [row[1] for row in _active_rows(paths["database"])] == ["abandoned", "active"]


def test_revert_restores_parameters_and_writes_event(paths, store):
    promoted = {}
    first = _promote_and_activate(store, _trial(_params(0.04), "cand-a"), promoted)
    _promote_and_activate(store, _trial(_params(0.05), "cand-b"), promoted)
    write_json_atomic(paths["promoted"], promoted)

    result = store.revert(strategy=STRATEGY, regime=REGIME, version_id=first["version_id"], reason="test")

    live = json.loads(paths["promoted"].read_text())
    assert live["regime_profile_ids"][STRATEGY][REGIME] == "cand-a"
    assert live["regime_profiles"][STRATEGY][REGIME]["minimum_score_spread"] == 0.04
    rows = _active_rows(paths["database"])
    assert [(row[1], row[2]) for row in rows] == [
        ("superseded", "promotion"), ("reverted", "promotion"), ("active", "revert"),
    ]
    assert rows[2][5] == first["version_id"]  # restores_version_id points to A
    events = store.history(STRATEGY, REGIME)["events"]
    assert events[-1]["action"] == "revert"
    assert events[-1]["to_version_id"] == result["new_version_id"]
    assert events[-1]["details"]["replaced_parameters"]["minimum_score_spread"] == 0.05
    assert events[-1]["reason"] == "test"


def test_revert_refuses_invalid_version_without_writing(paths, store):
    promoted = {}
    _promote_and_activate(store, _trial(_params(0.04), "cand-a"), promoted)
    write_json_atomic(paths["promoted"], promoted)
    before_json = paths["promoted"].read_text()
    with closing(store._connect()) as connection:
        pv._insert_version(
            connection, version_id="bad-version", strategy=STRATEGY, regime=REGIME,
            parameters={"not_a_parameter": 1}, fitness={}, metadata={}, status="superseded",
            source="backfill", candidate_id="cand-bad", parent=None, release_id="old",
            created_at="2026-01-01T00:00:00+00:00",
        )
    before_rows = _active_rows(paths["database"])

    with pytest.raises(pv.ProfileError, match="fails parameter validation"):
        store.revert(strategy=STRATEGY, regime=REGIME, version_id="bad-version")

    assert paths["promoted"].read_text() == before_json
    assert _active_rows(paths["database"]) == before_rows


def test_revert_dry_run_writes_nothing(paths, store):
    promoted = {}
    first = _promote_and_activate(store, _trial(_params(0.04), "cand-a"), promoted)
    _promote_and_activate(store, _trial(_params(0.05), "cand-b"), promoted)
    write_json_atomic(paths["promoted"], promoted)
    before_json = paths["promoted"].read_text()
    before_rows = _active_rows(paths["database"])

    plan = store.revert(
        strategy=STRATEGY, regime=REGIME, version_id=first["version_id"], dry_run=True,
    )

    assert plan["dry_run"] is True
    assert plan["changed_keys"] == ["minimum_score_spread"]
    assert paths["promoted"].read_text() == before_json
    assert _active_rows(paths["database"]) == before_rows


def _write_live_json(path):
    path.write_text(json.dumps({
        "history": [{
            "candidate_id": "cand-live",
            "change": "factor_weight_mode static -> xgboost_walk_forward",
            "recorded_at": "2026-10-20T10:00:00+00:00",
            "release_id": "manual-xgboost-core-20261020",
            "strategy": "multi_factor",
        }, {
            "candidate_id": "cand-old",
            "recorded_at": "2026-10-01T10:00:00+00:00",
            "release_id": "manual-old",
            "strategy": "multi_factor",
            "parameters": {"factor_weight_mode": "static"},
        }],
        "parameters": {"multi_factor": {"factor_weight_mode": "xgboost_walk_forward"}},
        "profile_ids": {"multi_factor": "cand-live"},
        "release_id": "manual-xgboost-core-20261020",
        "schema": "swarm_algo_promoted_profiles_v1",
        "updated_at": "2026-10-20T10:14:51+00:00",
    }, indent=2))


def test_backfill_is_idempotent_and_dry_run_matches_real(paths, store):
    _write_live_json(paths["promoted"])
    before_json = paths["promoted"].read_bytes()

    dry = store.backfill(dry_run=True)
    assert dry["versions_inserted"] == 2 and dry["events_inserted"] == 2
    assert dry["history_attached_to_live"] == 1
    assert _active_rows(paths["database"], "multi_factor", "*") == []

    real = store.backfill()
    assert real["versions_inserted"] == dry["versions_inserted"]
    rows = _active_rows(paths["database"], "multi_factor", "*")
    assert [(row[1], row[2]) for row in rows] == [("superseded", "backfill"), ("active", "backfill")]
    assert rows[1][4] == rows[0][0]  # live version's parent is the historical record

    again = store.backfill()
    assert again["versions_inserted"] == 0 and again["events_inserted"] == 0
    assert again["already_present"] == 2
    assert len(_active_rows(paths["database"], "multi_factor", "*")) == 2
    assert paths["promoted"].read_bytes() == before_json


def test_trial_count_does_not_shrink_after_history_trim(paths):
    database = paths["database"]
    ResearchLedger(database)  # creates the forward_trial table in the shared research database
    with closing(sqlite3.connect(database)) as connection:
        for index in range(3):
            connection.execute(
                "INSERT INTO forward_trial (trial_id, candidate_id, strategy, regime, status, metadata_json) "
                "VALUES (?, ?, ?, ?, 'rejected', '{}')",
                (f"stored-{index}", f"cand-{index}", STRATEGY, REGIME),
            )
        connection.execute(
            "INSERT INTO forward_trial (trial_id, candidate_id, strategy, regime, status, metadata_json) "
            "VALUES ('other', 'cand-x', 'swing_mean_reversion', ?, 'rejected', '{}')", (REGIME,),
        )
        connection.commit()

    # Legacy ledger rows count per strategy, across regimes, in the all-time count.
    assert pv.cumulative_trial_count(database, STRATEGY, window_days=None) == 3
    assert pv.cumulative_trial_count(database, "swing_mean_reversion", window_days=None) == 1
    # Legacy rows carry no timestamp, so the rolling window excludes them.
    assert pv.cumulative_trial_count(database, STRATEGY) == 0

    recorded = [
        ("forward:stored-0", STRATEGY, "forward", "cand-0"),  # already counted via the ledger
        ("discovery:" + STRATEGY + ":abc", STRATEGY, "discovery", "abc"),
    ]
    assert pv.record_trial_evaluations(database, recorded) == 2
    assert pv.record_trial_evaluations(database, recorded) == 0  # insert-only and idempotent
    # stored-0 appears in both tables but is one evaluation: stored-0..2 plus the discovery row.
    assert pv.cumulative_trial_count(database, STRATEGY, window_days=None) == 4


def test_cli_list_and_history_run_against_temp_store(paths, store, capsys):
    promoted = {}
    _promote_and_activate(store, _trial(_params(0.04), "cand-a"), promoted)
    args = ["--database", str(paths["database"]), "--promoted", str(paths["promoted"]),
            "--lock", str(paths["lock"])]

    assert pv.main([*args, "list", "--strategy", STRATEGY]) == 0
    assert pv.main([*args, "history", "--strategy", STRATEGY, "--regime", REGIME]) == 0
    output = capsys.readouterr().out
    assert "promote" in output and "active" in output


def test_forward_cycle_reconciles_pending_rows_before_staging(paths, store, monkeypatch):
    from datetime import date

    promoted = {}
    trial = _trial(_params(0.04), "cand-a")
    fv._promote(trial, {}, promoted, store)  # left pending by a crash before projection
    monkeypatch.setattr(fv, "PROMOTED_PROFILE_PATH", paths["promoted"])
    monkeypatch.setattr(fv, "FORWARD_VALIDATION_PATH", paths["lock"])
    monkeypatch.setattr(fv, "refresh_active_parameter_profile", lambda: {})
    monkeypatch.setattr(fv, "_forward_session_data_version", lambda *args: "data-version")
    monkeypatch.setattr(fv, "_current_regime", lambda *args: None)

    result = fv.run_forward_validation_cycle(
        discovery={}, verification={}, store=None, database=str(paths["database"]),
        symbols=[], end=date(2026, 10, 27), data_version="v", allow_stage=False,
        ledger_path=paths["database"],
    )

    assert result["status"] == "completed"
    assert [row[1] for row in _active_rows(paths["database"])] == ["abandoned"]
    assert not paths["promoted"].exists()  # nothing was projected, so the live file is untouched
