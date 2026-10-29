"""The shared Algo settings rules: Python applies the same cases as OpenStock."""

import copy
import json
from pathlib import Path

import pytest

from portfolio.settings_rules import load_rules, validate_settings
from portfolio.config import ALGO_STRATEGY_DEFAULTS_PATH

CASES = json.loads((Path(__file__).parent / "algo-strategy-rule-cases.json").read_text(encoding="utf-8"))["cases"]


def _apply(settings: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    target = settings
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_shared_case(case):
    defaults = json.loads(ALGO_STRATEGY_DEFAULTS_PATH.read_text(encoding="utf-8"))
    settings = copy.deepcopy(defaults)
    for dotted, value in case["set"].items():
        _apply(settings, dotted, value)
    if case["error"] is None:
        validate_settings(settings, defaults)
    else:
        with pytest.raises(ValueError) as raised:
            validate_settings(settings, defaults)
        assert str(raised.value) == case["error"]


def test_rule_file_has_no_duplicate_ids_and_known_checks():
    known = {"range", "integer", "member", "equals_default", "compare", "sum_le"}
    rules = load_rules()
    assert rules and all(rule["check"] in known for rule in rules)
