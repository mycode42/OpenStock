from datetime import datetime, timezone
import json

from runtime.atomic import atomic_write_json
import autoresearch.nightly as nightly


def test_nightly_starts_both_research_pipelines_and_attaches_separate_options_report(monkeypatch, tmp_path, capsys):
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
    assert report["parallel_research"]["concurrent"] is True
    assert launched == ["options", "equity"]
    summary = json.loads(capsys.readouterr().out)
    assert summary["options_status"] == "waiting_for_data"
