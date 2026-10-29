import json

from options.research import lab_run


def _events(output):
    return [json.loads(line[len(lab_run.EVENT_PREFIX):]) for line in output.splitlines() if line.startswith(lab_run.EVENT_PREFIX)]


def test_lab_run_emits_progress_and_continues_after_one_underlying_fails(monkeypatch, capsys):
    calls = []

    def fake_run_research(strategy, *, config_path, underlying, parallel_workers):
        calls.append((strategy, underlying, parallel_workers))
        if underlying == "BAD":
            raise ValueError("insufficient history for BAD")
        return {"run_dir": f"/runs/{underlying}", "run_id": f"id-{underlying}", "status": "completed",
                "validation": {"approved": underlying == "AAPL"}}

    monkeypatch.setattr(lab_run, "run_research", fake_run_research)
    code = lab_run.main(["--config", "/tmp/job.json", "--strategy", "vrp", "--underlyings", "aapl,BAD,msft", "--workers", "4"])

    assert code == 0
    assert calls == [("vrp", "AAPL", 4), ("vrp", "BAD", 4), ("vrp", "MSFT", 4)]
    events = _events(capsys.readouterr().out)
    kinds = [item["event"] for item in events]
    assert kinds == [
        "started",
        "underlying_started", "underlying_done",
        "underlying_started", "underlying_error",
        "underlying_started", "underlying_done",
        "finished",
    ]
    done = [item for item in events if item["event"] == "underlying_done"]
    assert done[0]["approved"] is True and done[1]["approved"] is False
    assert events[-1]["failures"] == 1


def test_lab_run_rejects_unknown_strategy(capsys):
    try:
        lab_run.main(["--config", "/tmp/job.json", "--strategy", "llm_driven", "--underlyings", "AAPL"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("unsupported strategy must be refused")
