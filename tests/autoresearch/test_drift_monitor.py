from copy import deepcopy
from datetime import date, timedelta
import json
import random

import pytest

from autoresearch import profile_versions as pv
from autoresearch.validation import drift_monitor as dm
from portfolio.profile_store import write_json_atomic
from strategies.registry import ALGO_DEFAULT_PARAMETERS

STRATEGY = "swing_momentum"
REGIME = "range_bound"


def _series(seed: int, mean: float, std: float, count: int, start_day: int = 1) -> list[dict]:
    rng = random.Random(seed)
    rows = []
    first = date(2026, 1, 1) + timedelta(days=start_day - 1)
    for offset in range(count):
        day = (first + timedelta(days=offset)).isoformat()
        rows.append({"date": day, "return": rng.gauss(mean, std)})
    return rows


def _reference_then(collapse: bool, *, reference: int = 21, after: int = 40) -> list[dict]:
    """Reference sessions at +0.1% mean, then stable or collapsed live returns."""
    rows = _series(1, 0.001, 0.01, reference)
    tail_mean = -0.016 if collapse else 0.001
    return rows + _series(2, tail_mean, 0.01, after, start_day=reference + 1)


def _run(tmp_path, live: dict, config: dict | None = None) -> dict:
    return dm.update_research_drift_monitor(
        None, output_dir=tmp_path / "drift", live_returns=live, config=config,
    )


def test_collapsed_live_sharpe_is_flagged(tmp_path):
    report = _run(tmp_path, {STRATEGY: _reference_then(collapse=True)})

    assert report["live"]["strategy_statuses"][STRATEGY]["status"] == "warning"
    assert report["live"]["warning_strategies"] == [STRATEGY]
    assert report["promotion_pause"] is True
    assert report["status"] == "warning"


def test_stable_live_returns_are_not_flagged(tmp_path):
    report = _run(tmp_path, {STRATEGY: _reference_then(collapse=False)})

    summary = report["live"]["all_strategy_statuses"][STRATEGY]
    assert summary["status"] == "monitoring"
    assert report["live"]["warning_strategies"] == []
    assert report["promotion_pause"] is False


def test_short_live_history_is_reported_as_warming_up(tmp_path):
    report = _run(tmp_path, {STRATEGY: _series(3, 0.001, 0.01, 10)})

    summary = report["live"]["all_strategy_statuses"][STRATEGY]
    assert summary["status"] == "warming_up"
    assert summary["reference_sessions_observed"] == 10
    assert summary["reference_sessions_required"] == 21
    assert report["live"]["warming_up_strategies"] == [STRATEGY]
    assert report["live"]["warning_strategies"] == []
    assert report["promotion_pause"] is False
    assert report["status"] == "warming_up"


def test_default_reference_window_is_reachable_in_one_month(tmp_path):
    report = _run(tmp_path, {STRATEGY: _series(4, 0.001, 0.01, 21)})

    assert report["config"]["reference_sessions"] == 21
    assert report["live"]["all_strategy_statuses"][STRATEGY]["status"] == "monitoring"


def test_live_cursor_consumes_each_session_once(tmp_path):
    rows = _series(5, 0.001, 0.01, 25)
    first = _run(tmp_path, {STRATEGY: rows})
    second = _run(tmp_path, {STRATEGY: rows})

    assert first["live"]["strategy_statuses"][STRATEGY]["new_sessions_consumed"] == 25
    assert second["live"]["strategy_statuses"][STRATEGY]["new_sessions_consumed"] == 0
    assert second["live"]["all_strategy_statuses"][STRATEGY]["reference_sessions_observed"] == 21


def test_no_live_feed_is_reported(tmp_path):
    report = _run(tmp_path, None)

    assert report["live"]["feed_present"] is False
    assert report["live"]["all_strategy_statuses"] == {}


@pytest.mark.parametrize("config", [
    {"unknown": 1},
    {"reference_sessions": 1},
    {"reference_sessions": True},
    {"auto_demote_mode": "live"},
    {"auto_demote": "yes"},
])
def test_invalid_config_is_rejected(config):
    with pytest.raises(ValueError):
        dm.resolve_drift_config(config)


def test_default_config_is_dry_run_auto_demote():
    assert dm.resolve_drift_config(None) == {
        "reference_sessions": 21, "auto_demote": True, "auto_demote_mode": "dry_run",
    }


def test_load_live_feed_requires_schema(tmp_path):
    feed = tmp_path / "feed.json"
    assert dm.load_live_drift_feed(feed) is None
    feed.write_text(json.dumps({"schema": "other", "strategies": {}}))
    assert dm.load_live_drift_feed(feed) is None
    feed.write_text(json.dumps({
        "schema": dm.LIVE_FEED_SCHEMA,
        "strategies": {STRATEGY: [{"date": "2026-01-02", "return": 0.1}]},
    }))
    assert dm.load_live_drift_feed(feed) == {STRATEGY: [{"date": "2026-01-02", "return": 0.1}]}


# Auto-demotion ---------------------------------------------------------------

BASE = deepcopy(ALGO_DEFAULT_PARAMETERS[STRATEGY])
OLD = {**deepcopy(BASE), "minimum_score_spread": 0.1}
NEW = {**deepcopy(BASE), "minimum_score_spread": 0.2}


@pytest.fixture
def paths(tmp_path):
    root = tmp_path / "research"
    return {
        "database": root / "research.sqlite3",
        "promoted": root / "promoted-profile.json",
        "lock": root / "forward-validation.json",
        "drift": tmp_path / "drift",
    }


@pytest.fixture
def store(paths):
    return pv.ProfileStore(
        paths["database"], promoted_path=paths["promoted"], lock_path=paths["lock"],
    )


def _promote(store, paths, candidate: str, parameters: dict) -> str:
    promoted = json.loads(paths["promoted"].read_text()) if paths["promoted"].exists() else {}
    version_id = store.stage_promotion(
        strategy=STRATEGY, regime=REGIME, candidate_id=candidate, parameters=parameters,
        release_id=f"release-{candidate}", fitness={}, metadata={}, replaced_parameters=None,
    )
    pv.project_live(promoted, STRATEGY, REGIME, parameters, candidate, f"release-{candidate}")
    write_json_atomic(paths["promoted"], promoted)
    store.activate([version_id])
    return version_id


def _flag(paths) -> dict:
    return _run(paths["drift"].parent, {STRATEGY: _reference_then(collapse=True)})


def _live_profile(paths) -> dict:
    return json.loads(paths["promoted"].read_text())["regime_profiles"][STRATEGY][REGIME]


def test_dry_run_plans_demotion_without_writing(paths, store):
    old = _promote(store, paths, "cand-old", OLD)
    new = _promote(store, paths, "cand-new", NEW)
    report = _flag(paths)
    before = paths["promoted"].read_text()

    result = dm.apply_drift_demotions(report, output_dir=paths["drift"], store=store, config={
        "auto_demote_mode": "dry_run",
    })

    assert result["mode"] == "dry_run"
    assert [(a["from_version_id"], a["to_version_id"]) for a in result["actions"]] == [(new, old)]
    assert result["actions"][0]["changed_keys"] == ["minimum_score_spread"]
    assert store.get_version(new)["status"] == "active"
    assert paths["promoted"].read_text() == before
    assert len(store.history(STRATEGY, REGIME)["events"]) == 2
    assert not json.loads((paths["drift"] / "drift-monitor.json").read_text())["live_strategies"][STRATEGY].get(
        "demotion_actioned"
    )


def test_apply_reverts_to_prior_version_once(paths, store):
    old = _promote(store, paths, "cand-old", OLD)
    _promote(store, paths, "cand-new", NEW)
    report = _flag(paths)

    result = dm.apply_drift_demotions(report, output_dir=paths["drift"], store=store, config={
        "auto_demote_mode": "apply",
    })

    assert result["mode"] == "apply"
    assert len(result["actions"]) == 1
    action = result["actions"][0]
    assert action["to_version_id"] == old
    active = store.active_versions(strategy=STRATEGY, regime=REGIME)
    assert len(active) == 1 and active[0]["parameters"]["minimum_score_spread"] == 0.1
    assert _live_profile(paths)["minimum_score_spread"] == 0.1
    event = store.history(STRATEGY, REGIME)["events"][-1]
    assert (event["action"], event["reason"], event["actor"]) == ("revert", "drift", "drift_monitor")
    assert json.loads((paths["drift"] / "drift-monitor.json").read_text())["live_strategies"][STRATEGY][
        "demotion_actioned"
    ] is True

    again = dm.apply_drift_demotions(report, output_dir=paths["drift"], store=store, config={
        "auto_demote_mode": "apply",
    })
    assert again["actions"] == []
    assert len(store.history(STRATEGY, REGIME)["events"]) == 3


def test_no_prior_version_records_warning_and_does_not_demote(paths, store):
    only = _promote(store, paths, "cand-only", OLD)
    report = _flag(paths)

    for mode in ("dry_run", "apply"):
        result = dm.apply_drift_demotions(report, output_dir=paths["drift"], store=store, config={
            "auto_demote_mode": mode,
        })
        assert result["actions"] == []
        assert result["warnings"] == [f"{STRATEGY}:{REGIME} has no prior version; not demoted"]
    assert store.get_version(only)["status"] == "active"


def test_auto_demote_disabled_takes_no_action(paths, store):
    _promote(store, paths, "cand-old", OLD)
    _promote(store, paths, "cand-new", NEW)
    report = _flag(paths)

    result = dm.apply_drift_demotions(report, output_dir=paths["drift"], store=store, config={
        "auto_demote": False, "auto_demote_mode": "apply",
    })

    assert result == {"enabled": False, "mode": "apply", "actions": [], "warnings": []}
    assert _live_profile(paths)["minimum_score_spread"] == 0.2


def test_backtest_only_warning_does_not_trigger_demotion(paths, store):
    _promote(store, paths, "cand-old", OLD)
    _promote(store, paths, "cand-new", NEW)
    report = {"live": {"warning_strategies": []}, "backtest": {"warning_strategies": [STRATEGY]}}

    result = dm.apply_drift_demotions(report, output_dir=paths["drift"], store=store, config={
        "auto_demote_mode": "apply",
    })

    assert result["actions"] == []
