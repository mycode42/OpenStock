import json

from options.research import simulator


def _events(capsys):
    return [json.loads(line[len("LAB_EVENT "):]) for line in capsys.readouterr().out.splitlines() if line.startswith("LAB_EVENT ")]


def test_replay_progress_is_silent_outside_lab_runs(monkeypatch, capsys):
    monkeypatch.delenv("ALGO_LAB_PROGRESS", raising=False)
    progress = simulator._ReplayProgress(10)
    progress.tick(0, None)
    progress.tick(10, None)
    assert _events(capsys) == []


def test_lab_replay_reports_fraction_under_its_label(monkeypatch, capsys):
    monkeypatch.setenv("ALGO_LAB_PROGRESS", "1")
    monkeypatch.setenv("ALGO_REPLAY_LABEL", "chosen")
    progress = simulator._ReplayProgress(4)
    progress.tick(0, None)
    progress.tick(4, None)  # the last session always reports, even inside the throttle window
    events = _events(capsys)
    assert events[0]["event"] == "replay_progress"
    assert events[0]["label"] == "chosen"
    assert events[-1]["sessions_done"] == 4 and events[-1]["fraction"] == 1.0
