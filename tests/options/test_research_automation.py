import json

from options.research import automation


def _write_config(tmp_path, *, data_path, strategies=None):
    config_path = tmp_path / "options-research.json"
    config_path.write_text(json.dumps({
        "data_path": str(data_path),
        "output_dir": str(tmp_path / "runs"),
        "automation": {
            "enabled": True,
            "strategies": strategies or ["vrp", "llm_driven"],
            "checkpoint_database": str(tmp_path / "history.sqlite3"),
        },
    }), encoding="utf-8")
    return config_path


def test_missing_chain_dataset_generates_separate_waiting_report(tmp_path):
    config_path = _write_config(tmp_path, data_path=tmp_path / "missing-chains.jsonl")

    report = automation.run_automated_options_research(config_path=config_path)

    assert report["status"] == "waiting_for_data"
    assert report["dataset_available"] is False
    assert set(report["strategies"]) == {"vrp", "llm_driven"}
    assert all(item["status"] == "skipped_missing_dataset" for item in report["strategies"].values())
    report_dir = tmp_path / "automation"
    assert json.loads((report_dir / "latest.json").read_text(encoding="utf-8"))["status"] == "waiting_for_data"
    assert "historical chain snapshots" in (report_dir / "latest.md").read_text(encoding="utf-8").lower()
    run = json.loads((report_dir / "runs.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert run["dataset_available"] is False
    assert not (report_dir / "checkpoints").exists()
    checkpoint = automation.OptionsHistoryStore(tmp_path / "history.sqlite3").read_automation_checkpoint(
        report["run_id"],
    )
    assert checkpoint["schema"] == "swarm_options_automation_checkpoint_v1"
    assert report["checkpoint_location"].endswith(f"#{report['run_id']}")


def test_automation_runs_only_configured_strategy_modules(monkeypatch, tmp_path):
    dataset_path = tmp_path / "chains.jsonl"
    dataset_path.write_text("placeholder", encoding="utf-8")
    config_path = _write_config(tmp_path, data_path=dataset_path, strategies=["llm_driven"])
    calls = []

    def fake_run_research(strategy, *, config_path, data_path, underlying, parallel_workers):
        calls.append((strategy, config_path, data_path, underlying))
        return {
            "run_id": "event-run-1", "run_dir": str(tmp_path / "runs" / "event-run-1"),
            "underlying": "SPY", "data_mode": "historical_chains",
            "validation": {"approved": False, "minimum_oos_trades_met": True},
            "walk_forward": {"oos_metrics": {"trade_count": 10, "annualized_sharpe": 0.7}},
            "holdout": {"metrics": {"trade_count": 4, "total_return": 0.02}},
            "final_parameters": {"min_move_edge_pct": 0.15},
        }

    monkeypatch.setattr(automation, "run_research", fake_run_research)

    report = automation.run_automated_options_research(config_path=config_path)

    assert calls == [("llm_driven", config_path.resolve(), dataset_path.resolve(), "SPY")]
    assert report["status"] == "completed"
    assert report["strategies"]["llm_driven"]["walk_forward"]["trade_count"] == 10
    assert report["strategies"]["llm_driven"]["approved"] is False
