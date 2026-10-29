"""Switching a strategy off for entries never removes its exit path for holdings."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from portfolio import config as portfolio_config
from strategies import rule_pipeline
from strategies.signals import Signal
from strategies.runner.llm_context import load_llm_context


@pytest.fixture
def default_config(monkeypatch, tmp_path):
    monkeypatch.setattr(portfolio_config, "ALGO_STRATEGY_CONFIG_PATH", tmp_path / "absent.json")


def _bars(count: int = 30) -> list[dict]:
    return [
        {"t": f"2026-10-{index + 1:02d}T15:55:00Z", "o": 100, "h": 101, "l": 99, "c": 100 - index * 0.1, "v": 1000}
        for index in range(count)
    ]


def test_disabled_intraday_strategy_still_emits_exits_for_a_holding(monkeypatch, default_config):
    calls = []

    def fake_evaluate(name, mode, ticker, bars, position_qty=0, **kwargs):
        calls.append((name, position_qty, kwargs.get("runtime_enabled_override")))
        return Signal(ticker, "sell", name, "stub exit", 0.8)

    monkeypatch.setattr(rule_pipeline, "evaluate_strategy", fake_evaluate)

    exit_signal = rule_pipeline.evaluate_rule_signal(
        "intraday_momentum", "AAPL", _bars(), parameters={}, position_qty=10,
        runtime_enabled_override=False,
    )

    assert exit_signal is not None and exit_signal.action == "sell"
    assert calls == [("intraday_momentum", 10, True)]


def test_disabled_intraday_strategy_emits_nothing_for_a_flat_name(monkeypatch, default_config):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("entries must stay blocked while the strategy is disabled")

    monkeypatch.setattr(rule_pipeline, "evaluate_strategy", fail_if_called)

    assert rule_pipeline.evaluate_rule_signal(
        "intraday_momentum", "AAPL", _bars(), parameters={}, position_qty=0,
        runtime_enabled_override=False,
    ) is None


def test_owned_position_keeps_an_exit_generator_when_the_snapshot_is_disabled(tmp_path):
    now = datetime(2026, 10, 27, 15, tzinfo=timezone.utc)
    flat = load_llm_context(
        enabled=False, owned_llm_position=False, llm_config=None,
        checked_at=lambda: now, signal_path=tmp_path / "missing.json",
    )
    owned = load_llm_context(
        enabled=False, owned_llm_position=True, llm_config=_config(),
        checked_at=lambda: now, signal_path=tmp_path / "missing.json",
    )

    assert flat.generator is None and flat.available is False
    assert owned.generator is not None and owned.available is False
    assert owned.signal_status == "disabled_by_config"


def _config():
    from strategies.llm_driven import LLMStrategyConfig

    return LLMStrategyConfig(enabled=True)
