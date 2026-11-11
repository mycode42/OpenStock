from reporting import snapshot_store
from options import paper


def _closes(monkeypatch, equities):
    rows = [{"equity": value} for value in equities]
    monkeypatch.setattr(snapshot_store, "load", lambda *args, **kwargs: rows)


def test_drawdown_is_measured_from_the_highest_recent_close(monkeypatch):
    _closes(monkeypatch, [100_000, 110_000, 104_000])
    assert paper._rolling_drawdown_pct("acct", 99_000, 20) == 1 - 99_000 / 110_000


def test_only_the_last_sessions_count(monkeypatch):
    _closes(monkeypatch, [200_000] + [100_000] * 20)
    assert paper._rolling_drawdown_pct("acct", 100_000, 20) == 0.0


def test_no_history_means_no_drawdown_gate(monkeypatch):
    _closes(monkeypatch, [])
    assert paper._rolling_drawdown_pct("acct", 100_000, 20) is None


def test_a_new_high_today_is_not_a_drawdown(monkeypatch):
    _closes(monkeypatch, [100_000, 105_000])
    assert paper._rolling_drawdown_pct("acct", 110_000, 20) == 0.0
