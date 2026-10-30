"""The ThetaData pipeline runs EOD then opening, completes once, never calls ThetaData after completion, and redacts the key."""

import json
from datetime import date

import pytest

from scripts.options import theta_pipeline as pipeline

END = date(2026, 10, 29)
SYMBOLS = ["AAA", "BBB"]
KEY = "theta-secret-123"


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def world(monkeypatch, tmp_path):
    research = tmp_path / "theta"
    monkeypatch.setattr(pipeline, "RESEARCH_DIR", research)
    monkeypatch.setattr(pipeline, "STATUS_PATH", research / "pipeline_status.json")
    monkeypatch.setattr(pipeline, "LOCK_PATH", research / "theta_backfill.lock")
    monkeypatch.setattr(pipeline, "_research_symbols", lambda: list(SYMBOLS))
    monkeypatch.setattr(pipeline, "_read_api_key", lambda: KEY)
    monkeypatch.setattr(pipeline, "_theta_client", lambda key: object())
    calls = {"eod": 0, "opening": 0, "telegram": []}
    monkeypatch.setattr(pipeline, "_notify", lambda text: calls["telegram"].append(text))

    def fake_eod(symbols, start, end, *, recent_sessions, on_recent_complete):
        calls["eod"] += 1
        assert recent_sessions == pipeline.RECENT_SESSIONS
        on_recent_complete()
        for symbol in symbols:
            _write(research / symbol / "manifest.json", {"archive_complete": True, "archive_end_date": END.isoformat()})
        return 0

    def fake_opening(client, symbol, *, start_date, end_date):
        calls["opening"] += 1
        _write(research / symbol / "opening_manifest.json", {"archive_complete": True, "archive_end_date": END.isoformat()})
        return {}

    monkeypatch.setattr(pipeline, "_run_symbols", fake_eod)
    monkeypatch.setattr(pipeline.opening, "_fetch_symbol", fake_opening)
    return {"research": research, "calls": calls}


def test_eod_runs_before_opening_and_completion_is_announced_once(world):
    assert pipeline.run(end_date=END) == 0
    status = json.loads((world["research"] / "pipeline_status.json").read_text())
    assert status["phase"] == "complete"
    assert status["recent_pass_complete_at"]
    assert world["calls"]["eod"] == 1 and world["calls"]["opening"] == len(SYMBOLS)
    assert len(world["calls"]["telegram"]) == 1
    assert "cancel the ThetaData subscription" in world["calls"]["telegram"][0]
    assert "remove THETA_DATA_API_KEY" in world["calls"]["telegram"][0]


def test_after_completion_no_theta_call_is_made(world, monkeypatch):
    pipeline.run(end_date=END)
    monkeypatch.setattr(pipeline, "_run_symbols", lambda *a, **k: pytest.fail("ThetaData must not be called after completion"))
    monkeypatch.setattr(pipeline, "_read_api_key", lambda: pytest.fail("the key must not be read after completion"))
    assert pipeline.run(end_date=END.replace(day=30)) == 0
    assert len(world["calls"]["telegram"]) == 1


def test_a_lagging_archive_end_still_counts_as_complete(world):
    """ThetaData publishes EOD a few sessions late; completion means the planned range is archived."""
    pipeline.run(end_date=END)
    lagging = world["research"] / "AAA" / "manifest.json"
    payload = json.loads(lagging.read_text())
    payload["archive_end_date"] = "2026-10-26"
    lagging.write_text(json.dumps(payload))
    assert pipeline.eod_complete("AAA") is True


def test_a_failed_stage_stops_the_run_and_is_announced_once_per_message(world, monkeypatch):
    monkeypatch.setattr(pipeline, "_run_symbols", lambda *a, **k: 1)
    assert pipeline.run(end_date=END) == 1
    assert pipeline.run(end_date=END) == 1
    status = json.loads((world["research"] / "pipeline_status.json").read_text())
    assert status["phase"] == "eod_recent"
    assert status["last_error"].startswith("EOD backfill is not complete yet")
    assert len(world["calls"]["telegram"]) == 1
    assert world["calls"]["opening"] == 0


def test_a_failed_completion_notice_is_retried_without_calling_theta(world, monkeypatch):
    monkeypatch.setattr(pipeline, "_notify", lambda text: "telegram failed: URLError")
    pipeline.run(end_date=END)
    status = json.loads((world["research"] / "pipeline_status.json").read_text())
    assert status["phase"] == "complete" and status["notify_pending"] is True

    sent = []
    monkeypatch.setattr(pipeline, "_notify", lambda text: sent.append(text))
    monkeypatch.setattr(pipeline, "_run_symbols", lambda *a, **k: pytest.fail("no ThetaData call during a notice retry"))
    assert pipeline.run(end_date=END) == 0
    assert len(sent) == 1
    final = json.loads((world["research"] / "pipeline_status.json").read_text())
    assert "notify_pending" not in final and final["completion_notified_at"]


def test_error_messages_never_contain_the_key(world, monkeypatch):
    def leaky(*args, **kwargs):
        raise RuntimeError(f"auth failed for {KEY}")

    monkeypatch.setattr(pipeline, "_run_symbols", leaky)
    pipeline.run(end_date=END)
    status = json.loads((world["research"] / "pipeline_status.json").read_text())
    assert KEY not in status["last_error"]
    assert "[redacted]" in status["last_error"]
    assert all(KEY not in text for text in world["calls"]["telegram"])
