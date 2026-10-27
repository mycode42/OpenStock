"""Cohort holdouts: frozen once, never superseded by a nightly run, retired only explicitly."""

import sqlite3
from contextlib import closing
from datetime import date

import pytest

from autoresearch.settings import LIVE_RESEARCH_STRATEGIES
from autoresearch.validation.final_holdout import (
    HOLDOUT_REGIMES, _public_status, active_holdout_pairs, cohort_holdout_status,
    holdout_report, initialize_cohort_holdouts, main, supersede_cohort_holdouts,
)

COHORTS = len(LIVE_RESEARCH_STRATEGIES) * len(HOLDOUT_REGIMES)


@pytest.fixture
def database(tmp_path):
    return tmp_path / "research" / "research.sqlite3"


def _count(database, sql, values=()):
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute(sql, values).fetchone()[0]


def test_initialize_freezes_one_holdout_per_cohort_with_window_and_hash(database):
    result = initialize_cohort_holdouts(database=database, frozen_on=date(2026, 10, 1))

    assert len(result["created"]) == COHORTS
    assert result["already_active"] == []
    assert result["window_start"] == "2026-10-02"
    rows = cohort_holdout_status(database)["active"]
    assert len(rows) == COHORTS
    assert all(len(row["parameters_sha256"]) == 64 for row in rows)
    assert all(row["window_end"] is None and row["window_start"] == "2026-10-02" for row in rows)


def test_initialize_is_idempotent_and_keeps_the_original_freeze(database):
    initialize_cohort_holdouts(database=database, frozen_on=date(2026, 10, 1))
    before = {row["holdout_id"]: row["parameters_sha256"] for row in cohort_holdout_status(database)["active"]}

    again = initialize_cohort_holdouts(database=database, frozen_on=date(2026, 11, 1))

    assert again["created"] == []
    assert len(again["already_active"]) == COHORTS
    after = {row["holdout_id"]: row["parameters_sha256"] for row in cohort_holdout_status(database)["active"]}
    assert after == before
    assert _count(database, "SELECT COUNT(*) FROM final_holdout") == COHORTS


def test_nightly_report_does_not_supersede_an_active_holdout(database, tmp_path):
    initialize_cohort_holdouts(database=database, frozen_on=date(2026, 10, 1))

    first = holdout_report(database=database, output_dir=tmp_path)
    second = holdout_report(database=database, output_dir=tmp_path)

    assert first["status"] == second["status"] == "active"
    assert first["active_holdout_count"] == COHORTS
    assert len(first["promotion_paused_pairs"]) == COHORTS
    assert _count(database, "SELECT COUNT(*) FROM final_holdout WHERE status='active'") == COHORTS
    assert _count(database, "SELECT COUNT(*) FROM final_holdout_event WHERE action='supersede'") == 0


def test_promotion_gate_sees_the_active_pairs_until_an_explicit_supersede(database):
    initialize_cohort_holdouts(database=database, frozen_on=date(2026, 10, 1))
    pairs = active_holdout_pairs(database)
    strategy = LIVE_RESEARCH_STRATEGIES[0]

    assert (strategy, "range_bound") in pairs
    assert len(pairs) == COHORTS


def test_supersede_requires_a_reason_and_writes_one_event_per_holdout(database):
    initialize_cohort_holdouts(database=database, frozen_on=date(2026, 10, 1))

    with pytest.raises(ValueError):
        supersede_cohort_holdouts(database=database, reason="   ")
    assert _count(database, "SELECT COUNT(*) FROM final_holdout WHERE status='active'") == COHORTS

    result = supersede_cohort_holdouts(database=database, reason="rebuilt cohort after data repair")

    assert len(result["superseded"]) == COHORTS
    assert _count(database, "SELECT COUNT(*) FROM final_holdout WHERE status='active'") == 0
    events = _count(
        database,
        "SELECT COUNT(*) FROM final_holdout_event WHERE action='supersede' AND reason=?",
        ("rebuilt cohort after data repair",),
    )
    assert events == COHORTS
    assert active_holdout_pairs(database) == set()


def test_supersede_can_target_one_cohort(database):
    initialize_cohort_holdouts(database=database, frozen_on=date(2026, 10, 1))
    strategy = LIVE_RESEARCH_STRATEGIES[0]

    result = supersede_cohort_holdouts(
        database=database, reason="one cohort", strategy=strategy, regime="volatile",
    )

    assert result["superseded"] == [f"{strategy}:volatile"]
    assert (strategy, "volatile") not in active_holdout_pairs(database)
    assert (strategy, "range_bound") in active_holdout_pairs(database)


def test_cli_initialize_status_and_supersede(database, capsys):
    assert main(["--database", str(database), "initialize", "--frozen-on", "2026-10-01"]) == 0
    assert main(["--database", str(database), "status"]) == 0
    assert f'"active": [' in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["--database", str(database), "supersede"])
    assert main(["--database", str(database), "supersede", "--reason", "operator decision"]) == 0
    assert active_holdout_pairs(database) == set()


def test_legacy_frozen_profile_without_the_new_key_reads_as_not_included():
    legacy = {
        "status": "superseded",
        "frozen_profile": {"strategies": [], "event_driven_overlay_included": True},
    }
    current = {
        "status": "superseded",
        "frozen_profile": {"strategies": [], "llm_driven_overlay_included": True},
    }

    assert _public_status(legacy)["llm_driven_overlay_included"] is False
    assert _public_status(current)["llm_driven_overlay_included"] is True
