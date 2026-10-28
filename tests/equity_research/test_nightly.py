from datetime import datetime, timezone
import json

from runtime.atomic import atomic_write_json
import equity_research.nightly as nightly


def test_nightly_runs_research_pipelines_sequentially_and_attaches_options_report(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(nightly, "ROOT", tmp_path)
    output_dir = tmp_path / "equity-report"
    options_path = tmp_path / "data/options/research/automation/latest.json"
    launched = []

    class FakeProcess:
        def __init__(self, command):
            self.command = command
            self.kind = "options" if "options.research.automation" in command else "equity"
            launched.append(self.kind)

        def wait(self):
            if self.kind == "options":
                assert launched == ["options"]
            else:
                assert launched == ["options", "equity"]
            now = datetime.now(timezone.utc).isoformat()
            if self.kind == "options":
                atomic_write_json(options_path, {
                    "schema": "swarm_options_nightly_research_v1",
                    "status": "waiting_for_data", "created_at": now,
                    "run_date_et": datetime.now(nightly.ET).date().isoformat(),
                    "dataset_available": False, "strategies": {},
                })
            else:
                atomic_write_json(output_dir / "latest.json", {
                    "schema": "test-equity-report", "status": "completed",
                    "run_date_et": datetime.now(nightly.ET).date().isoformat(),
                })
            return 0

    monkeypatch.setattr(nightly.subprocess, "Popen", lambda command, **kwargs: FakeProcess(command))

    exit_code = nightly.main(["--workers", "1", "--output-dir", str(output_dir)])

    assert exit_code == 0
    report = json.loads((output_dir / "latest.json").read_text(encoding="utf-8"))
    assert report["options_research"]["status"] == "waiting_for_data"
    assert report["parallel_research"]["concurrent"] is False
    assert launched == ["options", "equity"]
    summary = json.loads(capsys.readouterr().out)
    assert summary["options_status"] == "waiting_for_data"


def test_nightly_cycle_checkpoint_is_resumed_from_research_database(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from datetime import date

    from equity_research import nightly

    args = SimpleNamespace(
        db=str(tmp_path / "market.db"), output_dir=str(tmp_path), workers=1, no_refresh=False,
        factor_only=False, force=False, skip_options_research=False, new_run=False,
    )
    monkeypatch.setattr(nightly, "_cycle_date", lambda now=None: date(2026, 10, 27))

    state, cached = nightly._cycle_checkpoint(tmp_path, args)
    assert state["run_id"] == "nightly-20261027"
    assert cached is False
    state["status"] = "completed"
    state["final_result"] = {"exit_code": 0}
    nightly._checkpoint_store(tmp_path).write_nightly_checkpoint(state["run_id"], state)

    assert (tmp_path / "research.sqlite3").is_file()
    assert not (tmp_path / "parallel").exists()
    resumed, cached = nightly._cycle_checkpoint(tmp_path, args)
    assert cached is True
    assert resumed["final_result"] == {"exit_code": 0}
