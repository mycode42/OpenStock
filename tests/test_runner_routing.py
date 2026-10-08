"""Legacy analysis runners must propagate canonical executor failures."""

from types import SimpleNamespace

import pytest

import run_analysis
import run_hedge_fund


DECISIONS = {"AAPL": {"action": "buy", "quantity": 1, "reasoning": "test"}}


@pytest.mark.parametrize("module,helper", [
    (run_analysis, "_execute_via_canonical_executor"),
    (run_hedge_fund, "_execute_via_execute_trades"),
])
def test_analysis_runner_propagates_executor_failure(monkeypatch, module, helper):
    calls = []

    def failed_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=2, stdout='{"status":"failed"}', stderr="flatten incomplete")

    monkeypatch.setattr(module.subprocess, "run", failed_run)

    with pytest.raises(RuntimeError, match="exited 2"):
        getattr(module, helper)(DECISIONS, mode="day", dry_run=False)
    assert calls[0][0][-2:] == ["--mode", "day"]


def test_legacy_analysis_maps_submission_without_claiming_fill(monkeypatch):
    output = '{"results":[{"ticker":"AAPL","action":"buy","qty":1,"status":"submitted","broker_status":"pending_new","order_id":"one"}]}'
    monkeypatch.setattr(run_analysis.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout=output, stderr="",
    ))

    rows = run_analysis._execute_via_canonical_executor(DECISIONS, mode="swing", dry_run=False)

    assert rows[0]["success"]
    assert rows[0]["status"] == "submitted"
    assert rows[0]["broker_status"] == "pending_new"
