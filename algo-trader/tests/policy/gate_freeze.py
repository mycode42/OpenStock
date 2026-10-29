"""Extracts every rejection and suppression message the trading code can emit.

The set of messages is the set of gates. ``tests/policy/test_gate_freeze.py``
compares it with ``frozen_gates.json``; any change there is a policy change and
needs an explicit approval, recorded by regenerating the snapshot.
"""

from __future__ import annotations

import pathlib
import re

ALGO_ROOT = pathlib.Path(__file__).resolve().parents[2]
OPENSTOCK_ROOT = ALGO_ROOT.parent
PY_DIRS = ("strategies", "execution", "options", "portfolio")
PY_PATTERNS = (
    re.compile(r'ValidationResult\(\s*(?:approved=)?False,\s*(?:reason=)?f?"([^"]*)"'),
    re.compile(r'ValidationResult\(\s*approved=False,\s*reason=f?"([^"]*)"'),
    re.compile(r'_reject\(\s*(?:proposal|signal),\s*f?"([^"]*)"'),
    re.compile(r'_reject\(\s*f?"([^"]*)"\s*,\s*signal'),
    re.compile(r'suppressed_reason"\]\s*=\s*f?"([^"]*)"'),
    re.compile(r'"error":\s*f?"([^"]*)"'),
)
JS_PATTERN = re.compile(r"orderError\(\s*[`'\"]([^`'\"]*)[`'\"]")
JS_FILES = ("lib/alpaca-orders.js", "server/routes/alpaca.js")


def _normalize(message: str) -> str:
    return re.sub(r"\s+", " ", message).strip()


def extract() -> list[str]:
    found: set[str] = set()
    for directory in PY_DIRS:
        for path in (ALGO_ROOT / directory).rglob("*.py"):
            if "tests" in path.parts or "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            for pattern in PY_PATTERNS:
                for match in pattern.finditer(text):
                    found.add(_normalize(match.group(1)))
    for relative in JS_FILES:
        text = (OPENSTOCK_ROOT / relative).read_text(encoding="utf-8")
        for match in JS_PATTERN.finditer(text):
            found.add(_normalize(match.group(1)))
    return sorted(found)
