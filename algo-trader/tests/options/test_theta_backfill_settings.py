import importlib
import os

import pytest


def _reload_with(monkeypatch, **env):
    for key in ("THETA_STRIKE_RANGE", "THETA_DATABASE_SYNC", "THETA_RESEARCH_DIR"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import scripts.options.theta_historical_backfill as module
    return importlib.reload(module)


def test_defaults_collect_every_strike_into_the_max_folder_and_write_the_database(monkeypatch):
    module = _reload_with(monkeypatch)
    assert module.STRIKE_RANGE is None
    assert module.DATABASE_SYNC is True
    assert module.RESEARCH_DIR.name == "theta-max"


@pytest.mark.parametrize("value", ["20", "60"])
def test_a_numeric_strike_window_is_still_honoured(monkeypatch, value):
    module = _reload_with(monkeypatch, THETA_STRIKE_RANGE=value)
    assert module.STRIKE_RANGE == int(value)


def test_the_database_write_can_be_switched_off(monkeypatch):
    module = _reload_with(monkeypatch, THETA_DATABASE_SYNC="0")
    assert module.DATABASE_SYNC is False
    _reload_with(monkeypatch)  # restore defaults for other tests
