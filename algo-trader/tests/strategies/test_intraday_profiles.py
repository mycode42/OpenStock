"""Intraday lab profiles: every preset passes the registry validator, and standard is the registry default."""

import json
from pathlib import Path

from strategies.registry import BASE_ALGO_DEFAULT_PARAMETERS, validate_parameter_profile

PRESETS = json.loads(
    (Path(__file__).resolve().parents[3] / "server" / "algo-trader" / "profile-presets.json").read_text(encoding="utf-8")
)["equity"]
INTRADAY = ["intraday_trend_following", "intraday_momentum", "intraday_mean_reversion"]


def test_every_intraday_preset_passes_the_registry_validator():
    for strategy in INTRADAY:
        assert set(PRESETS[strategy]) == {"standard", "conservative", "high-risk"}
        for name, values in PRESETS[strategy].items():
            validate_parameter_profile({strategy: values})


def test_intraday_standard_is_the_registry_default():
    for strategy in INTRADAY:
        base = BASE_ALGO_DEFAULT_PARAMETERS[strategy]
        for key, value in PRESETS[strategy]["standard"].items():
            assert base[key] == value, (strategy, key)
