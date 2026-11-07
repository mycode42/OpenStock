"""Check profile values against the strategy validator, the same check a research run applies at start.

Reads a JSON object of {strategy: {parameter: value}} on stdin and prints {"errors": {strategy: message}}.
The research lab calls it before it starts a run or applies a plan, so an out-of-range value is refused up front.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strategies.registry import validate_parameter_profile  # noqa: E402


def main() -> int:
    overrides = json.load(sys.stdin)
    errors = {}
    for strategy, values in overrides.items():
        try:
            validate_parameter_profile({strategy: values})
        except ValueError as error:
            errors[strategy] = str(error)
    json.dump({"errors": errors}, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
