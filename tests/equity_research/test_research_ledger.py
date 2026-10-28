from copy import deepcopy
import json
import sqlite3

from equity_research.artifacts.candidate_archive import candidate_id
from equity_research.validation.forward_validation import _candidates
from equity_research.artifacts.research_ledger import ResearchLedger
from strategies.registry import ALGO_DEFAULT_PARAMETERS


def test_regime_candidate_can_enter_forward_trial_when_strategy_gate_fails(tmp_path):
    parameters = deepcopy(ALGO_DEFAULT_PARAMETERS["swing_momentum"])
    artifact = tmp_path / "discovery.json"
    artifact.write_text(json.dumps({
        "adaptive_regime_search": {
            "profiles": [{
                "regime": "range_bound",
                "parameters": parameters,
                "improved_over_baseline": True,
                "fitness": 0.4,
                "baseline_fitness": 0.1,
                "metrics": {"net_return_pct": 2.0},
            }],
        },
    }))
    discovery = {"strategies": {"swing_momentum": {
        "status": "completed", "detail_artifact": str(artifact),
    }}}
    verification = {"strategies": {"swing_momentum": {
        "status": "completed",
        "summary": {
            "checks": {"overall_pass": False},
            "thresholds": {"minimum_folds": 3},
            "stitched_oos": {"daily_sharpe_annualized": -0.4},
            "fold_count": 10,
        },
    }}, "candidate_validation": {"strategies": {"swing_momentum": {
        "status": "completed",
        "candidates": [{
            "candidate_id": candidate_id("swing_momentum", parameters),
            "status": "passed",
        }],
    }}}}

    result = _candidates(discovery, verification, "range_bound")

    assert len(result) == 1
    assert result[0]["candidate_id"] == candidate_id("swing_momentum", parameters, "range_bound")
    assert result[0]["historical_verification"]["overall_pass"] is False
    assert result[0]["screening"]["improved_over_training_baseline"] is True


def test_ledger_links_candidate_discovery_trial_observation_and_promotion(tmp_path):
    output = tmp_path / "research"
    output.mkdir()
    parameters = deepcopy(ALGO_DEFAULT_PARAMETERS["swing_momentum"])
    artifact = output / "discovery.json"
    artifact.write_text(json.dumps({
        "window": {"start": "2026-01-01", "end": "2026-02-01", "sessions": 20},
        "candidates": [{
            "name": "baseline", "parameters": parameters, "fitness": 0.1,
            "metrics": {"net_return_pct": 0.5},
        }],
        "adaptive_regime_search": {"profiles": [{
            "regime": "range_bound", "parameters": parameters,
            "improved_over_baseline": True, "fitness": 0.4,
            "baseline_fitness": 0.1, "metrics": {"net_return_pct": 2.0},
        }]},
    }))

    ledger = ResearchLedger(output / "research.sqlite3")
    invocation_id = "invocation-1"
    ledger.start_invocation(invocation_id)
    ledger.record_discovery_stage(
        invocation_id,
        {"status": "completed", "strategies": {"swing_momentum": {
            "status": "completed", "detail_artifact": str(artifact),
            "archive_sequence": 7, "search_seed": 42,
        }}},
        data_version="market-data-v1",
    )

    cid = candidate_id("swing_momentum", parameters, "range_bound")
    trial_id = "trial-1"
    data_end = "2026-03-01"
    simulation = {
        "status": "completed", "data_end": data_end,
        "data_version": "market-data-v1", "simulation_start": "2026-02-02",
        "candidate_metrics": {"net_realized_pnl_after_simulated_costs": 1200.0},
        "baseline_metrics": {"net_realized_pnl_after_simulated_costs": 400.0},
    }
    trial = {
        "trial_id": trial_id, "candidate_id": cid, "strategy": "swing_momentum",
        "regime": "range_bound", "parameters": parameters,
        "origin_invocation_id": invocation_id, "status": "promoted",
        "staged_on": "2026-02-01", "staged_at": "2026-02-01T00:00:00+00:00",
        "finished_at": "2026-03-01T00:00:00+00:00",
        "baseline_profile_sha256": "baseline-v1", "latest_simulation": simulation,
    }
    promotion = {
        "trial_id": trial_id, "candidate_id": cid, "release_id": "release-1",
        "promoted_at": "2026-03-01T00:00:00+00:00",
        "forward_metrics": simulation,
    }
    ledger.record_forward_cycle(
        invocation_id, active_trials={}, terminal_trials=[trial],
        promoted_rows=[promotion], data_version="market-data-v1",
        data_end=data_end, observed_trial_ids={trial_id},
    )

    with sqlite3.connect(ledger.path) as connection:
        evaluation = connection.execute(
            "SELECT stage, evaluation_kind, passed FROM candidate_evaluation WHERE candidate_id=?",
            (cid,),
        ).fetchone()
        observed = connection.execute(
            "SELECT trial_id, invocation_id, data_end FROM forward_trial_observation"
        ).fetchone()
        promoted = connection.execute(
            "SELECT trial_id, candidate_id, release_id FROM research_promotion"
        ).fetchone()

    assert evaluation == ("discovery", "regime_candidate", 1)
    assert observed == (trial_id, invocation_id, data_end)
    assert promoted == (trial_id, cid, "release-1")
