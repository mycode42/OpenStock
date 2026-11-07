"""The lab's equity profile keys must be keys the strategy validator accepts, or a profile run is refused."""

import json
from pathlib import Path

from strategies.registry import PARAMETER_KEYS

KEYS = json.loads((Path(__file__).resolve().parents[3] / "server" / "algo-trader" / "equity-profile-keys.json").read_text(encoding="utf-8"))


def test_every_profile_key_is_accepted_by_the_strategy_validator():
    for strategy, keys in KEYS.items():
        if strategy.startswith("_"):
            continue
        assert set(keys) <= PARAMETER_KEYS.get(strategy, set()), strategy
