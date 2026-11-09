from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from portfolio.config import get_algo_configuration, get_mode_config
from strategies.runner import signal_generation

ET = ZoneInfo("America/New_York")


def test_no_automatic_eod_flatten_even_if_legacy_flag_is_true(monkeypatch):
    monkeypatch.setattr(signal_generation, "_strategy_candidates", lambda *args: [])
    monkeypatch.setattr(signal_generation, "_llm_signal", lambda *args: None)
    monkeypatch.setattr(signal_generation, "_protective_exit_option", lambda *args: [])
    context = SimpleNamespace(
        mode="algo",
        positions={"SPCX": {"qty": 1}},
        bars_by_symbol={"SPCX": []},
        owner_map={"SPCX": "intraday_trend_following"},
        track_owners=True,
        risk_config={"flatten_eod": True},
        session_name="regular",
        now=datetime(2026, 11, 9, 15, 50, tzinfo=ET),
        regular_close=datetime(2026, 11, 9, 16, 0, tzinfo=ET),
        coordinator_config=SimpleNamespace(intraday_close_cutoff_minutes=15),
        strategy_states={},
        signal_diagnostics=[],
    )

    assert signal_generation._generate_symbol_signals(context, "SPCX") == []


def test_live_algo_configuration_no_longer_exposes_eod_flatten():
    assert "flatten_eod" not in get_algo_configuration()["portfolio"]
    assert "flatten_eod" not in get_mode_config("algo")["risk"]
