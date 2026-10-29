"""Evaluates the shared rule file against editable Algo strategy settings.

The same rules are evaluated by OpenStock's settings editor (see
``portfolio/algo-strategy-rules.json``). Paths use dots. ``*`` matches every key
of a mapping and binds ``{key}`` (first wildcard) and ``{regime}`` (second) in
messages. A string operand names another setting path; a leading ``$`` is optional.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

RULES_PATH = Path(__file__).with_name("algo-strategy-rules.json")
_OPS = {
    "<=": lambda a, b: a <= b,
    "<": lambda a, b: a < b,
    ">=": lambda a, b: a >= b,
    ">": lambda a, b: a > b,
    "==": lambda a, b: a == b,
}


def load_rules(path: Path = RULES_PATH) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != "algo_strategy_rules_v1":
        raise ValueError(f"unexpected rule file schema in {path}")
    return list(data["rules"])


def _get(root: object, path: str):
    current = root
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _expand(root: dict, path: str, bindings: tuple[str, ...] = ()):
    """Yield (concrete_path, bindings) for every wildcard expansion of ``path``."""
    if "*" not in path.split("."):
        yield path, bindings
        return
    head, _, tail = path.partition(".*")
    container = _get(root, head) if head else root
    if not isinstance(container, dict):
        return
    for key in container:
        concrete = f"{head}.{key}" if head else str(key)
        rest = tail.lstrip(".")
        if rest:
            yield from _expand(root, f"{concrete}.{rest}", bindings + (str(key),))
        else:
            yield concrete, bindings + (str(key),)


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _operand(settings: dict, value):
    """A number is itself; a string names another setting path (a leading ``$`` is optional)."""
    if isinstance(value, str):
        return _get(settings, value.lstrip("$"))
    return value


def _message(rule: dict, path: str, bindings: tuple[str, ...]) -> str:
    text = rule["message"]
    if bindings:
        text = text.replace("{key}", bindings[0])
    if len(bindings) > 1:
        text = text.replace("{regime}", bindings[1])
    return text if "{" not in text else text.replace("{key}", path)


def _check(rule: dict, settings: dict, defaults: dict, path: str, bindings: tuple[str, ...]) -> bool:
    kind = rule["check"]
    value = _get(settings, path)
    if kind == "range":
        low, high = rule.get("min"), _operand(settings, rule.get("max"))
        if not _number(value):
            return False
        if low is not None and (value <= low if rule.get("min_exclusive") else value < low):
            return False
        if high is not None and (value >= high if rule.get("max_exclusive") else value > high):
            return False
        return True
    if kind == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            return False
        if rule.get("min") is not None and value < rule["min"]:
            return False
        return rule.get("max") is None or value <= rule["max"]
    if kind == "member":
        return value in rule["values"]
    if kind == "equals_default":
        return value == _get(defaults, path)
    if kind == "compare":
        left = _operand(settings, rule["left"])
        right = _operand(settings, rule["right"])
        return _number(left) and _number(right) and _OPS[rule["op"]](left, right)
    if kind == "sum_le":
        terms = [_operand(settings, term) for term in rule["terms"]]
        right = _operand(settings, rule["right"])
        return all(_number(term) for term in terms) and _number(right) and sum(terms) <= right
    raise ValueError(f"unknown rule check: {kind}")


def validate_settings(settings: dict, defaults: dict, rules: list[dict] | None = None) -> None:
    """Raise ValueError with the first failing rule's message."""
    for rule in rules if rules is not None else load_rules():
        paths = _expand(settings, rule["path"]) if "path" in rule else [(None, ())]
        for path, bindings in paths:
            if path is None:
                if not _check(rule, settings, defaults, "", bindings):
                    raise ValueError(_message(rule, "", bindings))
                continue
            if not _check(rule, settings, defaults, path, bindings):
                raise ValueError(_message(rule, path, bindings))
