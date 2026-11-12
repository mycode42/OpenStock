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
        "search": {"parallel_workers": 8},
    }), encoding="utf-8")
    return config_path


def test_missing_chain_dataset_generates_separate_waiting_report(tmp_path):
    config_path = _write_config(tmp_path, data_path=tmp_path / "missing-chains.jsonl")

    report = automation.run_automated_options_research(config_path=config_path)

    assert report["status"] == "waiting_for_data"
    assert report["dataset_available"] is False
    assert set(report["strategies"]) == {"vrp", "llm_driven"}
    assert all(item["status"] == "skipped_missing_dataset" for item in report["strategies"].values())
    report_dir = tmp_path / "simulation"
    assert json.loads((report_dir / "latest.json").read_text(encoding="utf-8"))["status"] == "waiting_for_data"
    assert "historical chain snapshots" in (report_dir / "latest.md").read_text(encoding="utf-8").lower()
    run = json.loads((report_dir / "runs.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert run["dataset_available"] is False
    assert not (report_dir / "checkpoints").exists()
    checkpoint = automation.OptionsHistoryStore(tmp_path / "history.sqlite3").read_automation_checkpoint(
        report["run_id"],
    )
    assert checkpoint["schema"] == "swarm_options_automation_checkpoint_v2"
    assert report["checkpoint_location"].endswith(f"#{report['run_id']}")


def test_automation_runs_only_configured_strategy_modules(monkeypatch, tmp_path):
    monkeypatch.setattr(automation, "available_cpu_count", lambda: 8)
    dataset_path = tmp_path / "chains.jsonl"
    dataset_path.write_text("placeholder", encoding="utf-8")
    config_path = _write_config(tmp_path, data_path=dataset_path, strategies=["llm_driven"])
    calls = []

    def fake_portfolio_replay(config, *, strategies, underlyings_by_strategy, data_paths, workers, progress_callback=None):
        calls.append((strategies, underlyings_by_strategy, data_paths, workers))
        return ({
            "metrics": {"trade_count": 10, "total_return": 0.08, "annualized_sharpe": 0.7,
                        "max_drawdown": 0.04},
            "trades": [{"strategy": "llm_driven", "underlying": "SPY", "pnl": 800.0}],
            "underlyings": ["SPY"], "underlyings_by_strategy": {"llm_driven": ["SPY"]},
            "sleeve_metrics": {"llm_driven": {"trades": 10, "net_pnl_usd": 800.0}},
            "period_start": "2020-01-02T21:00:00+00:00", "period_end": "2026-11-09T21:00:00+00:00",
            "starting_equity": 100000.0, "data_mode": "historical_chains",
        }, {}, {"SPY": {"chain_workers_used": 4}})

    monkeypatch.setattr(automation, "_run_portfolio_replay", fake_portfolio_replay)

    report = automation.run_automated_options_research(config_path=config_path)

    assert calls == [(["llm_driven"], {"llm_driven": ["SPY"]}, {"SPY": dataset_path.resolve()}, 4)]
    assert report["status"] == "completed"
    assert report["portfolio_replay"]["metrics"]["trade_count"] == 10
    assert report["strategies"]["llm_driven"]["portfolio_metrics"]["net_pnl_usd"] == 800.0
    assert report["fold_workers_used_by_strategy"] == {"llm_driven": 0}
    assert report["replay_data_workers_used_by_strategy"] == {"llm_driven": 4}


def test_options_simulation_counts_only_underlyings_with_trades_and_sums_their_pnl():
    from options.research.automation import _render_markdown, _simulation_summary

    def replay(pnls, total_return, sharpe, drawdown):
        return {
            "trades": [{"pnl": value} for value in pnls],
            "period_start": "2020-01-02T22:15:00+00:00", "period_end": "2026-11-09T20:45:00+00:00",
            "metrics": {"trade_count": len(pnls), "total_return": total_return, "annualized_sharpe": sharpe,
                        "max_drawdown": drawdown},
        }

    report = {
        "run_date_et": "2026-11-09", "status": "partial_data", "dataset_available": True, "dataset_path": "x",
        "strategies": {
            "vrp": {"underlyings": {
                "AAPL": {"exploratory_replay": replay([100.0, -40.0, 30.0], 0.09, 0.5, 0.05)},
                "MSFT": {"exploratory_replay": replay([-20.0], -0.02, -0.1, 0.02)},
                "XLI": {"exploratory_replay": replay([], 0.0, 0.0, 0.0)},
            }},
            "llm_driven": {"underlyings": {"AAPL": {"exploratory_replay": replay([500.0], 0.5, 1.0, 0.0)}}},
        },
    }
    summary = _simulation_summary(report)
    assert set(summary) == {"vrp"}
    vrp = summary["vrp"]
    assert vrp["underlyings"] == 2
    assert vrp["trades"] == 4
    assert vrp["net_pnl_usd"] == 70.0
    assert vrp["win_rate"] == 0.5
    assert vrp["profit_factor"] == round(130.0 / 60.0, 3)
    assert vrp["worst_drawdown"] == 0.05
    markdown = _render_markdown(report)
    assert "## Options simulation" in markdown
    assert "| Volatility risk premium | 2 | 4 | +70 |" in markdown
