"""Every equity lab preset passes the strategy validator, so no built-in profile is refused at run or apply time."""

import json
from pathlib import Path

from strategies.registry import validate_parameter_profile

PRESETS = json.loads(
    (Path(__file__).resolve().parents[3] / "server" / "algo-trader" / "profile-presets.json").read_text(encoding="utf-8")
)["equity"]


def test_every_equity_preset_passes_the_strategy_validator():
    for strategy, by_name in PRESETS.items():
        assert set(by_name) == {"standard", "conservative", "high-risk"}, strategy
        for name, values in by_name.items():
            validate_parameter_profile({strategy: values})
