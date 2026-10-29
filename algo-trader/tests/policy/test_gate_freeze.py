"""The gate set is frozen. Changing it is a policy change, so it fails until approved.

To approve a gate change: edit ``frozen_gates.json`` in the same commit, with the
reason in the commit message and in ``docs/policy-frozen.md``.
"""

import json
from pathlib import Path

from tests.policy.gate_freeze import extract

FROZEN = Path(__file__).with_name("frozen_gates.json")


def test_gate_messages_match_the_frozen_policy():
    frozen = set(json.loads(FROZEN.read_text(encoding="utf-8"))["gates"])
    current = set(extract())
    added = sorted(current - frozen)
    removed = sorted(frozen - current)
    assert not added and not removed, (
        "Gate set changed without approval.\n"
        f"Added: {added}\nRemoved: {removed}\n"
        "If this is an approved policy change, update frozen_gates.json and docs/policy-frozen.md."
    )
