"""The cron report runs a prompt through the configured LLM, skips the model when the snapshot is stale, and never leaks the bot token."""

from datetime import datetime, timedelta, timezone

import pytest

from scripts import llm_report
from strategies.llm_driven.ai import publish_llm_signal_snapshot

NOW = datetime(2026, 10, 29, 15, 0, tzinfo=timezone.utc)


class FakeModel:
    def __init__(self, reply="report"):
        self.reply = reply
        self.calls = []

    def invoke(self, messages):
        self.calls.append([message.content for message in messages])
        return type("Reply", (), {"content": self.reply})()


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_PROVIDER", "OpenAI")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET")
    monkeypatch.setenv("ALGO_REPORT_TELEGRAM_TO", "42")
    monkeypatch.setattr(llm_report, "DEFAULT_AI_SIGNAL_DB", tmp_path / "signals.sqlite3")
    monkeypatch.setattr(llm_report, "load_dotenv", lambda *args, **kwargs: None)
    sent = []
    monkeypatch.setattr(llm_report, "send_telegram", lambda text, token, chat_id: sent.append((text, token, chat_id)))
    return {"sent": sent, "db": tmp_path / "signals.sqlite3"}


def _publish(db, generated):
    publish_llm_signal_snapshot(db, {
        "schema_version": 1, "strategy": "swing_llm_driven", "run_id": "run-1",
        "generated_at": generated.isoformat(), "expires_at": (generated + timedelta(minutes=90)).isoformat(),
        "universe": ["AAPL"], "owned_tickers": [],
        "decisions": {"AAPL": {"action": "buy", "confidence": 80.0, "reason": "test", "run_id": "run-1", "generated_at": generated.isoformat()}},
    }, now=generated)


def test_fresh_snapshot_goes_to_the_model_with_the_prompt(monkeypatch, env, tmp_path):
    prompt = tmp_path / "digest.md"
    prompt.write_text("Brief the root {ROOT}.", encoding="utf-8")
    _publish(env["db"], datetime.now(timezone.utc))
    model = FakeModel("the briefing")
    monkeypatch.setattr(llm_report, "get_model", lambda name, provider, timeout_seconds: model)
    assert llm_report.main(["--prompt", str(prompt), "--snapshot", "--telegram"]) == 0
    user_message = model.calls[0][1]
    assert f"Brief the root {llm_report.ROOT}." in user_message
    assert '"AAPL"' in user_message
    assert env["sent"] == [("the briefing", "123:SECRET", "42")]


def test_stale_snapshot_sends_a_notice_and_calls_no_model(monkeypatch, env, tmp_path):
    prompt = tmp_path / "digest.md"
    prompt.write_text("Brief.", encoding="utf-8")
    model = FakeModel()
    monkeypatch.setattr(llm_report, "get_model", lambda *args, **kwargs: pytest.fail("the model must not be called"))
    assert llm_report.main(["--prompt", str(prompt), "--snapshot", "--telegram"]) == 0
    assert env["sent"][0][0].startswith("No briefing was produced. The LLM signal snapshot is missing")
    assert model.calls == []


def test_failed_model_call_reports_only_the_exception_type(monkeypatch, env, tmp_path, capsys):
    prompt = tmp_path / "digest.md"
    prompt.write_text("Brief.", encoding="utf-8")

    def broken(*args, **kwargs):
        raise TimeoutError("boom")

    monkeypatch.setattr(llm_report, "get_model", broken)
    assert llm_report.main(["--prompt", str(prompt), "--telegram"]) == 1
    assert env["sent"] == [("Report failed: TimeoutError. Check the model settings and the API key in .env.", "123:SECRET", "42")]
    assert "boom" in capsys.readouterr().err


def test_dry_run_prints_the_message_without_a_model(monkeypatch, env, tmp_path, capsys):
    context = tmp_path / "latest.json"
    context.write_text('{"symbols": 3}', encoding="utf-8")
    prompt = tmp_path / "research.md"
    prompt.write_text("Summarise.", encoding="utf-8")
    monkeypatch.setattr(llm_report, "get_model", lambda *args, **kwargs: pytest.fail("no model in a dry run"))
    assert llm_report.main(["--prompt", str(prompt), "--context", str(context), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Summarise." in out and '{"symbols": 3}' in out
    assert env["sent"] == []


def test_telegram_needs_a_token_and_destination(monkeypatch, env, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    prompt = tmp_path / "digest.md"
    prompt.write_text("Brief.", encoding="utf-8")
    assert llm_report.main(["--prompt", str(prompt), "--telegram"]) == 2


def test_long_replies_are_split_under_the_telegram_limit():
    text = "\n".join(f"line {index} " + "x" * 90 for index in range(200))
    chunks = llm_report.split_for_telegram(text)
    assert len(chunks) > 1
    assert all(len(chunk) <= llm_report.TELEGRAM_LIMIT for chunk in chunks)
    assert "".join(chunks) == text


def test_telegram_errors_do_not_include_the_token(monkeypatch):
    def refuse(request, timeout):
        raise OSError(f"could not reach {request.full_url}")

    monkeypatch.setattr(llm_report.urllib.request, "urlopen", refuse)
    with pytest.raises(RuntimeError) as caught:
        llm_report.send_telegram("hi", "123:SECRET", "42")
    assert "SECRET" not in str(caught.value)
